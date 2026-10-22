"""Two in-process devices (hub-a, spoke-1) on an in-memory bus: the daemon's recompute/apply/trap/SNMP path.

These are the integration scenarios of the prompt without containers, so they run in ``make test``.
"""

from __future__ import annotations

import asyncio

from netsim_simd.snmpdb import SnmpDbReader

from netsim_common.enums import PwReason
from netsim_common.signaling import advert_key, cut_key, uplink_fault_key
from netsim_common.topology import load_topology


def run(coro):
    return asyncio.run(coro)


def set_vc(service: str, role: str, vc_id: int):
    def mutate(payload):
        for xc in payload["netsim-device:l2vpn"]["xconnect"]:
            if xc["service-id"] == service:
                for p in xc["pseudowire"]:
                    if p["role"] == role:
                        p["vc-id"] = vc_id

    return mutate


def test_golden_pair_comes_up(make_device, settle, pw_state):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        assert (pw_state(spoke, "SVC-0001", "primary").oper, pw_state(hub, "SVC-0001", "primary").oper) == ("up", "up")
        primary = pw_state(spoke, "SVC-0001", "primary")
        assert primary.reason is PwReason.OK and primary.rule == 10
        assert (primary.remote.hostname, primary.remote.ac_name, primary.remote.mtu) == (
            "hub-a",
            "GigabitEthernet0/0/2.101",
            1500,
        )
        # hub-b is not running: the backup has no peer
        assert pw_state(spoke, "SVC-0001", "backup").reason is PwReason.PEER_UNREACHABLE
        # the other services of hub-a point at spokes that are not running
        assert pw_state(hub, "SVC-0002", "primary").reason is PwReason.PEER_UNREACHABLE
        # SNMP rows are written; indexes are stable and persisted (golden rows numbered by service, primary first)
        expected = [f"{pw.service_id}:{pw.role.value}" for pw in load_topology().pseudowires_on("spoke-1")]
        assert expected == ["SVC-0001:primary", "SVC-0001:backup", "SVC-0008:primary", "SVC-0008:backup"]
        assert [p.index for p in spoke.state().pseudowires] == list(range(1, len(expected) + 1))
        names = [v for v in SnmpDbReader(spoke.s.snmp_db).all() if v.oid.startswith(spoke.oids.oid("cpwVcName") + ".")]
        assert [bytes.fromhex(v.value).decode() for v in names] == expected

    run(scenario())


def test_vcid_change_then_revert(make_device, settle, change_config, pw_state, oids):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        hub.sender.sent.clear()
        spoke.sender.sent.clear()

        await change_config(spoke, set_vc("SVC-0001", "primary", 1010))
        await settle(hub, spoke)
        for dev in (spoke, hub):
            p = pw_state(dev, "SVC-0001", "primary")
            assert (p.oper, p.reason) == ("down", PwReason.REMOTE_NOT_CONFIGURED)
        assert spoke.sender.names() == ["ciscoConfigManEvent", "cpwVcDown"]
        assert hub.sender.names() == ["cpwVcDown"]
        ccm = spoke.sender.sent[0]
        assert ccm.varbinds[-1].name == "ccmHistoryEventTerminalUser" and bytes(ccm.varbinds[-1].value) == b"restconf"
        down = spoke.sender.sent[1]
        assert down.varbinds[2].oid == down.varbinds[3].oid == oids.instance("cpwVcOperStatus", 1)
        change = [e for e in spoke.events.since() if e["type"] == "config-change"][-1]
        assert change["changed_by"] == "restconf"
        assert [d["message"] for d in change["diff"]] == [
            "l2vpn/xconnect[SVC-0001]/pseudowire[primary]/vc-id: expected 1001, got 1010"
        ]

        await change_config(spoke, set_vc("SVC-0001", "primary", 1001))
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").oper == pw_state(hub, "SVC-0001", "primary").oper == "up"
        assert spoke.sender.names()[-2:] == ["ciscoConfigManEvent", "cpwVcUp"]
        assert hub.sender.names()[-1] == "cpwVcUp"

    run(scenario())


