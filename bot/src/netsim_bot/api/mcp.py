"""``/mcp``: the NOC roles' tools as an MCP server (Streamable HTTP transport), for the Grok Bot app's routines.

Stateless and JSON only: every ``POST /mcp`` carries one JSON-RPC message (or a batch) and gets the answer as
``application/json``; there is no session and no server-sent stream (``GET`` answers 405, which the transport allows).
Supported methods: ``initialize``, ``ping``, ``tools/list``, ``tools/call`` and notifications (202, no body).

* Auth: ``Authorization: Bearer <token>``; the token decides the role (``BOT_NOCENG_TOKEN``, ``BOT_NETENG_TOKEN``,
  ``BOT_NOCMGR_TOKEN``). With no role token in ``.env`` the endpoint is off (404). A role token is not the operator
  token: it cannot approve, reject or change the bot.
* ``initialize`` returns instructions for that role; ``tools/list`` its tools (:mod:`netsim_bot.roles`).
* ``Origin``: a browser request from a page this server did not serve is refused (DNS rebinding); MCP clients send none.
"""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from netsim_bot import __version__
from netsim_bot.engine import Engine
from netsim_bot.roles import instructions
from netsim_bot.settings import ROLES

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

router = APIRouter()


def server_info(role: str) -> dict[str, str]:
    spec = ROLES[role]
    return {"name": f"netsim-noc-{spec.bot.lower()}", "title": f"NetSim NOC: {spec.title}", "version": __version__}


def _engine(request: Request) -> Engine | None:
    return getattr(request.app.state, "engine", None)


def _err(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _ok(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _origin_ok(request: Request, allowed_hosts: list[str]) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = (urlsplit(origin).hostname or "").lower()
    return host in {h.lower() for h in allowed_hosts}


def role_of(token: str, role_tokens: dict[str, str | None]) -> str | None:
    """The role whose token this is (constant-time compare against every role's token)."""
    found = None
    for role, expected in role_tokens.items():
        if expected and hmac.compare_digest(token.encode(), expected.encode()):
            found = role
    return found


async def handle(eng: Engine, role: str, msg: Any) -> dict[str, Any] | None:
    """One JSON-RPC message; ``None`` for notifications and responses (nothing to answer)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = msg.get("method")
    id_ = msg.get("id")
    if method is None:  # a response to something we never asked: ignore
        return None
    if "id" not in msg:  # notification (notifications/initialized, cancelled, ...)
        return None
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _err(id_, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _ok(
            id_,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": server_info(role),
                "instructions": instructions(role),
            },
        )
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": eng.gateway.tools(role)})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _err(id_, INVALID_PARAMS, "tools/call needs name (string) and arguments (object)")
        ok, result = await eng.gateway.call(role, name, args)
        text = json.dumps(result, default=str, separators=(",", ":"))
        out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": not ok}
        if isinstance(result, dict):
            out["structuredContent"] = json.loads(text)
        return _ok(id_, out)
    return _err(id_, METHOD_NOT_FOUND, f"method {method!r} is not supported")


@router.post("/mcp", include_in_schema=False)
async def mcp_post(request: Request) -> Response:
    eng = _engine(request)
    if eng is None:
        return JSONResponse({"error": "the engine is starting"}, status_code=503)
    s = eng.settings
    if not any(s.role_tokens.values()):
        return JSONResponse(
            {"error": "the MCP endpoint is off: set BOT_NOCENG_TOKEN, BOT_NETENG_TOKEN, BOT_NOCMGR_TOKEN in .env"},
            status_code=404,
        )
    if not _origin_ok(request, s.allowed_hosts):
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    role = role_of(given, s.role_tokens) if given else None
    if role is None:
        return JSONResponse(
            {"error": "missing or wrong Authorization: Bearer <a role token: BOT_NOCENG/NETENG/NOCMGR_TOKEN>"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="netsim-noc"'},
        )
    version = request.headers.get("mcp-protocol-version")
    if version and version not in PROTOCOL_VERSIONS:
        return JSONResponse(_err(None, INVALID_REQUEST, f"unsupported MCP-Protocol-Version {version}"), 400)
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_err(None, PARSE_ERROR, "body is not JSON"), status_code=400)
    if isinstance(body, list):
        if not body:
            return JSONResponse(_err(None, INVALID_REQUEST, "empty batch"), status_code=400)
        answers = [a for a in [await handle(eng, role, m) for m in body] if a is not None]
        return JSONResponse(answers) if answers else Response(status_code=202)
    answer = await handle(eng, role, body)
    return JSONResponse(answer) if answer is not None else Response(status_code=202)


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
async def mcp_other() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
