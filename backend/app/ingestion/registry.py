"""Document rows, their sharing, and the ingest job queue (Postgres, FOR UPDATE
SKIP LOCKED). Every read takes a Viewer and filters in SQL (app.auth.acl)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, delete, exists, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.auth.acl import Viewer, can_manage, can_see
from app.db.tables import chunks, document_groups, documents, ingest_jobs, sources, user_groups


@dataclass(frozen=True)
class DocumentInfo:
    id: int
    filename: str
    status: str
    error: str | None
    content_type: str | None
    size_bytes: int | None
    page_count: int | None
    chunks: int
    stale: bool  # has chunks embedded with a different model than the current one
    uploaded_by: int | None
    group_ids: list[int]
    can_manage: bool
    created_at: datetime
    updated_at: datetime
    source_id: int | None = None  # file share; None for uploads
    source_path: str | None = None  # path within the share


@dataclass(frozen=True)
class SourceInfo:
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


@dataclass(frozen=True)
class Job:
    id: int
    document_id: int
    kind: str
    attempts: int


class NotFoundError(LookupError):
    """Doesn't exist, or the viewer isn't allowed to know it exists."""


class ForbiddenError(PermissionError):
    pass


class ConflictError(RuntimeError):
    pass


def check_group_ids(engine: Engine, viewer: Viewer, group_ids: list[int]) -> list[int]:
    """Non-admins may only share with groups they belong to."""
    wanted = sorted(set(group_ids))
    if viewer.is_admin or not wanted:
        return wanted
    with engine.connect() as conn:
        mine = set(
            conn.execute(
                select(user_groups.c.group_id).where(user_groups.c.user_id == viewer.id)
            ).scalars()
        )
    if not set(wanted) <= mine:
        raise ForbiddenError("You can only share documents with groups you belong to")
    return wanted


def register(
    engine: Engine,
    *,
    filename: str,
    sha256: str,
    ext: str,
    size: int,
    storage_path: str,
    enqueue: bool,
    uploaded_by: int | None = None,
    group_ids: list[int] | None = None,
) -> tuple[int, bool]:
    """Insert a pending document shared with group_ids (and an ingest job if
    enqueue). Returns (document_id, created); identical content returns the
    existing id."""
    try:
        with engine.begin() as conn:
            doc_id: int = conn.execute(
                insert(documents)
                .values(
                    filename=filename,
                    sha256=sha256,
                    content_type=ext,
                    size_bytes=size,
                    storage_path=storage_path,
                    status="pending",
                    uploaded_by=uploaded_by,
                )
                .returning(documents.c.id)
            ).scalar_one()
            if group_ids:
                conn.execute(
                    insert(document_groups),
                    [{"document_id": doc_id, "group_id": g} for g in group_ids],
                )
            if enqueue:
                conn.execute(insert(ingest_jobs).values(document_id=doc_id, kind="ingest"))
        return doc_id, True
    except IntegrityError:
        existing = find_by_sha(engine, sha256)
        if existing is None:
            raise
        return existing, False


def find_by_sha(engine: Engine, sha256: str) -> int | None:
    """The uploaded document with this content. File-share copies don't count:
    uploading a file that is also on a share creates a separate document."""
    with engine.connect() as conn:
        found: int | None = conn.execute(
            select(documents.c.id).where(
                documents.c.sha256 == sha256, documents.c.source_id.is_(None)
            )
        ).scalar_one_or_none()
    return found


def _info_query(embed_model: str, viewer: Viewer) -> Any:
    n_chunks = (
        select(func.count())
        .where(chunks.c.document_id == documents.c.id)
        .correlate(documents)
        .scalar_subquery()
    )
    stale = (
        select(func.count() > 0)
        .where(chunks.c.document_id == documents.c.id, chunks.c.embed_model != embed_model)
        .correlate(documents)
        .scalar_subquery()
    )
    shared = (
        select(func.coalesce(func.array_agg(document_groups.c.group_id), text("'{}'::bigint[]")))
        .where(document_groups.c.document_id == documents.c.id)
        .correlate(documents)
        .scalar_subquery()
    )
    return select(
        documents.c.id,
        documents.c.filename,
        documents.c.status,
        documents.c.error,
        documents.c.content_type,
        documents.c.size_bytes,
        documents.c.page_count,
        n_chunks.label("chunks"),
        stale.label("stale"),
        documents.c.uploaded_by,
        shared.label("group_ids"),
        can_manage(viewer).label("can_manage"),
        documents.c.created_at,
        documents.c.updated_at,
        documents.c.source_id,
        documents.c.source_path,
    ).where(can_see(viewer))


def _to_info(row: Any) -> DocumentInfo:
    data = dict(row._mapping)
    data["group_ids"] = sorted(data["group_ids"] or [])
    data["can_manage"] = bool(data["can_manage"])
    return DocumentInfo(**data)


def list_documents(engine: Engine, embed_model: str, viewer: Viewer) -> list[DocumentInfo]:
    with engine.connect() as conn:
        rows = conn.execute(_info_query(embed_model, viewer).order_by(documents.c.id.desc())).all()
    return [_to_info(r) for r in rows]


def get_document(engine: Engine, doc_id: int, embed_model: str, viewer: Viewer) -> DocumentInfo:
    with engine.connect() as conn:
        row = conn.execute(
            _info_query(embed_model, viewer).where(documents.c.id == doc_id)
        ).one_or_none()
    if row is None:
        raise NotFoundError(doc_id)
    return _to_info(row)


def visible_file(engine: Engine, doc_id: int, viewer: Viewer) -> tuple[str, str, str | None]:
    """(storage_path, filename, ext) of a document the viewer may see."""
    with engine.connect() as conn:
        row = conn.execute(
            select(documents.c.storage_path, documents.c.filename, documents.c.content_type).where(
                documents.c.id == doc_id, can_see(viewer)
            )
        ).one_or_none()
    if row is None or row.storage_path is None:
        raise NotFoundError(doc_id)
    return row.storage_path, row.filename, row.content_type


def _require_manage(conn: Any, doc_id: int, viewer: Viewer) -> Any:
    row = conn.execute(
        select(
            documents.c.storage_path,
            documents.c.status,
            documents.c.source_id,
            can_manage(viewer).label("manage"),
        )
        .where(documents.c.id == doc_id, can_see(viewer))
        .with_for_update(of=documents)
    ).one_or_none()
    if row is None:
        raise NotFoundError(doc_id)
    if not row.manage:
        raise ForbiddenError("Only the uploader or an administrator can change this document")
    return row


def delete_document(engine: Engine, doc_id: int, viewer: Viewer) -> str | None:
    """Delete an uploaded document (chunks, jobs and sharing cascade). Returns the
    stored file path. File-share documents follow the share: the next scan
    would only add them again."""
    with engine.begin() as conn:
        row = _require_manage(conn, doc_id, viewer)
        if row.source_id is not None:
            raise ConflictError(
                "This document comes from a file share. Remove or exclude it on the "
                "share; the index follows at the next scan."
            )
        conn.execute(delete(documents).where(documents.c.id == doc_id))
    path: str | None = row.storage_path
    return path


def enqueue_reindex(engine: Engine, doc_id: int, viewer: Viewer) -> None:
    with engine.begin() as conn:
        row = _require_manage(conn, doc_id, viewer)
        if row.storage_path is None:
            raise ConflictError("The original file wasn't kept for this document; upload it again")
        if row.status in ("pending", "processing"):
            raise ConflictError("This document is already queued or being processed")
        conn.execute(
            update(documents)
            .where(documents.c.id == doc_id)
            .values(status="pending", error=None, updated_at=func.now())
        )
        conn.execute(insert(ingest_jobs).values(document_id=doc_id, kind="reindex"))


def set_groups(engine: Engine, doc_id: int, group_ids: list[int], viewer: Viewer) -> None:
    wanted = check_group_ids(engine, viewer, group_ids)
    try:
        with engine.begin() as conn:
            row = _require_manage(conn, doc_id, viewer)
            if row.source_id is not None:
                raise ConflictError(
                    "File-share documents are visible to the share's groups "
                    "(SHARE_n_GROUPS in .env), not set per document"
                )
            conn.execute(delete(document_groups).where(document_groups.c.document_id == doc_id))
            if wanted:
                conn.execute(
                    insert(document_groups),
                    [{"document_id": doc_id, "group_id": g} for g in wanted],
                )
    except IntegrityError as exc:
        raise ConflictError("One of those groups doesn't exist") from exc


# --------------------------------------------------------------- file shares


def list_sources(engine: Engine, viewer: Viewer) -> list[SourceInfo]:
    """File shares, for the sidebar. Non-admins see only shares holding a
    document they may see, so a share's name doesn't leak to other groups."""
    stmt = select(
        sources.c.id,
        sources.c.key,
        sources.c.name,
        sources.c.display_path,
        sources.c.active,
        sources.c.last_scan_at,
        sources.c.last_scan_error,
        sources.c.scan_requested_at.is_not(None).label("scan_requested"),
        sources.c.files_seen,
        sources.c.files_skipped,
    ).order_by(sources.c.id)
    if not viewer.is_admin:
        stmt = stmt.where(
            exists(
                select(1)
                .select_from(documents)
                .where(documents.c.source_id == sources.c.id, can_see(viewer))
            )
        )
    with engine.connect() as conn:
        return [SourceInfo(**dict(r._mapping)) for r in conn.execute(stmt)]


def request_scan(engine: Engine, source_id: int) -> None:
    with engine.begin() as conn:
        found = conn.execute(
            update(sources)
            .where(sources.c.id == source_id, sources.c.active)
            .values(scan_requested_at=func.now())
            .returning(sources.c.id)
        ).one_or_none()
    if found is None:
        raise NotFoundError(source_id)


def remove_source(engine: Engine, source_id: int) -> None:
    """Forget a share that is no longer configured, with all its documents."""
    with engine.begin() as conn:
        row = conn.execute(
            select(sources.c.active).where(sources.c.id == source_id).with_for_update()
        ).one_or_none()
        if row is None:
            raise NotFoundError(source_id)
        if row.active:
            raise ConflictError(
                "This share is still configured. Remove SHARE_n_PATH from .env and restart first."
            )
        conn.execute(delete(sources).where(sources.c.id == source_id))  # documents cascade


# ----------------------------------------------------------------------- queue


def claim_job(engine: Engine) -> Job | None:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status = 'running', started_at = now(), attempts = attempts + 1
                 WHERE id = (
                       SELECT id FROM ingest_jobs
                        WHERE status = 'queued'
                        ORDER BY id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1)
             RETURNING id, document_id, kind, attempts
                """
            )
        ).one_or_none()
    return Job(row.id, row.document_id, row.kind, row.attempts) if row else None


def finish_job(
    engine: Engine, job_id: int, *, error: str | None = None, retry: bool = False
) -> None:
    status = "queued" if retry else ("failed" if error else "done")
    with engine.begin() as conn:
        conn.execute(
            update(ingest_jobs)
            .where(ingest_jobs.c.id == job_id)
            .values(
                status=status,
                error=error,
                finished_at=None if retry else func.now(),
            )
        )


def requeue_stale_jobs(engine: Engine, older_than_s: float) -> int:
    """Jobs left 'running' by a worker that died go back on the queue."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE ingest_jobs SET status = 'queued' "
                "WHERE status = 'running' AND started_at < now() - make_interval(secs => :s)"
            ),
            {"s": older_than_s},
        )
    return result.rowcount
