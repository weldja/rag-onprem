"""Document upload, listing, status, deletion and re-indexing.

Uploads are stored and queued; the worker service does the indexing.
"""

import logging
from datetime import datetime
from pathlib import PurePath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import Engine

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.ingestion import registry, storage
from app.ingestion.loaders import SUPPORTED_EXTENSIONS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])


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
    created_at: datetime
    updated_at: datetime


class UploadOut(BaseModel):
    document: DocumentOut
    duplicate: bool


def _out(info: registry.DocumentInfo) -> DocumentOut:
    return DocumentOut.model_validate(info, from_attributes=True)


def _safe_filename(name: str | None) -> str:
    # Keep only the final path component; browsers may send full paths.
    base = PurePath((name or "").replace("\\", "/")).name.strip()
    return base[:255] or "upload"


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=UploadOut)
def upload(
    file: UploadFile,
    response: Response,
    engine: Annotated[Engine, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadOut:
    filename = _safe_filename(file.filename)
    ext = PurePath(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type '.{ext}'. Supported: "
            + ", ".join(f".{e}" for e in sorted(SUPPORTED_EXTENSIONS)),
        )
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
        response.status_code = status.HTTP_200_OK
        return UploadOut(
            document=_out(registry.get_document(engine, existing, settings.embed_model)),
            duplicate=True,
        )

    target = storage.finalise(received, settings.upload_dir, ext)
    doc_id, created = registry.register(
        engine,
        filename=filename,
        sha256=received.sha256,
        ext=ext,
        size=received.size,
        storage_path=str(target),
        enqueue=True,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    log.info("Queued document %d (.%s, %d bytes)", doc_id, ext, received.size)
    return UploadOut(
        document=_out(registry.get_document(engine, doc_id, settings.embed_model)),
        duplicate=not created,
    )


@router.get("", response_model=list[DocumentOut])
def list_documents(
    engine: Annotated[Engine, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[DocumentOut]:
    return [_out(d) for d in registry.list_documents(engine, settings.embed_model)]


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(
    doc_id: int,
    engine: Annotated[Engine, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentOut:
    try:
        return _out(registry.get_document(engine, doc_id, settings.embed_model))
    except registry.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found") from exc


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(doc_id: int, engine: Annotated[Engine, Depends(get_db)]) -> Response:
    try:
        path = registry.delete_document(engine, doc_id)
    except registry.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found") from exc
    storage.discard(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{doc_id}/reindex", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentOut)
def reindex(
    doc_id: int,
    engine: Annotated[Engine, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentOut:
    try:
        registry.enqueue_reindex(engine, doc_id)
    except registry.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found") from exc
    except registry.ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _out(registry.get_document(engine, doc_id, settings.embed_model))
