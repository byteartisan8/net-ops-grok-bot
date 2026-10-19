"""Pydantic v2 models of the ``netsim-device`` YANG module, in RESTCONF JSON encoding (RFC 7951).

The models mirror ``contract/yang/netsim-device.yang``:

* field aliases are the YANG node names (``vlan-id``, ``peer-ipv4`` ...) and ``model_dump()`` emits them;
* the three top-level containers use module-qualified keys (``netsim-device:l2vpn`` ...);
* YANG defaults are applied (``enabled`` defaults to true), so a config read back without default
  leaves compares equal to one that spells them out;
* list-key uniqueness, the ``unique "peer-ipv4 vc-id"`` statement, the subinterface ``must`` rules and
  the leafrefs are checked like the YANG does. The models are never stricter than the YANG: duplicate
  (peer-ipv4, vc-id) pairs *across* xconnects are valid config (simd reports them as reason
  ``duplicate-pw``), see :meth:`DeviceConfig.duplicate_pseudowires`.

Use :func:`netsim_common.canonical.canonical_json` / :func:`~netsim_common.canonical.diff_configs` to
compare configs.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationInfo, field_validator, model_validator

from netsim_common.datatypes import (
    CustomerName,
    Hostname,
    InterfaceDescription,
    InterfaceName,
    IPv4,
    Mtu,
    ServiceId,
    Text255,
    VcId,
    VlanId,
)
from netsim_common.enums import InterfaceType, PwRole

MODULE = "netsim-device"
NAMESPACE = "urn:netsim:device"
PREFIX = "nsd"

#: Top-level container -> RESTCONF data resource path.
RESTCONF_PATHS = {
    "system": f"/restconf/data/{MODULE}:system",
    "interfaces": f"/restconf/data/{MODULE}:interfaces",
    "l2vpn": f"/restconf/data/{MODULE}:l2vpn",
}

#: Effective MTU of a physical interface that has no ``mtu`` leaf (contract section 6 change log).
DEFAULT_PHYSICAL_MTU = 1500

_RESTCONF_DATA = "ietf-restconf:data"
_SKIP_REFERENCES = "skip_references"


class _Node(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


def _duplicates(values: list[Any]) -> list[Any]:
    return sorted((v for v, n in Counter(values).items() if n > 1), key=str)


class System(_Node):
    """``/nsd:system``: device identity."""

    hostname: Hostname | None = None
    loopback_ipv4: IPv4 | None = Field(
        default=None,
        alias="loopback-ipv4",
        description="Loopback0 address: router ID and PW peer address (simulated, not a container IP)",
    )


class Interface(_Node):
    """``/nsd:interfaces/nsd:interface``."""

    name: InterfaceName
    description: InterfaceDescription | None = None
    enabled: StrictBool = True
    type: InterfaceType
    parent: InterfaceName | None = Field(default=None, description="Parent interface (subinterfaces only)")
    vlan_id: VlanId | None = Field(default=None, alias="vlan-id", description="802.1Q VLAN (subinterfaces only)")
    mtu: Mtu | None = Field(default=None, description="Absent on a subinterface = inherit the parent's MTU")

    @model_validator(mode="after")
    def _subinterface_rules(self) -> Self:
        if self.type is InterfaceType.SUBINTERFACE:
            if self.parent is None or self.vlan_id is None:
                raise ValueError(f"interface {self.name}: a subinterface requires parent and vlan-id")
        elif self.parent is not None or self.vlan_id is not None:
            raise ValueError(f"interface {self.name}: parent and vlan-id are only allowed on a subinterface")
        return self

    @property
    def is_subinterface(self) -> bool:
        return self.type is InterfaceType.SUBINTERFACE


class Interfaces(_Node):
    """``/nsd:interfaces``."""

    interface: list[Interface] = Field(default_factory=list)

    @field_validator("interface")
    @classmethod
    def _unique_names(cls, value: list[Interface]) -> list[Interface]:
        if dups := _duplicates([i.name for i in value]):
            raise ValueError(f"duplicate interface name(s): {', '.join(dups)}")
        return value

    def get(self, name: str) -> Interface | None:
        return next((i for i in self.interface if i.name == name), None)


class Pseudowire(_Node):
    """``/nsd:l2vpn/nsd:xconnect/nsd:pseudowire``."""

    role: PwRole
    peer_ipv4: IPv4 = Field(alias="peer-ipv4", description="Peer PE loopback")
    vc_id: VcId = Field(alias="vc-id")
    enabled: StrictBool = True


class Xconnect(_Node):
    """``/nsd:l2vpn/nsd:xconnect``: one VPWS service attachment."""

    service_id: ServiceId = Field(alias="service-id")
    customer: CustomerName | None = None
    description: Text255 | None = None
    attachment_circuit: InterfaceName = Field(alias="attachment-circuit")
    enabled: StrictBool = True
    pseudowire: list[Pseudowire] = Field(default_factory=list)

    @field_validator("pseudowire")
    @classmethod
    def _unique_pseudowires(cls, value: list[Pseudowire]) -> list[Pseudowire]:
        if dups := _duplicates([p.role for p in value]):
            raise ValueError(f"duplicate pseudowire role(s): {', '.join(dups)}")
        if dups := _duplicates([(p.peer_ipv4, p.vc_id) for p in value]):
            pairs = ", ".join(f"{peer}/{vc}" for peer, vc in dups)
            raise ValueError(f'unique "peer-ipv4 vc-id" violated within the xconnect: {pairs}')
        return value

    def pw(self, role: PwRole | str) -> Pseudowire | None:
        role = PwRole(role)
        return next((p for p in self.pseudowire if p.role is role), None)


class L2vpn(_Node):
    """``/nsd:l2vpn``."""

    xconnect: list[Xconnect] = Field(default_factory=list)

    @field_validator("xconnect")
    @classmethod
    def _unique_services(cls, value: list[Xconnect]) -> list[Xconnect]:
        if dups := _duplicates([x.service_id for x in value]):
            raise ValueError(f"duplicate xconnect service-id(s): {', '.join(dups)}")
        return value

    def get(self, service_id: str) -> Xconnect | None:
        return next((x for x in self.xconnect if x.service_id == service_id), None)


class DeviceConfig(_Node):
    """Complete ``netsim-device`` configuration of one device (RESTCONF JSON shape).

    Validation context ``{"skip_references": True}`` (used by :meth:`from_restconf` with
    ``partial=True``) disables the leafref checks, for payloads holding only some containers.
    """

    system: System = Field(default_factory=System, alias=f"{MODULE}:system")
    interfaces: Interfaces = Field(default_factory=Interfaces, alias=f"{MODULE}:interfaces")
    l2vpn: L2vpn = Field(default_factory=L2vpn, alias=f"{MODULE}:l2vpn")

    @model_validator(mode="after")
    def _references(self, info: ValidationInfo) -> Self:
        if info.context and info.context.get(_SKIP_REFERENCES):
            return self
        problems = self.reference_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ parsing / dumping
    @classmethod
    def from_restconf(cls, payload: Mapping[str, Any] | str | bytes, *, partial: bool = False) -> DeviceConfig:
        """Parse RESTCONF JSON: a full datastore, ``{"ietf-restconf:data": ...}`` or single containers.

        Top-level members of other YANG modules are ignored. With ``partial=True`` the leafref checks
        are skipped, e.g. for the response of ``GET /restconf/data/netsim-device:l2vpn``.
        """
        data: Any = json.loads(payload) if isinstance(payload, str | bytes) else payload
        if not isinstance(data, Mapping):
            raise TypeError(f"expected a JSON object, got {type(data).__name__}")
        if _RESTCONF_DATA in data:
            data = data[_RESTCONF_DATA]
        own = {k: v for k, v in data.items() if k.startswith(f"{MODULE}:")}
        context = {_SKIP_REFERENCES: True} if partial else None
        return cls.model_validate(own, context=context)

    def to_restconf(self) -> dict[str, Any]:
        """Canonical RESTCONF JSON object (see :func:`netsim_common.canonical.canonical_dict`)."""
        from netsim_common.canonical import canonical_dict

        return canonical_dict(self)

    def canonical_json(self) -> str:
        from netsim_common.canonical import canonical_json

        return canonical_json(self)

    # ------------------------------------------------------------------ lookups
    @property
    def hostname(self) -> str | None:
        return self.system.hostname

    def interface(self, name: str) -> Interface | None:
        return self.interfaces.get(name)

    def xconnect(self, service_id: str) -> Xconnect | None:
        return self.l2vpn.get(service_id)

    def effective_mtu(self, interface_name: str) -> int:
        """MTU in effect on an interface: its own ``mtu``, else (subinterface) its parent's, else 1500."""
        iface = self.interface(interface_name)
        if iface is None:
            raise KeyError(f"no interface {interface_name!r}")
        if iface.mtu is not None:
            return iface.mtu
        if iface.is_subinterface and iface.parent is not None and (parent := self.interface(iface.parent)):
            return parent.mtu if parent.mtu is not None else DEFAULT_PHYSICAL_MTU
        return DEFAULT_PHYSICAL_MTU

    def admin_up(self, interface_name: str) -> bool:
        """``enabled`` of the interface and, for a subinterface, of its parent too."""
        iface = self.interface(interface_name)
        if iface is None:
            return False
        if iface.is_subinterface and iface.parent is not None:
            parent = self.interface(iface.parent)
            return iface.enabled and parent is not None and parent.enabled
        return iface.enabled

    def core_uplink(self) -> Interface | None:
        return next((i for i in self.interfaces.interface if i.type is InterfaceType.CORE_UPLINK), None)

    def duplicate_pseudowires(self) -> dict[tuple[str, int], list[tuple[str, PwRole]]]:
        """(peer-ipv4, vc-id) pairs used by more than one PW on this device -> [(service-id, role), ...].

        These PWs are down with reason ``duplicate-pw`` (contract section 7, rule 2).
        """
        users: dict[tuple[str, int], list[tuple[str, PwRole]]] = {}
        for xc in self.l2vpn.xconnect:
            for pw in xc.pseudowire:
                users.setdefault((pw.peer_ipv4, pw.vc_id), []).append((xc.service_id, pw.role))
        return {pair: sorted(u, key=lambda x: (x[0], x[1].sort_index)) for pair, u in users.items() if len(u) > 1}

    def reference_problems(self) -> list[str]:
        """Leafref / ``must`` violations that span containers (empty list = valid)."""
        problems: list[str] = []
        for iface in self.interfaces.interface:
            if iface.parent is None:
                continue
            parent = self.interface(iface.parent)
            if parent is None:
                problems.append(f"interface {iface.name}: parent {iface.parent} does not exist")
            elif parent.is_subinterface:
                problems.append(f"interface {iface.name}: parent {iface.parent} must be a physical interface")
        for xc in self.l2vpn.xconnect:
            if self.interface(xc.attachment_circuit) is None:
                problems.append(f"xconnect {xc.service_id}: attachment-circuit {xc.attachment_circuit} does not exist")
        return problems
