"""Zabbix, read the way lab-api, labctl and the e2e tests need it (on top of session 04's ``netsim_zabbix``).

* business services: the BSM tree flattened to ``{service_id: status, leaves: {role: status}}``;
* problems on the lab hosts with their tags (every lab trigger carries ``host``, ``scope``; PW checks also
  ``service``, ``pw_role``, ``check``);
* the per-PW truth items ``netsim.pw.row[<service>,<role>]`` (JSON) and the walk master's ``lastclock``;
* "execute now" of the walk master (``task.create`` type 6), e.g. to verify a change without waiting for the poll.
"""

from __future__ import annotations

import json
import re
from typing import Any

from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.settings import load_settings as zabbix_settings
from netsim_zabbix.status import bsm_tree, status_name
from netsim_zabbix.sync import execute_now, managed_hosts

OK = "OK"
SEVERITIES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")
ROW_KEY = re.compile(r"^netsim\.pw\.row\[(?P<service>[^,\]]+),(?P<role>[^\]]+)\]$")


def severity_value(name: str) -> int:
    """``OK`` -> -1, ``Warning`` -> 2, ... (Zabbix service status / problem severity)."""
    return -1 if name == OK else SEVERITIES.index(name)


class LabZabbix:
    def __init__(self, api: ZabbixApi) -> None:
        self.api = api
        self._hostids: dict[str, str] | None = None

    @classmethod
    def connect(cls) -> LabZabbix:
        return cls(zabbix_settings().api())

    def close(self) -> None:
        self.api.close()

    def version(self) -> str:
        return self.api.version()

    # ------------------------------------------------------------------ hosts
    def hostids(self, refresh: bool = False) -> dict[str, str]:
        if self._hostids is None or refresh:
            self._hostids = managed_hosts(self.api)
        return self._hostids

    # ------------------------------------------------------------------ services
    def services(self) -> dict[str, Any]:
        """``{"root": status, "customers": {...}, "services": {SVC: {name, status, leaves: {role: {...}}}}}``."""
        root_status = None
        customers: dict[str, str] = {}
        services: dict[str, dict[str, Any]] = {}

        def visit(node: dict[str, Any]) -> None:
            nonlocal root_status
            tags = node["tags"]
            if tags.get("scope") == "l2vpn" and "service" not in tags:
                root_status = node["status_name"]
            elif "service" in tags and "pw_role" in tags:
                svc = services.setdefault(tags["service"], {"leaves": {}})
                svc["leaves"][tags["pw_role"]] = {"name": node["name"], "status": node["status_name"]}
            elif "service" in tags:
                svc = services.setdefault(tags["service"], {"leaves": {}})
                svc.update(
                    name=node["name"],
                    status=node["status_name"],
                    customer=tags.get("customer"),
                    spoke=tags.get("spoke"),
                    sla_tier=tags.get("sla_tier"),
                )
            elif "customer" in tags:
                customers[tags["customer"]] = node["status_name"]
            for child in node["children"]:
                visit(child)

        for node in bsm_tree(self.api):
            visit(node)
        return {"root": root_status, "customers": customers, "services": dict(sorted(services.items()))}

    def service_statuses(self) -> dict[str, str]:
        """``{SVC-xxxx: OK|Warning|...}`` for every managed service node."""
        return {s: v.get("status", "?") for s, v in self.services()["services"].items() if "status" in v}

    # ------------------------------------------------------------------ problems
    def problems(
        self, *, service: str | None = None, scopes: tuple[str, ...] | None = None, min_severity: int = 0
    ) -> list[dict[str, Any]]:
        """Open problems on the lab hosts (newest last). ``scopes`` filters on the ``scope`` tag."""
        hostids = list(self.hostids().values())
        if not hostids:
            return []
        params: dict[str, Any] = {
            "output": ["eventid", "objectid", "name", "severity", "clock", "ns", "opdata", "acknowledged"],
            "hostids": hostids,
            "selectTags": ["tag", "value"],
            "sortfield": ["eventid"],
            "sortorder": "ASC",
        }
        if service:
            params["tags"] = [{"tag": "service", "value": service, "operator": 1}]
        out = []
        for p in self.api.call("problem.get", params):
            tags = {t["tag"]: t["value"] for t in p["tags"]}
            if scopes and tags.get(N.TAG_SCOPE) not in scopes:
                continue
            severity = int(p["severity"])
            if severity < min_severity:
                continue
            out.append(
                {
                    "eventid": p["eventid"],
                    "triggerid": p["objectid"],
                    "name": p["name"],
                    "severity": SEVERITIES[severity],
                    "severity_value": severity,
                    "clock": int(p["clock"]) + int(p.get("ns") or 0) / 1e9,
                    "opdata": p.get("opdata") or "",
                    "acknowledged": p.get("acknowledged") == "1",
                    "host": tags.get("host"),
                    "service": tags.get("service"),
                    "pw_role": tags.get("pw_role"),
                    "check": tags.get("check"),
                    "scope": tags.get(N.TAG_SCOPE),
                    "tags": tags,
                }
            )
        return out

    # ------------------------------------------------------------------ items
    def items(self, key_prefix: str, hosts: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        ids = self.hostids()
        wanted = {h: ids[h] for h in (hosts or ids) if h in ids}
        by_id = {v: k for k, v in wanted.items()}
        out: dict[str, list[dict[str, Any]]] = {h: [] for h in wanted}
        if not wanted:
            return out
        for item in self.api.call(
            "item.get",
            {
                "output": ["itemid", "hostid", "key_", "name", "lastvalue", "lastclock", "lastns"],
                "hostids": list(wanted.values()),
                "search": {"key_": key_prefix},
                "startSearch": True,
            },
        ):
            item["clock"] = int(item["lastclock"]) + int(item.get("lastns") or 0) / 1e9
            out[by_id[item["hostid"]]].append(item)
        return out

    def pw_rows(self, hosts: list[str] | None = None) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
        """``{host: {(service, role): row}}`` from ``netsim.pw.row[...]``; ``row["clock"]`` = its lastclock."""
        out: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for host, items in self.items("netsim.pw.row[", hosts).items():
            rows = {}
            for item in items:
                m = ROW_KEY.match(item["key_"])
                if not m:
                    continue
                try:
                    row = json.loads(item["lastvalue"]) if item["lastvalue"] else {}
                except ValueError:
                    row = {"raw": item["lastvalue"]}
                rows[(m["service"], m["role"])] = {**row, "clock": item["clock"] if item["lastclock"] != "0" else 0.0}
            out[host] = rows
        return out

    def walk_items(self, hosts: list[str] | None = None) -> dict[str, dict[str, Any]]:
        out = {}
        for host, items in self.items(N.WALK_KEY, hosts).items():
            item = next((i for i in items if i["key_"] == N.WALK_KEY), None)
            if item is not None:
                out[host] = item
        return out

    def walk_clocks(self, hosts: list[str] | None = None) -> dict[str, float]:
        return {h: i["clock"] for h, i in self.walk_items(hosts).items()}

    def poll_now(self, hosts: list[str] | None = None) -> list[str]:
        """Execute now the walk master of ``hosts`` (default all); Zabbix runs it within ~5 s (task manager grid)."""
        items = self.walk_items(hosts)
        return execute_now(self.api, [i["itemid"] for i in items.values()])


def status_label(value: Any) -> str:
    return status_name(value)
