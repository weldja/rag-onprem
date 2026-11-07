import logging
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_db, get_embedder, get_llm, get_ollama, get_retriever
from app.core.config import Settings, get_settings
from app.llm.base import Embedder, LLMClient
from app.llm.ollama import OllamaClient, OllamaError
from app.retrieval.answer import Retriever, answer_question

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class SourceOut(BaseModel):
    id: int
    document: str
    document_id: int
    page: int | None
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> AskResponse:
    try:
        result = answer_question(
            req.question.strip(), retrieve=retrieve, llm=llm, embedder=embedder, settings=settings
        )
    except OllamaError as exc:
        log.error("Model call failed: %s", exc)
        raise HTTPException(status_code=503, detail="The local model is unavailable.") from exc
    return AskResponse(
        answer=result.answer,
        found=result.found,
        sources=[SourceOut(**asdict(s)) for s in result.sources],
    )
