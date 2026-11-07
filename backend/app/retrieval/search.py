"""Hybrid retrieval: pgvector cosine search + Postgres full-text search,
merged with reciprocal rank fusion. Keyword search and RRF are adapted from
rag-claude (_keyword_candidates, _rrf_merge).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, func, literal_column, select, text

from app.auth.acl import Viewer, can_see
from app.db.tables import chunks, documents


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    filename: str
    page: int | None
    text: str
    score: float  # cosine similarity to the question, higher is closer
    section: str | None = None
    rerank_score: float | None = None  # cross-encoder score, when a reranker ran


def to_or_tsquery(question: str, max_terms: int = 40) -> str | None:
    """Keywords OR-ed together. Tokens are reduced to letters and digits, so no
    user text can inject tsquery syntax. Requiring every term would usually
    match nothing."""
    tokens: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", question.lower()):
        if len(tok) > 1 and tok not in tokens:
            tokens.append(tok)
    return " | ".join(tokens[:max_terms]) or None


def rrf_merge(
    ranked_lists: Sequence[Sequence[RetrievedChunk]], k_const: int = 60
) -> list[RetrievedChunk]:
    """Reciprocal rank fusion: score = sum of 1/(k + rank). Rank-based, so cosine
    similarity and ts_rank never need to share a scale."""
    scores: dict[int, float] = {}
    by_id: dict[int, RetrievedChunk] = {}
    for results in ranked_lists:
        for rank, hit in enumerate(results, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k_const + rank)
            by_id.setdefault(hit.chunk_id, hit)
    return [by_id[cid] for cid in sorted(scores, key=lambda c: scores[c], reverse=True)]


def select_hits(
    vector_hits: Sequence[RetrievedChunk],
    keyword_hits: Sequence[RetrievedChunk],
    *,
    k: int,
    min_similarity: float,
    keyword_keep: int,
) -> list[RetrievedChunk]:
    """Fuse both legs and apply the relevance gate.

    - If nothing is semantically close (best similarity < min_similarity), the
      question is off-topic: return nothing, whatever the keyword leg matched.
    - Otherwise keep chunks above min_similarity, plus the top `keyword_keep`
      keyword hits (exact terms such as codes and names that embeddings miss).
    """
    best = max((h.score for h in [*vector_hits, *keyword_hits]), default=-1.0)
    if best < min_similarity:
        return []
    keyword_ids = {h.chunk_id for h in keyword_hits[:keyword_keep]}
    fused = rrf_merge([vector_hits, keyword_hits])
    kept: list[RetrievedChunk] = []
    seen_text: set[str] = set()
    for h in fused:
        if h.score < min_similarity and h.chunk_id not in keyword_ids:
            continue
        # The same text in two documents (e.g. a re-saved copy of a file) would
        # spend two of the model's few context slots on one fact.
        normalised = " ".join(h.text.split()).lower()
        if normalised in seen_text:
            continue
        seen_text.add(normalised)
        kept.append(h)
        if len(kept) == k:
            break
    return kept


def _columns(similarity: Any) -> list[Any]:
    return [
        chunks.c.id,
        chunks.c.document_id,
        documents.c.filename,
        chunks.c.page,
        chunks.c.section,
        chunks.c.text,
        similarity,
    ]


def _to_chunk(row: object) -> RetrievedChunk:
    r = row._mapping  # type: ignore[attr-defined]
    return RetrievedChunk(
        chunk_id=r["id"],
        document_id=r["document_id"],
        filename=r["filename"],
        page=r["page"],
        section=r["section"],
        text=r["text"],
        score=float(r["score"]),
    )


def hybrid_search(
    engine: Engine,
    question: str,
    query_vector: list[float],
    *,
    viewer: Viewer,
    embed_model: str,
    k: int,
    candidates: int,
    min_similarity: float,
    keyword_keep: int,
    document_ids: Sequence[int] | None = None,
) -> list[RetrievedChunk]:
    """Relevant chunks, best first, limited to what `viewer` may see (and to
    `document_ids` when given). [] when the question is off-topic."""
    distance = chunks.c.embedding.cosine_distance(query_vector)
    similarity = (1 - distance).label("score")
    # Access control lives in the WHERE clause of both legs: chunks the viewer
    # can't see never leave the database.
    ready = (
        (documents.c.status == "ready") & (chunks.c.embed_model == embed_model) & can_see(viewer)
    )
    if document_ids:
        ready = ready & documents.c.id.in_(list(document_ids))

    vector_stmt = (
        select(*_columns(similarity))
        .join(documents, documents.c.id == chunks.c.document_id)
        .where(ready)
        .order_by(distance)
        .limit(candidates)
    )

    tsquery = to_or_tsquery(question)
    keyword_stmt = None
    if tsquery:
        q = func.to_tsquery(literal_column("'english'"), tsquery)
        keyword_stmt = (
            select(*_columns(similarity))
            .join(documents, documents.c.id == chunks.c.document_id)
            .where(ready, chunks.c.tsv.bool_op("@@")(q))
            .order_by(func.ts_rank_cd(chunks.c.tsv, q).desc())
            .limit(candidates)
        )

    with engine.begin() as conn:
        # pgvector >= 0.8: keep scanning the HNSW index when the WHERE filter
        # discards candidates, instead of returning fewer than requested.
        conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        vector_hits = [_to_chunk(r) for r in conn.execute(vector_stmt)]
        keyword_hits = (
            [_to_chunk(r) for r in conn.execute(keyword_stmt)] if keyword_stmt is not None else []
        )

    # relaxed_order can return rows slightly out of order.
    vector_hits.sort(key=lambda h: h.score, reverse=True)
    return select_hits(
        vector_hits,
        keyword_hits,
        k=k,
        min_similarity=min_similarity,
        keyword_keep=keyword_keep,
    )


def spread_positions(n: int, k: int) -> list[int]:
    """k indexes spread evenly over range(n), always including the first and last."""
    if n <= k:
        return list(range(n))
    if k == 1:
        return [0]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def document_spread(
    engine: Engine,
    document_ids: Sequence[int],
    *,
    viewer: Viewer,
    embed_model: str,
    k_per_document: int,
) -> list[RetrievedChunk]:
    """Chunks sampled evenly across each document, in reading order, for
    summaries, where similarity search would only find one corner of the text.
    Ported in spirit from rag-claude's _retrieve_document_spread."""
    if not document_ids:
        return []
    visible = (
        documents.c.id.in_(list(document_ids))
        & (documents.c.status == "ready")
        & (chunks.c.embed_model == embed_model)
        & can_see(viewer)
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(chunks.c.id, chunks.c.document_id)
            .join(documents, documents.c.id == chunks.c.document_id)
            .where(visible)
            .order_by(chunks.c.document_id, chunks.c.ordinal)
        ).all()
        by_doc: dict[int, list[int]] = {}
        for r in rows:
            by_doc.setdefault(r.document_id, []).append(r.id)
        chosen = [
            ids[i]
            for doc_id in document_ids
            if (ids := by_doc.get(doc_id))
            for i in spread_positions(len(ids), k_per_document)
        ]
        if not chosen:
            return []
        detail = {
            r.id: r
            for r in conn.execute(
                select(*_columns(literal_column("1.0").label("score")))
                .join(documents, documents.c.id == chunks.c.document_id)
                .where(chunks.c.id.in_(chosen))
            )
        }
    return [_to_chunk(detail[cid]) for cid in chosen]
