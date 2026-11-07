"""Ingest PDFs from the command line.

    python -m app.ingestion.cli path/to/file.pdf [more.pdf ...]

Milestone 1 only; milestone 2 replaces this with an upload endpoint and a worker.
"""

import argparse
import logging
import sys
from pathlib import Path

from app.core.config import get_settings
from app.db.engine import get_engine
from app.ingestion.pipeline import IngestError, ingest_pdf
from app.llm.ollama import OllamaClient, OllamaError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest PDF files into the RAG index.")
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

    failures = 0
    for path in args.paths:
        if not path.is_file():
            print(f"ERROR {path}: no such file", file=sys.stderr)
            failures += 1
            continue
        try:
            result = ingest_pdf(path, engine=engine, embedder=client, settings=settings)
        except (IngestError, OllamaError) as exc:
            print(f"ERROR {path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if result.skipped:
            print(f"SKIPPED {result.filename}: already ingested (document {result.document_id})")
        else:
            print(f"OK {result.filename}: document {result.document_id}, {result.chunks} chunks")
    client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
