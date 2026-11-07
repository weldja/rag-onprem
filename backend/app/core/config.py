"""Application settings, read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field, field_validator
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

    # OCR for scanned PDF pages and image files (Tesseract, in the image).
    # OCR_LANGUAGES must be covered by the image's OCR_LANGUAGES build arg.
    ocr_enabled: bool = True
    ocr_languages: str = "eng"
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    ocr_min_chars: int = Field(default=30, ge=0)
    ocr_max_pages: int = Field(default=300, gt=0)
    ocr_timeout_s: float = Field(default=120.0, gt=0)

    # Worker
    worker_poll_s: float = Field(default=2.0, gt=0)
    job_stale_after_s: float = Field(default=1800.0, gt=0)
    job_max_attempts: int = Field(default=3, gt=0)

    # Retrieval
    top_k: int = Field(default=6, gt=0, le=50)
    retrieval_candidates: int = Field(default=30, gt=0, le=200)  # per leg, before fusion
    min_similarity: float = Field(default=0.35, ge=-1.0, le=1.0)
    keyword_keep: int = Field(default=5, ge=0)  # top keyword hits kept even below min_similarity

    # Optional cross-encoder reranker, baked into the api image at build time.
    # Empty = disabled. Must match the image's RERANKER_MODEL build arg.
    reranker_model: str = ""
    reranker_cache_dir: str = "/opt/models/fastembed"
    rerank_candidates: int = Field(default=20, gt=0, le=100)  # fused pool the reranker sees
    rerank_min_score: float | None = None  # drop chunks the reranker scores below this

    # Conversation and summaries
    history_turns: int = Field(default=1, ge=0, le=5)  # previous exchanges sent to the LLM
    summary_chunks: int = Field(default=12, gt=0, le=40)  # sampled per summary request

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

    # Directory sign-in (LDAP / Active Directory). Local accounts keep working,
    # so the install-time admin remains a break-glass login.
    ldap_enabled: bool = False
    ldap_url: str = ""  # ldaps://dc1.example.local or ldap://... with LDAP_START_TLS=true
    ldap_start_tls: bool = False
    ldap_ca_file: str = ""  # PEM CA bundle for the directory's certificate (empty = system CAs)
    ldap_allow_plaintext: bool = False  # only for isolated test setups
    ldap_bind_dn: str = ""  # service account used to look users up
    ldap_bind_password: str = ""
    ldap_user_base_dn: str = ""
    # {username} is replaced by the escaped sign-in name.
    ldap_user_filter: str = "(&(objectClass=user)(sAMAccountName={username}))"
    ldap_display_name_attr: str = "displayName"
    ldap_group_base_dn: str = ""  # where to look for groups (empty = no group sync)
    # {user_dn} is replaced by the escaped user DN. For nested AD groups use
    # (member:1.2.840.113556.1.4.1941:={user_dn})
    ldap_group_filter: str = "(member={user_dn})"
    # Directory group DN -> app group name. Only these app groups are synced.
    ldap_group_map: dict[str, str] = Field(default_factory=dict)
    ldap_admin_group: str = ""  # members of this group DN are administrators
    ldap_required_group: str = ""  # if set, only members of this group DN may sign in
    ldap_timeout_s: float = Field(default=10.0, gt=0)

    log_level: str = "INFO"

    @field_validator("rerank_min_score", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # Compose passes unset variables through as "".
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
