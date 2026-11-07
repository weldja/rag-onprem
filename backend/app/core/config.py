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
    sheet_chunk_chars: int = Field(default=1800, gt=200)
    upload_dir: str = "/data/uploads"
    max_upload_mb: int = Field(default=50, gt=0)  # keep in step with nginx client_max_body_size

    # Worker
    worker_poll_s: float = Field(default=2.0, gt=0)
    job_stale_after_s: float = Field(default=1800.0, gt=0)
    job_max_attempts: int = Field(default=3, gt=0)

    # Retrieval
    top_k: int = Field(default=6, gt=0, le=50)
    retrieval_candidates: int = Field(default=30, gt=0, le=200)  # per leg, before fusion
    min_similarity: float = Field(default=0.35, ge=-1.0, le=1.0)
    keyword_keep: int = Field(default=5, ge=0)  # top keyword hits kept even below min_similarity

    # How long the API waits at startup for its models to appear in Ollama.
    startup_wait_s: float = Field(default=120.0, ge=0)

    # Auth
    cookie_secure: bool = True  # False only for plain-HTTP local testing
    session_idle_hours: float = Field(default=12.0, gt=0)
    session_max_days: float = Field(default=7.0, gt=0)
    login_max_failures: int = Field(default=5, gt=0)
    login_lockout_minutes: float = Field(default=5.0, gt=0)
    password_min_length: int = Field(default=12, ge=8)
    default_group: str = "All staff"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
