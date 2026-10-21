"""Shared fakes for the bot's unit tests: recorded lab-api responses, a scriptable lab-api and a scripted Grok.

``bot/tests/data/<state>.json`` hold real lab-api responses recorded from the running lab on 2026-10-20 (healthy,
fake-redundancy with its three apply-intent dry runs, hub-down), keyed ``"GET /path"`` / ``"POST /path"``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from netsim_bot.grok import FunctionCall, GrokResponse, Usage
from netsim_bot.labapi import LabApi
from netsim_bot.settings import BotSettings, load_settings

DATA = Path(__file__).parent / "data"


def recorded(state: str) -> dict[str, Any]:
    return json.loads((DATA / f"{state}.json").read_text(encoding="utf-8"))


class FakeLab:
    """lab-api as a dict of responses (``"GET /drift"`` -> body); every request is recorded.

    A value can be a callable ``(request) -> (status, body)`` for dynamic answers.
    """

    def __init__(self, *states: str) -> None:
        self.routes: dict[str, Any] = {}
        for s in states:
            self.load(s)
        self.requests: list[httpx.Request] = []

    def load(self, state: str) -> None:
        for key, value in recorded(state).items():
            if key.startswith("_"):
                continue
            self.routes[key.split("?")[0]] = value

    def set(self, key: str, value: Any) -> None:
        self.routes[key] = value

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        value = self.routes.get(key)
        if value is None:
            if key == "GET /health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(404, json={"detail": f"fake lab-api has no {key}"})
        if callable(value):
            status, body = value(request)
            return httpx.Response(status, json=body)
        return httpx.Response(200, json=value)

    def client(self, token: str | None = "lab-token") -> LabApi:
        return LabApi("http://lab-api.test", token=token, transport=httpx.MockTransport(self.handler))

    def posts(self, path_prefix: str = "") -> list[tuple[str, dict[str, Any], httpx.Headers]]:
        return [
            (r.url.path, json.loads(r.content or b"{}"), r.headers)
            for r in self.requests
            if r.method == "POST" and r.url.path.startswith(path_prefix)
        ]


@pytest.fixture
def fake_lab() -> Callable[..., FakeLab]:
    return FakeLab


def make_settings(tmp_path: Path, **env: str) -> BotSettings:
    base = {
        "BOT_DATA_DIR": str(tmp_path / "data"),
        "BOT_ALLOWED_HOSTS": "testserver,127.0.0.1",
        "BOT_OPERATOR_TOKEN": "op-token",
        "LAB_API_TOKEN": "lab-token",
        "BOT_SETTLE_S": "0",
        "BOT_MAX_SETTLE_S": "0",
        "BOT_RESOLVE_HOLD_S": "0",
        "BOT_FIELD_DELAY_S": "0",
    }
    base.update(env)
    return load_settings(environ=base, env_file=tmp_path / "no.env")


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., BotSettings]:
    return lambda **env: make_settings(tmp_path, **env)


def call(name: str, args: dict[str, Any], call_id: str | None = None) -> FunctionCall:
    return FunctionCall(call_id=call_id or f"call-{name}", name=name, arguments=json.dumps(args))


def response(
    rid: str, calls: list[FunctionCall] | None = None, text: str = "", usage: Usage | None = None
) -> GrokResponse:
    return GrokResponse(
        id=rid,
        model="grok-4.7",
        status="completed",
        text=text,
        reasoning="",
        calls=calls or [],
        usage=usage or Usage(input_tokens=1000, cached_tokens=200, output_tokens=100, reasoning_tokens=40, calls=1),
    )


class FakeGrok:
    """Stands in for GrokClient: returns scripted responses and records every request."""

    model = "grok-4.7"

    def __init__(self, script: list[GrokResponse]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def respond(self, **kw: Any) -> GrokResponse:
        self.requests.append(kw)
        if not self.script:
            return response("resp-end", text="done")
        return self.script.pop(0)

    async def aclose(self) -> None:
        pass


@pytest.fixture(name="recorded")
def recorded_fixture() -> Callable[[str], dict[str, Any]]:
    return recorded


@pytest.fixture
def grok_kit() -> SimpleNamespace:
    """``call(name, args)``, ``response(id, calls, text)`` and ``FakeGrok(script)``."""
    return SimpleNamespace(call=call, response=response, FakeGrok=FakeGrok)
