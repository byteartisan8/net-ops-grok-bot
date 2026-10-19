"""Zabbix 8.0 against the real lab: idempotency, both discovery views, every PW check, NetBox-driven BSM, traps.

Faults are physical (``core`` cut keys) or config drift (RESTCONF), never both mixed (contract 1). Timing tests rely on
the real path only (device transition -> trap -> action -> check now -> walk); other tests may "execute now" the walk
to avoid waiting for the 60 s poll.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from typing import TYPE_CHECKING, Any

import pytest
from netsim_netbox.settings import load_settings as netbox_settings
from netsim_zabbix import names as N
from netsim_zabbix.provision import TEMPLATE_RULES, provision
from netsim_zabbix.source import load_lab_spec
from netsim_zabbix.sync import CONFIG_CACHE_WAIT, execute_now, sync
from netsim_zabbix.template import template_path

from netsim_common.bsm import pw_spec_for_host, pw_spec_macro_value

if TYPE_CHECKING:
    from conftest import Lab, Timings

pytestmark = pytest.mark.integration

OK, WARNING, AVERAGE, HIGH, DISASTER = -1, 2, 3, 4, 5
CHECK_NOW_BUDGET = 5.0  # contract 9.5: trap -> item refreshed via check-now
BSM_BUDGET = 10.0  # contract 9.5: fault injected -> business service status changed


# ------------------------------------------------------------------------------------------------ helpers
def healthy(lab: Lab) -> bool:
    return not lab.l2vpn_problems() and lab.service_status("L2VPN") == OK


def ensure_healthy(lab: Lab, timeout: float = 90) -> None:
    lab.clear_faults()
    lab.poll_now()
    lab.wait_until(lambda: healthy(lab), timeout, 1.0, "no L2VPN problems and BSM root OK")


def tags_of(problem: dict[str, Any]) -> dict[str, str]:
    return {t["tag"]: t["value"] for t in problem["tags"]}


def pw_problems(lab: Lab, **want: str) -> list[dict[str, Any]]:
    return [p for p in lab.l2vpn_problems() if all(tags_of(p).get(k) == v for k, v in want.items())]


def walk_clock(lab: Lab, host: str) -> float:
    item = lab.items(host, N.WALK_KEY)[N.WALK_KEY]
    return int(item["lastclock"]) + int(item["lastns"]) / 1e9


@pytest.fixture(autouse=True)
def healthy_lab(lab: Lab):
    ensure_healthy(lab)
    yield
    ensure_healthy(lab)


# ------------------------------------------------------------------------------------------------ idempotency
def test_provision_and_sync_are_idempotent(lab: Lab) -> None:
    diff = lab.api.call(
        "configuration.importcompare",
        {"format": "yaml", "source": template_path().read_text(), "rules": TEMPLATE_RULES},
    )
    assert not diff, diff
    plan = provision(dry_run=True, wait=False, log=lambda _m: None)
    assert plan.changes == [], plan.lines()
    result = sync(dry_run=True)
    assert result.plan.changes == [], result.plan.lines()
    out = subprocess.run(["uv", "run", "zabbix-bootstrap"], capture_output=True, text=True, check=True).stdout
    assert "0 changes" in out, out


# ------------------------------------------------------------------------------------------------ discovery views
def test_intent_lld_discovers_exactly_the_spec(lab: Lab) -> None:
    spec = load_lab_spec("netbox").bsm
    for host in spec.hosts():
        macros = {
            m["macro"]: m["value"]
            for m in lab.api.call(
                "usermacro.get",
                {"output": ["macro", "value"], "hostids": [lab.hostids()[host]], "filter": {"macro": N.SPEC_MACROS}},
            )
        }
        assert macros == {N.SPEC_MACRO: pw_spec_macro_value(spec, host)}, host
        discovered = set(lab.items(host, "netsim.pw.present["))
        expected = {N.pw_key("present", e.service, e.role.value) for e in pw_spec_for_host(spec, host)}
        assert discovered == expected, host


def test_overflow_spec_chunk_is_discovered_and_pruned(lab: Lab) -> None:
    """{$NETSIM.PW.SPEC.2} (hosts beyond 2048 characters) is read by the discovery like the main macro."""
    hostid = lab.hostids()["hub-b"]
    extra = [
        {
            "service": "SVC-0098",
            "role": "backup",
            "vc_id": 2098,
            "expected_peer": "10.255.1.1",
            "expected_peer_host": "spoke-1",
            "customer": "Customer-A",
            "ac": "GigabitEthernet0/0/2.198",
        }
    ]
    lab.api.call("usermacro.create", {"hostid": hostid, "macro": N.SPEC_OVERFLOW_MACROS[0], "value": json.dumps(extra)})
    try:
        rule = lab.api.get_one(
            "discoveryrule.get", {"output": ["itemid"], "hostids": [hostid], "filter": {"key_": [N.SPEC_LLD_KEY]}}
        )
        time.sleep(CONFIG_CACHE_WAIT)
        execute_now(lab.api, [rule["itemid"]])
        lab.wait_until(
            lambda: N.pw_key("present", "SVC-0098", "backup") in lab.items("hub-b", "netsim.pw.present["),
            30,
            0.5,
            "SVC-0098 backup discovered from the overflow macro",
        )
        time.sleep(CONFIG_CACHE_WAIT)
        lab.poll_now(["hub-b"])
        lab.wait_until(lambda: pw_problems(lab, service="SVC-0098", check="missing", host="hub-b"), 30, 0.5, "missing")
    finally:
        result = sync()  # the BSM spec has no overflow for hub-b: the macro is deleted, the PW forgotten
    assert any(c.name == f"hub-b {N.SPEC_OVERFLOW_MACROS[0]}" and c.action == "delete" for c in result.plan.changes)
    lab.wait_until(lambda: not pw_problems(lab, service="SVC-0098"), 60, 0.5, "SVC-0098 forgotten")


def test_observed_lld_names_and_dotted_peers(lab: Lab) -> None:
    spec = load_lab_spec("netbox").bsm
    for host in spec.hosts():
        items = lab.items(host, "netsim.pw.observed.peer[")
        seen = {
            i["name"].removeprefix("Observed PW ").split(":")[0] + ":" + i["name"].split(":")[1]: i["lastvalue"]
            for i in items.values()
        }
        want = {f"{e.service}:{e.role.value}": str(e.expected_peer) for e in pw_spec_for_host(spec, host)}
        assert seen == want, host
        assert all(v.count(".") == 3 for v in seen.values())


def test_all_pws_present_and_up_and_services_ok(lab: Lab) -> None:
    for host, counts in _pw_counts(lab).items():
        assert counts["expected"] == counts["present"] == counts["up"] == counts["observed"] > 0, (host, counts)
    total = sum(c["expected"] for c in _pw_counts(lab).values())
    assert total == 24  # 12 PWs, both ends
    statuses = {s["name"]: int(s["status"]) for s in lab.api.call("service.get", {"output": ["name", "status"]})}
    assert len(statuses) == 1 + 4 + 6 + 12 and set(statuses.values()) == {OK}


def _pw_counts(lab: Lab) -> dict[str, dict[str, int]]:
    from netsim_zabbix.status import pw_counts

    return pw_counts(lab.api)


# ------------------------------------------------------------------------------------------------ PW checks
def test_pw_down_warning_both_down_disaster_recovery(lab: Lab, timings: Timings) -> None:
    svc = "SVC-0001 (spoke-1)"
    lab.core.set(lab.cut_key("spoke-1", "hub-a"), "1")
    try:
        t_warn = lab.wait_until(lambda: lab.service_status(svc) == WARNING, 30, 0.1, f"{svc} Warning")
        oper = pw_problems(lab, service="SVC-0001", pw_role="primary", check="oper")
        assert {tags_of(p)["host"] for p in oper} == {"spoke-1", "hub-a"}
        assert all(int(p["severity"]) == HIGH for p in oper)
        assert lab.service_status("SVC-0001 primary (hub-a)") == HIGH
        assert lab.service_status("SVC-0001 backup (hub-b)") == OK
        assert lab.service_status("Customer-A") == WARNING and lab.service_status("L2VPN") == WARNING
        timings.record("fault_one_pw_down_to_bsm_warning_s", round(t_warn, 2), BSM_BUDGET)

        lab.core.set(lab.cut_key("spoke-1", "hub-b"), "1")
        t_dis = lab.wait_until(lambda: lab.service_status(svc) == DISASTER, 30, 0.1, f"{svc} Disaster")
        timings.record("fault_second_pw_down_to_bsm_disaster_s", round(t_dis, 2), BSM_BUDGET)
        assert lab.service_status("Customer-A") == DISASTER
    finally:
        lab.clear_faults()
    t_ok = lab.wait_until(lambda: lab.service_status(svc) == OK, 30, 0.1, f"{svc} OK again")
    timings.record("fault_cleared_to_bsm_ok_s", round(t_ok, 2), BSM_BUDGET)
    assert t_warn <= BSM_BUDGET and t_dis <= BSM_BUDGET and t_ok <= BSM_BUDGET


def test_dead_device_is_seen_by_its_peers(lab: Lab, timings: Timings) -> None:
    """Power off spoke-4: the hubs see both PWs of SVC-0005 and SVC-0006 go down (the BSM leaves match problems
    from both ends), so a dead device is detected even though its own items go stale; its SNMP check fires too."""
    t0 = time.monotonic()
    subprocess.run(["docker", "stop", "spoke-4"], check=True, capture_output=True)
    try:
        lab.wait_until(
            lambda: all(lab.service_status(f"{s} (spoke-4)") == DISASTER for s in ("SVC-0005", "SVC-0006")),
            60,
            0.2,
            "spoke-4 services Disaster",
        )
        timings.record("device_power_off_to_bsm_disaster_s", round(time.monotonic() - t0, 2), BSM_BUDGET)
        seen_from = {
            tags_of(p)["host"]
            for p in pw_problems(lab, check="oper")
            if tags_of(p)["service"] in ("SVC-0005", "SVC-0006")
        }
        assert seen_from == {"hub-a", "hub-b"}
        lab.wait_until(lambda: lab.problems(scope=N.SCOPE_AVAILABILITY, host="spoke-4"), 120, 1, "spoke-4 SNMP down")
        timings.record("device_power_off_to_snmp_unreachable_s", round(time.monotonic() - t0, 2), None)
        assert lab.service_status("L2VPN") == DISASTER
    finally:
        subprocess.run(["docker", "start", "spoke-4"], check=True, capture_output=True)
        subprocess.run(["uv", "run", "netsim-devices", "wait", "--timeout", "90"], check=True, capture_output=True)


def test_pw_deleted_on_one_end_is_missing(lab: Lab, timings: Timings) -> None:
    svc = "SVC-0004 (spoke-3)"
    try:
        lab.restconf_delete("spoke-3", lab.xconnect_path("SVC-0004") + "/pseudowire=backup")
        t = lab.wait_until(
            lambda: pw_problems(lab, service="SVC-0004", pw_role="backup", check="missing"),
            30,
            0.2,
            "missing on spoke-3",
        )
        timings.record("config_pw_deleted_to_missing_problem_s", round(t, 2), BSM_BUDGET)
        [missing] = pw_problems(lab, service="SVC-0004", pw_role="backup", check="missing")
        assert tags_of(missing)["host"] == "spoke-3" and int(missing["severity"]) == HIGH
        assert missing["name"] == (
            "SVC-0004 backup on spoke-3: PW missing, NetBox expects VC ID 2004 to 10.255.0.2 (hub-b) "
            "on GigabitEthernet0/0/1.301"
        )
        lab.wait_until(
            lambda: pw_problems(lab, service="SVC-0004", pw_role="backup", check="oper", host="hub-b"),
            30,
            0.2,
            "hub-b side down",
        )
        lab.wait_until(lambda: lab.service_status(svc) == WARNING, 30, 0.2, f"{svc} Warning")
        # oper on the missing end is suppressed (expression requires the row, and it depends on 'missing')
        assert not pw_problems(lab, service="SVC-0004", pw_role="backup", check="oper", host="spoke-3")
    finally:
        lab.restore_golden("spoke-3")


def test_pw_repointed_but_up_is_spec_with_expected_vs_actual(lab: Lab) -> None:
    svc = "SVC-0002 (spoke-2)"
    try:
        lab.restconf_put(
            "spoke-2",
            lab.xconnect_path("SVC-0002") + "/pseudowire=primary/peer-ipv4",
            {"netsim-device:peer-ipv4": "10.255.0.2"},
        )
        lab.restconf_put(
            "hub-b",
            lab.xconnect_path("SVC-0002") + "/pseudowire=primary",
            {
                "netsim-device:pseudowire": [
                    {"role": "primary", "peer-ipv4": "10.255.1.2", "vc-id": 1002, "enabled": True}
                ]
            },
        )
        lab.wait_until(
            lambda: pw_problems(lab, check="spec") and lab.service_status(svc) == WARNING, 40, 0.3, "spec + Warning"
        )
        [spec] = pw_problems(lab, check="spec")
        assert spec["name"] == "SVC-0002 primary on spoke-2: peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)"
        assert int(spec["severity"]) == AVERAGE
        assert {k: tags_of(spec)[k] for k in ("service", "pw_role", "host", "check", "vcid", "peer", "customer")} == {
            "service": "SVC-0002",
            "pw_role": "primary",
            "host": "spoke-2",
            "check": "spec",
            "vcid": "1002",
            "peer": "10.255.0.1",
            "customer": "Customer-A",
        }
        # the repointed PW is up on spoke-2 (no oper problem there); hub-a lost its partner; hub-b has an extra PW
        assert not pw_problems(lab, check="oper", host="spoke-2")
        lab.wait_until(lambda: pw_problems(lab, check="unexpected", host="hub-b"), 30, 0.3, "unexpected on hub-b")
        assert lab.service_status(svc) == WARNING
    finally:
        lab.restore_golden("spoke-2")
        lab.restore_golden("hub-b")


def test_extra_pw_not_in_netbox_is_unexpected_and_bsm_unchanged(lab: Lab) -> None:
    before = {s["name"] for s in lab.api.call("service.get", {"output": ["name"]})}
    try:
        lab.restconf_put(
            "spoke-1",
            "netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F1.199",
            {
                "netsim-device:interface": [
                    {
                        "name": "GigabitEthernet0/0/1.199",
                        "type": "subinterface",
                        "parent": "GigabitEthernet0/0/1",
                        "vlan-id": 199,
                        "enabled": True,
                    }
                ]
            },
        )
        lab.restconf_put(
            "spoke-1",
            lab.xconnect_path("SVC-0099"),
            {
                "netsim-device:xconnect": [
                    {
                        "service-id": "SVC-0099",
                        "customer": "Customer-X",
                        "enabled": True,
                        "attachment-circuit": "GigabitEthernet0/0/1.199",
                        "pseudowire": [{"role": "primary", "peer-ipv4": "10.255.0.1", "vc-id": 1099, "enabled": True}],
                    }
                ]
            },
        )
        # The config trap re-polls the walk, which lets "PW table" discover the new row; its items get their first
        # value from the next walk (Zabbix does not re-dispatch the value that created them): poll once more.
        lab.wait_until(
            lambda: any(
                i["name"].startswith("Observed PW SVC-0099:primary")
                for i in lab.items("spoke-1", "netsim.pw.observed.expected[").values()
            ),
            75,
            0.5,
            "observed row SVC-0099:primary discovered",
        )
        time.sleep(6)  # the new items reach the server's configuration cache (CacheUpdateFrequency 5 s)
        lab.poll_now(["spoke-1"])
        lab.wait_until(lambda: pw_problems(lab, check="unexpected", host="spoke-1"), 40, 0.3, "unexpected on spoke-1")
        [p] = pw_problems(lab, check="unexpected", host="spoke-1")
        assert p["name"] == "SVC-0099:primary on spoke-1: PW not in NetBox (VC ID 1099 to 10.255.0.1)"
        assert int(p["severity"]) == WARNING and tags_of(p)["scope"] == N.SCOPE_UNEXPECTED_TAG
        assert {s["name"] for s in lab.api.call("service.get", {"output": ["name"]})} == before
        assert lab.service_status("L2VPN") == OK
    finally:
        lab.restore_golden("spoke-1")


# ------------------------------------------------------------------------------------------------ NetBox-driven
def test_service_added_in_netbox_is_ordered_not_delivered_then_pruned(lab: Lab, timings: Timings) -> None:
    nb = netbox_settings()
    created: list[tuple[str, int]] = []
    with nb.client(write=True) as rw:

        def one(endpoint: str, **params: Any) -> dict[str, Any]:
            [obj] = rw.list(endpoint, **params)
            return obj

        tenant = one("tenancy/tenants", slug="customer-a")
        group = one("ipam/vlan-groups", slug="l2vpn-acs")
        try:
            vlan = rw.create(
                "ipam/vlans",
                {
                    "group": group["id"],
                    "vid": 107,
                    "name": "SVC-0007",
                    "tenant": tenant["id"],
                    "tags": [{"slug": "netsim-lab"}],
                },
            )
            created.append(("ipam/vlans", vlan["id"]))
            subs = {}
            for host, parent in (
                ("spoke-1", "GigabitEthernet0/0/1"),
                ("hub-a", "GigabitEthernet0/0/2"),
                ("hub-b", "GigabitEthernet0/0/2"),
            ):
                p = one("dcim/interfaces", device=host, name=parent)
                sub = rw.create(
                    "dcim/interfaces",
                    {
                        "device": p["device"]["id"],
                        "name": f"{parent}.107",
                        "type": "virtual",
                        "parent": p["id"],
                        "mode": "tagged",
                        "tagged_vlans": [vlan["id"]],
                        "description": "Customer-A SVC-0007",
                    },
                )
                created.append(("dcim/interfaces", sub["id"]))
                subs[host] = sub
            for role, vc, hub in (("primary", 1007, "hub-a"), ("backup", 2007, "hub-b")):
                l2vpn = rw.create(
                    "vpn/l2vpns",
                    {
                        "name": f"SVC-0007-{role}",
                        "slug": f"svc-0007-{role}",
                        "type": "vpws",
                        "identifier": vc,
                        "tenant": tenant["id"],
                        "tags": [{"slug": "l2vpn-pw"}],
                        "custom_fields": {"service_id": "SVC-0007", "pw_role": role, "spoke_ac": subs["spoke-1"]["id"]},
                    },
                )
                created.append(("vpn/l2vpns", l2vpn["id"]))
                term = rw.create(
                    "vpn/l2vpn-terminations",
                    {
                        "l2vpn": l2vpn["id"],
                        "assigned_object_type": "dcim.interface",
                        "assigned_object_id": subs[hub]["id"],
                    },
                )
                created.append(("vpn/l2vpn-terminations", term["id"]))

            start = time.monotonic()
            result = sync()
            timings.record("netbox_change_sync_with_refresh_s", round(result.duration, 2), None)
            kinds = {(c.action, c.kind) for c in result.plan.changes}
            assert ("create", "service") in kinds and ("update", "macro") in kinds, result.plan.lines()
            names = {s["name"] for s in lab.api.call("service.get", {"output": ["name"]})}
            assert {"SVC-0007 (spoke-1)", "SVC-0007 primary (hub-a)", "SVC-0007 backup (hub-b)"} <= names
            ends = {("spoke-1", "primary"), ("spoke-1", "backup"), ("hub-a", "primary"), ("hub-b", "backup")}

            def missing_everywhere() -> bool:
                got = pw_problems(lab, service="SVC-0007", check="missing")
                return {(tags_of(p)["host"], tags_of(p)["pw_role"]) for p in got} == ends

            lab.wait_until(missing_everywhere, 90, 0.5, "missing on both ends of both SVC-0007 PWs")
            lab.wait_until(lambda: lab.service_status("SVC-0007 (spoke-1)") == DISASTER, 30, 0.5, "SVC-0007 Disaster")
            timings.record("netbox_new_service_sync_to_disaster_s", round(time.monotonic() - start, 2), None)
            assert sync().plan.changes == []  # one sync was enough: discovery ran, map links complete
        finally:
            for endpoint, ident in reversed(created):
                rw.delete(endpoint, ident)
            result = sync()  # always: Zabbix must forget SVC-0007 even if an assertion above failed
    deleted = {c.name for c in result.plan.changes if c.action == "delete" and c.kind == "service"}
    assert deleted == {"SVC-0007 (spoke-1)", "SVC-0007 primary (hub-a)", "SVC-0007 backup (hub-b)"}
    lab.wait_until(lambda: not pw_problems(lab, service="SVC-0007"), 60, 0.5, "SVC-0007 problems gone")
    spec = load_lab_spec("netbox").bsm
    macro = lab.api.get_one(
        "usermacro.get",
        {"output": ["value"], "hostids": [lab.hostids()["spoke-1"]], "filter": {"macro": [N.SPEC_MACRO]}},
    )
    assert macro["value"] == pw_spec_macro_value(spec, "spoke-1")
    assert sync().plan.changes == []


# ------------------------------------------------------------------------------------------------ traps
def trap_clock(lab: Lab, host: str, notification: str) -> float:
    """Timestamp of the last value of the trap item for ``notification`` (matched by its item name)."""
    item = next(i for i in lab.items(host, "snmptrap[").values() if f": {notification} " in i["name"])
    return int(item["lastclock"]) + int(item["lastns"]) / 1e9


def test_trap_kinds_land_in_their_items_and_raise_hints(lab: Lab) -> None:
    host = "spoke-4"
    t0 = time.time()
    # config change (description of the core uplink) and back: two ciscoConfigManEvent
    path = "netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F0/description"
    try:
        lab.restconf_put(host, path, {"netsim-device:description": "CORE (zabbix trap test)"})
    finally:
        lab.restore_golden(host)
    lab.wait_until(
        lambda: [p for p in lab.problems(trap="config") if int(p["clock"]) >= int(t0)], 20, 0.3, "config trap problem"
    )
    # coldStart: restart simd (new boot_id)
    subprocess.run(["docker", "exec", host, "supervisorctl", "restart", "simd"], check=True, capture_output=True)
    lab.wait_until(
        lambda: [p for p in lab.problems(trap="boot") if int(p["clock"]) >= int(t0)], 40, 0.3, "boot trap problem"
    )
    # an unknown trap -> fallback item
    subprocess.run(
        [
            "docker",
            "exec",
            host,
            "snmptrap",
            "-v",
            "2c",
            "-c",
            "netsim",
            "172.30.10.42:1162",
            "",
            "1.3.6.1.4.1.99999.0.1",
        ],
        check=True,
    )
    lab.wait_until(
        lambda: int(lab.items(host, "snmptrap.fallback")["snmptrap.fallback"]["lastclock"]) >= int(t0),
        20,
        0.3,
        "fallback trap item",
    )
    alerts = lab.api.call(
        "alert.get", {"output": ["status", "error", "message"], "time_from": int(t0), "hostids": [lab.hostids()[host]]}
    )
    assert alerts and all(a["status"] == "1" for a in alerts), alerts


def test_trap_to_check_now_latency(lab: Lab, timings: Timings) -> None:
    """cpwVcDown/cpwVcUp -> trap item -> trigger -> action -> webhook task.create -> walk polled.

    The two hosts' walk interval is raised to 1 h during the test (host macro), so every walk value seen within
    seconds of a trap comes from the check-now and not from the regular 60 s poll.
    """
    hosts = ("spoke-3", "hub-b")
    ids = lab.hostids()
    added = lab.api.call(
        "usermacro.create", [{"hostid": ids[h], "macro": "{$NETSIM.WALK.INTERVAL}", "value": "1h"} for h in hosts]
    )["hostmacroids"]
    samples: list[float] = []
    fault_to_trap: list[float] = []
    fault_to_walk: list[float] = []
    try:
        time.sleep(7)  # configuration cache picks up the new interval
        for trial in range(12):
            time.sleep(1.3 * (trial % 4))  # spread trials over the task manager's 5 s cycle
            before = walk_clock(lab, "spoke-3")
            t_fault = time.time()
            if trial % 2 == 0:
                lab.core.set(lab.cut_key("spoke-3", "hub-b"), "1")
            else:
                lab.core.delete(lab.cut_key("spoke-3", "hub-b"))
            trap = "cpwVcDown" if trial % 2 == 0 else "cpwVcUp"
            lab.wait_until(lambda b=before: walk_clock(lab, "spoke-3") > b, 20, 0.1, f"walk after {trap}")
            t_trap = trap_clock(lab, "spoke-3", trap)
            t_walk = walk_clock(lab, "spoke-3")
            samples.append(round(t_walk - t_trap, 2))
            fault_to_trap.append(round(t_trap - t_fault, 2))
            fault_to_walk.append(round(t_walk - t_fault, 2))
    finally:
        lab.core.delete(lab.cut_key("spoke-3", "hub-b"))
        lab.api.call("usermacro.delete", added)
    timings.record("trap_to_walk_lastclock_samples_s", samples, CHECK_NOW_BUDGET)
    timings.record("trap_to_walk_lastclock_median_s", round(statistics.median(samples), 2), CHECK_NOW_BUDGET)
    timings.record("trap_to_walk_lastclock_max_s", max(samples), CHECK_NOW_BUDGET)
    timings.record("fault_to_trap_in_zabbix_samples_s", fault_to_trap, None)
    timings.record("fault_to_walk_samples_s", fault_to_walk, None)
    assert statistics.median(samples) <= CHECK_NOW_BUDGET, samples
    # Zabbix 8.0's task manager handles "execute now" tasks on a fixed 5 s grid (ZBX_TM_PROCESS_PERIOD), so the
    # worst case is ~5 s + poll/preprocessing; anything slower is a regression.
    assert max(samples) <= 7.0, samples
