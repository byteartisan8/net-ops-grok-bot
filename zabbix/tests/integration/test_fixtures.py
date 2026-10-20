"""snmpsim fixtures in place of the devices (opt-in, disruptive: ``make zabbix-test-fixtures``).

Stops the device containers (``core`` and their volumes stay), runs every fixture variant on the device IPs,
checks what Zabbix makes of it, sends a trap from a fixture, then removes the fixtures and starts the devices again.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING, Any

import pytest
from netsim_zabbix import names as N
from netsim_zabbix.fixtures import down, trap, up

from netsim_common import load_topology

if TYPE_CHECKING:
    from conftest import Lab

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("NETSIM_ZABBIX_FIXTURES") != "1", reason="set NETSIM_ZABBIX_FIXTURES=1"),
]
OK, WARNING = -1, 2
HOSTS = load_topology().hostnames


def tags_of(p: dict[str, Any]) -> dict[str, str]:
    return {t["tag"]: t["value"] for t in p["tags"]}


def checks(lab: Lab) -> set[tuple[str, str, str, str]]:
    """(check, host, service-or-vcname, role) of every current L2VPN problem."""
    out = set()
    for p in lab.l2vpn_problems():
        t = tags_of(p)
        out.add((t.get("check", ""), t.get("host", ""), t.get("service", t.get("vcname", "")), t.get("pw_role", "")))
    return out


def settle(lab: Lab, want: set[tuple[str, str, str, str]], timeout: float = 120) -> None:
    """Poll now until every host has a fresh walk from the fixture and the problems are exactly ``want``.

    Polls repeatedly: the second walk feeds rows that the first one let discovery create.
    """
    since = time.time()

    def fresh() -> bool:
        walks = [lab.items(h, N.WALK_KEY)[N.WALK_KEY] for h in HOSTS]
        return all(int(w["lastclock"]) >= int(since) and w["state"] == "0" for w in walks)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lab.poll_now()
        for _ in range(12):
            if fresh() and checks(lab) == want:
                return
            time.sleep(1)
    pytest.fail(f"fresh walks: {fresh()}; problems {sorted(checks(lab))} != expected {sorted(want)}")


@pytest.fixture(scope="module")
def fixtures(lab: Lab):
    subprocess.run(["docker", "stop", *HOSTS], check=True, capture_output=True)
    try:
        yield
    finally:
        down()
        subprocess.run(["docker", "start", *HOSTS], check=True, capture_output=True)
        subprocess.run(["uv", "run", "netsim-devices", "wait", "--timeout", "120"], check=True, capture_output=True)


def test_fixture_variants(lab: Lab, fixtures: None) -> None:
    up("healthy")
    settle(lab, set())
    assert lab.service_status("L2VPN") == OK

    up("pw-down")
    settle(lab, {("oper", "spoke-1", "SVC-0001", "primary"), ("oper", "hub-a", "SVC-0001", "primary")})
    assert lab.service_status("SVC-0001 (spoke-1)") == WARNING

    up("pw-missing")
    settle(lab, {("missing", "spoke-3", "SVC-0004", "backup"), ("oper", "hub-b", "SVC-0004", "backup")})
    assert lab.service_status("SVC-0004 (spoke-3)") == WARNING

    up("wrong-peer")
    settle(
        lab,
        {
            ("spec", "spoke-2", "SVC-0002", "primary"),
            ("oper", "hub-a", "SVC-0002", "primary"),
            ("unexpected", "hub-b", "SVC-0002:primary", ""),
        },
    )
    [spec] = [p for p in lab.l2vpn_problems() if tags_of(p).get("check") == "spec"]
    assert spec["name"] == "SVC-0002 primary on spoke-2: peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)"
    assert lab.service_status("SVC-0002 (spoke-2)") == WARNING

    # a trap from a fixture container: matched by numeric OID, one problem event, check-now via the action
    up("healthy")
    settle(lab, set())
    t0 = time.time()
    trap("spoke-1", ["cpwVcDown", "--first", "1", "--status", "lowerLayerDown"])
    lab.wait_until(lambda: [p for p in lab.problems(trap="pw") if int(p["clock"]) >= int(t0)], 20, 0.3, "trap problem")
    hostid = lab.hostids()["spoke-1"]
    lab.wait_until(
        lambda: lab.api.call("alert.get", {"output": ["status"], "hostids": [hostid], "time_from": int(t0)}),
        20,
        0.3,
        "check-now script ran",
    )
    lab.wait_until(lambda: int(lab.items("spoke-1", N.WALK_KEY)[N.WALK_KEY]["lastclock"]) >= int(t0), 20, 0.3, "walk")
