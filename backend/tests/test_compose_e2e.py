"""End-to-end checks against the running stack, over HTTPS through nginx.
Run via scripts/e2e.sh, which ingests samples/sample.pdf (shared with
"All staff"), resets the e2e-admin password, and runs this inside the compose
`edge` network."""

import io
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.enable_socket]

BASE = os.environ.get("RAG_BASE_URL", "https://127.0.0.1:8443")
TIMEOUT = 900.0  # CPU-only answers can be slow
CSRF = {"X-Requested-With": "rag-onprem"}
USER_PASSWORD = "e2e-user-password-123"


def _client() -> httpx.Client:
    # Dev certificate is self-signed.
    return httpx.Client(base_url=BASE, headers=CSRF, verify=False, timeout=TIMEOUT)


def _login(username: str, password: str) -> httpx.Client:
    client = _client()
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return client


@pytest.fixture(scope="module")
def admin() -> Iterator[httpx.Client]:
    client = _login(os.environ["RAG_ADMIN_USER"], os.environ["RAG_ADMIN_PASSWORD"])
    yield client
    client.close()


def _wait_until_done(client: httpx.Client, doc_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    while True:
        doc: dict[str, Any] = client.get(f"/api/documents/{doc_id}").json()
        if doc["status"] in ("ready", "failed") or time.monotonic() > deadline:
            return doc
        time.sleep(2)


def _docx(heading: str, text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_heading(heading, level=1)
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ basics


def test_health_and_frontend() -> None:
    with _client() as client:
        assert client.get("/api/health").json()["status"] == "ok"
        page = client.get("/")
        assert page.status_code == 200 and '<div id="root">' in page.text
        csp = page.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_http_redirects_to_https() -> None:
    resp = httpx.get(BASE.replace("https://", "http://") + "/", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].startswith("https://")


def test_api_requires_sign_in() -> None:
    with _client() as client:
        assert client.post("/api/ask", json={"question": "q"}).status_code == 401
        bad = client.post("/api/auth/login", json={"username": "nobody", "password": "nope"})
        assert bad.status_code == 401


# ------------------------------------------------------------------ answers


def _sample_id(client: httpx.Client) -> int:
    docs = client.get("/api/documents").json()
    return int(next(d["id"] for d in docs if d["filename"] == "sample.pdf"))


def test_answer_cites_the_right_page(admin: httpx.Client) -> None:
    # Scoped to sample.pdf: the admin also sees the eval corpus, whose handbook
    # has identical pages, and duplicate text is only retrieved once.
    body = admin.post(
        "/api/ask",
        json={
            "question": "How many unused days of annual leave can I carry over?",
            "document_ids": [_sample_id(admin)],
        },
    ).json()
    assert body["found"] is True, body
    assert "5" in body["answer"]
    assert any(s["document"] == "sample.pdf" and s["page"] == 2 for s in body["sources"]), body


def test_stream_ends_with_cited_done_event(admin: httpx.Client) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    with admin.stream(
        "POST",
        "/api/ask/stream",
        json={"question": "What is the mileage rate for the first 10,000 miles?"},
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
    assert any(s["page"] == 3 for s in done["sources"])


def test_summary_samples_across_the_document(admin: httpx.Client) -> None:
    body = admin.post(
        "/api/ask",
        json={
            "question": "Summarise sample.pdf",
            "mode": "summary",
            "document_ids": [_sample_id(admin)],
            "debug": True,
        },
    ).json()
    assert body["found"] is True, body
    # The spread covers the whole handbook, not just the closest match.
    assert {s["page"] for s in body["retrieved"]} == {1, 2, 3, 4}
    assert all(s["document"] == "sample.pdf" for s in body["sources"])


def test_follow_up_uses_conversation_history(admin: httpx.Client) -> None:
    body = admin.post(
        "/api/ask",
        json={
            "question": "And how many can be carried over?",
            "history": [
                {"role": "user", "content": "How many days of annual leave do I get?"},
                {"role": "assistant", "content": "Full-time staff get 27 days [1]."},
            ],
        },
    ).json()
    assert body["found"] is True, body
    assert "5" in body["answer"], body


def test_off_topic_question_is_not_found(admin: httpx.Client) -> None:
    body = admin.post("/api/ask", json={"question": "What is the capital of Peru?"}).json()
    assert body["found"] is False, body


# -------------------------------------------------------------- lifecycle


def test_upload_index_reindex_ask_download_delete(admin: httpx.Client) -> None:
    marker = uuid.uuid4().hex[:8]  # unique content, so it is never a duplicate
    name = f"parking-{marker}.docx"
    content = _docx(
        "Parking policy",
        f"Staff parking permits (reference PK-{marker}) cost 12 pounds per month "
        "and are issued by reception.",
    )
    resp = admin.post("/api/documents", files={"file": (name, content)})
    assert resp.status_code == 202, resp.text
    doc_id = resp.json()["document"]["id"]
    try:
        doc = _wait_until_done(admin, doc_id)
        assert doc["status"] == "ready", doc
        assert doc["chunks"] >= 1 and doc["stale"] is False and doc["group_ids"] == []

        assert admin.post(f"/api/documents/{doc_id}/reindex").status_code == 202
        redone = _wait_until_done(admin, doc_id)
        assert redone["status"] == "ready" and redone["chunks"] == doc["chunks"]

        again = admin.post("/api/documents", files={"file": (name, content)})
        assert again.status_code == 200 and again.json()["duplicate"] is True

        body = admin.post(
            "/api/ask", json={"question": "How much does a staff parking permit cost per month?"}
        ).json()
        assert body["found"] is True, body
        assert any(
            s["document"] == name and s["section"] == "Parking policy" for s in body["sources"]
        ), body

        download = admin.get(f"/api/documents/{doc_id}/file")
        assert download.status_code == 200 and download.content == content
        assert download.headers["content-disposition"].startswith("attachment")
    finally:
        assert admin.delete(f"/api/documents/{doc_id}").status_code == 204
    assert admin.get(f"/api/documents/{doc_id}").status_code == 404


# -------------------------------------------------------------------- ACLs


def test_groups_isolate_documents(admin: httpx.Client) -> None:
    """A document shared with one group is invisible, in answers and listings,
    to a user outside that group."""
    marker = uuid.uuid4().hex[:8]
    group = admin.post("/api/admin/groups", json={"name": f"Legal {marker}"}).json()
    insider = admin.post(
        "/api/admin/users",
        json={"username": f"in-{marker}", "password": USER_PASSWORD, "group_ids": [group["id"]]},
    ).json()
    outsider = admin.post(
        "/api/admin/users",
        json={"username": f"out-{marker}", "password": USER_PASSWORD, "group_ids": []},
    ).json()
    name = f"settlement-{marker}.docx"
    upload = admin.post(
        "/api/documents",
        files={
            "file": (
                name,
                _docx(
                    "Settlement terms",
                    f"The Orion settlement (case {marker}) pays 48,000 pounds "
                    "in three instalments.",
                ),
            )
        },
        data={"group_ids": [str(group["id"])]},
    )
    assert upload.status_code == 202, upload.text
    doc_id = upload.json()["document"]["id"]
    question = {"question": "How much does the Orion settlement pay?"}
    try:
        assert _wait_until_done(admin, doc_id)["status"] == "ready"

        with closing(_login(insider["username"], USER_PASSWORD)) as alice:
            body = alice.post("/api/ask", json=question).json()
            assert body["found"] is True, body
            assert any(s["document"] == name for s in body["sources"]), body
            assert alice.get(f"/api/documents/{doc_id}").status_code == 200

        with closing(_login(outsider["username"], USER_PASSWORD)) as bob:
            body = bob.post("/api/ask", json=question).json()
            assert all(s["document"] != name for s in body["sources"]), body
            assert "48,000" not in body["answer"], body
            assert doc_id not in [d["id"] for d in bob.get("/api/documents").json()]
            assert bob.get(f"/api/documents/{doc_id}").status_code == 404
            assert bob.get(f"/api/documents/{doc_id}/file").status_code == 404
            assert bob.delete(f"/api/documents/{doc_id}").status_code == 404
            assert bob.get("/api/admin/users").status_code == 403
    finally:
        admin.delete(f"/api/documents/{doc_id}")
        for user in (insider, outsider):
            admin.delete(f"/api/admin/users/{user['id']}")
        admin.delete(f"/api/admin/groups/{group['id']}")
