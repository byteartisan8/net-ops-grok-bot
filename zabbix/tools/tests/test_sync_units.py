"""zabbix-sync pieces against an in-memory Zabbix (``fake_zabbix``): BSM tree, SLAs, spec macros, map."""

from __future__ import annotations

import pytest
from netsim_zabbix import names as N
from netsim_zabbix.bsm_sync import TreeSync, desired_tree, redundant_pair_rules, sync_slas
from netsim_zabbix.map_sync import desired_map, pairs, sync_map
from netsim_zabbix.source import from_topology
from netsim_zabbix.sync import sync_macros

from netsim_common import bsm_spec_from_topology, load_topology, pw_spec_macro_value
from netsim_common.bsm import BsmSpec, PwEnd, build_customer, build_pseudowire, build_root, build_service

SPEC = bsm_spec_from_topology(load_topology())


def with_extra_service(spec: BsmSpec) -> BsmSpec:
    """The lab spec plus SVC-0007 on spoke-1 (as if ordered in NetBox)."""
    customers = []
    for c in spec.customers:
        services = list(c.services)
        if c.name == "Customer-A":
            pws = [
                build_pseudowire("SVC-0007", "primary", 1007, PwEnd(host="spoke-1", ac="GigabitEthernet0/0/1.107",
                                 expected_peer="10.255.0.1"), PwEnd(host="hub-a", ac="GigabitEthernet0/0/2.107",
                                 expected_peer="10.255.1.1")),
                build_pseudowire("SVC-0007", "backup", 2007, PwEnd(host="spoke-1", ac="GigabitEthernet0/0/1.107",
                                 expected_peer="10.255.0.2"), PwEnd(host="hub-b", ac="GigabitEthernet0/0/2.107",
                                 expected_peer="10.255.1.1")),
            ]  # fmt: skip
            services.append(build_service("SVC-0007", c.name, "spoke-1", pws))
        customers.append(build_customer(c.name, c.sla_tier, services))
    return BsmSpec(root=build_root(), customers=customers)


# ------------------------------------------------------------------------------------------------ tree
def test_desired_tree_shape_and_rules() -> None:
    nodes = desired_tree(SPEC)
    assert [n.level for n in nodes].count(0) == 1 and len(nodes) == 1 + 4 + 6 + 12
    by_name = {n.name: n for n in nodes}
    assert all((N.MANAGED_TAG, N.MANAGED_VALUE) in n.tags for n in nodes)
    svc = by_name["SVC-0002 (spoke-2)"]
    assert svc.parent == "Customer-A" and svc.algorithm == 0  # "set status to OK" + additional rules
    assert svc.status_rules == redundant_pair_rules() == ((0, 1, 3, 2), (0, 2, 4, 5))
    assert ("sla_tier", "gold") in svc.tags and ("service", "SVC-0002") in svc.tags
    leaf = by_name["SVC-0002 primary (hub-a)"]
    assert leaf.problem_tags == (("pw_role", 0, "primary"), ("scope", 0, "l2vpn-pw"), ("service", 0, "SVC-0002"))
    assert "NetBox" in by_name["L2VPN"].description or "generated from NetBox" in by_name["L2VPN"].description
    assert by_name["Customer-A"].algorithm == by_name["L2VPN"].algorithm == 2  # most critical of children


def test_tree_sync_creates_top_down_then_makes_zero_changes(fake_zabbix) -> None:
    plan = TreeSync(fake_zabbix, SPEC).run()
    assert len(plan) == 23 and {c.action for c in plan.changes} == {"create"}
    assert len(fake_zabbix.services) == 23
    fake_zabbix.calls.clear()
    assert TreeSync(fake_zabbix, SPEC).run().changes == []
    assert fake_zabbix.writes() == []


