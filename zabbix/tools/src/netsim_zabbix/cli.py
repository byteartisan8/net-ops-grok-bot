"""``zabbix-provision`` and ``zabbix-sync`` command lines (see :mod:`netsim_zabbix.provision` / ``.sync``)."""

from __future__ import annotations

import argparse
import json
import sys
import time

from netsim_zabbix.api import ZabbixApiError
from netsim_zabbix.plan import Plan
from netsim_zabbix.provision import ProvisionError, provision
from netsim_zabbix.settings import SOURCES, SettingsError, load_settings
from netsim_zabbix.source import SpecError, load_lab_spec
from netsim_zabbix.spec_macros import SpecTooLarge
from netsim_zabbix.sync import sync

ERRORS = (ZabbixApiError, SpecError, SpecTooLarge, SettingsError, ProvisionError)


def _parser(prog: str, doc: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=doc)
    parser.add_argument(
        "--source", choices=SOURCES, default=None, help="BSM spec source (default: NETSIM_BSM_SOURCE or netbox)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    return parser


def _report(prog: str, plan: Plan, source: str, elapsed: float, as_json: bool, dry_run: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "source": source,
                    "dry_run": dry_run,
                    "changes": [vars(c) for c in plan.changes],
                    "warnings": plan.warnings,
                    "seconds": round(elapsed, 2),
                },
                indent=2,
            )
        )
        return
    for line in plan.lines():
        print(f"  {line}")
    verb = "would make" if dry_run else "made"
    print(f"{prog}: {verb} {plan.summary()} (source {source}, {elapsed:.1f} s)")


def sync_main(argv: list[str] | None = None) -> int:
    parser = _parser("zabbix-sync", "Spec macros, BSM tree, SLAs and map from the BSM spec (NetBox by default).")
    parser.add_argument("--no-refresh", action="store_true", help="do not run discovery now after macro changes")
    args = parser.parse_args(argv)
    try:
        source = args.source or load_settings().bsm_source
        lab = load_lab_spec(source)
        result = sync(
            lab, dry_run=args.dry_run, refresh_discovery=not args.no_refresh, log=None if args.json else print
        )
    except ERRORS as exc:
        print(f"zabbix-sync: {exc}", file=sys.stderr)
        return 1
    _report("zabbix-sync", result.plan, source, result.duration, args.json, args.dry_run)
    return 0


def provision_main(argv: list[str] | None = None) -> int:
    parser = _parser("zabbix-provision", "Template, hosts, trap action, BSM sync, map and dashboard.")
    parser.add_argument("--no-wait", action="store_true", help="do not wait for discovery before building the map")
    args = parser.parse_args(argv)
    start = time.monotonic()
    try:
        source = args.source or load_settings().bsm_source
        lab = load_lab_spec(source)
        plan = provision(
            lab, dry_run=args.dry_run, wait=not args.no_wait, log=(lambda _m: None) if args.json else print
        )
    except ERRORS as exc:
        print(f"zabbix-provision: {exc}", file=sys.stderr)
        return 1
    _report("zabbix-provision", plan, source, time.monotonic() - start, args.json, args.dry_run)
    return 0
