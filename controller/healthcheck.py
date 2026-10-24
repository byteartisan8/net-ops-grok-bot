#!/usr/local/bin/python3
"""Container HEALTHCHECK of the controller (owner: 10-clixon-controller): the backend accepts sessions on its socket
and sshd listens on :22 (read from /proc/net/tcp: a probe connection would put a line in sshd's log every few
seconds). Connected PEs are not part of it: a powered-off PE is a lab state, not a sick controller
(``show connections`` and lab-api's ``GET /controller/status`` tell those apart).

Exit 0 when both pass, 1 otherwise (one line per check, as ``docker inspect`` shows it).
"""

import socket
import sys

SOCK = "/run/controller/controller.sock"


def backend() -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(SOCK)
        return True, f"backend socket {SOCK}"
    except OSError as exc:
        return False, f"backend socket {SOCK}: {exc}"


def listening(port: int, tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")) -> bool:
    """Some socket listens on TCP ``port`` (state 0A in /proc/net/tcp or tcp6)."""
    for table in tables:
        try:
            with open(table, encoding="ascii") as f:
                rows = [line.split() for line in f.readlines()[1:]]
        except OSError:
            continue
        if any(r[3] == "0A" and int(r[1].rsplit(":", 1)[1], 16) == port for r in rows if len(r) > 3):
            return True
    return False


def sshd() -> tuple[bool, str]:
    ok = listening(22)
    return ok, "sshd :22 listening" if ok else "sshd: nothing listens on :22"


def main() -> int:
    results = [backend(), sshd()]
    for ok, text in results:
        print(("ok   " if ok else "FAIL ") + text)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
