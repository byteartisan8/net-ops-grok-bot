"""``zabbix-status``: the L2VPN BSM tree with statuses, current L2VPN problems and per-host PW counts (API only).

zabbix-status            # human-readable
zabbix-status --json     # the same data for scripts (lab-api, the bot, tests)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi, ZabbixApiError
from netsim_zabbix.settings import SettingsError, load_settings

STATUS = {-1: "OK", 0: "Not classified", 1: "Information", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}
TAG_EQUALS = 1


def status_name(value: int | str) -> str:
    return STATUS.get(int(value), str(value))


def bsm_tree(api: ZabbixApi) -> list[dict[str, Any]]:
    """Managed services as a nested tree: ``[{name, status, tags, children: [...]}]`` sorted like the BSM."""
    services = api.call(
        "service.get",
        {
            "output": ["serviceid", "name", "status", "sortorder"],
            "selectParents": ["serviceid"],
            "selectTags": ["tag", "value"],
            "tags": [{"tag": N.MANAGED_TAG, "value": N.MANAGED_VALUE, "operator": TAG_EQUALS}],
        },
    )
    nodes = {
        s["serviceid"]: {
            "name": s["name"],
            "status": int(s["status"]),
            "status_name": status_name(s["status"]),
            "tags": {t["tag"]: t["value"] for t in s["tags"] if t["tag"] != N.MANAGED_TAG},
            "sortorder": int(s["sortorder"]),
            "children": [],
        }
        for s in services
    }
    roots = []
    for s in services:
        parents = [p["serviceid"] for p in s["parents"] if p["serviceid"] in nodes]
        (nodes[parents[0]]["children"] if parents else roots).append(nodes[s["serviceid"]])

    def order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items:
            item["children"] = order(item["children"])
        return sorted(items, key=lambda n: (n["sortorder"], n["name"]))

    return order(roots)


def l2vpn_problems(api: ZabbixApi) -> list[dict[str, Any]]:
    probs = api.call(
        "problem.get",
        {
            "output": ["eventid", "name", "severity", "clock", "opdata"],
            "selectTags": "extend",
            "tags": [{"tag": N.TAG_SCOPE, "value": "l2vpn", "operator": 0}],
            "sortfield": ["eventid"],
        },
    )
    return [
        {
            "eventid": p["eventid"],
            "severity": status_name(p["severity"]),
            "name": p["name"],
            "clock": int(p["clock"]),
            "tags": {t["tag"]: t["value"] for t in p["tags"]},
        }
        for p in probs
    ]


def pw_counts(api: ZabbixApi) -> dict[str, dict[str, int]]:
    hosts = {
        h["hostid"]: h["host"]
        for h in api.call(
            "host.get",
            {"output": ["hostid", "host"], "tags": [{"tag": N.MANAGED_TAG, "value": N.MANAGED_VALUE, "operator": 1}]},
        )
    }
    out = {h: {"expected": 0, "present": 0, "up": 0, "observed": 0} for h in sorted(hosts.values())}
    if not hosts:
        return out
    items = api.call(
        "item.get",
        {
            "output": ["hostid", "key_", "lastvalue"],
            "hostids": list(hosts),
            "search": {"key_": ["netsim.pw.present[", "netsim.pw.oper[", "netsim.pw.observed.oper["]},
            "searchByAny": True,
            "startSearch": True,
        },
    )
    for i in items:
        row = out[hosts[i["hostid"]]]
        if i["key_"].startswith("netsim.pw.present["):
            row["expected"] += 1
            row["present"] += i["lastvalue"] == "1"
        elif i["key_"].startswith("netsim.pw.oper["):
            row["up"] += i["lastvalue"] == "1"
        else:
            row["observed"] += 1
    return out


def snapshot(api: ZabbixApi) -> dict[str, Any]:
    return {"bsm": bsm_tree(api), "problems": l2vpn_problems(api), "pws": pw_counts(api)}


def render_tree(nodes: list[dict[str, Any]], prefix: str = "") -> list[str]:
    lines = []
    for i, node in enumerate(nodes):
        last = i == len(nodes) - 1
        branch = "" if not prefix and len(nodes) == 1 else ("└── " if last else "├── ")
        lines.append(f"{prefix}{branch}{node['name']:<{max(1, 44 - len(prefix) - len(branch))}} {node['status_name']}")
        extension = "    " if last else "│   "
        lines += render_tree(node["children"], prefix + (extension if branch else ""))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zabbix-status", description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        with load_settings().api() as api:
            data = snapshot(api)
    except (ZabbixApiError, SettingsError) as exc:
        print(f"zabbix-status: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print("BSM (Zabbix services, managed by zabbix-sync):")
    for line in render_tree(data["bsm"]):
        print(f"  {line}")
    checks = [p for p in data["problems"] if p["tags"].get(N.TAG_SCOPE) != N.SCOPE_TRAP]
    hints = [p for p in data["problems"] if p["tags"].get(N.TAG_SCOPE) == N.SCOPE_TRAP]
    print(f"\nCurrent L2VPN problems (PW checks): {len(checks)}")
    for p in checks:
        print(f"  [{p['severity']:<11}] {p['name']}  (check={p['tags'].get('check', '-')})")
    if hints:
        hosts = sorted({p["tags"].get("host", "?") for p in hints})
        print(f"Trap hints (Information, {len(hints)} event(s) not closed by the check-now yet): {', '.join(hosts)}")
    print("\nPWs per host (expected = NetBox spec; present/up on the device; observed = rows on the device):")
    for host, c in data["pws"].items():
        print(f"  {host:<8} expected {c['expected']}  present {c['present']}  up {c['up']}  observed {c['observed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
