"""The business service tree (contract 9.4) and the SLAs, diffed against Zabbix and applied.

Tree: root ``L2VPN`` -> customers -> ``SVC-xxxx (spoke)`` -> PW leaves. Every node carries the spec's tags and its
description (NetBox URL, expected values), plus ``managed_by=netsim-zabbix``: only services with that tag are ever
updated or pruned, so hand-made services are left alone. Identity is the node name (unique by construction).

Status calculation (Zabbix 8.0 service semantics: additional rules can only raise the status computed by the main
rule, and the highest matching rule wins):

* leaf: problems tagged ``scope=l2vpn-pw``, ``service=<id>``, ``pw_role=<role>`` (from both PW ends);
* service (``redundant-pair``): main rule "set status to OK" + additional rules ">= 2 children at High or above ->
  Disaster" and ">= 1 child at Average or above -> Warning";
* customer, root: "most critical of child services".

SLAs: one per ``sla_tier`` (``L2VPN gold`` 99.95 %, ``L2VPN silver`` 99.9 %, from contract/topology.yaml),
selecting services by the service tag ``sla_tier``, which customers carry in the spec and which zabbix-sync also
puts on their services (so the SLA report lists each customer service).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from netsim_common.bsm import REDUNDANT_PAIR, BsmSpec, StatusRules
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.plan import Plan

ALG_SET_OK, ALG_MOST_CRITICAL_ALL, ALG_MOST_CRITICAL_ONE = 0, 1, 2
RULE_N_GE = 0
SEVERITY = {"Not classified": 0, "Information": 1, "Warning": 2, "Average": 3, "High": 4, "Disaster": 5}
TAG_EQUALS = 0
SERVICE_TAG_FILTER_EQUALS = 1
SLA_PERIOD_MONTHLY = 2
SLA_MARKER = f"{N.MANAGED_TAG}: {N.MANAGED_VALUE}"


@dataclass(frozen=True)
class SvcNode:
    name: str
    parent: str | None
    level: int
    sortorder: int
    algorithm: int
    description: str
    tags: tuple[tuple[str, str], ...]
    problem_tags: tuple[tuple[str, int, str], ...] = ()
    status_rules: tuple[tuple[int, int, int, int], ...] = ()

    def body(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "algorithm": self.algorithm,
            "sortorder": self.sortorder,
            "weight": 0,
            "propagation_rule": 0,
            "propagation_value": 0,
            "description": self.description,
            "tags": [{"tag": t, "value": v} for t, v in self.tags],
            "problem_tags": [{"tag": t, "operator": op, "value": v} for t, op, v in self.problem_tags],
            "status_rules": [
                {"type": ty, "limit_value": lv, "limit_status": ls, "new_status": ns}
                for ty, lv, ls, ns in self.status_rules
            ],
        }


def _tags(tags: dict[str, str], **extra: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({**tags, **extra, N.MANAGED_TAG: N.MANAGED_VALUE}.items()))


def redundant_pair_rules() -> tuple[tuple[int, int, int, int], ...]:
    rule = REDUNDANT_PAIR
    return tuple(
        sorted(
            [
                (
                    RULE_N_GE,
                    rule.disaster_min_children,
                    SEVERITY[rule.disaster_child_severity],
                    SEVERITY["Disaster"],
                ),
                (RULE_N_GE, rule.warning_min_children, SEVERITY[rule.warning_child_severity], SEVERITY["Warning"]),
            ]
        )
    )


def desired_tree(spec: BsmSpec) -> list[SvcNode]:
    """Nodes in creation order (parents before children)."""
    root = spec.root
    nodes = [SvcNode(root.name, None, 0, 0, ALG_MOST_CRITICAL_ONE, root.description, _tags(root.tags))]
    for ci, cust in enumerate(spec.customers):
        nodes.append(SvcNode(cust.name, root.name, 1, ci, ALG_MOST_CRITICAL_ONE, cust.description, _tags(cust.tags)))
        for si, svc in enumerate(cust.services):
            if svc.status_rules is not StatusRules.REDUNDANT_PAIR:
                raise ValueError(f"{svc.name}: unsupported status rules {svc.status_rules}")
            nodes.append(
                SvcNode(
                    svc.name,
                    cust.name,
                    2,
                    si,
                    ALG_SET_OK,
                    svc.description,
                    _tags(svc.tags, sla_tier=cust.sla_tier.value),
                    status_rules=redundant_pair_rules(),
                )
            )
            for pi, pw in enumerate(svc.pseudowires):
                problem = tuple(sorted((t, TAG_EQUALS, v) for t, v in pw.problem_tags().items()))
                nodes.append(
                    SvcNode(pw.name, svc.name, 3, pi, ALG_MOST_CRITICAL_ONE, pw.description, _tags(pw.tags), problem)
                )
    names = [n.name for n in nodes]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate BSM node names: {sorted({n for n in names if names.count(n) > 1})}")
    return nodes


def _current_node(svc: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": int(svc["algorithm"]),
        "sortorder": int(svc["sortorder"]),
        "weight": int(svc["weight"]),
        "propagation_rule": int(svc["propagation_rule"]),
        "propagation_value": int(svc["propagation_value"]),
        "description": svc["description"].replace("\r\n", "\n"),
        "tags": tuple(sorted((t["tag"], t["value"]) for t in svc["tags"])),
        "problem_tags": tuple(sorted((t["tag"], int(t["operator"]), t["value"]) for t in svc["problem_tags"])),
        "status_rules": tuple(
            sorted(
                (int(r["type"]), int(r["limit_value"]), int(r["limit_status"]), int(r["new_status"]))
                for r in svc["status_rules"]
            )
        ),
    }


def _wanted_node(node: SvcNode) -> dict[str, Any]:
    return {
        "algorithm": node.algorithm,
        "sortorder": node.sortorder,
        "weight": 0,
        "propagation_rule": 0,
        "propagation_value": 0,
        "description": node.description,
        "tags": node.tags,
        "problem_tags": node.problem_tags,
        "status_rules": node.status_rules,
    }


@dataclass
class TreeSync:
    api: ZabbixApi
    spec: BsmSpec
    dry_run: bool = False
    log: Callable[[str], None] = print
    ids: dict[str, str] = field(default_factory=dict)

    def managed(self) -> list[dict[str, Any]]:
        return self.api.call(
            "service.get",
            {
                "output": "extend",
                "selectParents": ["serviceid", "name"],
                "selectTags": "extend",
                "selectProblemTags": "extend",
                "selectStatusRules": "extend",
                "tags": [{"tag": N.MANAGED_TAG, "value": N.MANAGED_VALUE, "operator": SERVICE_TAG_FILTER_EQUALS}],
            },
        )

    def run(self) -> Plan:
        plan = Plan()
        wanted = desired_tree(self.spec)
        wanted_names = {n.name for n in wanted}
        current: dict[str, dict[str, Any]] = {}
        extras: list[dict[str, Any]] = []
        for svc in self.managed():
            if svc["name"] in wanted_names and svc["name"] not in current:
                current[svc["name"]] = svc
            else:
                extras.append(svc)
        self.ids = {name: svc["serviceid"] for name, svc in current.items()}

        # prune first (children before parents), so renamed nodes never clash
        for svc in sorted(extras, key=lambda s: -len(s.get("parents") or [])):
            plan.add("delete", "service", svc["name"], "no longer in the BSM spec")
        if extras and not self.dry_run:
            self.api.call("service.delete", [s["serviceid"] for s in extras])

        for level in sorted({n.level for n in wanted}):
            creates: list[SvcNode] = []
            for node in (n for n in wanted if n.level == level):
                cur = current.get(node.name)
                parent_id = self.ids.get(node.parent) if node.parent else None
                if cur is None:
                    creates.append(node)
                    continue
                diff = {k for k, v in _wanted_node(node).items() if _current_node(cur)[k] != v}
                cur_parents = sorted(p["name"] for p in cur.get("parents") or [])
                if cur_parents != ([node.parent] if node.parent else []):
                    diff.add("parents")
                if diff:
                    plan.add("update", "service", node.name, ", ".join(sorted(diff)))
                    if not self.dry_run:
                        body = {"serviceid": cur["serviceid"], **node.body()}
                        body["parents"] = [{"serviceid": parent_id}] if parent_id else []
                        self.api.call("service.update", body)
            for node in creates:
                plan.add("create", "service", node.name, f"under {node.parent}" if node.parent else "root")
            if creates and not self.dry_run:
                bodies = []
                for node in creates:
                    body = node.body()
                    if node.parent:
                        body["parents"] = [{"serviceid": self.ids[node.parent]}]
                    bodies.append(body)
                for node, sid in zip(creates, self.api.call("service.create", bodies)["serviceids"], strict=True):
                    self.ids[node.name] = sid
        return plan


# ------------------------------------------------------------------------------------------------ SLA
def desired_slas(sla_slo: dict[str, float]) -> list[dict[str, Any]]:
    out = []
    for tier, slo in sorted(sla_slo.items()):
        out.append(
            {
                "name": N.SLA_NAME.format(tier=tier),
                "slo": f"{slo:.4f}",
                "period": SLA_PERIOD_MONTHLY,
                "timezone": "UTC",
                "status": 1,
                "service_tags": [{"tag": "sla_tier", "operator": TAG_EQUALS, "value": tier}],
                "description": (
                    f"NetSim L2VPN SLA tier {tier}: SLO {slo} %, monthly, every service tagged sla_tier={tier} "
                    f"(customers and their services).\n{SLA_MARKER}"
                ),
            }
        )
    return out


def sync_slas(api: ZabbixApi, sla_slo: dict[str, float], *, dry_run: bool = False) -> Plan:
    plan = Plan()
    current = {s["name"]: s for s in api.call("sla.get", {"output": "extend", "selectServiceTags": "extend"})}
    wanted = desired_slas(sla_slo)
    for sla in wanted:
        cur = current.get(sla["name"])
        if cur is None:
            plan.add("create", "sla", sla["name"], f"SLO {sla['slo']} % for sla_tier={sla['service_tags'][0]['value']}")
            if not dry_run:
                # Required by sla.create in 8.0: the day the SLA starts (kept on later updates, so is its history).
                today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                api.call("sla.create", {**sla, "effective_date": int(today.timestamp())})
            continue
        same = (
            float(cur["slo"]) == float(sla["slo"])
            and int(cur["period"]) == sla["period"]
            and cur["timezone"] == sla["timezone"]
            and int(cur["status"]) == sla["status"]
            and cur["description"].replace("\r\n", "\n") == sla["description"]
            and sorted((t["tag"], int(t["operator"]), t["value"]) for t in cur["service_tags"])
            == sorted((t["tag"], t["operator"], t["value"]) for t in sla["service_tags"])
        )
        if not same:
            plan.add("update", "sla", sla["name"])
            if not dry_run:
                api.call("sla.update", {"slaid": cur["slaid"], **sla})
    wanted_names = {s["name"] for s in wanted}
    for name, cur in sorted(current.items()):
        if name not in wanted_names and SLA_MARKER in cur["description"]:
            plan.add("delete", "sla", name, "tier no longer defined")
            if not dry_run:
                api.call("sla.delete", [cur["slaid"]])
    return plan
