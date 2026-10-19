"""Simulated control plane, pure functions (contract section 7). No I/O: the daemon feeds snapshots in.

Interfaces (IF-MIB view, contract section 8 "ifOperStatus follows config enabled and not fault"):

* physical interface: ``up`` if ``enabled`` and no fault, else ``down``. Faults are the interface key
  ``netsim:fault:ac:<host>:<ifname>`` (a physical fault on any named interface) and, for ``core-uplink``
  interfaces, ``netsim:fault:uplink:<host>``;
* subinterface: ``down`` if disabled, ``lowerLayerDown`` if its parent is not up, ``down`` if it has its own
  fault, else ``up``.

The uplink is up when a ``core-uplink`` interface is oper up (``config enabled and not fault``). A PW's AC is
up when its attachment-circuit interface is oper up, so a fault on the physical access interface takes every
service subinterface (and their PWs: ``ac-down``) with it.

Pseudowires are evaluated with the contract's table, first match wins (:func:`evaluate_pseudowires`).
Only peer, VC ID, MTU, admin state and reachability matter; service-id and role are never compared between
the two ends (that is what makes drift realistic). Edge cases the contract leaves open:

* a PW whose peer is this device's own loopback has no LDP neighbor: ``peer-unreachable``;
* several fresh adverts with the same loopback: the first by hostname is the peer;
* several matching PWs on the peer (the peer then reports ``duplicate-pw``): the first by (service, role).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from netsim_common.device_config import DEFAULT_PHYSICAL_MTU, DeviceConfig
from netsim_common.enums import IfOperStatus, InterfaceType, PwOperStatus, PwReason, PwRole
from netsim_common.signaling import Advert, AdvertPw


@dataclass(frozen=True, slots=True)
class LocalFaults:
    """Physical faults that concern this device (read from the bus, never from config)."""

    uplink: bool = False
    interfaces: frozenset[str] = frozenset()
    cut_peers: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class IfState:
    name: str
    type: InterfaceType
    admin_up: bool
    oper: IfOperStatus
    fault: bool
    mtu: int
    description: str | None
    parent: str | None

    @property
    def oper_up(self) -> bool:
        return self.oper is IfOperStatus.up


@dataclass(frozen=True, slots=True)
class UplinkState:
    interfaces: tuple[str, ...]
    admin_up: bool
    fault: bool
    oper_up: bool


@dataclass(frozen=True, slots=True)
class LocalPw:
    """A pseudowire as configured on this device."""

    service_id: str
    role: PwRole
    peer: str
    vc_id: int
    xc_enabled: bool
    pw_enabled: bool
    ac: str
    customer: str | None

    @property
    def key(self) -> tuple[str, PwRole]:
        return (self.service_id, self.role)

    @property
    def name(self) -> str:
        """``cpwVcName``: ``<service-id>:<role>`` (contract section 8)."""
        return f"{self.service_id}:{self.role.value}"

    @property
    def admin_up(self) -> bool:
        return self.xc_enabled and self.pw_enabled


@dataclass(frozen=True, slots=True)
class RemoteInfo:
    """The PW on the peer that matches ours (peer = our loopback, same VC ID), as learned from its advert."""

    hostname: str
    mtu: int
    ac_name: str
    service_id: str
    role: PwRole
    enabled: bool
    ac_oper_up: bool


@dataclass(frozen=True, slots=True)
class PwEval:
    pw: LocalPw
    oper: PwOperStatus
    reason: PwReason
    local_mtu: int
    ac_oper_up: bool
    peer_hostname: str | None
    remote: RemoteInfo | None


@dataclass(frozen=True, slots=True)
class PeerView:
    """What this device currently sees on the bus."""

    adverts: Mapping[str, Advert]
    own_fresh: bool
    cut_peers: frozenset[str] = frozenset()


def interface_states(config: DeviceConfig, faults: LocalFaults) -> dict[str, IfState]:
    """IF-MIB oper state of every configured interface (config order)."""
    out: dict[str, IfState] = {}
    for iface in config.interfaces.interface:
        if iface.is_subinterface:
            continue
        fault = iface.name in faults.interfaces or (iface.type is InterfaceType.CORE_UPLINK and faults.uplink)
        out[iface.name] = IfState(
            name=iface.name,
            type=iface.type,
            admin_up=iface.enabled,
            oper=IfOperStatus.up if iface.enabled and not fault else IfOperStatus.down,
            fault=fault,
            mtu=config.effective_mtu(iface.name),
            description=iface.description,
            parent=None,
        )
    for iface in config.interfaces.interface:
        if not iface.is_subinterface:
            continue
        fault = iface.name in faults.interfaces
        parent = out.get(iface.parent or "")
        if not iface.enabled:
            oper = IfOperStatus.down
        elif parent is None or not parent.oper_up:
            oper = IfOperStatus.lowerLayerDown
        elif fault:
            oper = IfOperStatus.down
        else:
            oper = IfOperStatus.up
        out[iface.name] = IfState(
            name=iface.name,
            type=iface.type,
            admin_up=iface.enabled,
            oper=oper,
            fault=fault,
            mtu=config.effective_mtu(iface.name),
            description=iface.description,
            parent=iface.parent,
        )
    order = {i.name: n for n, i in enumerate(config.interfaces.interface)}
    return dict(sorted(out.items(), key=lambda kv: order[kv[0]]))


def uplink_state(config: DeviceConfig, ifstates: Mapping[str, IfState], faults: LocalFaults) -> UplinkState:
    """``uplink_oper_up = config enabled and not fault`` over the core-uplink interface(s)."""
    names = tuple(i.name for i in config.interfaces.interface if i.type is InterfaceType.CORE_UPLINK)
    return UplinkState(
        interfaces=names,
        admin_up=any(ifstates[n].admin_up for n in names),
        fault=faults.uplink,
        oper_up=any(ifstates[n].oper_up for n in names),
    )


def local_pseudowires(config: DeviceConfig) -> list[LocalPw]:
    """Every configured PW, sorted by (service-id, role) with primary first."""
    pws = [
        LocalPw(
            service_id=xc.service_id,
            role=pw.role,
            peer=pw.peer_ipv4,
            vc_id=pw.vc_id,
            xc_enabled=xc.enabled,
            pw_enabled=pw.enabled,
            ac=xc.attachment_circuit,
            customer=xc.customer,
        )
        for xc in config.l2vpn.xconnect
        for pw in xc.pseudowire
    ]
    return sorted(pws, key=lambda p: (p.service_id, p.role.sort_index))


def _ac(ifstates: Mapping[str, IfState], name: str) -> tuple[bool, int]:
    state = ifstates.get(name)
    return (state is not None and state.oper_up, state.mtu if state is not None else DEFAULT_PHYSICAL_MTU)


def advert_pws(config: DeviceConfig, ifstates: Mapping[str, IfState]) -> list[AdvertPw]:
    """The ``pws`` member of this device's advert (contract section 7)."""
    out = []
    for pw in local_pseudowires(config):
        ac_up, mtu = _ac(ifstates, pw.ac)
        out.append(
            AdvertPw(
                service_id=pw.service_id,
                role=pw.role,
                peer=pw.peer,
                vc_id=pw.vc_id,
                enabled=pw.admin_up,
                ac_oper_up=ac_up,
                mtu=mtu,
                ac_name=pw.ac,
            )
        )
    return out


