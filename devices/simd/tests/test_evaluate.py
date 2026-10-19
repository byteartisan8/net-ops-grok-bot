"""Table-driven tests of contract section 7: interfaces, reachability and every row of the evaluation table."""

from __future__ import annotations

from typing import Any

import pytest
from netsim_simd.evaluate import (
    LocalFaults,
    PeerView,
    evaluate_pseudowires,
    interface_states,
    reachable,
    uplink_state,
)

from netsim_common.device_config import DeviceConfig
from netsim_common.enums import IfOperStatus, PwOperStatus, PwReason
from netsim_common.signaling import Advert

A_LO, B_LO = "10.0.0.1", "10.0.0.2"
UPLINK, ACCESS, AC = "Gi0/0/0", "Gi0/0/1", "Gi0/0/1.100"


def config(
    *,
    uplink_enabled: bool = True,
    access_enabled: bool = True,
    ac_enabled: bool = True,
    access_mtu: int | None = 1500,
    ac_mtu: int | None = None,
    xc_enabled: bool = True,
    pw_enabled: bool = True,
    peer: str = B_LO,
    vc_id: int = 100,
    loopback: str | None = A_LO,
    extra_xconnects: list[dict[str, Any]] | None = None,
) -> DeviceConfig:
    access: dict[str, Any] = {"name": ACCESS, "type": "access", "enabled": access_enabled}
    if access_mtu is not None:
        access["mtu"] = access_mtu
    sub: dict[str, Any] = {"name": AC, "type": "subinterface", "parent": ACCESS, "vlan-id": 100, "enabled": ac_enabled}
    if ac_mtu is not None:
        sub["mtu"] = ac_mtu
    system: dict[str, Any] = {"hostname": "a"}
    if loopback:
        system["loopback-ipv4"] = loopback
    xconnects = [
        {
            "service-id": "SVC-0001",
            "customer": "Customer-A",
            "attachment-circuit": AC,
            "enabled": xc_enabled,
            "pseudowire": [{"role": "primary", "peer-ipv4": peer, "vc-id": vc_id, "enabled": pw_enabled}],
        },
        *(extra_xconnects or []),
    ]
    return DeviceConfig.from_restconf(
        {
            "netsim-device:system": system,
            "netsim-device:interfaces": {
                "interface": [
                    {"name": UPLINK, "type": "core-uplink", "mtu": 9000, "enabled": uplink_enabled},
                    access,
                    sub,
                ]
            },
            "netsim-device:l2vpn": {"xconnect": xconnects},
        }
    )


def remote(
    *,
    host: str = "b",
    loopback: str = B_LO,
    uplink: bool = True,
    peer: str = A_LO,
    vc_id: int = 100,
    enabled: bool = True,
    ac_oper_up: bool = True,
    mtu: int = 1500,
    service_id: str = "SVC-0001",
    role: str = "primary",
    pws: list[dict[str, Any]] | None = None,
) -> Advert:
    entry = {
        "service_id": service_id,
        "role": role,
        "peer": peer,
        "vc_id": vc_id,
        "enabled": enabled,
        "ac_oper_up": ac_oper_up,
        "mtu": mtu,
        "ac_name": "Gi0/0/2.100",
    }
    return Advert(
        hostname=host,
        loopback=loopback,
        mgmt_ip="172.30.10.99",
        boot_id="b00t",
        seq=1,
        uplink_oper_up=uplink,
        pws=[entry] if pws is None else pws,
    )


def evaluate(cfg: DeviceConfig, adverts: list[Advert], *, faults: LocalFaults | None = None, own_fresh: bool = True):
    faults = faults or LocalFaults()
    ifs = interface_states(cfg, faults)
    up = uplink_state(cfg, ifs, faults)
    view = PeerView(adverts={a.hostname: a for a in adverts}, own_fresh=own_fresh, cut_peers=faults.cut_peers)
    return evaluate_pseudowires(
        hostname="a", loopback=cfg.system.loopback_ipv4, config=cfg, ifstates=ifs, uplink=up, view=view
    )


