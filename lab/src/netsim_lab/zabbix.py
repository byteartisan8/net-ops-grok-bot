"""Zabbix, read the way lab-api, labctl and the e2e tests need it (on top of session 04's ``netsim_zabbix``).

* business services: the BSM tree flattened to ``{service_id: status, leaves: {role: status}}``;
* problems on the lab hosts with their tags (every lab trigger carries ``host``, ``scope``; PW checks also
  ``service``, ``pw_role``, ``check``);
* the per-PW truth items ``netsim.pw.row[<service>,<role>]`` (JSON) and the walk master's ``lastclock``;
* "execute now" of the walk master (``task.create`` type 6), e.g. to verify a change without waiting for the poll;
* direct reads for the NOC roles (:meth:`LabZabbix.read`: an allow-list of ``*.get`` methods, with a default
  ``limit``) and acknowledgments as a role's own Zabbix user (:meth:`LabZabbix.acknowledge`). Service events cannot
  be acknowledged in Zabbix 8.0 ("No permissions"), so a service event (a customer alert) stands for the open trigger
  problems of its subtree (``selectProblemEvents`` of the node and its descendants: only the PW leaves list
  problems), and those are acknowledged.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.settings import load_settings as zabbix_settings
from netsim_zabbix.status import bsm_tree, status_name
from netsim_zabbix.sync import execute_now, managed_hosts

OK = "OK"
SEVERITIES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")
ROW_KEY = re.compile(r"^netsim\.pw\.row\[(?P<service>[^,\]]+),(?P<role>[^\]]+)\]$")

#: What the NOC roles may read directly (``POST /zabbix/api``); every other method is refused.
READ_METHODS = (
    "problem.get",
    "event.get",
    "trigger.get",
    "item.get",
    "history.get",
    "trend.get",
    "host.get",
    "hostgroup.get",
    "service.get",
    "sla.get",
    "sla.getsli",
    "alert.get",
    "maintenance.get",
)
DEFAULT_LIMIT = 1000
MAX_LIMIT = 10_000
ACK_MESSAGE_MAX = 2048  # acknowledges.message varchar(2048) in Zabbix 8.0
EVENT_SOURCE_TRIGGERS, EVENT_SOURCE_SERVICE = 0, 4
EVENT_OBJECT_TRIGGER, EVENT_OBJECT_SERVICE = 0, 6
ACK_ACKNOWLEDGE, ACK_MESSAGE = 2, 4


class ReadRefused(ValueError):
    """A direct read the NOC may not make (method not on the allow-list, malformed params)."""


def severity_value(name: str) -> int:
    """``OK`` -> -1, ``Warning`` -> 2, ... (Zabbix service status / problem severity)."""
    return -1 if name == OK else SEVERITIES.index(name)


#: How long :meth:`LabZabbix.hostids` trusts its host map.
HOSTIDS_TTL_S = 60.0


class LabZabbix:
    def __init__(self, api: ZabbixApi) -> None:
        self.api = api
        self._hostids: dict[str, str] | None = None
        self._hostids_at = 0.0

    @classmethod
    def connect(cls) -> LabZabbix:
        return cls(zabbix_settings().api())

    def close(self) -> None:
        self.api.close()

    def version(self) -> str:
        return self.api.version()

    # ------------------------------------------------------------------ hosts
    def hostids(self, refresh: bool = False) -> dict[str, str]:
        """``{host: hostid}`` of the hosts zabbix-provision manages, kept for :data:`HOSTIDS_TTL_S`. An empty map is
        never kept: lab-api (and grok-bot's feed through it) asks before ``make seed`` has created the hosts, and a
        map cached then made every reset wait in vain for polls of hosts it could not see."""
        now = time.monotonic()
        if refresh or not self._hostids or now - self._hostids_at > HOSTIDS_TTL_S:
            self._hostids = managed_hosts(self.api)
            self._hostids_at = now
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

    # ------------------------------------------------------------------ direct reads (NOC roles)
    def read(self, method: str, params: Any) -> Any:
        """One allow-listed read with lab-api's token. ``*.get`` calls get ``limit`` 1000 unless they set one
        (at most 10000); ``sla.getsli`` takes its own parameters."""
        if method not in READ_METHODS:
            raise ReadRefused(f"{method!r} is not a read the NOC may make (allowed: {', '.join(READ_METHODS)})")
        if not isinstance(params, dict):
            raise ReadRefused("params must be a JSON object")
        params = dict(params)
        if method.endswith(".get"):
            try:
                limit = int(params.get("limit") or DEFAULT_LIMIT)
            except (TypeError, ValueError):
                raise ReadRefused("limit must be a number") from None
            params["limit"] = max(1, min(limit, MAX_LIMIT))
        return self.api.call(method, params)

    # ------------------------------------------------------------------ acknowledgments
    def problems_behind(self, eventids: list[str]) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
        """``({given eventid: [open trigger problem eventids]}, [{eventid, why}])`` for trigger problem events of
        the lab hosts (as they are) and service events (the trigger problems of the service's subtree)."""
        hostids = list(self.hostids().values())
        out: dict[str, list[str]] = {}
        skipped: list[dict[str, str]] = []
        if not eventids:
            return out, skipped
        triggers = {
            e["eventid"]: e
            for e in self.api.call(
                "event.get",
                {
                    "eventids": eventids,
                    "hostids": hostids,
                    "source": EVENT_SOURCE_TRIGGERS,
                    "object": EVENT_OBJECT_TRIGGER,
                    "output": ["eventid", "value"],
                },
            )
        }
        services = {
            e["eventid"]: e
            for e in self.api.call(
                "event.get",
                {
                    "eventids": eventids,
                    "source": EVENT_SOURCE_SERVICE,
                    "object": EVENT_OBJECT_SERVICE,
                    "output": ["eventid", "objectid", "value", "name"],
                },
            )
        }
        by_service: dict[str, list[str]] = {}
        for sid in sorted({e["objectid"] for e in services.values()}):
            # Zabbix 8.0.0rc1 lists problem events on the PW leaves only (a customer node's problem_events stays
            # empty, verified on the running server), so collect them from the customer's whole subtree.
            nodes = self.api.call(
                "service.get",
                {"serviceids": [sid], "output": ["serviceid"], "selectProblemEvents": ["eventid"]},
            ) + self.api.call(
                "service.get",
                {
                    "parentids": [sid],
                    "deep_parentids": True,
                    "output": ["serviceid"],
                    "selectProblemEvents": ["eventid"],
                },
            )
            found = {p["eventid"] for n in nodes for p in n.get("problem_events") or []}
            by_service[sid] = sorted(found, key=int)
        for eid in eventids:
            if eid in triggers:
                if triggers[eid]["value"] != "1":
                    skipped.append({"eventid": eid, "why": "an OK event, not a problem"})
                else:
                    out[eid] = [eid]
            elif eid in services:
                behind = by_service.get(services[eid]["objectid"], [])
                if behind:
                    out[eid] = behind
                else:
                    skipped.append({"eventid": eid, "why": "the service has no open problem behind it (recovered?)"})
            else:
                skipped.append({"eventid": eid, "why": "no such problem event on the lab hosts and no service event"})
        return out, skipped

    def acknowledge(self, eventids: list[str], message: str, role_api: ZabbixApi) -> dict[str, Any]:
        """Acknowledge (with ``message``) the open trigger problems behind ``eventids`` as the user of
        ``role_api``. Problems already acknowledged only get the message."""
        resolved, skipped = self.problems_behind(eventids)
        problem_ids = sorted({p for ps in resolved.values() for p in ps}, key=int)
        open_problems = {
            p["eventid"]: p
            for p in (
                self.api.call(
                    "problem.get",
                    {"eventids": problem_ids, "output": ["eventid", "name", "acknowledged", "severity"]},
                )
                if problem_ids
                else []
            )
        }
        for pid in problem_ids:
            if pid not in open_problems:
                skipped.append({"eventid": pid, "why": "resolved in the meantime"})
        text = message[:ACK_MESSAGE_MAX]
        fresh = [p for p in problem_ids if p in open_problems and open_problems[p].get("acknowledged") != "1"]
        again = [p for p in problem_ids if p in open_problems and open_problems[p].get("acknowledged") == "1"]
        if fresh:
            role_api.call(
                "event.acknowledge", {"eventids": fresh, "action": ACK_ACKNOWLEDGE | ACK_MESSAGE, "message": text}
            )
        if again:
            role_api.call("event.acknowledge", {"eventids": again, "action": ACK_MESSAGE, "message": text})
        return {
            "acknowledged": fresh,
            "commented": again,
            "resolved": {k: v for k, v in resolved.items() if v != [k]},
            "skipped": skipped,
            "problems": [
                {
                    "eventid": p,
                    "name": open_problems[p]["name"],
                    "severity": SEVERITIES[int(open_problems[p]["severity"])],
                }
                for p in problem_ids
                if p in open_problems
            ],
        }


def status_label(value: Any) -> str:
    return status_name(value)
