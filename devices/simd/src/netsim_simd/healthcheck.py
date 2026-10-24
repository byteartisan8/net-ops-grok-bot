"""Container HEALTHCHECK: RESTCONF answers, SNMP ``sysName.0`` answers, simd ``/health`` is up, and sshd listens on
the NETCONF port (830, for the Clixon controller since 10-clixon-controller; read from /proc/net/tcp, since a probe
connection would put a line in sshd's log every few seconds).

Exit 0 when all four pass, 1 otherwise (one line per check on stdout, as ``docker inspect`` shows it).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

from netsim_common.constants import lab_constants
from netsim_common.oids import load_oids
from netsim_simd import snmpclient


def _http(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        return 0, str(exc).encode()


def check_restconf(base: str) -> tuple[bool, str]:
    status, body = _http(f"{base}/restconf")
    return status == 200, f"restconf {status or body.decode(errors='replace')}"


def check_simd(base: str) -> tuple[bool, str]:
    status, body = _http(f"{base}/health")
    try:
        state = json.loads(body).get("status")
    except (ValueError, AttributeError):
        state = None
    return status == 200 and state in ("ok", "degraded"), f"simd {status} {state}"


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


def check_ssh(port: int = 830) -> tuple[bool, str]:
    """sshd listens for NETCONF over SSH (logging in needs the controller's key)."""
    ok = listening(port)
    return ok, f"ssh :{port} listening" if ok else f"ssh: nothing listens on :{port}"


def check_snmp(port: int, community: str, expected: str | None) -> tuple[bool, str]:
    oid = load_oids().instance("sysName")
    try:
        vbs = asyncio.run(snmpclient.get("127.0.0.1", port, community, oid, timeout=1.0, retries=1))
    except Exception as exc:
        return False, f"snmp sysName: {exc}"
    value = vbs[0].rendered if vbs and not vbs[0].end else None
    ok = value is not None and (expected is None or value == expected)
    return ok, f"snmp sysName={value!r}"


def main() -> int:
    restconf = os.environ.get("RESTCONF_URL", "http://127.0.0.1:8080").rstrip("/")
    simd = f"http://127.0.0.1:{os.environ.get('SIMD_PORT', '9000')}"
    community = os.environ.get("SNMP_COMMUNITY", "") or lab_constants().snmp_community
    results = [
        check_restconf(restconf),
        check_snmp(161, community, None),
        check_simd(simd),
        check_ssh(int(os.environ.get("NETCONF_SSH_PORT", "830"))),
    ]
    for ok, text in results:
        print(("ok   " if ok else "FAIL ") + text)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
