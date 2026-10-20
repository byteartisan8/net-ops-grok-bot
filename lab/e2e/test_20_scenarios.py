"""Every scenario: reset -> apply -> assert simd, SNMP, Zabbix items, Zabbix service status, drift -> reset -> green.

Everything asserted comes from the scenario file (``lab/scenarios/<name>.yaml``): its steps, its expectations, its
follow-up phases and its time budget (contract 9.5: fault -> business service status <= 10 s, with traps). The CLI
is checked against the same file: ``labctl drift`` exits 1 exactly when the file expects drift and prints the
expected findings; ``labctl impact`` matches the file's impact expectations.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from netsim_lab.drift import DRIFT_CLASSES
from netsim_lab.scenarios import Phase, run_steps, steps_end, verify_phase

ORDER = [
    "healthy",
    "primary-fail",
    "hub-down",
    "spoke-isolated",
    "double-failure",
    "vcid-typo",
    "mtu-mismatch",
    "fake-redundancy",
    "stale-backup",
    "new-service-pending",
]


def labctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "netsim_lab.cli", *args], capture_output=True, text=True, timeout=120)


def check_cli(phase: Phase) -> None:
    if phase.expect.drift is not None:
        result = labctl("drift", "--json")
        report = json.loads(result.stdout)
        expects_drift = any(f.cls in DRIFT_CLASSES for f in phase.expect.drift.findings)
        assert result.returncode == (1 if expects_drift else 0), (result.returncode, result.stderr)
        messages = {f["message"] for f in report["findings"]}
        for f in phase.expect.drift.findings:
            if f.message:
                assert f.message in messages, (f.message, messages)
    for imp in phase.expect.impact:
        result = labctl("impact", imp.host, "--json")
        assert result.returncode == 0, result.stderr
        got = {s["service"]: s for s in json.loads(result.stdout)["services"]}
        for field, want in (("intent_impact", imp.intent), ("live_impact", imp.live)):
            for service, s in got.items():
                expected = want.get(service, want.get("default"))
                if expected:
                    assert s[field] == expected, (imp.host, service, field, s[field], expected)


def run_phase(lab, name: str, phase: Phase, timing) -> None:
    records = run_steps(lab, phase.steps, f"e2e:{name}:{phase.name}")
    result = verify_phase(lab, phase, steps_end(records))
    label = name if phase.name == "main" else f"{name} / {phase.name}"
    has_services = phase.expect.zabbix is not None and bool(phase.expect.zabbix.services)
    if phase.steps and has_services:
        timing(f"{label}: last step -> Zabbix service status", result["zabbix_services_s"], phase.budget_s)
    timing(f"{label}: every expectation holds", result["elapsed_s"], None, "simd+SNMP+Zabbix+drift")
    assert result["ok"], json.dumps(result["mismatches"], indent=1)
    if phase.steps and has_services and phase.budget_s is not None:
        assert result["zabbix_services_s"] is not None and result["zabbix_services_s"] <= phase.budget_s, result
    check_cli(phase)


@pytest.mark.parametrize("name", ORDER)
def test_scenario(name, lab, scenarios, timing, green):
    scenario = scenarios[name]
    green(f"before {name}")
    try:
        for phase in scenario.phases:
            run_phase(lab, name, phase, timing)
    finally:
        green(f"after {name}")
