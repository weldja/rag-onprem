"""Directory (LDAP/Active Directory) users.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("auth_source", sa.Text, nullable=False, server_default="local")
    )
    op.create_check_constraint("ck_users_auth_source", "users", "auth_source IN ('local', 'ldap')")
    # Distinguished name at the last successful directory sign-in.
    op.add_column("users", sa.Column("ldap_dn", sa.Text, nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "ldap_dn")
    op.drop_constraint("ck_users_auth_source", "users")
    op.drop_column("users", "auth_source")
