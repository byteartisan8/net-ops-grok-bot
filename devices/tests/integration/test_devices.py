"""Device integration scenarios (prompt 02, "Integration" 1-7) on real containers, with contract section 9 timings.

The tests run in file order and share one stack (core + hub-a + spoke-1 + trap sink); each scenario
leaves the devices as it found them.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest

pytestmark = pytest.mark.integration

UP, DOWN, LLD = 1, 2, 7
VC_ID = "netsim-device:l2vpn/xconnect=SVC-0001/pseudowire=primary/vc-id"
PRIMARY = "SVC-0001:primary"


def parse_docker_time(text):
    """Docker's RFC 3339 timestamp with nanoseconds -> epoch seconds."""
    whole, _, frac = text.rstrip("Z").partition(".")
    return datetime.fromisoformat(whole).replace(tzinfo=UTC).timestamp() + float("0." + (frac or "0"))


def traps_named(lab, name, since, source=None):
    return [
        t
        for t in lab.traps(since)
        if t.get("trap") == name and (source is None or t["source"].split(":")[0] == lab.mgmt_ip(source))
    ]


def trap_latency(lab, host, since, trap_name):
    """Seconds from the device's oper-change event to the trap's arrival at the sink (worst PW)."""
    worst = 0.0
    changes = [e for e in lab.events(host, since) if e["type"] == "oper-change"]
    for trap in traps_named(lab, trap_name, since, source=host):
        indexes = {vb["index"][0] for vb in trap["varbinds"] if vb["name"] == "cpwVcOperStatus"}
        for e in changes:
            if e["index"] in indexes and e["ts"] <= trap["received_at"]:
                worst = max(worst, trap["received_at"] - e["ts"])
    return worst


# ------------------------------------------------------------------------------------------- 1
def test_1_golden_boot_primary_up_on_both_ends(lab):
    lab.wait_until(lambda: lab.pw("spoke-1", "SVC-0001", "primary")["oper"] == "up", 30, what="spoke-1 primary up")
    lab.wait_until(lambda: lab.pw("hub-a", "SVC-0001", "primary")["oper"] == "up", 30, what="hub-a primary up")
    spoke, hub = lab.pw_rows("spoke-1"), lab.pw_rows("hub-a")
    assert sorted(spoke) == ["SVC-0001:backup", "SVC-0001:primary"]
    assert len(hub) == 6  # hub-a holds the primary of every service
    row = spoke[PRIMARY]
    assert (row["cpwVcOperStatus"], row["cpwVcID"], row["cpwVcPeerAddr"]) == (UP, 1001, "0x0aff0001")
    assert (row["cpwVcDescr"], row["cpwVcRemoteIfString"], row["cpwVcRemoteIfMtu"]) == (
        "Customer-A",
        "GigabitEthernet0/0/2.101",
        1500,
    )
    assert hub[PRIMARY]["cpwVcOperStatus"] == UP and hub[PRIMARY]["cpwVcPeerAddr"] == "0x0aff0101"
    # hub-b is not part of this stack: backups have no peer
    assert spoke["SVC-0001:backup"]["cpwVcOperStatus"] == DOWN
    assert lab.pw("spoke-1", "SVC-0001", "backup")["reason"] == "peer-unreachable"
    # every device announced itself with coldStart, from its mgmt address
    sources = {t["source"].split(":")[0] for t in lab.traps() if t.get("trap") == "coldStart"}
    assert {lab.mgmt_ip("hub-a"), lab.mgmt_ip("spoke-1")} <= sources


