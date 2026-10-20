"""contract/golden: rendered from the topology, canonical, and equal to the contract's spoke-1 example."""

from __future__ import annotations

import json

import pytest
from netsim_testlib import GOLDEN, contract_example, load_tool

from netsim_common import DeviceConfig, canonical_json, diff_configs, load_topology, render_device_config

HOSTS = ["hub-a", "hub-b", "hub-c", "hub-d", "spoke-1", "spoke-2", "spoke-3", "spoke-4", "spoke-5", "spoke-6"]


def test_golden_files_are_current():
    assert load_tool("render_golden").main(["--check"]) == 0


def test_golden_files_exist_for_every_device():
    assert sorted(p.stem for p in GOLDEN.glob("*.json")) == sorted(load_topology().hostnames) == HOSTS


def test_spoke1_golden_equals_contract_example():
    example = contract_example("RESTCONF JSON example (spoke-1, golden)")
    golden = (GOLDEN / "spoke-1.json").read_text(encoding="utf-8")
    assert canonical_json(example) == golden
    assert diff_configs(example, golden) == []


@pytest.mark.parametrize("host", HOSTS)
def test_golden_is_valid_and_canonical(host):
    text = (GOLDEN / f"{host}.json").read_text(encoding="utf-8")
    cfg = DeviceConfig.from_restconf(text)  # full validation, including leafrefs
    assert canonical_json(cfg) == text
    assert canonical_json(json.loads(text)) == text
    assert cfg.hostname == host
    assert cfg.duplicate_pseudowires() == {}


def test_hubs_hold_only_their_role_and_spokes_both():
    topo = load_topology()
    for host in HOSTS:
        cfg = render_device_config(topo, host)
        roles = {pw.role.value for xc in cfg.l2vpn.xconnect for pw in xc.pseudowire}
        primary, backup = {"primary"}, {"backup"}
        expected = {"hub-a": primary, "hub-b": backup, "hub-c": primary, "hub-d": backup}.get(host, primary | backup)
        assert roles == expected, host
    hub_a = render_device_config(topo, "hub-a")
    assert len(hub_a.l2vpn.xconnect) == 8
    assert hub_a.xconnect("SVC-0003").pw("primary").peer_ipv4 == topo.device("spoke-2").loopback
    spoke5 = render_device_config(topo, "spoke-5")
    assert [pw.role.value for pw in spoke5.xconnect("SVC-0009").pseudowire] == ["primary"]  # unprotected
    assert spoke5.xconnect("SVC-0007").pw("backup").peer_ipv4 == topo.device("hub-d").loopback


def test_subinterfaces_inherit_mtu():
    cfg = render_device_config(load_topology(), "spoke-2")
    sub = cfg.interface("GigabitEthernet0/0/1.201")
    assert sub.mtu is None and sub.parent == "GigabitEthernet0/0/1" and sub.vlan_id == 201
    assert sub.description == "Customer-B SVC-0003"
    assert cfg.effective_mtu(sub.name) == 1500
