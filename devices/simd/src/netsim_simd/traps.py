"""SNMPv2c notifications (pysnmp message encoding) and PW-transition batching.

Every PDU is ``sysUpTime.0``, ``snmpTrapOID.0`` then the notification's OBJECTS from contract/oids.yaml
(RFC 3416 section 4.2.6). ``sysUpTime.0`` counts from simd boot.

* ``coldStart``: once per simd start (new boot_id), when the agent is serving.
* ``ciscoConfigManEvent``: once per detected running-config change. Varbinds (indexed by a per-boot
  ``ccmHistoryEventIndex``): CommandSource = commandLine, ConfigSource = commandSource,
  ConfigDestination = running, TerminalUser = the Clixon session user (``root`` from the CLI,
  ``restconf`` from RESTCONF).
* ``cpwVcDown`` / ``cpwVcUp``: on every cpwVcOperStatus transition of an existing row. ``cpwVcUp`` when a row
  enters ``up``; ``cpwVcDown`` on every other transition (into ``down`` or ``lowerLayerDown``, including
  ``down`` <-> ``lowerLayerDown``). The two ``cpwVcOperStatus`` varbinds carry the row's actual new value and
  bound the affected index range, as the MIB describes.

Dampening (:class:`TransitionBatcher`): transitions are collected for a short window (default 100 ms) and
coalesced into one notification per run of *consecutive* cpwVcIndex values that made the same transition
in the same window (the MIB's "range of contiguous indexes"). A PW is never in two ranges of one batch and
a batch never merges transitions of different kinds, so each real transition yields exactly one
notification covering that PW, in order (a fast up -> down -> up still sends cpwVcDown then cpwVcUp).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pyasn1.codec.ber import decoder, encoder
from pyasn1.type import univ
from pysnmp.proto import api, rfc1902

from netsim_common.enums import PwOperStatus
from netsim_common.oids import OidRegistry
from netsim_simd.settings import TrapTarget

log = logging.getLogger("simd.traps")

_V2C = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
_V1 = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]


@dataclass(frozen=True, slots=True)
class Varbind:
    name: str
    oid: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "oid": self.oid, "value": render_value(self.value)}


@dataclass(frozen=True, slots=True)
class Trap:
    name: str
    oid: str
    varbinds: tuple[Varbind, ...]
    pws: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "oid": self.oid,
            "varbinds": [vb.to_dict() for vb in self.varbinds],
            **({"pws": list(self.pws)} if self.pws else {}),
        }


def render_value(value: Any) -> Any:
    """JSON-friendly rendering of a pysnmp/pyasn1 value (OIDs dotted, printable strings as text, else hex)."""
    if isinstance(value, univ.ObjectIdentifier):
        return ".".join(str(x) for x in value)
    if isinstance(value, univ.OctetString):
        raw = bytes(value)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + raw.hex()
        return text if text.isprintable() else "0x" + raw.hex()
    if isinstance(value, univ.Integer):
        return int(value)
    if hasattr(value, "prettyPrint"):
        return value.prettyPrint()
    return value


class TrapBuilder:
    """Builds the contract's notifications from contract/oids.yaml."""

    def __init__(self, oids: OidRegistry) -> None:
        self.oids = oids
        self._uptime_oid = oids.instance("sysUpTime")
        self._trap_oid = oids.instance("snmpTrapOID")

    def _header(self, name: str, uptime_ticks: int) -> list[Varbind]:
        return [
            Varbind("sysUpTime", self._uptime_oid, rfc1902.TimeTicks(uptime_ticks)),
            Varbind("snmpTrapOID", self._trap_oid, rfc1902.ObjectIdentifier(self.oids.oid(name))),
        ]

    def cold_start(self, uptime_ticks: int) -> Trap:
        n = self.oids.notification("coldStart")
        return Trap(n.name, n.oid, tuple(self._header(n.name, uptime_ticks)))

    def pw_range(
        self, up: bool, first: int, last: int, value: PwOperStatus, uptime_ticks: int, pws: Sequence[str]
    ) -> Trap:
        n = self.oids.notification("cpwVcUp" if up else "cpwVcDown")
        lo, hi = n.objects
        varbinds = [
            *self._header(n.name, uptime_ticks),
            Varbind(lo.name, lo.instance(first), rfc1902.Integer(int(value))),
            Varbind(hi.name, hi.instance(last), rfc1902.Integer(int(value))),
        ]
        return Trap(n.name, n.oid, tuple(varbinds), tuple(pws))

    def config_man_event(self, event_index: int, user: str, uptime_ticks: int) -> Trap:
        n = self.oids.notification("ciscoConfigManEvent")
        values = {
            "ccmHistoryEventCommandSource": rfc1902.Integer(
                self.oids.value("ccmHistoryEventCommandSource", "commandLine")
            ),
            "ccmHistoryEventConfigSource": rfc1902.Integer(
                self.oids.value("ccmHistoryEventConfigSource", "commandSource")
            ),
            "ccmHistoryEventConfigDestination": rfc1902.Integer(
                self.oids.value("ccmHistoryEventConfigDestination", "running")
            ),
            "ccmHistoryEventTerminalUser": rfc1902.OctetString(user.encode("utf-8")[:64]),
        }
        varbinds = self._header(n.name, uptime_ticks)
        varbinds += [Varbind(o.name, o.instance(event_index), values[o.name]) for o in n.objects]
        return Trap(n.name, n.oid, tuple(varbinds))


