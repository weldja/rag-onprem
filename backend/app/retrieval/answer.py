"""Question -> retrieve -> prompt -> cited answer (whole, or streamed as events).

Context building (excerpts wrapped in <excerpt> tags, with any literal
</excerpt> neutralised so file content can't break out of the data region)
is adapted from rag-claude's ask_stream. The tag is not called <document>:
the model then counted six excerpts as "6 documents indexed". Citations are numbered [n] ids that
map back to the retrieved chunks.

Two modes:
  answer   hybrid retrieval for the question (plus the previous question when
           the new one looks like a follow-up), answer prompt
  summary  chunks sampled across the chosen documents, summary prompt; also
           used for a typed request like "summarise this doc" (asks_for_summary)
"""

import html
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from app.llm.base import ChatMessage, Embedder, LLMClient
from app.llm.ollama import OllamaError
from app.llm.prompts import NOT_FOUND_ANSWER, is_not_found_answer, load_prompt, prompt_version
from app.retrieval.cache import AnswerCache
from app.retrieval.search import RetrievedChunk

log = logging.getLogger(__name__)

# (question, query_vector) -> relevant chunks, best first; [] when nothing is relevant.
Retriever = Callable[[str, list[float]], list[RetrievedChunk]]
Mode = Literal["answer", "summary"]

_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_SNIPPET_CHARS = 300
# A short question after earlier turns is probably a follow-up ("and for
# part-timers?"); retrieval then also uses the previous question.
_FOLLOW_UP_MAX_WORDS = 12


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
    rerank_score: float | None = None


@dataclass(frozen=True)
class Answer:
    answer: str
    found: bool
    sources: list[Source]
    retrieved: list[Source] = field(default_factory=list)  # every chunk sent to the LLM
    # Token counts from the model (prompt_tokens, output_tokens, prompt_ms); debug only.
    usage: dict[str, int] = field(default_factory=dict)


NOT_FOUND = Answer(NOT_FOUND_ANSWER, found=False, sources=[])


# Typed requests for a summary of the chosen documents as a whole ("summarise
# this doc", "give me an overview of the manual"). Answer mode would search for
# the words "summarise this doc" and find nothing relevant. A request naming a
# topic ("summarise the SSO section") stays a question.
_SUMMARY_ASK = re.compile(
    r"\b(summar(y|ies|i[sz]e|i[sz]ing)|overview|tl;?dr|gist|"
    r"what\b.*\babout|what\b.*\bcover(s|ed)?)\b"
)
_FILENAME = re.compile(r"\S+\.[a-z0-9]{2,5}\b")
_FILLER_WORDS = """
    a about all an and any as be bullet bullets brief briefly can content contents cover covered
    covers could describe do does doc docs document documents each entire explain file files for
    full gist give i in is it its key list main manual me my of on one overview page pages paper
    paragraph paragraphs pdf please point points provide quick quickly report s sentence sentences
    short summaries summarise summarize summarising summarizing summary tell than that the these
    this those three tldr to two up us want what whats whole with would write you
"""
_FILLER = frozenset(_FILLER_WORDS.split())


def mentions_summary(question: str) -> bool:
    """Cheap first check for asks_for_summary (no document names needed)."""
    return _SUMMARY_ASK.search(question.lower()) is not None


def asks_for_summary(question: str, document_names: Sequence[str] = ()) -> bool:
    """True if the question only asks for a summary of the documents in scope:
    after dropping file names, words of the scoped documents' names, numbers
    and filler, nothing is left."""
    if not mentions_summary(question):
        return False
    q = re.sub(r"\btl;?dr\b", " ", _FILENAME.sub(" ", question.lower()))
    names = {w for n in document_names for w in re.findall(r"[a-z0-9]+", n.lower())}
    rest = [
        w
        for w in re.findall(r"[a-z0-9]+", q)
        if w not in _FILLER and w not in names and not w.isdigit()
    ]
    return not rest


@dataclass(frozen=True)
class Request:
    question: str
    history: Sequence[ChatMessage] = ()
    mode: Mode = "answer"
    history_turns: int = 1
    # Longest excerpt sent to the model in answer mode (0 = whole chunks); see trim_excerpt.
    excerpt_max_chars: int = 0


