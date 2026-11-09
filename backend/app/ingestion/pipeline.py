"""Process one registered document: load -> split -> embed -> replace its chunks.

Used by the worker (uploads, re-index jobs) and the CLI. All of a document's
chunks are replaced in a single transaction, so a re-index never leaves a
half-written document visible to search.
"""

import logging
from itertools import batched
from pathlib import Path

from sqlalchemy import Engine, delete, func, insert, select, text, update

from app.core.config import Settings
from app.db.tables import chunks, documents
from app.ingestion.chunking import Document, RecursiveTextSplitter
from app.ingestion.loaders import LoadError, is_toc_chunk, load_file
from app.ingestion.ocr import OcrOptions
from app.llm.base import Embedder

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """Permanent failure for this document (bad file, no text, wrong dimension)."""


def split(pieces: list[Document], settings: Settings) -> list[Document]:
    splitter = RecursiveTextSplitter(settings.chunk_size, settings.chunk_overlap)
    out: list[Document] = []
    for piece in pieces:
        if piece.metadata.get("pre_chunked"):
            out.append(piece)
        else:
            out.extend(splitter.split_documents([piece]))
    return [c for c in out if not is_toc_chunk(c.page_content, c.metadata.get("page"))]


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
    with engine.begin() as conn:
        conn.execute(
            update(documents)
            .where(documents.c.id == doc_id)
            .values(status=status, error=error, updated_at=func.now())
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
        conn.execute(
            update(documents)
            .where(documents.c.id == doc_id)
            .values(status="ready", error=None, page_count=donor.page_count, updated_at=func.now())
        )
    return int(copied)


def process_document(engine: Engine, doc_id: int, *, embedder: Embedder, settings: Settings) -> int:
    """Index (or re-index) a document. Returns the number of chunks written.
    Raises IngestError for permanent problems; model errors propagate so the
    caller can retry."""
    with engine.connect() as conn:
        row = conn.execute(
            select(
                documents.c.filename,
                documents.c.storage_path,
                documents.c.content_type,
                documents.c.sha256,
                documents.c.source_id,
            ).where(documents.c.id == doc_id)
        ).one_or_none()
    if row is None:
        raise IngestError(f"Document {doc_id} no longer exists")
    if not row.storage_path or not Path(row.storage_path).is_file():
        if row.source_id is not None:
            raise IngestError(f"{row.filename} is no longer on the share (or can't be read)")
        raise IngestError(f"Original file for {row.filename} is missing; upload it again")

    copied = copy_from_duplicate(engine, doc_id, row.sha256, embedder.embed_model)
    if copied is not None:
        log.info("Document %d: copied %d chunks from an identical document", doc_id, copied)
        return copied

    set_status(engine, doc_id, "processing")
    try:
        loaded = load_file(
            Path(row.storage_path),
            row.content_type or Path(row.filename).suffix,
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
        vectors.extend(embedder.embed([p.page_content for p in batch]))
    bad = {len(v) for v in vectors if len(v) != settings.embed_dim}
    if bad:
        raise IngestError(
            f"Embedding model {embedder.embed_model} returned dimension {sorted(bad)}, "
            f"but EMBED_DIM is {settings.embed_dim}"
        )

    with engine.begin() as conn:
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
        conn.execute(
            update(documents)
            .where(documents.c.id == doc_id)
            .values(status="ready", error=None, page_count=loaded.page_count, updated_at=func.now())
        )
    log.info("Indexed document %d: %d chunks", doc_id, len(pieces))
    return len(pieces)
