"""Ingest one PDF: load -> split -> embed -> store, all chunks in one transaction."""

import hashlib
import logging
from dataclasses import dataclass
from itertools import batched
from pathlib import Path

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.tables import chunks, documents
from app.ingestion.chunking import RecursiveTextSplitter
from app.ingestion.loaders import LoadError, load_pdf
from app.llm.base import Embedder

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


@dataclass
class IngestResult:
    document_id: int
    filename: str
    chunks: int
    skipped: bool  # True if identical content was already ingested


def ingest_pdf(
    path: Path, *, engine: Engine, embedder: Embedder, settings: Settings
) -> IngestResult:
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    with engine.connect() as conn:
        existing = conn.execute(
            select(documents.c.id).where(documents.c.sha256 == sha256)
        ).scalar_one_or_none()
    if existing is not None:
        return IngestResult(existing, path.name, 0, skipped=True)

    try:
        loaded = load_pdf(path)
    except LoadError as exc:
        raise IngestError(str(exc)) from exc
    if not loaded.pages:
        raise IngestError(
            f"{path.name} has no extractable text (scanned PDFs need OCR, planned for later)"
        )

    splitter = RecursiveTextSplitter(settings.chunk_size, settings.chunk_overlap)
    pieces = splitter.split_documents(loaded.pages)

    vectors: list[list[float]] = []
    for batch in batched(pieces, settings.embed_batch):
        vectors.extend(embedder.embed([p.page_content for p in batch]))
    bad = {len(v) for v in vectors if len(v) != settings.embed_dim}
    if bad:
        raise IngestError(
            f"Embedding model {embedder.embed_model} returned dimension {bad}, "
            f"but EMBED_DIM is {settings.embed_dim}"
        )

    try:
        with engine.begin() as conn:
            document_id = conn.execute(
                insert(documents)
                .values(filename=path.name, sha256=sha256, page_count=loaded.page_count)
                .returning(documents.c.id)
            ).scalar_one()
            conn.execute(
                insert(chunks),
                [
                    {
                        "document_id": document_id,
                        "page": piece.metadata.get("page"),
                        "ordinal": ordinal,
                        "text": piece.page_content,
                        "embedding": vector,
                        "embed_model": embedder.embed_model,
                    }
                    for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
                ],
            )
    except IntegrityError:
        # Same file ingested concurrently; the other run won.
        with engine.connect() as conn:
            existing = conn.execute(
                select(documents.c.id).where(documents.c.sha256 == sha256)
            ).scalar_one()
        return IngestResult(existing, path.name, 0, skipped=True)

    log.info("Ingested %s: %d pages, %d chunks", path.name, loaded.page_count, len(pieces))
    return IngestResult(document_id, path.name, len(pieces), skipped=False)
