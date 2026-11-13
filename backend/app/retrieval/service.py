"""Per-request search service: binds the viewer, settings and optional
reranker to the retrieval functions. The API depends on the Search protocol so
tests can substitute a fake."""

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import Engine

from app.auth.acl import Viewer
from app.core.config import Settings
from app.retrieval.answer import Retriever
from app.retrieval.library import LibraryStats, library_stats
from app.retrieval.rerank import Reranker, rerank
from app.retrieval.search import (
    RetrievedChunk,
    document_names,
    document_spread,
    hybrid_search,
)


class Search(Protocol):
    def retriever(self, document_ids: Sequence[int] | None = None) -> Retriever:
        """Question -> relevant chunks (optionally limited to some documents)."""
        ...

    def spread(self, document_ids: Sequence[int]) -> list[RetrievedChunk]:
        """Chunks sampled across whole documents, for summaries."""
        ...

    def names(self, document_ids: Sequence[int]) -> list[str]:
        """File names of the documents the viewer can see."""
        ...

    def library(self, document_ids: Sequence[int] | None = None) -> LibraryStats:
        """Counts of the documents the viewer can see (within the scope, if any)."""
        ...


class DbSearch:
    def __init__(
        self, engine: Engine, viewer: Viewer, settings: Settings, reranker: Reranker | None
    ) -> None:
        self._engine = engine
        self._viewer = viewer
        self._s = settings
        self._reranker = reranker

    def retriever(self, document_ids: Sequence[int] | None = None) -> Retriever:
        s = self._s

        def retrieve(question: str, vector: list[float]) -> list[RetrievedChunk]:
            hits = hybrid_search(
                self._engine,
                question,
                vector,
                viewer=self._viewer,
                embed_model=s.embed_model,
                # With a reranker, fuse a wider pool and let it pick the top k.
                k=s.rerank_candidates if self._reranker else s.top_k,
                candidates=s.retrieval_candidates,
                min_similarity=s.min_similarity,
                keyword_keep=s.keyword_keep,
                leg_keep=s.leg_keep,
                document_ids=document_ids,
            )
            if self._reranker is None:
                return hits
            return rerank(self._reranker, question, hits, k=s.top_k, min_score=s.rerank_min_score)

        return retrieve

    def spread(self, document_ids: Sequence[int]) -> list[RetrievedChunk]:
        per_doc = max(2, self._s.summary_chunks // max(1, len(document_ids)))
        return document_spread(
            self._engine,
            document_ids,
            viewer=self._viewer,
            embed_model=self._s.embed_model,
            k_per_document=per_doc,
            # No more text than per_doc full-size chunks: a short document is
            # read whole without making the summary any slower.
            max_chars_per_document=per_doc * self._s.chunk_size,
        )

    def names(self, document_ids: Sequence[int]) -> list[str]:
        return document_names(self._engine, document_ids, viewer=self._viewer)

    def library(self, document_ids: Sequence[int] | None = None) -> LibraryStats:
        return library_stats(self._engine, self._viewer, document_ids)
