"""Client for a local Ollama server, using its native HTTP API.

Never calls /api/pull: models are baked into the ollama image at build time,
and a missing model is a deployment error, not something to fix at runtime.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx

from app.llm.base import ChatMessage

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Ollama unreachable, timed out, or returned something unusable."""


def model_matches(wanted: str, available: str) -> bool:
    """Ollama lists untagged models as 'name:latest'."""
    if wanted == available:
        return True
    if ":" not in wanted:
        return available == f"{wanted}:latest"
    return False


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        llm_model: str,
        embed_model: str,
        timeout_s: float = 600.0,
        num_ctx: int = 8192,
        max_tokens: int = 0,
        temperature: float = 0.1,
        embed_num_gpu: int | None = None,
        think: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.llm_model = llm_model
        # Hybrid reasoning models (e.g. plain qwen3 tags) write a long hidden
        # "thinking" section first unless told not to; None sends nothing.
        self._think: dict[str, Any] = {} if think is None else {"think": think}
        self._embed_model = embed_model
        self._embed_options: dict[str, Any] = (
            {} if embed_num_gpu is None else {"num_gpu": embed_num_gpu}
        )
        self._options: dict[str, Any] = {"temperature": temperature, "num_ctx": num_ctx}
        if max_tokens > 0:
            self._options["num_predict"] = max_tokens
        # trust_env=False: ignore HTTP(S)_PROXY and friends, so a proxy setting
        # on the host can never route model traffic off the server.
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            trust_env=False,
            transport=transport,
        )

    @property
    def embed_model(self) -> str:
        return self._embed_model

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._http.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Ollama timed out on {path}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise OllamaError(f"Ollama {path} returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise OllamaError(f"Ollama {path} returned invalid JSON") from exc

    def chat(self, messages: list[ChatMessage]) -> str:
        data = self._request(
            "POST",
            "/api/chat",
            {
                "model": self.llm_model,
                "messages": messages,
                "stream": False,
                "options": self._options,
                **self._think,
            },
        )
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise OllamaError("Ollama /api/chat response has no message content")
        if data.get("done_reason") == "length":
            log.warning("LLM answer was cut off at the token limit")
        return content

    def chat_stream(self, messages: list[ChatMessage]) -> Iterator[str]:
        payload = {
            "model": self.llm_model,
            "messages": messages,
            "stream": True,
            "options": self._options,
            **self._think,
        }
        try:
            with self._http.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode(errors="replace")[:300]
                    raise OllamaError(f"Ollama /api/chat returned HTTP {resp.status_code}: {body}")
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError as exc:
                        raise OllamaError("Ollama /api/chat streamed invalid JSON") from exc
                    if "error" in data:
                        raise OllamaError(f"Ollama error: {data['error']}")
                    piece = (data.get("message") or {}).get("content") or ""
                    if piece:
                        yield piece
                    if data.get("done"):
                        if data.get("done_reason") == "length":
                            log.warning("LLM answer was cut off at the token limit")
                        return
        except httpx.TimeoutException as exc:
            raise OllamaError("Ollama timed out on /api/chat") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc
        raise OllamaError("Ollama /api/chat stream ended without completing")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self._embed_model, "input": texts, "truncate": True}
        if self._embed_options:
            payload["options"] = self._embed_options
        data = self._request("POST", "/api/embed", payload)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError(
                f"Ollama /api/embed returned {len(vectors) if isinstance(vectors, list) else 'no'}"
                f" embeddings for {len(texts)} inputs"
            )
        return vectors

    def load_models(self) -> None:
        """Load both models into memory now, so the first question doesn't wait
        for them (11 s measured for bge-m3 on a laptop). An empty chat loads the
        LLM without generating; the options match real requests, or Ollama
        would load it again for the first question. LLM first: loading it
        after the embedding model made Ollama unload the embedding model."""
        self._request(
            "POST",
            "/api/chat",
            {"model": self.llm_model, "messages": [], "stream": False, "options": self._options},
        )
        self.embed(["warm-up"])

    def list_models(self) -> list[str]:
        data = self._request("GET", "/api/tags")
        return [m["name"] for m in data.get("models", []) if isinstance(m, dict) and "name" in m]

    def missing_models(self) -> list[str]:
        """Configured models that are not present in Ollama. Raises OllamaError if unreachable."""
        available = self.list_models()
        return [
            wanted
            for wanted in (self.llm_model, self._embed_model)
            if not any(model_matches(wanted, a) for a in available)
        ]
