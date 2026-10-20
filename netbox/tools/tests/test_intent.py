"""Intent renderer, BSM spec and bot queries on the recorded, freshly seeded NetBox (contract section 10)."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest
from netsim_netbox.intent import (
    IntentError,
    Snapshot,
    blast_radius,
    bsm_spec,
    expected_pws,
    load_snapshot,
    render_all,
    render_device,
    service_detail,
    services_on_device,
)
from netsim_netbox.intent import render as render_mod
from netsim_testlib import REPO, docker_available

from netsim_common import bsm_spec_from_topology, canonical_json, diff_configs
from netsim_common.paths import contract_dir

GOLDEN = contract_dir() / "golden"
HOSTS = sorted(p.stem for p in GOLDEN.glob("*.json"))
SPOKES = [h for h in HOSTS if h.startswith("spoke-")]


def golden(host: str) -> str:
    return (GOLDEN / f"{host}.json").read_text(encoding="utf-8")


def iface(fake, host: str, name: str) -> dict:
    return fake.find("dcim/interfaces", **{"device.name": host, "name": name})


def add_service(fake, service_id: str, vid: int, vc_id: int, spoke: str = "spoke-1", hub: str = "hub-a") -> None:
    """Simulate an operator adding a service in NetBox: two subinterfaces, an L2VPN and its hub termination."""
    template_l2vpn = fake.find("vpn/l2vpns", name="SVC-0001-primary")
    new_ifaces = []
    for host, parent_name in ((spoke, "GigabitEthernet0/0/1"), (hub, "GigabitEthernet0/0/2")):
        sub = copy.deepcopy(iface(fake, host, f"{parent_name}.101"))
        sub["id"] = fake._next_id = fake._next_id + 1
        sub["name"] = f"{parent_name}.{vid}"
        sub["description"] = f"Customer-A {service_id}"
        sub["tagged_vlans"] = [{**sub["tagged_vlans"][0], "id": 9000 + vid, "vid": vid, "name": service_id}]
        fake.objects("dcim/interfaces").append(sub)
        new_ifaces.append(sub)
    l2vpn = copy.deepcopy(template_l2vpn)
    l2vpn.update(id=8000 + vid, name=f"{service_id}-primary", slug=f"{service_id.lower()}-primary", identifier=vc_id)
    l2vpn["custom_fields"] = {
        **l2vpn["custom_fields"],
        "service_id": service_id,
        "spoke_ac": {"id": new_ifaces[0]["id"], "name": new_ifaces[0]["name"], "device": new_ifaces[0]["device"]},
    }
    fake.objects("vpn/l2vpns").append(l2vpn)
    term = copy.deepcopy(fake.find("vpn/l2vpn-terminations", **{"l2vpn.name": "SVC-0001-primary"}))
    term.update(id=7000 + vid, assigned_object_id=new_ifaces[1]["id"])
    term["l2vpn"] = {**term["l2vpn"], "id": l2vpn["id"], "name": l2vpn["name"]}
    term["assigned_object"] = {**term["assigned_object"], "id": new_ifaces[1]["id"], "name": new_ifaces[1]["name"]}
    fake.objects("vpn/l2vpn-terminations").append(term)


# --------------------------------------------------------------------------------------------- rendering
def test_recorded_netbox_has_no_intent_problems(intent):
    assert intent.problems == []
    assert sorted(intent.devices) == HOSTS
    assert len(intent.pseudowires) == 20


@pytest.mark.parametrize("host", HOSTS)
def test_render_equals_golden_byte_for_byte(intent, host):
    assert canonical_json(render_device(host, intent)) == golden(host)


def test_render_all_covers_every_device(intent):
    assert sorted(render_all(intent)) == HOSTS


def test_rendering_uses_netbox_only(fake, make_intent, monkeypatch):
    """No topology at all: the intent comes from NetBox data alone."""

    def no_topology(*args, **kwargs):
        raise AssertionError("the intent renderer must not read contract/topology.yaml")

    monkeypatch.setattr("netsim_common.topology._load", no_topology)
    intent = make_intent(fake)
    assert canonical_json(render_device("hub-a", intent)) == golden("hub-a")


def test_identifier_change_moves_exactly_that_vc_id(fake, make_intent):
    fake.find("vpn/l2vpns", name="SVC-0002-primary")["identifier"] = 1102
    intent = make_intent(fake)
    expected = ["l2vpn/xconnect[SVC-0002]/pseudowire[primary]/vc-id: expected 1002, got 1102"]
    for host in ("spoke-2", "hub-a"):
        assert [d.message for d in diff_configs(golden(host), render_device(host, intent))] == expected
    for host in set(HOSTS) - {"spoke-2", "hub-a"}:
        assert diff_configs(golden(host), render_device(host, intent)) == []


def test_vlan_of_a_subinterface_is_its_tagged_vlan(fake, make_intent):
    sub = iface(fake, "spoke-1", "GigabitEthernet0/0/1.101")
    sub["tagged_vlans"][0]["vid"] = 105
    diffs = diff_configs(golden("spoke-1"), render_device("spoke-1", make_intent(fake)))
    assert [d.message for d in diffs] == [
        "interfaces/interface[GigabitEthernet0/0/1.101]/vlan-id: expected 101, got 105"
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.update(mode={"value": "access", "label": "Access"}), id="access-mode"),
        pytest.param(lambda s: s.update(tagged_vlans=[]), id="no-vlan"),
        pytest.param(lambda s: s["tagged_vlans"].append({**s["tagged_vlans"][0], "id": 1, "vid": 7}), id="two-vlans"),
        pytest.param(lambda s: s.update(untagged_vlan={"id": 1, "vid": 7, "name": "x"}), id="untagged-vlan"),
    ],
)
def test_ambiguous_subinterface_vlan_blocks_only_that_device(fake, make_intent, mutate):
    mutate(iface(fake, "spoke-1", "GigabitEthernet0/0/1.101"))
    intent = make_intent(fake)
    with pytest.raises(IntentError, match=r"spoke-1 GigabitEthernet0/0/1\.101: the VLAN of a subinterface"):
        render_device("spoke-1", intent)
    assert canonical_json(render_device("hub-a", intent)) == golden("hub-a")


def test_interface_role_and_description_come_from_netbox(fake, make_intent):
    uplink = iface(fake, "hub-b", "GigabitEthernet0/0/0")
    uplink.update(mtu=1600, description="", enabled=False)
    messages = {d.message for d in diff_configs(golden("hub-b"), render_device("hub-b", make_intent(fake)))}
    assert messages == {
        "interfaces/interface[GigabitEthernet0/0/0]/mtu: expected 9000, got 1600",
        "interfaces/interface[GigabitEthernet0/0/0]/description: expected CORE, got <absent>",
        "interfaces/interface[GigabitEthernet0/0/0]/enabled: expected true, got false",
    }


def test_broken_l2vpn_blocks_its_ends_its_spoke_and_the_spec(fake, make_intent):
    fake.find("vpn/l2vpns", name="SVC-0004-primary")["custom_fields"]["spoke_ac"] = None
    intent = make_intent(fake)
    with pytest.raises(IntentError, match="spoke_ac"):
        bsm_spec(intent)
    for host in ("hub-a", "spoke-3"):  # its hub end, and the spoke whose SVC-0004 xconnect would be incomplete
        with pytest.raises(IntentError, match="SVC-0004-primary"):
            render_device(host, intent)
    for host in ("hub-b", "spoke-1"):  # hub-b's SVC-0004 xconnect holds only the intact backup PW
        assert canonical_json(render_device(host, intent)) == golden(host)


def test_spoke_ac_termination_is_ignored_and_extra_ones_are_problems(fake, make_intent):
    term = fake.find("vpn/l2vpn-terminations", **{"l2vpn.name": "SVC-0001-primary"})
    spoke_ac = fake.find("vpn/l2vpns", name="SVC-0001-primary")["custom_fields"]["spoke_ac"]
    on_spoke = {**copy.deepcopy(term), "id": 99_001, "assigned_object_id": spoke_ac["id"]}
    fake.objects("vpn/l2vpn-terminations").append(on_spoke)
    assert make_intent(fake).problems == []  # NetBox lets one L2VPN terminate the spoke AC: same intent
    extra = {**copy.deepcopy(term), "id": 99_002, "assigned_object_id": iface(fake, "hub-b", "Loopback0")["id"]}
    fake.objects("vpn/l2vpn-terminations").append(extra)
    assert "exactly one termination" in str(make_intent(fake).problems[0])


def test_unknown_or_foreign_devices_raise(intent):
    with pytest.raises(IntentError, match="no device with platform netsim-clixon"):
        render_device("core", intent)


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="yanglint runs in a container")
def test_rendered_configs_pass_yanglint(intent):
    out = REPO / "build" / "test-intent-yanglint"  # tools/yanglint.sh only mounts the repository
    paths = render_mod.write_configs(render_all(intent), out)
    result = render_mod.yanglint(paths)
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------------------------- BSM spec
def test_bsm_spec_equals_the_topology_spec(intent, topology):
    assert bsm_spec(intent).normalized() == bsm_spec_from_topology(topology)


def test_bsm_spec_validates_against_the_contract_schema(intent):
    schema = json.loads((contract_dir() / "bsm-spec.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(json.loads(bsm_spec(intent).to_json()), schema)


def test_bsm_spec_carries_netbox_ids_and_urls(fake, intent):
    spec = bsm_spec(intent)
    tenant = fake.find("tenancy/tenants", name="Customer-A")
    customer = next(c for c in spec.customers if c.name == "Customer-A")
    assert customer.tags["netbox_tenant_id"] == str(tenant["id"])
    assert customer.netbox_url == f"http://netbox.test/tenancy/tenants/{tenant['id']}/"
    assert spec.root.netbox_url == "http://netbox.test/vpn/l2vpns/?tag=l2vpn-pw"
    service = spec.service("SVC-0002")
    assert service.netbox_url == "http://netbox.test/vpn/l2vpns/?cf_service_id=SVC-0002"
    for pw in service.pseudowires:
        l2vpn = fake.find("vpn/l2vpns", name=f"SVC-0002-{pw.role.value}")
        assert pw.netbox_l2vpn_id == l2vpn["id"] and pw.tags["netbox_l2vpn_id"] == str(l2vpn["id"])
        assert pw.netbox_url in pw.description


def test_added_l2vpn_appears_as_a_new_leaf(fake, make_intent):
    add_service(fake, "SVC-0012", vid=107, vc_id=1012)
    intent = make_intent(fake)
    spec = bsm_spec(intent)
    svc = spec.service("SVC-0012")
    assert [(pw.role.value, pw.vc_id, pw.a_end.host, pw.z_end.host) for pw in svc.pseudowires] == [
        ("primary", 1012, "spoke-1", "hub-a")
    ]
    assert svc.status_rules.value == "unprotected"  # one L2VPN so far: single-homed
    assert len(spec.services()) == 12
    diffs = {d.path for d in diff_configs(golden("spoke-1"), render_device("spoke-1", intent))}
    assert diffs == {"interfaces/interface[GigabitEthernet0/0/1.107]", "l2vpn/xconnect[SVC-0012]"}


def test_deleted_l2vpn_disappears(fake, make_intent):
    removed = fake.remove("vpn/l2vpns", name="SVC-0001-backup")
    fake.remove("vpn/l2vpn-terminations", **{"l2vpn.id": removed["id"]})
    intent = make_intent(fake)
    service = bsm_spec(intent).service("SVC-0001")
    assert [pw.role.value for pw in service.pseudowires] == ["primary"]
    assert service.status_rules.value == "unprotected"  # NetBox now says single-homed
    diffs = [d.path for d in diff_configs(golden("hub-b"), render_device("hub-b", intent))]
    assert diffs == ["l2vpn/xconnect[SVC-0001]"]  # the subinterface stays: interfaces are separate intent


def test_tenant_without_sla_tier_breaks_only_the_spec(fake, make_intent):
    fake.find("tenancy/tenants", name="Customer-C")["custom_fields"]["sla_tier"] = None
    intent = make_intent(fake)
    with pytest.raises(IntentError, match="Customer-C"):
        bsm_spec(intent)
    assert canonical_json(render_device("spoke-3", intent)) == golden("spoke-3")


# --------------------------------------------------------------------------------------------- queries
def test_blast_radius_of_hub_a_degrades_the_north_and_downs_the_unprotected(intent):
    impact = {i.service_id: (i.impact, i.affected_roles) for i in blast_radius("hub-a", intent)}
    north = ["SVC-0001", "SVC-0002", "SVC-0003", "SVC-0004", "SVC-0005", "SVC-0006", "SVC-0008"]
    assert impact == {
        **dict.fromkeys(north, ("degraded", ("primary",))),
        "SVC-0010": ("down", ("primary",)),  # unprotected: its only PW ends on hub-a
        **dict.fromkeys(("SVC-0007", "SVC-0009", "SVC-0011"), ("unaffected", ())),  # South: own hub pair
    }


def test_blast_radius_of_spoke_2_downs_its_services(intent):
    impact = {i.service_id: i.impact for i in blast_radius("spoke-2", intent)}
    assert impact == {
        "SVC-0001": "unaffected",
        "SVC-0002": "down",
        "SVC-0003": "down",
        "SVC-0004": "unaffected",
        "SVC-0005": "unaffected",
        "SVC-0006": "unaffected",
        "SVC-0007": "unaffected",
        "SVC-0008": "unaffected",
        "SVC-0009": "unaffected",
        "SVC-0010": "unaffected",
        "SVC-0011": "unaffected",
    }


def test_blast_radius_matches_the_topology_for_every_host(intent, topology):
    for host in HOSTS:
        got = {i.service_id: i.impact for i in blast_radius(host, intent)}
        for s in topology.services:
            on_host = [r for r in s.roles if host in (s.spoke, s.pw(r).hub)]
            want = "down" if len(on_host) == len(s.roles) else "degraded" if on_host else "unaffected"
            assert got[s.id] == want, (host, s.id)


def test_services_on_device(intent):
    assert [(s.service_id, s.roles, s.ac) for s in services_on_device("spoke-2", intent)] == [
        ("SVC-0002", ("primary", "backup"), "GigabitEthernet0/0/1.102"),
        ("SVC-0003", ("primary", "backup"), "GigabitEthernet0/0/1.201"),
    ]
    assert {s.roles for s in services_on_device("hub-b", intent)} == {("backup",)}


@pytest.mark.parametrize("host", HOSTS)
def test_expected_pws_match_the_topology(intent, topology, host):
    got = [(p.service_id, p.role, p.peer_host, p.peer_loopback, p.vc_id, p.ac) for p in expected_pws(host, intent)]
    want = [
        (pw.service_id, pw.role.value, pw.peer_host, pw.peer_loopback, pw.vc_id, pw.local_ac)
        for pw in topology.pseudowires_on(host)
    ]
    assert got == want


def test_service_detail(intent):
    detail = service_detail("SVC-0002", intent)
    assert (detail["customer"], detail["sla_tier"], detail["spoke"]) == ("Customer-A", "gold", "spoke-2")
    assert [(p["role"], p["vc_id"], p["a_end"]["host"], p["z_end"]["host"]) for p in detail["pseudowires"]] == [
        ("primary", 1002, "spoke-2", "hub-a"),
        ("backup", 2002, "spoke-2", "hub-b"),
    ]
    assert detail["pseudowires"][0]["a_end"] == {
        "host": "spoke-2",
        "ac": "GigabitEthernet0/0/1.102",
        "expected_peer": "10.255.0.1",
    }
    with pytest.raises(IntentError, match="SVC-0999"):
        service_detail("SVC-0999", intent)


# --------------------------------------------------------------------------------------------- snapshot
def test_snapshot_round_trips_through_json(fake):
    snapshot = load_snapshot(fake)
    assert Snapshot.from_json(snapshot.to_json()) == snapshot


def test_snapshot_of_an_empty_netbox(empty_netbox):
    """No tag, platform or tenant group yet: no filter on unknown slugs (NetBox would answer HTTP 400)."""
    snapshot = load_snapshot(empty_netbox)
    assert (snapshot.devices, snapshot.l2vpns, snapshot.tenants) == ([], [], [])
