"""Read-only questions the bot asks about intent (NetBox only; no device or Zabbix state).

* :func:`services_on_device` - services and PW roles terminating on a host;
* :func:`expected_pws` - every PW a host must have: (service, role, peer host, peer loopback, vc-id, AC);
* :func:`blast_radius` - per service, what losing a host does *by design*: ``down`` if every PW of the service
  terminates on the host (always the case for the service's own spoke, and for the hub of an unprotected
  service), ``degraded`` if some do, else ``unaffected``;
* :func:`service_detail` - customer, SLA tier, status rules, spoke and its PWs (two, or one if unprotected) with
  their ends and vc-ids.

All results are frozen dataclasses with ``to_dict()`` (JSON-ready).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from netsim_common.bsm import status_rules_for
from netsim_netbox.intent.model import IntentError, IntentProblem, LabIntent, PwIntent

Impact = Literal["down", "degraded", "unaffected"]


def _require_clean(intent: LabIntent, what: str) -> None:
    if problems := intent.spec_problems():
        raise IntentError(problems, what)


@dataclass(frozen=True)
class ServiceOnDevice:
    service_id: str
    customer: str | None
    spoke: str
    roles: tuple[str, ...]
    ac: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "roles": list(self.roles)}


@dataclass(frozen=True)
class ExpectedPw:
    service_id: str
    role: str
    peer_host: str
    peer_loopback: str
    vc_id: int
    ac: str
    customer: str | None
    netbox_l2vpn_id: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceImpact:
    service_id: str
    customer: str | None
    spoke: str
    impact: Impact
    affected_roles: tuple[str, ...]
    total_pws: int

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "affected_roles": list(self.affected_roles)}


def services_on_device(intent: LabIntent, host: str) -> list[ServiceOnDevice]:
    intent.device(host)
    _require_clean(intent, f"the services on {host}")
    out = []
    for service_id, pws in intent.services().items():
        local = [pw for pw in pws if pw.ends_on(host)]
        if local:
            out.append(
                ServiceOnDevice(
                    service_id=service_id,
                    customer=local[0].customer,
                    spoke=local[0].a_end.host,
                    roles=tuple(pw.role.value for pw in local),
                    ac=local[0].local_remote(host)[0].ac,
                )
            )
    return out


def expected_pws(intent: LabIntent, host: str) -> list[ExpectedPw]:
    intent.device(host)
    _require_clean(intent, f"the expected PWs of {host}")
    out = []
    for pw in intent.pws_on(host):
        local, remote = pw.local_remote(host)
        out.append(
            ExpectedPw(
                service_id=pw.service_id,
                role=pw.role.value,
                peer_host=remote.host,
                peer_loopback=remote.loopback,
                vc_id=pw.vc_id,
                ac=local.ac,
                customer=pw.customer,
                netbox_l2vpn_id=pw.l2vpn_id,
            )
        )
    return out


def blast_radius(intent: LabIntent, host: str) -> list[ServiceImpact]:
    """Impact on every service of losing ``host`` (sorted by service)."""
    intent.device(host)
    _require_clean(intent, f"the blast radius of {host}")
    out = []
    for service_id, pws in intent.services().items():
        hit = [pw for pw in pws if pw.ends_on(host)]
        impact: Impact = "down" if len(hit) == len(pws) else "degraded" if hit else "unaffected"
        out.append(
            ServiceImpact(
                service_id=service_id,
                customer=pws[0].customer,
                spoke=pws[0].a_end.host,
                impact=impact,
                affected_roles=tuple(pw.role.value for pw in hit),
                total_pws=len(pws),
            )
        )
    return out


def _pw_detail(pw: PwIntent) -> dict[str, Any]:
    return {
        "name": pw.name,
        "role": pw.role.value,
        "vc_id": pw.vc_id,
        "status": pw.status,
        "netbox_l2vpn_id": pw.l2vpn_id,
        "netbox_url": pw.url,
        "a_end": {"host": pw.a_end.host, "ac": pw.a_end.ac, "expected_peer": pw.z_end.loopback},
        "z_end": {"host": pw.z_end.host, "ac": pw.z_end.ac, "expected_peer": pw.a_end.loopback},
    }


def service_detail(intent: LabIntent, service_id: str) -> dict[str, Any]:
    _require_clean(intent, f"service {service_id}")
    pws = intent.services().get(service_id)
    if not pws:
        raise IntentError(
            [IntentProblem(f"no L2VPN with service_id {service_id}", "pseudowire")], f"service {service_id}"
        )
    customer = intent.customers.get(pws[0].customer or "")
    return {
        "service_id": service_id,
        "customer": pws[0].customer,
        "sla_tier": customer.sla_tier.value if customer and customer.sla_tier else None,
        "netbox_tenant_url": customer.url if customer else None,
        "spoke": pws[0].a_end.host,
        "status_rules": status_rules_for(len(pws)).value,
        "pseudowires": [_pw_detail(pw) for pw in pws],
    }