# One case per row of the contract table, plus the alternative triggers of each row.
CASES: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], PwReason]] = [
    ("1 xconnect disabled", {"xc_enabled": False}, {}, {}, PwReason.ADMIN_DOWN),
    ("1 pw disabled", {"pw_enabled": False}, {}, {}, PwReason.ADMIN_DOWN),
    (
        "1 wins over everything",
        {"pw_enabled": False, "ac_enabled": False},
        {"uplink": False},
        {"uplink": True},
        PwReason.ADMIN_DOWN,
    ),
    ("3 AC disabled", {"ac_enabled": False}, {}, {}, PwReason.AC_DOWN),
    ("3 AC parent disabled", {"access_enabled": False}, {}, {}, PwReason.AC_DOWN),
    ("3 AC fault", {}, {}, {"interfaces": frozenset({AC})}, PwReason.AC_DOWN),
    ("3 AC parent fault", {}, {}, {"interfaces": frozenset({ACCESS})}, PwReason.AC_DOWN),
    ("3 wins over 4", {"ac_enabled": False, "uplink_enabled": False}, {}, {}, PwReason.AC_DOWN),
    ("4 uplink disabled", {"uplink_enabled": False}, {}, {}, PwReason.LOCAL_UPLINK_DOWN),
    ("4 uplink fault", {}, {}, {"uplink": True}, PwReason.LOCAL_UPLINK_DOWN),
    ("4 uplink interface fault", {}, {}, {"interfaces": frozenset({UPLINK})}, PwReason.LOCAL_UPLINK_DOWN),
    ("5 no advert for the peer", {"peer": "10.0.0.9"}, {}, {}, PwReason.PEER_UNREACHABLE),
    ("5 peer uplink down", {}, {"uplink": False}, {}, PwReason.PEER_UNREACHABLE),
    ("5 core path cut", {}, {}, {"cut_peers": frozenset({"b"})}, PwReason.PEER_UNREACHABLE),
    ("5 peer is our own loopback", {"peer": A_LO}, {"peer": A_LO}, {}, PwReason.PEER_UNREACHABLE),
    ("6 peer has another vc-id", {}, {"vc_id": 101}, {}, PwReason.REMOTE_NOT_CONFIGURED),
    ("6 peer points elsewhere", {}, {"peer": "10.0.0.7"}, {}, PwReason.REMOTE_NOT_CONFIGURED),
    ("6 peer has no PW at all", {}, {"pws": []}, {}, PwReason.REMOTE_NOT_CONFIGURED),
    ("7 remote PW disabled", {}, {"enabled": False}, {}, PwReason.REMOTE_ADMIN_DOWN),
    ("7 wins over 8 and 9", {}, {"enabled": False, "ac_oper_up": False, "mtu": 1400}, {}, PwReason.REMOTE_ADMIN_DOWN),
    ("8 remote AC down", {}, {"ac_oper_up": False}, {}, PwReason.REMOTE_AC_DOWN),
    ("8 wins over 9", {}, {"ac_oper_up": False, "mtu": 1400}, {}, PwReason.REMOTE_AC_DOWN),
    ("9 remote MTU differs", {}, {"mtu": 1400}, {}, PwReason.MTU_MISMATCH),
    ("9 local AC MTU override", {"ac_mtu": 9000}, {}, {}, PwReason.MTU_MISMATCH),
    ("9 inherited parent MTU", {"access_mtu": 1400}, {"mtu": 1400}, {}, PwReason.OK),
    ("9 physical default 1500", {"access_mtu": None}, {"mtu": 1500}, {}, PwReason.OK),
    ("10 everything fine", {}, {}, {}, PwReason.OK),
    ("10 service-id/role differ remotely", {}, {"service_id": "SVC-0999", "role": "backup"}, {}, PwReason.OK),
]


