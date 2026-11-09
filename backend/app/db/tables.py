"""SQLAlchemy Core table definitions. Schema changes go through Alembic migrations;
keep these in step with migrations/versions/."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.core.config import get_settings

metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("filename", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("page_count", Integer, nullable=True),
    # pending -> processing -> ready | failed; excluded (by an admin: no chunks,
    # never queued, kept so a share scan doesn't add it again)
    Column("status", Text, nullable=False, server_default="pending"),
    Column("error", Text, nullable=True),
    Column("content_type", Text, nullable=True),  # file extension without the dot
    Column("storage_path", Text, nullable=True),
    Column("size_bytes", BigInteger, nullable=True),
    # Uploader can always see and manage the document; NULL for CLI ingests.
    Column("uploaded_by", BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    # File-share documents (migration 0005): indexed in place from a read-only
    # mount. NULL for uploads. sha256 is unique among uploads only (partial
    # index uq_documents_upload_sha256): a share may hold the same file twice.
    Column("source_id", BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=True),
    Column("source_path", Text, nullable=True),  # POSIX path relative to the share root
    Column("source_mtime", Float, nullable=True),
    # Migration 0006: excerpts embedded so far while processing; last time ready.
    Column("progress_done", Integer, nullable=True),
    Column("progress_total", Integer, nullable=True),
    Column("indexed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("source_id", "source_path", name="uq_documents_source_path"),
    CheckConstraint(
        "status IN ('pending','processing','ready','failed','excluded')",
        name="ck_documents_status",
    ),
)

sources = Table(
    "sources",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("key", Text, nullable=False),  # share1, share2, ... from settings
    Column("name", Text, nullable=False),
    Column("root", Text, nullable=False),  # mount point inside the containers
    Column("display_path", Text, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("last_scan_at", DateTime(timezone=True), nullable=True),
    Column("last_scan_error", Text, nullable=True),
    Column("scan_requested_at", DateTime(timezone=True), nullable=True),
    Column("files_seen", Integer, nullable=False, server_default="0"),
    Column("files_skipped", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("key", name="uq_sources_key"),
)

# Files on a share that aren't indexed, replaced on every scan (migration 0006).
source_skipped = Table(
    "source_skipped",
    metadata,
    Column("source_id", BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
    Column("path", Text, primary_key=True),
    Column("reason", Text, nullable=False),  # unsupported | empty | too_large | excluded
    Column("size_bytes", BigInteger, nullable=True),
)

chunks = Table(
    "chunks",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "document_id",
        BigInteger,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("page", Integer, nullable=True),  # 1-based page or slide number
    Column("section", Text, nullable=True),  # heading, sheet name + rows, ...
    Column("ordinal", Integer, nullable=False),  # position within the document
    Column("text", Text, nullable=False),
    Column("embedding", Vector(get_settings().embed_dim), nullable=False),
    # Vectors from different models must never be compared; every query filters on this.
    Column("embed_model", Text, nullable=False),
    Column("tsv", TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)),
)

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("username", Text, nullable=False),  # always lower-case
    Column("display_name", Text, nullable=True),
    Column("password_hash", Text, nullable=False),
    Column("is_admin", Boolean, nullable=False, server_default="false"),
    Column("disabled", Boolean, nullable=False, server_default="false"),
    Column("failed_logins", Integer, nullable=False, server_default="0"),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    # 'local' (password hash here) or 'ldap' (password checked by the directory;
    # password_hash holds an unusable marker).
    Column("auth_source", Text, nullable=False, server_default="local"),
    Column("ldap_dn", Text, nullable=True),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("username", name="uq_users_username"),
)

groups = Table(
    "groups",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("name", name="uq_groups_name"),
)

user_groups = Table(
    "user_groups",
    metadata,
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)

document_groups = Table(
    "document_groups",
    metadata,
    Column(
        "document_id", BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("group_id", BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),  # sha256 of the cookie value
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

ingest_jobs = Table(
    "ingest_jobs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "document_id",
        BigInteger,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("kind", Text, nullable=False, server_default="ingest"),  # ingest | reindex
    # queued | running | done | failed | cancelled (document excluded, migration 0006)
    Column("status", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
)
