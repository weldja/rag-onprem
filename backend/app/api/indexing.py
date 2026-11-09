"""The Indexing page: bulk index actions and files on shares that aren't indexed.

The document list itself is GET /api/documents (with progress and queue
position). Actions apply only to documents the caller may manage.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, Db
from app.ingestion import registry

router = APIRouter(prefix="/api/indexing", tags=["indexing"])


class ActionIn(BaseModel):
    action: Literal["reindex", "exclude", "include"]
    document_ids: list[int] = Field(min_length=1, max_length=5000)


class SkippedOut(BaseModel):
    id: int
    reason: str


class ActionOut(BaseModel):
    changed: list[int]
    skipped: list[SkippedOut]


class SkippedFileOut(BaseModel):
    source_id: int
    path: str
    reason: str
    size_bytes: int | None


@router.post("/actions", response_model=ActionOut)
def act(body: ActionIn, viewer: CurrentUser, engine: Db) -> ActionOut:
    result = registry.apply_action(engine, body.action, body.document_ids, viewer)
    return ActionOut(
        changed=result.changed,
        skipped=[SkippedOut(id=k, reason=v) for k, v in result.skipped.items()],
    )


@router.get("/skipped", response_model=list[SkippedFileOut])
def skipped(_admin: AdminUser, engine: Db) -> list[SkippedFileOut]:
    return [
        SkippedFileOut.model_validate(s, from_attributes=True)
        for s in registry.list_skipped(engine)
    ]
