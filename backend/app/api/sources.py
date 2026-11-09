"""File shares the documents are indexed from (shown in the sidebar).

Shares are configured in .env (SHARE_n_PATH ...) and scanned by the worker;
the API only reports them and lets an administrator ask for a scan now.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from app.api.deps import AdminUser, CurrentUser, Db
from app.ingestion import registry

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceOut(BaseModel):
    id: int
    key: str
    name: str
    display_path: str
    active: bool
    last_scan_at: datetime | None
    last_scan_error: str | None
    scan_requested: bool
    files_seen: int
    files_skipped: int


@router.get("", response_model=list[SourceOut])
def list_sources(viewer: CurrentUser, engine: Db) -> list[SourceOut]:
    return [
        SourceOut.model_validate(s, from_attributes=True)
        for s in registry.list_sources(engine, viewer)
    ]


@router.post("/{source_id}/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_now(source_id: int, _: AdminUser, engine: Db) -> Response:
    """The worker picks the request up within a few seconds."""
    try:
        registry.request_scan(engine, source_id)
    except registry.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found") from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove(source_id: int, _: AdminUser, engine: Db) -> Response:
    try:
        registry.remove_source(engine, source_id)
    except registry.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found") from exc
    except registry.ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
