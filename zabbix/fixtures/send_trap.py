"""Send one contract trap (SNMPv2c, pysnmp) from inside a netsim-pe container (owner: 04-zabbix).

Runs with the image's Python (/opt/venv: pysnmp 7.1.30, netsim_common, netsim_simd), so the trap is encoded exactly
like the devices' own (netsim_simd.traps) and leaves from the container's labnet address, which Zabbix matches to
the host. Used by ``zabbix-fixtures trap`` for the snmpsim fixtures; works in a real device container too.

    python /opt/netsim-fixtures/send_trap.py cpwVcDown --first 1 --last 2 --status lowerLayerDown
    python /opt/netsim-fixtures/send_trap.py linkDown --if-index 3 --status lowerLayerDown
    python /opt/netsim-fixtures/send_trap.py linkUp --if-index 3
    python /opt/netsim-fixtures/send_trap.py ciscoConfigManEvent --user restconf
    python /opt/netsim-fixtures/send_trap.py coldStart
"""

from __future__ import annotations

import argparse
import os
import socket

from netsim_simd.traps import TrapBuilder, encode_v2c_trap

from netsim_common import load_oids
from netsim_common.enums import IfOperStatus

NOTIFICATIONS = ["cpwVcDown", "cpwVcUp", "linkDown", "linkUp", "ciscoConfigManEvent", "coldStart"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("notification", choices=NOTIFICATIONS)
    parser.add_argument("--first", type=int, default=1, help="first cpwVcIndex of the range (PW traps)")
    parser.add_argument("--last", type=int, default=None, help="last cpwVcIndex (default: --first)")
    parser.add_argument("--status", default=None, help="cpwVcOperStatus / ifOperStatus label (default: down / up)")
    parser.add_argument("--if-index", type=int, default=1, help="ifIndex (link traps)")
    parser.add_argument("--admin-down", action="store_true", help="ifAdminStatus down(2) instead of up(1) (link traps)")
    parser.add_argument("--name", default="", help="interface name for the log line (link traps)")
    parser.add_argument("--user", default="fixture", help="ccmHistoryEventTerminalUser (config trap)")
    parser.add_argument("--target", default=os.environ.get("TRAP_TARGETS", "172.30.10.42:1162").split(",")[0])
    parser.add_argument("--community", default=os.environ.get("SNMP_COMMUNITY", "netsim"))
    args = parser.parse_args()

    oids = load_oids()
    builder = TrapBuilder(oids)
    uptime = 4242
    if args.notification == "coldStart":
        trap = builder.cold_start(uptime)
    elif args.notification == "ciscoConfigManEvent":
        trap = builder.config_man_event(1, args.user, uptime)
    elif args.notification in ("linkDown", "linkUp"):
        up = args.notification == "linkUp"
        oper = IfOperStatus[args.status or ("up" if up else "down")]
        name = args.name or f"ifIndex {args.if_index}"
        trap = builder.link(up, args.if_index, not args.admin_down, oper, uptime, name)
    else:
        up = args.notification == "cpwVcUp"
        label = args.status or ("up" if up else "down")
        trap = builder.pw_range(
            up, args.first, args.last or args.first, oids.value("cpwVcOperStatus", label), uptime, ()
        )
    host, port = args.target.rsplit(":", 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(encode_v2c_trap(args.community, trap), (host, int(port)))
    print(f"sent {args.notification} to {host}:{port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
