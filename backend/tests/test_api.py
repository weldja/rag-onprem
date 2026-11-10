from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.auth.acl import Viewer
from app.main import CSRF_HEADER, CSRF_VALUE, app
from app.retrieval.cache import AnswerCache
from app.retrieval.search import RetrievedChunk
from tests.fakes import FakeEmbedder, FakeLLM, FakeSearch, chunk

ALICE = Viewer(id=7, username="alice", is_admin=False)


@pytest.fixture
def anon() -> Iterator[TestClient]:
    # No `with TestClient(...)`: that would run the startup model check.
    yield TestClient(app, headers={CSRF_HEADER: CSRF_VALUE})
    app.dependency_overrides.clear()


@pytest.fixture
def client(anon: TestClient) -> TestClient:
    app.dependency_overrides[deps.get_current_user] = lambda: ALICE
    # A fresh cache per test, so one test's answer is never another's hit.
    cache = AnswerCache(100, 60)
    app.dependency_overrides[deps.get_answer_cache] = lambda: cache
    app.dependency_overrides[deps.get_answering] = lambda: answering
    return anon


answering_calls: list[str] = []


@contextmanager
def answering() -> Iterator[None]:
    answering_calls.append("start")
    yield
    answering_calls.append("end")


def test_answers_mark_themselves_in_progress(client: TestClient) -> None:
    override("Yes [1].", [chunk(1)])
    answering_calls.clear()
    client.post("/api/ask", json={"question": "q"})
    assert answering_calls == ["start", "end"]
    answering_calls.clear()
    client.post("/api/ask/stream", json={"question": "q2"})
    assert answering_calls == ["start", "end"]


def override(reply: str, hits: list[RetrievedChunk], *, fail: bool = False) -> FakeLLM:
    llm, _ = override_with_search(reply, FakeSearch(hits), fail=fail)
    return llm


def override_with_search(
    reply: str, search: FakeSearch, *, fail: bool = False
) -> tuple[FakeLLM, FakeSearch]:
    llm = FakeLLM(reply, fail=fail)
    app.dependency_overrides[deps.get_llm] = lambda: llm
    app.dependency_overrides[deps.get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[deps.get_search] = lambda: search
    return llm, search


def test_ask_scope_history_and_debug(client: TestClient) -> None:
    llm, search = override_with_search("Yes [1].", FakeSearch([chunk(1), chunk(2)]))
    body = client.post(
        "/api/ask",
        json={
            "question": "And part-timers?",
            "document_ids": [4, 9],
            "history": [
                {"role": "user", "content": "How much leave?"},
                {"role": "assistant", "content": "27 days [1]."},
            ],
            "debug": True,
        },
    ).json()
    assert search.scopes == [[4, 9]]
    assert search.queries == ["How much leave?\nAnd part-timers?"]
    assert [m["role"] for m in llm.messages] == ["system", "user", "assistant", "user"]
    assert [s["id"] for s in body["retrieved"]] == [1, 2]
    assert len(body["prompt_version"]) == 10


def test_ask_without_debug_omits_debug_fields(client: TestClient) -> None:
    override("Yes [1].", [chunk(1)])
    body = client.post("/api/ask", json={"question": "q"}).json()
    assert "retrieved" not in body and "prompt_version" not in body


def test_summary_mode(client: TestClient) -> None:
    _, search = override_with_search("Overview [1].", FakeSearch([], spread=[chunk(1, page=3)]))
    body = client.post(
        "/api/ask", json={"question": "Summarise", "mode": "summary", "document_ids": [5]}
    ).json()
    assert body["found"] is True and body["sources"][0]["page"] == 3
    assert ("spread", [5]) in search.scopes


def test_typed_summary_request_runs_as_summary(client: TestClient) -> None:
    spread = [chunk(1, page=3, filename="ENC_Administrator_Manual.pdf")]
    _, search = override_with_search("Overview [1].", FakeSearch([chunk(2)], spread=spread))
    for q in (
        "In ENC_Administrator_Manual.pdf\nsummarise this doc",
        "Give me an overview of the ENC administrator manual",
    ):
        body = client.post("/api/ask", json={"question": q, "document_ids": [1]}).json()
        assert body["sources"][0]["page"] == 3
    with client.stream(
        "POST", "/api/ask/stream", json={"question": "Summarise this", "document_ids": [1]}
    ) as resp:
        assert '"summary": true' in "".join(resp.iter_text())
    assert search.scopes.count(("spread", [1])) == 3
    # A topic stays a question, and without a scope there's nothing to summarise.
    client.post("/api/ask", json={"question": "Summarise the SSO section", "document_ids": [1]})
    client.post("/api/ask", json={"question": "summarise this doc"})
    assert search.scopes.count(("spread", [1])) == 3


def test_summary_mode_needs_documents(client: TestClient) -> None:
    override("x", [])
    resp = client.post("/api/ask", json={"question": "Summarise", "mode": "summary"})
    assert resp.status_code == 422


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
            "rerank_score": None,
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
