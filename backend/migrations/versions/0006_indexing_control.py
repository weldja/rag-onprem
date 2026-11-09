"""Indexing control: excluded documents, progress, and skipped share files.

- documents.status gains 'excluded': kept as a row (so a share scan doesn't add
  it again) but with no chunks, never queued.
- progress_done/progress_total: excerpts embedded so far while processing.
- indexed_at: when the document last became ready.
- source_skipped: files on a share that aren't indexed (unsupported, empty, too
  large, or excluded by SHARE_n_EXCLUDE), replaced on every scan, so the
  Indexing page can show everything in a share, not just what's indexed.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_documents_status", "documents")
    op.create_check_constraint(
        "ck_documents_status",
        "documents",
        "status IN ('pending','processing','ready','failed','excluded')",
    )
    # Jobs stopped because the document was excluded.
    op.drop_constraint("ck_ingest_jobs_status", "ingest_jobs")
    op.create_check_constraint(
        "ck_ingest_jobs_status",
        "ingest_jobs",
        "status IN ('queued','running','done','failed','cancelled')",
    )
    op.add_column("documents", sa.Column("progress_done", sa.Integer, nullable=True))
    op.add_column("documents", sa.Column("progress_total", sa.Integer, nullable=True))
    op.add_column("documents", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE documents SET indexed_at = updated_at WHERE status = 'ready'")
    op.create_table(
        "source_skipped",
        sa.Column(
            "source_id",
            sa.BigInteger,
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text, primary_key=True),  # relative POSIX path (file or folder)
        sa.Column("reason", sa.Text, nullable=False),  # unsupported|empty|too_large|excluded
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("source_skipped")
    op.drop_column("documents", "indexed_at")
    op.drop_column("documents", "progress_total")
    op.drop_column("documents", "progress_done")
    # Excluded documents have no chunks; queue them again rather than lose them.
    op.execute("UPDATE documents SET status = 'pending' WHERE status = 'excluded'")
    op.execute(
        "INSERT INTO ingest_jobs (document_id, kind) "
        "SELECT id, 'ingest' FROM documents d WHERE status = 'pending' AND NOT EXISTS "
        "(SELECT 1 FROM ingest_jobs j WHERE j.document_id = d.id AND j.status = 'queued')"
    )
    op.drop_constraint("ck_documents_status", "documents")
    op.create_check_constraint(
        "ck_documents_status", "documents", "status IN ('pending','processing','ready','failed')"
    )
    op.execute("UPDATE ingest_jobs SET status = 'failed' WHERE status = 'cancelled'")
    op.drop_constraint("ck_ingest_jobs_status", "ingest_jobs")
    op.create_check_constraint(
        "ck_ingest_jobs_status", "ingest_jobs", "status IN ('queued','running','done','failed')"
    )
