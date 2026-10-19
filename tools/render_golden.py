#!/usr/bin/env python3
"""Render contract/golden/<host>.json from contract/topology.yaml (contract section 6 rules).

    uv run python tools/render_golden.py            # (re)write the golden files
    uv run python tools/render_golden.py --check    # exit 1 if any file is missing or stale

Each file is exactly ``netsim_common.canonical_json(render_device_config(topology, host))``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netsim_common.golden import render_golden
from netsim_common.topology import load_topology

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "contract" / "golden"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify only, do not write")
    parser.add_argument("--topology", type=Path, default=REPO_ROOT / "contract" / "topology.yaml")
    parser.add_argument("--out", type=Path, default=GOLDEN_DIR)
    args = parser.parse_args(argv)

    rendered = render_golden(load_topology(args.topology))
    wanted = {f"{host}.json": text for host, text in rendered.items()}
    existing = {p.name for p in args.out.glob("*.json")} if args.out.is_dir() else set()

    if args.check:
        problems = [f"{name}: missing" for name in sorted(set(wanted) - existing)]
        problems += [f"{name}: stale (no such device)" for name in sorted(existing - set(wanted))]
        problems += [
            f"{name}: out of date"
            for name in sorted(set(wanted) & existing)
            if (args.out / name).read_text(encoding="utf-8") != wanted[name]
        ]
        for problem in problems:
            print(f"ERROR: golden/{problem} - run `make contract`", file=sys.stderr)
        if not problems:
            print(f"OK: {len(wanted)} golden configs are up to date")
        return 1 if problems else 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name in sorted(existing - set(wanted)):
        (args.out / name).unlink()
        print(f"removed {name}")
    for name, text in sorted(wanted.items()):
        path = args.out / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