def test_uplink_fault_is_not_a_config_change(make_device, settle, store, pw_state):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        digest = hub.config_digest
        hub.sender.sent.clear()
        store.set(uplink_fault_key("hub-a"), "1")
        await settle(hub, spoke)
        p = pw_state(spoke, "SVC-0001", "primary")
        assert (p.oper, p.reason) == ("down", PwReason.PEER_UNREACHABLE)
        h = pw_state(hub, "SVC-0001", "primary")
        assert (h.oper, h.reason) == ("lowerLayerDown", PwReason.LOCAL_UPLINK_DOWN)
        assert hub.config_digest == digest
        assert "ciscoConfigManEvent" not in hub.sender.names()
        state = hub.state()
        assert (state.uplink.oper, state.uplink.fault, state.faults.uplink) == ("down", True, True)
        # the core uplink left up: one linkDown (the cause, sent first); then every hub PW went lowerLayerDown at
        # once: one range notification (MIB "contiguous indexes")
        n = len(load_topology().pseudowires_on("hub-a"))
        link, trap = hub.sender.sent
        assert (link.name, link.interfaces) == ("linkDown", ("GigabitEthernet0/0/0",))
        assert [int(vb.value) for vb in link.varbinds[3:]] == [1, 2]  # ifAdminStatus up, ifOperStatus down
        assert trap.name == "cpwVcDown" and len(trap.pws) == n == 8
        assert [vb.oid.rsplit(".", 1)[1] for vb in trap.varbinds[2:]] == ["1", str(n)]
        assert {e["type"] for e in hub.events.since()} >= {"fault-change", "oper-change", "if-oper-change"}
        # a value other than "1" is not a fault
        store.set(uplink_fault_key("hub-a"), "0")
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").oper == "up"

    run(scenario())


def test_cut_between_the_pair(make_device, settle, store, pw_state):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        store.set(cut_key("spoke-1", "hub-a"), "1")
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").reason is PwReason.PEER_UNREACHABLE
        assert pw_state(hub, "SVC-0001", "primary").reason is PwReason.PEER_UNREACHABLE
        assert hub.state().faults.cuts == ["spoke-1"]
        store.delete(cut_key("hub-a", "spoke-1"))
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").oper == "up"

    run(scenario())


def test_ac_fault_and_remote_ac_down(make_device, settle, store, pw_state):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        store.set("netsim:fault:ac:spoke-1:GigabitEthernet0/0/1", "1")  # the physical access interface
        await settle(hub, spoke)
        s = pw_state(spoke, "SVC-0001", "primary")
        assert (s.oper, s.reason) == ("lowerLayerDown", PwReason.AC_DOWN)
        h = pw_state(hub, "SVC-0001", "primary")
        assert (h.oper, h.reason) == ("down", PwReason.REMOTE_AC_DOWN)
        ifs = {i.name: i for i in spoke.state().interfaces}
        assert ifs["GigabitEthernet0/0/1"].oper == "down" and ifs["GigabitEthernet0/0/1.101"].oper == "lowerLayerDown"

    run(scenario())


def test_power_off_peer(make_device, settle, store, pw_state):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        store.delete(advert_key("hub-a"))  # hub-a stopped: its advert expired
        await settle(spoke)
        assert pw_state(spoke, "SVC-0001", "primary").reason is PwReason.PEER_UNREACHABLE
        assert [n.hostname for n in spoke.neighbors()] == []
        assert any(e["type"] == "neighbor-down" and e["neighbor"] == "hub-a" for e in spoke.events.since())
        # hub-a boots again: new process, same volume (indexes) and running config
        hub2 = await make_device("hub-a")
        await settle(hub2, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").oper == "up"
        assert pw_state(hub2, "SVC-0001", "primary").index == pw_state(hub, "SVC-0001", "primary").index

    run(scenario())


def test_delete_and_readd_reuses_index(make_device, settle, change_config, pw_state):
    def drop_backup(payload):
        for xc in payload["netsim-device:l2vpn"]["xconnect"]:
            if xc["service-id"] == "SVC-0001":
                xc["pseudowire"] = [p for p in xc["pseudowire"] if p["role"] != "backup"]

    def add_backup(payload):
        for xc in payload["netsim-device:l2vpn"]["xconnect"]:
            if xc["service-id"] == "SVC-0001":
                xc["pseudowire"].append({"role": "backup", "peer-ipv4": "10.255.0.2", "vc-id": 2001, "enabled": True})

    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        backup_index = pw_state(spoke, "SVC-0001", "backup").index
        reader = SnmpDbReader(spoke.s.snmp_db)
        col = spoke.oids.oid("cpwVcName")

        await change_config(spoke, drop_backup)
        await settle(hub, spoke)
        assert [p.role.value for p in spoke.state().pseudowires if p.service_id == "SVC-0001"] == ["primary"]
        assert reader.get(f"{col}.{backup_index}") is None
        assert any(e["type"] == "pw-removed" for e in spoke.events.since())

        await change_config(spoke, add_backup)
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "backup").index == backup_index
        assert bytes.fromhex(reader.get(f"{col}.{backup_index}").value) == b"SVC-0001:backup"

    run(scenario())


def test_mtu_mismatch_both_ends(make_device, settle, change_config, pw_state):
    def hub_access_mtu(payload):
        for i in payload["netsim-device:interfaces"]["interface"]:
            if i["name"] == "GigabitEthernet0/0/2":
                i["mtu"] = 1600

    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        await change_config(hub, hub_access_mtu)
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").reason is PwReason.MTU_MISMATCH
        assert pw_state(hub, "SVC-0001", "primary").reason is PwReason.MTU_MISMATCH
        assert pw_state(hub, "SVC-0001", "primary").local_mtu == 1600

    run(scenario())


