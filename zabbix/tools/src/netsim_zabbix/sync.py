"""``zabbix-sync``: NetBox -> Zabbix for the parts that follow intent (fast, safe to run often, idempotent).

1. ``{$NETSIM.PW.SPEC}`` (+ overflow chunks) on every provisioned host, from the BSM spec: entries that disappear
   from NetBox are pruned by rewriting the value; overflow macros that are no longer needed are deleted.
2. The BSM tree and the SLAs (:mod:`netsim_zabbix.bsm_sync`).
3. Refresh: when a host's spec changed, make it effective now (see :func:`refresh`): new PWs are checked within
   ~15-25 s instead of at the next discovery/poll interval (``refresh_discovery=False`` / ``--no-refresh`` skips it).
4. The ``L2VPN Topology`` map (:mod:`netsim_zabbix.map_sync`), last: its links use the discovered PW triggers.

Diff-based: prints a plan, writes nothing with ``dry_run``, and a second run makes 0 changes. Only objects that
zabbix-provision / zabbix-sync created (tag ``managed_by=netsim-zabbix``) are touched.

    from netsim_zabbix.sync import sync
    result = sync()                      # NETSIM_BSM_SOURCE (default netbox), ZABBIX_URL, ZABBIX_API_TOKEN
    print(result.plan.summary())

lab-api (session 05) calls :func:`sync` after a (debounced) NetBox webhook; with a refresh it takes ~15-25 s,
so run it off the request path.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from netsim_common.bsm import pw_spec_for_host
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.bsm_sync import TreeSync, sync_slas
from netsim_zabbix.map_sync import sync_map
from netsim_zabbix.plan import Plan
from netsim_zabbix.settings import load_settings
from netsim_zabbix.source import LabSpec, load_lab_spec
from netsim_zabbix.spec_macros import host_spec_macros

#: Zabbix server CacheUpdateFrequency in zabbix/compose.yaml (5 s) + margin: a macro written through the API is
#: only seen by the spec discovery after the configuration cache has synced it.
CONFIG_CACHE_WAIT = 6.0
TASK_EXECUTE_NOW = 6
TAG_FILTER_EQUALS = 1


@dataclass
class SyncResult:
    plan: Plan
    source: str
    changed_hosts: list[str] = field(default_factory=list)
    refreshed: bool = False
    duration: float = 0.0


def managed_hosts(api: ZabbixApi) -> dict[str, str]:
    """``{host: hostid}`` of the hosts zabbix-provision created."""
    hosts = api.call(
        "host.get",
        {
            "output": ["hostid", "host"],
            "tags": [{"tag": N.MANAGED_TAG, "value": N.MANAGED_VALUE, "operator": TAG_FILTER_EQUALS}],
        },
    )
    return {h["host"]: h["hostid"] for h in hosts}


def sync_macros(
    api: ZabbixApi, lab: LabSpec, hostids: dict[str, str], *, dry_run: bool = False
) -> tuple[Plan, list[str]]:
    plan = Plan()
    for host in sorted(set(lab.bsm.hosts()) - set(hostids)):
        plan.warn(f"host {host} is in the BSM spec but not in Zabbix (run zabbix-provision)")
    if not hostids:
        return plan, []
    current: dict[str, dict[str, dict[str, Any]]] = {h: {} for h in hostids}
    by_id = {v: k for k, v in hostids.items()}
    for m in api.call(
        "usermacro.get",
        {
            "output": ["hostmacroid", "hostid", "macro", "value"],
            "hostids": list(hostids.values()),
            "filter": {"macro": N.SPEC_MACROS},
        },
    ):
        current[by_id[m["hostid"]]][m["macro"]] = m
    creates, updates, deletes, changed = [], [], [], []
    for host in sorted(hostids):
        wanted = host_spec_macros(lab.bsm, host)
        have = current[host]
        touched = False
        for macro, value in wanted.items():
            entries = value.count('"service"')
            if macro not in have:
                creates.append({"hostid": hostids[host], "macro": macro, "value": value, "description": _desc(macro)})
                plan.add("create", "macro", f"{host} {macro}", f"{entries} PW(s)")
                touched = True
            elif have[macro]["value"] != value:
                updates.append({"hostmacroid": have[macro]["hostmacroid"], "value": value})
                plan.add("update", "macro", f"{host} {macro}", f"{entries} PW(s)")
                touched = True
        for macro, cur in have.items():
            if macro not in wanted:
                deletes.append(cur["hostmacroid"])
                plan.add("delete", "macro", f"{host} {macro}", "overflow chunk no longer needed")
                touched = True
        if touched:
            changed.append(host)
    if not dry_run:
        if creates:
            api.call("usermacro.create", creates)
        if updates:
            api.call("usermacro.update", updates)
        if deletes:
            api.call("usermacro.delete", deletes)
    return plan, changed


def _desc(macro: str) -> str:
    return f"NetBox spec of this host's PWs ({macro}), written by zabbix-sync from the BSM spec. Do not edit."


def execute_now(api: ZabbixApi, itemids: list[str]) -> list[str]:
    if not itemids:
        return []
    return api.call("task.create", [{"type": TASK_EXECUTE_NOW, "request": {"itemid": i}} for i in itemids])["taskids"]


def discovered_rows(api: ZabbixApi, hostids: dict[str, str]) -> dict[str, set[str]]:
    """``{host: {row item keys}}``: what the "Expected PWs" discovery has created so far."""
    by_id = {v: k for k, v in hostids.items()}
    rows: dict[str, set[str]] = {h: set() for h in hostids}
    for item in api.call(
        "item.get",
        {
            "output": ["hostid", "key_"],
            "hostids": list(hostids.values()),
            "search": {"key_": "netsim.pw.row["},
            "startSearch": True,
        },
    ):
        rows[by_id[item["hostid"]]].add(item["key_"])
    return rows


def refresh(
    api: ZabbixApi,
    lab: LabSpec,
    hostids: dict[str, str],
    *,
    cache_wait: float = CONFIG_CACHE_WAIT,
    timeout: float = 60.0,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Make the spec of ``hostids`` effective now instead of at the next discovery/poll interval.

    The server only sees API changes after its configuration cache synced them (``CacheUpdateFrequency``), so:
    wait for the new macros, execute now the spec discovery, wait until the discovered PW items match the spec,
    wait until the server has those items, then execute now the walk so the new checks get their first values.
    Returns False if discovery did not finish within ``timeout``.
    """
    if not hostids:
        return True
    ids = list(hostids.values())
    want = {h: {N.pw_key("row", e.service, e.role.value) for e in pw_spec_for_host(lab.bsm, h)} for h in hostids}
    time.sleep(cache_wait)
    rules = api.call("discoveryrule.get", {"output": ["itemid"], "hostids": ids, "filter": {"key_": [N.SPEC_LLD_KEY]}})
    execute_now(api, [r["itemid"] for r in rules])
    deadline = time.monotonic() + timeout
    done = False
    while time.monotonic() < deadline:
        if discovered_rows(api, hostids) == want:
            done = True
            break
        time.sleep(1.0)
    if not done and log:
        log(f"zabbix-sync: discovery on {', '.join(sorted(hostids))} not complete after {timeout:.0f} s")
    time.sleep(cache_wait)
    walks = api.call("item.get", {"output": ["itemid"], "hostids": ids, "filter": {"key_": [N.WALK_KEY]}})
    execute_now(api, [w["itemid"] for w in walks])
    return done


