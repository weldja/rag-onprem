"""Questions go before indexing.

Ollama has one queue per model, and indexing sends it a steady stream of
embedding requests. On a server that embeds on the CPU (small GPUs, CPU-only)
each excerpt takes seconds, so a question's own embedding used to wait behind
whole batches (measured: 70-200 s on a 4 GB laptop GPU while a share was being
indexed), and indexing also slowed the answer itself by taking the CPU.

So while an answer is in progress the api holds a shared Postgres advisory
lock, and the worker checks for it before embedding each excerpt and waits
until no answer is running. Postgres is already shared by both, so this needs
no new service. Transaction-level locks are released when the connection goes
back to the pool, even if the request is cancelled half way through.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import Engine, text

# "RAGA" (answering); shares.py uses "RAGS" for the scan lock.
ANSWERING_LOCK = 0x52414741

Answering = Callable[[], AbstractContextManager[None]]


@contextmanager
def answering(engine: Engine) -> Iterator[None]:
    """Hold for the whole answer, including streaming. Never blocks: the worker
    only ever takes the lock for an instant."""
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": ANSWERING_LOCK})
        yield


def answer_in_progress(engine: Engine) -> bool:
    with engine.connect() as conn:
        free = conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ANSWERING_LOCK}
        ).scalar_one()
    return not free


def wait_while_answering(engine: Engine, max_wait_s: float, poll_s: float = 0.25) -> float:
    """Wait until no answer is in progress, at most max_wait_s (so a constant
    stream of questions can delay indexing but never stop it). Returns the
    seconds waited."""
    start = time.monotonic()
    while answer_in_progress(engine):
        waited = time.monotonic() - start
        if waited >= max_wait_s:
            return waited
        time.sleep(poll_s)
    return time.monotonic() - start
