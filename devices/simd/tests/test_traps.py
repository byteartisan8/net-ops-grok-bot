"""Notifications follow contract/oids.yaml exactly; the batcher coalesces without ever losing a transition."""

from __future__ import annotations

import pytest
from netsim_simd.traps import Transition, TransitionBatcher, TrapBuilder, decode_trap, encode_v2c_trap, link_transition

from netsim_common.enums import IfOperStatus, PwOperStatus

UP, DOWN, LLD = PwOperStatus.up, PwOperStatus.down, PwOperStatus.lowerLayerDown


def test_pw_trap_varbinds(oids):
    trap = TrapBuilder(oids).pw_range(False, 3, 5, DOWN, 1234, ["a", "b", "c"])
    names = [vb.name for vb in trap.varbinds]
    assert names == ["sysUpTime", "snmpTrapOID", "cpwVcOperStatus", "cpwVcOperStatus"]
    assert trap.oid == oids.notification("cpwVcDown").oid
    assert [vb.oid for vb in trap.varbinds] == [
        oids.instance("sysUpTime"),
        oids.instance("snmpTrapOID"),
        oids.instance("cpwVcOperStatus", 3),
        oids.instance("cpwVcOperStatus", 5),
    ]
    assert [int(vb.value) for vb in trap.varbinds[2:]] == [oids.value("cpwVcOperStatus", "down")] * 2
    up = TrapBuilder(oids).pw_range(True, 7, 7, UP, 1, ["x"])
    assert up.oid == oids.notification("cpwVcUp").oid
    assert up.varbinds[2].oid == up.varbinds[3].oid == oids.instance("cpwVcOperStatus", 7)


def test_config_man_event_varbinds(oids):
    trap = TrapBuilder(oids).config_man_event(4, "restconf", 99)
    spec = oids.notification("ciscoConfigManEvent")
    assert trap.oid == spec.oid
    assert [vb.name for vb in trap.varbinds[2:]] == [o.name for o in spec.objects]
    assert [vb.oid for vb in trap.varbinds[2:]] == [o.instance(4) for o in spec.objects]
    values = {vb.name: vb.value for vb in trap.varbinds}
    assert int(values["ccmHistoryEventCommandSource"]) == oids.value("ccmHistoryEventCommandSource", "commandLine")
    assert int(values["ccmHistoryEventConfigSource"]) == oids.value("ccmHistoryEventConfigSource", "commandSource")
    assert int(values["ccmHistoryEventConfigDestination"]) == oids.value("ccmHistoryEventConfigDestination", "running")
    assert bytes(values["ccmHistoryEventTerminalUser"]) == b"restconf"


def test_link_trap_varbinds_are_the_mibs(oids):
    down = TrapBuilder(oids).link(False, 5, True, IfOperStatus.lowerLayerDown, 77, "GigabitEthernet0/0/1.101")
    assert down.name == "linkDown" and down.oid == oids.notification("linkDown").oid == "1.3.6.1.6.3.1.1.5.3"
    assert [vb.name for vb in down.varbinds] == ["sysUpTime", "snmpTrapOID", "ifIndex", "ifAdminStatus", "ifOperStatus"]
    assert [vb.oid for vb in down.varbinds[2:]] == [
        oids.instance("ifIndex", 5),
        oids.instance("ifAdminStatus", 5),
        oids.instance("ifOperStatus", 5),
    ]
    assert [int(vb.value) for vb in down.varbinds[2:]] == [5, 1, 7]
    assert down.interfaces == ("GigabitEthernet0/0/1.101",) and down.summary()["interfaces"] == [down.interfaces[0]]
    up = TrapBuilder(oids).link(True, 2, True, IfOperStatus.up, 78, "GigabitEthernet0/0/1")
    assert up.name == "linkUp" and up.oid == "1.3.6.1.6.3.1.1.5.4"
    assert [int(vb.value) for vb in up.varbinds[2:]] == [2, 1, 1]
    admin_down = TrapBuilder(oids).link(False, 3, False, IfOperStatus.down, 79, "GigabitEthernet0/0/2")
    assert [int(vb.value) for vb in admin_down.varbinds[2:]] == [3, 2, 2]
    decoded = decode_trap(encode_v2c_trap("netsim", down))
    assert decoded.value(oids.instance("snmpTrapOID")) == oids.notification("linkDown").oid
    assert decoded.value(oids.instance("ifOperStatus", 5)) == 7


