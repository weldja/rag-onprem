"""SQLAlchemy Core table definitions. Schema changes go through Alembic migrations;
keep these in step with migrations/versions/."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
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
