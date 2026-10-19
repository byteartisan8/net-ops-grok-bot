"""``netsim-show``: router-style views of the simd API (also behind ``show l2vpn ...`` in clixon_cli).

netsim-show                       # pseudowires (default)
netsim-show neighbors | interfaces | events [--since <ts>] | health
netsim-show --url http://127.0.0.1:19021 pw     # from the host, through the published port
netsim-show pw --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = f"http://127.0.0.1:{os.environ.get('SIMD_PORT', '9000')}"


def _get(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"netsim-show: {url}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
    except OSError as exc:
        raise SystemExit(f"netsim-show: cannot reach simd at {url}: {exc}") from exc


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [headers] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def show_pw(state: dict[str, Any]) -> str:
    up = state["uplink"]
    head = (
        f"{state['hostname']}  loopback {state['loopback']}  mgmt {state['mgmt_ip']}  "
        f"uplink {up['oper']}{' (FAULT)' if up['fault'] else ''}  boot {state['boot_id']}"
    )
    rows = []
    for p in state["pseudowires"]:
        r = p["remote"]
        remote = f"{r['hostname']} {r['ac_name']} mtu {r['mtu']}" if r else "-"
        rows.append(
            [
                p["index"],
                p["service_id"],
                p["role"],
                p["peer"],
                p["vc_id"],
                p["admin"],
                p["oper"],
                p["reason"],
                p["ac"],
                remote,
                p["since"],
            ]
        )
    headers = ["idx", "service", "role", "peer", "vc-id", "admin", "oper", "reason", "local ac", "remote", "since"]
    return head + "\n\n" + _table(headers, rows)


def show_interfaces(state: dict[str, Any]) -> str:
    rows = [
        [
            i["index"],
            i["name"],
            i["type"],
            i["admin"],
            i["oper"],
            "yes" if i["fault"] else "",
            i["mtu"],
            i["description"],
        ]
        for i in state["interfaces"]
    ]
    return _table(["ifIndex", "name", "type", "admin", "oper", "fault", "mtu", "description"], rows)


def show_neighbors(neighbors: list[dict[str, Any]]) -> str:
    rows = [
        [
            n["hostname"],
            n["loopback"],
            n["mgmt_ip"],
            "up" if n["uplink_oper_up"] else "down",
            n["pw_count"],
            n["age_s"],
            "yes" if n["reachable"] else "no",
        ]
        for n in neighbors
    ]
    return _table(["neighbor", "loopback", "mgmt", "uplink", "pws", "age_s", "reachable"], rows)


def show_events(resp: dict[str, Any]) -> str:
    out = []
    for e in resp["events"]:
        extra = {k: v for k, v in e.items() if k not in {"id", "ts", "time", "host", "boot_id", "type"}}
        out.append(f"{e['time']}  {e['type']:<16} {json.dumps(extra, separators=(',', ':'))}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("view", nargs="?", default="pw", choices=["pw", "interfaces", "neighbors", "events", "health"])
    parser.add_argument("--url", default=DEFAULT_URL, help=f"simd base URL (default {DEFAULT_URL})")
    parser.add_argument("--since", default=None, help="events: epoch seconds or ISO 8601")
    parser.add_argument("--json", action="store_true", help="raw JSON")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    if args.view in ("pw", "interfaces"):
        data = _get(f"{base}/state")
        text = show_pw(data) if args.view == "pw" else show_interfaces(data)
    elif args.view == "neighbors":
        data = _get(f"{base}/neighbors")
        text = show_neighbors(data)
    elif args.view == "events":
        data = _get(f"{base}/events" + (f"?since={args.since}" if args.since else ""))
        text = show_events(data)
    else:
        data = _get(f"{base}/health")
        text = json.dumps(data, indent=2)
    print(json.dumps(data, indent=2) if args.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
