"""File-share sources: documents indexed in place from read-only folders.

A share document keeps its file on the share (storage_path points into the
read-only mount) and is identified by (source_id, source_path). The same file
content can sit in several folders of a share, so sha256 is now only unique
among uploads.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("key", sa.Text, nullable=False),  # share1, share2, ... from settings
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("root", sa.Text, nullable=False),  # mount point inside the containers
        sa.Column("display_path", sa.Text, nullable=False),  # what users recognise
        # False once the share is no longer configured; its documents stay
        # searchable until an administrator removes the source.
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_error", sa.Text, nullable=True),
        sa.Column("scan_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files_seen", sa.Integer, nullable=False, server_default="0"),
        sa.Column("files_skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("key", name="uq_sources_key"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "source_id",
            sa.BigInteger,
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column("documents", sa.Column("source_path", sa.Text, nullable=True))
    op.add_column("documents", sa.Column("source_mtime", sa.Float, nullable=True))
    op.create_check_constraint(
        "ck_documents_source_path",
        "documents",
        "(source_id IS NULL) = (source_path IS NULL)",
    )
    op.create_unique_constraint(
        "uq_documents_source_path", "documents", ["source_id", "source_path"]
    )
    op.drop_constraint("uq_documents_sha256", "documents")
    op.create_index(
        "uq_documents_upload_sha256",
        "documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("source_id IS NULL"),
    )
    # Duplicate detection across shares and copying chunks between copies.
    op.create_index("ix_documents_sha256", "documents", ["sha256"])


def downgrade() -> None:
    op.execute("DELETE FROM documents WHERE source_id IS NOT NULL")
    op.drop_index("ix_documents_sha256", "documents")
    op.drop_index("uq_documents_upload_sha256", "documents")
    op.create_unique_constraint("uq_documents_sha256", "documents", ["sha256"])
    op.drop_constraint("uq_documents_source_path", "documents")
    op.drop_constraint("ck_documents_source_path", "documents")
    op.drop_column("documents", "source_mtime")
    op.drop_column("documents", "source_path")
    op.drop_column("documents", "source_id")
    op.drop_table("sources")
