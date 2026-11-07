"""SQLAlchemy Core table definitions. Schema changes go through Alembic migrations."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
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
    Column("page_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("sha256", name="uq_documents_sha256"),
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
    Column("page", Integer, nullable=True),  # 1-based
    Column("ordinal", Integer, nullable=False),  # position within the document
    Column("text", Text, nullable=False),
    Column("embedding", Vector(get_settings().embed_dim), nullable=False),
    # Vectors from different models must never be compared; every query filters on this.
    Column("embed_model", Text, nullable=False),
    Column("tsv", TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)),
)
