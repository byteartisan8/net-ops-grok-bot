"""The xAI Responses API client: request shape, parsing, retries and error kinds (no network)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from netsim_bot.grok import GrokClient, GrokError, GrokResponse, Usage, function_tool, tool_output

RAW = {
    "id": "resp_123",
    "model": "grok-4.7",
    "status": "completed",
    "output": [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "check drift first"}],
            "encrypted_content": "x",
        },
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking drift."}]},
        {"type": "function_call", "call_id": "call_1", "name": "get_drift", "arguments": "{}"},
        {"type": "function_call", "call_id": "call_2", "name": "get_impact", "arguments": '{"host": "hub-b"}'},
    ],
    "usage": {
        "input_tokens": 1500,
        "output_tokens": 300,
        "input_tokens_details": {"cached_tokens": 1000},
        "output_tokens_details": {"reasoning_tokens": 200},
    },
}


def client(handler, **kw) -> GrokClient:
    return GrokClient("xai-test", base_url="https://api.x.test/v1", transport=httpx.MockTransport(handler), **kw)


def test_parses_text_reasoning_calls_and_usage() -> None:
    r = GrokResponse.from_api(RAW)
    assert r.id == "resp_123" and r.text == "Checking drift." and r.reasoning == "check drift first"
    assert [c.name for c in r.calls] == ["get_drift", "get_impact"]
    assert r.calls[1].args() == {"host": "hub-b"} and r.calls[1].call_id == "call_2"
    assert r.usage == Usage(input_tokens=1500, cached_tokens=1000, output_tokens=300, reasoning_tokens=200, calls=1)


def test_bad_arguments_do_not_raise() -> None:
    r = GrokResponse.from_api(
        {"output": [{"type": "function_call", "call_id": "c", "name": "x", "arguments": "{oops"}]}
    )
    assert r.calls[0].args() == {"_unparsed": "{oops"}


def test_request_shape_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=RAW)

    tool = function_tool("get_drift", "drift", {"type": "object", "properties": {}})

    async def run() -> GrokResponse:
        c = client(handler, model="grok-4.7", reasoning_effort="high")
        try:
            return await c.respond(
                input=[tool_output("call_0", {"ok": True})], tools=[tool], previous_response_id="resp_0"
            )
        finally:
            await c.aclose()

    r = asyncio.run(run())
    assert r.calls
    req = seen[0]
    assert req.url.path == "/v1/responses"
    assert req.headers["authorization"] == "Bearer xai-test"
    body = json.loads(req.content)
    assert body["model"] == "grok-4.7"
    assert body["previous_response_id"] == "resp_0"
    assert body["tools"][0] == {
        "type": "function",
        "name": "get_drift",
        "description": "drift",
        "parameters": tool["parameters"],
    }
    assert body["tool_choice"] == "auto" and body["parallel_tool_calls"] is True
    assert body["reasoning"] == {"effort": "high"}
    assert body["input"][0] == {"type": "function_call_output", "call_id": "call_0", "output": '{"ok":true}'}


def test_retries_rate_limit_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("netsim_bot.grok.asyncio.sleep", no_sleep)

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "1"})
        return httpx.Response(200, json=RAW)

    async def run() -> GrokResponse:
        c = client(handler)
        try:
            return await c.respond(input="hi")
        finally:
            await c.aclose()

    assert asyncio.run(run()).id == "resp_123"
    assert calls["n"] == 3


def test_reasoning_rejected_is_retried_without_it() -> None:
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "reasoning" in body:
            return httpx.Response(400, json={"error": {"message": "Argument not supported: reasoning"}})
        return httpx.Response(200, json=RAW)

    async def run() -> None:
        c = client(handler, reasoning_effort="low")
        try:
            await c.respond(input="hi")
            await c.respond(input="again")
        finally:
            await c.aclose()

    asyncio.run(run())
    assert ["reasoning" in b for b in bodies] == [True, False, False]


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (403, "billing"), (400, "bad-request")])
def test_error_kinds(status: int, kind: str) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    async def run() -> None:
        c = client(handler, max_retries=0)
        try:
            await c.respond(input="hi")
        finally:
            await c.aclose()

    with pytest.raises(GrokError) as err:
        asyncio.run(run())
    assert err.value.kind == kind and err.value.status == status and "nope" in str(err.value)


def test_empty_key_is_refused() -> None:
    with pytest.raises(GrokError):
        GrokClient("")
