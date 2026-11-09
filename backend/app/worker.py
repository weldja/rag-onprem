"""Ingestion worker: claims jobs from the Postgres queue and indexes documents.

    python -m app.worker

Safe to run several copies: jobs are claimed with FOR UPDATE SKIP LOCKED.
"""

import logging
import signal
import time
from types import FrameType

from sqlalchemy import Engine

from app.core.config import Settings, get_settings
from app.db import priority
from app.db.engine import get_engine
from app.ingestion import registry, shares
from app.ingestion.pipeline import IngestError, process_document, set_status
from app.llm.base import Embedder
from app.llm.ollama import OllamaClient, OllamaError

log = logging.getLogger("app.worker")
_SHARE_CHECK_S = 5.0


def run_once(engine: Engine, embedder: Embedder, settings: Settings) -> bool:
    """Process one job if there is one. Returns True if a job was handled."""
    job = registry.claim_job(engine)
    if job is None:
        return False
    log.info("Job %d: %s document %d (attempt %d)", job.id, job.kind, job.document_id, job.attempts)
    try:
        process_document(
            engine,
            job.document_id,
            embedder=embedder,
            settings=settings,
            before_embed=lambda: priority.wait_while_answering(engine, settings.index_pause_max_s),
        )
    except IngestError as exc:
        log.warning("Job %d failed permanently: %s", job.id, exc)
        set_status(engine, job.document_id, "failed", str(exc))
        registry.finish_job(engine, job.id, error=str(exc))
    except OllamaError as exc:
        # Usually transient (model loading, restart); retry a few times.
        retry = job.attempts < settings.job_max_attempts
        log.warning("Job %d model error (retry=%s): %s", job.id, retry, exc)
        set_status(engine, job.document_id, "pending" if retry else "failed", str(exc))
        registry.finish_job(engine, job.id, error=str(exc), retry=retry)
        if retry:
            time.sleep(min(30.0, 5.0 * job.attempts))
    except Exception as exc:
        log.exception("Job %d crashed", job.id)
        set_status(engine, job.document_id, "failed", f"Unexpected error: {exc}")
        registry.finish_job(engine, job.id, error=f"Unexpected error: {exc}")
    else:
        registry.finish_job(engine, job.id)
    return True


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = get_engine()
    client = OllamaClient(
        settings.ollama_url,
        llm_model=settings.llm_model,
        embed_model=settings.embed_model,
        timeout_s=settings.llm_timeout_s,
        embed_num_gpu=settings.embed_num_gpu,
    )
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        log.info("Received signal %d, finishing current job then stopping", signum)
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    requeued = registry.requeue_stale_jobs(engine, settings.job_stale_after_s)
    if requeued:
        log.info("Re-queued %d stale job(s)", requeued)
    shares.sync_sources(engine, settings.shares())
    log.info(
        "Worker started (embed model %s, %d file share(s))",
        settings.embed_model,
        len(settings.shares()),
    )
    last_share_check = 0.0
    while not stopping:
        # Cheap check (one query) at most every few seconds, also between jobs,
        # so "Scan now" responds quickly even while a large share is indexing.
        if settings.shares() and time.monotonic() - last_share_check >= _SHARE_CHECK_S:
            last_share_check = time.monotonic()
            try:
                shares.scan_due(engine, settings)
            except Exception:
                log.exception("File share scan failed")
        try:
            busy = run_once(engine, client, settings)
        except Exception:
            log.exception("Worker loop error; backing off")
            busy = False
            time.sleep(5)
        if not busy:
            time.sleep(settings.worker_poll_s)
    client.close()


if __name__ == "__main__":
    main()
