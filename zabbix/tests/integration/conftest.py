"""Zabbix integration tests against the running lab (owner: 04-zabbix).

Needs the whole lab up and provisioned: devices (02), NetBox seeded (03), Zabbix (``make zabbix-up`` +
``make zabbix-provision``). Faults are injected the way lab-api will (fault/cut keys in ``core``, RESTCONF commits on
the devices, objects in NetBox); every test restores what it changed (golden configs, fault keys, NetBox objects).

    make zabbix-test-integration     # = uv run pytest zabbix/tests/integration -m integration -s

Timings are appended to ``build/zabbix-it/timings.json`` (the report's numbers).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import redis
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.settings import load_settings
from netsim_zabbix.sync import execute_now

from netsim_common.constants import lab_constants
from netsim_common.paths import contract_dir, repo_root
from netsim_common.topology import load_topology

STATE = repo_root() / "build" / "zabbix-it"
TIMINGS = STATE / "timings.json"
RESTCONF_JSON = "application/yang-data+json"


def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = 0.25, what: str = "") -> float:
    """Poll ``predicate`` until truthy; return the elapsed seconds (fail after ``timeout``)."""
    start = time.monotonic()
    last: Any = None
    while True:
        last = predicate()
        if last:
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            pytest.fail(f"timeout after {timeout:.0f} s waiting for {what or predicate}: last={last!r}")
        time.sleep(interval)


@dataclass
class Lab:
    api: ZabbixApi
    core: redis.Redis
    topology: Any = field(default_factory=load_topology)

    wait_until = staticmethod(wait_until)

    # ------------------------------------------------------------------ devices
    def restconf_url(self, host: str) -> str:
        return lab_constants().devices[host].restconf_url

    def restconf_get(self, host: str, path: str) -> dict[str, Any]:
        r = httpx.get(f"{self.restconf_url(host)}/restconf/data/{path}", headers={"Accept": RESTCONF_JSON})
        r.raise_for_status()
        return r.json()

    def restconf_put(self, host: str, path: str, body: dict[str, Any]) -> None:
        r = httpx.put(
            f"{self.restconf_url(host)}/restconf/data/{path}",
            json=body,
            headers={"Content-Type": RESTCONF_JSON, "Accept": RESTCONF_JSON},
        )
        assert r.status_code in (200, 201, 204), f"PUT {host} {path}: {r.status_code} {r.text}"

    def restconf_delete(self, host: str, path: str) -> None:
        r = httpx.delete(f"{self.restconf_url(host)}/restconf/data/{path}")
        assert r.status_code in (200, 204), f"DELETE {host} {path}: {r.status_code} {r.text}"

    def golden(self, host: str) -> dict[str, Any]:
        return json.loads((contract_dir() / "golden" / f"{host}.json").read_text(encoding="utf-8"))

    def restore_golden(self, host: str) -> None:
        """Whole running config back to golden in one commit (contract 6 / 02's report)."""
        self.restconf_put(host, "", {"ietf-restconf:data": self.golden(host)})

    @staticmethod
    def xconnect_path(service: str) -> str:
        return f"netsim-device:l2vpn/xconnect={quote(service, safe='')}"

    def clear_faults(self) -> None:
        keys = self.core.keys("netsim:fault:*") + self.core.keys("netsim:cut:*")
        if keys:
            self.core.delete(*keys)

    @staticmethod
    def cut_key(a: str, b: str) -> str:
        x, y = sorted((a, b))
        return f"netsim:cut:{x}|{y}"

    # ------------------------------------------------------------------ zabbix
    def hostids(self) -> dict[str, str]:
        return {
            h["host"]: h["hostid"]
            for h in self.api.call(
                "host.get",
                {
                    "output": ["hostid", "host"],
                    "tags": [{"tag": N.MANAGED_TAG, "value": N.MANAGED_VALUE, "operator": 1}],
                },
            )
        }

    def service_status(self, name: str) -> int:
        svc = self.api.get_one("service.get", {"output": ["status"], "filter": {"name": [name]}})
        assert svc, f"no service {name!r}"
        return int(svc["status"])

    def problems(self, **tags: str) -> list[dict[str, Any]]:
        return self.api.call(
            "problem.get",
            {
                "output": ["eventid", "name", "severity", "clock", "ns"],
                "selectTags": "extend",
                "tags": [{"tag": k, "value": v, "operator": 1} for k, v in tags.items()],
                "sortfield": ["eventid"],
            },
        )

    def items(self, host: str, key_prefix: str) -> dict[str, dict[str, Any]]:
        hostid = self.hostids()[host]
        return {
            i["key_"]: i
            for i in self.api.call(
                "item.get",
                {
                    "output": ["itemid", "key_", "name", "lastvalue", "lastclock", "lastns", "state"],
                    "hostids": [hostid],
                    "search": {"key_": key_prefix},
                    "startSearch": True,
                },
            )
        }

    def poll_now(self, hosts: list[str] | None = None) -> None:
        """Execute now the walk of ``hosts`` (default all): tests do not wait for the 60 s poll."""
        ids = self.hostids()
        wanted = [ids[h] for h in (hosts or list(ids))]
        walks = self.api.call("item.get", {"output": ["itemid"], "hostids": wanted, "filter": {"key_": [N.WALK_KEY]}})
        execute_now(self.api, [w["itemid"] for w in walks])

    def l2vpn_problems(self) -> list[dict[str, Any]]:
        return self.problems(scope=N.SCOPE_PW_TAG) + self.problems(scope=N.SCOPE_UNEXPECTED_TAG)


@pytest.fixture(scope="session")
def lab() -> Iterator[Lab]:
    settings = load_settings()
    if not settings.token:
        pytest.skip("ZABBIX_API_TOKEN not set: run `make zabbix-up` (bootstrap) first")
    core_ip = lab_constants().core_ip
    with settings.api() as api:
        try:
            api.version()
        except Exception as exc:  # pragma: no cover - environment
            pytest.skip(f"Zabbix not reachable at {settings.url}: {exc}")
        lab = Lab(api=api, core=redis.Redis(host=core_ip, port=6379, socket_timeout=5))
        lab.core.ping()
        yield lab
        lab.clear_faults()


class Timings(dict):
    """Measured timings of this run, written to ``build/zabbix-it/timings.json`` (the report's numbers)."""

    def record(self, name: str, value: float | list[float], budget: float | None = None) -> None:
        self[name] = {"measured": value, "budget": budget, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        print(f"\n  TIMING {name}: {value} (budget {budget})")


@pytest.fixture(scope="session")
def timings() -> Iterator[Timings]:
    data = Timings(json.loads(TIMINGS.read_text()) if TIMINGS.is_file() else {})
    yield data
    STATE.mkdir(parents=True, exist_ok=True)
    TIMINGS.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
