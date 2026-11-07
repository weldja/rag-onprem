"""Document rows and the ingest job queue (Postgres, FOR UPDATE SKIP LOCKED)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.tables import chunks, documents, ingest_jobs


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
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Job:
    id: int
    document_id: int
    kind: str
    attempts: int


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


def register(
    engine: Engine,
    *,
    filename: str,
    sha256: str,
    ext: str,
    size: int,
    storage_path: str,
    enqueue: bool,
) -> tuple[int, bool]:
    """Insert a pending document (and an ingest job if enqueue). Returns
    (document_id, created). Identical content returns the existing id."""
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
                )
                .returning(documents.c.id)
            ).scalar_one()
            if enqueue:
                conn.execute(insert(ingest_jobs).values(document_id=doc_id, kind="ingest"))
        return doc_id, True
    except IntegrityError:
        existing = find_by_sha(engine, sha256)
        if existing is None:
            raise
        return existing, False


def find_by_sha(engine: Engine, sha256: str) -> int | None:
    with engine.connect() as conn:
        found: int | None = conn.execute(
            select(documents.c.id).where(documents.c.sha256 == sha256)
        ).scalar_one_or_none()
    return found


def _info_query(embed_model: str) -> Any:
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
        documents.c.created_at,
        documents.c.updated_at,
    )


def list_documents(engine: Engine, embed_model: str) -> list[DocumentInfo]:
    with engine.connect() as conn:
        rows = conn.execute(_info_query(embed_model).order_by(documents.c.id.desc())).all()
    return [DocumentInfo(**r._mapping) for r in rows]


def get_document(engine: Engine, doc_id: int, embed_model: str) -> DocumentInfo:
    with engine.connect() as conn:
        row = conn.execute(_info_query(embed_model).where(documents.c.id == doc_id)).one_or_none()
    if row is None:
        raise NotFoundError(doc_id)
    return DocumentInfo(**row._mapping)


def delete_document(engine: Engine, doc_id: int) -> str | None:
    """Delete the row (chunks and jobs cascade). Returns the stored file path."""
    with engine.begin() as conn:
        path = conn.execute(
            delete(documents).where(documents.c.id == doc_id).returning(documents.c.storage_path)
        ).one_or_none()
    if path is None:
        raise NotFoundError(doc_id)
    stored: str | None = path[0]
    return stored


def enqueue_reindex(engine: Engine, doc_id: int) -> None:
    with engine.begin() as conn:
        row = conn.execute(
            select(documents.c.storage_path, documents.c.status)
            .where(documents.c.id == doc_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise NotFoundError(doc_id)
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
