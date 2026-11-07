"""Ingest files from the command line, synchronously (no worker needed).

    python -m app.ingestion.cli path/to/file.pdf [more files ...]

Files are stored in the uploads volume like API uploads, so they can be re-indexed.
"""

import argparse
import logging
import sys
from pathlib import Path

from app.core.config import get_settings
from app.db.engine import get_engine
from app.ingestion import registry, storage
from app.ingestion.loaders import SUPPORTED_EXTENSIONS
from app.ingestion.pipeline import IngestError, process_document, set_status
from app.llm.ollama import OllamaClient, OllamaError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest documents into the RAG index.")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    client = OllamaClient(
        settings.ollama_url,
        llm_model=settings.llm_model,
        embed_model=settings.embed_model,
        timeout_s=settings.llm_timeout_s,
    )
    engine = get_engine()
    max_bytes = settings.max_upload_mb * 1024 * 1024

    failures = 0
    for path in args.paths:
        ext = path.suffix.lower().lstrip(".")
        if not path.is_file():
            print(f"ERROR {path}: no such file", file=sys.stderr)
            failures += 1
            continue
        if ext not in SUPPORTED_EXTENSIONS:
            print(f"ERROR {path.name}: unsupported file type .{ext}", file=sys.stderr)
            failures += 1
            continue
        try:
            received = storage.receive_path(path, settings.upload_dir, max_bytes)
        except storage.TooLargeError as exc:
            print(f"ERROR {path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        existing = registry.find_by_sha(engine, received.sha256)
        if existing is not None:
            storage.discard(received.path)
            doc = registry.get_document(engine, existing, settings.embed_model)
            if doc.status == "ready":
                print(f"SKIPPED {path.name}: already ingested (document {existing})")
                continue
            doc_id = existing
        else:
            target = storage.finalise(received, settings.upload_dir, ext)
            doc_id, _ = registry.register(
                engine,
                filename=path.name,
                sha256=received.sha256,
                ext=ext,
                size=received.size,
                storage_path=str(target),
                enqueue=False,
            )
        try:
            n = process_document(engine, doc_id, embedder=client, settings=settings)
        except (IngestError, OllamaError) as exc:
            set_status(engine, doc_id, "failed", str(exc))
            print(f"ERROR {path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"OK {path.name}: document {doc_id}, {n} chunks")
    client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