# ------------------------------------------------------------------------------------------- 2 + 3
def test_2_vcid_change_takes_both_ends_down_and_3_revert(lab):
    spoke_idx = lab.pw_rows("spoke-1")[PRIMARY]["index"]
    hub_idx = lab.pw_rows("hub-a")[PRIMARY]["index"]

    t0, wall0 = time.monotonic(), time.time()
    lab.restconf_put("spoke-1", VC_ID, {"netsim-device:vc-id": 1010})
    lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == DOWN, 3, what="spoke-1 SNMP down")
    lab.wait_until(lambda: lab.snmp_oper("hub-a", hub_idx) == DOWN, 3, what="hub-a SNMP down")
    lab.record("Config commit -> both ends' SNMP reflect new oper", 3.0, time.monotonic() - t0)
    for host in ("spoke-1", "hub-a"):
        pw = lab.pw(host, "SVC-0001", "primary")
        assert (pw["oper"], pw["reason"]) == ("down", "remote-not-configured"), host
    lab.wait_until(lambda: traps_named(lab, "ciscoConfigManEvent", wall0, "spoke-1"), 2, what="ciscoConfigManEvent")
    lab.wait_until(lambda: traps_named(lab, "cpwVcDown", wall0, "spoke-1"), 2, what="cpwVcDown from spoke-1")
    lab.wait_until(lambda: traps_named(lab, "cpwVcDown", wall0, "hub-a"), 2, what="cpwVcDown from hub-a")
    [ccm] = traps_named(lab, "ciscoConfigManEvent", wall0, "spoke-1")
    user = next(vb["value"] for vb in ccm["varbinds"] if vb["name"] == "ccmHistoryEventTerminalUser")
    assert user == "restconf"
    [down] = traps_named(lab, "cpwVcDown", wall0, "spoke-1")
    assert [vb["index"] for vb in down["varbinds"] if vb["name"] == "cpwVcOperStatus"] == [[spoke_idx], [spoke_idx]]
    assert not traps_named(lab, "ciscoConfigManEvent", wall0, "hub-a")  # hub-a's config did not change
    for host in ("spoke-1", "hub-a"):
        lab.record("Oper transition -> trap sent (received)", 1.0, trap_latency(lab, host, wall0, "cpwVcDown"))
    lab.record(
        "Config commit -> ciscoConfigManEvent received",
        1.0,
        traps_named(lab, "ciscoConfigManEvent", wall0, "spoke-1")[0]["received_at"] - wall0,
    )

    # 3: revert
    t0, wall0 = time.monotonic(), time.time()
    lab.restconf_put("spoke-1", VC_ID, {"netsim-device:vc-id": 1001})
    lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == UP, 3, what="spoke-1 SNMP up")
    lab.wait_until(lambda: lab.snmp_oper("hub-a", hub_idx) == UP, 3, what="hub-a SNMP up")
    lab.record("Config commit -> both ends' SNMP reflect new oper", 3.0, time.monotonic() - t0)
    lab.wait_until(lambda: traps_named(lab, "cpwVcUp", wall0, "spoke-1"), 2, what="cpwVcUp from spoke-1")
    lab.wait_until(lambda: traps_named(lab, "cpwVcUp", wall0, "hub-a"), 2, what="cpwVcUp from hub-a")
    for host in ("spoke-1", "hub-a"):
        lab.record("Oper transition -> trap sent (received)", 1.0, trap_latency(lab, host, wall0, "cpwVcUp"))
    assert lab.pw("spoke-1", "SVC-0001", "primary")["reason"] == "ok"


# ------------------------------------------------------------------------------------------- 4
def test_4_uplink_fault_is_physical_not_config(lab):
    hashes = {h: lab.state(h)["config_hash"] for h in ("spoke-1", "hub-a")}
    spoke_idx = lab.pw_rows("spoke-1")[PRIMARY]["index"]
    t0, wall0 = time.monotonic(), time.time()
    lab.core("SET", "netsim:fault:uplink:hub-a", "1")
    try:
        lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == DOWN, 3, what="spoke-1 down")
        lab.record("Fault key set -> peer PW down in SNMP", None, time.monotonic() - t0)
        pw = lab.pw("spoke-1", "SVC-0001", "primary")
        assert (pw["oper"], pw["reason"]) == ("down", "peer-unreachable")
        hub = lab.pw("hub-a", "SVC-0001", "primary")
        assert (hub["oper"], hub["reason"]) == ("lowerLayerDown", "local-uplink-down")
        assert lab.state("hub-a")["uplink"] == {
            "admin": "up",
            "oper": "down",
            "fault": True,
            "interfaces": ["GigabitEthernet0/0/0"],
        }
        assert {h: lab.state(h)["config_hash"] for h in hashes} == hashes  # no config changed
        time.sleep(0.5)
        assert not traps_named(lab, "ciscoConfigManEvent", wall0)
        # hub-a: all six PWs went lowerLayerDown together -> one cpwVcDown for the whole index range
        lab.wait_until(lambda: traps_named(lab, "cpwVcDown", wall0, "hub-a"), 2, what="cpwVcDown from hub-a")
        [hub_trap] = traps_named(lab, "cpwVcDown", wall0, "hub-a")
        bounds = [vb["index"][0] for vb in hub_trap["varbinds"] if vb["name"] == "cpwVcOperStatus"]
        assert bounds == [1, 6] and {vb["value"] for vb in hub_trap["varbinds"][2:]} == {LLD}
    finally:
        lab.core("DEL", "netsim:fault:uplink:hub-a")
    lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == UP, 3, what="spoke-1 back up")