def test_tree_sync_adds_and_prunes_a_netbox_service(fake_zabbix) -> None:
    TreeSync(fake_zabbix, SPEC).run()
    bigger = with_extra_service(SPEC)
    plan = TreeSync(fake_zabbix, bigger).run()
    created = {c.name for c in plan.changes if c.action == "create"}
    assert created == {"SVC-0007 (spoke-1)", "SVC-0007 primary (hub-a)", "SVC-0007 backup (hub-b)"}
    updated = {c.name for c in plan.changes if c.action == "update"}
    assert updated == set()  # siblings keep their sortorder (SVC-0007 sorts after SVC-0002)
    plan = TreeSync(fake_zabbix, SPEC).run()
    assert {c.name for c in plan.changes if c.action == "delete"} == created
    assert TreeSync(fake_zabbix, SPEC).run().changes == []


def test_tree_sync_leaves_foreign_services_alone(fake_zabbix) -> None:
    fake_zabbix.call("service.create", [{"name": "Hand made", "algorithm": 2, "sortorder": 0, "tags": []}])
    TreeSync(fake_zabbix, SPEC).run()
    assert "Hand made" in {s["name"] for s in fake_zabbix.services.values()}


def test_tree_sync_repairs_a_changed_node(fake_zabbix) -> None:
    TreeSync(fake_zabbix, SPEC).run()
    sid = next(i for i, s in fake_zabbix.services.items() if s["name"] == "SVC-0004 (spoke-3)")
    fake_zabbix.services[sid]["status_rules"] = []
    plan = TreeSync(fake_zabbix, SPEC).run()
    assert [str(c) for c in plan.changes] == ["update service   SVC-0004 (spoke-3): status_rules"]


def test_dry_run_writes_nothing(fake_zabbix) -> None:
    plan = TreeSync(fake_zabbix, SPEC, dry_run=True).run()
    assert len(plan) == 23 and fake_zabbix.writes() == []


# ------------------------------------------------------------------------------------------------ SLA
def test_slas_per_tier_idempotent_and_pruned(fake_zabbix) -> None:
    slo = {"gold": 99.95, "silver": 99.9}
    plan = sync_slas(fake_zabbix, slo)
    assert sorted(c.name for c in plan.changes) == ["L2VPN gold", "L2VPN silver"]
    gold = next(s for s in fake_zabbix.slas.values() if s["name"] == "L2VPN gold")
    assert gold["slo"] == "99.9500" and gold["service_tags"] == [{"tag": "sla_tier", "operator": 0, "value": "gold"}]
    assert "effective_date" in gold
    assert sync_slas(fake_zabbix, slo).changes == []
    plan = sync_slas(fake_zabbix, {"gold": 99.95})
    assert [str(c) for c in plan.changes] == ["delete sla       L2VPN silver: tier no longer defined"]


# ------------------------------------------------------------------------------------------------ macros
def test_spec_macros_written_updated_and_pruned(fake_zabbix) -> None:
    lab = from_topology()
    hostids = {h.name: str(i) for i, h in enumerate(lab.hosts, start=1)}
    plan, changed = sync_macros(fake_zabbix, lab, hostids)
    assert len(plan) == 6 and sorted(changed) == sorted(hostids)
    values = {(m["hostid"], m["macro"]): m["value"] for m in fake_zabbix.macros.values()}
    assert values[(hostids["hub-a"], N.SPEC_MACRO)] == pw_spec_macro_value(lab.bsm, "hub-a")
    assert sync_macros(fake_zabbix, lab, hostids)[0].changes == []
    bigger = type(lab)(source="t", hosts=lab.hosts, bsm=with_extra_service(lab.bsm), sla_slo=lab.sla_slo)
    plan, changed = sync_macros(fake_zabbix, bigger, hostids)
    assert sorted(changed) == ["hub-a", "hub-b", "spoke-1"]
    plan, changed = sync_macros(fake_zabbix, lab, hostids)  # removed from NetBox again: entries pruned
    assert sorted(changed) == ["hub-a", "hub-b", "spoke-1"]
    assert {m["value"] for m in fake_zabbix.macros.values() if m["hostid"] == hostids["spoke-1"]} == {
        pw_spec_macro_value(lab.bsm, "spoke-1")
    }


