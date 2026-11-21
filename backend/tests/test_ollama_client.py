import json

import httpx
import pytest

from app.ai.ollama_client import (
    OllamaClient,
    OllamaModelMissing,
    OllamaServerError,
    OllamaTimeout,
    OllamaUnavailable,
)


def client_for(handler) -> OllamaClient:
    return OllamaClient("http://127.0.0.1:11434", transport=httpx.MockTransport(handler))


async def test_chat_sends_expected_payload_and_parses_result():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={
            "message": {"content": "Hello chat!", "thinking": ""}, "eval_count": 40,
            "eval_duration": 1_000_000_000, "prompt_eval_count": 100, "load_duration": 5, "total_duration": 9,
            "done_reason": "stop",
        })

    res = await client_for(handler).chat(
        "m", [{"role": "user", "content": "hi"}], options={"num_ctx": 4096, "num_gpu": 99}, keep_alive="30m", timeout=5
    )
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["body"]["think"] is False and seen["body"]["stream"] is False
    assert seen["body"]["options"] == {"num_ctx": 4096, "num_gpu": 99} and seen["body"]["keep_alive"] == "30m"
    assert res.content == "Hello chat!" and res.tokens_per_second == pytest.approx(40.0)


@pytest.mark.parametrize("status,exc", [(404, OllamaModelMissing), (500, OllamaServerError), (400, OllamaServerError)])
async def test_http_errors_map_to_typed_exceptions(status, exc):
    client = client_for(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(exc):
        await client.chat("m", [], options={}, keep_alive="1m", timeout=5)


async def test_connection_refused_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaUnavailable):
        await client_for(handler).version()


async def test_read_timeout_is_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(OllamaTimeout):
        await client_for(handler).chat("m", [], options={}, keep_alive="1m", timeout=5)


async def test_non_json_body_is_a_server_error():
    with pytest.raises(OllamaServerError):
        await client_for(lambda r: httpx.Response(200, text="<html>")).version()


async def test_list_and_running_models():
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "a"}, {"name": "b"}]})
        return httpx.Response(200, json={"models": [{"name": "a", "size": 10, "size_vram": 10}]})

    c = client_for(handler)
    assert await c.list_models() == ["a", "b"]
    assert (await c.running_models())[0]["size_vram"] == 10
