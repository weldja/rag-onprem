"""Vector search over chunks. Milestone 2 adds the keyword leg and RRF fusion."""

from dataclasses import dataclass

from sqlalchemy import Engine, select, text

from app.db.tables import chunks, documents


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    filename: str
    page: int | None
    text: str
    score: float  # cosine similarity, higher is closer


def vector_search(
    engine: Engine, query_vector: list[float], *, embed_model: str, k: int
) -> list[RetrievedChunk]:
    distance = chunks.c.embedding.cosine_distance(query_vector)
    stmt = (
        select(
            chunks.c.id,
            chunks.c.document_id,
            documents.c.filename,
            chunks.c.page,
            chunks.c.text,
            (1 - distance).label("score"),
        )
        .join(documents, documents.c.id == chunks.c.document_id)
        .where(chunks.c.embed_model == embed_model)
        .order_by(distance)
        .limit(k)
    )
    with engine.begin() as conn:
        # pgvector >= 0.8: keep scanning the HNSW index when the WHERE filter
        # discards candidates, instead of returning fewer than k rows.
        conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        rows = conn.execute(stmt).all()
    results = [
        RetrievedChunk(
            chunk_id=r.id,
            document_id=r.document_id,
            filename=r.filename,
            page=r.page,
            text=r.text,
            score=float(r.score),
        )
        for r in rows
    ]
    # relaxed_order can return rows slightly out of order.
    results.sort(key=lambda c: c.score, reverse=True)
    return results
