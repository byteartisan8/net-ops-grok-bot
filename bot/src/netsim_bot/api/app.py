"""grok-bot's web server: the NOC room (``GET /``), its REST API (``/api/...``) and the live event stream.

Reads are open (the port is published on 127.0.0.1 only). Every write (approve, reject, ask, control) needs
``X-Operator-Token`` (``BOT_OPERATOR_TOKEN`` in ``.env``; the NOC page served by this process carries it, ``botctl``
reads ``.env``), and ``X-Operator`` names who acted. Requests whose ``Host`` is not in ``BOT_ALLOWED_HOSTS`` are
refused, so a web page elsewhere cannot reach the bot through DNS rebinding.

Outside agents use ``/mcp`` (:mod:`netsim_bot.api.mcp`, ``BOT_AGENT_TOKEN``). A Host in ``BOT_MCP_HOSTS`` (a tunnel
that publishes the bot for a cloud agent) reaches ``/mcp`` and nothing else: not the page, not the open reads.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from netsim_bot import __version__
from netsim_bot.api.mcp import router as mcp_router
from netsim_bot.engine import Engine

WEB = Path(__file__).resolve().parent.parent / "web"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Decision(_In):
    comment: str | None = Field(default=None, max_length=500, description="Why (shown on the proposal)")


class Ask(_In):
    message: str = Field(min_length=1, max_length=4000, description="Question or request for the NOC bots")
    chat_id: str | None = Field(default=None, description="Continue this conversation (omit for a new one)")


class ControlIn(_In):
    paused: bool | None = Field(default=None, description="Maintenance mode: watch only, open no incidents")
    reason: str | None = Field(default=None, max_length=200)
    brain: str | None = Field(default=None, description="auto, grok, rules, or default (back to .env)")
    autonomy: str | None = Field(default=None, description="observe, propose, auto, or default (back to .env)")
    pager_muted: bool | None = Field(default=None, description="Mute the pager (no webhooks to the outside agent)")


def operator_token(engine: Engine) -> str:
    """``BOT_OPERATOR_TOKEN``, else a token generated once and kept in the data directory."""
    token = engine.settings.operator_token
    if token:
        return token
    path = engine.settings.data_dir / "operator-token"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def host_matches(host: str, patterns: list[str]) -> bool:
    """Starlette's TrustedHost rules: exact names, or ``*.example.com`` for its subdomains."""
    host = host.split(":")[0].lower()
    for p in patterns:
        p = p.lower()
        if p == "*" or host == p or (p.startswith("*.") and host.endswith(p[1:])):
            return True
    return False


class McpOnlyHosts:
    """Requests for a Host in ``BOT_MCP_HOSTS`` (and not in ``BOT_ALLOWED_HOSTS``) may only reach ``/mcp``."""

    def __init__(self, app: ASGIApp, mcp_hosts: list[str], allowed_hosts: list[str]) -> None:
        self.app = app
        self.mcp_hosts = mcp_hosts
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.mcp_hosts and scope.get("path") != "/mcp":
            host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "")
            if host_matches(host, self.mcp_hosts) and not host_matches(host, self.allowed_hosts):
                await JSONResponse({"error": "only /mcp is published on this host"}, status_code=404)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def get_engine(request: Request) -> Engine:
    eng = getattr(request.app.state, "engine", None)
    if eng is None:
        raise HTTPException(503, "the engine is starting")
    return eng


EngineDep = Annotated[Engine, Depends(get_engine)]


def operator(
    eng: EngineDep,
    x_operator_token: Annotated[str | None, Header(description="BOT_OPERATOR_TOKEN")] = None,
    x_operator: Annotated[str | None, Header(description="Who acts (shown in the audit trail)")] = None,
) -> str:
    """Every write needs ``X-Operator-Token``; ``X-Operator`` names who acted."""
    token = operator_token(eng)
    if not x_operator_token or not hmac.compare_digest(x_operator_token.encode(), token.encode()):
        raise HTTPException(401, "missing or wrong X-Operator-Token")
    return (x_operator or "operator")[:60]


OperatorDep = Annotated[str, Depends(operator)]


