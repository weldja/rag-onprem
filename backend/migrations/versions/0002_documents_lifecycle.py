"""Document lifecycle: status, stored file, sections, and the ingest job queue.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("status", sa.Text, nullable=False, server_default="pending")
    )
    # Documents from milestone 1 were ingested synchronously and are complete.
    op.execute("UPDATE documents SET status = 'ready'")
    op.create_check_constraint(
        "ck_documents_status", "documents", "status IN ('pending','processing','ready','failed')"
    )
    op.add_column("documents", sa.Column("error", sa.Text, nullable=True))
    op.add_column("documents", sa.Column("content_type", sa.Text, nullable=True))
    op.execute("UPDATE documents SET content_type = 'pdf'")
    # Path of the original file in the uploads volume. NULL for milestone-1 rows,
    # which therefore can't be re-indexed (re-upload them instead).
    op.add_column("documents", sa.Column("storage_path", sa.Text, nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger, nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.alter_column("documents", "page_count", nullable=True)

    # Sheet name / heading / slide title, for citations of non-paged formats.
    op.add_column("chunks", sa.Column("section", sa.Text, nullable=True))

    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False, server_default="ingest"),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('ingest','reindex')", name="ck_ingest_jobs_kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')", name="ck_ingest_jobs_status"
        ),
    )
    op.create_index("ix_ingest_jobs_document_id", "ingest_jobs", ["document_id"])
    op.create_index(
        "ix_ingest_jobs_queued",
        "ingest_jobs",
        ["id"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_table("ingest_jobs")
    op.drop_column("chunks", "section")
    op.execute("DELETE FROM documents WHERE page_count IS NULL")
    op.alter_column("documents", "page_count", nullable=False)
    for col in ("updated_at", "size_bytes", "storage_path", "content_type", "error"):
        op.drop_column("documents", col)
    op.drop_constraint("ck_documents_status", "documents")
    op.drop_column("documents", "status")
