"""Document rows, their sharing, and the ingest job queue (Postgres, FOR UPDATE
SKIP LOCKED). Every read takes a Viewer and filters in SQL (app.auth.acl)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Engine, delete, exists, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.auth.acl import Viewer, can_manage, can_see
from app.db.tables import (
    chunks,
    document_groups,
    documents,
    ingest_jobs,
    source_skipped,
    sources,
    user_groups,
)
from app.ingestion.attachments import descendant_paths


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
    progress_done: int | None = None  # excerpts embedded so far (while processing)
    progress_total: int | None = None
    indexed_at: datetime | None = None
    queue_position: int | None = None  # 1 = next to be indexed
    parent_id: int | None = None  # the email this document was attached to


@dataclass(frozen=True)
class SkippedFile:
    source_id: int
    path: str
    reason: str  # unsupported | empty | too_large | excluded
    size_bytes: int | None


@dataclass
class BulkResult:
    changed: list[int]
    skipped: dict[int, str]  # document id -> why it was left alone


Action = Literal["reindex", "exclude", "include"]


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
                documents.c.sha256 == sha256,
                documents.c.source_id.is_(None),
                documents.c.parent_id.is_(None),
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
    # Position in the queue: rank of the document's earliest queued job among
    # all queued jobs (one window pass, not a count per document).
    ranked = (
        select(
            ingest_jobs.c.document_id,
            func.row_number().over(order_by=ingest_jobs.c.id).label("pos"),
        )
        .where(ingest_jobs.c.status == "queued")
        .subquery()
    )
    queue = (
        select(ranked.c.document_id, func.min(ranked.c.pos).label("queue_position"))
        .group_by(ranked.c.document_id)
        .subquery()
    )
    return (
        select(
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
            documents.c.progress_done,
            documents.c.progress_total,
            documents.c.indexed_at,
            queue.c.queue_position,
            documents.c.parent_id,
        )
        .select_from(documents.outerjoin(queue, queue.c.document_id == documents.c.id))
        .where(can_see(viewer))
    )


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
            documents.c.parent_id,
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


_ATTACHMENT_FOLLOWS = (
    "This document is an email attachment. It follows its email: change or remove the email."
)


def delete_document(engine: Engine, doc_id: int, viewer: Viewer) -> tuple[str | None, list[str]]:
    """Delete an uploaded document (chunks, jobs, sharing and attachment
    documents cascade). Returns its stored file and its attachments' files
    (see attachments.discard_unreferenced). File-share documents follow the
    share: the next scan would only add them again."""
    with engine.begin() as conn:
        row = _require_manage(conn, doc_id, viewer)
        if row.source_id is not None:
            raise ConflictError(
                "This document comes from a file share. Remove or exclude it on the "
                "share; the index follows at the next scan."
            )
        if row.parent_id is not None:
            raise ConflictError(_ATTACHMENT_FOLLOWS)
        paths = descendant_paths(conn, doc_id)
        conn.execute(delete(documents).where(documents.c.id == doc_id))
    own: str | None = row.storage_path
    return own, paths


def enqueue_reindex(engine: Engine, doc_id: int, viewer: Viewer) -> None:
    with engine.begin() as conn:
        row = _require_manage(conn, doc_id, viewer)
        if row.storage_path is None:
            raise ConflictError("The original file wasn't kept for this document; upload it again")
        if row.status in ("pending", "processing"):
            raise ConflictError("This document is already queued or being processed")
        if row.status == "excluded":
            raise ConflictError("This document is excluded from the index; include it first")
        conn.execute(
            update(documents)
            .where(documents.c.id == doc_id)
            .values(status="pending", error=None, updated_at=func.now())
        )
        conn.execute(insert(ingest_jobs).values(document_id=doc_id, kind="reindex"))


def apply_action(engine: Engine, action: Action, doc_ids: list[int], viewer: Viewer) -> BulkResult:
    """The Indexing page's actions, on documents the viewer may manage, in one
    transaction (the rows are locked, so the worker sees the change at its next
    excerpt and stops).
      reindex  ready/failed -> queued again
      exclude  anything -> excluded: queued or running jobs cancelled, chunks
               deleted, never queued again (a share scan keeps it excluded)
      include  excluded -> queued again
    """
    result = BulkResult(changed=[], skipped={})
    wanted = sorted(set(doc_ids))
    with engine.begin() as conn:
        rows = {
            r.id: r
            for r in conn.execute(
                select(
                    documents.c.id,
                    documents.c.status,
                    documents.c.storage_path,
                    can_manage(viewer).label("manage"),
                )
                .where(documents.c.id.in_(wanted), can_see(viewer))
                .with_for_update(of=documents)
            )
        }
        for doc_id in wanted:
            row = rows.get(doc_id)
            if row is None:
                result.skipped[doc_id] = "not found"
                continue
            if not row.manage:
                result.skipped[doc_id] = "only the uploader or an administrator can change it"
                continue
            reason = _apply_one(conn, action, row)
            if reason:
                result.skipped[doc_id] = reason
            else:
                result.changed.append(doc_id)
    return result


def _descendants(conn: Any, doc_id: int) -> list[int]:
    """An email's attachment documents, at any depth."""
    return list(
        conn.execute(
            text(
                """
                WITH RECURSIVE tree(id) AS (
                    SELECT id FROM documents WHERE parent_id = :doc
                    UNION ALL
                    SELECT d.id FROM documents d JOIN tree t ON d.parent_id = t.id)
                SELECT id FROM tree
                """
            ),
            {"doc": doc_id},
        ).scalars()
    )


