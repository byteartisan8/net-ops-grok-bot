"""Contract 9.5 timings that need their own setup: the trap path and the polling-only variant."""

from __future__ import annotations

import os
import statistics
import subprocess
import time
from collections.abc import Callable
from typing import Any

from netsim_lab.scenarios import run_steps, steps_end, verify_phase
from netsim_zabbix import names as N

from netsim_common.paths import repo_root

CHECK_NOW_MEDIAN = 5.0  # contract 9.5
CHECK_NOW_MAX = 7.0  # contract change log (04-zabbix): the task manager's fixed 5 s grid
POLLING_ONLY_BUDGET = 70.0


def wait_until(predicate: Callable[[], Any], timeout: float, what: str, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")


def first_after(read: Callable[[], float], after: float, timeout: float, what: str) -> float:
    """Poll ``read()`` until it returns a value later than ``after``, and return that value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value > after:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")


def walk_clock(lab, host: str) -> float:
    return lab.zabbix().walk_clocks([host])[host]


def trap_clock(lab, host: str, notification: str) -> float:
    items = lab.zabbix().items("snmptrap[", [host])[host]
    item = next(i for i in items if f": {notification} " in i["name"])
    return item["clock"]


def test_trap_refreshes_the_walk_long_before_the_next_scheduled_poll(lab, timing, green):
    """Regular 60 s walk: right after a scheduled poll, cut spoke-3 <-> hub-b. The trap's check-now re-polls the walk
    within seconds, ~55 s before the next scheduled poll would have."""
    host = "spoke-3"
    before = walk_clock(lab, host)
    wait_until(lambda: walk_clock(lab, host) > before, 75, "a scheduled walk on spoke-3")
    scheduled = walk_clock(lab, host)
    t_fault = time.time()
    lab.core.set_cut("spoke-3", "hub-b")
    try:
        wait_until(lambda: walk_clock(lab, host) > scheduled, 20, "the walk after the trap")
        refreshed = walk_clock(lab, host)
        t_trap = trap_clock(lab, host, "cpwVcDown")
    finally:
        lab.core.set_cut("spoke-3", "hub-b", False)
    timing("trap path: fault -> trap item in Zabbix", t_trap - t_fault, None)
    timing("trap path: fault -> walk lastclock (check-now)", refreshed - t_fault, CHECK_NOW_MAX)
    timing("trap path: walk refreshed after the scheduled poll", refreshed - scheduled, 60.0, "next poll at +60 s")
    assert t_trap >= t_fault - 1
    assert refreshed - t_fault <= CHECK_NOW_MAX
    assert refreshed - scheduled < 50, "the refresh must come from the trap, well before the next 60 s poll"
    green("after the trap path test")


def test_trap_to_check_now_latency(lab, timing, green):
    """Trap item value -> walk lastclock, 8 traps. The two hosts' walk interval is 1 h during the test (host
    macro), so every walk value seen within seconds of a trap comes from the check-now (same method as 04). Each
    sample pairs the trial's own trap with the first walk after it, so no other walk can be mistaken for its re-poll."""
    hosts = ("spoke-3", "hub-b")
    z = lab.zabbix()
    ids = z.hostids()
    added = z.api.call(
        "usermacro.create", [{"hostid": ids[h], "macro": "{$NETSIM.WALK.INTERVAL}", "value": "1h"} for h in hosts]
    )["hostmacroids"]
    samples: list[float] = []
    try:
        time.sleep(7)  # configuration cache picks up the interval
        for trial in range(8):
            time.sleep(1.3 * (trial % 4))  # spread over the task manager's 5 s grid
            down = trial % 2 == 0
            notification = "cpwVcDown" if down else "cpwVcUp"
            t_toggle = time.time()
            lab.core.set_cut("spoke-3", "hub-b", down)
            trap = first_after(
                lambda n=notification: trap_clock(lab, "spoke-3", n), t_toggle, 10, f"the {notification} trap"
            )
            walk = first_after(lambda: walk_clock(lab, "spoke-3"), trap, 20, "the walk after the trap")
            samples.append(round(walk - trap, 2))
    finally:
        lab.core.set_cut("spoke-3", "hub-b", False)
        z.api.call("usermacro.delete", added)
    timing("trap -> walk re-polled (check-now), 8 traps", samples, CHECK_NOW_MAX, f"median budget {CHECK_NOW_MEDIAN}")
    assert statistics.median(samples) <= CHECK_NOW_MEDIAN, samples
    assert max(samples) <= CHECK_NOW_MAX, samples
    green("after the latency test")


def _recreate_devices(lab, traps: bool) -> None:
    env = {k: v for k, v in os.environ.items() if k != "TRAP_TARGETS"}
    if not traps:
        env["TRAP_TARGETS"] = ""  # set but empty: simd sends no traps (contract change log, 02)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", *lab.hosts],
        cwd=repo_root(),
        env=env,
        check=True,
        capture_output=True,
    )


def _trap_targets(host: str) -> str:
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", host],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return next((line.split("=", 1)[1] for line in out.splitlines() if line.startswith("TRAP_TARGETS=")), "<unset>")


def test_polling_only_fault_to_service_status(lab, scenarios, timing, green):
    """Every PE recreated with TRAP_TARGETS empty: Zabbix learns only from the 60 s walk. spoke-isolated must still
    reach its expected status within 70 s."""
    scenario = scenarios["spoke-isolated"]
    _recreate_devices(lab, traps=False)
    try:
        assert {_trap_targets(h) for h in lab.hosts} == {""}
        green("devices without traps")
        records = run_steps(lab, scenario.steps, "e2e:polling-only")
        t0 = steps_end(records)
        result = verify_phase(lab, scenario.main, t0, timeout=150)
        assert result["ok"], result["mismatches"]
        traps_since = [
            i for host in lab.hosts for i in lab.zabbix().items("snmptrap[", [host])[host] if i["clock"] >= t0
        ]
        timing("polling only: fault -> Zabbix service status", result["zabbix_services_s"], POLLING_ONLY_BUDGET)
        assert result["zabbix_services_s"] <= POLLING_ONLY_BUDGET
        assert traps_since == [], "a trap reached Zabbix although trap targets were disabled"
    finally:
        lab.core.clear_faults()
        _recreate_devices(lab, traps=True)
        assert {_trap_targets(h) for h in lab.hosts} == {"172.30.10.42:1162"}
        green("devices with traps again")
    assert N.WALK_KEY  # the walk is the only path in this test
