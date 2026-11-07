"""Question -> retrieve -> prompt -> cited answer (whole, or streamed as events).

Context building (excerpts wrapped in <document> tags, with any literal
</document> neutralised so file content can't break out of the data region)
is adapted from rag-claude's ask_stream. Citations are numbered [n] ids that
map back to the retrieved chunks.
"""

import html
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any

from app.llm.base import ChatMessage, Embedder, LLMClient
from app.llm.ollama import OllamaError
from app.llm.prompts import NOT_FOUND_ANSWER, is_not_found_answer, load_prompt
from app.retrieval.search import RetrievedChunk

log = logging.getLogger(__name__)

# (question, query_vector) -> relevant chunks, best first; [] when nothing is relevant.
Retriever = Callable[[str, list[float]], list[RetrievedChunk]]

_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_SNIPPET_CHARS = 300


@dataclass(frozen=True)
class Source:
    id: int  # the [n] used in the answer
    document: str
    document_id: int
    page: int | None
    section: str | None
    chunk_id: int
    snippet: str
    score: float


@dataclass(frozen=True)
class Answer:
    answer: str
    found: bool
    sources: list[Source]


NOT_FOUND = Answer(NOT_FOUND_ANSWER, found=False, sources=[])


def build_context(hits: list[RetrievedChunk]) -> str:
    parts = []
    for n, hit in enumerate(hits, start=1):
        attrs = f'id="{n}" source="{html.escape(hit.filename, quote=True)}"'
        if hit.page is not None:
            attrs += f' page="{hit.page}"'
        if hit.section:
            attrs += f' section="{html.escape(hit.section, quote=True)}"'
        body = hit.text.replace("</document>", "<\\/document>")
        parts.append(f"<document {attrs}>\n{body}\n</document>")
    return "\n\n".join(parts)


def build_messages(question: str, hits: list[RetrievedChunk]) -> list[ChatMessage]:
    return [
        {"role": "system", "content": load_prompt("answer_system")},
        {
            "role": "user",
            "content": load_prompt("answer_user").format(
                context=build_context(hits), question=question
            ),
        },
    ]


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


def finalise(text: str, hits: list[RetrievedChunk]) -> Answer:
    text = text.strip()
    if not text or is_not_found_answer(text):
        return NOT_FOUND
    sources = []
    for n in parse_citations(text, len(hits)):
        hit = hits[n - 1]
        sources.append(
            Source(
                id=n,
                document=hit.filename,
                document_id=hit.document_id,
                page=hit.page,
                section=hit.section,
                chunk_id=hit.chunk_id,
                snippet=_snippet(hit.text),
                score=round(hit.score, 4),
            )
        )
    if not sources:
        log.warning("LLM answer contained no valid citations")
    return Answer(text, found=True, sources=sources)


def _retrieve(question: str, retrieve: Retriever, embedder: Embedder) -> list[RetrievedChunk]:
    hits = retrieve(question, embedder.embed([question])[0])
    log.debug("Retrieved %d chunks", len(hits))
    return hits


def answer_question(
    question: str, *, retrieve: Retriever, llm: LLMClient, embedder: Embedder
) -> Answer:
    hits = _retrieve(question, retrieve, embedder)
    if not hits:
        return NOT_FOUND
    return finalise(llm.chat(build_messages(question, hits)), hits)


def answer_events(
    question: str, *, retrieve: Retriever, llm: LLMClient, embedder: Embedder
) -> Iterator[dict[str, Any]]:
    """Streaming protocol (event dicts, each with a "type"):
      status  {"stage": "retrieving" | "generating"}
      token   {"text": "..."}                    zero or more
      done    {"answer", "found", "sources"}     final, authoritative answer
      error   {"message": "..."}                 instead of done
    Clients should replace the streamed text with done.answer: a not-found reply
    is normalised there, and sources are only known once the answer is complete.
    """
    try:
        yield {"type": "status", "stage": "retrieving"}
        hits = _retrieve(question, retrieve, embedder)
        if not hits:
            yield {"type": "done", **_answer_dict(NOT_FOUND)}
            return
        yield {"type": "status", "stage": "generating"}
        parts: list[str] = []
        for piece in llm.chat_stream(build_messages(question, hits)):
            parts.append(piece)
            yield {"type": "token", "text": piece}
        yield {"type": "done", **_answer_dict(finalise("".join(parts), hits))}
    except OllamaError as exc:
        log.error("Model call failed: %s", exc)
        yield {"type": "error", "message": "The local model is unavailable."}


def _answer_dict(answer: Answer) -> dict[str, Any]:
    return {
        "answer": answer.answer,
        "found": answer.found,
        "sources": [asdict(s) for s in answer.sources],
    }
