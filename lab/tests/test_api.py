"""lab-api without a lab: write auth, the NetBox webhook signature, the audit log, the OpenAPI export."""

from __future__ import annotations

import hashlib
import hmac
import json
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import (a UserWarning subclass)
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_lab.api.app import AppState, create_app
from netsim_lab.api.server import openapi_text
from netsim_lab.settings import load_settings

from netsim_common.paths import repo_root

TOKEN = "t0ken-for-tests"
SECRET = "webhook-secret-for-tests"


class FakeCore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_uplink(self, host, on=True):
        self.calls.append(("uplink", host, on))
        return f"netsim:fault:uplink:{host}"

    def faults(self):
        return []

    def clear_faults(self):
        return []


class FakeLab:
    def __init__(self, settings) -> None:
        from netsim_common.topology import load_topology

        self.settings = settings
        self.topology = load_topology()
        self.core = FakeCore()
        self.forgotten = 0

    def forget_intent(self) -> None:
        self.forgotten += 1

    def stopped(self):
        return []


@pytest.fixture
def client(tmp_path: Path):
    settings = replace(load_settings(environ={}), lab_api_token=TOKEN, webhook_secret=SECRET, data_dir=tmp_path)
    syncs: list[int] = []
    state = AppState(
        settings=settings,
        lab=FakeLab(settings),
        sync_fn=lambda: syncs.append(1) or {"summary": "0 changes"},
        settings_loader=lambda: settings,
    )
    app = create_app(state)
    with TestClient(app) as c:
        c.state, c.syncs = state, syncs  # type: ignore[attr-defined]
        yield c
    state.syncer.stop()


def test_health_needs_no_token(client):
    assert client.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/faults", {"kind": "uplink", "host": "hub-a"}),
        ("delete", "/faults", None),
        ("post", "/devices/spoke-1/config", {"patch": [{"op": "delete", "path": "l2vpn/xconnect[SVC-0001]"}]}),
        ("post", "/devices/spoke-1/apply-intent", {}),
        ("post", "/scenarios/healthy/apply", None),
        ("post", "/reset", {}),
        ("post", "/sync", {}),
    ],
)
def test_every_write_needs_the_token(client, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(client, method)(path, headers={"X-Lab-Token": "wrong"}, **kwargs).status_code == 401


def test_write_is_audited_with_actor(client):
    r = client.post(
        "/faults", json={"kind": "uplink", "host": "hub-a"}, headers={"X-Lab-Token": TOKEN, "X-Lab-Actor": "grok-bot"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["done"] == "uplink down on hub-a"
    assert client.state.lab.core.calls == [("uplink", "hub-a", True)]
    [entry] = client.get("/audit").json()
    assert (
        entry["actor"] == "grok-bot"
        and entry["action"] == "fault.inject"
        and entry["target"] == "hub-a"
        and entry["ok"]
    )


def test_failed_write_is_audited_too(client):
    r = client.post("/faults", json={"kind": "uplink", "host": "nope"}, headers={"X-Lab-Token": TOKEN})
    assert r.status_code == 404
    [entry] = client.get("/audit").json()
    assert not entry["ok"] and entry["actor"] == "unknown"


def test_unconfigured_token_refuses_writes(tmp_path):
    settings = replace(load_settings(environ={}), lab_api_token=None, data_dir=tmp_path)
    state = AppState(settings=settings, lab=FakeLab(settings), sync_fn=lambda: {}, settings_loader=lambda: settings)
    with TestClient(create_app(state)) as c:
        assert c.post("/sync", json={}, headers={"X-Lab-Token": "x"}).status_code == 503
    state.syncer.stop()


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def test_netbox_webhook_signature_and_debounced_sync(client):
    body = json.dumps(
        {"event": "created", "object_type": "vpn.l2vpn", "data": {"id": 7, "display": "SVC-0007-primary"}}
    ).encode()
    assert client.post("/hooks/netbox", content=body).status_code == 401
    assert (
        client.post("/hooks/netbox", content=body, headers={"X-Hook-Signature": sign(body, "other")}).status_code == 401
    )
    r = client.post("/hooks/netbox", content=body, headers={"X-Hook-Signature": sign(body)})
    assert r.status_code == 202 and r.json()["sync"]["pending"]
    assert client.state.lab.forgotten == 1
    assert client.get("/hooks/netbox").json()[-1]["object"] == "SVC-0007-primary"
    client.state.syncer.wait(1, timeout=5)
    assert client.syncs == [1]
    actions = [e["action"] for e in client.get("/audit").json()]
    assert actions.count("hook.netbox") == 3 and "sync.run" in actions


def test_sync_request_waits_for_its_run(client):
    r = client.post(
        "/sync", json={"wait": True, "reason": "test"}, headers={"X-Lab-Token": TOKEN, "X-Lab-Actor": "tester"}
    )
    assert r.status_code == 200 and r.json()["runs"] == 1 and r.json()["last"]["reasons"] == ["tester: test"]


def test_openapi_file_is_current():
    path = repo_root() / "lab" / "openapi.json"
    assert path.read_text(encoding="utf-8") == openapi_text(), "run `make lab-openapi`"


def test_openapi_describes_every_operation():
    spec = json.loads(openapi_text())
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert op.get("summary"), f"{method.upper()} {path} has no summary"
    writes = [
        (p, m) for p, ops in spec["paths"].items() for m in ops if m in ("post", "delete") and p != "/hooks/netbox"
    ]
    for path, method in writes:
        params = {p["name"] for p in spec["paths"][path][method].get("parameters", [])}
        assert "x-lab-token" in params, f"{method.upper()} {path} does not declare X-Lab-Token"
