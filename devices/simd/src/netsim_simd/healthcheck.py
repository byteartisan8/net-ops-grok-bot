"""Container HEALTHCHECK: RESTCONF answers, SNMP ``sysName.0`` answers, simd ``/health`` is up.

Exit 0 when all three pass, 1 otherwise (one line per check on stdout, as ``docker inspect`` shows it).
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
    ]
    for ok, text in results:
        print(("ok   " if ok else "FAIL ") + text)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
