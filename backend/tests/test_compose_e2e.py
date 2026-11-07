"""End-to-end checks against the running stack. Run via scripts/e2e.sh, which
ingests samples/sample.pdf first and runs this inside the compose `edge` network."""

import io
import json
import os
import time
import uuid

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


def test_stream_ends_with_cited_done_event() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    with httpx.stream(
        "POST",
        f"{BASE}/api/ask/stream",
        json={"question": "What is the mileage rate for the first 10,000 miles?"},
        timeout=TIMEOUT,
    ) as resp:
        assert resp.status_code == 200
        name = ""
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[6:])))
    kinds = [k for k, _ in events]
    assert "token" in kinds
    assert kinds[-1] == "done", events[-1]
    done = events[-1][1]
    assert done["found"] is True
    assert any(s["page"] == 3 for s in done["sources"])  # type: ignore[union-attr]


def test_upload_index_ask_delete_docx() -> None:
    """Full lifecycle through the API and the worker, with a DOCX."""
    import docx

    marker = uuid.uuid4().hex[:8]  # unique content, so it is never a duplicate
    d = docx.Document()
    d.add_heading("Parking policy", level=1)
    d.add_paragraph(
        f"Staff parking permits (reference PK-{marker}) cost 12 pounds per month "
        "and are issued by reception."
    )
    buf = io.BytesIO()
    d.save(buf)
    name = f"parking-{marker}.docx"

    resp = httpx.post(
        f"{BASE}/api/documents",
        files={"file": (name, buf.getvalue(), "application/octet-stream")},
        timeout=60,
    )
    assert resp.status_code == 202, resp.text
    doc_id = resp.json()["document"]["id"]

    def wait_until_done() -> dict[str, object]:
        deadline = time.monotonic() + 300
        while True:
            doc: dict[str, object] = httpx.get(f"{BASE}/api/documents/{doc_id}", timeout=10).json()
            if doc["status"] in ("ready", "failed") or time.monotonic() > deadline:
                return doc
            time.sleep(2)

    try:
        doc = wait_until_done()
        assert doc["status"] == "ready", doc
        assert doc["chunks"] >= 1 and doc["stale"] is False

        reindex = httpx.post(f"{BASE}/api/documents/{doc_id}/reindex", timeout=10)
        assert reindex.status_code == 202, reindex.text
        redone = wait_until_done()
        assert redone["status"] == "ready", redone
        assert redone["chunks"] == doc["chunks"]

        again = httpx.post(
            f"{BASE}/api/documents",
            files={"file": (name, buf.getvalue(), "application/octet-stream")},
            timeout=60,
        )
        assert again.status_code == 200 and again.json()["duplicate"] is True

        body = httpx.post(
            f"{BASE}/api/ask",
            json={"question": "How much does a staff parking permit cost per month?"},
            timeout=TIMEOUT,
        ).json()
        assert body["found"] is True, body
        assert any(
            s["document"] == name and s["section"] == "Parking policy" for s in body["sources"]
        ), body
    finally:
        assert httpx.delete(f"{BASE}/api/documents/{doc_id}", timeout=10).status_code == 204
    assert httpx.get(f"{BASE}/api/documents/{doc_id}", timeout=10).status_code == 404


def test_off_topic_question_is_not_found() -> None:
    resp = httpx.post(
        f"{BASE}/api/ask", json={"question": "What is the capital of Peru?"}, timeout=TIMEOUT
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["found"] is False, resp.json()
