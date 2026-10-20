"""BSM spec (contract section 9.3): the NetBox-derived description of the Zabbix business service tree.

Producers: ``netbox-intent bsm-spec`` (session 03, from NetBox) and :func:`bsm_spec_from_topology`
(from ``contract/topology.yaml``, for parallel development). Consumer: ``zabbix-provision`` /
``zabbix-sync`` (session 04). Schema: ``contract/bsm-spec.schema.json`` (generated from these models).

Build nodes with the ``build_*`` helpers so names, tags and descriptions are identical whichever source
produced the spec; the models reject names and tags that disagree with the node's own spec fields.
Compare specs from different sources with ``a.normalized() == b.normalized()`` (drops NetBox IDs/URLs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from netsim_common.datatypes import CustomerName, Hostname, InterfaceName, IPv4, ServiceId, VcId
from netsim_common.enums import PwRole, SlaTier
from netsim_common.topology import Topology

ROOT_NAME = "L2VPN"
TAG_SCOPE = "scope"
SCOPE_ROOT = "l2vpn"
SCOPE_PW = "l2vpn-pw"
SCOPE_UNEXPECTED = "l2vpn-unexpected"
PW_SPEC_MACRO = "{$NETSIM.PW.SPEC}"
NETBOX_TAG_PREFIX = "netbox_"


class StatusRules(StrEnum):
    """Status calculation of a service node (contract section 9.4), set by its number of PW leaves."""

    REDUNDANT_PAIR = "redundant-pair"
    UNPROTECTED = "unprotected"

    @property
    def rule(self) -> ServiceRule:
        return SERVICE_RULES[self]


@dataclass(frozen=True)
class ServiceRule:
    """>=N children at a severity or worse -> Disaster; >=M children at another severity or worse -> Warning."""

    name: StatusRules
    disaster_min_children: int
    disaster_child_severity: str = "High"
    warning_min_children: int = 1
    warning_child_severity: str = "Average"

    def describe(self) -> str:
        """The rule as one line of a node description."""

        def pws(n: int) -> str:
            return f">={n} PW{'s' if n > 1 else ''}"

        return (
            f"Status rule {self.name.value}: {pws(self.disaster_min_children)} at {self.disaster_child_severity} or "
            f"worse -> Disaster; {pws(self.warning_min_children)} at {self.warning_child_severity} or worse -> "
            "Warning; else OK."
        )


#: Primary + backup PW: both down -> Disaster (down); one down or off-spec -> Warning (degraded or at risk).
REDUNDANT_PAIR = ServiceRule(StatusRules.REDUNDANT_PAIR, disaster_min_children=2)
#: Primary PW only: down -> Disaster; off-spec -> Warning.
UNPROTECTED = ServiceRule(StatusRules.UNPROTECTED, disaster_min_children=1)
SERVICE_RULES = {rule.name: rule for rule in (REDUNDANT_PAIR, UNPROTECTED)}


def status_rules_for(pw_count: int) -> StatusRules:
    """A service with two PW leaves is ``redundant-pair``, one with a single leaf ``unprotected``."""
    return StatusRules.REDUNDANT_PAIR if pw_count >= 2 else StatusRules.UNPROTECTED


class PwCheck(StrEnum):
    """Zabbix checks on pseudowires (contract section 9.2)."""

    MISSING = "missing"
    OPER = "oper"
    SPEC = "spec"
    UNEXPECTED = "unexpected"

    @property
    def severity(self) -> str:
        return {"missing": "High", "oper": "High", "spec": "Average", "unexpected": "Warning"}[self.value]

    @property
    def scope(self) -> str:
        return SCOPE_UNEXPECTED if self is PwCheck.UNEXPECTED else SCOPE_PW


Tags = dict[str, str]


def _with_url(lines: list[str], label: str, url: str | None) -> str:
    return "\n".join([*lines, f"{label}: {url}"] if url else lines)


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    tags: Tags = Field(description="Zabbix service tags; spec-derived tags are mandatory, extra tags allowed")
    description: str = Field(description="Human-readable spec of the node, including its NetBox URL when known")
    netbox_url: str | None = Field(
        default=None, description="NetBox UI URL of the source object (null if not from NetBox)"
    )

    def _check(self, expected_name: str, expected_tags: Tags) -> None:
        if self.name != expected_name:
            raise ValueError(f"node name {self.name!r} must be {expected_name!r}")
        wrong = {k: v for k, v in expected_tags.items() if self.tags.get(k) != v}
        if wrong:
            raise ValueError(f"node {self.name!r}: tags must include {wrong} (got {self.tags})")


# ---------------------------------------------------------------------------------------- models
class BsmRoot(_Node):
    """Root node ``L2VPN``: most critical child."""

    @model_validator(mode="after")
    def _spec(self) -> Self:
        self._check(ROOT_NAME, root_tags())
        return self


class PwEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: Hostname
    ac: InterfaceName = Field(description="Attachment circuit (subinterface) on this host")
    expected_peer: IPv4 = Field(
        description="PW peer address this end must be configured with (the other end's loopback)"
    )


class BsmPseudowire(_Node):
    """Leaf: one pseudowire. Matches problems tagged scope=l2vpn-pw, service, pw_role from both ends."""

    role: PwRole
    vc_id: VcId
    netbox_l2vpn_id: int | None = Field(default=None, description="NetBox L2VPN object ID (null if not from NetBox)")
    ends: list[PwEnd] = Field(min_length=2, max_length=2, description="[a-end (spoke), z-end (hub)]")

    @property
    def a_end(self) -> PwEnd:
        return self.ends[0]

    @property
    def z_end(self) -> PwEnd:
        return self.ends[1]

    @model_validator(mode="after")
    def _spec(self) -> Self:
        if self.a_end.host == self.z_end.host:
            raise ValueError(f"pseudowire {self.name!r}: both ends on {self.a_end.host}")
        service_id = self.tags.get("service", "")
        self._check(
            pw_name(service_id, self.role, self.z_end.host), pw_tags(service_id, self.role, self.vc_id, self.ends)
        )
        return self

    def problem_tags(self) -> Tags:
        """Problem tags this leaf maps (contract section 9.4)."""
        return {TAG_SCOPE: SCOPE_PW, "service": self.tags["service"], "pw_role": self.role.value}


class BsmService(_Node):
    service_id: ServiceId
    spoke: Hostname
    status_rules: StatusRules = Field(
        description="redundant-pair with two PW leaves (dual-homed), unprotected with one (single-homed)"
    )
    pseudowires: list[BsmPseudowire] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _spec(self) -> Self:
        roles = [pw.role for pw in self.pseudowires]
        if len(set(roles)) != len(roles):
            raise ValueError(f"service {self.service_id}: duplicate PW roles {roles}")
        if self.status_rules is not (want := status_rules_for(len(self.pseudowires))):
            raise ValueError(
                f"service {self.service_id}: {len(self.pseudowires)} PW leaf/leaves need status rules {want.value}, "
                f"got {self.status_rules.value}"
            )
        for pw in self.pseudowires:
            if pw.tags.get("service") != self.service_id:
                raise ValueError(f"service {self.service_id}: PW {pw.name!r} belongs to {pw.tags.get('service')}")
            if pw.a_end.host != self.spoke:
                raise ValueError(f"service {self.service_id}: PW {pw.name!r} a-end must be the spoke {self.spoke}")
        customer = self.tags.get("customer", "")
        self._check(service_name(self.service_id, self.spoke), service_tags(self.service_id, customer, self.spoke))
        return self

    def pw(self, role: PwRole | str) -> BsmPseudowire | None:
        role = PwRole(role)
        return next((p for p in self.pseudowires if p.role is role), None)


class BsmCustomer(_Node):
    sla_tier: SlaTier
    services: list[BsmService] = Field(default_factory=list)

    @model_validator(mode="after")
    def _spec(self) -> Self:
        self._check(self.name, customer_tags(self.name, self.sla_tier))
        for svc in self.services:
            if svc.tags.get("customer") != self.name:
                raise ValueError(f"customer {self.name}: service {svc.service_id} is tagged {svc.tags.get('customer')}")
        return self


class BsmSpec(BaseModel):
    """The whole BSM spec (contract section 9.3)."""

    model_config = ConfigDict(extra="forbid", title="NetSim BSM spec")

    root: BsmRoot
    customers: list[BsmCustomer]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [c.name for c in self.customers]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate customers in {names}")
        ids = [s.service_id for s in self.services()]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate service ids in {ids}")
        return self

    def services(self) -> list[BsmService]:
        return [s for c in self.customers for s in c.services]

    def service(self, service_id: str) -> BsmService:
        for s in self.services():
            if s.service_id == service_id:
                return s
        raise KeyError(f"service {service_id!r} not in the BSM spec")

    def customer_of(self, service_id: str) -> BsmCustomer:
        for c in self.customers:
            if any(s.service_id == service_id for s in c.services):
                return c
        raise KeyError(f"service {service_id!r} not in the BSM spec")

    def pseudowires(self) -> list[tuple[BsmService, BsmPseudowire]]:
        return [(s, pw) for s in self.services() for pw in s.pseudowires]

    def hosts(self) -> list[str]:
        """Every host terminating at least one PW (sorted)."""
        return sorted({end.host for _, pw in self.pseudowires() for end in pw.ends})

    def sorted(self) -> BsmSpec:
        """Copy with customers by name, services by id and PWs primary-first."""
        customers = []
        for c in sorted(self.customers, key=lambda c: c.name):
            services = [
                s.model_copy(update={"pseudowires": sorted(s.pseudowires, key=lambda p: p.role.sort_index)})
                for s in sorted(c.services, key=lambda s: s.service_id)
            ]
            customers.append(c.model_copy(update={"services": services}))
        return self.model_copy(update={"customers": customers})

    def normalized(self) -> BsmSpec:
        """Source-independent form: sorted, NetBox IDs/URLs and ``netbox_*`` tags removed, descriptions re-rendered."""
        spec = self.sorted()
        customers = []
        for c in spec.customers:
            services = []
            for s in c.services:
                pws = [build_pseudowire(s.service_id, pw.role, pw.vc_id, pw.a_end, pw.z_end) for pw in s.pseudowires]
                services.append(build_service(s.service_id, c.name, s.spoke, pws))
            customers.append(build_customer(c.name, c.sla_tier, services))
        return BsmSpec(root=build_root(), customers=customers)

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent) + ("\n" if indent else "")


# ---------------------------------------------------------------------------------------- naming
def root_tags() -> Tags:
    return {TAG_SCOPE: SCOPE_ROOT}


def customer_tags(customer: str, sla_tier: SlaTier | str) -> Tags:
    return {"customer": customer, "sla_tier": SlaTier(sla_tier).value}


def service_name(service_id: str, spoke: str) -> str:
    return f"{service_id} ({spoke})"


def service_tags(service_id: str, customer: str, spoke: str) -> Tags:
    return {"service": service_id, "customer": customer, "spoke": spoke}


def pw_name(service_id: str, role: PwRole | str, hub: str) -> str:
    return f"{service_id} {PwRole(role).value} ({hub})"


def pw_tags(service_id: str, role: PwRole | str, vc_id: int, ends: list[PwEnd]) -> Tags:
    return {
        "service": service_id,
        "pw_role": PwRole(role).value,
        "vcid": str(vc_id),
        "a_end": ends[0].host,
        "z_end": ends[1].host,
    }


# ---------------------------------------------------------------------------------------- builders
def build_root(*, netbox_url: str | None = None) -> BsmRoot:
    lines = [
        "NetSim L2VPN business services (VPWS hub-and-spoke: dual-homed or unprotected), generated from NetBox intent.",
        "Status: most critical customer.",
    ]
    return BsmRoot(
        name=ROOT_NAME, tags=root_tags(), description=_with_url(lines, "NetBox", netbox_url), netbox_url=netbox_url
    )


def build_customer(
    name: str,
    sla_tier: SlaTier | str,
    services: list[BsmService],
    *,
    netbox_url: str | None = None,
    extra_tags: Tags | None = None,
) -> BsmCustomer:
    """``extra_tags`` carries source-specific tags such as ``netbox_tenant_id``."""
    tier = SlaTier(sla_tier)
    lines = [f"Customer {name}, SLA tier {tier.value}.", "Status: most critical service."]
    return BsmCustomer(
        name=name,
        tags={**customer_tags(name, tier), **(extra_tags or {})},
        description=_with_url(lines, "NetBox tenant", netbox_url),
        netbox_url=netbox_url,
        sla_tier=tier,
        services=services,
    )


def build_service(
    service_id: str,
    customer: str,
    spoke: str,
    pseudowires: list[BsmPseudowire],
    *,
    netbox_url: str | None = None,
    extra_tags: Tags | None = None,
) -> BsmService:
    status_rules = status_rules_for(len(pseudowires))
    lines = [f"{service_id} for {customer} on {spoke}.", status_rules.rule.describe()]
    for pw in sorted(pseudowires, key=lambda p: p.role.sort_index):
        lines.append(
            f"{pw.role.value}: vc-id {pw.vc_id}, {pw.a_end.host} {pw.a_end.ac} <-> {pw.z_end.host} {pw.z_end.ac}"
        )
    if status_rules is StatusRules.UNPROTECTED:
        lines.append("Unprotected (one PW, no second hub): when that PW fails, the service is down.")
    return BsmService(
        name=service_name(service_id, spoke),
        tags={**service_tags(service_id, customer, spoke), **(extra_tags or {})},
        description=_with_url(lines, "NetBox", netbox_url),
        netbox_url=netbox_url,
        service_id=service_id,
        spoke=spoke,
        status_rules=status_rules,
        pseudowires=pseudowires,
    )


def build_pseudowire(
    service_id: str,
    role: PwRole | str,
    vc_id: int,
    a_end: PwEnd,
    z_end: PwEnd,
    *,
    netbox_l2vpn_id: int | None = None,
    netbox_url: str | None = None,
    extra_tags: Tags | None = None,
) -> BsmPseudowire:
    """``a_end`` is the spoke end, ``z_end`` the hub end."""
    role = PwRole(role)
    ends = [a_end, z_end]
    lines = [
        f"{service_id} {role.value} pseudowire, VC ID {vc_id}.",
        f"A-end {a_end.host} {a_end.ac} expects peer {a_end.expected_peer} ({z_end.host}).",
        f"Z-end {z_end.host} {z_end.ac} expects peer {z_end.expected_peer} ({a_end.host}).",
        f"Problems: {TAG_SCOPE}={SCOPE_PW} service={service_id} pw_role={role.value} "
        f"(checks: {', '.join(c.value for c in PwCheck if c.scope == SCOPE_PW)}).",
    ]
    return BsmPseudowire(
        name=pw_name(service_id, role, z_end.host),
        tags={**pw_tags(service_id, role, vc_id, ends), **(extra_tags or {})},
        description=_with_url(lines, "NetBox L2VPN", netbox_url),
        netbox_url=netbox_url,
        role=role,
        vc_id=vc_id,
        netbox_l2vpn_id=netbox_l2vpn_id,
        ends=ends,
    )


# ---------------------------------------------------------------------------------------- topology
def bsm_spec_from_topology(topology: Topology) -> BsmSpec:
    """The BSM spec NetBox should produce for a freshly seeded lab (no NetBox IDs/URLs)."""
    customers = []
    for customer in sorted(topology.customers, key=lambda c: c.name):
        services = []
        for svc in sorted((s for s in topology.services if s.customer == customer.name), key=lambda s: s.id):
            spoke = topology.device(svc.spoke)
            pws = []
            for role in svc.roles:
                hub = topology.device(svc.pw(role).hub)
                pws.append(
                    build_pseudowire(
                        svc.id,
                        role,
                        svc.pw(role).vc_id,
                        PwEnd(host=spoke.hostname, ac=svc.spoke_ac, expected_peer=hub.loopback),
                        PwEnd(host=hub.hostname, ac=svc.hub_ac, expected_peer=spoke.loopback),
                    )
                )
            services.append(build_service(svc.id, customer.name, spoke.hostname, pws))
        customers.append(build_customer(customer.name, customer.sla_tier, services))
    return BsmSpec(root=build_root(), customers=customers)


# ---------------------------------------------------------------------------------------- {$NETSIM.PW.SPEC}
class PwSpecEntry(BaseModel):
    """One element of a host's ``{$NETSIM.PW.SPEC}`` macro (contract section 9.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceId
    role: PwRole
    vc_id: VcId
    expected_peer: IPv4 = Field(description="Peer address this host must use for the PW")
    expected_peer_host: Hostname = Field(description="Host at the other end")
    customer: CustomerName
    ac: InterfaceName = Field(description="Attachment circuit on this host")


def pw_spec_for_host(spec: BsmSpec, host: str) -> list[PwSpecEntry]:
    """Expected PWs of ``host`` (both ends of every leaf count), sorted by service then role."""
    entries = []
    for svc, pw in spec.pseudowires():
        customer = svc.tags["customer"]
        for local, remote in ((pw.a_end, pw.z_end), (pw.z_end, pw.a_end)):
            if local.host == host:
                entries.append(
                    PwSpecEntry(
                        service=svc.service_id,
                        role=pw.role,
                        vc_id=pw.vc_id,
                        expected_peer=local.expected_peer,
                        expected_peer_host=remote.host,
                        customer=customer,
                        ac=local.ac,
                    )
                )
    return sorted(entries, key=lambda e: (e.service, e.role.sort_index))


def pw_spec_macro_value(spec: BsmSpec, host: str) -> str:
    """Compact JSON value of ``{$NETSIM.PW.SPEC}`` for ``host`` (deterministic)."""
    return json.dumps([e.model_dump(mode="json") for e in pw_spec_for_host(spec, host)], separators=(",", ":"))
