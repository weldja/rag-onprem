"""FastAPI dependencies. Tests override these with fakes."""

from functools import lru_cache, partial
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine

from app.auth import sessions
from app.auth.acl import Viewer
from app.core.config import Settings, get_settings
from app.db.engine import get_engine
from app.llm.base import Embedder, LLMClient
from app.llm.ollama import OllamaClient
from app.retrieval.answer import Retriever
from app.retrieval.search import hybrid_search


@lru_cache
def get_ollama() -> OllamaClient:
    s = get_settings()
    return OllamaClient(
        s.ollama_url,
        llm_model=s.llm_model,
        embed_model=s.embed_model,
        timeout_s=s.llm_timeout_s,
        num_ctx=s.llm_num_ctx,
        temperature=s.llm_temperature,
    )


def get_llm() -> LLMClient:
    return get_ollama()


def get_embedder() -> Embedder:
    return get_ollama()


def get_db() -> Engine:
    return get_engine()


def get_current_user(
    request: Request,
    engine: Annotated[Engine, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Viewer:
    token = request.cookies.get(sessions.COOKIE_NAME)
    viewer = sessions.resolve(engine, token, settings) if token else None
    if viewer is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return viewer


def require_admin(viewer: Annotated[Viewer, Depends(get_current_user)]) -> Viewer:
    if not viewer.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrators only")
    return viewer


def get_retriever(
    viewer: Annotated[Viewer, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Retriever:
    return partial(
        hybrid_search,
        get_engine(),
        viewer=viewer,
        embed_model=settings.embed_model,
        k=settings.top_k,
        candidates=settings.retrieval_candidates,
        min_similarity=settings.min_similarity,
        keyword_keep=settings.keyword_keep,
    )


CurrentUser = Annotated[Viewer, Depends(get_current_user)]
AdminUser = Annotated[Viewer, Depends(require_admin)]
Db = Annotated[Engine, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
