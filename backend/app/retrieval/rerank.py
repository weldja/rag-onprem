"""Optional cross-encoder reranking (fastembed / ONNX Runtime, CPU).

The model is downloaded into the api image at BUILD time (RERANKER_MODEL build
arg) and loaded here with HF_HUB_OFFLINE=1 and local_files_only, so a missing
model is a startup error, never a download.
"""

import logging
import os
from dataclasses import replace
from typing import Protocol

from app.retrieval.search import RetrievedChunk

log = logging.getLogger(__name__)


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class RerankerUnavailableError(RuntimeError):
    pass


class FastEmbedReranker:
    def __init__(self, model_name: str, cache_dir: str) -> None:
        # Also set in the image; repeated here so the guarantees hold however
        # the process is started. Must happen before onnxruntime is imported.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        try:
            import onnxruntime

            onnxruntime.disable_telemetry_events()
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(
                model_name=model_name, cache_dir=cache_dir, local_files_only=True
            )
        except Exception as exc:
            raise RerankerUnavailableError(
                f"Reranker '{model_name}' isn't in the image ({exc}). Rebuild the api image "
                "with the RERANKER_MODEL build arg, or set RERANKER_MODEL empty to disable."
            ) from exc
        self.model_name = model_name

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, texts)]


def rerank(
    reranker: Reranker,
    query: str,
    hits: list[RetrievedChunk],
    *,
    k: int,
    min_score: float | None,
) -> list[RetrievedChunk]:
    """Reorder by cross-encoder score and keep the top k (optionally dropping
    anything below min_score). The cosine `score` is kept for display."""
    if not hits:
        return []
    scores = reranker.score(query, [h.text for h in hits])
    ranked = sorted(
        (replace(h, rerank_score=s) for h, s in zip(hits, scores, strict=True)),
        key=lambda h: h.rerank_score if h.rerank_score is not None else 0.0,
        reverse=True,
    )
    if min_score is not None:
        ranked = [h for h in ranked if (h.rerank_score or 0.0) >= min_score]
    return ranked[:k]
