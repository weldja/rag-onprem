"""Process one registered document: load -> split -> embed -> replace its chunks.

Used by the worker (uploads, re-index jobs) and the CLI. All of a document's
chunks are replaced in a single transaction, so a re-index never leaves a
half-written document visible to search.
"""

import logging
import time
from collections.abc import Callable
from itertools import batched
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, delete, func, insert, select, text, update

from app.core.config import Settings
from app.db.tables import chunks, documents
from app.ingestion import attachments
from app.ingestion.chunking import Document, RecursiveTextSplitter
from app.ingestion.loaders import LoadError, is_toc_chunk, load_file
from app.ingestion.msg import Email, MsgError, read_msg
from app.ingestion.ocr import OcrOptions
from app.llm.base import Embedder

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """Permanent failure for this document (bad file, no text, wrong dimension)."""


class IndexCancelled(RuntimeError):
    """The document was excluded or deleted while it was being indexed."""


def split(pieces: list[Document], settings: Settings) -> list[Document]:
    splitter = RecursiveTextSplitter(settings.chunk_size, settings.chunk_overlap)
    out: list[Document] = []
    for piece in pieces:
        if piece.metadata.get("pre_chunked"):
            out.append(piece)
        else:
            out.extend(splitter.split_documents([piece]))
    return [c for c in out if not is_toc_chunk(c.page_content, c.metadata.get("page"))]


def embedding_text(filename: str, piece: Document) -> str:
    """What gets embedded for a chunk: its text under the document's name (and
    section heading, if any). A chunk deep in a form or manual rarely names the
    document itself, so a question that does ("what priority was the EMS NBI
    trap assessment change request given?") missed it. Only the vector sees
    this; the stored chunk text, keyword search and the prompt are unchanged."""
    title = Path(filename).stem.replace("_", " ").strip()
    section = piece.metadata.get("section")
    if section and not piece.page_content.startswith(section):
        title = f"{title} - {section}"
    return f"{title}\n\n{piece.page_content}"


def ocr_options(settings: Settings) -> OcrOptions:
    return OcrOptions(
        enabled=settings.ocr_enabled,
        languages=settings.ocr_languages,
        dpi=settings.ocr_dpi,
        min_chars=settings.ocr_min_chars,
        max_pages=settings.ocr_max_pages,
        timeout_s=settings.ocr_timeout_s,
    )


def set_status(engine: Engine, doc_id: int, status: str, error: str | None = None) -> None:
    """Never overrides an administrator's exclusion."""
    with engine.begin() as conn:
        conn.execute(
            update(documents)
            .where(documents.c.id == doc_id, documents.c.status != "excluded")
            .values(
                status=status,
                error=error,
                progress_done=None,
                progress_total=None,
                updated_at=func.now(),
            )
        )


def _lock_still_processing(conn: Any, doc_id: int) -> None:
    """Inside the transaction that writes the result: stop if the document was
    excluded or deleted meanwhile (the row lock orders us after that change)."""
    status = conn.execute(
        select(documents.c.status).where(documents.c.id == doc_id).with_for_update()
    ).scalar_one_or_none()
    if status != "processing":
        raise IndexCancelled(doc_id)


def _mark_ready(conn: Any, doc_id: int, page_count: int | None) -> None:
    conn.execute(
        update(documents)
        .where(documents.c.id == doc_id)
        .values(
            status="ready",
            error=None,
            page_count=page_count,
            progress_done=None,
            progress_total=None,
            indexed_at=func.now(),
            updated_at=func.now(),
        )
    )


def copy_from_duplicate(engine: Engine, doc_id: int, sha256: str, embed_model: str) -> int | None:
    """If another ready document has the same content, fully embedded with the
    current model, copy its chunks instead of parsing and embedding again (file
    shares often hold the same file in several folders). Returns the number of
    chunks copied, or None when there is no such document."""
    with engine.begin() as conn:
        donor = conn.execute(
            text(
                """
                SELECT d.id, d.page_count FROM documents d
                 WHERE d.sha256 = :sha AND d.id <> :doc AND d.status = 'ready'
                   AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)
                   AND NOT EXISTS (SELECT 1 FROM chunks c
                                    WHERE c.document_id = d.id AND c.embed_model <> :model)
                 ORDER BY d.id LIMIT 1
                """
            ),
            {"sha": sha256, "doc": doc_id, "model": embed_model},
        ).one_or_none()
        if donor is None:
            return None
        _lock_still_processing(conn, doc_id)
        conn.execute(delete(chunks).where(chunks.c.document_id == doc_id))
        copied = conn.execute(
            text(
                """
                INSERT INTO chunks
                       (document_id, page, section, ordinal, text, embedding, embed_model)
                SELECT :doc, page, section, ordinal, text, embedding, embed_model
                  FROM chunks WHERE document_id = :donor
                """
            ),
            {"doc": doc_id, "donor": donor.id},
        ).rowcount
        _mark_ready(conn, doc_id, donor.page_count)
    return int(copied)


