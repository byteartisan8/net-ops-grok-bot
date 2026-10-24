"""The web server: the NOC page, reads, operator-token writes, the alert intake, /mcp per role, the Host guards."""

from __future__ import annotations

import json
import warnings

import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_bot.api.app import create_app, operator_token
from netsim_bot.engine import Engine
from netsim_bot.models import Incident, Proposal
from netsim_bot.roles import COMMON, TOOLSETS
from netsim_bot.store import Store

OP = {"X-Operator-Token": "op-token", "X-Operator": "pytest"}
HOOK = {"Authorization": "Bearer hook-token"}
TOKENS = {"noc-engineer": "noceng-token", "network-engineer": "neteng-token", "noc-manager": "nocmgr-token"}


@pytest.fixture
def client(fake_lab, settings_factory, tmp_path, routines):
    s = settings_factory(BOT_MCP_HOSTS="*.trycloudflare.com", BOT_BATCH_S="30", BOT_MAX_BATCH_S="30")
    eng = Engine(
        settings_loader=lambda: s,
        store=Store(tmp_path / "bot.db"),
        lab=fake_lab("healthy").client(),
        page_transport=routines.transport(),
    )
    app = create_app(engine=eng, start_engine=False)
    app.state.keepalive_s = 0.05
    with TestClient(app) as c:
        c.engine = eng
        yield c


def mcp(client, role: str | None, method: str, params: dict | None = None, **headers: str):
    h = {"Authorization": f"Bearer {TOKENS[role]}"} if role else {}
    h.update(headers)
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=h)


def test_page_carries_the_operator_token_and_links(client) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    boot = json.loads(r.text.split('<script id="boot" type="application/json">')[1].split("</script>")[0])
    assert boot["token"] == "op-token" and boot["links"]["zabbix"] == "http://127.0.0.1:8080"
    assert client.get("/static/js/main.js").status_code == 200
    assert client.get("/static/css/noc.css").status_code == 200


def test_reads_show_the_three_roles_and_no_secret(client) -> None:
    st = client.get("/api/state").json()
    assert [m["name"] for m in st["roles"]] == ["NOCENG", "NETENG", "NOCMGR"]
    assert st["settings"]["roles"]["noc-engineer"] == {
        "bot": "NOCENG",
        "title": "NOC Engineer",
        "mcp_token": True,
        "terminal": True,
        "routine": True,
    }
    text = json.dumps(st)
    for secret in ("noceng-token", "noceng-terminal", "crsr_noceng", "hook-token", "lab-token", "routines.test"):
        assert secret not in text
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/incidents").json() == [] and client.get("/api/incidents/INC-9999").status_code == 404
    assert client.get("/api/pages").json()["roles"]["noc-manager"]["routine"] is True
    assert client.get("/api/pages", params={"role": "boss"}).status_code == 404


def test_the_alert_intake_needs_its_bearer_token(client, kit) -> None:
    body = kit.zabbix_body("Customer-A")
    assert client.post("/hooks/zabbix", json=body).status_code == 401
    assert client.post("/hooks/zabbix", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/hooks/zabbix", json=body, headers=OP).status_code == 401  # the operator token is not it
    r = client.post("/hooks/zabbix", json=body, headers=HOOK)
    assert r.status_code == 202 and r.json()["outcome"] == "batched" and r.json()["batch"].startswith("BAT-")
    assert client.post("/hooks/zabbix", content=b"{nope", headers=HOOK).status_code == 400
    bad = client.post("/hooks/zabbix", json={**body, "role": "boss"}, headers=HOOK)
    assert bad.status_code == 422 and "role must be one of" in bad.json()["error"]
    assert client.get("/api/alerts").json()[0]["customer"] == "Customer-A"


def test_the_intake_is_off_without_a_token(fake_lab, settings_factory, tmp_path, kit) -> None:
    s = settings_factory(BOT_ZABBIX_HOOK_TOKEN="")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "x.db"), lab=fake_lab().client())
    with TestClient(create_app(engine=eng, start_engine=False)) as c:
        assert c.post("/hooks/zabbix", json=kit.zabbix_body("Customer-A"), headers=HOOK).status_code == 503


