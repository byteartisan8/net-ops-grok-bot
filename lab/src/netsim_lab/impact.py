"""What losing (or maintaining) a host would do: intent blast radius combined with the live backup paths.

NetBox's blast radius (``netsim_netbox.intent.blast_radius``) answers "by design": losing hub-a *degrades* every
protected service of the North region, because each one keeps its backup through hub-b, and takes an unprotected
service on hub-a (no backup) **down**. "Degraded" is only true if the backup works *now*. This module checks the
surviving PWs in simd: if a surviving PW is already down (stale backup), losing the host takes the service **down**
(Disaster), and the result says why. Every service carries its customer's SLA tier and its status rules, and
``by_tier`` counts the live impact per tier, so a maintenance window can be weighed by who it hurts.
"""

from __future__ import annotations

from typing import Any

from netsim_common.bsm import status_rules_for
from netsim_lab.drift import ROLE_ORDER, plans_from_intent
from netsim_lab.status import _end

SEVERITY = {"down": "Disaster", "degraded": "Warning", "unaffected": "OK"}


def impact(lab: Any, host: str) -> dict[str, Any]:
    from netsim_netbox.intent import blast_radius

    lab.settings.device(host)
    intent = lab.intent()
    radius = blast_radius(host, intent)
    plans = plans_from_intent(intent)
    tiers = {name: (c.sla_tier.value if c.sla_tier else None) for name, c in intent.customers.items()}
    states = dict(lab.devices.states())
    try:
        zabbix_now = lab.zabbix().service_statuses()
    except Exception:
        zabbix_now = {}
    services = []
    for item in radius:
        plan = plans[item.service_id]
        lost = set(item.affected_roles)
        surviving = []
        for role, pw in sorted(plan.pws.items(), key=lambda kv: ROLE_ORDER.get(kv[0], 9)):
            if role in lost:
                continue
            ends = [
                _end(states, pw.spoke, pw.spoke_ac, item.service_id, role),
                _end(states, pw.hub, pw.hub_ac, item.service_id, role),
            ]
            surviving.append(
                {
                    "role": role,
                    "path": f"{pw.spoke} <-> {pw.hub}",
                    "up": all(e.up for e in ends),
                    "ends": {e.host: f"{e.oper}/{e.reason}" for e in ends},
                }
            )
        broken = [s for s in surviving if not s["up"]]
        if item.impact == "down":
            live = "down"
            why = f"every PW of {item.service_id} ends on {host}"
        elif item.impact == "unaffected":
            live = "unaffected"
            why = f"no PW of {item.service_id} ends on {host}"
        elif broken:
            live = "down"
            detail = "; ".join(
                f"{s['role']} {s['path']} is down ({', '.join(f'{h} {v}' for h, v in s['ends'].items())})"
                for s in broken
            )
            removed = "/".join(sorted(lost, key=ROLE_ORDER.get))
            why = f"losing {host} removes {removed} and the remaining path is already broken: {detail}"
        else:
            live = "degraded"
            why = f"{', '.join(s['role'] + ' via ' + s['path'] for s in surviving)} still up"
        services.append(
            {
                "service": item.service_id,
                "customer": item.customer,
                "sla_tier": tiers.get(item.customer or ""),
                "status_rules": status_rules_for(item.total_pws).value,
                "spoke": item.spoke,
                "intent_impact": item.impact,
                "intent_severity": SEVERITY[item.impact],
                "live_impact": live,
                "live_severity": SEVERITY[live],
                "lost_roles": sorted(lost, key=lambda r: ROLE_ORDER.get(r, 9)),
                "surviving": surviving,
                "zabbix_now": zabbix_now.get(item.service_id),
                "explanation": why,
                "worse_than_intent": live != item.impact,
            }
        )
    summary: dict[str, int] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for s in services:
        summary[s["live_impact"]] = summary.get(s["live_impact"], 0) + 1
        if s["live_impact"] != "unaffected":
            tier = by_tier.setdefault(s["sla_tier"] or "unknown", {})
            tier[s["live_impact"]] = tier.get(s["live_impact"], 0) + 1
    return {
        "host": host,
        "services": services,
        "summary": summary,
        "by_tier": by_tier,
        "worse_than_intent": [s["service"] for s in services if s["worse_than_intent"]],
    }