def process_document(
    engine: Engine,
    doc_id: int,
    *,
    embedder: Embedder,
    settings: Settings,
    progress: Callable[[int, int], object] | None = None,
) -> int:
    """Index (or re-index) a document. Returns the number of chunks written.
    Raises IngestError for permanent problems and IndexCancelled if the document
    is excluded or deleted (before or during indexing); model errors propagate
    so the caller can retry. `progress(done, total)` runs before each embedding
    request: the worker reports progress there, lets questions go first, and
    raises IndexCancelled to stop."""
    with engine.begin() as conn:
        row = conn.execute(
            update(documents)
            .where(documents.c.id == doc_id, documents.c.status != "excluded")
            .values(status="processing", progress_done=None, progress_total=None)
            .returning(
                documents.c.filename,
                documents.c.storage_path,
                documents.c.content_type,
                documents.c.sha256,
                documents.c.source_id,
            )
        ).one_or_none()
    if row is None:
        raise IndexCancelled(doc_id)
    if not row.storage_path or not Path(row.storage_path).is_file():
        if row.source_id is not None:
            raise IngestError(f"{row.filename} is no longer on the share (or can't be read)")
        raise IngestError(f"Original file for {row.filename} is missing; upload it again")
    started = time.monotonic()
    ext = (row.content_type or Path(row.filename).suffix).lower().lstrip(".")

    copied = copy_from_duplicate(engine, doc_id, row.sha256, embedder.embed_model)
    if copied is not None:
        queued = ""
        if ext == "msg":  # the copy has no attachment documents of its own yet
            try:
                mail = read_msg(row.storage_path)
            except MsgError as exc:
                raise IngestError(str(exc)) from exc
            queued = _sync_attachments(engine, doc_id, mail, settings)
        log.info(
            "Document %d: copied %d chunks from an identical document in %.1f s%s",
            doc_id,
            copied,
            time.monotonic() - started,
            queued,
        )
        return copied

    try:
        loaded = load_file(
            Path(row.storage_path),
            ext,
            sheet_chunk_chars=settings.sheet_chunk_chars,
            ocr=ocr_options(settings),
        )
    except LoadError as exc:
        raise IngestError(str(exc)) from exc
    if not loaded.pages:
        hint = "even after OCR" if settings.ocr_enabled else "(OCR is disabled)"
        raise IngestError(f"{row.filename} has no readable text {hint}")
    ocr_pages = sum(1 for p in loaded.pages if p.metadata.get("ocr"))
    if ocr_pages:
        log.info("Document %d: %d page(s) read with OCR", doc_id, ocr_pages)

    pieces = split(loaded.pages, settings)
    if not pieces:
        raise IngestError(f"{row.filename} produced no indexable text")

    vectors: list[list[float]] = []
    for batch in batched(pieces, settings.embed_batch):
        if progress is not None:
            progress(len(vectors), len(pieces))
        vectors.extend(embedder.embed([embedding_text(row.filename, p) for p in batch]))
    bad = {len(v) for v in vectors if len(v) != settings.embed_dim}
    if bad:
        raise IngestError(
            f"Embedding model {embedder.embed_model} returned dimension {sorted(bad)}, "
            f"but EMBED_DIM is {settings.embed_dim}"
        )

    with engine.begin() as conn:
        _lock_still_processing(conn, doc_id)
        conn.execute(delete(chunks).where(chunks.c.document_id == doc_id))
        conn.execute(
            insert(chunks),
            [
                {
                    "document_id": doc_id,
                    "page": piece.metadata.get("page"),
                    "section": piece.metadata.get("section"),
                    "ordinal": ordinal,
                    "text": piece.page_content,
                    "embedding": vector,
                    "embed_model": embedder.embed_model,
                }
                for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
            ],
        )
        _mark_ready(conn, doc_id, loaded.page_count)
    queued = _sync_attachments(engine, doc_id, loaded.email, settings) if loaded.email else ""
    log.info(
        "Indexed document %d: %d chunks in %.1f s%s",
        doc_id,
        len(pieces),
        time.monotonic() - started,
        queued,
    )
    return len(pieces)


def _sync_attachments(engine: Engine, doc_id: int, mail: Email, settings: Settings) -> str:
    """Create, update or remove the email's attachment documents. Returns a
    note for the log line ("; 2 attachments queued")."""
    planned = attachments.plan(doc_id, mail, settings)
    with engine.begin() as conn:
        status = conn.execute(
            select(documents.c.status).where(documents.c.id == doc_id).with_for_update()
        ).scalar_one_or_none()
        if status != "ready":  # excluded or deleted meanwhile: leave its children alone
            return ""
        queued, removed, released = attachments.sync(conn, doc_id, planned)
    attachments.discard_unreferenced(engine, released, settings.upload_dir)
    parts = [f"{queued} attachment(s) queued"] if queued else []
    if removed:
        parts.append(f"{removed} removed")
    return f"; {', '.join(parts)}" if parts else ""
