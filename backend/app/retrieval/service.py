"""Per-request search service: binds the viewer, settings and optional
reranker to the retrieval functions. The API depends on the Search protocol so
tests can substitute a fake."""

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import Engine

from app.auth.acl import Viewer
from app.core.config import Settings
from app.retrieval.answer import Retriever
from app.retrieval.rerank import Reranker, rerank
from app.retrieval.search import RetrievedChunk, document_spread, hybrid_search


class Search(Protocol):
    def retriever(self, document_ids: Sequence[int] | None = None) -> Retriever:
        """Question -> relevant chunks (optionally limited to some documents)."""
        ...

    def spread(self, document_ids: Sequence[int]) -> list[RetrievedChunk]:
        """Chunks sampled across whole documents, for summaries."""
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
        )
