"""Minimal client for the xAI Responses API (``POST {XAI_BASE_URL}/responses``), the API xAI recommends for Grok.

Function calling as documented at docs.x.ai (checked 2026-10-20): tools are ``{"type": "function", "name",
"description", "parameters"}`` (JSON Schema object); the model's calls come back as output items of type
``function_call`` (``name``, ``arguments`` as a JSON string, ``call_id``); results go back as input items
``{"type": "function_call_output", "call_id", "output"}`` with ``previous_response_id`` set to the response they
answer (the conversation is stored server-side, ``store`` defaults to true). Several calls can come in one response
(parallel tool calls, on by default).

Plain httpx (no SDK), so tests replace the transport and the bot has no extra dependency. Retries 429/5xx and
network errors with backoff; a ``400`` that names the ``reasoning`` field is retried once without it (models that
do not take a reasoning effort).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class GrokError(RuntimeError):
    """``kind``: auth (bad key), billing (no credits / forbidden), rate, server, bad-request, network."""

    def __init__(self, message: str, kind: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.calls += other.calls

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "calls": self.calls,
        }

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}

        def num(*keys: str) -> int:
            cur: Any = raw
            for k in keys:
                if not isinstance(cur, dict):
                    return 0
                cur = cur.get(k)
            return int(cur) if isinstance(cur, int | float) else 0

        return cls(
            input_tokens=num("input_tokens") or num("prompt_tokens"),
            cached_tokens=num("input_tokens_details", "cached_tokens") or num("prompt_tokens_details", "cached_tokens"),
            output_tokens=num("output_tokens") or num("completion_tokens"),
            reasoning_tokens=num("output_tokens_details", "reasoning_tokens")
            or num("completion_tokens_details", "reasoning_tokens"),
            calls=1,
        )


@dataclass
class FunctionCall:
    call_id: str
    name: str
    arguments: str

    def args(self) -> dict[str, Any]:
        if not self.arguments:
            return {}
        try:
            value = json.loads(self.arguments)
        except ValueError:
            return {"_unparsed": self.arguments}
        return value if isinstance(value, dict) else {"_value": value}


@dataclass
class GrokResponse:
    id: str
    model: str
    status: str
    text: str
    reasoning: str
    calls: list[FunctionCall]
    usage: Usage
    output: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any], seconds: float = 0.0) -> GrokResponse:
        texts: list[str] = []
        reasoning: list[str] = []
        calls: list[FunctionCall] = []
        output = raw.get("output") or []
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                calls.append(
                    FunctionCall(
                        call_id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=item.get("arguments")
                        if isinstance(item.get("arguments"), str)
                        else json.dumps(item.get("arguments") or {}),
                    )
                )
            elif kind == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text") and part.get("text"):
                        texts.append(str(part["text"]))
            elif kind == "reasoning":
                for part in item.get("summary") or []:
                    if isinstance(part, dict) and part.get("text"):
                        reasoning.append(str(part["text"]))
        if not texts and isinstance(raw.get("output_text"), str):
            texts.append(raw["output_text"])
        return cls(
            id=str(raw.get("id") or ""),
            model=str(raw.get("model") or ""),
            status=str(raw.get("status") or ""),
            text="\n".join(t.strip() for t in texts if t.strip()),
            reasoning="\n".join(r.strip() for r in reasoning if r.strip()),
            calls=calls,
            usage=Usage.from_api(raw.get("usage")),
            output=[i for i in output if isinstance(i, dict)],
            seconds=seconds,
        )


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """A tool declaration in the Responses API format."""
    return {"type": "function", "name": name, "description": description, "parameters": parameters}


def tool_output(call_id: str, output: Any) -> dict[str, Any]:
    """The input item that answers a function call (``output`` is sent as a JSON string)."""
    text = output if isinstance(output, str) else json.dumps(output, default=str, separators=(",", ":"))
    return {"type": "function_call_output", "call_id": call_id, "output": text}


class GrokClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
        model: str = "grok-4.7",
        reasoning_effort: str | None = None,
        timeout: float = 300.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise GrokError("XAI_API_KEY is empty", "auth")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_retries = max_retries
        self._reasoning_ok = True
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _body(
        self,
        input: list[dict[str, Any]] | str,
        tools: list[dict[str, Any]] | None,
        previous_response_id: str | None,
        tool_choice: str | dict[str, Any] | None,
        max_output_tokens: int | None,
        parallel_tool_calls: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "input": input}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
            body["parallel_tool_calls"] = parallel_tool_calls
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        if max_output_tokens:
            body["max_output_tokens"] = max_output_tokens
        if self.reasoning_effort and self._reasoning_ok:
            body["reasoning"] = {"effort": self.reasoning_effort}
        return body

    async def respond(
        self,
        input: list[dict[str, Any]] | str,
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
        parallel_tool_calls: bool = True,
    ) -> GrokResponse:
        attempt = 0
        while True:
            body = self._body(input, tools, previous_response_id, tool_choice, max_output_tokens, parallel_tool_calls)
            started = time.monotonic()
            try:
                resp = await self._http.post("/responses", json=body)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(min(2**attempt, 20))
                    continue
                raise GrokError(f"xAI API unreachable: {type(exc).__name__}: {exc}", "network") from exc
            if resp.status_code < 400:
                try:
                    raw = resp.json()
                except ValueError as exc:
                    raise GrokError(
                        f"xAI API answered non-JSON: {resp.text[:200]}", "server", resp.status_code
                    ) from exc
                return GrokResponse.from_api(raw, seconds=round(time.monotonic() - started, 2))
            message = _error_message(resp)
            if resp.status_code == 400 and "reasoning" in message.lower() and "reasoning" in body:
                self._reasoning_ok = False  # this model takes no reasoning effort: retry once without it
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                attempt += 1
                retry_after = resp.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                await asyncio.sleep(min(delay, 30))
                continue
            kind = {401: "auth", 402: "billing", 403: "billing", 429: "rate"}.get(
                resp.status_code, "server" if resp.status_code >= 500 else "bad-request"
            )
            raise GrokError(f"xAI API HTTP {resp.status_code}: {message}", kind, resp.status_code)


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:400]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:400]
        if err:
            return str(err)[:400]
        if data.get("code") or data.get("message"):
            return f"{data.get('code', '')} {data.get('message', '')}".strip()[:400]
    return json.dumps(data)[:400]
