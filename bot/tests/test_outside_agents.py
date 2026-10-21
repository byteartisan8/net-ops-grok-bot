"""Outside agents: the ``/mcp`` endpoint (protocol, auth, Origin and Host guards), the agent's tools (reads, proposals
that always wait for a human, NOC tools), the visitor robot, and the pager (policy, delivery, retries, payload)."""

from __future__ import annotations

import asyncio
import json
import warnings
from typing import Any

import httpx
import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_bot import agent as agent_mod
from netsim_bot.api.app import create_app
from netsim_bot.engine import Engine
from netsim_bot.pager import Pager
from netsim_bot.settings import SettingsError
from netsim_bot.store import Store

from .test_engine import drain, lab_for, until

AUTH = {"Authorization": "Bearer agent-token"}


def rpc(method: str, params: dict[str, Any] | None = None, id_: int | None = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


def make_client(lab, settings_factory, tmp_path, **env):
    env = {"BOT_BRAIN": "rules", "BOT_AGENT_TOKEN": "agent-token", **env}
    s = settings_factory(**env)
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())
    app = create_app(engine=eng, start_engine=False)
    c = TestClient(app)
    c.engine = eng
    return c


@pytest.fixture
def client(fake_lab, settings_factory, tmp_path):
    with make_client(fake_lab("healthy"), settings_factory, tmp_path) as c:
        yield c


def tool_result(r: httpx.Response) -> tuple[bool, Any]:
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return not res["isError"], res["structuredContent"]


# ---------------------------------------------------------------------------------------------- the protocol
def test_endpoint_is_off_without_a_token(fake_lab, settings_factory, tmp_path) -> None:
    with make_client(fake_lab("healthy"), settings_factory, tmp_path, BOT_AGENT_TOKEN="") as c:
        r = c.post("/mcp", json=rpc("ping"), headers=AUTH)
        assert r.status_code == 404 and "BOT_AGENT_TOKEN" in r.json()["error"]


def test_auth_and_the_operator_token_is_not_the_agent_token(client) -> None:
    assert client.post("/mcp", json=rpc("ping")).status_code == 401
    r = client.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer op-token"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    # and the agent token opens no operator write
    assert (
        client.post("/api/control", json={"paused": True}, headers={"X-Operator-Token": "agent-token"}).status_code
        == 401
    )
    assert client.post("/mcp", json=rpc("ping"), headers=AUTH).json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_initialize_negotiates_the_version(client) -> None:
    r = client.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}), headers=AUTH)
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "netsim-noc"
    assert res["capabilities"] == {"tools": {"listChanged": False}} and "cannot approve" in res["instructions"]
    r = client.post("/mcp", json=rpc("initialize", {"protocolVersion": "1999-01-01"}), headers=AUTH)
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    # notifications get 202 and no body; a batch of only notifications too
    r = client.post("/mcp", json=rpc("notifications/initialized", id_=None), headers=AUTH)
    assert r.status_code == 202 and r.content == b""
    assert client.post("/mcp", json=[rpc("notifications/initialized", id_=None)], headers=AUTH).status_code == 202


def test_protocol_errors(client) -> None:
    assert client.post("/mcp", content=b"{nope", headers=AUTH).json()["error"]["code"] == -32700
    assert client.post("/mcp", json={"id": 1, "method": "ping"}, headers=AUTH).json()["error"]["code"] == -32600
    assert client.post("/mcp", json=rpc("resources/list"), headers=AUTH).json()["error"]["code"] == -32601
    r = client.post("/mcp", json=rpc("tools/call", {"name": 5}), headers=AUTH)
    assert r.json()["error"]["code"] == -32602
    r = client.post("/mcp", json=rpc("ping"), headers={**AUTH, "MCP-Protocol-Version": "2020-01-01"})
    assert r.status_code == 400
    batch = client.post("/mcp", json=[rpc("ping", id_=1), rpc("ping", id_=2)], headers=AUTH).json()
    assert [b["id"] for b in batch] == [1, 2]
    assert client.get("/mcp", headers=AUTH).status_code == 405