# Words too common to say which part of an excerpt a question is about.
_STOP_WORDS_TEXT = """
    a about after all an and any are as at be been before but by can could did do does each
    for from get got had has have how i if in into is it its may me might more most much must my
    no not of on or our out over per should so some than that the their them then there these
    they this those to up us was we were what when where which while who whom why will with
    would you your
"""
_STOP_WORDS = frozenset(_STOP_WORDS_TEXT.split())
_SHEET_HEADER = re.compile(r"^(Sheet .*|Table) columns: ")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")


def question_terms(question: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 1} - _STOP_WORDS


def _covers(unit: str, terms: set[str]) -> set[str]:
    """Question terms a piece of text contains. Long terms also match by their
    stem, so "wavelengths" matches "wavelength"."""
    words = set(re.findall(r"[a-z0-9]+", unit.lower()))
    found = terms & words
    for term in terms - found:
        if len(term) >= 5 and any(w.startswith(term[: len(term) - 2]) for w in words):
            found.add(term)
    return found


def trim_excerpt(text: str, question: str, max_chars: int) -> str:
    """The part of an excerpt (at most max_chars) that covers the most question
    terms, so the model reads less before it starts writing. Cuts fall on line
    or sentence boundaries and are marked with "…". A spreadsheet chunk keeps
    its column header line. Returned unchanged when it already fits, or when
    no term matches (then nothing says which part matters, so nothing is cut).
    The excerpt keeps its id, document and page, so citations are unaffected."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    terms = question_terms(question)
    lines = text.split("\n")
    head = lines.pop(0) if _SHEET_HEADER.match(lines[0]) and len(lines) > 1 else None
    budget = max_chars - (len(head) + 1 if head else 0)
    units: list[str] = []
    for line in lines:  # long paragraphs (DOCX, HTML) are split into sentences
        units.extend(_SENTENCE_END.split(line) if len(line) > budget // 2 else [line])
    covers = [_covers(u, terms) for u in units]
    best: tuple[int, int, int] | None = None  # (terms covered, start, end)
    for start in range(len(units)):
        size, seen, end = 0, set(), start
        while end < len(units) and size + len(units[end]) + 1 <= budget:
            size += len(units[end]) + 1
            seen |= covers[end]
            end += 1
        if end > start and (best is None or len(seen) > best[0]):
            best = (len(seen), start, end)
    if best is None or best[0] == 0:
        return text
    _, start, end = best
    body = "\n".join(units[start:end])
    if start > 0:
        body = "…\n" + body
    if end < len(units):
        body += "\n…"
    return f"{head}\n{body}" if head else body


def build_context(hits: list[RetrievedChunk], question: str = "", max_chars: int = 0) -> str:
    parts = []
    for n, hit in enumerate(hits, start=1):
        attrs = f'id="{n}" source="{html.escape(hit.filename, quote=True)}"'
        if hit.page is not None:
            attrs += f' page="{hit.page}"'
        if hit.section:
            attrs += f' section="{html.escape(hit.section, quote=True)}"'
        text = trim_excerpt(hit.text, question, max_chars)
        body = text.replace("</excerpt>", "<\\/excerpt>")
        parts.append(f"<excerpt {attrs}>\n{body}\n</excerpt>")
    return "\n\n".join(parts)


def strip_citations(text: str) -> str:
    """Earlier answers' [n] markers refer to excerpts the model no longer sees."""
    return re.sub(r"\s*" + _CITATION.pattern, "", text)


def recent_history(history: Sequence[ChatMessage], turns: int) -> list[ChatMessage]:
    """The last `turns` complete user/assistant exchanges, oldest first."""
    if turns <= 0:
        return []
    kept: list[ChatMessage] = []
    msgs = [m for m in history if m["role"] in ("user", "assistant") and m["content"].strip()]
    i = len(msgs) - 1
    while i >= 1 and len(kept) < 2 * turns:
        if msgs[i]["role"] == "assistant" and msgs[i - 1]["role"] == "user":
            kept[:0] = [
                {"role": "user", "content": msgs[i - 1]["content"]},
                {"role": "assistant", "content": strip_citations(msgs[i]["content"])},
            ]
            i -= 2
        else:
            i -= 1
    return kept


