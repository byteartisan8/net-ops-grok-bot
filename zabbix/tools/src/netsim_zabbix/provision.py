"""``zabbix-provision``: template, hosts, trap action, then ``zabbix-sync``, then the dashboard (idempotent).

* Template ``NetSim L2VPN PE SNMP`` from ``zabbix/template/netsim_l2vpn_pe_snmp.yaml``: imported only when
  ``configuration.importcompare`` reports a difference (the YAML is in Zabbix's canonical export form).
* Hosts (one per PE of the spec source): name = hostname, SNMPv2 interface = mgmt IP:161 with community
  ``{$SNMP_COMMUNITY}`` (host macro = ``netsim``), group ``L2VPN/Hubs`` or ``L2VPN/Spokes``, tags ``role``,
  ``site``, ``managed_by``, inventory from NetBox (manual mode), template link. Hosts tagged ``managed_by`` that are
  no longer in the source are deleted.
* Action ``NetSim: SNMP trap -> check now``: trap-trigger problem events of the template -> global script
  ``NetSim: check now`` on the current host.
* ``zabbix-sync`` (spec macros, BSM, SLAs, map), wait for discovery, map again (its links need discovered triggers).
* Dashboard ``L2VPN Services`` from ``zabbix/dashboards/l2vpn_services.yaml`` (Zabbix 8.0 dashboard import).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from netsim_common.constants import lab_constants
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.dashboard import sync_dashboard
from netsim_zabbix.map_sync import sync_map
from netsim_zabbix.plan import Plan
from netsim_zabbix.settings import load_settings
from netsim_zabbix.source import HostSpec, LabSpec, load_lab_spec
from netsim_zabbix.sync import managed_hosts, sync, wait_for_discovery
from netsim_zabbix.template import template_path

INTERFACE_SNMP = 2
SNMP_V2C = 2
INVENTORY_MANUAL = 0
EVENTSOURCE_TRIGGERS = 0
COND_HOST_TEMPLATE, COND_EVENT_TAG = 13, 25
OPERATION_GLOBAL_SCRIPT = 1
EVALTYPE_AND = 1

#: Import rules for the template file. Template groups are created by zabbix-bootstrap (Super admin only).
TEMPLATE_RULES: dict[str, dict[str, bool]] = {
    "templates": {"createMissing": True, "updateExisting": True},
    "items": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
    "discoveryRules": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
    "triggers": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
    "valueMaps": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
}


class ProvisionError(RuntimeError):
    pass


def import_template(api: ZabbixApi, *, dry_run: bool = False) -> Plan:
    plan = Plan()
    source = template_path().read_text(encoding="utf-8")
    params = {"format": "yaml", "source": source, "rules": TEMPLATE_RULES}
    diff = api.call("configuration.importcompare", params)
    if diff:
        kinds = sorted({k for t in diff.get("templates", {}).values() for k in ("added", "updated", "removed") if t})
        plan.add(
            "update" if "templates" in diff and diff["templates"].get("updated") else "create",
            "template",
            N.TEMPLATE,
            ", ".join(kinds) or "changed",
        )
        if not dry_run:
            api.call("configuration.import", params)
    return plan


@dataclass(frozen=True)
class Ids:
    groups: dict[str, str]
    templateid: str | None


def lookup_ids(api: ZabbixApi) -> Ids:
    groups = {
        g["name"]: g["groupid"]
        for g in api.call(
            "hostgroup.get", {"output": ["groupid", "name"], "filter": {"name": list(N.HOST_GROUPS.values())}}
        )
    }
    missing = sorted(set(N.HOST_GROUPS.values()) - set(groups))
    if missing:
        raise ProvisionError(f"host groups {missing} missing: run `make zabbix-bootstrap` first")
    tpl = api.get_one("template.get", {"output": ["templateid"], "filter": {"host": [N.TEMPLATE]}})
    return Ids(groups, tpl["templateid"] if tpl else None)


def host_body(h: HostSpec, ids: Ids) -> dict[str, Any]:
    group = N.HOST_GROUPS.get(h.role)
    if group is None:
        raise ProvisionError(f"{h.name}: role {h.role!r} has no host group (expected one of {sorted(N.HOST_GROUPS)})")
    tags = {"role": h.role, N.MANAGED_TAG: N.MANAGED_VALUE}
    if h.site:
        tags["site"] = h.site
    origin = h.netbox_url or "the topology"
    desc = f"NetSim PE {h.name} ({h.role}, {h.site}). Provisioned by zabbix-provision from {origin}."
    return {
        "host": h.name,
        "name": h.name,
        "description": desc,
        "groups": [{"groupid": ids.groups[group]}],
        "tags": [{"tag": k, "value": v} for k, v in sorted(tags.items())],
        "templates": [{"templateid": ids.templateid}] if ids.templateid else [],
        "inventory_mode": INVENTORY_MANUAL,
        "inventory": h.inventory,
    }


def interface_body(h: HostSpec) -> dict[str, Any]:
    return {
        "type": INTERFACE_SNMP,
        "main": 1,
        "useip": 1,
        "ip": h.mgmt_ip,
        "dns": "",
        "port": "161",
        "details": {"version": SNMP_V2C, "bulk": 1, "community": N.SNMP_COMMUNITY_MACRO},
    }


def sync_hosts(api: ZabbixApi, hosts: list[HostSpec], ids: Ids, *, dry_run: bool = False) -> Plan:
    plan = Plan()
    community = lab_constants().snmp_community
    current = {
        h["host"]: h
        for h in api.call(
            "host.get",
            {
                "output": ["hostid", "host", "name", "description", "inventory_mode"],
                "selectHostGroups": ["groupid"],
                "selectTags": ["tag", "value"],
                "selectParentTemplates": ["templateid"],
                "selectInterfaces": ["interfaceid", "type", "main", "useip", "ip", "dns", "port", "details"],
                "selectInventory": "extend",
                "selectMacros": ["hostmacroid", "macro", "value"],
                "filter": {"host": [h.name for h in hosts]},
            },
        )
    }
    for h in hosts:
        body = host_body(h, ids)
        iface = interface_body(h)
        cur = current.get(h.name)
        if cur is None:
            plan.add("create", "host", h.name, f"{h.role} {h.mgmt_ip}:161 SNMPv2c")
            if not dry_run:
                api.call(
                    "host.create",
                    {
                        **body,
                        "interfaces": [iface],
                        "macros": [
                            {
                                "macro": N.SNMP_COMMUNITY_MACRO,
                                "value": community,
                                "description": "SNMP v2c community (contract 8)",
                            }
                        ],
                    },
                )
            continue
        diff = _host_diff(cur, body, iface)
        if diff:
            plan.add("update", "host", h.name, ", ".join(diff))
            if not dry_run:
                update = {"hostid": cur["hostid"], **body}
                if "interface" in diff:
                    snmp = [i for i in cur["interfaces"] if int(i["type"]) == INTERFACE_SNMP]
                    update["interfaces"] = [{**iface, **({"interfaceid": snmp[0]["interfaceid"]} if snmp else {})}]
                api.call("host.update", update)
        macro = next((m for m in cur["macros"] if m["macro"] == N.SNMP_COMMUNITY_MACRO), None)
        if macro is None or macro["value"] != community:
            plan.add("update", "macro", f"{h.name} {N.SNMP_COMMUNITY_MACRO}", community)
            if not dry_run:
                if macro is None:
                    api.call(
                        "usermacro.create",
                        {"hostid": cur["hostid"], "macro": N.SNMP_COMMUNITY_MACRO, "value": community},
                    )
                else:
                    api.call("usermacro.update", {"hostmacroid": macro["hostmacroid"], "value": community})
    wanted = {h.name for h in hosts}
    for name, hostid in sorted(managed_hosts(api).items()):
        if name not in wanted:
            plan.add("delete", "host", name, "no longer a PE in the source")
            if not dry_run:
                api.call("host.delete", [hostid])
    return plan


def _host_diff(cur: dict[str, Any], body: dict[str, Any], iface: dict[str, Any]) -> list[str]:
    diff = []
    if cur["name"] != body["name"] or cur["description"].replace("\r\n", "\n") != body["description"]:
        diff.append("name/description")
    if sorted(g["groupid"] for g in cur["hostgroups"]) != sorted(g["groupid"] for g in body["groups"]):
        diff.append("groups")
    if sorted((t["tag"], t["value"]) for t in cur["tags"]) != sorted((t["tag"], t["value"]) for t in body["tags"]):
        diff.append("tags")
    if sorted(t["templateid"] for t in cur["parentTemplates"]) != sorted(t["templateid"] for t in body["templates"]):
        diff.append("templates")
    inv = cur["inventory"] if isinstance(cur["inventory"], dict) else {}
    if int(cur["inventory_mode"]) != INVENTORY_MANUAL or any(inv.get(k, "") != v for k, v in body["inventory"].items()):
        diff.append("inventory")
    snmp = [i for i in cur["interfaces"] if int(i["type"]) == INTERFACE_SNMP]
    if len(cur["interfaces"]) != 1 or len(snmp) != 1 or not _iface_equal(snmp[0], iface):
        diff.append("interface")
    return diff


def _iface_equal(cur: dict[str, Any], want: dict[str, Any]) -> bool:
    d = cur.get("details") or {}
    wd = want["details"]
    return (
        cur["ip"] == want["ip"]
        and str(cur["port"]) == want["port"]
        and int(cur["useip"]) == 1
        and int(cur["main"]) == 1
        and int(d.get("version", 0)) == wd["version"]
        and int(d.get("bulk", 0)) == wd["bulk"]
        and d.get("community") == wd["community"]
    )


def sync_action(api: ZabbixApi, ids: Ids, *, dry_run: bool = False) -> Plan:
    plan = Plan()
    script = api.get_one("script.get", {"output": ["scriptid"], "filter": {"name": [N.CHECK_NOW_SCRIPT]}})
    if script is None:
        raise ProvisionError(f"global script {N.CHECK_NOW_SCRIPT!r} missing: run `make zabbix-bootstrap` first")
    if ids.templateid is None:
        plan.add("create", "action", N.TRAP_ACTION, "after the template import")
        return plan
    body = {
        "name": N.TRAP_ACTION,
        "eventsource": EVENTSOURCE_TRIGGERS,
        "status": 0,
        "esc_period": "1h",
        "pause_symptoms": 0,
        "pause_suppressed": 0,
        "notify_if_canceled": 0,
        "filter": {
            "evaltype": EVALTYPE_AND,
            "conditions": [
                {"conditiontype": COND_HOST_TEMPLATE, "operator": 0, "value": ids.templateid},
                {"conditiontype": COND_EVENT_TAG, "operator": 0, "value": N.TRAP_TAG},
            ],
        },
        "operations": [
            {
                "operationtype": OPERATION_GLOBAL_SCRIPT,
                "esc_step_from": 1,
                "esc_step_to": 1,
                "opcommand": {"scriptid": script["scriptid"]},
                "opcommand_hst": [{"hostid": "0"}],
            }
        ],
    }
    cur = api.get_one(
        "action.get",
        {
            "output": "extend",
            "selectFilter": "extend",
            "selectOperations": "extend",
            "filter": {"name": [N.TRAP_ACTION]},
        },
    )
    if cur is None:
        plan.add("create", "action", N.TRAP_ACTION, f"trap trigger events -> {N.CHECK_NOW_SCRIPT}")
        if not dry_run:
            api.call("action.create", body)
        return plan
    conds = sorted((int(c["conditiontype"]), int(c["operator"]), c["value"]) for c in cur["filter"]["conditions"])
    want_conds = sorted((c["conditiontype"], c["operator"], c["value"]) for c in body["filter"]["conditions"])
    ops = cur["operations"]
    same = (
        int(cur["status"]) == 0
        and int(cur["filter"]["evaltype"]) == EVALTYPE_AND
        and conds == want_conds
        and len(ops) == 1
        and int(ops[0]["operationtype"]) == OPERATION_GLOBAL_SCRIPT
        and ops[0].get("opcommand", {}).get("scriptid") == script["scriptid"]
        and [h["hostid"] for h in ops[0].get("opcommand_hst", [])] == ["0"]
        and int(cur["pause_symptoms"]) == 0
        and int(cur["pause_suppressed"]) == 0
    )
    if not same:
        plan.add("update", "action", N.TRAP_ACTION)
        if not dry_run:
            api.call(
                "action.update", {"actionid": cur["actionid"], **{k: v for k, v in body.items() if k != "eventsource"}}
            )
    return plan


def _about_created_host(warning: str, created: set[str]) -> bool:
    return any(warning.startswith(f"host {h} is in the BSM spec but not in Zabbix") for h in created)


def provision(
    lab: LabSpec | None = None,
    *,
    source: str | None = None,
    dry_run: bool = False,
    wait: bool = True,
    log: Callable[[str], None] = print,
) -> Plan:
    settings = load_settings()
    lab = lab or load_lab_spec(source or settings.bsm_source)
    plan = Plan()
    with settings.api() as api:
        plan.extend(import_template(api, dry_run=dry_run))
        ids = lookup_ids(api)
        plan.extend(sync_hosts(api, lab.hosts, ids, dry_run=dry_run))
        plan.extend(sync_action(api, ids, dry_run=dry_run))
        result = sync(lab, api=api, dry_run=dry_run, log=log)
        if dry_run:
            # hosts that this run would create cannot carry macros yet: say what they would get instead
            created = {c.name for c in plan.changes if (c.action, c.kind) == ("create", "host")}
            result.plan.warnings = [w for w in result.plan.warnings if not _about_created_host(w, created)]
            for host in sorted(created & set(lab.bsm.hosts())):
                plan.add("create", "macro", f"{host} {N.SPEC_MACRO}", "with the new host")
        plan.extend(result.plan)
        if wait and not dry_run:
            hostids = managed_hosts(api)
            start = time.monotonic()
            if wait_for_discovery(api, lab, hostids, log=log):
                log(f"zabbix-provision: discovery complete ({time.monotonic() - start:.1f} s)")
            map_plan = sync_map(api, lab.bsm, hostids, {h.name: h.role for h in lab.hosts}, dry_run=dry_run)
            map_plan.warnings = [w for w in map_plan.warnings if w not in plan.warnings]
            plan.changes += map_plan.changes
            plan.warnings = [w for w in plan.warnings if not w.startswith("map link")] + map_plan.warnings
        plan.extend(sync_dashboard(api, dry_run=dry_run))
    return plan