def _apply_one(conn: Any, action: Action, row: Any) -> str | None:
    """Returns why the document was left alone, or None if changed. Excluding
    or including an email does the same to its attachments."""
    if action == "exclude":
        if row.status == "excluded":
            return "already excluded"
        targets = [row.id, *_descendants(conn, row.id)]
        conn.execute(
            update(ingest_jobs)
            .where(
                ingest_jobs.c.document_id.in_(targets),
                ingest_jobs.c.status.in_(["queued", "running"]),
            )
            .values(status="cancelled", finished_at=func.now())
        )
        conn.execute(delete(chunks).where(chunks.c.document_id.in_(targets)))
        for doc in targets:
            _set(conn, doc, "excluded")
        return None
    if action == "include":
        if row.status != "excluded":
            return "not excluded"
        # The email's attachments come back when it is indexed again: its
        # re-index re-creates or re-queues them.
        for child in _descendants(conn, row.id):
            _set(conn, child, "pending")
            conn.execute(insert(ingest_jobs).values(document_id=child, kind="ingest"))
        _set(conn, row.id, "pending")
        conn.execute(insert(ingest_jobs).values(document_id=row.id, kind="ingest"))
        return None
    # reindex
    if row.status in ("pending", "processing"):
        return "already queued or being indexed"
    if row.status == "excluded":
        return "excluded; include it to index it again"
    if row.storage_path is None:
        return "the original file wasn't kept"
    _set(conn, row.id, "pending")
    conn.execute(insert(ingest_jobs).values(document_id=row.id, kind="reindex"))
    return None


def _set(conn: Any, doc_id: int, status: str) -> None:
    conn.execute(
        update(documents)
        .where(documents.c.id == doc_id)
        .values(
            status=status,
            error=None,
            progress_done=None,
            progress_total=None,
            updated_at=func.now(),
        )
    )


def list_skipped(engine: Engine) -> list[SkippedFile]:
    """Files on the shares that aren't indexed (administrators only: paths on a
    share aren't filtered by document visibility)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                source_skipped.c.source_id,
                source_skipped.c.path,
                source_skipped.c.reason,
                source_skipped.c.size_bytes,
            ).order_by(source_skipped.c.source_id, source_skipped.c.path)
        ).all()
    return [SkippedFile(**dict(r._mapping)) for r in rows]


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
            if row.parent_id is not None:
                raise ConflictError(_ATTACHMENT_FOLLOWS)
            # An email's attachments (at any depth) are shared exactly as it is.
            targets = [doc_id, *_descendants(conn, doc_id)]
            conn.execute(delete(document_groups).where(document_groups.c.document_id.in_(targets)))
            if wanted:
                conn.execute(
                    insert(document_groups),
                    [{"document_id": d, "group_id": g} for d in targets for g in wanted],
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


def report_progress(engine: Engine, doc_id: int, done: int, total: int) -> bool:
    """Record indexing progress. False if the document is no longer being
    processed (excluded or deleted meanwhile): the worker then stops."""
    with engine.begin() as conn:
        row = conn.execute(
            update(documents)
            .where(documents.c.id == doc_id, documents.c.status == "processing")
            .values(progress_done=done, progress_total=total)
            .returning(documents.c.id)
        ).one_or_none()
    return row is not None


def finish_job(
    engine: Engine,
    job_id: int,
    *,
    error: str | None = None,
    retry: bool = False,
    cancelled: bool = False,
) -> None:
    status = "cancelled" if cancelled else "queued" if retry else "failed" if error else "done"
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
    """Jobs left 'running' by a worker that died go back on the queue, and
    their documents show as queued again (not as still being indexed)."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE ingest_jobs SET status = 'queued' "
                "WHERE status = 'running' AND started_at < now() - make_interval(secs => :s)"
            ),
            {"s": older_than_s},
        )
        conn.execute(
            text(
                "UPDATE documents d SET status = 'pending', progress_done = NULL, "
                "progress_total = NULL WHERE d.status = 'processing' AND NOT EXISTS "
                "(SELECT 1 FROM ingest_jobs j WHERE j.document_id = d.id AND j.status = 'running')"
            )
        )
    return result.rowcount
