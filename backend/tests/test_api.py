from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import Settings, get_settings
from app.main import app
from app.retrieval.search import RetrievedChunk
from tests.fakes import FakeEmbedder, FakeLLM, chunk


@pytest.fixture
def client() -> Iterator[TestClient]:
    # No `with TestClient(...)`: that would run the startup model check.
    yield TestClient(app)
    app.dependency_overrides.clear()


def override(reply: str, hits: list[RetrievedChunk]) -> FakeLLM:
    llm = FakeLLM(reply)
    app.dependency_overrides[deps.get_llm] = lambda: llm
    app.dependency_overrides[deps.get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[deps.get_retriever] = lambda: lambda _v: hits
    app.dependency_overrides[get_settings] = lambda: Settings(min_similarity=0.5)
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


def test_ask_rejects_empty_question(client: TestClient) -> None:
    override("unused", [])
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_swagger_ui_disabled(client: TestClient) -> None:
    # Swagger UI pulls assets from a CDN; it must not be served.
    assert client.get("/docs").status_code == 404