def test_writes_need_the_operator_token(client) -> None:
    p = Proposal(id="CHG-0001", incident_id=None, kind="field-repair", host="hub-b", reason="r", proposed_by="NOCENG")
    client.engine.store.save_proposal(p)
    assert client.post("/api/proposals/CHG-0001/approve").status_code == 401
    assert client.post("/api/proposals/CHG-0001/approve", headers={"X-Operator-Token": "wrong"}).status_code == 401
    assert (
        client.post("/api/proposals/CHG-0001/approve", headers={"X-Operator-Token": "noceng-token"}).status_code == 401
    )
    early = client.post("/api/proposals/CHG-0001/approve", headers=OP)
    assert early.status_code == 409 and "NOC Manager's review" in early.json()["error"]
    p.status = "pending"  # the NOC Manager proposed it
    client.engine.store.save_proposal(p)
    r = client.post("/api/proposals/CHG-0001/reject", headers=OP, json={"comment": "not now"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decided_by"] == "pytest"
    again = client.post("/api/proposals/CHG-0001/approve", headers=OP)
    assert again.status_code == 409 and "rejected" in again.json()["error"]
    assert client.post("/api/proposals/CHG-0404/approve", headers=OP).status_code == 404
    assert client.post("/api/control", headers=OP, json={"autonomy": "auto"}).status_code == 422  # gone
    assert client.post("/api/control", headers=OP, json={"sink_url": "https://evil.example/x"}).status_code == 422
    c = client.post("/api/control", headers=OP, json={"paused": True, "reason": "e2e"}).json()
    assert c["paused"] is True and c["effective"]["paused"] is True
    c = client.post("/api/control", headers=OP, json={"sink_url": "http://172.30.10.61:8096"}).json()
    assert c["sink_url"] is True  # shown as set, not where
    assert client.post("/api/control", headers=OP, json={"bogus": 1}).status_code == 422
    assert client.post("/api/pages/test/noc-engineer").status_code == 401
    assert client.post("/api/pages/test/boss", headers=OP).status_code == 404


def test_approve_incident_takes_exactly_the_tickets_you_saw(client) -> None:
    eng = client.engine
    eng._save(Incident(id="INC-0007"))
    for pid, kind, plan in (("CHG-0011", "apply-intent", {"plan_id": "plan-a"}), ("CHG-0012", "field-repair", None)):
        p = Proposal(id=pid, incident_id="INC-0007", kind=kind, host="hub-a", reason="r", proposed_by="NETENG")
        p.status, p.plan = "pending", plan
        eng.store.save_proposal(p)
    review = Proposal(
        id="CHG-0013", incident_id="INC-0007", kind="apply-intent", host="hub-b", reason="r", proposed_by="NETENG"
    )
    eng.store.save_proposal(review)  # still in the NOC Manager's review
    saw = {"CHG-0011": "plan-a"}  # the page showed one ticket; another one arrived since
    r = client.post("/api/incidents/INC-0007/approve-all", headers=OP, json={"tickets": saw})
    assert r.status_code == 409 and "CHG-0011" in r.json()["error"] and "CHG-0012" in r.json()["error"]
    assert eng.store.proposal("CHG-0011").status == "pending"
    stale = client.post("/api/incidents/INC-0007/approve-all", headers=OP, json={"tickets": {**saw, "CHG-0012": "x"}})
    assert stale.status_code == 409
    r = client.post("/api/incidents/INC-0007/approve-all", headers=OP, json={"tickets": {**saw, "CHG-0012": None}})
    assert r.status_code == 200 and [p["id"] for p in r.json()] == ["CHG-0012", "CHG-0011"]  # make before break
    assert eng.store.proposal("CHG-0013").status == "review"
    assert client.post("/api/incidents/INC-0404/approve-all", headers=OP).status_code == 404


def test_a_test_page(client, routines) -> None:
    r = client.post("/api/pages/test/noc-manager", headers=OP)
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["event"] == "test"
    assert routines.pages[-1]["path"] == "/nocmgr" and routines.pages[-1]["body"]["event"] == "test"


def test_host_guards(client, kit) -> None:
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/health", headers={"Host": "127.0.0.1:8095"}).status_code == 200
    tunnel = {"Host": "x-y.trycloudflare.com"}
    assert client.get("/", headers=tunnel).status_code == 404  # a tunnel host reaches /mcp only
    assert client.get("/api/state", headers=tunnel).status_code == 404
    hook = client.post("/hooks/zabbix", json=kit.zabbix_body("Customer-A"), headers={**tunnel, **HOOK})
    assert hook.status_code == 404  # never the alert intake
    assert mcp(client, "noc-engineer", "ping", Host="x-y.trycloudflare.com").status_code == 200


def test_mcp_tokens_decide_the_role(client) -> None:
    assert mcp(client, None, "ping").status_code == 401
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer op-token"}).status_code == 401
    for role, spec_bot in (("noc-engineer", "noceng"), ("network-engineer", "neteng"), ("noc-manager", "nocmgr")):
        init = mcp(client, role, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
        assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == f"netsim-noc-{spec_bot}"
        assert f"({spec_bot.upper()})" in init["instructions"]
        names = [t["name"] for t in mcp(client, role, "tools/list").json()["result"]["tools"]]
        assert names == [*COMMON, *TOOLSETS[role]]
    eng_tools = {t["name"]: t for t in mcp(client, "noc-engineer", "tools/list").json()["result"]["tools"]}
    assert eng_tools["ack_zabbix_problems"]["annotations"]["readOnlyHint"] is False
    assert eng_tools["zabbix_get"]["annotations"]["readOnlyHint"] is True
    assert eng_tools["escalate"]["inputSchema"]["properties"]["to"]["enum"] == ["network-engineer", "noc-manager"]
    net = {t["name"]: t for t in mcp(client, "network-engineer", "tools/list").json()["result"]["tools"]}
    assert net["escalate"]["inputSchema"]["properties"]["to"]["enum"] == ["noc-manager"]
    assert "incident_id" in net["propose_apply_intent"]["inputSchema"]["required"]
    for tools in (eng_tools, net):
        assert not [n for n in tools if "approve" in n or "apply_change" in n or "reset" in n or "inject" in n]


def test_a_wrong_role_tool_names_the_role_that_has_it(client) -> None:
    r = mcp(client, "noc-manager", "tools/call", {"name": "take_incident", "arguments": {"incident_id": "INC-0001"}})
    out = r.json()["result"]
    assert out["isError"] is True and "belongs to the NOC Engineer (noc-engineer)" in out["content"][0]["text"]
    r = mcp(client, "noc-engineer", "tools/call", {"name": "propose_apply_intent", "arguments": {}})
    assert "Network Engineer" in r.json()["result"]["structuredContent"]["error"]
    r = mcp(client, "noc-engineer", "tools/call", {"name": "propose_field_repair", "arguments": {}})
    assert "no incident" in r.json()["result"]["structuredContent"]["error"]  # its own tool: refused on the input
    r = mcp(client, "noc-engineer", "tools/call", {"name": "approve_ticket", "arguments": {}})
    assert "no tool" in r.json()["result"]["structuredContent"]["error"]


def test_mcp_protocol_details(client) -> None:
    assert client.get("/mcp").status_code == 405
    batch = client.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}],
        headers={"Authorization": "Bearer noceng-token"},
    )
    assert batch.status_code == 200 and len(batch.json()) == 1
    note = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": "Bearer noceng-token"},
    )
    assert note.status_code == 202
    assert mcp(client, "noc-engineer", "ping", Origin="https://evil.example").status_code == 403
    assert mcp(client, "noc-engineer", "nope").json()["error"]["code"] == -32601


