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
    # pending -> processing -> ready | failed
    Column("status", Text, nullable=False, server_default="pending"),
    Column("error", Text, nullable=True),
    Column("content_type", Text, nullable=True),  # file extension without the dot
    Column("storage_path", Text, nullable=True),
    Column("size_bytes", BigInteger, nullable=True),
    # Uploader can always see and manage the document; NULL for CLI ingests.
    Column("uploaded_by", BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("sha256", name="uq_documents_sha256"),
    CheckConstraint(
        "status IN ('pending','processing','ready','failed')", name="ck_documents_status"
    ),
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
    Column("status", Text, nullable=False, server_default="queued"),  # queued|running|done|failed
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
)
