"""Indexing pauses while an answer is in progress (app/db/priority.py)."""

import threading
import time

import pytest
from sqlalchemy import Engine

from app.db import priority

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]


def test_answer_in_progress_only_while_answering(db: Engine) -> None:
    assert not priority.answer_in_progress(db)
    with priority.answering(db):
        assert priority.answer_in_progress(db)
        with priority.answering(db):  # concurrent answers don't block each other
            assert priority.answer_in_progress(db)
        assert priority.answer_in_progress(db)
    assert not priority.answer_in_progress(db)


def test_lock_released_when_answer_fails(db: Engine) -> None:
    try:
        with priority.answering(db):
            raise RuntimeError("model died")
    except RuntimeError:
        pass
    assert not priority.answer_in_progress(db)


def test_worker_waits_until_answer_finishes(db: Engine) -> None:
    def answer() -> None:
        with priority.answering(db):
            time.sleep(0.6)

    t = threading.Thread(target=answer)
    t.start()
    time.sleep(0.1)
    waited = priority.wait_while_answering(db, max_wait_s=10, poll_s=0.05)
    t.join()
    assert 0.3 < waited < 5


def test_worker_wait_is_capped(db: Engine) -> None:
    with priority.answering(db):
        waited = priority.wait_while_answering(db, max_wait_s=0.2, poll_s=0.05)
    assert 0.2 <= waited < 2