def test_mcp_is_off_without_role_tokens(fake_lab, settings_factory, tmp_path) -> None:
    no_roles = {"BOT_NOCENG_TOKEN": "", "BOT_NETENG_TOKEN": "", "BOT_NOCMGR_TOKEN": ""}
    no_terminals = {"BOT_NOCENG_TERMINAL_TOKEN": "", "BOT_NETENG_TERMINAL_TOKEN": "", "BOT_NOCMGR_TERMINAL_TOKEN": ""}
    s = settings_factory(**no_roles, **no_terminals)
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "y.db"), lab=fake_lab().client())
    with TestClient(create_app(engine=eng, start_engine=False)) as c:
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer x"}).status_code == 404
    s = settings_factory(**no_roles)  # the terminals alone keep it on, for controller_cli
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "z.db"), lab=fake_lab().client())
    with TestClient(create_app(engine=eng, start_engine=False)) as c:
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer x"}).status_code == 401
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert c.post("/mcp", json=ping, headers={"Authorization": "Bearer neteng-terminal"}).status_code == 200


def test_stream_starts_with_a_snapshot(client) -> None:
    with client.stream("GET", "/api/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = []
        for line in r.iter_lines():
            lines.append(line)
            if line.startswith("data:"):
                break
    assert "event: hello" in lines
    hello = json.loads(lines[-1][5:])
    assert "roles" in hello and "lab" in hello and "pages" in hello


def test_operator_token_is_generated_once_when_not_configured(fake_lab, settings_factory, tmp_path) -> None:
    s = settings_factory(BOT_OPERATOR_TOKEN="")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=fake_lab().client())
    first = operator_token(eng)
    assert len(first) >= 24 and operator_token(eng) == first
    assert (s.data_dir / "operator-token").stat().st_mode & 0o777 == 0o600
