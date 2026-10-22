"""lab-api's direct reads and acknowledgments (no containers): Zabbix allow-list, NetBox GET passthrough, RESTCONF
GET, acknowledging as a NOC role. Nothing that writes gets through a read endpoint."""

from __future__ import annotations

import json
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

import netsim_lab.api.app as lab_app
from netsim_lab.api.app import AppState, create_app
from netsim_lab.settings import load_settings
from netsim_lab.zabbix import READ_METHODS, LabZabbix
from netsim_zabbix.api import ZabbixApiError

TOKEN = "t0ken-for-tests"
HEADERS = {"X-Lab-Token": TOKEN, "X-Lab-Actor": "grok-bot/NOCENG"}


class FakeZabbixApi:
    """The lab's Zabbix as the read and acknowledge paths see it: two customer service events and their trigger
    problems (only the PW leaves list problem events, as on the running 8.0.0rc1 server)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.acked: list[dict[str, Any]] = []
        self.acknowledged = {"301": "0", "302": "1", "303": "0"}
        self.resolved: set[str] = set()

    def call(self, method: str, params: Any = None, *, auth: bool = True) -> Any:
        self.calls.append((method, params))
        if method == "host.get":
            return [{"hostid": "10501", "host": "spoke-1"}, {"hostid": "10502", "host": "hub-a"}]
        if method == "problem.get" and "eventids" in params:
            return [
                {"eventid": e, "name": f"problem {e}", "acknowledged": self.acknowledged[e], "severity": "4"}
                for e in params["eventids"]
                if e in self.acknowledged and e not in self.resolved
            ]
        if method == "problem.get":
            return [{"eventid": "301", "name": "SVC-0001 primary on spoke-1: PW down"}]
        if method == "event.get":
            if params.get("source") == 4:
                services = {"901": "2", "902": "3"}
                return [
                    {"eventid": e, "objectid": services[e], "value": "1"} for e in params["eventids"] if e in services
                ]
            triggers = {"301": "1", "302": "1", "303": "1", "399": "0"}
            return [{"eventid": e, "value": triggers[e]} for e in params["eventids"] if e in triggers]
        if method == "service.get":
            if "parentids" in params:  # the subtree: services and PW leaves
                return {
                    "2": [
                        {"serviceid": "20", "problem_events": []},
                        {"serviceid": "21", "problem_events": [{"eventid": "301"}, {"eventid": "302"}]},
                    ],
                    "3": [{"serviceid": "30", "problem_events": []}],
                }[params["parentids"][0]]
            return [{"serviceid": params["serviceids"][0], "problem_events": []}]  # a customer node lists none
        if method == "event.acknowledge":
            self.acked.append(params)
            return {"eventids": params["eventids"]}
        if method == "user.checkAuthentication":
            return {"userid": "4", "username": "noc-engineer"}
        if method in ("sla.getsli",):
            return {"periods": [], "serviceids": [], "sli": []}
        if method == "item.get" and params.get("output") == "boom":
            raise ZabbixApiError(method, -32602, "Invalid params.", 'unexpected parameter "output"')
        return [{"method": method, "params": params}]

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeZabbixApi:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeDevice:
    def __init__(self, host: str) -> None:
        self.host = host
        self.gets: list[tuple[str, str]] = []

    def restconf_get(self, path: str, content: str = "config") -> tuple[int, Any]:
        self.gets.append((path, content))
        if path.startswith("netsim-device:nothing"):
            return 404, None
        return 200, {"netsim-device:interface": [{"name": "GigabitEthernet0/0/1.101"}]}


class FakeDevices(dict):
    def __missing__(self, host: str) -> FakeDevice:
        self[host] = FakeDevice(host)
        return self[host]


class FakeLab:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.api = FakeZabbixApi()
        self.devices = FakeDevices()
        self._zabbix = LabZabbix(self.api)  # type: ignore[arg-type]

    def zabbix(self) -> LabZabbix:
        return self._zabbix

    def forget_intent(self) -> None:
        pass

    def stopped(self):
        return []


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = replace(load_settings(environ={}), lab_api_token=TOKEN, webhook_secret="s", data_dir=tmp_path)
    state = AppState(settings=settings, lab=FakeLab(settings), sync_fn=lambda: {}, settings_loader=lambda: settings)
    env = tmp_path / ".env"  # never the repository's .env: the role tokens and URLs are the test's
    env.write_text(
        "ZABBIX_URL=http://127.0.0.1:8080\nZABBIX_NOCENG_TOKEN=noceng-zabbix-token\nZABBIX_NETENG_TOKEN=\n"
        "NETBOX_URL=http://netbox.test:8080\nNETBOX_TOKEN_RO=nbt_abc123def456.0123456789\n",
        encoding="utf-8",
    )
    for name in ("ZABBIX_URL", "ZABBIX_NOCENG_TOKEN", "ZABBIX_NETENG_TOKEN", "NETBOX_URL", "NETBOX_TOKEN_RO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("netsim_zabbix.settings.default_env_file", lambda: env)
    monkeypatch.setattr("netsim_netbox.settings.default_env_file", lambda: env)
    role_apis: list[tuple[str, str]] = []

    def fake_api(url: str, token: str, **kw: Any) -> FakeZabbixApi:
        role_apis.append((url, token))
        return state.lab.api  # type: ignore[attr-defined]

    monkeypatch.setattr("netsim_zabbix.api.ZabbixApi", fake_api)
    with TestClient(create_app(state)) as c:
        c.state, c.role_apis = state, role_apis  # type: ignore[attr-defined]
        yield c
    state.syncer.stop()


# ------------------------------------------------------------------------------------------------ Zabbix reads
def test_zabbix_read_passes_an_allowed_method_with_a_default_limit(client) -> None:
    r = client.post("/zabbix/api", json={"method": "problem.get", "params": {"output": ["eventid"], "severities": [4]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "problem.get" and body["size"] > 0
    method, params = client.state.lab.api.calls[-1]
    assert method == "problem.get" and params == {"output": ["eventid"], "severities": [4], "limit": 1000}
    client.post("/zabbix/api", json={"method": "history.get", "params": {"limit": 99999}})
    assert client.state.lab.api.calls[-1][1]["limit"] == 10000
    client.post("/zabbix/api", json={"method": "sla.getsli", "params": {"slaid": "1"}})
    assert client.state.lab.api.calls[-1] == ("sla.getsli", {"slaid": "1"})  # not a *.get: no limit added


@pytest.mark.parametrize(
    "method",
    [
        "event.acknowledge",
        "host.create",
        "host.update",
        "host.delete",
        "configuration.import",
        "task.create",
        "script.execute",
        "user.get",
        "token.generate",
        "service.update",
        "usermacro.updateglobal",
        "action.create",
        "problem.get ",
    ],
)
def test_zabbix_read_refuses_everything_else(client, method: str) -> None:
    before = len(client.state.lab.api.calls)
    r = client.post("/zabbix/api", json={"method": method, "params": {}})
    assert r.status_code == 403 and "is not a read the NOC may make" in r.text
    assert len(client.state.lab.api.calls) == before  # nothing reached Zabbix


def test_zabbix_read_needs_an_object_and_reports_zabbix_errors(client) -> None:
    assert client.post("/zabbix/api", json={"method": "problem.get", "params": [1]}).status_code == 422
    r = client.post("/zabbix/api", json={"method": "item.get", "params": {"output": "boom"}})
    assert r.status_code == 400 and "unexpected parameter" in r.text
    assert set(READ_METHODS) >= {"problem.get", "service.get", "alert.get", "sla.getsli"}


def test_reads_are_size_capped(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_app, "READ_MAX_BYTES", 40)
    r = client.post("/zabbix/api", json={"method": "host.get", "params": {}})
    assert r.status_code == 413 and "narrow it" in r.json()["detail"]["message"]


# ------------------------------------------------------------------------------------------------ acknowledgments
def test_acknowledge_needs_the_lab_token_and_a_known_role(client) -> None:
    body = {"eventids": ["301"], "message": "on it", "role": "noc-engineer"}
    assert client.post("/zabbix/acknowledge", json=body).status_code == 401
    assert client.post("/zabbix/acknowledge", json=body, headers={"X-Lab-Token": "wrong"}).status_code == 401
    bad = client.post("/zabbix/acknowledge", json={**body, "role": "admin"}, headers=HEADERS)
    assert bad.status_code == 422
    missing = client.post("/zabbix/acknowledge", json={**body, "role": "network-engineer"}, headers=HEADERS)
    assert missing.status_code == 503 and "ZABBIX_NETENG_TOKEN" in missing.text
    assert client.state.lab.api.acked == []


def test_acknowledge_a_customer_alert_as_the_noc_engineer(client) -> None:
    r = client.post(
        "/zabbix/acknowledge",
        json={
            "eventids": ["901", "303", "399", "777"],
            "message": "NOCENG: hub-a power-off, field ticket",
            "role": "noc-engineer",
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["zabbix_user"] == {"userid": "4", "username": "noc-engineer"}
    assert client.role_apis == [("http://127.0.0.1:8080", "noceng-zabbix-token")]  # the role's own token
    # the service event stands for the trigger problems of its subtree (the leaves); 302 was acknowledged already
    assert out["resolved"] == {"901": ["301", "302"]}
    assert out["acknowledged"] == ["301", "303"] and out["commented"] == ["302"]
    acked = client.state.lab.api.acked
    assert acked == [
        {"eventids": ["301", "303"], "action": 6, "message": "NOCENG: hub-a power-off, field ticket"},
        {"eventids": ["302"], "action": 4, "message": "NOCENG: hub-a power-off, field ticket"},
    ]
    assert {s["eventid"] for s in out["skipped"]} == {"399", "777"}  # an OK event, an unknown id
    [entry] = [e for e in client.get("/audit").json() if e["action"] == "zabbix.acknowledge"]
    assert entry["actor"] == "grok-bot/NOCENG" and entry["target"] == "noc-engineer" and entry["ok"]
    assert entry["result"]["acknowledged"] == ["301", "303"]


def test_acknowledge_skips_what_recovered(client) -> None:
    client.state.lab.api.resolved = {"301", "302"}
    out = client.post(
        "/zabbix/acknowledge",
        json={"eventids": ["901", "902"], "message": "m", "role": "noc-engineer"},
        headers=HEADERS,
    ).json()
    assert out["acknowledged"] == [] and out["commented"] == []
    assert {s["eventid"] for s in out["skipped"]} == {"902", "301", "302"}
    assert client.state.lab.api.acked == []


# ------------------------------------------------------------------------------------------------ NetBox
@pytest.fixture
def netbox(monkeypatch: pytest.MonkeyPatch):
    seen: list[dict[str, Any]] = []

    def fake_get(url: str, params=None, headers=None, timeout=None) -> httpx.Response:
        seen.append({"url": url, "params": list(params or []), "headers": headers})
        if "/missing/" in url:
            return httpx.Response(404, json={"detail": "Not found."})
        if "/broken/" in url:
            return httpx.Response(500, text="oops")
        nxt = "http://netbox.test:8080/api/dcim/devices/?limit=2&offset=2"
        return httpx.Response(200, json={"count": 4, "next": nxt, "previous": None, "results": [{"name": "hub-a"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_netbox_passthrough_is_a_get_with_the_read_only_token(client, netbox) -> None:
    r = client.get("/netbox/dcim/devices/", params=[("role", "hub"), ("role", "spoke"), ("limit", "2")])
    assert r.status_code == 200, r.text
    [call] = netbox
    assert call["url"] == "http://netbox.test:8080/api/dcim/devices/"
    assert call["params"] == [("role", "hub"), ("role", "spoke"), ("limit", "2")]
    assert call["headers"]["Authorization"] == "Bearer nbt_abc123def456.0123456789"
    data = r.json()["data"]
    assert data["next"] == "/netbox/dcim/devices/?limit=2&offset=2"  # page links point back at lab-api
    assert client.get("/netbox/").json()["path"] == "" and netbox[-1]["url"] == "http://netbox.test:8080/api/"


def test_netbox_passthrough_refuses_writes_and_secrets(client, netbox) -> None:
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/netbox/dcim/devices/").status_code == 405
    assert client.get("/netbox/users/tokens/").status_code == 403
    assert client.get("/netbox/dcim/devices/%2E%2E/x").status_code in (403, 404, 422)
    assert client.get("/netbox/dcim/devices/;rm").status_code == 422
    assert netbox == []  # nothing reached NetBox
    assert client.get("/netbox/missing/").status_code == 404
    assert client.get("/netbox/broken/").status_code == 502


# ------------------------------------------------------------------------------------------------ RESTCONF
def test_restconf_get_keeps_the_percent_encoded_keys(client) -> None:
    path = "netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F1.101"
    r = client.get(f"/devices/spoke-1/restconf/{path}", params={"content": "all"})
    assert r.status_code == 200, r.text
    assert client.state.lab.devices["spoke-1"].gets == [(path, "all")]
    assert r.json()["data"]["netsim-device:interface"][0]["name"] == "GigabitEthernet0/0/1.101"
    assert client.get("/devices/spoke-1/restconf").status_code == 200
    assert client.state.lab.devices["spoke-1"].gets[-1] == ("", "config")  # the default content


def test_restconf_get_is_read_only_and_checks_its_input(client) -> None:
    assert client.get("/devices/spoke-1/restconf/netsim-device:nothing").status_code == 404
    assert client.get("/devices/nope/restconf/netsim-device:l2vpn").status_code == 404
    assert client.get("/devices/spoke-1/restconf/netsim-device:l2vpn", params={"content": "x"}).status_code == 422
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/devices/spoke-1/restconf/netsim-device:l2vpn").status_code == 405
    assert client.get("/devices/spoke-1/restconf/netsim-device:l2vpn;x").status_code == 422


def test_the_openapi_names_the_new_endpoints() -> None:
    from netsim_lab.api.server import openapi_text

    spec = json.loads(openapi_text())
    assert {"/zabbix/api", "/zabbix/acknowledge", "/netbox/{path}", "/devices/{host}/restconf/{path}"} <= set(
        spec["paths"]
    )
    ack = spec["paths"]["/zabbix/acknowledge"]["post"]
    assert "x-lab-token" in {p["name"] for p in ack["parameters"]}


def test_the_host_map_is_not_kept_before_zabbix_is_provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    """lab-api is asked about Zabbix before ``make seed`` creates the hosts: that empty answer must not stick (it
    once made every reset of a fresh lab wait 240 s for polls of hosts it could not see)."""
    import netsim_lab.zabbix as lz

    answers = [
        [],
        [{"hostid": "1", "host": "hub-a"}],
        [{"hostid": "1", "host": "hub-a"}, {"hostid": "2", "host": "hub-b"}],
    ]

    class Api:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, method: str, params: Any = None, **kw: Any) -> Any:
            assert method == "host.get"
            self.calls += 1
            return answers[min(self.calls, len(answers)) - 1]

    clock = [100.0]
    monkeypatch.setattr(lz.time, "monotonic", lambda: clock[0])
    api = Api()
    z = LabZabbix(api)  # type: ignore[arg-type]
    assert z.hostids() == {}
    assert z.hostids() == {"hub-a": "1"} and api.calls == 2  # the empty map was not kept
    clock[0] += 30
    assert z.hostids() == {"hub-a": "1"} and api.calls == 2  # a map is kept for a while
    clock[0] += lz.HOSTIDS_TTL_S
    assert z.hostids() == {"hub-a": "1", "hub-b": "2"} and api.calls == 3  # then asked again