def sync(
    lab: LabSpec | None = None,
    *,
    source: str | None = None,
    dry_run: bool = False,
    refresh_discovery: bool = True,
    api: ZabbixApi | None = None,
    log: Callable[[str], None] | None = None,
) -> SyncResult:
    """Bring Zabbix in line with the BSM spec (see module docstring). Importable: lab-api calls this."""
    start = time.monotonic()
    settings = load_settings()
    lab = lab or load_lab_spec(source or settings.bsm_source)
    own = api is None
    api = api or settings.api()
    try:
        hostids = managed_hosts(api)
        roles = {h.name: h.role for h in lab.hosts}
        plan = Plan()
        macro_plan, changed = sync_macros(api, lab, hostids, dry_run=dry_run)
        plan.extend(macro_plan)
        plan.extend(TreeSync(api, lab.bsm, dry_run=dry_run).run())
        plan.extend(sync_slas(api, lab.sla_slo, dry_run=dry_run))
        refreshed = False
        if refresh_discovery and changed and not dry_run:
            if log:
                log(f"zabbix-sync: running the spec discovery now on {', '.join(changed)} (after the config cache)")
            refreshed = refresh(api, lab, {h: hostids[h] for h in changed}, log=log)
        # last: the map's link indicators are the discovered PW triggers
        plan.extend(sync_map(api, lab.bsm, hostids, roles, dry_run=dry_run))
        return SyncResult(plan, lab.source, changed, refreshed, time.monotonic() - start)
    finally:
        if own:
            api.close()


def expected_pw_triggers(lab: LabSpec, hostids: dict[str, str]) -> int:
    """Number of PW trigger prototypes' children the spec implies (missing/oper/spec on every PW end)."""
    ends = sum(1 for _, pw in lab.bsm.pseudowires() for end in pw.ends if end.host in hostids)
    return 3 * ends


def wait_for_discovery(
    api: ZabbixApi, lab: LabSpec, hostids: dict[str, str], *, timeout: float = 120.0, log: Callable[[str], None] = print
) -> bool:
    """Wait until every PW end of the spec has its three discovered triggers (missing, oper, spec)."""
    want = expected_pw_triggers(lab, hostids)
    deadline = time.monotonic() + timeout
    have = 0
    while time.monotonic() < deadline:
        have = int(
            api.call(
                "trigger.get",
                {
                    "countOutput": True,
                    "hostids": list(hostids.values()),
                    "tags": [{"tag": "scope", "value": N.SCOPE_PW_TAG, "operator": TAG_FILTER_EQUALS}],
                },
            )
        )
        if have >= want:
            return True
        time.sleep(2.0)
    log(f"zabbix-sync: discovery incomplete after {timeout:.0f} s: {have}/{want} PW triggers")
    return False
