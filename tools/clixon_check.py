#!/usr/bin/env python3
"""Validate golden configs and YANG examples in a real Clixon backend (run inside tools/clixon-check.sh).

Each RESTCONF JSON file is converted to NETCONF XML, sent with <edit-config> (replace) to the candidate
datastore and checked with <validate>; the outcome must match contract/yang/examples/expectations.yaml
(golden configs must be valid). Standard library + PyYAML only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

REPO = Path(__file__).resolve().parents[1]
YANG_DIR = REPO / "contract" / "yang"
EXAMPLES = YANG_DIR / "examples"
NAMESPACE = "urn:netsim:device"
MODULE = "netsim-device"
EOM = "]]>]]>"
# Differences that only exist in the JSON encoding (XML carries every value as text).
JSON_ONLY = {"string-vc-id.json"}

CONFIG = """<clixon-config xmlns="http://clicon.org/config">
  <CLICON_CONFIGFILE>{conf}</CLICON_CONFIGFILE>
  <CLICON_FEATURE>*:*</CLICON_FEATURE>
  <CLICON_YANG_DIR>/usr/local/share/clixon</CLICON_YANG_DIR>
  <CLICON_YANG_DIR>{yang_dir}</CLICON_YANG_DIR>
  <CLICON_YANG_MAIN_FILE>{yang_dir}/netsim-device.yang</CLICON_YANG_MAIN_FILE>
  <CLICON_SOCK>{tmp}/clixon.sock</CLICON_SOCK>
  <CLICON_SOCK_GROUP>clicon</CLICON_SOCK_GROUP>
  <CLICON_BACKEND_PIDFILE>{tmp}/clixon.pid</CLICON_BACKEND_PIDFILE>
  <CLICON_XMLDB_DIR>{tmp}/db</CLICON_XMLDB_DIR>
  <CLICON_STARTUP_MODE>init</CLICON_STARTUP_MODE>
  <CLICON_NETCONF_HELLO_OPTIONAL>true</CLICON_NETCONF_HELLO_OPTIONAL>
</clixon-config>
"""


def to_xml(name: str, value: object) -> str:
    if isinstance(value, dict):
        return f"<{name}>" + "".join(children(value)) + f"</{name}>"
    if isinstance(value, bool):
        return f"<{name}>{'true' if value else 'false'}</{name}>"
    return f"<{name}>{escape(str(value))}</{name}>"


def children(obj: dict) -> list[str]:
    out = []
    for key, value in obj.items():
        for item in value if isinstance(value, list) else [value]:
            out.append(to_xml(key, item))
    return out


def config_xml(data: dict) -> str:
    parts = []
    for key, value in data.items():
        module, _, name = key.partition(":")
        assert module == MODULE, key
        parts.append(to_xml(name, value).replace(f"<{name}>", f'<{name} xmlns="{NAMESPACE}">', 1))
    return "<config>" + "".join(parts) + "</config>"


def rpc(message_id: int, body: str) -> str:
    return f'<rpc xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" message-id="{message_id}">{body}</rpc>{EOM}'


def check(conf: Path, data: dict) -> tuple[bool, str]:
    """Return (valid, error text) for one config."""
    msgs = [
        rpc(1, "<edit-config><target><candidate/></target><default-operation>replace</default-operation>"
               f"{config_xml(data)}</edit-config>"),
        rpc(2, "<validate><source><candidate/></source></validate>"),
        rpc(3, "<discard-changes/>"),
        rpc(4, "<close-session/>"),
    ]  # fmt: skip
    proc = subprocess.run(
        ["clixon_netconf", "-f", str(conf), "-q", "-0", "-l", "e"],
        input="".join(msgs),
        capture_output=True,
        text=True,
        timeout=60,
    )
    replies = [r for r in proc.stdout.split(EOM) if "<rpc-reply" in r]
    if len(replies) < 2:
        return False, f"no reply (stderr: {proc.stderr.strip()[:300]})"
    errors = []
    for reply in replies[:2]:
        for err in re.findall(r"<rpc-error>(.*?)</rpc-error>", reply, re.S):
            found = re.search(r"<error-message[^>]*>(.*?)</error-message>", err, re.S) or re.search(
                r"<error-app-tag>(.*?)</error-app-tag>", err
            )
            errors.append(found.group(1).strip() if found else err[:300])
    return not errors, "; ".join(errors)


def main() -> int:
    expect = yaml.safe_load((EXAMPLES / "expectations.yaml").read_text(encoding="utf-8"))
    cases = [(p, True) for p in sorted((REPO / "contract" / "golden").glob("*.json"))]
    cases += [(EXAMPLES / "valid" / f, True) for f in sorted(expect["valid"])]
    cases += [(EXAMPLES / "invalid" / f, False) for f in sorted(expect["invalid"])]

    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "clixon.xml"
        conf.write_text(CONFIG.format(conf=conf, tmp=tmp, yang_dir=YANG_DIR), encoding="utf-8")
        os.makedirs(Path(tmp) / "db")
        probe = subprocess.run(["clixon_backend", "-V"], capture_output=True, text=True)
        version = next((ln for ln in (probe.stdout + probe.stderr).splitlines() if "Clixon version" in ln), "")
        backend = subprocess.Popen(
            ["clixon_backend", "-f", str(conf), "-F", "-s", "init", "-l", "e"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if (Path(tmp) / "clixon.sock").exists() or backend.poll() is not None:
                    break
                time.sleep(0.1)
            if backend.poll() is not None:
                print(f"clixon_backend failed to start:\n{backend.stdout.read()}", file=sys.stderr)
                return 1
            print(f"{version or 'Clixon'}: netsim-device loaded")
            failures = 0
            for path, want_valid in cases:
                name = path.relative_to(REPO).as_posix()
                if path.name in JSON_ONLY:
                    print(f"  skip  {name} (JSON-encoding check only)")
                    continue
                valid, error = check(conf, json.loads(path.read_text(encoding="utf-8")))
                good = valid == want_valid
                failures += not good
                verdict = "valid" if valid else f"rejected: {error}"
                print(f"  {'ok  ' if good else 'FAIL'}  {name}: {verdict}")
            print(f"{len(cases) - failures}/{len(cases)} cases behave as expected in Clixon")
            return 1 if failures else 0
        finally:
            backend.terminate()
            backend.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