# ---------------------------------------------------------------------------------------------- wire format
def encode_v2c_trap(community: str, trap: Trap, request_id: int = 0) -> bytes:
    pdu = _V2C.SNMPv2TrapPDU()
    _V2C.apiTrapPDU.set_defaults(pdu)
    if request_id:
        _V2C.apiTrapPDU.set_request_id(pdu, request_id)
    _V2C.apiTrapPDU.set_varbinds(pdu, [(_V2C.ObjectIdentifier(vb.oid), vb.value) for vb in trap.varbinds])
    msg = _V2C.Message()
    _V2C.apiMessage.set_defaults(msg)
    _V2C.apiMessage.set_community(msg, community)
    _V2C.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


@dataclass(frozen=True, slots=True)
class DecodedTrap:
    version: str
    community: str
    request_id: int | None
    varbinds: tuple[tuple[str, Any], ...]

    def value(self, oid: str) -> Any:
        """Rendered value of the varbind ``oid`` (e.g. ``oids.instance("snmpTrapOID")``), or None."""
        return next((render_value(v) for o, v in self.varbinds if o == oid), None)


def decode_trap(data: bytes) -> DecodedTrap:
    """Decode an SNMPv1/v2c trap message (used by the trap sink and the tests)."""
    version = int(api.decodeMessageVersion(data))
    module = _V2C if version == api.SNMP_VERSION_2C else _V1
    msg, _ = decoder.decode(data, asn1Spec=module.Message())
    community = bytes(module.apiMessage.get_community(msg)).decode("utf-8", "replace")
    pdu = module.apiMessage.get_pdu(msg)
    if version == api.SNMP_VERSION_2C:
        varbinds = module.apiTrapPDU.get_varbinds(pdu)
        request_id = int(module.apiTrapPDU.get_request_id(pdu))
        label = "v2c"
    else:
        varbinds = module.apiTrapPDU.get_varbinds(pdu)
        request_id = None
        label = "v1"
    return DecodedTrap(label, community, request_id, tuple((str(oid), val) for oid, val in varbinds))


# ---------------------------------------------------------------------------------------------- batching
@dataclass(frozen=True, slots=True)
class Transition:
    index: int
    name: str
    old: PwOperStatus | None
    new: PwOperStatus

    @property
    def up(self) -> bool:
        return self.new is PwOperStatus.up


@dataclass(frozen=True, slots=True)
class PlannedTrap:
    up: bool
    value: PwOperStatus
    first: int
    last: int
    pws: tuple[str, ...]


@dataclass
class TransitionBatcher:
    """Collects PW transitions and turns them into range notifications (see module docstring)."""

    pending: list[Transition] = field(default_factory=list)

    def add(self, transition: Transition) -> None:
        self.pending.append(transition)

    def __len__(self) -> int:
        return len(self.pending)

    def drain(self) -> list[PlannedTrap]:
        items, self.pending = self.pending, []
        batches: list[list[Transition]] = []
        for t in items:
            batch = batches[-1] if batches else None
            if batch is None or (batch[0].up, batch[0].new) != (t.up, t.new) or any(b.index == t.index for b in batch):
                batches.append([t])
            else:
                batch.append(t)
        plans: list[PlannedTrap] = []
        for batch in batches:
            ordered = sorted(batch, key=lambda t: t.index)
            run: list[Transition] = [ordered[0]]
            for t in ordered[1:]:
                if t.index == run[-1].index + 1:
                    run.append(t)
                else:
                    plans.append(_plan(run))
                    run = [t]
            plans.append(_plan(run))
        return plans


def _plan(run: list[Transition]) -> PlannedTrap:
    return PlannedTrap(
        up=run[0].up,
        value=run[0].new,
        first=run[0].index,
        last=run[-1].index,
        pws=tuple(t.name for t in run),
    )


# ---------------------------------------------------------------------------------------------- sending
class TrapSender:
    """Fire-and-forget UDP sender to every TRAP_TARGETS entry (from the container's mgmt address)."""

    def __init__(self, targets: Sequence[TrapTarget], community: str) -> None:
        self.targets = tuple(targets)
        self.community = community
        self._transport: asyncio.DatagramTransport | None = None
        self._request_id = 0

    async def start(self) -> None:
        if self._transport is None and self.targets:
            loop = asyncio.get_running_loop()
            self._transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, family=socket.AF_INET)

    async def send(self, trap: Trap) -> list[str]:
        """Send to every target; returns the targets it was handed to (UDP: no delivery guarantee)."""
        if not self.targets:
            return []
        await self.start()
        assert self._transport is not None
        self._request_id = self._request_id % 2_000_000_000 + 1
        data = encode_v2c_trap(self.community, trap, self._request_id)
        sent: list[str] = []
        loop = asyncio.get_running_loop()
        for target in self.targets:
            try:
                infos = await loop.getaddrinfo(target.host, target.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
                self._transport.sendto(data, infos[0][4])
                sent.append(str(target))
            except OSError as exc:
                log.warning("trap to %s failed: %s", target, exc, extra={"fields": {"event": "trap-error"}})
        return sent

    def close(self) -> None:
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
        self._transport = None