# ------------------------------------------------------------------------------------------- 5
def test_5_power_off_and_on_preserves_config(lab):
    spoke_idx = lab.pw_rows("spoke-1")[PRIMARY]["index"]
    # drift on hub-a that must survive the restart
    lab.restconf_put(
        "hub-a", "netsim-device:l2vpn/xconnect=SVC-0003/description", {"netsim-device:description": "drift"}
    )
    before = lab.state("hub-a")
    t0 = time.monotonic()
    lab.docker("stop", lab.container("hub-a"))  # SIGTERM: simd stops without withdrawing its advert
    lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == DOWN, 4.5, what="spoke-1 down after power-off")
    detected = time.time()
    lab.record("Device power off (docker stop issued) -> peers' PWs down", 4.0, time.monotonic() - t0)
    finished = lab.docker("inspect", "-f", "{{.State.FinishedAt}}", lab.container("hub-a")).stdout.strip()
    lab.record("  ... of which after the container exited", None, detected - parse_docker_time(finished))
    assert lab.pw("spoke-1", "SVC-0001", "primary")["reason"] == "peer-unreachable"
    assert "hub-a" not in {n["hostname"] for n in httpx.get(f"{lab.simd('spoke-1')}/neighbors").json()}

    t0 = time.monotonic()
    lab.docker("start", lab.container("hub-a"))
    lab.discover_ports()  # ephemeral ports are re-assigned on start
    lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == UP, 30, what="spoke-1 up after power-on")
    lab.record("Device power on -> peers' PWs up", None, time.monotonic() - t0)
    after = lab.state("hub-a")
    assert after["boot_id"] != before["boot_id"]
    assert after["config_hash"] == before["config_hash"]
    assert lab.restconf_get("hub-a", "netsim-device:l2vpn/xconnect=SVC-0003/description") == {
        "netsim-device:description": "drift"
    }
    assert [p["index"] for p in after["pseudowires"]] == [p["index"] for p in before["pseudowires"]]
    lab.restconf_delete("hub-a", "netsim-device:l2vpn/xconnect=SVC-0003/description")
    lab.wait_healthy()


# ------------------------------------------------------------------------------------------- 6
def test_6_delete_and_readd_pw_reuses_index(lab):
    backup = "SVC-0001:backup"
    index = lab.pw_rows("spoke-1")[backup]["index"]
    lab.restconf_delete("spoke-1", "netsim-device:l2vpn/xconnect=SVC-0001/pseudowire=backup")
    lab.wait_until(lambda: backup not in lab.pw_rows("spoke-1"), 3, what="row withdrawn")
    [vb] = lab.snmp_get("spoke-1", lab.oids.instance("cpwVcName", index))
    assert vb.end
    lab.restconf_put(
        "spoke-1",
        "netsim-device:l2vpn/xconnect=SVC-0001/pseudowire=backup",
        {"netsim-device:pseudowire": [{"role": "backup", "peer-ipv4": "10.255.0.2", "vc-id": 2001, "enabled": True}]},
    )
    lab.wait_until(lambda: backup in lab.pw_rows("spoke-1"), 3, what="row back")
    assert lab.pw_rows("spoke-1")[backup]["index"] == index


# ------------------------------------------------------------------------------------------- 7
def test_7_cli_change_is_handled_like_restconf(lab):
    spoke_idx = lab.pw_rows("spoke-1")[PRIMARY]["index"]
    hub_idx = lab.pw_rows("hub-a")[PRIMARY]["index"]
    wall0 = time.time()
    out = lab.cli("spoke-1", "set l2vpn xconnect SVC-0001 pseudowire primary enabled false", "commit")
    assert "error" not in out.lower(), out
    lab.wait_until(lambda: lab.snmp_oper("spoke-1", spoke_idx) == DOWN, 3, what="spoke-1 down")
    lab.wait_until(lambda: lab.snmp_oper("hub-a", hub_idx) == DOWN, 3, what="hub-a down")
    assert lab.pw("spoke-1", "SVC-0001", "primary")["reason"] == "admin-down"
    assert lab.pw("hub-a", "SVC-0001", "primary")["reason"] == "remote-admin-down"
    change = [e for e in lab.events("spoke-1", wall0) if e["type"] == "config-change"]
    assert change and change[-1]["source"] == "notification" and change[-1]["changed_by"] == "root"
    assert [d["path"] for d in change[-1]["diff"]] == ["l2vpn/xconnect[SVC-0001]/pseudowire[primary]/enabled"]
    lab.wait_until(lambda: traps_named(lab, "ciscoConfigManEvent", wall0, "spoke-1"), 2, what="ccm from CLI")
    [ccm] = traps_named(lab, "ciscoConfigManEvent", wall0, "spoke-1")
    assert next(vb["value"] for vb in ccm["varbinds"] if vb["name"] == "ccmHistoryEventTerminalUser") == "root"
    show = lab.cli("spoke-1", "show l2vpn")
    assert "admin-down" in show and "SVC-0001" in show
    lab.cli("spoke-1", "set l2vpn xconnect SVC-0001 pseudowire primary enabled true", "commit")
    lab.wait_until(lambda: lab.snmp_oper("hub-a", hub_idx) == UP, 3, what="hub-a back up")


# ------------------------------------------------------------------------------------------- report
def test_zz_timing_report(lab, timings):
    lines = ["", "Device timings (contract section 9 budget; worst case over the runs above):"]
    lines.append(f"  {'path':<55} {'budget':>8} {'measured':>9}  result")
    failed = []
    for path, t in timings.items():
        budget = t["budget"]
        ok = budget is None or t["measured"] <= budget
        lines.append(
            f"  {path:<55} {('<= ' + format(budget, 'g') + ' s') if budget else '-':>8} {t['measured']:>8.3f}s  "
            + ("ok" if ok else "OVER BUDGET")
        )
        if not ok:
            failed.append(path)
    print("\n".join(lines))
    assert not failed, f"over budget: {failed}"
