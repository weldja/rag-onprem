"""Document upload, listing, status, download, sharing, deletion and re-indexing.

Uploads are stored and queued; the worker service does the indexing. Every
endpoint is scoped to what the signed-in user may see (app.auth.acl).
"""

import logging
from datetime import datetime
from pathlib import Path, PurePath
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.deps import AppSettings, CurrentUser, Db
from app.ingestion import registry, storage
from app.ingestion.loaders import SUPPORTED_EXTENSIONS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])

# Served inline (the browser shows them); everything else downloads. Never HTML:
# an uploaded page rendered on our origin could run script as the viewer.
_INLINE_TYPES = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8"}


class DocumentOut(BaseModel):
    id: int
    filename: str
    status: str
    error: str | None
    content_type: str | None
    size_bytes: int | None
    page_count: int | None
    chunks: int
    stale: bool
    uploaded_by: int | None
    group_ids: list[int]
    can_manage: bool
    created_at: datetime
    updated_at: datetime


class UploadOut(BaseModel):
    document: DocumentOut
    duplicate: bool


class GroupsIn(BaseModel):
    group_ids: list[int]


def _out(info: registry.DocumentInfo) -> DocumentOut:
    return DocumentOut.model_validate(info, from_attributes=True)


def _safe_filename(name: str | None) -> str:
    # Keep only the final path component; browsers may send full paths.
    base = PurePath((name or "").replace("\\", "/")).name.strip()
    return base[:255] or "upload"


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=UploadOut)
def upload(
    file: UploadFile,
    response: Response,
    viewer: CurrentUser,
    engine: Db,
    settings: AppSettings,
    group_ids: Annotated[list[int] | None, Form()] = None,
) -> UploadOut:
    """Upload a document. group_ids: groups to share it with (must be your own
    groups unless you're an admin). With none, only you and admins can see it."""
    filename = _safe_filename(file.filename)
    ext = PurePath(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type '.{ext}'. Supported: "
            + ", ".join(f".{e}" for e in sorted(SUPPORTED_EXTENSIONS)),
        )
    try:
        shared_with = registry.check_group_ids(engine, viewer, group_ids or [])
    except registry.ForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    try:
        received = storage.receive(
            file.file, settings.upload_dir, settings.max_upload_mb * 1024 * 1024
        )
    except storage.TooLargeError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    if received.size == 0:
        storage.discard(received.path)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty")

    existing = registry.find_by_sha(engine, received.sha256)
    if existing is not None:
        storage.discard(received.path)
        try:
            info = registry.get_document(engine, existing, settings.embed_model, viewer)
        except registry.NotFoundError as exc:
            # Identical content uploaded by someone this user can't see. Saying so
            # reveals only that a file the user already holds exists here.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This file has already been uploaded by someone else. "
                "Ask an administrator to share it with you.",
            ) from exc
        response.status_code = status.HTTP_200_OK
        return UploadOut(document=_out(info), duplicate=True)

    target = storage.finalise(received, settings.upload_dir, ext)
    doc_id, created = registry.register(
        engine,
        filename=filename,
        sha256=received.sha256,
        ext=ext,
        size=received.size,
        storage_path=str(target),
        enqueue=True,
        uploaded_by=viewer.id,
        group_ids=shared_with,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    log.info("User %d queued document %d (.%s, %d bytes)", viewer.id, doc_id, ext, received.size)
    return UploadOut(
        document=_out(registry.get_document(engine, doc_id, settings.embed_model, viewer)),
        duplicate=not created,
    )


@router.get("", response_model=list[DocumentOut])
def list_documents(viewer: CurrentUser, engine: Db, settings: AppSettings) -> list[DocumentOut]:
    return [_out(d) for d in registry.list_documents(engine, settings.embed_model, viewer)]


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(
    doc_id: int, viewer: CurrentUser, engine: Db, settings: AppSettings
) -> DocumentOut:
    try:
        return _out(registry.get_document(engine, doc_id, settings.embed_model, viewer))
    except registry.NotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/{doc_id}/file")
def download(doc_id: int, viewer: CurrentUser, engine: Db) -> FileResponse:
    try:
        path, filename, ext = registry.visible_file(engine, doc_id, viewer)
    except registry.NotFoundError as exc:
        raise _not_found(exc) from exc
    if not Path(path).is_file():
        raise HTTPException(status.HTTP_410_GONE, "The original file is no longer stored")
    inline = _INLINE_TYPES.get(ext or "")
    return FileResponse(
        path,
        media_type=inline or "application/octet-stream",
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.put("/{doc_id}/groups", response_model=DocumentOut)
def share(
    doc_id: int, body: GroupsIn, viewer: CurrentUser, engine: Db, settings: AppSettings
) -> DocumentOut:
    try:
        registry.set_groups(engine, doc_id, body.group_ids, viewer)
    except registry.NotFoundError as exc:
        raise _not_found(exc) from exc
    except registry.ForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except registry.ConflictError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _out(registry.get_document(engine, doc_id, settings.embed_model, viewer))


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(doc_id: int, viewer: CurrentUser, engine: Db) -> Response:
    try:
        path = registry.delete_document(engine, doc_id, viewer)
    except registry.NotFoundError as exc:
        raise _not_found(exc) from exc
    except registry.ForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    storage.discard(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{doc_id}/reindex", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentOut)
def reindex(doc_id: int, viewer: CurrentUser, engine: Db, settings: AppSettings) -> DocumentOut:
    try:
        registry.enqueue_reindex(engine, doc_id, viewer)
    except registry.NotFoundError as exc:
        raise _not_found(exc) from exc
    except registry.ForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except registry.ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _out(registry.get_document(engine, doc_id, settings.embed_model, viewer))
