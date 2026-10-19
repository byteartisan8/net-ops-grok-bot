"""``netsim-contract``: command-line access to the shared contract helpers.

netsim-contract canonical running.json            # canonical JSON of a config ("-" = stdin)
netsim-contract diff golden.json running.json     # structured diff, exit 1 if different
netsim-contract golden spoke-1                    # golden config rendered from the topology
netsim-contract pw-spec hub-a                     # {$NETSIM.PW.SPEC} value (from the topology)
netsim-contract bsm-spec                          # BSM spec from the topology
netsim-contract hosts                             # addresses and published ports
netsim-contract oid cpwVcOperStatus 3             # numeric (instance) OID
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netsim_common.bsm import bsm_spec_from_topology, pw_spec_for_host
from netsim_common.canonical import canonical_json, diff_configs, format_diffs
from netsim_common.constants import lab_constants
from netsim_common.golden import render_device_config
from netsim_common.oids import load_oids
from netsim_common.topology import load_topology


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _cmd_canonical(args: argparse.Namespace) -> int:
    sys.stdout.write(canonical_json(_read(args.file)))
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    diffs = diff_configs(_read(args.expected), _read(args.actual))
    if args.json:
        print(json.dumps([d.to_dict() for d in diffs], indent=2))
    else:
        print(format_diffs(diffs))
    return 1 if diffs else 0


def _cmd_golden(args: argparse.Namespace) -> int:
    sys.stdout.write(canonical_json(render_device_config(load_topology(), args.host)))
    return 0


def _cmd_pw_spec(args: argparse.Namespace) -> int:
    entries = [e.model_dump(mode="json") for e in pw_spec_for_host(bsm_spec_from_topology(load_topology()), args.host)]
    print(json.dumps(entries, indent=2) if args.pretty else json.dumps(entries, separators=(",", ":")))
    return 0


def _cmd_bsm_spec(args: argparse.Namespace) -> int:
    text = bsm_spec_from_topology(load_topology()).to_json()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_hosts(args: argparse.Namespace) -> int:
    topo = load_topology()
    lab = lab_constants(topo)
    rows = []
    for d in topo.devices:
        ep = lab.devices[d.hostname]
        rows.append(
            {
                "host": d.hostname,
                "role": d.role.value,
                "site": d.site,
                "mgmt_ip": d.mgmt_ip,
                "loopback": d.loopback,
                "restconf": ep.restconf_url,
                "simd": ep.simd_url,
                "snmp": f"{ep.snmp[0]}:{ep.snmp[1]}/udp",
            }
        )
    for h in topo.infrastructure:
        rows.append(
            {
                "host": h.name,
                "role": h.description,
                "mgmt_ip": h.mgmt_ip,
                "published": f"{topo.host_bind}:{h.host_port}" if h.host_port else "-",
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print("  ".join(f"{k}={v}" for k, v in r.items()))
    return 0


def _cmd_oid(args: argparse.Namespace) -> int:
    oids = load_oids()
    print(oids.instance(args.name, *args.index) if args.index else oids.oid(args.name))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netsim-contract", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("canonical", help="print the canonical JSON of a config file")
    p.add_argument("file", help='RESTCONF JSON file ("-" = stdin)')
    p.set_defaults(func=_cmd_canonical)

    p = sub.add_parser("diff", help="structured diff of two configs (exit 1 if they differ)")
    p.add_argument("expected")
    p.add_argument("actual")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_diff)

    p = sub.add_parser("golden", help="golden config of a host, rendered from the topology")
    p.add_argument("host")
    p.set_defaults(func=_cmd_golden)

    p = sub.add_parser("pw-spec", help="{$NETSIM.PW.SPEC} value of a host, from the topology")
    p.add_argument("host")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=_cmd_pw_spec)

    p = sub.add_parser("bsm-spec", help="BSM spec derived from the topology")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_bsm_spec)

    p = sub.add_parser("hosts", help="lab hosts, addresses and published ports")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_hosts)

    p = sub.add_parser("oid", help="numeric OID of an object/notification (instance OID if INDEX given)")
    p.add_argument("name")
    p.add_argument("index", nargs="*", type=int)
    p.set_defaults(func=_cmd_oid)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        print(f"netsim-contract: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
