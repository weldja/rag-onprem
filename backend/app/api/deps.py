"""FastAPI dependencies. Tests override these with fakes."""

from functools import lru_cache, partial

from sqlalchemy import Engine

from app.core.config import get_settings
from app.db.engine import get_engine
from app.llm.base import Embedder, LLMClient
from app.llm.ollama import OllamaClient
from app.retrieval.answer import Retriever
from app.retrieval.search import vector_search


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


def get_retriever() -> Retriever:
    s = get_settings()
    return partial(vector_search, get_engine(), embed_model=s.embed_model, k=s.top_k)
