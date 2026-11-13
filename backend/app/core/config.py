"""Application settings, read from environment variables (see .env.example)."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rag:rag@db:5432/rag"

    # Local model runtime. Never a hosted endpoint.
    ollama_url: str = "http://ollama:11434"
    llm_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    embed_model: str = "bge-m3"
    embed_dim: int = Field(default=1024, gt=0, le=2000)  # HNSW limit is 2000
    # GPU layers for the embedding model. Empty = Ollama decides. 0 = CPU only:
    # on a small card the LLM and embedding model can't both fit, and Ollama then
    # swaps them on every question (5-10 s each, measured on a 4 GB card).
    embed_num_gpu: int | None = Field(default=None, ge=0)
    llm_num_ctx: int = Field(default=8192, gt=0)
    # Longest answer, in tokens (~750 words per 1000). Bounds the time a summary
    # can run on a slow machine; as in rag-claude. 0 = no limit.
    llm_max_tokens: int = Field(default=1024, ge=0)
    llm_temperature: float = 0.1
    # Sent as Ollama's "think" flag when set. LLM_THINK=false for hybrid
    # reasoning models such as qwen3:1.7b; leave unset for instruct models.
    llm_think: bool | None = None
    llm_timeout_s: float = Field(default=600.0, gt=0)

    # Ingestion
    chunk_size: int = Field(default=1000, gt=0)
    chunk_overlap: int = Field(default=150, ge=0)
    # Excerpts per embedding request while indexing. Ollama embeds them one at a
    # time anyway, and a question can only start between requests, so keep it
    # small (see app/db/priority.py).
    embed_batch: int = Field(default=1, gt=0)
    # Longest the worker pauses indexing (per excerpt) while answers are running.
    index_pause_max_s: float = Field(default=120.0, ge=0)
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

    # File shares indexed in place (read-only). Up to three, each bind-mounted by
    # compose from SHARE_n_PATH on the host to /shares/n in api and worker. A
    # share is enabled when SHARE_n_PATH is set. Its documents are visible to
    # SHARE_n_GROUPS (comma-separated app group names, created if missing).
    share_1_path: str = ""
    share_1_name: str = ""
    share_1_display_path: str = ""  # shown to users; defaults to SHARE_1_PATH
    share_1_groups: str = "All staff"
    share_1_exclude: str = ""  # comma-separated globs on the relative path, e.g. HR/*
    share_2_path: str = ""
    share_2_name: str = ""
    share_2_display_path: str = ""
    share_2_groups: str = "All staff"
    share_2_exclude: str = ""
    share_3_path: str = ""
    share_3_name: str = ""
    share_3_display_path: str = ""
    share_3_groups: str = "All staff"
    share_3_exclude: str = ""
    shares_mount_dir: str = "/shares"
    # How often the worker looks for new, changed and deleted files. A scan only
    # stats files; it reads a file again only when its size or time changed.
    share_scan_interval_s: float = Field(default=300.0, ge=30)

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
    # Sampled per summary request. Every excerpt adds to the wait before the
    # first word, so a summary reads as many as a question (TOP_K), as rag-claude
    # does on-prem. Raise it for fuller summaries on a fast GPU.
    summary_chunks: int = Field(default=6, gt=0, le=40)

    # Answers kept in api memory, keyed on the exact excerpts, history and
    # question sent to the model. A repeated question that retrieves the same
    # excerpts is answered at once. Changed or deleted documents change the
    # excerpts and so miss the cache. 0 disables it.
    answer_cache_entries: int = Field(default=500, ge=0)
    answer_cache_ttl_s: float = Field(default=86400.0, gt=0)

    # How long the API waits at startup for its models to appear in Ollama.
    startup_wait_s: float = Field(default=120.0, ge=0)

    # Auth
    cookie_secure: bool = True  # False only for plain-HTTP local testing
    session_idle_hours: float = Field(default=12.0, gt=0)
    session_max_days: float = Field(default=7.0, gt=0)
    login_max_failures: int = Field(default=5, gt=0)
    login_lockout_minutes: float = Field(default=5.0, gt=0)
    # 12 for customers; can be lowered to 1 (no character rules; empty is never allowed).
    password_min_length: int = Field(default=12, ge=1)
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

    def shares(self) -> list["ShareConfig"]:
        """Configured file shares, in order."""
        out = []
        for n in (1, 2, 3):
            host_path = str(getattr(self, f"share_{n}_path")).strip()
            if not host_path:
                continue
            name = str(getattr(self, f"share_{n}_name")).strip()
            display = str(getattr(self, f"share_{n}_display_path")).strip()
            out.append(
                ShareConfig(
                    key=f"share{n}",
                    name=name
                    or PurePosixPath(host_path.replace("\\", "/").rstrip("/")).name
                    or f"Share {n}",
                    root=f"{self.shares_mount_dir.rstrip('/')}/{n}",
                    display_path=display or host_path,
                    groups=_csv(getattr(self, f"share_{n}_groups")),
                    exclude=_csv(getattr(self, f"share_{n}_exclude")),
                )
            )
        return out


@dataclass(frozen=True)
class ShareConfig:
    key: str
    name: str
    root: str
    display_path: str
    groups: list[str]
    exclude: list[str]


def _csv(value: object) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
