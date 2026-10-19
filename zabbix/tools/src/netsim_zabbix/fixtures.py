"""SNMP fixtures for developing the Zabbix side without the devices (snmpsim on the device IPs).

``render`` writes ``zabbix/fixtures/<variant>/<host>/netsim.snmprec`` (community ``netsim``) from the golden configs,
the topology and ``contract/oids.yaml`` only (no hand-written OIDs): system group, IF-MIB, cpwVcTable.

| variant      | what the agents answer                                           | expected in Zabbix               |
|--------------|------------------------------------------------------------------|----------------------------------|
| healthy      | every PW up on both ends                                         | no problem, BSM OK               |
| pw-down      | SVC-0001 primary down on spoke-1 and hub-a                       | oper x2, SVC-0001 Warning        |
| pw-missing   | spoke-3 lacks SVC-0004 backup; hub-b's end is down               | missing + oper, SVC-0004 Warning |
| wrong-peer   | spoke-2 SVC-0002 primary peers with hub-b (up, extra row there)  | spec + unexpected + oper,        |
|              | and hub-a's end is down                                          | SVC-0002 Warning                 |

``up --variant X`` runs one snmpsim container per device (the netsim-pe image, which ships snmpsim 1.2.2) on the
device's labnet address, so host interfaces and trap sources match; it refuses while the real devices run
(``make devices-down`` first; ``make devices-up`` after ``down``). ``trap`` sends a contract trap from a fixture
(or device) container with ``zabbix/fixtures/send_trap.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from netsim_common import DeviceConfig, load_oids, load_topology
from netsim_common.oids import OidRegistry
from netsim_common.paths import contract_dir, repo_root

IMAGE = "netsim/netsim-pe:0.1.0"
COMMUNITY = "netsim"
LABEL = "org.netsim.fixture=zabbix"
UPTIME = 4242
VARIANTS = {
    "healthy": "every PW up on both ends (golden configs)",
    "pw-down": "SVC-0001 primary oper down on spoke-1 and hub-a",
    "pw-missing": "spoke-3 has no SVC-0004 backup row; hub-b's end of it is down",
    "wrong-peer": "spoke-2 SVC-0002 primary peers with hub-b (up, extra row on hub-b); hub-a's end is down",
}
# snmprec type tags (snmpsim): Integer32, OCTET STRING, hex OCTET STRING, OBJECT IDENTIFIER, Gauge32, TimeTicks
INT, STR, HEX, OID, GAUGE, TICKS = "2", "4", "4x", "6", "66", "67"


def fixtures_dir() -> Path:
    return repo_root() / "zabbix" / "fixtures"


@dataclass
class PwRow:
    service: str
    role: str
    peer: str
    vc_id: int
    ac: str
    customer: str
    oper: str = "up"
    remote_if: str = ""
    index: int = 0


def golden(host: str) -> DeviceConfig:
    return DeviceConfig.from_restconf((contract_dir() / "golden" / f"{host}.json").read_text(encoding="utf-8"))


def pw_rows(host: str, variant: str) -> list[PwRow]:
    """PW rows of ``host`` in ``variant`` (start from golden, then apply the variant)."""
    topo = load_topology()
    cfg = golden(host)
    rows: list[PwRow] = []
    for xc in cfg.l2vpn.xconnect if cfg.l2vpn else []:
        svc = topo.service(xc.service_id)
        for pw in xc.pseudowire:
            remote_ac = svc.hub_ac if host == svc.spoke else svc.spoke_ac
            rows.append(
                PwRow(
                    xc.service_id,
                    pw.role.value,
                    str(pw.peer_ipv4),
                    pw.vc_id,
                    xc.attachment_circuit,
                    xc.customer or "",
                    remote_if=remote_ac,
                )
            )

    # cpwVcIndex is stable per (service, role) (contract 7): golden rows are numbered once, a removed row leaves a
    # gap and an added row gets max+1, as simd does. Variants must not renumber, or index-keyed items would shift.
    rows.sort(key=lambda r: (r.service, r.role != "primary"))
    for n, r in enumerate(rows, start=1):
        r.index = n

    def find(service: str, role: str) -> PwRow:
        return next(r for r in rows if (r.service, r.role) == (service, role))

    if variant == "pw-down" and host in ("spoke-1", "hub-a"):
        r = find("SVC-0001", "primary")
        r.oper, r.remote_if = "down", ""
    elif variant == "pw-missing":
        if host == "spoke-3":
            rows.remove(find("SVC-0004", "backup"))
        elif host == "hub-b":
            r = find("SVC-0004", "backup")
            r.oper, r.remote_if = "down", ""
    elif variant == "wrong-peer":
        if host == "spoke-2":
            find("SVC-0002", "primary").peer = topo.device("hub-b").loopback
        elif host == "hub-a":
            r = find("SVC-0002", "primary")
            r.oper, r.remote_if = "down", ""
        elif host == "hub-b":
            svc = topo.service("SVC-0002")
            rows.append(
                PwRow(
                    "SVC-0002",
                    "primary",
                    topo.device("spoke-2").loopback,
                    svc.pw("primary").vc_id,
                    svc.hub_ac,
                    svc.customer,
                    remote_if=svc.spoke_ac,
                    index=max(r.index for r in rows) + 1,
                )
            )
    elif variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    return sorted(rows, key=lambda r: r.index)


def snmprec(host: str, variant: str, oids: OidRegistry | None = None) -> str:
    oids = oids or load_oids()
    topo = load_topology()
    dev = topo.device(host)
    cfg = golden(host)
    lines: list[tuple[str, str, str]] = [
        (oids.instance("sysDescr"), STR, f"NetSim PE {host} fixture (snmpsim, variant {variant})"),
        (oids.instance("sysObjectID"), OID, "0.0"),
        (oids.instance("sysUpTime"), TICKS, str(UPTIME)),
        (oids.instance("sysName"), STR, host),
        (oids.instance("sysLocation"), STR, dev.site or ""),
    ]
    ifaces = sorted(cfg.interfaces.interface if cfg.interfaces else [], key=lambda i: i.name)
    for idx, iface in enumerate(ifaces, start=1):
        sub = iface.type.value == "subinterface"
        lines += [
            (oids.instance("ifIndex", idx), INT, str(idx)),
            (oids.instance("ifDescr", idx), STR, iface.name),
            (oids.instance("ifType", idx), INT, str(oids.value("ifType", "l2vlan" if sub else "ethernetCsmacd"))),
            (oids.instance("ifMtu", idx), INT, str(cfg.effective_mtu(iface.name))),
            (oids.instance("ifAdminStatus", idx), INT, str(oids.value("ifAdminStatus", "up"))),
            (oids.instance("ifOperStatus", idx), INT, str(oids.value("ifOperStatus", "up"))),
            (oids.instance("ifName", idx), STR, iface.name),
            (oids.instance("ifAlias", idx), STR, iface.description or ""),
        ]
    for row in pw_rows(host, variant):
        idx = row.index
        up = row.oper == "up"
        oper = str(oids.value("cpwVcOperStatus", row.oper))
        mtu = cfg.effective_mtu(row.ac) if cfg.interfaces and cfg.interfaces.get(row.ac) else 1500
        vc_type = "ethernetVLAN" if "." in row.ac else "ethernet"

        def col(name: str, tag: str, value: object, i: int = idx) -> tuple[str, str, str]:
            return (oids.instance(name, i), tag, str(value))

        lines += [
            col("cpwVcType", INT, oids.value("cpwVcType", vc_type)),
            col("cpwVcPsnType", INT, oids.value("cpwVcPsnType", "mpls")),
            col("cpwVcPeerAddrType", INT, oids.value("cpwVcPeerAddrType", "ipv4")),
            col("cpwVcPeerAddr", HEX, "".join(f"{int(o):02x}" for o in row.peer.split("."))),
            col("cpwVcID", GAUGE, row.vc_id),
            col("cpwVcLocalIfMtu", GAUGE, mtu),
            col("cpwVcRemoteIfMtu", GAUGE, mtu if up else 0),
            col("cpwVcRemoteIfString", STR, row.remote_if if up else ""),
            col("cpwVcName", STR, f"{row.service}:{row.role}"),
            col("cpwVcDescr", STR, row.customer),
            col("cpwVcUpTime", TICKS, UPTIME if up else 0),
            col("cpwVcAdminStatus", INT, oids.value("cpwVcAdminStatus", "up")),
            col("cpwVcOperStatus", INT, oper),
            col("cpwVcInboundOperStatus", INT, oper),
            col("cpwVcOutboundOperStatus", INT, oper),
        ]
    lines.sort(key=lambda line: tuple(int(x) for x in line[0].split(".")))
    return "".join(f"{oid}|{tag}|{value}\n" for oid, tag, value in lines)


def render(out: Path | None = None) -> list[Path]:
    out = out or fixtures_dir()
    oids = load_oids()
    written = []
    for variant in VARIANTS:
        for host in load_topology().hostnames:
            path = out / variant / host / f"{COMMUNITY}.snmprec"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snmprec(host, variant, oids), encoding="utf-8")
            written.append(path)
    return written


# ------------------------------------------------------------------------------------------------ containers
def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def running_devices() -> list[str]:
    names = set(_docker("ps", "--format", "{{.Names}}").stdout.split())
    return sorted(h for h in load_topology().hostnames if h in names)


def fixture_containers() -> list[str]:
    return _docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}").stdout.split()


def down() -> None:
    names = fixture_containers()
    if names:
        _docker("rm", "-f", *names)


def up(variant: str) -> list[str]:
    if variant not in VARIANTS:
        raise SystemExit(f"unknown variant {variant!r}; one of {', '.join(VARIANTS)}")
    devices = running_devices()
    if devices:
        raise SystemExit(f"the real devices are running ({', '.join(devices)}): `make devices-down` first")
    render()
    down()
    topo = load_topology()
    started = []
    for host in topo.hostnames:
        data = fixtures_dir() / variant / host
        _docker(
            "run",
            "-d",
            "--name",
            f"fx-{host}",
            "--hostname",
            host,
            "--label",
            LABEL,
            "--network",
            topo.network.name,
            "--ip",
            topo.device(host).mgmt_ip,
            "-v",
            f"{data}:/data:ro",
            "-v",
            f"{fixtures_dir() / 'send_trap.py'}:/opt/netsim-fixtures/send_trap.py:ro",
            "--no-healthcheck",  # the image's check probes RESTCONF and simd, which a fixture does not run
            "--entrypoint",
            "snmpsim-command-responder",
            IMAGE,
            "--data-dir=/data",
            "--cache-dir=/tmp/snmpsim-cache",
            "--agent-udpv4-endpoint=0.0.0.0:161",
            # snmpsim refuses to keep root: bind :161, then drop to the image's snmpsim user (as the devices do)
            "--process-user=snmpsim",
            "--process-group=snmpsim",
        )
        started.append(f"fx-{host}")
    return started


def trap(host: str, args: list[str]) -> str:
    container = f"fx-{host}" if f"fx-{host}" in fixture_containers() else host
    res = _docker("exec", container, "python", "/opt/netsim-fixtures/send_trap.py", *args, check=False)
    if res.returncode:
        raise SystemExit(res.stderr.strip() or res.stdout.strip())
    return res.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zabbix-fixtures", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="write zabbix/fixtures/<variant>/<host>/netsim.snmprec")
    p_up = sub.add_parser("up", help="run the snmpsim fixtures on the device IPs (devices must be down)")
    p_up.add_argument("--variant", default="healthy", choices=list(VARIANTS))
    sub.add_parser("down", help="remove the fixture containers")
    sub.add_parser("variants", help="list the variants")
    p_trap = sub.add_parser("trap", help="send a trap from a fixture (or device) container: HOST NOTIFICATION [opts]")
    p_trap.add_argument("host")
    p_trap.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cmd == "render":
        paths = render()
        print(f"zabbix-fixtures: wrote {len(paths)} snmprec files under {fixtures_dir()}")
    elif args.cmd == "up":
        print(f"zabbix-fixtures: started {', '.join(up(args.variant))} (variant {args.variant})")
    elif args.cmd == "down":
        down()
        print("zabbix-fixtures: fixture containers removed (`make devices-up` starts the devices again)")
    elif args.cmd == "variants":
        print(json.dumps(VARIANTS, indent=2))
    else:
        print(trap(args.host, args.rest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
