from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.auth.acl import Viewer
from app.main import CSRF_HEADER, CSRF_VALUE, app
from app.retrieval.search import RetrievedChunk
from tests.fakes import FakeEmbedder, FakeLLM, chunk, fixed_retriever

ALICE = Viewer(id=7, username="alice", is_admin=False)


@pytest.fixture
def anon() -> Iterator[TestClient]:
    # No `with TestClient(...)`: that would run the startup model check.
    yield TestClient(app, headers={CSRF_HEADER: CSRF_VALUE})
    app.dependency_overrides.clear()


@pytest.fixture
def client(anon: TestClient) -> TestClient:
    app.dependency_overrides[deps.get_current_user] = lambda: ALICE
    return anon


def override(reply: str, hits: list[RetrievedChunk], *, fail: bool = False) -> FakeLLM:
    llm = FakeLLM(reply, fail=fail)
    app.dependency_overrides[deps.get_llm] = lambda: llm
    app.dependency_overrides[deps.get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[deps.get_retriever] = lambda: fixed_retriever(hits)
    return llm


def test_ask_returns_answer_with_cited_sources(client: TestClient) -> None:
    override("Carry over up to 5 days [1].", [chunk(1, page=2, text="Up to 5 unused days")])
    resp = client.post("/api/ask", json={"question": "How many days carry over?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["answer"].startswith("Carry over")
    assert body["sources"] == [
        {
            "id": 1,
            "document": "a.pdf",
            "document_id": 1,
            "page": 2,
            "section": None,
            "chunk_id": 101,
            "snippet": "Up to 5 unused days",
            "score": 0.8,
        }
    ]


def test_ask_not_found(client: TestClient) -> None:
    override("unused", [])
    body = client.post("/api/ask", json={"question": "Capital of Peru?"}).json()
    assert body["found"] is False
    assert body["sources"] == []


def test_ask_model_down_is_503(client: TestClient) -> None:
    override("x", [chunk(1)], fail=True)
    assert client.post("/api/ask", json={"question": "q"}).status_code == 503


def test_ask_rejects_empty_question(client: TestClient) -> None:
    override("unused", [])
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_ask_stream_is_server_sent_events(client: TestClient) -> None:
    override("Five days [1].", [chunk(1, page=2)])
    with client.stream("POST", "/api/ask/stream", json={"question": "q"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    assert "event: token\n" in body
    assert body.rstrip().splitlines()[-2] == "event: done"
    assert '"found": true' in body


def test_upload_rejects_unsupported_type(client: TestClient) -> None:
    resp = client.post(
        "/api/documents", files={"file": ("setup.exe", b"MZ...", "application/octet-stream")}
    )
    assert resp.status_code == 415
    assert ".pdf" in resp.json()["detail"]


def test_requests_without_a_session_are_rejected(anon: TestClient) -> None:
    # The real get_current_user runs and finds no cookie; no database needed.
    assert anon.post("/api/ask", json={"question": "q"}).status_code == 401
    assert anon.get("/api/documents").status_code == 401


def test_state_changes_need_the_csrf_header(client: TestClient) -> None:
    override("x", [chunk(1)])
    bare = TestClient(app)
    resp = bare.post("/api/ask", json={"question": "q"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Missing CSRF header"
    assert client.post("/api/ask", json={"question": "q"}).status_code == 200


def test_admin_endpoints_need_admin(client: TestClient) -> None:
    assert client.get("/api/admin/users").status_code == 403


def test_swagger_ui_disabled(anon: TestClient) -> None:
    # Swagger UI pulls assets from a CDN; it must not be served.
    assert anon.get("/docs").status_code == 404