def test_spec_host_missing_in_zabbix_is_a_warning(fake_zabbix) -> None:
    lab = from_topology()
    plan, _ = sync_macros(fake_zabbix, lab, {"hub-a": "1"})
    assert any("spoke-1 is in the BSM spec but not in Zabbix" in w for w in plan.warnings)


# ------------------------------------------------------------------------------------------------ map
def test_pairs_cover_every_spoke_hub_link() -> None:
    ps = pairs(SPEC)
    assert len(ps) == 8  # 4 spokes x 2 hubs
    sp2a = next(p for p in ps if (p.spoke, p.hub) == ("spoke-2", "hub-a"))
    assert sp2a.pws == (("SVC-0002", "primary", 1002), ("SVC-0003", "primary", 1003))
    assert sp2a.label == "SVC-0002 primary (1002)\nSVC-0003 primary (1003)"


def _triggers(hosts: dict[str, str]) -> list[dict]:
    out = []
    n = 0
    for svc, pw in SPEC.pseudowires():
        for end in pw.ends:
            for check, prio in (("missing", "4"), ("oper", "4"), ("spec", "3")):
                n += 1
                out.append(
                    {
                        "triggerid": str(5000 + n),
                        "priority": prio,
                        "hosts": [{"host": end.host}],
                        "tags": [
                            {"tag": "service", "value": svc.service_id},
                            {"tag": "pw_role", "value": pw.role.value},
                            {"tag": "check", "value": check},
                        ],
                    }
                )
    return out


def test_desired_map_links_use_both_ends_triggers() -> None:
    hostids = {h: str(i) for i, h in enumerate(SPEC.hosts(), start=1)}
    roles = {h: ("hub" if h.startswith("hub") else "spoke") for h in hostids}
    body, pending = desired_map(SPEC, hostids, roles, {"hub": "1", "spoke": "2"}, _triggers(hostids))
    assert pending == [] and len(body["selements"]) == 6 and len(body["links"]) == 8
    link = next(ln for ln in body["links"] if ln["label"].startswith("SVC-0001 primary"))
    assert link["indicator_type"] == 1 and len(link["linktriggers"]) == 6  # 3 checks x 2 ends
    colors = {t["color"] for t in link["linktriggers"]}
    assert colors == {"D32F2F", "F57C00"}
    hubs_y = {s["y"] for s in body["selements"] if s["iconid_off"] == "1"}
    spokes_y = {s["y"] for s in body["selements"] if s["iconid_off"] == "2"}
    assert len(hubs_y) == len(spokes_y) == 1 and hubs_y != spokes_y


def test_map_links_wait_for_discovery() -> None:
    hostids = {h: str(i) for i, h in enumerate(SPEC.hosts(), start=1)}
    roles = {h: ("hub" if h.startswith("hub") else "spoke") for h in hostids}
    body, pending = desired_map(SPEC, hostids, roles, {"hub": "1", "spoke": "2"}, [])
    assert len(pending) == 8 and all(ln["indicator_type"] == 0 for ln in body["links"])


@pytest.mark.parametrize("triggers_first", [True, False])
def test_sync_map_is_idempotent(fake_zabbix, triggers_first: bool) -> None:
    hostids = {h: str(i) for i, h in enumerate(SPEC.hosts(), start=1)}
    roles = {h: ("hub" if h.startswith("hub") else "spoke") for h in hostids}
    if triggers_first:
        fake_zabbix.triggers = _triggers(hostids)
    assert [c.action for c in sync_map(fake_zabbix, SPEC, hostids, roles).changes] == ["create"]
    if not triggers_first:
        fake_zabbix.triggers = _triggers(hostids)
        assert [c.action for c in sync_map(fake_zabbix, SPEC, hostids, roles).changes] == ["update"]
    assert sync_map(fake_zabbix, SPEC, hostids, roles).changes == []
