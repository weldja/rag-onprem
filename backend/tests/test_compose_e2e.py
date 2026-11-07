"""End-to-end checks against the running stack. Run via scripts/e2e.sh, which
ingests samples/sample.pdf first and runs this inside the compose `edge` network."""

import os

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.enable_socket]

BASE = os.environ.get("RAG_BASE_URL", "http://127.0.0.1:8000")
TIMEOUT = 900.0  # CPU-only answers can be slow


def test_health() -> None:
    resp = httpx.get(f"{BASE}/api/health", timeout=10)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_answer_cites_the_right_page() -> None:
    resp = httpx.post(
        f"{BASE}/api/ask",
        json={"question": "How many unused days of annual leave can I carry over?"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True, body
    assert "5" in body["answer"]
    assert body["sources"], "answer has no citations"
    assert any(s["document"] == "sample.pdf" and s["page"] == 2 for s in body["sources"]), body


def test_off_topic_question_is_not_found() -> None:
    resp = httpx.post(
        f"{BASE}/api/ask", json={"question": "What is the capital of Peru?"}, timeout=TIMEOUT
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["found"] is False, resp.json()
