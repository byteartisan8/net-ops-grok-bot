"""Every scenario file is valid and only refers to things that exist (hosts, services, config paths)."""

from __future__ import annotations

import pytest
from netsim_lab.configpath import apply_patch, validate
from netsim_lab.scenarios import PW_KEY, Scenario, load_scenarios
from netsim_lab.settings import load_settings

from netsim_common.canonical import canonical_dict

#: The ten scenarios of prompt 05, plus the two of session 06 (second region, unprotected bronze services).
PROMPT_SCENARIOS = {
    "healthy",
    "primary-fail",
    "hub-down",
    "spoke-isolated",
    "double-failure",
    "vcid-typo",
    "mtu-mismatch",
    "fake-redundancy",
    "new-service-pending",
    "stale-backup",
    "region-down",
    "maintenance-by-tier",
}


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return load_scenarios(load_settings().scenarios_dir)


def test_every_prompt_scenario_exists(scenarios):
    assert set(scenarios) == PROMPT_SCENARIOS


def _services_added(sc: Scenario) -> set[str]:
    return {s.netbox["add_service"]["id"] for p in sc.phases for s in p.steps if s.netbox and "add_service" in s.netbox}


@pytest.mark.parametrize("name", sorted(PROMPT_SCENARIOS))
def test_scenario_refers_to_real_things(name, scenarios, topology, golden):
    sc = scenarios[name]
    hosts = set(topology.hostnames)
    services = {s.id for s in topology.services} | _services_added(sc)
    configs = {h: canonical_dict(c) for h, c in golden.items()}
    for phase in sc.phases:
        for step in phase.steps:
            if step.config is not None:
                host = step.config["host"]
                assert host in hosts
                configs[host] = apply_patch(configs[host], step.config["patch"])
                assert validate(configs[host]).valid, (name, step.describe(), validate(configs[host]).errors)
            if step.device is not None:
                assert next(iter(step.device.values())) in hosts
            if step.cut is not None:
                assert set(step.cut) <= hosts
            if step.lab_api is not None:
                assert set(step.lab_api["apply_intent"]) <= hosts
        for key, value in phase.expect.pws.items():
            if key == "default":
                continue
            assert PW_KEY.match(key)["service"] in services, key
            if isinstance(value, dict):
                assert set(value) <= hosts, key
        if phase.expect.zabbix:
            assert set(phase.expect.zabbix.services) - {"default"} <= services
            assert set(phase.expect.zabbix.customers) - {"default"} <= {c.name for c in topology.customers}
        if phase.expect.drift:
            for f in phase.expect.drift.findings:
                assert f.service in services | {"*", None}
                assert set(f.services or []) <= services
                assert f.cls in ("config-drift", "fault", "intent-only")
        for imp in phase.expect.impact:
            assert imp.host in hosts
            assert set(imp.intent) | set(imp.live) - {"default"} <= services | {"default"}
        assert phase.budget_s is None or phase.budget_s <= phase.timeout_s
