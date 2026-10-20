"""Link Zabbix problems to one root cause from API data alone: same ``service`` tag, overlapping start times.

This is the correlation the bot will do. Example (scenario ``fake-redundancy``): three problems on three hosts,
``missing`` on hub-a, ``spec`` on spoke-2 and ``unexpected`` on hub-b, all tagged ``service=SVC-0002`` and raised
within seconds of each other, form one group: "SVC-0002: one change, three symptoms".
"""

from __future__ import annotations

from typing import Any

#: Problem scopes that describe a pseudowire (contract section 9.2); trap hints, interfaces and availability
#: problems are context, not symptoms of one PW change.
PW_SCOPES = ("l2vpn-pw", "l2vpn-unexpected")
SEVERITY_ORDER = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")


def correlate(problems: list[dict[str, Any]], window_s: float = 120.0) -> list[dict[str, Any]]:
    """Groups of PW problems with the same ``service`` tag whose start times lie within ``window_s`` of each other.

    ``problems`` are :meth:`netsim_lab.zabbix.LabZabbix.problems` entries. A group's ``summary`` names the
    service, the checks and the hosts, so a reader (or the bot) sees one cause instead of N alarms.
    """
    by_service: dict[str, list[dict[str, Any]]] = {}
    for p in problems:
        if p.get("scope") in PW_SCOPES and p.get("service"):
            by_service.setdefault(p["service"], []).append(p)
    groups = []
    for service, items in sorted(by_service.items()):
        items.sort(key=lambda p: p["clock"])
        cluster: list[dict[str, Any]] = []
        for p in items:
            if cluster and p["clock"] - cluster[0]["clock"] > window_s:
                groups.append(_group(service, cluster))
                cluster = []
            cluster.append(p)
        if cluster:
            groups.append(_group(service, cluster))
    return groups


def _group(service: str, problems: list[dict[str, Any]]) -> dict[str, Any]:
    checks = sorted({p.get("check") or "?" for p in problems})
    hosts = sorted({p.get("host") or "?" for p in problems})
    roles = sorted({p.get("pw_role") for p in problems if p.get("pw_role")})
    worst = max(problems, key=lambda p: SEVERITY_ORDER.index(p["severity"]))["severity"]
    first, last = problems[0]["clock"], problems[-1]["clock"]
    return {
        "service": service,
        "problems": len(problems),
        "eventids": [p["eventid"] for p in problems],
        "checks": checks,
        "hosts": hosts,
        "roles": roles,
        "severity": worst,
        "first_clock": first,
        "last_clock": last,
        "spread_s": round(last - first, 3),
        "summary": (
            f"{service}: {len(problems)} problem(s) on {', '.join(hosts)} "
            f"({', '.join(checks)}) within {last - first:.1f} s: one root cause"
        ),
        "names": [p["name"] for p in problems],
    }
