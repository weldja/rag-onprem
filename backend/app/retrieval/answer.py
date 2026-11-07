"""Question -> retrieve -> prompt -> cited answer.

Context building (excerpts wrapped in <document> tags, with any literal
</document> neutralised so file content can't break out of the data region)
is adapted from rag-claude's ask_stream. Citations are numbered [n] ids that
map back to the retrieved chunks.
"""

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.llm.base import ChatMessage, Embedder, LLMClient
from app.llm.prompts import NOT_FOUND_ANSWER, is_not_found_answer, load_prompt
from app.retrieval.search import RetrievedChunk

log = logging.getLogger(__name__)

Retriever = Callable[[list[float]], list[RetrievedChunk]]

_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_SNIPPET_CHARS = 300


@dataclass(frozen=True)
class Source:
    id: int  # the [n] used in the answer
    document: str
    document_id: int
    page: int | None
    chunk_id: int
    snippet: str
    score: float


@dataclass(frozen=True)
class Answer:
    answer: str
    found: bool
    sources: list[Source]


def build_context(hits: list[RetrievedChunk]) -> str:
    parts = []
    for n, hit in enumerate(hits, start=1):
        source = html.escape(hit.filename, quote=True)
        page = f' page="{hit.page}"' if hit.page is not None else ""
        body = hit.text.replace("</document>", "<\\/document>")
        parts.append(f'<document id="{n}" source="{source}"{page}>\n{body}\n</document>')
    return "\n\n".join(parts)


def parse_citations(text: str, n_sources: int) -> list[int]:
    """Distinct cited ids in order of first appearance, ignoring out-of-range ids."""
    seen: list[int] = []
    for match in _CITATION.finditer(text):
        for raw in re.split(r"[,;]", match.group(1)):
            n = int(raw)
            if 1 <= n <= n_sources and n not in seen:
                seen.append(n)
    return seen


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _SNIPPET_CHARS else flat[: _SNIPPET_CHARS - 1].rstrip() + "…"


def answer_question(
    question: str,
    *,
    retrieve: Retriever,
    llm: LLMClient,
    embedder: Embedder,
    settings: Settings,
) -> Answer:
    query_vector = embedder.embed([question])[0]
    hits = [h for h in retrieve(query_vector) if h.score >= settings.min_similarity]
    log.debug("Retrieved %d chunks above similarity %.2f", len(hits), settings.min_similarity)
    if not hits:
        return Answer(NOT_FOUND_ANSWER, found=False, sources=[])

    messages: list[ChatMessage] = [
        {"role": "system", "content": load_prompt("answer_system")},
        {
            "role": "user",
            "content": load_prompt("answer_user").format(
                context=build_context(hits), question=question
            ),
        },
    ]
    text = llm.chat(messages).strip()
    if not text or is_not_found_answer(text):
        return Answer(NOT_FOUND_ANSWER, found=False, sources=[])

    sources = [
        Source(
            id=n,
            document=hits[n - 1].filename,
            document_id=hits[n - 1].document_id,
            page=hits[n - 1].page,
            chunk_id=hits[n - 1].chunk_id,
            snippet=_snippet(hits[n - 1].text),
            score=round(hits[n - 1].score, 4),
        )
        for n in parse_citations(text, len(hits))
    ]
    if not sources:
        log.warning("LLM answer contained no valid citations")
    return Answer(text, found=True, sources=sources)