@pytest.mark.parametrize(
    ("cfg_kw", "adv_kw", "fault_kw", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_evaluation_table(cfg_kw, adv_kw, fault_kw, expected):
    [result] = evaluate(config(**cfg_kw), [remote(**adv_kw)], faults=LocalFaults(**fault_kw))
    assert result.reason is expected
    assert result.oper is expected.oper_status


def test_reason_oper_mapping_matches_contract():
    lower = {PwReason.AC_DOWN, PwReason.LOCAL_UPLINK_DOWN}
    for reason in PwReason:
        want = (
            PwOperStatus.up
            if reason is PwReason.OK
            else (PwOperStatus.lowerLayerDown if reason in lower else PwOperStatus.down)
        )
        assert reason.oper_status is want


def test_rule_2_duplicate_pw_across_xconnects():
    dup = {
        "service-id": "SVC-0002",
        "attachment-circuit": AC,
        "pseudowire": [{"role": "backup", "peer-ipv4": B_LO, "vc-id": 100}],
    }
    results = evaluate(config(extra_xconnects=[dup]), [remote()])
    assert [(r.pw.service_id, r.reason) for r in results] == [
        ("SVC-0001", PwReason.DUPLICATE_PW),
        ("SVC-0002", PwReason.DUPLICATE_PW),
    ]


def test_rule_1_beats_rule_2():
    dup = {
        "service-id": "SVC-0002",
        "attachment-circuit": AC,
        "enabled": False,
        "pseudowire": [{"role": "backup", "peer-ipv4": B_LO, "vc-id": 100}],
    }
    reasons = {r.pw.service_id: r.reason for r in evaluate(config(extra_xconnects=[dup]), [remote()])}
    assert reasons == {"SVC-0001": PwReason.DUPLICATE_PW, "SVC-0002": PwReason.ADMIN_DOWN}


def test_own_advert_not_fresh_means_unreachable():
    [result] = evaluate(config(), [remote()], own_fresh=False)
    assert result.reason is PwReason.PEER_UNREACHABLE


def test_no_loopback_means_no_signaling():
    [result] = evaluate(config(loopback=None), [remote()], own_fresh=False)
    assert result.reason is PwReason.PEER_UNREACHABLE and result.remote is None


def test_remote_info_comes_from_the_matching_pw():
    [result] = evaluate(config(), [remote(mtu=1500, service_id="SVC-0042", role="backup")])
    assert result.remote is not None
    assert (result.remote.hostname, result.remote.mtu, result.remote.ac_name) == ("b", 1500, "Gi0/0/2.100")
    assert (result.remote.service_id, result.remote.role.value) == ("SVC-0042", "backup")
    assert result.peer_hostname == "b"


def test_remote_info_absent_when_unreachable():
    [result] = evaluate(config(), [remote(uplink=False)])
    assert result.remote is None and result.peer_hostname == "b"


def test_peer_chosen_by_hostname_when_loopbacks_collide():
    good = remote(host="b1")
    bad = remote(host="b2", vc_id=999)
    [result] = evaluate(config(), [bad, good])
    assert result.reason is PwReason.OK and result.peer_hostname == "b1"


@pytest.mark.parametrize(
    ("own_fresh", "own_uplink", "peer_uplink", "cut", "expected"),
    [
        (True, True, True, False, True),
        (False, True, True, False, False),
        (True, False, True, False, False),
        (True, True, False, False, False),
        (True, True, True, True, False),
    ],
)
def test_reachability(own_fresh, own_uplink, peer_uplink, cut, expected):
    peer = remote(uplink=peer_uplink)
    view = PeerView(adverts={"b": peer}, own_fresh=own_fresh, cut_peers=frozenset({"b"} if cut else set()))
    assert reachable(peer, view, own_uplink) is expected


def test_interface_states():
    faults = LocalFaults(interfaces=frozenset({ACCESS}))
    states = interface_states(config(), faults)
    assert states[UPLINK].oper is IfOperStatus.up
    assert states[ACCESS].oper is IfOperStatus.down and states[ACCESS].fault
    assert states[AC].oper is IfOperStatus.lowerLayerDown and not states[AC].fault
    disabled = interface_states(config(ac_enabled=False), LocalFaults())
    assert disabled[AC].oper is IfOperStatus.down and not disabled[AC].admin_up
    assert interface_states(config(), LocalFaults(uplink=True))[UPLINK].oper is IfOperStatus.down
    assert interface_states(config(), LocalFaults(uplink=True))[ACCESS].oper is IfOperStatus.up


def test_uplink_state_follows_config_and_fault():
    cfg = config()
    assert uplink_state(cfg, interface_states(cfg, LocalFaults()), LocalFaults()).oper_up
    faults = LocalFaults(uplink=True)
    state = uplink_state(cfg, interface_states(cfg, faults), faults)
    assert (state.admin_up, state.fault, state.oper_up) == (True, True, False)
    off = config(uplink_enabled=False)
    state = uplink_state(off, interface_states(off, LocalFaults()), LocalFaults())
    assert (state.admin_up, state.oper_up) == (False, False)


def test_golden_mtus_agree_between_spoke_and_hub(golden):
    """Golden configs never trip rule 9: AC MTUs of both ends are equal."""
    spoke, hub = golden("spoke-1"), golden("hub-a")
    assert spoke.effective_mtu("GigabitEthernet0/0/1.101") == hub.effective_mtu("GigabitEthernet0/0/2.101") == 1500
