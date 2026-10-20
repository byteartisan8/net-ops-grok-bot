"""The template's JavaScript, run with Node.js (Zabbix runs it with Duktape; the scripts stick to ES5 for that).

Zabbix wraps a preprocessing/script body in a function of ``value``; these tests do the same and substitute LLD and
user macros first, as Zabbix does. Real input: ``data/walk_spoke-2.txt`` is a walk[] value from the running lab.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from netsim_zabbix import names as N
from netsim_zabbix.template import js

from netsim_common import bsm_spec_from_topology, load_topology, pw_spec_macro_value

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")
WALK = (Path(__file__).parent / "data" / "walk_spoke-2.txt").read_text()
ENTRY = "1.3.6.1.4.1.9.10.106.1.2.1"


class JsError(RuntimeError):
    pass


def run(code: str, value: str, *, macros: dict[str, str] | None = None, prelude: str = "") -> str:
    for macro, val in (macros or {}).items():
        code = code.replace(macro, val)
    program = (
        f"{prelude}\nconst __value = {json.dumps(value)};\n"
        f"const __out = (function (value) {{\n{code}\n}})(__value);\nprocess.stdout.write(String(__out));"
    )
    res = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=20)
    if res.returncode:
        raise JsError(res.stderr.strip() or "error")  # node prints a thrown string on a line of its own
    return res.stdout


def row_script() -> str:
    from netsim_zabbix.template import Oids

    from netsim_common import load_oids

    o = Oids(load_oids())
    return js(
        "walk_lib",
        "pw_row",
        CPW_ENTRY=o.entry,
        COL_NAME=o.col["cpwVcName"],
        COL_OPER=o.col["cpwVcOperStatus"],
        COL_ADMIN=o.col["cpwVcAdminStatus"],
        COL_PEER=o.col["cpwVcPeerAddr"],
        COL_VCID=o.col["cpwVcID"],
        COL_RMTU=o.col["cpwVcRemoteIfMtu"],
        COL_DESCR=o.col["cpwVcDescr"],
    )


def row(service: str, role: str, walk: str = WALK) -> dict[str, Any]:
    return json.loads(run(row_script(), walk, macros={"{#SERVICE}": service, "{#ROLE}": role}))


# ------------------------------------------------------------------------------------------------ row
def test_row_from_a_real_walk() -> None:
    assert row("SVC-0002", "primary") == {
        "present": 1,
        "index": 1,
        "name": "SVC-0002:primary",
        "oper": 1,
        "admin": 1,
        "peer": "10.255.0.1",
        "vc_id": 1002,
        "remote_mtu": 1500,
        "customer": "Customer-A",
    }
    assert row("SVC-0003", "backup")["peer"] == "10.255.0.2"
    assert row("SVC-0003", "backup")["vc_id"] == 2003


def test_absent_row_is_a_defined_value() -> None:
    assert row("SVC-0001", "primary") == {
        "present": 0,
        "index": 0,
        "name": "SVC-0001:primary",
        "oper": 0,
        "admin": 0,
        "peer": "absent",
        "vc_id": 0,
        "remote_mtu": 0,
        "customer": "",
    }


def test_row_handles_printable_peer_octets_and_down_status() -> None:
    walk = "\n".join(
        [
            f'.{ENTRY}.21.7 = STRING: "SVC-0042:backup"',
            f'.{ENTRY}.9.7 = STRING: "ABCD"',  # 65.66.67.68: printable octets come back as a string
            f".{ENTRY}.10.7 = Gauge32: 2042",
            f".{ENTRY}.25.7 = INTEGER: 2",
            f".{ENTRY}.26.7 = INTEGER: 7",
            f'.{ENTRY}.22.7 = STRING: "Say \\"hi\\""',
        ]
    )
    r = row("SVC-0042", "backup", walk)
    assert (r["peer"], r["oper"], r["admin"], r["index"], r["customer"]) == ("65.66.67.68", 7, 2, 7, 'Say "hi"')


def test_row_with_an_empty_walk() -> None:
    assert row("SVC-0002", "primary", "")["present"] == 0


# ------------------------------------------------------------------------------------------------ spec diff
def diff(r: dict[str, Any]) -> str:
    macros = {"{#EXPECTED_PEER}": "10.255.0.1", "{#EXPECTED_PEER_HOST}": "hub-a", "{#VC_ID}": "1002"}
    return run(js("pw_spec_diff"), json.dumps(r), macros=macros)


@pytest.mark.parametrize(
    ("peer", "vc_id", "expected"),
    [
        ("10.255.0.1", 1002, "matches NetBox"),
        ("10.255.0.2", 1002, "peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)"),
        ("10.255.0.1", 1102, "VC ID 1102, NetBox expects 1002"),
        ("10.255.0.2", 1102, "peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a); VC ID 1102, NetBox expects 1002"),
    ],
)
def test_spec_diff(peer: str, vc_id: int, expected: str) -> None:
    assert diff({"present": 1, "peer": peer, "vc_id": vc_id}) == expected


def test_spec_diff_absent() -> None:
    assert diff({"present": 0, "peer": "absent", "vc_id": 0}) == "absent"


# ------------------------------------------------------------------------------------------------ spec discovery
def discovery(params: dict[str, str]) -> list[dict[str, Any]]:
    return json.loads(run(js("pw_spec_discovery"), json.dumps(params)))


def test_spec_discovery_concatenates_chunks_in_order() -> None:
    spec = bsm_spec_from_topology(load_topology())
    value = json.loads(pw_spec_macro_value(spec, "hub-a"))
    params = {f"spec{n}": "[]" for n in range(1, 9)}
    params["spec1"] = json.dumps(value[:4])
    params["spec10"] = "[]"
    params["spec2"] = json.dumps(value[4:])
    rows = discovery(params)
    assert [r["vcname"] for r in rows] == [f"{e['service']}:{e['role']}" for e in value]
    assert rows[0] | {} == {**value[0], "vcname": "SVC-0001:primary"}


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"spec1": "{$NETSIM.PW.SPEC}"}, "is not valid JSON"),
        ({"spec1": "{}"}, "must be a JSON list"),
        ({"spec1": '[{"service": "SVC-0001"}]'}, "malformed spec entry"),
        (
            {
                "spec1": '[{"service":"SVC-0001","role":"primary","vc_id":1}]',
                "spec2": '[{"service":"SVC-0001","role":"primary","vc_id":1}]',
            },
            "duplicate spec entry SVC-0001:primary",
        ),
    ],
)
def test_spec_discovery_fails_loudly(params: dict[str, str], message: str) -> None:
    with pytest.raises(JsError, match=message):
        discovery(params)


# ------------------------------------------------------------------------------------------------ observed view
def test_observed_lld_converts_peers() -> None:
    rows = [
        {"{#SNMPINDEX}": "1", "{#VCNAME}": "SVC-0002:primary", "{#PEER}": "0A FF 00 01"},
        {"{#SNMPINDEX}": "2", "{#VCNAME}": "odd-name", "{#PEER}": "0A FF 00 02"},
    ]
    out = json.loads(run(js("hex_lib", "observed_lld"), json.dumps(rows)))
    assert out[0]["{#PEER}"] == "10.255.0.1"
    assert (out[0]["{#SERVICE}"], out[0]["{#ROLE}"]) == ("SVC-0002", "primary")
    assert (out[1]["{#SERVICE}"], out[1]["{#ROLE}"]) == ("odd-name", "")


def test_observed_expected_reads_the_spec_macros() -> None:
    spec = bsm_spec_from_topology(load_topology())
    code = js("observed_expected", SPEC_MACROS=",".join(N.SPEC_MACROS))
    macros = dict.fromkeys(N.SPEC_MACROS, "[]") | {N.SPEC_MACRO: pw_spec_macro_value(spec, "spoke-2")}
    assert run(code, "SVC-0002:primary", macros=macros) == "1"
    assert run(code, "SVC-0099:primary", macros=macros) == "0"
    assert run(code, "SVC-0001:primary", macros=macros) == "0"  # SVC-0001 is not on spoke-2


# ------------------------------------------------------------------------------------------------ check now
MOCK = """
const __calls = [];
function HttpRequest() { this.headers = []; }
HttpRequest.prototype.addHeader = function (h) { this.headers.push(h); };
HttpRequest.prototype.getStatus = function () { return 200; };
HttpRequest.prototype.post = function (url, body) {
    const req = JSON.parse(body);
    __calls.push({url: url, method: req.method, params: req.params, auth: this.headers});
    const results = {
        'item.get': function () { return [{itemid: '501', key_: 'netsim.snmp.walk'}]; },
        'discoveryrule.get': function () {
            return [{itemid: '601', key_: 'netsim.pw.spec.discovery', type: '21'},
                    {itemid: '602', key_: 'netsim.pw.discovery', type: '18'}];
        },
        'task.create': function () { return {taskids: req.params.map(function (t, i) { return String(900 + i); })}; },
        'event.acknowledge': function () { return {eventids: req.params.eventids}; }
    };
    return JSON.stringify({jsonrpc: '2.0', result: results[req.method](), id: 1});
};
const Zabbix = {log: function () {}};
"""


def check_now(trap: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    params = {
        "api_url": "http://api",
        "token": "t0k",
        "hostid": "10500",
        "trap": trap,
        "walk_key": N.WALK_KEY,
        "event": "77",
    }
    code = js("check_now") + ""
    program_tail = "\nprocess.stderr.write(JSON.stringify(__calls));"
    res = subprocess.run(
        [
            NODE,
            "-e",
            f"{MOCK}\nconst __out = (function (value) {{\n{code}\n}})({json.dumps(json.dumps(params))});"
            f"\nprocess.stdout.write(String(__out));{program_tail}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(res.stdout), json.loads(res.stderr)


def test_check_now_pw_trap_executes_the_walk_only() -> None:
    out, calls = check_now("pw")
    assert [c["method"] for c in calls] == ["item.get", "task.create", "event.acknowledge"]
    assert calls[1]["params"] == [{"type": 6, "request": {"itemid": "501"}}]
    assert "Authorization: Bearer t0k" in calls[0]["auth"]
    assert out["executed"] == [N.WALK_KEY]
    # the trap event is closed (1) with a message (4) once the re-poll is scheduled: one short event per trap
    close = calls[-1]["params"]
    assert (close["eventids"], close["action"]) == (["77"], 5) and "re-poll scheduled" in close["message"]
    assert out["closed"] == "77"


@pytest.mark.parametrize("trap", ["config", "boot"])
def test_check_now_config_trap_also_runs_the_spec_discovery(trap: str) -> None:
    out, calls = check_now(trap)
    assert [c["method"] for c in calls] == ["item.get", "discoveryrule.get", "task.create", "event.acknowledge"]
    assert [t["request"]["itemid"] for t in calls[2]["params"]] == ["501", "601"]  # not the dependent rule 602
    assert out["executed"] == [N.WALK_KEY, N.SPEC_LLD_KEY]
