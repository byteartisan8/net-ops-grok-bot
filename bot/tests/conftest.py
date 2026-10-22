"""Shared fakes for the office's unit tests: recorded lab-api responses, a scriptable lab-api, Zabbix alert bodies as
the media type posts them, and a capture of the pages sent to the routines.

``bot/tests/data/<state>.json`` hold real lab-api responses recorded from the running lab on 2026-10-20 (healthy,
fake-redundancy with its three apply-intent dry runs, hub-down), keyed ``"GET /path"`` / ``"POST /path"``. The root
causes below are what Zabbix 8.0.0rc1 sent for a hub-a power-off on 2026-10-21 (``{SERVICE.ROOTCAUSE}``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from netsim_bot.labapi import LabApi
from netsim_bot.settings import BotSettings, load_settings

DATA = Path(__file__).parent / "data"
TIERS = {
    "Customer-A": "gold",
    "Customer-B": "gold",
    "Customer-C": "silver",
    "Customer-D": "silver",
    "Customer-E": "gold",
    "Customer-F": "bronze",
}
LOOPBACKS = {
    "hub-a": "10.255.0.1",
    "hub-b": "10.255.0.2",
    "hub-c": "10.255.0.3",
    "hub-d": "10.255.0.4",
    **{f"spoke-{n}": f"10.255.1.{n}" for n in range(1, 7)},
}


def recorded(state: str) -> dict[str, Any]:
    return json.loads((DATA / f"{state}.json").read_text(encoding="utf-8"))


def problem_line(host: str, service: str, role: str, peer_host: str, check: str = "oper", customer: str = "") -> str:
    """One line of ``{SERVICE.ROOTCAUSE}`` as Zabbix writes it for a PW problem."""
    peer = LOOPBACKS[peer_host]
    name = f"{service} {role} on {host}: PW down (2), VC ID 1001 to {peer} ({peer_host})"
    tags = (
        f"check:{check}, class:network, component:pw, customer:{customer}, host:{host}, managed_by:netsim-zabbix, "
        f"peer:{peer}, pw_role:{role}, role:spoke, scope:l2vpn-pw, service:{service}, target:netsim-pe"
    )
    return f'Host: "{host}" Problem name: "{name}" Severity: "High" Age: 2s Problem tags: "{tags}"'


#: What a hub-a power-off alerts (customer -> the PW problems behind it: on the spokes, peer hub-a).
HUB_A_DOWN: dict[str, list[tuple[str, str, str, str]]] = {
    "Customer-A": [("spoke-1", "SVC-0001", "primary", "hub-a"), ("spoke-2", "SVC-0002", "primary", "hub-a")],
    "Customer-B": [("spoke-2", "SVC-0003", "primary", "hub-a"), ("spoke-4", "SVC-0006", "primary", "hub-a")],
    "Customer-C": [("spoke-3", "SVC-0004", "primary", "hub-a")],
    "Customer-D": [("spoke-4", "SVC-0005", "primary", "hub-a")],
    "Customer-E": [("spoke-1", "SVC-0008", "primary", "hub-a")],
    "Customer-F": [("spoke-4", "SVC-0010", "primary", "hub-a")],
}
HUB_A_SEVERITY = {c: ("Disaster" if c == "Customer-F" else "Warning") for c in HUB_A_DOWN}


def zabbix_body(
    customer: str,
    kind: str = "problem",
    severity: str = "Warning",
    event_id: str = "4683",
    role: str = "noc-engineer",
    problems: list[tuple[str, str, str, str]] | None = None,
    level: str = "customer",
    **extra: Any,
) -> dict[str, Any]:
    """The body the media type ``NetSim NOC (grok-bot)`` posts (``noc_webhook.js``)."""
    lines = [
        problem_line(*p, customer=customer)
        for p in (problems if problems is not None else HUB_A_DOWN.get(customer, []))
    ]
    tags = {"customer": customer, "managed_by": "netsim-zabbix", "sla_tier": TIERS.get(customer, "gold")}
    if level:
        tags["bsm_level"] = level
    body = {
        "source": "zabbix",
        "role": role,
        "kind": kind,
        "event_id": event_id,
        "recovery_event_id": None,
        "severity": "OK" if kind == "recovery" else severity,
        "event_severity": "Warning",
        "update_severity": severity if kind == "update" else None,
        "status": "RESOLVED" if kind == "recovery" else "PROBLEM",
        "event_time": 1792554326,
        "update_time": None,
        "recovery_time": None,
        "event_name": f'Status of service "{customer}" changed to {severity}',
        "service": {
            "id": str(2 + sorted(TIERS).index(customer)) if customer in TIERS else "99",
            "name": customer,
            "tags": tags,
        },
        "root_cause": "\n".join(lines) if kind != "recovery" else "",
        "action": "NetSim: customer alert",
        "subject": f"{customer}: {severity} (NetSim NOC page)",
    }
    body.update(extra)
    return body


def hub_a_storm(event_base: int = 4683) -> list[dict[str, Any]]:
    return [
        zabbix_body(c, severity=HUB_A_SEVERITY[c], event_id=str(event_base + i))
        for i, c in enumerate(sorted(HUB_A_DOWN))
    ]


class FakeLab:
    """lab-api as a dict of responses (``"GET /drift"`` -> body); every request is recorded.

    A value can be a callable ``(request) -> (status, body)`` for dynamic answers. Built in: the Zabbix reads
    (``POST /zabbix/api``: every customer OK unless ``customer_status`` says otherwise), acknowledgments and field
    repairs (``POST /faults``).
    """

    def __init__(self, *states: str) -> None:
        self.routes: dict[str, Any] = {}
        for s in states:
            self.load(s)
        self.requests: list[httpx.Request] = []
        self.customer_status: dict[str, int] = {}
        self.acks: list[dict[str, Any]] = []
        self.routes.setdefault("POST /zabbix/api", self._zabbix_api)
        self.routes.setdefault("POST /zabbix/acknowledge", self._ack)
        self.routes.setdefault("POST /faults", self._fault)

    def load(self, state: str) -> None:
        for key, value in recorded(state).items():
            if key.startswith("_"):
                continue
            self.routes[key.split("?")[0]] = value

    def set(self, key: str, value: Any) -> None:
        self.routes[key] = value

    def _zabbix_api(self, request: httpx.Request) -> tuple[int, Any]:
        body = json.loads(request.content)
        if body["method"] == "service.get":
            names = (body["params"].get("filter") or {}).get("name") or list(TIERS)
            result = [{"name": n, "status": str(self.customer_status.get(n, -1))} for n in names]
            return 200, {"method": "service.get", "result": result, "size": 10}
        return 200, {"method": body["method"], "result": [], "size": 2}

    def _ack(self, request: httpx.Request) -> tuple[int, Any]:
        body = json.loads(request.content)
        self.acks.append(
            {**body, "actor": request.headers.get("x-lab-actor"), "token": request.headers.get("x-lab-token")}
        )
        ids = [f"3{e}" for e in body["eventids"]]
        return 200, {
            "role": body["role"],
            "zabbix_user": {"userid": "4", "username": body["role"]},
            "acknowledged": ids,
            "commented": [],
            "resolved": {e: [f"3{e}"] for e in body["eventids"]},
            "skipped": [],
            "problems": [{"eventid": i, "name": f"problem {i}", "severity": "High"} for i in ids],
        }

    def _fault(self, request: httpx.Request) -> tuple[int, Any]:
        body = json.loads(request.content)
        verb = "powered on" if body["kind"] == "device" else "restored"
        return 200, {"done": f"{body['host']} {verb}", "faults": [], "stopped": []}

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


class Routines:
    """The roles' routines as an httpx transport: records every page; ``status`` per URL path answers."""

    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []
        self.status: dict[str, int | list[int]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.pages.append(
            {
                "url": str(request.url),
                "path": path,
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content or b"{}"),
            }
        )
        status = self.status.get(path, 200)
        if isinstance(status, list):
            status = status.pop(0) if len(status) > 1 else status[0]
        return httpx.Response(status, json={"ok": status < 300})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def events(self, path: str | None = None) -> list[str]:
        return [p["body"].get("event") for p in self.pages if path is None or p["path"] == path]