def test_foreign_origin_is_refused(client) -> None:
    r = client.post("/mcp", json=rpc("ping"), headers={**AUTH, "Origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.post("/mcp", json=rpc("ping"), headers={**AUTH, "Origin": "http://127.0.0.1:8095"}).status_code == 200


def test_mcp_only_hosts(fake_lab, settings_factory, tmp_path) -> None:
    with make_client(fake_lab("healthy"), settings_factory, tmp_path, BOT_MCP_HOSTS="*.trycloudflare.com") as c:
        tunnel = {"Host": "lab-noc.trycloudflare.com"}
        assert c.post("/mcp", json=rpc("ping"), headers={**AUTH, **tunnel}).status_code == 200
        for path in ("/", "/api/state", "/api/incidents", "/api/pager"):
            assert c.get(path, headers=tunnel).status_code == 404, path
        assert c.get("/api/state").status_code == 200  # the local name still sees everything
        assert c.get("/api/state", headers={"Host": "evil.example"}).status_code == 400


# ---------------------------------------------------------------------------------------------- the tools
def test_tools_list_follows_autonomy(client) -> None:
    tools = client.post("/mcp", json=rpc("tools/list"), headers=AUTH).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert len(tools) == 20 and {"get_drift", "propose_apply_intent", "get_noc_status", "wait_for_proposal"} <= names
    assert "finish_investigation" not in names
    propose = next(t for t in tools if t["name"] == "propose_apply_intent")
    assert "incident_id" in propose["inputSchema"]["properties"] and not propose["annotations"]["readOnlyHint"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    asyncio.run(client.engine.set_control("tester", autonomy="observe"))
    names = {t["name"] for t in client.post("/mcp", json=rpc("tools/list"), headers=AUTH).json()["result"]["tools"]}
    assert not any(n.startswith("propose_") for n in names)


def test_a_read_brings_the_visitor_in_and_it_leaves_later(client, monkeypatch) -> None:
    eng = client.engine
    assert eng.crew["GROK"].status == "away" and eng.crew["GROK"].role == "visitor"
    ok, result = tool_result(client.post("/mcp", json=rpc("tools/call", {"name": "list_services"}), headers=AUTH))
    assert ok and result["services"]
    assert eng.crew["GROK"].status == "working"
    acts = [a for a in eng.store.activities() if a.bot == "GROK"]
    assert [a.kind for a in acts] == ["tool", "result"] and acts[0].station == "screen"
    monkeypatch.setattr(agent_mod, "VISITOR_LINGER_S", 0.0)
    eng.agent.tick()
    assert eng.crew["GROK"].status == "away"


def test_tool_errors_come_back_as_results(client) -> None:
    ok, res = tool_result(client.post("/mcp", json=rpc("tools/call", {"name": "approve"}), headers=AUTH))
    assert not ok and "no tool 'approve'" in res["error"]
    args = {"name": "get_device_state", "arguments": {}}
    ok, res = tool_result(client.post("/mcp", json=rpc("tools/call", args), headers=AUTH))
    assert not ok and "'host' is required" in res["error"]
    ok, res = tool_result(
        client.post(
            "/mcp",
            json=rpc("tools/call", {"name": "get_incident", "arguments": {"incident_id": "INC-9"}}),
            headers=AUTH,
        )
    )
    assert not ok and "no incident" in res["error"]


def test_agent_proposal_waits_for_a_human_even_in_auto_mode(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")
    s = settings_factory(BOT_BRAIN="rules", BOT_AGENT_TOKEN="agent-token", BOT_AUTONOMY="auto")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())

    async def scenario() -> None:
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        task = eng._tasks.pop(inc.id)  # stop the crew's own investigation: this test is about the agent
        task.cancel()
        ok, res = await eng.agent.call(
            "propose_apply_intent", {"host": "hub-a", "reason": "r", "incident_id": "INC-77"}
        )
        assert not ok and "no incident INC-77" in res["error"]
        ok, res = await eng.agent.call(
            "propose_apply_intent", {"host": "hub-a", "reason": "NetBox wins", "incident_id": inc.id}
        )
        assert ok and res["status"] == "pending" and res["note"] == "waits for the operator's approval"
        p = eng.store.proposal(res["proposal_id"])
        assert p.proposed_by == "GROK" and p.brain == "external" and p.incident_id == inc.id
        assert p.id in inc.proposals and eng._exec_queue.empty()
        _path, body, headers = lab.posts("/devices/hub-a/apply-intent")[0]
        assert headers["x-lab-actor"] == "grok-bot/GROK" and not body.get("confirm")
        # the agent waits; a human approves; the executor applies with the human in the audit actor
        waiter = asyncio.ensure_future(eng.agent.call("wait_for_proposal", {"proposal_id": p.id, "timeout_s": 5}))
        await asyncio.sleep(0.05)
        await eng.approve(p.id, "alice")
        await drain(eng)
        ok, res = await waiter
        assert ok and res["decided"] and res["status"] == "applied" and res["verdict"] == "ok"
        confirm = [h for _p, b, h in lab.posts("/devices/hub-a/apply-intent") if b.get("confirm")]
        assert confirm[0]["x-lab-actor"] == "grok-bot/GROK approved-by:alice"
        ok, res = await eng.agent.call("add_incident_note", {"incident_id": inc.id, "text": "told Customer-A\nmore"})
        assert ok and inc.notes[-1] == "GROK: told Customer-A\nmore"
        ok, res = await eng.agent.call("get_incident", {"incident_id": inc.id})
        assert ok and res["proposal_details"][0]["status"] == "applied" and res["notes"][-1].startswith("GROK:")
        ok, res = await eng.agent.call("get_noc_status", {})
        assert ok and res["active_incidents"][0]["id"] == inc.id and res["noc_room"] == "http://127.0.0.1:8095"
        await eng.stop()

    asyncio.run(scenario())


def test_wait_times_out(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")
    s = settings_factory(BOT_BRAIN="rules", BOT_AGENT_TOKEN="agent-token")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())

    async def scenario() -> None:
        ok, res = await eng.agent.call("propose_apply_intent", {"host": "hub-a", "reason": "r"})
        assert ok and res["status"] == "pending"
        ok, res = await eng.agent.call("wait_for_proposal", {"proposal_id": res["proposal_id"], "timeout_s": 1})
        assert ok and not res["decided"] and "still pending" in res["summary"]
        await eng.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------- the pager
class Receiver:
    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses) or [200]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, json={"ok": status < 300})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def paged_engine(fake_lab, settings_factory, tmp_path, receiver: Receiver, state="hub-down", **env) -> Engine:
    lab = lab_for(fake_lab, state)
    base = {"BOT_BRAIN": "rules", "BOT_PAGER_URL": "https://api2.cursor.sh/automations/webhook/abc"}
    s = settings_factory(**{**base, **env})
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())
    eng.pager._transport = httpx.MockTransport(receiver)
    eng.pager.delays = (0.0, 0.0, 0.0)
    return eng


