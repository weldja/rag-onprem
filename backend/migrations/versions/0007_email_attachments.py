"""Email attachments as documents of their own.

- documents.parent_id: the email (.msg) a document was attached to. Deleting
  the email deletes its attachments (and theirs).
- documents.attachment_key: the attachment's position in the email, so a
  re-indexed email updates the same attachment documents.
- sha256 stays unique among uploads, but only for top-level documents: the
  same file attached to two uploaded emails is two attachment documents
  (they share chunks via copy_from_duplicate, as share copies do).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "parent_id",
            sa.BigInteger,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column("documents", sa.Column("attachment_key", sa.Text, nullable=True))
    op.create_check_constraint(
        "ck_documents_attachment",
        "documents",
        "(parent_id IS NULL) = (attachment_key IS NULL)",
    )
    op.create_index(
        "uq_documents_attachment",
        "documents",
        ["parent_id", "attachment_key"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )
    op.drop_index("uq_documents_upload_sha256", "documents")
    op.create_index(
        "uq_documents_upload_sha256",
        "documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("source_id IS NULL AND parent_id IS NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM documents WHERE parent_id IS NOT NULL")
    op.drop_index("uq_documents_upload_sha256", "documents")
    op.create_index(
        "uq_documents_upload_sha256",
        "documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("source_id IS NULL"),
    )
    op.drop_index("uq_documents_attachment", "documents")
    op.drop_constraint("ck_documents_attachment", "documents")
    op.drop_column("documents", "attachment_key")
    op.drop_column("documents", "parent_id")
