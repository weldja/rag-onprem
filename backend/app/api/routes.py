import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_db, get_embedder, get_llm, get_ollama, get_retriever
from app.llm.base import Embedder, LLMClient
from app.llm.ollama import OllamaClient, OllamaError
from app.retrieval.answer import Retriever, answer_events, answer_question

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class SourceOut(BaseModel):
    id: int
    document: str
    document_id: int
    page: int | None
    section: str | None
    chunk_id: int
    snippet: str
    score: float


class AskResponse(BaseModel):
    answer: str
    found: bool
    sources: list[SourceOut]


@router.get("/health")
def health(
    response: Response,
    engine: Annotated[Engine, Depends(get_db)],
    ollama: Annotated[OllamaClient, Depends(get_ollama)],
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
    }


@router.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    retrieve: Annotated[Retriever, Depends(get_retriever)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> AskResponse:
    try:
        result = answer_question(
            req.question.strip(), retrieve=retrieve, llm=llm, embedder=embedder
        )
    except OllamaError as exc:
        log.error("Model call failed: %s", exc)
        raise HTTPException(status_code=503, detail="The local model is unavailable.") from exc
    return AskResponse(
        answer=result.answer,
        found=result.found,
        sources=[SourceOut(**asdict(s)) for s in result.sources],
    )


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        payload = {k: v for k, v in event.items() if k != "type"}
        yield f"event: {event['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/ask/stream")
def ask_stream(
    req: AskRequest,
    retrieve: Annotated[Retriever, Depends(get_retriever)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> StreamingResponse:
    """Server-sent events; see app.retrieval.answer.answer_events for the protocol."""
    events = answer_events(req.question.strip(), retrieve=retrieve, llm=llm, embedder=embedder)
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
