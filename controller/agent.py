#!/usr/local/bin/python3
"""controller-agent (owner: 10-clixon-controller): what the controller does on its own, after each backend start.

1. It learns each PE's SSH host key the first time the PE's NETCONF port answers (``ssh-keyscan``, trust on first
   use) and keeps it in ``/var/lib/controller/ssh/known_hosts`` on the volume; the controller connects with strict
   host key checking.
2. It connects every PE once the backend is up (``connection open <pe>``, as root, the NACM recovery user).
   Connecting is the controller's sync: it takes the router's running config as its copy. A PE that is not up
   within ``BOOT_WINDOW_S`` stays closed until lab-api reconnects it (lab-api's resync, after a reset or an approved
   change).

It never pulls, and after the window it never connects anything: lab-api does. Stdlib only (the image's Python).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

CONFIG = "/etc/clixon/controller.xml"
STARTUP = "/opt/netsim/controller/startup_db"
KNOWN_HOSTS = "/var/lib/controller/ssh/known_hosts"
PIDFILE = "/run/controller/controller.pid"
NS = "{http://clicon.org/controller}"
BOOT_WINDOW_S = 600.0
BUSY_S = 3.0
IDLE_S = 30.0


@dataclass(frozen=True)
class Pe:
    name: str
    addr: str
    port: int

    @property
    def known_hosts_key(self) -> str:
        return self.addr if self.port == 22 else f"[{self.addr}]:{self.port}"


def log(msg: str) -> None:
    print(f"controller-agent: {msg}", flush=True)


def pes(path: str = STARTUP) -> list[Pe]:
    """The devices of the startup datastore, with the port of their profile (830 if neither sets one)."""
    devices = ET.parse(path).getroot().find(f"{NS}devices")
    if devices is None:
        return []
    ports = {p.findtext(f"{NS}name"): p.findtext(f"{NS}port") for p in devices.findall(f"{NS}device-profile")}
    out = []
    for d in devices.findall(f"{NS}device"):
        port = d.findtext(f"{NS}port") or ports.get(d.findtext(f"{NS}device-profile")) or "830"
        out.append(Pe(str(d.findtext(f"{NS}name")), str(d.findtext(f"{NS}addr")), int(port)))
    return out


def parse_connections(text: str) -> dict[str, str]:
    """``show connections`` -> {device: state}: the table's rows after the ``====`` line."""
    states: dict[str, str] = {}
    rows = False
    for line in text.splitlines():
        if line.startswith("===="):
            rows = True
            continue
        parts = line.split()
        if rows and len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def known(pe: Pe, path: str = KNOWN_HOSTS) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return any(line.split(" ", 1)[0] == pe.known_hosts_key for line in f)
    except FileNotFoundError:
        return False


def reachable(pe: Pe) -> bool:
    try:
        with socket.create_connection((pe.addr, pe.port), timeout=2):
            return True
    except OSError:
        return False


def learn(pe: Pe) -> bool:
    """Trust on first use: the PE's ed25519 host key into known_hosts."""
    try:
        out = subprocess.run(
            ["ssh-keyscan", "-T", "3", "-t", "ed25519", "-p", str(pe.port), pe.addr],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = [line for line in out.splitlines() if line.startswith(pe.known_hosts_key + " ")]
    if not lines:
        return False
    with open(KNOWN_HOSTS, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"{pe.name}: host key learned ({lines[0].split()[1]})")
    return True


def backend_id() -> str | None:
    """The running backend's pid and start time (a new value = the backend (re)started), None while it is down."""
    try:
        with open(PIDFILE, encoding="ascii") as f:
            pid = f.read().strip()
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f"{pid}:{f.read().rsplit(')', 1)[1].split()[19]}"
    except (OSError, IndexError, ValueError):
        return None


def cli(*command: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["clixon_cli", "-1", "-f", CONFIG, *command], capture_output=True, text=True, timeout=timeout)


def connections() -> dict[str, str]:
    try:
        return parse_connections(cli("show", "connections").stdout)
    except (OSError, subprocess.TimeoutExpired):
        return {}


def main() -> int:
    devices = pes()
    log(f"{len(devices)} PE(s): {', '.join(p.name for p in devices)}")
    backend, opened, started = None, set(), 0.0
    while True:
        current = backend_id()
        if current is None:
            time.sleep(1)
            continue
        if current != backend:
            backend, opened, started = current, set(), time.monotonic()
            log("backend up: connecting the PEs")
        for pe in devices:
            if not known(pe) and reachable(pe):
                learn(pe)
        booting = time.monotonic() - started < BOOT_WINDOW_S
        if booting:
            for pe in devices:
                if pe.name in opened or not known(pe) or not reachable(pe):
                    continue
                result = cli("connection", "open", pe.name)
                if result.returncode != 0 or result.stderr.strip():
                    log(f"{pe.name}: connection open: {(result.stdout + result.stderr).strip()[:300]}")
            states = connections()
            for pe in devices:
                if pe.name not in opened and states.get(pe.name) == "OPEN":
                    opened.add(pe.name)
                    log(f"{pe.name}: connected")
        done = all(known(pe) for pe in devices) and (len(opened) == len(devices) or not booting)
        time.sleep(IDLE_S if done else BUSY_S)


if __name__ == "__main__":
    sys.exit(main())
