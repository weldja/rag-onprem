"""Users, groups, sessions and document ACLs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_GROUP = "All staff"


def _ts(name: str, nullable: bool = False) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=None if nullable else sa.func.now(),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("username", sa.Text, nullable=False),  # stored lower-case
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("failed_logins", sa.Integer, nullable=False, server_default="0"),
        _ts("locked_until", nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("username = lower(username)", name="ck_users_username_lower"),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("name", name="uq_groups_name"),
    )
    op.create_table(
        "user_groups",
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "group_id",
            sa.BigInteger,
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_user_groups_group_id", "user_groups", ["group_id"])
    op.create_table(
        "document_groups",
        sa.Column(
            "document_id",
            sa.BigInteger,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "group_id",
            sa.BigInteger,
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_document_groups_group_id", "document_groups", ["group_id"])
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),  # sha256 of the cookie value
        sa.Column(
            "user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        _ts("created_at"),
        _ts("last_seen_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.add_column(
        "documents",
        sa.Column(
            "uploaded_by",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Before this migration every document was visible to everyone. Keep that by
    # sharing existing documents with the default group.
    op.execute(f"INSERT INTO groups (name) VALUES ('{DEFAULT_GROUP}')")
    op.execute(
        "INSERT INTO document_groups (document_id, group_id) "
        f"SELECT d.id, g.id FROM documents d, groups g WHERE g.name = '{DEFAULT_GROUP}'"
    )


def downgrade() -> None:
    op.drop_column("documents", "uploaded_by")
    for table in ("sessions", "document_groups", "user_groups", "groups", "users"):
        op.drop_table(table)