def find_peer(view: PeerView, loopback: str, self_hostname: str) -> Advert | None:
    """The fresh advert of another device that owns ``loopback`` (first by hostname)."""
    owners = sorted(h for h, adv in view.adverts.items() if adv.loopback == loopback and h != self_hostname)
    return view.adverts[owners[0]] if owners else None


def reachable(peer: Advert, view: PeerView, own_uplink_up: bool) -> bool:
    """``fresh(A) and fresh(B) and uplink_oper_up(A) and uplink_oper_up(B) and not cut(A,B)``.

    ``fresh(B)`` holds because ``peer`` comes from the view of fresh adverts.
    """
    return view.own_fresh and own_uplink_up and peer.uplink_oper_up and peer.hostname not in view.cut_peers


def remote_pseudowire(peer: Advert, local_loopback: str, vc_id: int) -> AdvertPw | None:
    """The peer's PW with ``peer = our loopback`` and the same VC ID (first by service, role)."""
    matches = [p for p in peer.pws if p.peer == local_loopback and p.vc_id == vc_id]
    return min(matches, key=lambda p: (p.service_id, PwRole(p.role).sort_index)) if matches else None


def evaluate_pseudowires(
    *,
    hostname: str,
    loopback: str | None,
    config: DeviceConfig,
    ifstates: Mapping[str, IfState],
    uplink: UplinkState,
    view: PeerView,
) -> list[PwEval]:
    """Contract section 7 oper evaluation for every local PW (first matching rule wins)."""
    duplicates = {key for users in config.duplicate_pseudowires().values() for key in users}
    out: list[PwEval] = []
    for pw in local_pseudowires(config):
        ac_up, local_mtu = _ac(ifstates, pw.ac)
        peer = find_peer(view, pw.peer, hostname) if loopback and pw.peer != loopback else None
        is_reachable = peer is not None and reachable(peer, view, uplink.oper_up)
        rpw = remote_pseudowire(peer, loopback, pw.vc_id) if peer is not None and is_reachable and loopback else None
        remote = (
            RemoteInfo(
                hostname=peer.hostname,
                mtu=rpw.mtu,
                ac_name=rpw.ac_name,
                service_id=rpw.service_id,
                role=PwRole(rpw.role),
                enabled=rpw.enabled,
                ac_oper_up=rpw.ac_oper_up,
            )
            if peer is not None and rpw is not None
            else None
        )
        if not pw.admin_up:
            reason = PwReason.ADMIN_DOWN
        elif pw.key in duplicates:
            reason = PwReason.DUPLICATE_PW
        elif not ac_up:
            reason = PwReason.AC_DOWN
        elif not uplink.oper_up:
            reason = PwReason.LOCAL_UPLINK_DOWN
        elif not is_reachable:
            reason = PwReason.PEER_UNREACHABLE
        elif rpw is None:
            reason = PwReason.REMOTE_NOT_CONFIGURED
        elif not rpw.enabled:
            reason = PwReason.REMOTE_ADMIN_DOWN
        elif not rpw.ac_oper_up:
            reason = PwReason.REMOTE_AC_DOWN
        elif rpw.mtu != local_mtu:
            reason = PwReason.MTU_MISMATCH
        else:
            reason = PwReason.OK
        out.append(
            PwEval(
                pw=pw,
                oper=reason.oper_status,
                reason=reason,
                local_mtu=local_mtu,
                ac_oper_up=ac_up,
                peer_hostname=peer.hostname if peer is not None else None,
                remote=remote,
            )
        )
    return out
