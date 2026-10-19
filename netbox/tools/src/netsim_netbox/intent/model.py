"""The lab's intent as NetBox states it, built from a :class:`~netsim_netbox.intent.snapshot.Snapshot`. Pure: no I/O.

Interpretation (contract section 10; names in :mod:`netsim_netbox.objects`):

* **devices**: every device with platform ``netsim-clixon``; its loopback is its IP address with role
  ``loopback`` (the one on ``Loopback0`` if there are several);
* **config interfaces** of a device: interfaces with custom field ``if_role`` (``core-uplink`` / ``access``, the
  YANG ``type``) and subinterfaces (interfaces with a ``parent``; ``vlan-id`` = the VID of their single tagged VLAN,
  802.1Q mode ``tagged``). Everything else (``Mgmt0``, ``Loopback0``, ...) is not part of the device config. Name,
  ``enabled``, ``mtu`` (empty = absent) and ``description`` (empty = absent) are taken as they are;
* **pseudowires**: every L2VPN tagged ``l2vpn-pw``: type ``vpws``, ``identifier`` = vc-id, custom fields
  ``service_id`` and ``pw_role``, tenant = customer. Spoke end (a-end) = the interface in custom field ``spoke_ac``;
  hub end (z-end) = the L2VPN's interface termination (a termination on the spoke AC itself is ignored). Each end's
  peer address is the other end's loopback;
* **customers**: tenants of group ``l2vpn-customers`` and tenants owning a pseudowire, with ``sla_tier``.

Anything that cannot be interpreted becomes an :class:`IntentProblem` naming the devices it affects; rendering a
device with problems (or the BSM spec while any pseudowire or customer has one) raises :class:`IntentError`, because
partial intent would make the bot "fix" devices towards a wrong target.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from netsim_common.datatypes import SERVICE_ID_PATTERN
from netsim_common.enums import InterfaceType, PwRole, SlaTier
from netsim_netbox.intent.snapshot import Obj, Snapshot
from netsim_netbox.objects import (
    CF_IF_ROLE,
    CF_PW_ROLE,
    CF_SERVICE_ID,
    CF_SLA_TIER,
    CF_SPOKE_AC,
    INTERFACE_OBJECT,
    IP_ROLE_LOOPBACK,
    KINDS,
    L2VPN_TYPE,
    LOOPBACK_IF,
    MODE_TAGGED,
    TENANT_GROUP,
)

Scope = Literal["device", "pseudowire", "customer"]
_VC_ID_MAX = 4294967295
_PHYSICAL_ROLES = {InterfaceType.CORE_UPLINK.value, InterfaceType.ACCESS.value}
#: (object type, id) -> NetBox UI URL
UrlFn = Callable[[str, int], str]


@dataclass(frozen=True)
class IntentProblem:
    """NetBox data that cannot be turned into intent. ``hosts`` = devices whose rendered config it affects."""

    message: str
    scope: Scope
    hosts: frozenset[str] = frozenset()
    url: str | None = None
    service_id: str | None = None

    def __str__(self) -> str:
        return f"{self.message} ({self.url})" if self.url else self.message


class IntentError(ValueError):
    def __init__(self, problems: list[IntentProblem], what: str = "intent") -> None:
        self.problems = problems
        lines = "\n  - ".join(str(p) for p in problems)
        super().__init__(f"cannot build {what} from NetBox:\n  - {lines}")


@dataclass(frozen=True)
class InterfaceIntent:
    id: int
    name: str
    type: InterfaceType
    enabled: bool
    mtu: int | None = None
    description: str | None = None
    parent: str | None = None
    vlan_id: int | None = None


@dataclass(frozen=True)
class DeviceIntent:
    id: int
    name: str
    role: str | None
    site: str | None
    url: str
    loopback: str | None
    interfaces: tuple[InterfaceIntent, ...]


@dataclass(frozen=True)
class PwEndIntent:
    host: str
    ac: str
    interface_id: int
    loopback: str  # this end's loopback = the address the *other* end peers with


@dataclass(frozen=True)
class PwIntent:
    """One pseudowire (one NetBox L2VPN). ``a_end`` = spoke, ``z_end`` = hub."""

    l2vpn_id: int
    name: str
    service_id: str
    role: PwRole
    vc_id: int
    customer: str | None
    tenant_id: int | None
    status: str | None
    url: str
    a_end: PwEndIntent
    z_end: PwEndIntent

    def ends_on(self, host: str) -> bool:
        return host in (self.a_end.host, self.z_end.host)

    def local_remote(self, host: str) -> tuple[PwEndIntent, PwEndIntent]:
        if host == self.a_end.host:
            return self.a_end, self.z_end
        if host == self.z_end.host:
            return self.z_end, self.a_end
        raise KeyError(f"{self.name} does not terminate on {host}")


@dataclass(frozen=True)
class CustomerIntent:
    tenant_id: int
    name: str
    slug: str
    sla_tier: SlaTier | None
    url: str
    in_group: bool


@dataclass
class LabIntent:
    netbox_version: str
    public_url: str
    devices: dict[str, DeviceIntent]
    pseudowires: list[PwIntent]
    customers: dict[str, CustomerIntent]
    problems: list[IntentProblem] = field(default_factory=list)

    def device(self, host: str) -> DeviceIntent:
        try:
            return self.devices[host]
        except KeyError:
            raise IntentError(
                [
                    IntentProblem(
                        f"{host}: no device with platform netsim-clixon in NetBox", "device", frozenset({host})
                    )
                ],
                f"the intent of {host}",
            ) from None

    def pws_on(self, host: str) -> list[PwIntent]:
        return sorted((p for p in self.pseudowires if p.ends_on(host)), key=_pw_order)

    def services(self) -> dict[str, list[PwIntent]]:
        out: dict[str, list[PwIntent]] = defaultdict(list)
        for pw in sorted(self.pseudowires, key=_pw_order):
            out[pw.service_id].append(pw)
        return dict(sorted(out.items()))

    def problems_for(self, host: str) -> list[IntentProblem]:
        return [p for p in self.problems if host in p.hosts]

    def spec_problems(self) -> list[IntentProblem]:
        """Problems that make the BSM spec (and the service views) unreliable."""
        return [p for p in self.problems if p.scope in ("pseudowire", "customer")]


def _pw_order(pw: PwIntent) -> tuple[str, int]:
    return pw.service_id, pw.role.sort_index


# --------------------------------------------------------------------------------------------- readers
def choice(value: Any) -> Any:
    """Raw value of a choice field or selection custom field (4.7 returns ``{"value", "label"}``)."""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def object_id(value: Any) -> int | None:
    """ID of a nested object or object custom field (or a bare ID)."""
    if isinstance(value, dict):
        return value.get("id")
    return value if isinstance(value, int) else None


def _cf(obj: Obj, name: str) -> Any:
    return (obj.get("custom_fields") or {}).get(name)


def _host(address: str) -> str:
    return address.split("/", 1)[0]


# --------------------------------------------------------------------------------------------- build
def build_intent(snapshot: Snapshot, public_url: str) -> LabIntent:
    """Interpret a snapshot (see module docstring). Never raises for bad data: it records problems."""
    base = public_url.rstrip("/")
    problems: list[IntentProblem] = []

    def url(kind: str, ident: int) -> str:
        return KINDS[kind].ui_url(base, ident)

    devices_by_id = {d["id"]: d for d in snapshot.devices}
    ifaces_by_device: dict[int, list[Obj]] = defaultdict(list)
    for i in snapshot.interfaces:
        if object_id(i.get("device")) in devices_by_id:
            ifaces_by_device[object_id(i["device"])].append(i)
    iface_by_id = {i["id"]: i for lst in ifaces_by_device.values() for i in lst}

    devices: dict[str, DeviceIntent] = {}
    for d in sorted(snapshot.devices, key=lambda d: d["name"] or ""):
        name = d["name"]
        if not name:
            problems.append(IntentProblem(f"device {d['id']} has no name", "device", url=url("dcim.device", d["id"])))
            continue
        loopback = _loopback(d, snapshot, iface_by_id, problems, url("dcim.device", d["id"]))
        interfaces = _interfaces(name, ifaces_by_device[d["id"]], problems, url)
        devices[name] = DeviceIntent(
            id=d["id"],
            name=name,
            role=(d.get("role") or {}).get("slug"),
            site=(d.get("site") or {}).get("name"),
            url=url("dcim.device", d["id"]),
            loopback=loopback,
            interfaces=interfaces,
        )

    device_name = {d.id: n for n, d in devices.items()}
    terms: dict[int, list[Obj]] = defaultdict(list)
    for t in snapshot.terminations:
        terms[object_id(t.get("l2vpn")) or 0].append(t)
    pws = [
        pw
        for l2vpn in sorted(snapshot.l2vpns, key=lambda o: o["name"])
        if (pw := _pseudowire(l2vpn, terms[l2vpn["id"]], iface_by_id, device_name, devices, problems, url))
    ]
    pws = _check_services(pws, problems)
    # A broken PW leaves its service's xconnect on the spoke incomplete: block the spoke too.
    spokes = defaultdict(set)
    for pw in pws:
        spokes[pw.service_id].add(pw.a_end.host)
    problems[:] = [
        IntentProblem(p.message, p.scope, p.hosts | spokes[p.service_id], p.url, p.service_id) if p.service_id else p
        for p in problems
    ]

    customers = _customers(snapshot, pws, problems, url)
    return LabIntent(
        netbox_version=snapshot.netbox_version,
        public_url=base,
        devices=devices,
        pseudowires=pws,
        customers=customers,
        problems=problems,
    )


def _loopback(
    device: Obj, snapshot: Snapshot, iface_by_id: dict[int, Obj], problems: list[IntentProblem], url: str
) -> str | None:
    name = device["name"]
    candidates = []
    for ip in snapshot.ip_addresses:
        iface = (
            iface_by_id.get(ip.get("assigned_object_id"))
            if ip.get("assigned_object_type") == INTERFACE_OBJECT
            else None
        )
        if iface and object_id(iface["device"]) == device["id"] and choice(ip.get("role")) == IP_ROLE_LOOPBACK:
            candidates.append((iface["name"], _host(ip["address"])))
    if len(candidates) > 1:
        candidates = [c for c in candidates if c[0] == LOOPBACK_IF] or candidates
    hosts = sorted({c[1] for c in candidates})
    if len(hosts) == 1:
        return hosts[0]
    what = "no IP address with role loopback" if not hosts else f"several loopback addresses {hosts}"
    problems.append(IntentProblem(f"{name}: {what}", "device", frozenset({name}), url))
    return None


def _interfaces(host: str, ifaces: list[Obj], problems: list[IntentProblem], url: UrlFn) -> tuple[InterfaceIntent, ...]:
    by_id = {i["id"]: i for i in ifaces}
    out: list[InterfaceIntent] = []
    for i in sorted(ifaces, key=lambda i: i["name"]):
        where = f"{host} {i['name']}"
        link = url("dcim.interface", i["id"])
        role = choice(_cf(i, CF_IF_ROLE))
        parent_id = object_id(i.get("parent"))
        common = {
            "id": i["id"],
            "name": i["name"],
            "enabled": bool(i.get("enabled", True)),
            "mtu": i.get("mtu"),
            "description": i.get("description") or None,
        }
        if parent_id is not None:
            if role:
                problems.append(
                    IntentProblem(
                        f"{where}: a subinterface must not have {CF_IF_ROLE}", "device", frozenset({host}), link
                    )
                )
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                problems.append(
                    IntentProblem(f"{where}: parent is on another device", "device", frozenset({host}), link)
                )
                continue
            mode = choice(i.get("mode"))
            tagged = [v["vid"] for v in i.get("tagged_vlans") or []]
            untagged = i.get("untagged_vlan")
            if mode != MODE_TAGGED or len(tagged) != 1 or untagged:
                problems.append(
                    IntentProblem(
                        f"{where}: the VLAN of a subinterface is its single tagged VLAN (802.1Q mode tagged, no "
                        f"untagged VLAN); found mode {mode or '-'}, tagged {tagged}, untagged "
                        f"{(untagged or {}).get('vid', '-')}",
                        "device",
                        frozenset({host}),
                        link,
                    )
                )
                continue
            out.append(
                InterfaceIntent(type=InterfaceType.SUBINTERFACE, parent=parent["name"], vlan_id=tagged[0], **common)
            )
        elif role in _PHYSICAL_ROLES:
            out.append(InterfaceIntent(type=InterfaceType(role), **common))
        elif role:
            problems.append(IntentProblem(f"{where}: unknown {CF_IF_ROLE} {role!r}", "device", frozenset({host}), link))
    return tuple(out)


def _pseudowire(
    l2vpn: Obj,
    terminations: list[Obj],
    iface_by_id: dict[int, Obj],
    device_name: dict[int, str],
    devices: dict[str, DeviceIntent],
    problems: list[IntentProblem],
    url: UrlFn,
) -> PwIntent | None:
    name = l2vpn["name"]
    link = url("vpn.l2vpn", l2vpn["id"])
    found: list[str] = []
    hosts: set[str] = set()

    def end_of(iface_id: int | None, what: str) -> PwEndIntent | None:
        iface = iface_by_id.get(iface_id) if iface_id is not None else None
        if iface is None:
            found.append(f"{what} is not an interface of a netsim-clixon device")
            return None
        host = device_name[object_id(iface["device"])]
        hosts.add(host)
        loopback = devices[host].loopback
        if loopback is None:
            found.append(f"{what} {host} has no loopback")
            return None
        return PwEndIntent(host=host, ac=iface["name"], interface_id=iface["id"], loopback=loopback)

    l2vpn_type = choice(l2vpn.get("type"))
    if l2vpn_type != L2VPN_TYPE:
        found.append(f"type is {l2vpn_type}, expected {L2VPN_TYPE}")
    service_id = _cf(l2vpn, CF_SERVICE_ID)
    if not isinstance(service_id, str) or not re.match(SERVICE_ID_PATTERN, service_id):
        found.append(f"custom field {CF_SERVICE_ID} is {service_id!r}, expected SVC-NNNN")
    role = choice(_cf(l2vpn, CF_PW_ROLE))
    if role not in {r.value for r in PwRole}:
        found.append(f"custom field {CF_PW_ROLE} is {role!r}, expected primary or backup")
    vc_id = l2vpn.get("identifier")
    if not isinstance(vc_id, int) or not 1 <= vc_id <= _VC_ID_MAX:
        found.append(f"identifier (vc-id) is {vc_id!r}, expected 1..{_VC_ID_MAX}")

    spoke_id = object_id(_cf(l2vpn, CF_SPOKE_AC))
    a_end = end_of(spoke_id, f"spoke end ({CF_SPOKE_AC})") if spoke_id is not None else None
    if spoke_id is None:
        found.append(f"custom field {CF_SPOKE_AC} (spoke subinterface) is not set")
    hub_terms = [
        t
        for t in terminations
        if not (t.get("assigned_object_type") == INTERFACE_OBJECT and t.get("assigned_object_id") == spoke_id)
    ]
    if any(t.get("assigned_object_type") != INTERFACE_OBJECT for t in hub_terms):
        found.append("a termination is not an interface")
    z_end = None
    if len(hub_terms) != 1:
        found.append(f"expected exactly one termination (the hub subinterface), found {len(hub_terms)}")
    elif hub_terms[0].get("assigned_object_type") == INTERFACE_OBJECT:
        z_end = end_of(hub_terms[0].get("assigned_object_id"), "hub end (termination)")
    if a_end and z_end and a_end.host == z_end.host:
        found.append(f"both ends are on {a_end.host}")

    if found or a_end is None or z_end is None:
        service = service_id if isinstance(service_id, str) and re.match(SERVICE_ID_PATTERN, service_id) else None
        problem = IntentProblem(f"L2VPN {name}: " + "; ".join(found), "pseudowire", frozenset(hosts), link, service)
        problems.append(problem)
        return None
    tenant = l2vpn.get("tenant") or None
    return PwIntent(
        l2vpn_id=l2vpn["id"],
        name=name,
        service_id=service_id,
        role=PwRole(role),
        vc_id=vc_id,
        customer=tenant["name"] if tenant else None,
        tenant_id=tenant["id"] if tenant else None,
        status=choice(l2vpn.get("status")),
        url=link,
        a_end=a_end,
        z_end=z_end,
    )


def _check_services(pws: list[PwIntent], problems: list[IntentProblem]) -> list[PwIntent]:
    """PWs of one service must agree on spoke, spoke AC and customer, with distinct roles; hubs need one AC."""
    by_service: dict[str, list[PwIntent]] = defaultdict(list)
    for pw in pws:
        by_service[pw.service_id].append(pw)
    bad: set[int] = set()
    for service_id, members in by_service.items():
        hosts = frozenset(h for p in members for h in (p.a_end.host, p.z_end.host))
        names = ", ".join(p.name for p in members)
        issues = []
        roles = [p.role for p in members]
        if len(set(roles)) != len(roles):
            issues.append("two L2VPNs have the same pw_role")
        if len({(p.a_end.host, p.a_end.ac) for p in members}) > 1:
            issues.append("the PWs have different spoke ends (spoke_ac)")
        if len({p.customer for p in members}) > 1:
            issues.append("the PWs have different tenants")
        per_host: dict[str, set[str]] = defaultdict(set)
        for p in members:
            per_host[p.z_end.host].add(p.z_end.ac)
        if any(len(acs) > 1 for acs in per_host.values()):
            issues.append("two PWs end on one hub with different subinterfaces")
        if issues:
            problems.append(IntentProblem(f"service {service_id} ({names}): " + "; ".join(issues), "pseudowire", hosts))
            bad |= {p.l2vpn_id for p in members}
    return [p for p in pws if p.l2vpn_id not in bad]


def _customers(
    snapshot: Snapshot, pws: list[PwIntent], problems: list[IntentProblem], url: UrlFn
) -> dict[str, CustomerIntent]:
    group_ids = {g["id"] for g in snapshot.tenant_groups if g.get("slug") == TENANT_GROUP}
    owners = {pw.tenant_id for pw in pws}
    out: dict[str, CustomerIntent] = {}
    for t in sorted(snapshot.tenants, key=lambda t: t["name"]):
        in_group = object_id(t.get("group")) in group_ids
        if not in_group and t["id"] not in owners:
            continue
        tier = choice(_cf(t, CF_SLA_TIER))
        link = url("tenancy.tenant", t["id"])
        if tier not in {s.value for s in SlaTier}:
            if t["id"] in owners:
                problems.append(
                    IntentProblem(f"tenant {t['name']}: custom field {CF_SLA_TIER} is {tier!r}", "customer", url=link)
                )
            continue
        out[t["name"]] = CustomerIntent(t["id"], t["name"], t["slug"], SlaTier(tier), link, in_group)
    for pw in pws:
        if pw.customer is None:
            problems.append(IntentProblem(f"L2VPN {pw.name}: no tenant (customer)", "customer", url=pw.url))
    return out
