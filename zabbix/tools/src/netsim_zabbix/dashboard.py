"""Dashboard ``L2VPN Services``, kept in git as a Zabbix 8.0 dashboard export (new in 8.0: dashboard export/import).

``zabbix/dashboards/l2vpn_services.yaml`` is the definition; ``zabbix-provision`` imports it with
``configuration.import`` (rule ``dashboards``) when the dashboard in Zabbix differs from it. References inside the
file are by name (host groups, the SLAs, the map), so it imports into any lab Zabbix.

    zabbix-dashboard import     # same as the provisioning step
    zabbix-dashboard export     # after editing the dashboard in the UI: write it back into the YAML (then commit)
    zabbix-dashboard check      # exit 1 if Zabbix's dashboard differs from the YAML
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from netsim_common.paths import repo_root
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.plan import Plan
from netsim_zabbix.settings import load_settings

RULES = {"dashboards": {"createMissing": True, "updateExisting": True}}
HEADER = (
    '# Dashboard "L2VPN Services" (owner: 04-zabbix): Zabbix 8.0 dashboard export, imported by zabbix-provision.\n'
    "# Edit in the UI, then `uv run zabbix-dashboard export` writes it back here (or edit this file and re-import).\n"
)


def dashboard_path() -> Path:
    return repo_root() / "zabbix" / "dashboards" / "l2vpn_services.yaml"


def _norm(data: Any) -> Any:
    """Comparable form: CRLF -> LF, dashboards' widgets sorted by position, widget fields by (type, name, value)."""
    if isinstance(data, str):
        return data.replace("\r\n", "\n")
    if isinstance(data, list):
        items = [_norm(v) for v in data]
        # widgets (Zabbix's export leaves out x/y when 0, so a missing coordinate is 0)
        if items and all(isinstance(i, dict) and {"type", "width", "height"} <= set(i) for i in items):
            return sorted(items, key=lambda w: (int(w.get("y", 0)), int(w.get("x", 0))))
        if items and all(isinstance(i, dict) and {"type", "name"} <= set(i) and "value" in i for i in items):
            return sorted(items, key=lambda f: (str(f["type"]), str(f["name"]), str(f["value"])))
        return items
    if isinstance(data, dict):
        return {k: _norm(v) for k, v in data.items()}
    return data


def current_export(api: ZabbixApi) -> str | None:
    found = api.get_one("dashboard.get", {"output": ["dashboardid"], "filter": {"name": [N.DASHBOARD]}})
    if not found:
        return None
    return api.call("configuration.export", {"format": "yaml", "options": {"dashboards": [found["dashboardid"]]}})


def differs(api: ZabbixApi, source: str) -> bool | None:
    """``None`` if the dashboard does not exist, else whether it differs from ``source``."""
    exported = current_export(api)
    if exported is None:
        return None
    a = _norm(yaml.safe_load(source)["zabbix_export"].get("dashboards"))
    b = _norm(yaml.safe_load(exported)["zabbix_export"].get("dashboards"))
    return a != b


def sync_dashboard(api: ZabbixApi, *, dry_run: bool = False) -> Plan:
    plan = Plan()
    path = dashboard_path()
    if not path.is_file():
        plan.warn(f"{path} missing: dashboard not imported")
        return plan
    source = path.read_text(encoding="utf-8")
    state = differs(api, source)
    if state is None or state:
        plan.add("create" if state is None else "update", "dashboard", N.DASHBOARD, f"from {path.name}")
        if not dry_run:
            api.call("configuration.import", {"format": "yaml", "source": source, "rules": RULES})
    # The export format carries no sharing: an imported dashboard is private to the importing user (netsim-api).
    found = api.get_one("dashboard.get", {"output": ["dashboardid", "private"], "filter": {"name": [N.DASHBOARD]}})
    if found and int(found["private"]) != 0:
        plan.add("update", "dashboard", N.DASHBOARD, "sharing: public")
        if not dry_run:
            api.call("dashboard.update", {"dashboardid": found["dashboardid"], "private": 0})
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zabbix-dashboard", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["import", "export", "check"])
    args = parser.parse_args(argv)
    path = dashboard_path()
    with load_settings().api() as api:
        if args.command == "export":
            exported = current_export(api)
            if exported is None:
                print(f"zabbix-dashboard: no dashboard {N.DASHBOARD!r} in Zabbix", file=sys.stderr)
                return 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(HEADER + exported, encoding="utf-8")
            print(f"wrote {path}")
            return 0
        if args.command == "check":
            state = differs(api, path.read_text(encoding="utf-8"))
            print({None: "missing in Zabbix", True: "differs from the YAML", False: "matches the YAML"}[state])
            return 0 if state is False else 1
        plan = sync_dashboard(api)
        for line in plan.lines():
            print(f"  {line}")
        print(f"zabbix-dashboard: {plan.summary()}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
