from collections.abc import Iterator

from app.llm.base import ChatMessage
from app.llm.ollama import OllamaError
from app.retrieval.search import RetrievedChunk


class FakeEmbedder:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    @property
    def embed_model(self) -> str:
        return "fake-embed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(t) % 7), 1.0, 0.0, 0.5][: self.dim] for t in texts]


class FakeLLM:
    def __init__(self, reply: str, *, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.messages: list[ChatMessage] = []

    def chat(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        if self.fail:
            raise OllamaError("down")
        return self.reply

    def chat_stream(self, messages: list[ChatMessage]) -> Iterator[str]:
        self.messages = messages
        if self.fail:
            raise OllamaError("down")
        for word in self.reply.split(" "):
            yield word + " "


def chunk(
    n: int,
    *,
    page: int | None = 1,
    score: float = 0.8,
    text: str = "text",
    filename: str = "a.pdf",
    section: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=100 + n,
        document_id=1,
        filename=filename,
        page=page,
        text=text,
        score=score,
        section=section,
    )


def fixed_retriever(hits: list[RetrievedChunk]):  # type: ignore[no-untyped-def]
    return lambda _question, _vector: hits


class FakeSearch:
    """Stands in for DbSearch; records the scope it was asked for."""

    def __init__(self, hits: list[RetrievedChunk], spread: list[RetrievedChunk] | None = None):
        self.hits = hits
        self.spread_hits = spread if spread is not None else hits
        self.scopes: list[object] = []
        self.queries: list[str] = []

    def retriever(self, document_ids=None):  # type: ignore[no-untyped-def]
        self.scopes.append(document_ids)

        def retrieve(question: str, _vector: list[float]) -> list[RetrievedChunk]:
            self.queries.append(question)
            return self.hits

        return retrieve

    def spread(self, document_ids):  # type: ignore[no-untyped-def]
        self.scopes.append(("spread", list(document_ids)))
        return self.spread_hits


class FakeReranker:
    """Scores by how many times the query's first word appears in the text."""

    def score(self, query: str, texts: list[str]) -> list[float]:
        word = query.split()[0].lower()
        return [float(t.lower().count(word)) for t in texts]