@pytest.fixture
def fake_lab() -> Callable[..., FakeLab]:
    return FakeLab


@pytest.fixture
def routines() -> Routines:
    return Routines()


ROUTINE_ENV = {
    "BOT_ROUTINE_NOCENG_URL": "https://routines.test/noceng",
    "BOT_ROUTINE_NOCENG_KEY": "crsr_noceng",
    "BOT_ROUTINE_NETENG_URL": "https://routines.test/neteng",
    "BOT_ROUTINE_NETENG_KEY": "Bearer crsr_neteng",
    "BOT_ROUTINE_NOCMGR_URL": "https://routines.test/nocmgr",
    "BOT_ROUTINE_NOCMGR_KEY": "crsr_nocmgr",
}


def make_settings(tmp_path: Path, **env: str) -> BotSettings:
    base = {
        "BOT_DATA_DIR": str(tmp_path / "data"),
        "BOT_ALLOWED_HOSTS": "testserver,127.0.0.1",
        "BOT_OPERATOR_TOKEN": "op-token",
        "LAB_API_TOKEN": "lab-token",
        "BOT_ZABBIX_HOOK_TOKEN": "hook-token",
        "BOT_NOCENG_TOKEN": "noceng-token",
        "BOT_NETENG_TOKEN": "neteng-token",
        "BOT_NOCMGR_TOKEN": "nocmgr-token",
        "BOT_BATCH_S": "0",
        "BOT_MAX_BATCH_S": "0",
        "BOT_FIELD_DELAY_S": "0",
        **ROUTINE_ENV,
    }
    base.update(env)
    return load_settings(environ=base, env_file=tmp_path / "no.env")


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., BotSettings]:
    return lambda **env: make_settings(tmp_path, **env)


@pytest.fixture(name="recorded")
def recorded_fixture() -> Callable[[str], dict[str, Any]]:
    return recorded


@pytest.fixture
def kit() -> SimpleNamespace:
    """The helpers above for test modules (``--import-mode=importlib``: they cannot import this file)."""
    return SimpleNamespace(
        HUB_A_DOWN=HUB_A_DOWN,
        HUB_A_SEVERITY=HUB_A_SEVERITY,
        LOOPBACKS=LOOPBACKS,
        TIERS=TIERS,
        ROUTINE_ENV=ROUTINE_ENV,
        zabbix_body=zabbix_body,
        hub_a_storm=hub_a_storm,
        problem_line=problem_line,
        recorded=recorded,
    )
