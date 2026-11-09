import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import (
    AppSettings,
    get_answer_cache,
    get_db,
    get_embedder,
    get_llm,
    get_ollama,
    get_search,
)
from app.llm.base import ChatMessage, Embedder, LLMClient
from app.llm.ollama import OllamaClient, OllamaError
from app.llm.prompts import prompt_version
from app.retrieval.answer import Request, answer_events, answer_question
from app.retrieval.cache import AnswerCache
from app.retrieval.service import Search

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Earlier turns of this conversation, oldest first. The client keeps the
    # conversation; the server stores nothing. Only the last few are used.
    history: list[Turn] = Field(default_factory=list, max_length=20)
    # Limit the search to these documents (ones the user can't see are ignored),
    # e.g. a folder of a file share picked in the sidebar.
    document_ids: list[int] | None = Field(default=None, max_length=2000)
    mode: Literal["answer", "summary"] = "answer"
    # Include every retrieved chunk and the prompt version (used by evals).
    debug: bool = False

    @model_validator(mode="after")
    def _summary_needs_documents(self) -> "AskRequest":
        if self.mode == "summary" and not self.document_ids:
            raise ValueError("summary mode needs document_ids")
        if self.mode == "summary" and len(self.document_ids or []) > 5:
            raise ValueError("summarise at most 5 documents at a time")
        return self


class SourceOut(BaseModel):
    id: int
    document: str
    document_id: int
    page: int | None
    section: str | None
    chunk_id: int
    snippet: str
    score: float
    rerank_score: float | None = None


class AskResponse(BaseModel):
    answer: str
    found: bool
    sources: list[SourceOut]
    retrieved: list[SourceOut] | None = None
    prompt_version: str | None = None


def _cache(body: AskRequest, cache: AnswerCache) -> AnswerCache | None:
    # Debug requests come from the eval, which must measure the model every time.
    return None if body.debug else cache


def _request(body: AskRequest, settings: AppSettings) -> Request:
    history: list[ChatMessage] = [{"role": t.role, "content": t.content} for t in body.history]
    return Request(
        question=body.question.strip(),
        history=history,
        mode=body.mode,
        history_turns=settings.history_turns,
    )


@router.get("/health")
def health(
    response: Response,
    engine: Annotated[Engine, Depends(get_db)],
    ollama: Annotated[OllamaClient, Depends(get_ollama)],
    settings: AppSettings,
) -> dict[str, Any]:
    database = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database = False

    missing: list[str] | None
    try:
        missing = ollama.missing_models()
    except OllamaError:
        missing = None

    ok = database and missing == []
    if not ok:
        response.status_code = 503
    return {
        "status": "ok" if ok else "unavailable",
        "database": database,
        "ollama": missing is not None,
        "models": {
            m: (missing is not None and m not in missing)
            for m in (ollama.llm_model, ollama.embed_model)
        },
        "reranker": settings.reranker_model or None,
    }


# exclude_unset (not exclude_none): debug fields are only set when asked for,
# while sources keep explicit nulls such as page/section.
@router.post("/ask", response_model=AskResponse, response_model_exclude_unset=True)
def ask(
    body: AskRequest,
    search: Annotated[Search, Depends(get_search)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    cache: Annotated[AnswerCache, Depends(get_answer_cache)],
    settings: AppSettings,
) -> AskResponse:
    ids = body.document_ids
    try:
        result = answer_question(
            _request(body, settings),
            retrieve=search.retriever(ids),
            spread=(lambda: search.spread(ids or [])) if body.mode == "summary" else None,
            llm=llm,
            embedder=embedder,
            cache=_cache(body, cache),
        )
    except OllamaError as exc:
        log.error("Model call failed: %s", exc)
        raise HTTPException(status_code=503, detail="The local model is unavailable.") from exc
    extra: dict[str, Any] = {}
    if body.debug:
        extra["retrieved"] = [SourceOut(**asdict(s)) for s in result.retrieved]
        extra["prompt_version"] = prompt_version()
    return AskResponse(
        answer=result.answer,
        found=result.found,
        sources=[SourceOut(**asdict(s)) for s in result.sources],
        **extra,
    )


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        payload = {k: v for k, v in event.items() if k != "type"}
        yield f"event: {event['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/ask/stream")
def ask_stream(
    body: AskRequest,
    search: Annotated[Search, Depends(get_search)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    cache: Annotated[AnswerCache, Depends(get_answer_cache)],
    settings: AppSettings,
) -> StreamingResponse:
    """Server-sent events; see app.retrieval.answer.answer_events for the protocol."""
    ids = body.document_ids
    events = answer_events(
        _request(body, settings),
        retrieve=search.retriever(ids),
        spread=(lambda: search.spread(ids or [])) if body.mode == "summary" else None,
        llm=llm,
        embedder=embedder,
        debug=body.debug,
        cache=_cache(body, cache),
    )
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
