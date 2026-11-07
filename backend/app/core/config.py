"""Application settings, read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rag:rag@db:5432/rag"

    # Local model runtime. Never a hosted endpoint.
    ollama_url: str = "http://ollama:11434"
    llm_model: str = "qwen2.5:7b-instruct"
    embed_model: str = "bge-m3"
    embed_dim: int = Field(default=1024, gt=0, le=2000)  # HNSW limit is 2000
    llm_num_ctx: int = Field(default=8192, gt=0)
    llm_temperature: float = 0.1
    llm_timeout_s: float = Field(default=600.0, gt=0)

    # Ingestion
    chunk_size: int = Field(default=1000, gt=0)
    chunk_overlap: int = Field(default=150, ge=0)
    embed_batch: int = Field(default=32, gt=0)

    # Retrieval
    top_k: int = Field(default=6, gt=0, le=50)
    min_similarity: float = Field(default=0.35, ge=-1.0, le=1.0)

    # How long the API waits at startup for its models to appear in Ollama.
    startup_wait_s: float = Field(default=120.0, ge=0)

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
