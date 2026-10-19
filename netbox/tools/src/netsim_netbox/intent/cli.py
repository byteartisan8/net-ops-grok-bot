"""``netbox-intent``: the lab's intent, read from NetBox only (``NETBOX_TOKEN_RO``; ``journal``: ``NETBOX_TOKEN_RW``).

    netbox-intent render spoke-2 [--json]                 # intended config (--json: canonical RESTCONF JSON)
    netbox-intent render-all --out build/intent [--yanglint] [--check-golden]
    netbox-intent bsm-spec [--out spec.json] [--normalized]
    netbox-intent services spoke-2 [--json]               # services and PW roles terminating on a device
    netbox-intent expected-pws hub-a [--json]             # PWs a device must have
    netbox-intent blast-radius hub-a [--json]             # down / degraded / unaffected per service
    netbox-intent service SVC-0002 [--json]               # customer, spoke, both PWs with ends and vc-ids
    netbox-intent journal device:spoke-2 "text" [--kind success]
    netbox-intent problems                                # NetBox data that cannot be turned into intent
    netbox-intent snapshot [--out snap.json]              # the raw NetBox objects the intent is built from

``--snapshot FILE`` (before the command) renders from a saved snapshot instead of a live NetBox.
Exit codes: 0 ok, 1 intent problems or a failed check, 2 usage / connection / token errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from netsim_common.canonical import canonical_json, diff_configs, format_diffs
from netsim_common.paths import contract_dir
from netsim_netbox.client import NetBoxError
from netsim_netbox.intent import (
    JOURNAL_KINDS,
    IntentError,
    JournalError,
    LabIntent,
    Snapshot,
    build_intent,
    load_snapshot,
    write_journal,
)
from netsim_netbox.intent import bsm as _bsm
from netsim_netbox.intent import queries as _queries
from netsim_netbox.intent import render as _render
from netsim_netbox.settings import SettingsError, load_settings


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [headers] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _snapshot(args: argparse.Namespace) -> Snapshot:
    if args.snapshot:
        return Snapshot.load(args.snapshot)
    with load_settings().client() as client:
        return load_snapshot(client)


def _intent(args: argparse.Namespace) -> LabIntent:
    return build_intent(_snapshot(args), load_settings().public_url)


def _emit(args: argparse.Namespace, data: Any, text: str) -> None:
    print(json.dumps(data, indent=2) if args.json else text)


# --------------------------------------------------------------------------------------------- commands
def cmd_render(args: argparse.Namespace) -> int:
    intent = _intent(args)
    config = _render.render_device(intent, args.host)
    if args.json:
        sys.stdout.write(canonical_json(config))
        return 0
    device = intent.devices[args.host]
    print(f"{args.host}  loopback {device.loopback}  role {device.role}  site {device.site}  {device.url}")
    rows = [
        [i.name, i.type.value, _bool(i.enabled), i.mtu, i.vlan_id, i.parent, i.description]
        for i in sorted(config.interfaces.interface, key=lambda i: i.name)
    ]
    print("\n" + _table(["interface", "type", "enabled", "mtu", "vlan", "parent", "description"], rows))
    rows = []
    for xc in sorted(config.l2vpn.xconnect, key=lambda x: x.service_id):
        for n, pw in enumerate(sorted(xc.pseudowire, key=lambda p: p.role.sort_index)):
            first = n == 0
            rows.append(
                [
                    xc.service_id if first else "",
                    xc.customer if first else "",
                    xc.attachment_circuit if first else "",
                    pw.role.value,
                    pw.peer_ipv4,
                    pw.vc_id,
                    _bool(pw.enabled),
                ]
            )
    print("\n" + _table(["xconnect", "customer", "attachment-circuit", "pw", "peer", "vc-id", "enabled"], rows))
    return 0


def cmd_render_all(args: argparse.Namespace) -> int:
    configs = _render.render_all(_intent(args))
    paths = _render.write_configs(configs, args.out)
    print(f"netbox-intent: wrote {len(paths)} config(s) to {args.out}: {', '.join(sorted(configs))}")
    status = 0
    if args.check_golden:
        golden_dir = contract_dir() / "golden"
        golden = {p.stem: p.read_text(encoding="utf-8") for p in sorted(golden_dir.glob("*.json"))}
        for host in sorted(set(golden) | set(configs)):
            if host not in configs:
                print(f"  {host}: in contract/golden but not a netsim device in NetBox")
                status = 1
            elif host not in golden:
                print(f"  {host}: rendered, but no contract/golden/{host}.json")
                status = 1
            elif canonical_json(configs[host]) == golden[host]:
                print(f"  {host}: equals contract/golden/{host}.json (byte for byte)")
            else:
                print(f"  {host}: differs from contract/golden/{host}.json:")
                print("    " + format_diffs(diff_configs(golden[host], configs[host])).replace("\n", "\n    "))
                status = 1
    if args.yanglint:
        result = _render.yanglint(paths)
        if result.returncode == 0:
            print(f"  yanglint: {len(paths)} file(s) valid against netsim-device (config)")
        else:
            print(f"  yanglint failed:\n{result.stdout}{result.stderr}")
            status = 1
    return status


def cmd_bsm_spec(args: argparse.Namespace) -> int:
    spec = _bsm.bsm_spec(_intent(args))
    if args.normalized:
        spec = spec.normalized()
    text = spec.to_json()
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        services = spec.services()
        print(
            f"netbox-intent: BSM spec ({len(spec.customers)} customers, {len(services)} services, "
            f"{sum(len(s.pseudowires) for s in services)} PW leaves) -> {args.out}"
        )
    else:
        sys.stdout.write(text)
    return 0


def cmd_services(args: argparse.Namespace) -> int:
    items = _queries.services_on_device(_intent(args), args.host)
    rows = [[s.service_id, s.customer, s.spoke, ",".join(s.roles), s.ac] for s in items]
    _emit(args, [s.to_dict() for s in items], _table(["service", "customer", "spoke", "roles here", "ac"], rows))
    return 0


def cmd_expected_pws(args: argparse.Namespace) -> int:
    items = _queries.expected_pws(_intent(args), args.host)
    rows = [[p.service_id, p.role, p.peer_host, p.peer_loopback, p.vc_id, p.ac] for p in items]
    _emit(args, [p.to_dict() for p in items], _table(["service", "role", "peer", "peer loopback", "vc-id", "ac"], rows))
    return 0


def cmd_blast_radius(args: argparse.Namespace) -> int:
    items = _queries.blast_radius(_intent(args), args.host)
    rows = [[i.service_id, i.customer, i.spoke, i.impact, ",".join(i.affected_roles) or "-"] for i in items]
    text = f"losing {args.host} (by intent):\n" + _table(["service", "customer", "spoke", "impact", "PWs lost"], rows)
    _emit(args, [i.to_dict() for i in items], text)
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    detail = _queries.service_detail(_intent(args), args.service_id)
    if args.json:
        print(json.dumps(detail, indent=2))
        return 0
    print(f"{detail['service_id']}  customer {detail['customer']} ({detail['sla_tier']})  spoke {detail['spoke']}")
    rows = [
        [
            pw["role"],
            pw["vc_id"],
            f"{pw['a_end']['host']} {pw['a_end']['ac']}",
            f"{pw['z_end']['host']} {pw['z_end']['ac']}",
            pw["netbox_url"],
        ]
        for pw in detail["pseudowires"]
    ]
    print(_table(["pw", "vc-id", "a-end (spoke)", "z-end (hub)", "netbox"], rows))
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    entry = write_journal(args.object, args.message, args.kind)
    link = entry.get("display_url") or entry.get("url")
    print(f"netbox-intent: journal entry {entry['id']} ({args.kind}) on {args.object}: {link}")
    return 0


def cmd_problems(args: argparse.Namespace) -> int:
    intent = _intent(args)
    data = [{"scope": p.scope, "hosts": sorted(p.hosts), "message": p.message, "url": p.url} for p in intent.problems]
    text = "\n".join(f"- [{p.scope}] {p}" for p in intent.problems) or "no problems: NetBox's data is complete"
    _emit(args, data, text)
    return 1 if intent.problems else 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    text = _snapshot(args).to_json()
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"netbox-intent: snapshot -> {args.out}")
    else:
        sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netbox-intent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--snapshot", type=Path, help="use a saved snapshot (netbox-intent snapshot) instead of NetBox")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str, func: Any, help_text: str, *, host: bool = False, json_flag: bool = True
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        if host:
            p.add_argument("host")
        if json_flag:
            p.add_argument("--json", action="store_true", help="JSON output")
        p.set_defaults(func=func)
        return p

    add("render", cmd_render, "intended config of a device", host=True)
    p = add("render-all", cmd_render_all, "write every device's intended config to a directory", json_flag=False)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--yanglint", action="store_true", help="validate the files with yanglint (out dir inside the repo)")
    p.add_argument("--check-golden", action="store_true", help="compare with contract/golden (exit 1 if different)")
    p = add("bsm-spec", cmd_bsm_spec, "BSM spec (contract section 9.3) built from NetBox", json_flag=False)
    p.add_argument("--out", type=Path)
    p.add_argument("--normalized", action="store_true", help="without NetBox IDs/URLs (as netsim-contract bsm-spec)")
    add("services", cmd_services, "services and PW roles terminating on a device", host=True)
    add("expected-pws", cmd_expected_pws, "PWs a device must have", host=True)
    add("blast-radius", cmd_blast_radius, "per service: down / degraded / unaffected if a device is lost", host=True)
    p = add("service", cmd_service, "one service: customer, spoke, both PWs")
    p.add_argument("service_id")
    p = add("journal", cmd_journal, "add a journal entry (NETBOX_TOKEN_RW)", json_flag=False)
    p.add_argument("object", help="device:<name>, l2vpn:<name>, interface:<host>:<if>, tenant:<name>, <type>:<id>")
    p.add_argument("message")
    p.add_argument("--kind", choices=JOURNAL_KINDS, default="info")
    add("problems", cmd_problems, "NetBox data that cannot be turned into intent (exit 1 if any)")
    p = add("snapshot", cmd_snapshot, "dump the raw NetBox objects the intent is built from", json_flag=False)
    p.add_argument("--out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except IntentError as exc:
        print(f"netbox-intent: {exc}", file=sys.stderr)
        return 1
    except (NetBoxError, SettingsError, JournalError, FileNotFoundError, KeyError) as exc:
        print(f"netbox-intent: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
