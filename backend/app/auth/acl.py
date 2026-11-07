"""Document visibility. Enforced in SQL wherever documents or chunks are read,
never by filtering results afterwards.

A user can see a document if they are an admin, uploaded it, or belong to a
group the document is shared with.
"""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, exists, or_, select, true

from app.db.tables import document_groups, documents, user_groups


@dataclass(frozen=True)
class Viewer:
    id: int
    username: str
    is_admin: bool
    display_name: str | None = None


def can_see(viewer: Viewer) -> ColumnElement[bool]:
    """WHERE clause over `documents` limiting rows to those the viewer may see.
    Group membership is read from the database at query time, so removing a user
    from a group takes effect immediately."""
    if viewer.is_admin:
        return true()
    shared_with_viewer = exists(
        select(1)
        .select_from(document_groups)
        .join(user_groups, user_groups.c.group_id == document_groups.c.group_id)
        .where(
            document_groups.c.document_id == documents.c.id,
            user_groups.c.user_id == viewer.id,
        )
    )
    return or_(documents.c.uploaded_by == viewer.id, shared_with_viewer)


def can_manage(viewer: Viewer) -> ColumnElement[bool]:
    """Delete, re-index and re-share: admins and the uploader only."""
    if viewer.is_admin:
        return true()
    return documents.c.uploaded_by == viewer.id
