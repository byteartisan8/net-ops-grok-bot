"""Shared fixtures for the lab unit tests (no containers): the contract topology and golden configs."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from netsim_lab.configpath import apply_patch
from netsim_lab.drift import DriftInputs, plans_from_topology

from netsim_common.canonical import canonical_dict
from netsim_common.device_config import DeviceConfig
from netsim_common.golden import render_device_config
from netsim_common.topology import Topology, load_topology


@pytest.fixture(scope="session")
def topology() -> Topology:
    return load_topology()


@pytest.fixture(scope="session")
def golden(topology: Topology) -> dict[str, DeviceConfig]:
    return {h: render_device_config(topology, h) for h in topology.hostnames}


def _patched(config: DeviceConfig, patch: list[dict[str, Any]]) -> DeviceConfig:
    return DeviceConfig.from_restconf(apply_patch(canonical_dict(config), patch))


def _up_state(topology: Topology, host: str, overrides: dict[tuple[str, str], str] | None = None) -> dict[str, Any]:
    """A simd ``/state`` with every PW of ``host`` up/ok, except ``overrides[(service, role)] = "oper/reason"``."""
    overrides = overrides or {}
    pws = []
    for svc in topology.services:
        for role in svc.roles:
            if host not in (svc.spoke, svc.pw(role).hub):
                continue
            oper, reason = overrides.get((svc.id, role.value), "up/ok").split("/")
            pws.append({"service_id": svc.id, "role": role.value, "oper": oper, "reason": reason})
    return {"hostname": host, "pseudowires": pws}


def _make_inputs(
    topology: Topology,
    golden: dict[str, DeviceConfig],
    *,
    running: dict[str, Any] | None = None,
    intended: dict[str, Any] | None = None,
    baselines: dict[str, Any] | None = None,
    states: dict[str, Any] | None = None,
    plans: Any = None,
    **kw: Any,
) -> DriftInputs:
    hosts = list(topology.hostnames)
    run = {h: copy.deepcopy(golden[h]) for h in hosts} | (running or {})
    want = {h: golden[h] for h in hosts} | (intended or {})
    return DriftInputs(
        hosts=hosts,
        intended=want,
        running=run,
        baselines={h: canonical_dict(golden[h]) for h in hosts} | (baselines or {}),
        baseline_source=dict.fromkeys(hosts, "golden"),
        plans=plans if plans is not None else plans_from_topology(topology),
        loopbacks={d.hostname: d.loopback for d in topology.devices},
        states={h: _up_state(topology, h) for h in hosts} | (states or {}),
        **kw,
    )


@pytest.fixture(scope="session")
def patched():
    """``patched(config, patch)``: a DeviceConfig with a config patch applied."""
    return _patched


@pytest.fixture(scope="session")
def up_state(topology):
    """``up_state(host, overrides)``: a simd /state with every PW up except the overrides."""
    return lambda host, overrides=None, topo=None: _up_state(topo or topology, host, overrides)


@pytest.fixture(scope="session")
def make_inputs(topology, golden):
    """``make_inputs(running=..., intended=..., ...)``: DriftInputs over the golden lab."""
    return lambda **kw: _make_inputs(topology, golden, **kw)
