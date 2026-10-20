"""Problem correlation and the debounced sync runner."""

from __future__ import annotations

import threading
import time

from netsim_lab.correlate import correlate
from netsim_lab.syncer import SyncRunner


def problem(eventid, service, check, host, clock, scope="l2vpn-pw", severity="High"):
    return {
        "eventid": eventid,
        "service": service,
        "check": check,
        "host": host,
        "clock": clock,
        "scope": scope,
        "severity": severity,
        "name": f"{service} {check} on {host}",
        "pw_role": "primary",
    }


def test_fake_redundancy_problems_form_one_group():
    problems = [
        problem("1", "SVC-0002", "missing", "hub-a", 100.0),
        problem("2", "SVC-0002", "spec", "spoke-2", 101.5, severity="Average"),
        problem("3", "SVC-0002", "unexpected", "hub-b", 130.0, scope="l2vpn-unexpected", severity="Warning"),
        problem("4", "SVC-0002", "trap", "hub-b", 100.0, scope="l2vpn-trap", severity="Information"),
        problem("5", "SVC-0001", "oper", "spoke-1", 900.0),
    ]
    groups = correlate(problems, window_s=60)
    by_service = {g["service"]: g for g in groups}
    g = by_service["SVC-0002"]
    assert g["checks"] == ["missing", "spec", "unexpected"] and g["hosts"] == ["hub-a", "hub-b", "spoke-2"]
    assert g["severity"] == "High" and g["spread_s"] == 30.0 and "one root cause" in g["summary"]
    assert by_service["SVC-0001"]["problems"] == 1


def test_problems_far_apart_are_separate_groups():
    groups = correlate(
        [problem("1", "SVC-0001", "oper", "a", 0.0), problem("2", "SVC-0001", "oper", "b", 500.0)], window_s=60
    )
    assert len(groups) == 2


def test_sync_runner_coalesces_bursts():
    calls = []
    gate = threading.Event()

    def run():
        calls.append(time.monotonic())
        gate.wait(2)
        return {"summary": "0 changes"}

    runner = SyncRunner(run, debounce_s=0.2)
    try:
        for _ in range(20):
            runner.request("netbox event")
        time.sleep(0.5)
        assert len(calls) == 1  # a burst of webhooks -> one run
        runner.request("during run")  # arrives while running -> exactly one more run
        gate.set()
        assert runner.wait(2, timeout=3)
        assert len(calls) == 2
        status = runner.run_now("manual", wait=True, timeout=3)
        assert status["runs"] == 3 and status["last"]["ok"] and status["last"]["reasons"] == ["manual"]
    finally:
        runner.stop()


def test_sync_runner_records_errors():
    def boom():
        raise RuntimeError("zabbix down")

    runner = SyncRunner(boom, debounce_s=0.0)
    try:
        status = runner.run_now("x", wait=True, timeout=3)
        assert status["last"]["ok"] is False and "zabbix down" in status["last"]["error"]
    finally:
        runner.stop()