def create_app(engine: Engine | None = None, start_engine: bool = True) -> FastAPI:
    """The app; ``engine`` (tests) is used as given, else one is built from the settings at startup."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        eng = engine or Engine()
        app.state.engine = eng
        if start_engine:
            await eng.start()
        try:
            yield
        finally:
            if start_engine:
                await eng.stop()

    app = FastAPI(
        title="NetSim grok-bot",
        version=__version__,
        description=__doc__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    if engine is not None:
        settings = engine.settings
    else:
        from netsim_bot.settings import load_settings

        settings = load_settings()
    hosts = settings.allowed_hosts or ["127.0.0.1"]
    app.add_middleware(McpOnlyHosts, mcp_hosts=settings.mcp_hosts, allowed_hosts=hosts)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*hosts, *settings.mcp_hosts])
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    app.include_router(mcp_router)

    # ------------------------------------------------------------------ the page
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(eng: EngineDep) -> HTMLResponse:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        boot = {"token": operator_token(eng), "version": __version__, "links": eng.settings.public()["links"]}
        html = html.replace("__BOOT__", json.dumps(boot).replace("<", "\\u003c"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health(eng: EngineDep) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "lab_ok": eng.snapshot.ok,
            "lab_error": eng.snapshot.error,
            "effective": eng.effective(),
        }

    @app.get("/api/state")
    def state(eng: EngineDep) -> dict[str, Any]:
        return eng.state()

    @app.get("/api/stream")
    async def stream(
        request: Request,
        eng: EngineDep,
        after: Annotated[int | None, Query(description="Replay kept events after this id")] = None,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        since = after
        if since is None and last_event_id and last_event_id.isdigit():
            since = int(last_event_id)
        kept = eng.bus.recent()
        oldest = kept[0]["id"] if kept else None
        if since is not None and (since > eng.bus.last_id or (oldest is not None and oldest > since + 1)):
            since = None  # the bot restarted (ids began again) or the gap is no longer kept: start with a snapshot
        sub = eng.bus.open(after=since)

        async def gen() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                if since is None:
                    snapshot = eng.state()
                    hello = json.dumps(snapshot, default=str)
                    yield f"id: {snapshot['last_event_id']}\nevent: hello\ndata: {hello}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    event = await sub.next(timeout=getattr(request.app.state, "keepalive_s", 15.0))
                    if event is None:
                        yield ": keep-alive\n\n"
                        continue
                    data = json.dumps(event, default=str)
                    yield f"id: {event['id']}\nevent: {event['type']}\ndata: {data}\n\n"
            finally:
                sub.close()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ incidents, proposals, activity
    @app.get("/api/incidents")
    def incidents(
        eng: EngineDep, limit: Annotated[int, Query(ge=1, le=500)] = 50, active: bool = False
    ) -> list[dict[str, Any]]:
        return [i.summary() for i in eng.store.incidents(limit=limit, active_only=active)]

    @app.get("/api/incidents/{incident_id}")
    def incident(eng: EngineDep, incident_id: str) -> dict[str, Any]:
        inc = eng.tracker.incidents.get(incident_id) or eng.store.incident(incident_id)
        if inc is None:
            raise HTTPException(404, f"no incident {incident_id}")
        return {
            **inc.to_dict(),
            "proposals": [
                p.to_dict() for p in sorted(eng.store.proposals(incident_id=incident_id), key=lambda p: p.created)
            ],
            "activities": [a.to_dict() for a in eng.store.activities(incident_id=incident_id, limit=400)],
        }

    @app.get("/api/proposals")
    def proposals(
        eng: EngineDep, status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[dict[str, Any]]:
        return [p.to_dict() for p in eng.store.proposals(status=status, limit=limit)]

    @app.get("/api/proposals/{proposal_id}")
    def proposal(eng: EngineDep, proposal_id: str) -> dict[str, Any]:
        p = eng.store.proposal(proposal_id)
        if p is None:
            raise HTTPException(404, f"no proposal {proposal_id}")
        return p.to_dict()

    @app.get("/api/activities")
    def activities(
        eng: EngineDep,
        after: int | None = None,
        incident: str | None = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    ) -> list[dict[str, Any]]:
        return [a.to_dict() for a in eng.store.activities(incident_id=incident, after=after, limit=limit)]

    @app.get("/api/pager")
    def pager(eng: EngineDep) -> dict[str, Any]:
        return eng.pager_state()

    @app.get("/api/chat")
    def chat(
        eng: EngineDep, chat_id: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[dict[str, Any]]:
        return [m.to_dict() for m in eng.store.chat(chat_id, limit=limit)]

    # ------------------------------------------------------------------ writes (operator)
    def _decide(fn: Any) -> Any:
        async def run(*args: Any) -> Any:
            try:
                return await fn(*args)
            except KeyError as exc:
                raise HTTPException(404, f"not found: {exc}") from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        return run

    @app.post("/api/proposals/{proposal_id}/approve")
    async def approve(
        eng: EngineDep, who: OperatorDep, proposal_id: str, body: Decision | None = None
    ) -> dict[str, Any]:
        p = await _decide(eng.approve)(proposal_id, who, (body or Decision()).comment)
        return p.to_dict()

    @app.post("/api/proposals/{proposal_id}/reject")
    async def reject(
        eng: EngineDep, who: OperatorDep, proposal_id: str, body: Decision | None = None
    ) -> dict[str, Any]:
        p = await _decide(eng.reject)(proposal_id, who, (body or Decision()).comment)
        return p.to_dict()

    @app.post("/api/incidents/{incident_id}/approve-all")
    async def approve_all(
        eng: EngineDep, who: OperatorDep, incident_id: str, body: Decision | None = None
    ) -> list[dict[str, Any]]:
        done = await _decide(eng.approve_incident)(incident_id, who, (body or Decision()).comment)
        return [p.to_dict() for p in done]

    @app.post("/api/incidents/{incident_id}/reinvestigate")
    async def reinvestigate(eng: EngineDep, who: OperatorDep, incident_id: str) -> dict[str, Any]:
        inc = await _decide(eng.reinvestigate)(incident_id, who)
        return inc.summary()

    @app.post("/api/chat")
    async def ask(eng: EngineDep, who: OperatorDep, body: Ask) -> dict[str, Any]:
        msg = await eng.ask(body.message, body.chat_id, who)
        return msg.to_dict()

    @app.post("/api/control")
    async def control(eng: EngineDep, who: OperatorDep, body: ControlIn) -> dict[str, Any]:
        try:
            c = await eng.set_control(
                who,
                paused=body.paused,
                brain=body.brain,
                autonomy=body.autonomy,
                reason=body.reason,
                pager_muted=body.pager_muted,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**c.to_dict(), "effective": eng.effective()}

    @app.post("/api/pager/test")
    async def pager_test(eng: EngineDep, who: OperatorDep) -> dict[str, Any]:
        return await eng.pager_test()

    @app.post("/api/grok/check")
    async def grok_check(eng: EngineDep, who: OperatorDep) -> dict[str, Any]:
        return await eng.grok_check()

    @app.exception_handler(HTTPException)
    async def http_error(_req: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