IF_UP, IF_DOWN, IF_LLD = IfOperStatus.up, IfOperStatus.down, IfOperStatus.lowerLayerDown


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param(IF_UP, IF_DOWN, "linkDown", id="up -> down"),
        pytest.param(IF_UP, IF_LLD, "linkDown", id="up -> lowerLayerDown (parent down)"),
        pytest.param(IF_DOWN, IF_UP, "linkUp", id="down -> up"),
        pytest.param(IF_LLD, IF_UP, "linkUp", id="lowerLayerDown -> up"),
        pytest.param(IF_DOWN, IF_LLD, None, id="down -> lowerLayerDown: never was up"),
        pytest.param(IF_LLD, IF_DOWN, None, id="lowerLayerDown -> down"),
        pytest.param(IF_UP, IF_UP, None, id="no change"),
        pytest.param(None, IF_UP, "linkUp", id="new interface that comes up"),
        pytest.param(None, IF_DOWN, None, id="new interface that stays down"),
    ],
)
def test_link_transition_rule(old, new, expected):
    lt = link_transition("Gi", old, new, True)
    assert (None if lt is None else ("linkUp" if lt.up else "linkDown")) == expected


def test_cold_start_has_only_the_header(oids):
    trap = TrapBuilder(oids).cold_start(5)
    assert trap.oid == oids.notification("coldStart").oid
    assert [vb.name for vb in trap.varbinds] == ["sysUpTime", "snmpTrapOID"]


def test_v2c_encoding_round_trip(oids):
    trap = TrapBuilder(oids).pw_range(True, 1, 2, UP, 4242, ["a", "b"])
    decoded = decode_trap(encode_v2c_trap("netsim", trap, request_id=77))
    assert (decoded.version, decoded.community, decoded.request_id) == ("v2c", "netsim", 77)
    assert [oid for oid, _ in decoded.varbinds] == [vb.oid for vb in trap.varbinds]
    assert decoded.value(oids.instance("snmpTrapOID")) == oids.notification("cpwVcUp").oid
    assert decoded.value(oids.instance("sysUpTime")) == 4242


def t(index: int, new: PwOperStatus, old: PwOperStatus | None = None) -> Transition:
    return Transition(index, f"pw{index}", old, new)


def plans(*transitions: Transition) -> list[tuple[bool, int, int, int]]:
    batcher = TransitionBatcher()
    for tr in transitions:
        batcher.add(tr)
    return [(p.up, p.first, p.last, int(p.value)) for p in batcher.drain()]


@pytest.mark.parametrize(
    ("transitions", "expected"),
    [
        pytest.param([t(4, DOWN)], [(False, 4, 4, 2)], id="single"),
        pytest.param([t(1, LLD), t(2, LLD), t(3, LLD)], [(False, 1, 3, 7)], id="contiguous range"),
        pytest.param([t(3, DOWN), t(1, DOWN), t(2, DOWN)], [(False, 1, 3, 2)], id="order inside a batch"),
        pytest.param([t(1, DOWN), t(3, DOWN)], [(False, 1, 1, 2), (False, 3, 3, 2)], id="gap splits"),
        pytest.param([t(1, DOWN), t(2, LLD)], [(False, 1, 1, 2), (False, 2, 2, 7)], id="different values"),
        pytest.param([t(1, DOWN), t(2, UP)], [(False, 1, 1, 2), (True, 2, 2, 1)], id="down then up"),
        pytest.param(
            [t(1, DOWN), t(1, UP), t(1, DOWN)],
            [(False, 1, 1, 2), (True, 1, 1, 1), (False, 1, 1, 2)],
            id="flap is never suppressed",
        ),
        pytest.param([t(1, DOWN), t(1, DOWN)], [(False, 1, 1, 2), (False, 1, 1, 2)], id="same pw twice"),
    ],
)
def test_batcher(transitions, expected):
    assert plans(*transitions) == expected


def test_each_transition_is_covered_exactly_once():
    seq = [t(i % 7 + 1, [UP, DOWN, LLD][i % 3]) for i in range(40)]
    batcher = TransitionBatcher()
    for tr in seq:
        batcher.add(tr)
    covered = [name for plan in batcher.drain() for name in plan.pws]
    assert sorted(covered) == sorted(tr.name for tr in seq)
    assert batcher.drain() == []
