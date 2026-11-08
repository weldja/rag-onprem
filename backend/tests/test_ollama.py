import json

import httpx
import pytest

from app.llm.base import ChatMessage
from app.llm.ollama import OllamaClient, OllamaError, model_matches


def make_client(handler: httpx.MockTransport) -> OllamaClient:
    return OllamaClient(
        "http://ollama:11434", llm_model="qwen:7b", embed_model="bge-m3", transport=handler
    )


def test_chat_posts_native_api_and_returns_content() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "hi [1]"}})

    out = make_client(httpx.MockTransport(handler)).chat([{"role": "user", "content": "q"}])
    assert out == "hi [1]"
    assert seen["path"] == "/api/chat"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen:7b" and body["stream"] is False


def test_answer_length_limit_is_sent_only_when_set() -> None:
    bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        if bodies[-1]["stream"]:
            return httpx.Response(200, content=_ndjson({"message": {"content": "x"}, "done": True}))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "x"}})

    msgs: list[ChatMessage] = [{"role": "user", "content": "q"}]
    limited = OllamaClient(
        "http://o:11434",
        llm_model="m",
        embed_model="e",
        max_tokens=768,
        num_ctx=4096,
        transport=httpx.MockTransport(handler),
    )
    limited.chat(msgs)
    list(limited.chat_stream(msgs))
    make_client(httpx.MockTransport(handler)).chat(msgs)
    assert [b["options"] for b in bodies[:2]] == [
        {"temperature": 0.1, "num_ctx": 4096, "num_predict": 768}
    ] * 2
    assert "num_predict" not in bodies[2]["options"]  # type: ignore[operator]


def test_embed_can_be_pinned_to_cpu() -> None:
    bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    cpu = OllamaClient(
        "http://o:11434",
        llm_model="m",
        embed_model="e",
        embed_num_gpu=0,
        transport=httpx.MockTransport(handler),
    )
    cpu.embed(["q"])
    make_client(httpx.MockTransport(handler)).embed(["q"])
    assert bodies[0]["options"] == {"num_gpu": 0}
    assert "options" not in bodies[1]  # default: Ollama decides


def test_embed_num_gpu_setting() -> None:
    from app.core.config import Settings

    assert Settings().embed_num_gpu is None
    assert Settings(embed_num_gpu=0).embed_num_gpu == 0


def _ndjson(*objs: dict[str, object]) -> bytes:
    return b"".join(json.dumps(o).encode() + b"\n" for o in objs)


def test_chat_stream_yields_pieces_until_done() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content)["stream"] is True
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"content": "Five "}, "done": False},
                {"message": {"content": "days [1]."}, "done": False},
                {"message": {"content": ""}, "done": True, "done_reason": "stop"},
            ),
        )

    pieces = list(make_client(httpx.MockTransport(handler)).chat_stream([]))
    assert pieces == ["Five ", "days [1]."]


def test_chat_stream_error_line_raises() -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=_ndjson({"error": "model crashed"}))
    )
    with pytest.raises(OllamaError, match="model crashed"):
        list(make_client(transport).chat_stream([]))


def test_chat_stream_truncated_raises() -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=_ndjson({"message": {"content": "Fi"}}))
    )
    with pytest.raises(OllamaError, match="without completing"):
        list(make_client(transport).chat_stream([]))


def test_embed_returns_vectors_in_order() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/embed"
        inputs = json.loads(req.content)["input"]
        return httpx.Response(200, json={"embeddings": [[float(len(t))] for t in inputs]})

    assert make_client(httpx.MockTransport(handler)).embed(["a", "bbb"]) == [[1.0], [3.0]]


def test_embed_count_mismatch_raises() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"embeddings": [[1.0]]}))
    with pytest.raises(OllamaError):
        make_client(transport).embed(["a", "b"])


def test_http_error_becomes_ollama_error() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(404, text="model not found"))
    with pytest.raises(OllamaError, match="404"):
        make_client(transport).chat([{"role": "user", "content": "q"}])


def test_connection_failure_becomes_ollama_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(OllamaError, match="Cannot reach"):
        make_client(httpx.MockTransport(handler)).list_models()


def test_missing_models() -> None:
    tags = {"models": [{"name": "bge-m3:latest"}, {"name": "llama3:8b"}]}
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=tags))
    assert make_client(transport).missing_models() == ["qwen:7b"]


def test_model_matches() -> None:
    assert model_matches("bge-m3", "bge-m3:latest")
    assert model_matches("qwen:7b", "qwen:7b")
    assert not model_matches("qwen:7b", "qwen:14b")
    assert not model_matches("bge", "bge-m3:latest")


def test_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:3128")
    client = OllamaClient("http://ollama:11434", llm_model="m", embed_model="e")
    assert client._http._mounts == {}  # no proxy transports were configured
