from app.llm.base import ChatMessage
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
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[ChatMessage] = []

    def chat(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        return self.reply


def chunk(
    n: int, *, page: int | None = 1, score: float = 0.8, text: str = "text", filename: str = "a.pdf"
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=100 + n, document_id=1, filename=filename, page=page, text=text, score=score
    )
