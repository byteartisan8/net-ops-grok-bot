"""The device's MIB view (contract section 8), built from simd state. Every OID comes from contract/oids.yaml.

Served objects:

* SNMPv2-MIB system group: ``sysDescr`` = ``NetSim PE (Clixon <ver>) <hostname>``, ``sysObjectID`` =
  ``zeroDotZero`` (SNMPv2-SMI ``{ 0 0 }``: the lab has no enterprise number), ``sysUpTime`` (live, from simd
  boot), ``sysContact`` (empty), ``sysName`` = hostname, ``sysLocation`` = ``$SITE``, ``sysServices`` = 6
  (layers 2 and 3);
* IF-MIB ``ifNumber`` + ``ifTable`` (ifIndex, ifDescr, ifType, ifMtu, ifAdminStatus, ifOperStatus) and
  ``ifXTable`` (ifName, ifAlias) for every configured interface; ``ifType`` is ``ethernetCsmacd`` for
  physical interfaces and ``l2vlan`` for 802.1Q subinterfaces;
* CISCO-IETF-PW-MIB ``cpwVcTable``, the 15 columns of contract section 8, one row per configured PW:
  ``cpwVcName`` = ``<service-id>:<role>``, ``cpwVcDescr`` = customer, ``cpwVcPeerAddr`` = 4 octets,
  ``cpwVcType`` = ``ethernetVLAN`` (subinterface AC) or ``ethernet`` (physical AC), ``cpwVcPsnType`` = mpls,
  inbound/outbound oper status = ``cpwVcOperStatus``, remote MTU/interface from the peer's advert
  (0 / empty when unknown), ``cpwVcUpTime`` live since the last up transition (0 while not up).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

from netsim_common.enums import AdminStatus, IfOperStatus, InterfaceType, PwOperStatus
from netsim_common.oids import OidRegistry
from netsim_simd.snmpdb import Row, oid_tuple

#: snmprec tag per SMI base type (snmpsim SnmprecGrammar.TAG_MAP; tests check them against snmpsim).
SNMPREC_TAG_BY_BASE: dict[str, str] = {
    "INTEGER": "2",
    "Integer32": "2",
    "OCTET STRING": "4x",
    "OBJECT IDENTIFIER": "6",
    "IpAddress": "64",
    "Counter32": "65",
    "Gauge32": "66",
    "Unsigned32": "66",
    "TimeTicks": "67",
    "Counter64": "70",
}

#: ``zeroDotZero`` from the pinned SNMPv2-SMI (tests check the MIB text).
ZERO_DOT_ZERO = "0.0"
#: sysServices: 2**(2-1) + 2**(3-1), a layer-2 + layer-3 device (RFC 3418).
SYS_SERVICES = 6

SYSTEM_OBJECTS = ("sysDescr", "sysObjectID", "sysUpTime", "sysContact", "sysName", "sysLocation", "sysServices")
IF_COLUMNS = ("ifIndex", "ifDescr", "ifType", "ifMtu", "ifAdminStatus", "ifOperStatus")
IFX_COLUMNS = ("ifName", "ifAlias")
PW_COLUMNS = (
    "cpwVcType",
    "cpwVcPsnType",
    "cpwVcPeerAddrType",
    "cpwVcPeerAddr",
    "cpwVcID",
    "cpwVcLocalIfMtu",
    "cpwVcRemoteIfMtu",
    "cpwVcRemoteIfString",
    "cpwVcName",
    "cpwVcDescr",
    "cpwVcUpTime",
    "cpwVcAdminStatus",
    "cpwVcOperStatus",
    "cpwVcInboundOperStatus",
    "cpwVcOutboundOperStatus",
)
SERVED_TABLES = ("system", "ifNumber", "ifTable", "ifXTable", "cpwVcTable")


@dataclass(frozen=True, slots=True)
class SysInfo:
    hostname: str
    descr: str
    location: str
    contact: str
    boot_since: float


@dataclass(frozen=True, slots=True)
class IfRow:
    index: int
    name: str
    type: InterfaceType
    mtu: int
    admin_up: bool
    oper: IfOperStatus
    alias: str


@dataclass(frozen=True, slots=True)
class PwRow:
    index: int
    name: str
    descr: str
    subinterface_ac: bool
    peer: str
    vc_id: int
    local_mtu: int
    remote_mtu: int
    remote_if: str
    admin_up: bool
    oper: PwOperStatus
    up_since: float | None


class MibView:
    def __init__(self, oids: OidRegistry) -> None:
        self.oids = oids
        for name in (*SYSTEM_OBJECTS, *IF_COLUMNS, *IFX_COLUMNS, *PW_COLUMNS, "ifNumber"):
            base = self._base(name)
            if base not in SNMPREC_TAG_BY_BASE:
                raise ValueError(f"{name}: unsupported SMI base type {base}")

    def _base(self, name: str) -> str:
        syntax = self.oids.object(name).syntax
        if syntax is None:
            raise ValueError(f"{name} has no syntax in oids.yaml")
        return syntax.base

    def anchor(self) -> str:
        """Longest OID prefix shared by everything this view serves (the snmpsim data-file anchor)."""
        prefixes = [oid_tuple(self.oids.oid(name)) for name in SERVED_TABLES]
        common: list[int] = []
        for parts in zip(*prefixes, strict=False):
            if len(set(parts)) != 1:
                break
            common.append(parts[0])
        return ".".join(str(x) for x in common)

    # ------------------------------------------------------------------ cells
    def _cell(self, name: str, index: tuple[int, ...], value: int | str | bytes, since: float | None = None) -> Row:
        obj = self.oids.object(name)
        oid = oid_tuple(obj.oid) + (index if obj.kind == "column" else (0,))
        base = self._base(name)
        tag = SNMPREC_TAG_BY_BASE[base]
        if base == "OCTET STRING":
            raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
            text = raw.hex()
        elif base == "OBJECT IDENTIFIER":
            text = str(value)
        else:
            text = str(int(value))
        return Row(oid, tag, text, since)

    def _enum(self, name: str, label: str) -> int:
        return self.oids.value(name, label)

    # ------------------------------------------------------------------ groups
    def system_rows(self, sys: SysInfo) -> list[Row]:
        return [
            self._cell("sysDescr", (), sys.descr),
            self._cell("sysObjectID", (), ZERO_DOT_ZERO),
            self._cell("sysUpTime", (), 0, since=sys.boot_since),
            self._cell("sysContact", (), sys.contact),
            self._cell("sysName", (), sys.hostname),
            self._cell("sysLocation", (), sys.location),
            self._cell("sysServices", (), SYS_SERVICES),
        ]

    def interface_rows(self, ifaces: Iterable[IfRow]) -> list[Row]:
        ifaces = list(ifaces)
        rows = [self._cell("ifNumber", (), len(ifaces))]
        for i in ifaces:
            idx = (i.index,)
            if_type = "l2vlan" if i.type is InterfaceType.SUBINTERFACE else "ethernetCsmacd"
            rows += [
                self._cell("ifIndex", idx, i.index),
                self._cell("ifDescr", idx, i.name),
                self._cell("ifType", idx, self._enum("ifType", if_type)),
                self._cell("ifMtu", idx, i.mtu),
                self._cell("ifAdminStatus", idx, int(AdminStatus.up if i.admin_up else AdminStatus.down)),
                self._cell("ifOperStatus", idx, int(i.oper)),
                self._cell("ifName", idx, i.name),
                self._cell("ifAlias", idx, i.alias),
            ]
        return rows

    def pw_rows(self, pws: Iterable[PwRow]) -> list[Row]:
        rows: list[Row] = []
        for p in pws:
            idx = (p.index,)
            oper = int(p.oper)
            up = p.oper is PwOperStatus.up and p.up_since is not None
            rows += [
                self._cell(
                    "cpwVcType", idx, self._enum("cpwVcType", "ethernetVLAN" if p.subinterface_ac else "ethernet")
                ),
                self._cell("cpwVcPsnType", idx, self._enum("cpwVcPsnType", "mpls")),
                self._cell("cpwVcPeerAddrType", idx, self._enum("cpwVcPeerAddrType", "ipv4")),
                self._cell("cpwVcPeerAddr", idx, ipaddress.IPv4Address(p.peer).packed),
                self._cell("cpwVcID", idx, p.vc_id),
                self._cell("cpwVcLocalIfMtu", idx, p.local_mtu),
                self._cell("cpwVcRemoteIfMtu", idx, p.remote_mtu),
                self._cell("cpwVcRemoteIfString", idx, p.remote_if),
                self._cell("cpwVcName", idx, p.name),
                self._cell("cpwVcDescr", idx, p.descr),
                self._cell("cpwVcUpTime", idx, 0, since=p.up_since if up else None),
                self._cell("cpwVcAdminStatus", idx, int(AdminStatus.up if p.admin_up else AdminStatus.down)),
                self._cell("cpwVcOperStatus", idx, oper),
                self._cell("cpwVcInboundOperStatus", idx, oper),
                self._cell("cpwVcOutboundOperStatus", idx, oper),
            ]
        return rows

    def rows(self, sys: SysInfo, ifaces: Iterable[IfRow], pws: Iterable[PwRow]) -> list[Row]:
        out = self.system_rows(sys) + self.interface_rows(ifaces) + self.pw_rows(pws)
        return sorted(out, key=lambda r: r.oid)