def test_core_down_and_back(make_device, settle, pw_state):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        spoke.bus.down = True
        spoke.advert_ok_at = None  # pretend the hold time passed without a successful refresh
        await settle(spoke)
        assert pw_state(spoke, "SVC-0001", "primary").reason is PwReason.PEER_UNREACHABLE
        assert spoke.health().checks["core"] is False
        spoke.bus.down = False
        await settle(hub, spoke)
        assert pw_state(spoke, "SVC-0001", "primary").oper == "up"

    run(scenario())


def test_no_duplicate_traps_on_repeated_recompute(make_device, settle, change_config):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        await change_config(spoke, set_vc("SVC-0001", "primary", 1010))
        await settle(hub, spoke, rounds=5)
        assert spoke.sender.names().count("cpwVcDown") == 1
        assert hub.sender.names().count("cpwVcDown") == 1

    run(scenario())


def test_baseline_sends_no_pw_traps_and_new_up_pw_does(make_device, settle, change_config):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        assert "cpwVcUp" in hub.sender.names() or "cpwVcUp" in spoke.sender.names()  # came up after baseline
        first = await make_device("spoke-1")  # restart with peers already up: baseline is up, no PW trap
        await settle(first)
        assert first.sender.names() == []

    run(scenario())


def test_parent_port_down_sends_one_link_trap_per_interface(make_device, settle, store, oids):
    async def scenario():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        assert not [n for n in spoke.sender.names() if n.startswith("link")]  # no link traps for the baseline
        spoke.sender.sent.clear()
        store.set("netsim:fault:ac:spoke-1:GigabitEthernet0/0/1", "1")  # the physical access interface
        await settle(hub, spoke)
        links = [t for t in spoke.sender.sent if t.name.startswith("link")]
        index = spoke.indexes.if_map
        got = [(t.name, t.interfaces[0], int(t.varbinds[2].value), int(t.varbinds[4].value)) for t in links]
        assert got == [
            ("linkDown", "GigabitEthernet0/0/1", index["GigabitEthernet0/0/1"], 2),
            ("linkDown", "GigabitEthernet0/0/1.101", index["GigabitEthernet0/0/1.101"], 7),
            ("linkDown", "GigabitEthernet0/0/1.502", index["GigabitEthernet0/0/1.502"], 7),
        ]
        assert spoke.sender.names()[: len(links)] == ["linkDown"] * 3  # the cause goes out before the PW traps
        sent = [e for e in spoke.events.since() if e["type"] == "trap-sent" and e["name"] == "linkDown"]
        assert [e["interfaces"] for e in sent] == [[name] for _, name, _, _ in got]
        spoke.sender.sent.clear()
        store.delete("netsim:fault:ac:spoke-1:GigabitEthernet0/0/1")
        await settle(hub, spoke)
        assert [(t.name, t.interfaces[0]) for t in spoke.sender.sent if t.name.startswith("link")] == [
            ("linkUp", "GigabitEthernet0/0/1"),
            ("linkUp", "GigabitEthernet0/0/1.101"),
            ("linkUp", "GigabitEthernet0/0/1.502"),
        ]
        spoke.sender.sent.clear()
        await settle(hub, spoke, rounds=4)  # nothing changes: no link trap is sent again
        assert not [t for t in spoke.sender.sent if t.name.startswith("link")]

    run(scenario())


def test_subinterface_fault_and_new_subinterface(make_device, settle, store, change_config):
    def add_subinterface(payload):
        payload["netsim-device:interfaces"]["interface"].append(
            {
                "name": "GigabitEthernet0/0/1.999",
                "type": "subinterface",
                "parent": "GigabitEthernet0/0/1",
                "vlan-id": 999,
                "enabled": True,
            }
        )

    async def scenario():
        spoke = await make_device("spoke-1")
        await settle(spoke)
        spoke.sender.sent.clear()
        store.set("netsim:fault:ac:spoke-1:GigabitEthernet0/0/1.101", "1")  # only the subinterface
        await settle(spoke)
        links = [
            (t.name, t.interfaces[0], int(t.varbinds[4].value)) for t in spoke.sender.sent if t.name.startswith("link")
        ]
        assert links == [("linkDown", "GigabitEthernet0/0/1.101", 2)]
        spoke.sender.sent.clear()
        await change_config(spoke, add_subinterface)  # a new interface that comes up: linkUp (as for PW rows)
        await settle(spoke)
        names = [(t.name, t.interfaces) for t in spoke.sender.sent]
        assert ("linkUp", ("GigabitEthernet0/0/1.999",)) in names and names[0][0] == "ciscoConfigManEvent"

    run(scenario())