def test_one_page_per_incident_with_bearer_and_payload(fake_lab, settings_factory, tmp_path) -> None:
    rx = Receiver(200)
    eng = paged_engine(
        fake_lab, settings_factory, tmp_path, rx, BOT_PAGER_TOKEN="Bearer crsr_abc", BOT_PAGER_EVENTS="opened,resolved"
    )

    async def scenario() -> None:
        await eng.tick()
        await until(lambda: len(rx.requests) == 1)
        req = rx.requests[0]
        assert req.headers["authorization"] == "Bearer crsr_abc"  # "Bearer " pasted along is not doubled
        body = rx.bodies()[0]
        inc = next(iter(eng.tracker.incidents.values()))
        assert body["event"] == "incident.opened" and body["incident"]["id"] == inc.id
        assert body["incident"]["severity"] == inc.severity and body["incident"]["problems"]
        assert body["services"] and {"customer", "tier"} <= set(body["services"][0])
        assert body["links"]["noc_room"] == "http://127.0.0.1:8095" and "propose" in body["how_to_respond"]
        await eng.tick()  # the same incident: no second page
        await asyncio.sleep(0.05)
        assert len(rx.requests) == 1
        await until(lambda: any(a.kind == "page" for a in eng.store.activities()))
        eng.lab = lab_for(fake_lab, "healthy").client()
        task = eng._tasks.get(inc.id)
        if task is not None:
            task.cancel()
        await eng.tick()
        await until(lambda: len(rx.requests) == 2)
        assert rx.bodies()[1]["event"] == "incident.resolved" and "report" in rx.bodies()[1]
        assert [h["status"] for h in eng.pager_state()["history"]] == ["sent", "sent"]
        await eng.stop()

    asyncio.run(scenario())


def test_muted_paused_severity_and_rate_limit(fake_lab, settings_factory, tmp_path) -> None:
    rx = Receiver(200)
    eng = paged_engine(fake_lab, settings_factory, tmp_path, rx, BOT_PAGER_MAX_PER_HOUR="1")
    inc = {"id": "INC-0001", "severity": "High"}

    async def scenario() -> None:
        p = eng.pager
        assert p.decide("opened", inc, muted=True, paused=False) == "muted"
        assert p.decide("opened", inc, muted=False, paused=True) == "maintenance mode"
        assert "below" in p.decide("opened", {"id": "INC-2", "severity": "Warning"}, muted=False, paused=False)
        assert p.decide("resolved", inc, muted=False, paused=False).startswith("event resolved")
        assert p.page("opened", inc, {}) is None
        assert p.page("opened", {"id": "INC-0002", "severity": "High"}, {}).startswith("rate limit")
        await until(lambda: len(rx.requests) == 1)
        await eng.set_control("tester", pager_muted=True)
        assert eng.state()["control"]["pager_muted"] is True
        await eng.stop()

    asyncio.run(scenario())


