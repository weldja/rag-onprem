"""Interfaces for model access. Implementations must be local (see CLAUDE.md)."""

from collections.abc import Iterator
from typing import Protocol, TypedDict


class ChatMessage(TypedDict):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMClient(Protocol):
    def chat(self, messages: list[ChatMessage]) -> str: ...

    def chat_stream(self, messages: list[ChatMessage]) -> Iterator[str]:
        """Yield the answer in pieces as the model produces them."""
        ...


class Embedder(Protocol):
    @property
    def embed_model(self) -> str:
        """Name stored with every vector, so vectors from different models never mix."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
