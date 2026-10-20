"""Drift survives ``make down && make up``: device configs live on volumes (contract section 12)."""

from __future__ import annotations

import subprocess
import time

from netsim_lab.bringup import wait_healthy
from netsim_lab.scenarios import run_steps, steps_end, verify_phase

from netsim_common.paths import repo_root


def test_vcid_typo_survives_make_down_and_up(lab, scenarios, timing, green):
    scenario = scenarios["vcid-typo"]
    green("before the persistence test")
    records = run_steps(lab, scenario.steps, "e2e:persistence")
    assert verify_phase(lab, scenario.main, steps_end(records))["ok"]
    start = time.monotonic()
    for target in ("down", "up"):
        subprocess.run(["make", target], cwd=repo_root(), check=True, capture_output=True, timeout=900)
    wait_healthy(timeout=900, log=lambda _m: None)
    timing("make down && make up -> every container healthy", time.monotonic() - start, None)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and any(isinstance(s, Exception) for s in lab.devices.states().values()):
        time.sleep(1)
    running = lab.devices["spoke-1"].running()
    assert running.xconnect("SVC-0001").pw("backup").vc_id == 2010, "the typo must survive the restart"
    deadline = time.monotonic() + 120
    findings: list[str] = []
    while time.monotonic() < deadline:
        try:
            report = lab.drift()
        except Exception:  # NetBox still warming up
            time.sleep(2)
            continue
        findings = [f.message for f in report.findings if f.is_drift]
        if findings:
            break
        time.sleep(2)
    assert findings == ["SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001"], findings
    green("after the persistence test")
