"""FastAPI dependencies. Tests override these with fakes."""

from functools import lru_cache, partial
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine

from app.auth import sessions
from app.auth.acl import Viewer
from app.auth.ldap import Directory, LdapDirectory
from app.core.config import Settings, get_settings
from app.db import priority
from app.db.engine import get_engine
from app.db.priority import Answering
from app.llm.base import Embedder, LLMClient
from app.llm.ollama import OllamaClient
from app.retrieval.cache import AnswerCache
from app.retrieval.rerank import FastEmbedReranker, Reranker
from app.retrieval.service import DbSearch, Search


@lru_cache
def get_ollama() -> OllamaClient:
    s = get_settings()
    return OllamaClient(
        s.ollama_url,
        llm_model=s.llm_model,
        embed_model=s.embed_model,
        timeout_s=s.llm_timeout_s,
        num_ctx=s.llm_num_ctx,
        max_tokens=s.llm_max_tokens,
        embed_num_gpu=s.embed_num_gpu,
        temperature=s.llm_temperature,
    )


@lru_cache
def get_answer_cache() -> AnswerCache:
    s = get_settings()
    salt = f"{s.llm_model}|{s.llm_num_ctx}|{s.llm_max_tokens}|{s.llm_temperature}"
    return AnswerCache(s.answer_cache_entries, s.answer_cache_ttl_s, salt=salt)


def get_llm() -> LLMClient:
    return get_ollama()


def get_embedder() -> Embedder:
    return get_ollama()


def get_db() -> Engine:
    return get_engine()


def get_answering() -> Answering:
    """Marks an answer as in progress so the worker pauses indexing."""
    return partial(priority.answering, get_engine())


@lru_cache
def get_directory() -> Directory | None:
    """The LDAP directory, or None when LDAP_ENABLED is false. Bad configuration
    raises DirectoryConfigError (checked at start-up)."""
    s = get_settings()
    return LdapDirectory(s) if s.ldap_enabled else None


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


@lru_cache
def get_reranker() -> Reranker | None:
    """Loaded once; None when RERANKER_MODEL is empty."""
    s = get_settings()
    if not s.reranker_model:
        return None
    return FastEmbedReranker(s.reranker_model, s.reranker_cache_dir)


def get_search(
    viewer: Annotated[Viewer, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Search:
    return DbSearch(get_engine(), viewer, settings, get_reranker())


CurrentUser = Annotated[Viewer, Depends(get_current_user)]
AdminUser = Annotated[Viewer, Depends(require_admin)]
Db = Annotated[Engine, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
