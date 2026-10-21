"""The web server: the NOC page, reads, operator-token writes, the Host guard and the event stream."""

from __future__ import annotations

import json
import warnings

import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_bot.api.app import create_app, operator_token
from netsim_bot.engine import Engine
from netsim_bot.models import Proposal
from netsim_bot.store import Store

OP = {"X-Operator-Token": "op-token", "X-Operator": "pytest"}


@pytest.fixture
def client(fake_lab, settings_factory, tmp_path):
    s = settings_factory(BOT_BRAIN="rules")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=fake_lab("healthy").client())
    app = create_app(engine=eng, start_engine=False)
    app.state.keepalive_s = 0.05
    with TestClient(app) as c:
        c.engine = eng
        yield c


def test_page_carries_the_operator_token_and_links(client) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    boot = json.loads(r.text.split('<script id="boot" type="application/json">')[1].split("</script>")[0])
    assert boot["token"] == "op-token" and boot["links"]["zabbix"] == "http://127.0.0.1:8080"
    assert client.get("/static/js/main.js").status_code == 200
    assert client.get("/static/css/noc.css").status_code == 200


def test_reads(client) -> None:
    st = client.get("/api/state").json()
    assert st["effective"]["brain"] == "rules" and {m["name"] for m in st["crew"]} == {
        "POLL",
        "PING",
        "TRACE",
        "PATCH",
        "GROK",
    }
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/incidents").json() == []
    assert client.get("/api/incidents/INC-9999").status_code == 404


def test_writes_need_the_operator_token(client) -> None:
    p = Proposal(
        id="CHG-0001",
        incident_id=None,
        kind="field-repair",
        host="hub-b",
        reason="r",
        proposed_by="PING",
        brain="rules",
        params={"kind": "device"},
    )
    client.engine.store.save_proposal(p)
    assert client.post("/api/proposals/CHG-0001/approve").status_code == 401
    assert client.post("/api/proposals/CHG-0001/approve", headers={"X-Operator-Token": "wrong"}).status_code == 401
    r = client.post("/api/proposals/CHG-0001/reject", headers=OP, json={"comment": "not now"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decided_by"] == "pytest"
    again = client.post("/api/proposals/CHG-0001/approve", headers=OP)
    assert again.status_code == 409 and "rejected" in again.json()["error"]
    assert client.post("/api/proposals/CHG-0404/approve", headers=OP).status_code == 404
    assert client.post("/api/control", headers=OP, json={"autonomy": "reckless"}).status_code == 422
    c = client.post("/api/control", headers=OP, json={"paused": True, "reason": "e2e"}).json()
    assert c["paused"] is True and c["effective"]["paused"] is True
    assert client.post("/api/control", headers=OP, json={"bogus": 1}).status_code == 422


def test_host_guard(client) -> None:
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/health", headers={"Host": "127.0.0.1:8095"}).status_code == 200


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
    assert "crew" in hello and "lab" in hello


def test_operator_token_is_generated_once_when_not_configured(fake_lab, settings_factory, tmp_path) -> None:
    s = settings_factory(BOT_OPERATOR_TOKEN="")
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=fake_lab().client())
    first = operator_token(eng)
    assert len(first) >= 24 and operator_token(eng) == first
    assert (s.data_dir / "operator-token").stat().st_mode & 0o777 == 0o600