def test_retries_5xx_but_not_4xx(settings_factory) -> None:
    async def scenario() -> None:
        s = settings_factory(BOT_PAGER_URL="https://hook.test/x")
        rx = Receiver(503, 503, 200)
        p = Pager(lambda: s, transport=httpx.MockTransport(rx))
        p.delays = (0.0, 0.0, 0.0)
        rec = await p.deliver(p.payload("opened", {"incident": {"id": "INC-1"}}))
        assert rec["status"] == "sent" and rec["attempts"] == 3
        rx = Receiver(401)
        p = Pager(lambda: s, transport=httpx.MockTransport(rx))
        p.delays = (0.0, 0.0, 0.0)
        rec = await p.deliver(p.payload("opened", {"incident": {"id": "INC-1"}}))
        assert rec["status"] == "failed" and rec["attempts"] == 1 and rec["http"] == 401

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        p = Pager(lambda: s, transport=httpx.MockTransport(down))
        p.delays = (0.0, 0.0, 0.0)
        rec = await p.deliver(p.payload("opened", {}))
        assert rec["status"] == "failed" and rec["attempts"] == 3 and "ConnectError" in rec["error"]

    asyncio.run(scenario())


def test_pager_test_endpoint(fake_lab, settings_factory, tmp_path) -> None:
    rx = Receiver(200)
    with make_client(
        fake_lab("healthy"), settings_factory, tmp_path, BOT_PAGER_URL="https://hook.test/x", BOT_PAGER_TOKEN="k"
    ) as c:
        c.engine.pager._transport = httpx.MockTransport(rx)
        assert c.post("/api/pager/test").status_code == 401
        r = c.post("/api/pager/test", headers={"X-Operator-Token": "op-token"})
        assert r.json()["status"] == "sent" and rx.bodies()[0]["event"] == "incident.test"
        st = c.get("/api/pager").json()
        assert st["configured"] and st["history"][0]["status"] == "sent" and "url" not in json.dumps(st).lower()


def test_settings_validation(settings_factory) -> None:
    with pytest.raises(SettingsError, match="BOT_PAGER_EVENTS"):
        settings_factory(BOT_PAGER_EVENTS="opened,exploded")
    with pytest.raises(SettingsError, match="taken by the crew"):
        settings_factory(BOT_AGENT_NAME="ping")
    with pytest.raises(SettingsError, match="BOT_PAGER_MIN_SEVERITY"):
        settings_factory(BOT_PAGER_MIN_SEVERITY="loud")
    s = settings_factory(BOT_PAGER_MIN_SEVERITY="high", BOT_AGENT_NAME="claw")
    assert s.pager_min_severity == "High" and s.agent_name == "CLAW"


def test_crew_and_agent_filing_the_same_change_at_once_make_one_ticket(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")
    s = settings_factory(BOT_BRAIN="rules", BOT_AGENT_TOKEN="agent-token")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())

    async def scenario() -> None:
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        eng._tasks.pop(inc.id).cancel()
        real = eng.lab.plan_apply_intent

        async def slow_dry_run(*a: Any, **kw: Any) -> Any:  # a real dry run takes 1-2 s: the window of the race
            await asyncio.sleep(0.05)
            return await real(*a, **kw)

        eng.lab.plan_apply_intent = slow_dry_run
        crew = eng._propose(inc.id, "PING", "rules", kind="apply-intent", host="hub-a", reason="crew", params={})
        visitor = eng.agent.call("propose_apply_intent", {"host": "hub-a", "reason": "agent", "incident_id": inc.id})
        a, (ok, b) = await asyncio.gather(crew, visitor)
        assert ok and a["proposal_id"] == b["proposal_id"] and b["proposed_by"] == "PING"
        tickets = [p for p in eng.store.proposals(incident_id=inc.id) if p.host == "hub-a"]
        assert len(tickets) == 1 and tickets[0].reason == "crew"
        await eng.stop()

    asyncio.run(scenario())


def test_a_stale_plan_with_nothing_left_to_change_is_obsolete(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")
    s = settings_factory(BOT_BRAIN="rules", BOT_AGENT_TOKEN="agent-token")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())

    async def scenario() -> None:
        ok, res = await eng.agent.call("propose_apply_intent", {"host": "hub-a", "reason": "r"})
        assert ok
        empty = {"plan_id": "0ff", "host": "hub-a", "no_change": True, "diff": []}
        lab.set("POST /devices/hub-a/apply-intent", lambda r: (409, {"detail": {"message": "changed", "plan": empty}}))
        await eng.approve(res["proposal_id"], "alice")
        await drain(eng)
        p = eng.store.proposal(res["proposal_id"])
        assert p.status == "obsolete" and "nothing left to change" in p.comment
        await eng.stop()

    asyncio.run(scenario())
