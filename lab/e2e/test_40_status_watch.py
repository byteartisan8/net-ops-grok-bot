"""``labctl status --watch``: intent, live state and Zabbix side by side, following a change within ~1-2 s."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time


def test_status_watch_follows_a_fault(lab, timing, green):
    proc = subprocess.Popen(
        [sys.executable, "-m", "netsim_lab.cli", "status", "--watch", "--plain", "--interval", "1", "--count", "60"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "COLUMNS": "200", "NO_COLOR": "1"},
    )
    lines: list[tuple[float, str]] = []

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append((time.time(), line.rstrip("\n")))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def first(pattern: tuple[str, ...], after: float, timeout: float) -> float:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for t, line in list(lines):
                if t >= after and all(p in line for p in pattern):
                    return t
            time.sleep(0.05)
        raise AssertionError(f"no line with {pattern} within {timeout} s; last lines: {[x[1] for x in lines[-12:]]}")

    try:
        first(("SVC-0004", "live up", "Zabbix OK"), 0, 30)  # running, all green
        t_fault = time.time()
        lab.core.set_cut("spoke-3", "hub-b")
        live = first(("SVC-0004", "live degraded"), t_fault, 10) - t_fault
        zabbix = first(("SVC-0004", "live degraded", "Zabbix Warning"), t_fault, 30) - t_fault
        lab.core.set_cut("spoke-3", "hub-b", False)
        t_restore = time.time()
        back = first(("SVC-0004", "live up"), t_restore, 10) - t_restore
    finally:
        lab.core.set_cut("spoke-3", "hub-b", False)
        proc.terminate()
        proc.wait(10)
    frame_lines = [line for _, line in lines if "intent (NetBox)" in line]
    assert frame_lines, "the header naming the three sources is missing"
    timing("status --watch: fault -> live state shown", live, 2.5)
    timing("status --watch: restore -> live state shown", back, 2.5)
    timing("status --watch: fault -> Zabbix status shown", zabbix, 12.0)
    assert live <= 2.5 and back <= 2.5
    assert zabbix <= 12.0
    green("after the status watch test")
