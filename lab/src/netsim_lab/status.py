"""Service status: NetBox intent, live device state (simd) and Zabbix business service status side by side.

Each source may be unavailable on its own (NetBox down, a device stopped, Zabbix restarting): the view then shows
what the others know and names the missing source in ``errors``. Used by ``labctl status [--watch]`` and lab-api's
``GET /services``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from netsim_lab.drift import ROLE_ORDER, ServicePlan, plans_from_intent, plans_from_topology


@dataclass
class EndView:
    host: str
    ac: str
    oper: str | None = None  # up | down | lowerLayerDown | absent | unreachable
    reason: str | None = None
    peer: str | None = None
    vc_id: int | None = None

    @property
    def up(self) -> bool:
        return self.oper == "up"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "ac": self.ac,
            "oper": self.oper,
            "reason": self.reason,
            "peer": self.peer,
            "vc_id": self.vc_id,
        }


@dataclass
class PwView:
    role: str
    vc_id: int
    hub: str
    spoke_end: EndView
    hub_end: EndView
    zabbix: str | None = None

    @property
    def up(self) -> bool:
        return self.spoke_end.up and self.hub_end.up

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "vc_id": self.vc_id,
            "hub": self.hub,
            "up": self.up,
            "spoke_end": self.spoke_end.to_dict(),
            "hub_end": self.hub_end.to_dict(),
            "zabbix_leaf": self.zabbix,
        }


@dataclass
class ServiceView:
    service: str
    customer: str | None
    spoke: str
    sla_tier: str | None
    pws: list[PwView]
    zabbix: str | None = None
    in_intent: bool = True

    @property
    def live(self) -> str:
        """``up`` (every PW up), ``degraded`` (some) or ``down`` (none), from simd."""
        ups = sum(pw.up for pw in self.pws)
        return "up" if ups == len(self.pws) else "down" if ups == 0 else "degraded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "customer": self.customer,
            "spoke": self.spoke,
            "sla_tier": self.sla_tier,
            "in_netbox": self.in_intent,
            "live": self.live,
            "zabbix": self.zabbix,
            "pseudowires": [p.to_dict() for p in self.pws],
        }


@dataclass
class StatusSnapshot:
    services: list[ServiceView]
    devices: dict[str, dict[str, Any]]
    zabbix_root: str | None
    errors: dict[str, str] = field(default_factory=dict)
    generated_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    def service(self, service_id: str) -> ServiceView | None:
        return next((s for s in self.services if s.service == service_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": [s.to_dict() for s in self.services],
            "devices": self.devices,
            "zabbix_root": self.zabbix_root,
            "errors": self.errors,
            "generated_at": self.generated_at,
            "duration_s": round(self.duration_s, 3),
        }


def _end(states: dict[str, Any], host: str, ac: str, service: str, role: str) -> EndView:
    st = states.get(host)
    if st is None or isinstance(st, Exception):
        return EndView(host, ac, "unreachable", None)
    row = next((p for p in st.get("pseudowires", []) if p["service_id"] == service and p["role"] == role), None)
    if row is None:
        return EndView(host, ac, "absent", "not configured")
    return EndView(host, row.get("ac") or ac, row["oper"], row["reason"], row.get("peer"), row.get("vc_id"))


def build_views(
    plans: dict[str, ServicePlan],
    states: dict[str, Any],
    zabbix_services: dict[str, Any] | None,
    sla: dict[str, str | None] | None = None,
) -> list[ServiceView]:
    views = []
    zsvc = (zabbix_services or {}).get("services", {}) if zabbix_services else {}
    for service, plan in sorted(plans.items()):
        z = zsvc.get(service, {})
        pws = []
        for role, pw in sorted(plan.pws.items(), key=lambda kv: ROLE_ORDER.get(kv[0], 9)):
            pws.append(
                PwView(
                    role=role,
                    vc_id=pw.vc_id,
                    hub=pw.hub,
                    spoke_end=_end(states, pw.spoke, pw.spoke_ac, service, role),
                    hub_end=_end(states, pw.hub, pw.hub_ac, service, role),
                    zabbix=(z.get("leaves", {}).get(role) or {}).get("status"),
                )
            )
        views.append(
            ServiceView(
                service=service,
                customer=plan.customer,
                spoke=plan.spoke,
                sla_tier=(sla or {}).get(service) or z.get("sla_tier"),
                pws=pws,
                zabbix=z.get("status"),
            )
        )
    # services Zabbix still has but NetBox no longer has (pruned on the next sync)
    for service, z in zsvc.items():
        if service not in plans and "status" in z:
            views.append(
                ServiceView(
                    service, z.get("customer"), z.get("spoke") or "?", z.get("sla_tier"), [], z["status"], False
                )
            )
    return views


def device_summary(states: dict[str, Any], stopped: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for host, st in states.items():
        if isinstance(st, Exception):
            out[host] = {"reachable": False, "stopped": host in stopped, "error": str(st)}
            continue
        out[host] = {
            "reachable": True,
            "stopped": False,
            "uplink": st.get("uplink", {}).get("oper"),
            "faults": st.get("faults", {}),
            "config_hash": (st.get("config_hash") or "")[:12],
            "uptime_s": st.get("uptime_s"),
            "pws_up": sum(1 for p in st.get("pseudowires", []) if p["oper"] == "up"),
            "pws": len(st.get("pseudowires", [])),
        }
    return out


def collect(lab: Any, *, intent_max_age: float = 5.0, with_zabbix: bool = True) -> StatusSnapshot:
    """One status snapshot of the lab (NetBox intent cached up to ``intent_max_age`` seconds)."""
    start = time.monotonic()
    errors: dict[str, str] = {}
    sla: dict[str, str | None] = {}
    try:
        intent = lab.intent(max_age=intent_max_age)
        plans = plans_from_intent(intent)
        for service, pws in intent.services().items():
            customer = intent.customers.get(pws[0].customer or "")
            sla[service] = customer.sla_tier.value if customer and customer.sla_tier else None
    except Exception as exc:  # NetBox down or intent broken: fall back to the topology
        errors["netbox"] = f"{type(exc).__name__}: {exc}"
        plans = plans_from_topology(lab.topology)
    states = dict(lab.devices.states())
    zservices = None
    if with_zabbix:
        try:
            zservices = lab.zabbix().services()
        except Exception as exc:
            errors["zabbix"] = f"{type(exc).__name__}: {exc}"
    stopped = lab.stopped() if any(isinstance(s, Exception) for s in states.values()) else []
    return StatusSnapshot(
        services=build_views(plans, states, zservices, sla),
        devices=device_summary(states, stopped),
        zabbix_root=(zservices or {}).get("root"),
        errors=errors,
        duration_s=time.monotonic() - start,
    )