def retrieval_query(question: str, history: Sequence[ChatMessage]) -> str:
    previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
    if previous and len(question.split()) <= _FOLLOW_UP_MAX_WORDS:
        return f"{previous}\n{question}"
    return question


def build_messages(req: Request, hits: list[RetrievedChunk]) -> list[ChatMessage]:
    system = load_prompt("summary_system" if req.mode == "summary" else "answer_system")
    # Summaries need the whole of each sampled excerpt; answers only the part
    # about the question (with the previous question for a follow-up).
    if req.mode == "answer":
        context = build_context(
            hits, retrieval_query(req.question, req.history), req.excerpt_max_chars
        )
    else:
        context = build_context(hits)
    return [
        {"role": "system", "content": system},
        *recent_history(req.history, req.history_turns),
        {
            "role": "user",
            "content": load_prompt("answer_user").format(context=context, question=req.question),
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


def _source(n: int, hit: RetrievedChunk) -> Source:
    return Source(
        id=n,
        document=hit.filename,
        document_id=hit.document_id,
        page=hit.page,
        section=hit.section,
        chunk_id=hit.chunk_id,
        snippet=_snippet(hit.text),
        score=round(hit.score, 4),
        rerank_score=round(hit.rerank_score, 4) if hit.rerank_score is not None else None,
    )


def finalise(text: str, hits: list[RetrievedChunk], mode: Mode = "answer") -> Answer:
    retrieved = [_source(n, h) for n, h in enumerate(hits, start=1)]
    text = text.strip()
    if not text or (mode == "answer" and is_not_found_answer(text)):
        return Answer(NOT_FOUND.answer, found=False, sources=[], retrieved=retrieved)
    sources = [retrieved[n - 1] for n in parse_citations(text, len(hits))]
    if not sources:
        log.warning("LLM answer contained no valid citations")
    return Answer(text, found=True, sources=sources, retrieved=retrieved)


class _Timer:
    """Per-answer timings for the log: counts and milliseconds only, never text
    (questions and document contents must not be logged at INFO)."""

    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self.start = time.monotonic()
        self.retrieved_at: float | None = None
        self.first_word_at: float | None = None
        self.cached = False
        self.usage: dict[str, int] = {}  # filled by the LLM client
        self.context_chars: int | None = None  # after trimming, if messages were built

    def built(self, messages: list[ChatMessage]) -> None:
        self.context_chars = len(messages[-1]["content"])

    def retrieved(self) -> None:
        self.retrieved_at = time.monotonic()

    def word(self) -> None:
        if self.first_word_at is None:
            self.first_word_at = time.monotonic()

    def _ms(self, t: float | None) -> str:
        return "-" if t is None else f"{(t - self.start) * 1000:.0f}ms"

    def log(self, hits: list[RetrievedChunk], found: bool | None) -> None:
        # context_chars: the user message as sent (excerpts after trimming,
        # plus the question); prompt_tokens: what the model actually read.
        context_chars = (
            self.context_chars if self.context_chars is not None else sum(len(h.text) for h in hits)
        )
        log.info(
            "answer timing: mode=%s chunks=%d context_chars=%d prompt_tokens=%s prompt=%s "
            "retrieval=%s first_word=%s total=%s found=%s cached=%s",
            self.mode,
            len(hits),
            context_chars,
            self.usage.get("prompt_tokens", "-"),
            f"{self.usage['prompt_ms']}ms" if "prompt_ms" in self.usage else "-",
            self._ms(self.retrieved_at),
            self._ms(self.first_word_at),
            self._ms(time.monotonic()),
            "error" if found is None else found,
            self.cached,
        )


def gather(
    req: Request,
    *,
    retrieve: Retriever,
    spread: Callable[[], list[RetrievedChunk]] | None,
    embedder: Embedder,
) -> list[RetrievedChunk]:
    if req.mode == "summary":
        if spread is None:
            raise ValueError("summary mode needs a spread retriever")
        return spread()
    query = retrieval_query(req.question, req.history)
    hits = retrieve(query, embedder.embed([query])[0])
    log.debug("Retrieved %d chunks", len(hits))
    return hits


def reading_status(
    hits: list[RetrievedChunk], *, cached: bool = False, summary: bool = False
) -> dict[str, Any]:
    """The "generating" status event: what the model is about to read, so the
    user sees progress (and where the answer will come from) during the wait
    before the first word. `summary` tells the client the answer is a summary
    (the server may have recognised a typed request for one)."""
    names = list(dict.fromkeys(h.filename for h in hits))
    event: dict[str, Any] = {
        "type": "status",
        "stage": "generating",
        "excerpts": len(hits),
        "documents": names,
    }
    if cached:
        event["cached"] = True
    if summary:
        event["summary"] = True
    return event


def answer_question(
    req: Request,
    *,
    retrieve: Retriever,
    llm: LLMClient,
    embedder: Embedder,
    spread: Callable[[], list[RetrievedChunk]] | None = None,
    cache: AnswerCache | None = None,
) -> Answer:
    timer = _Timer(req.mode)
    hits = gather(req, retrieve=retrieve, spread=spread, embedder=embedder)
    timer.retrieved()
    if not hits:
        timer.log(hits, found=False)
        return NOT_FOUND
    messages = build_messages(req, hits)
    timer.built(messages)
    key = cache.key(messages) if cache is not None else ""
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        timer.cached = True
        result = finalise(cached, hits, req.mode)
        timer.log(hits, found=result.found)
        return result
    try:
        result = finalise(llm.chat(messages, timer.usage), hits, req.mode)
    except OllamaError:
        timer.log(hits, found=None)
        raise
    if cache is not None and result.found:
        cache.put(key, result.answer)
    timer.log(hits, found=result.found)
    return replace(result, usage=dict(timer.usage))


def answer_events(
    req: Request,
    *,
    retrieve: Retriever,
    llm: LLMClient,
    embedder: Embedder,
    spread: Callable[[], list[RetrievedChunk]] | None = None,
    debug: bool = False,
    cache: AnswerCache | None = None,
) -> Iterator[dict[str, Any]]:
    """Streaming protocol (event dicts, each with a "type"):
      status  {"stage": "retrieving"}
      status  {"stage": "generating", "excerpts": n, "documents": [names]
               [, "cached": true][, "summary": true]}
      token   {"text": "..."}                    zero or more
      done    {"answer", "found", "sources"[, "retrieved"]}   final, authoritative
      error   {"message": "..."}                 instead of done
    Clients should replace the streamed text with done.answer: a not-found reply
    is normalised there, and sources are only known once the answer is complete.
    """
    timer = _Timer(req.mode)
    hits: list[RetrievedChunk] = []
    try:
        yield {"type": "status", "stage": "retrieving"}
        hits = gather(req, retrieve=retrieve, spread=spread, embedder=embedder)
        timer.retrieved()
        if not hits:
            timer.log(hits, found=False)
            yield {"type": "done", **answer_dict(NOT_FOUND, debug)}
            return
        messages = build_messages(req, hits)
        timer.built(messages)
        key = cache.key(messages) if cache is not None else ""
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            timer.cached = True
            timer.word()
            yield reading_status(hits, cached=True, summary=req.mode == "summary")
            yield {"type": "token", "text": cached}
            result = finalise(cached, hits, req.mode)
            timer.log(hits, found=result.found)
            yield {"type": "done", **answer_dict(result, debug)}
            return
        yield reading_status(hits, summary=req.mode == "summary")
        parts: list[str] = []
        for piece in llm.chat_stream(messages, timer.usage):
            if piece.strip():
                timer.word()
            parts.append(piece)
            yield {"type": "token", "text": piece}
        result = replace(finalise("".join(parts), hits, req.mode), usage=dict(timer.usage))
        if cache is not None and result.found:
            cache.put(key, result.answer)
        timer.log(hits, found=result.found)
        yield {"type": "done", **answer_dict(result, debug)}
    except OllamaError as exc:
        log.error("Model call failed: %s", exc)
        timer.log(hits, found=None)
        yield {"type": "error", "message": "The local model is unavailable."}


def answer_dict(answer: Answer, debug: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "answer": answer.answer,
        "found": answer.found,
        "sources": [asdict(s) for s in answer.sources],
    }
    if debug:
        out["retrieved"] = [asdict(s) for s in answer.retrieved]
        out["prompt_version"] = prompt_version()
        out["usage"] = answer.usage
    return out
