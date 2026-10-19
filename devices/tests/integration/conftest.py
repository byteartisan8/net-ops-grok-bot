"""Isolated container stack for the device integration tests (owner: 02-device-simulator).

``core`` + ``hub-a`` + ``spoke-1`` + a trap receiver (``netsim-trapsink``), rendered from the topology by
``netsim_simd.devtool.render_compose`` onto a private network (default 172.30.99.0/24, same last octets as
the lab) with ephemeral host ports, so it never collides with a running lab. Run with:

    make devices-test-integration          # = uv run pytest devices/tests/integration -m integration

``NETSIM_IT_SUBNET`` changes the subnet; ``NETSIM_IT_KEEP=1`` leaves the stack up for debugging.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from netsim_simd import snmpclient
from netsim_simd.devtool import RenderOptions, render_compose

from netsim_common.oids import OidRegistry, load_oids
from netsim_common.paths import repo_root
from netsim_common.topology import load_topology

PROJECT = "netsim-it"
PREFIX = "netsim-it-"
HOSTS = ("hub-a", "spoke-1")
SUBNET = os.environ.get("NETSIM_IT_SUBNET", "172.30.99.0/24")
COMMUNITY = "netsim"
TIMINGS: dict[str, dict[str, Any]] = {}


def record(path: str, budget: float | None, measured: float) -> None:
    """Keep the worst measurement per path (printed by test_zz_timing_report)."""
    old = TIMINGS.get(path)
    if old is None or measured > old["measured"]:
        TIMINGS[path] = {"budget": budget, "measured": measured}


def _docker(
    *args: str, check: bool = True, input: str | None = None, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, input=input, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


@dataclass
class Lab:
    compose_file: Path
    oids: OidRegistry
    ports: dict[tuple[str, str], int] = field(default_factory=dict)

    # ------------------------------------------------------------------ plumbing
    def compose(self, *args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return _docker("compose", "-p", PROJECT, "-f", str(self.compose_file), *args, check=check, timeout=timeout)

    @staticmethod
    def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _docker(*args, check=check)

    def discover_ports(self) -> None:
        for host in HOSTS:
            for container_port in ("8080/tcp", "9000/tcp", "161/udp"):
                self.ports[(host, container_port)] = self._port(host, container_port)
        self.ports[("trapsink", "9162/tcp")] = self._port("trapsink", "9162/tcp")

    def _port(self, service: str, container_port: str) -> int:
        port, proto = container_port.split("/")
        out = self.compose("port", "--protocol", proto, service, port).stdout.strip()
        return int(out.rsplit(":", 1)[1])

    def container(self, host: str) -> str:
        return PREFIX + host

    def restconf(self, host: str) -> str:
        return f"http://127.0.0.1:{self.ports[(host, '8080/tcp')]}"

    def simd(self, host: str) -> str:
        return f"http://127.0.0.1:{self.ports[(host, '9000/tcp')]}"

    def sink(self) -> str:
        return f"http://127.0.0.1:{self.ports[('trapsink', '9162/tcp')]}"

    # ------------------------------------------------------------------ device access
    def state(self, host: str) -> dict[str, Any]:
        return httpx.get(f"{self.simd(host)}/state", timeout=3).json()

    def pw(self, host: str, service: str, role: str) -> dict[str, Any]:
        return next(p for p in self.state(host)["pseudowires"] if p["service_id"] == service and p["role"] == role)

    def events(self, host: str, since: float | None = None) -> list[dict[str, Any]]:
        params = {"since": since} if since is not None else {}
        return httpx.get(f"{self.simd(host)}/events", params=params, timeout=3).json()["events"]

    def snmp_walk(self, host: str, root: str) -> list[snmpclient.VarBind]:
        return asyncio.run(snmpclient.walk("127.0.0.1", self.ports[(host, "161/udp")], COMMUNITY, root))

    def snmp_get(self, host: str, *oids: str) -> list[snmpclient.VarBind]:
        return asyncio.run(snmpclient.get("127.0.0.1", self.ports[(host, "161/udp")], COMMUNITY, *oids))

    def pw_rows(self, host: str) -> dict[str, dict[str, Any]]:
        """cpwVcTable by cpwVcName: {name: {"index", column: value...}}."""
        rows: dict[int, dict[str, Any]] = {}
        for vb in self.snmp_walk(host, self.oids.oid("cpwVcTable")):
            name = self.oids.name_of(vb.oid)
            index = int(vb.oid.rsplit(".", 1)[1])
            rows.setdefault(index, {"index": index})[name] = vb.rendered
        return {r["cpwVcName"]: r for r in rows.values()}

    def snmp_oper(self, host: str, index: int) -> int:
        [vb] = self.snmp_get(host, self.oids.instance("cpwVcOperStatus", index))
        return vb.rendered

    def restconf_put(self, host: str, path: str, body: dict[str, Any]) -> None:
        resp = httpx.put(
            f"{self.restconf(host)}/restconf/data/{path}",
            json=body,
            headers={"Content-Type": "application/yang-data+json"},
            timeout=5,
        )
        assert resp.status_code in (200, 201, 204), resp.text

    def restconf_delete(self, host: str, path: str) -> None:
        resp = httpx.delete(f"{self.restconf(host)}/restconf/data/{path}", timeout=5)
        assert resp.status_code in (200, 204), resp.text

    def restconf_get(self, host: str, path: str) -> dict[str, Any]:
        resp = httpx.get(
            f"{self.restconf(host)}/restconf/data/{path}", headers={"Accept": "application/yang-data+json"}, timeout=5
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def cli(self, host: str, *commands: str) -> str:
        out = _docker("exec", "-i", self.container(host), "clixon_cli", input="\n".join(commands) + "\n")
        return out.stdout + out.stderr

    def core(self, *args: str) -> str:
        return _docker("exec", self.container("core"), "valkey-cli", *args).stdout.strip()

    def traps(self, since: float | None = None) -> list[dict[str, Any]]:
        params = {"since": since} if since is not None else {}
        return httpx.get(f"{self.sink()}/traps", params=params, timeout=3).json()["traps"]

    def mgmt_ip(self, host: str) -> str:
        last = load_topology().device(host).mgmt_ip.rsplit(".", 1)[1]
        return SUBNET.rsplit(".", 1)[0] + "." + last

    @staticmethod
    def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = 0.05, what: str = "") -> float:
        return wait_until(predicate, timeout, interval, what)

    @staticmethod
    def record(path: str, budget: float | None, measured: float) -> None:
        record(path, budget, measured)

    def wait_healthy(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = {
                h: _docker("inspect", "-f", "{{.State.Health.Status}}", self.container(h)).stdout.strip() for h in HOSTS
            }
            if all(s == "healthy" for s in states.values()):
                return
            time.sleep(0.5)
        raise AssertionError(f"devices not healthy: {states}")


def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = 0.05, what: str = "") -> float:
    """Poll until ``predicate()`` is truthy; return the elapsed seconds (AssertionError on timeout)."""
    start = time.monotonic()
    last_exc: Exception | None = None
    while True:
        try:
            if predicate():
                return time.monotonic() - start
        except Exception as exc:  # device restarting, etc.
            last_exc = exc
        if time.monotonic() - start > timeout:
            raise AssertionError(
                f"timed out after {timeout}s waiting for {what or predicate}" + (f": {last_exc}" if last_exc else "")
            )
        time.sleep(interval)


@pytest.fixture(scope="session")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("needs a Docker daemon")
    topo = load_topology()
    sink_ip = SUBNET.rsplit(".", 1)[0] + "." + topo.infra("zabbix-snmptraps").mgmt_ip.rsplit(".", 1)[1]
    text = render_compose(
        topo,
        RenderOptions(
            hosts=HOSTS,
            network=f"{PROJECT}-net",
            external_network=False,
            subnet=SUBNET,
            container_prefix=PREFIX,
            publish="ephemeral",
            trapsink=True,
            trap_targets=f"{sink_ip}:1162",
            build_context=str(repo_root()),
            header="# netsim-pe integration test stack (generated by devices/tests/integration/conftest.py)\n",
        ),
    )
    compose_file = tmp_path_factory.mktemp("it") / "compose.yaml"
    compose_file.write_text(text, encoding="utf-8")
    lab = Lab(compose_file, load_oids())
    lab.compose("down", "--volumes", "--remove-orphans", check=False)
    start = time.monotonic()
    lab.compose("up", "-d", "--wait", "--wait-timeout", "120", timeout=900)
    lab.discover_ports()
    lab.wait_healthy()
    record("stack up -> devices healthy (2 devices)", None, time.monotonic() - start)
    try:
        yield lab
    finally:
        if os.environ.get("NETSIM_IT_KEEP") != "1":
            lab.compose("down", "--volumes", "--remove-orphans", check=False)
        else:
            print(f"\nNETSIM_IT_KEEP=1: stack left running ({compose_file})")


@pytest.fixture(scope="session")
def timings() -> dict[str, dict[str, Any]]:
    return TIMINGS


def dump_timings(path: Path) -> None:
    path.write_text(json.dumps(TIMINGS, indent=2) + "\n", encoding="utf-8")
