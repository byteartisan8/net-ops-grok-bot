"""Template ``NetSim L2VPN PE SNMP`` (contract sections 8, 9.1, 9.2), generated into git as Zabbix 8.0 export YAML.

``zabbix-template`` writes ``zabbix/template/netsim_l2vpn_pe_snmp.yaml`` (``--check``: fail if it is stale);
``zabbix-provision`` imports that file through the API. Every OID comes from ``contract/oids.yaml``, the JavaScript
from ``netsim_zabbix/js/``. UUIDs are derived from the object's path, so regenerating changes nothing.

Polling: one SNMP ``walk[]`` master item (cpwVcTable columns + IF-MIB) every 60 s; everything else is dependent.

* "Expected PWs" (intent): Script discovery whose data is the host's ``{$NETSIM.PW.SPEC}`` (+ overflow chunks).
  Per PW a row item (JSON, dependent on the walk; JavaScript finds ``cpwVcName == "<service>:<role>"``) and field
  items read from it; trigger prototypes ``missing`` / ``oper`` / ``spec`` exactly as contract section 9.2.
* "PW table" (observed): SNMP walk to JSON over cpwVcTable; trigger ``unexpected`` for rows not in the spec.
* Interfaces: SNMP walk to JSON over IF-MIB (oper/admin status, MTU).
* Traps (matched by numeric OID): cpwVcDown/cpwVcUp (``trap=pw``), IF-MIB linkDown/linkUp (``trap=link``),
  ciscoConfigManEvent (``trap=config``), coldStart (``trap=boot``), fallback; exactly one problem event per trap,
  closed by the check-now action.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from netsim_common.oids import OidRegistry, load_oids
from netsim_common.paths import repo_root
from netsim_zabbix import names as N
from netsim_zabbix.canonical import canonicalize

EXPORT_VERSION = "8.0"
T = N.TEMPLATE


def template_path() -> Path:
    return repo_root() / "zabbix" / "template" / "netsim_l2vpn_pe_snmp.yaml"


def zbx_uuid(*parts: str) -> str:
    """Deterministic UUIDv4-formatted ID (Zabbix import requires version 4 / RFC 4122 variant bits)."""
    raw = bytearray(hashlib.sha256("\x1f".join(("netsim", *parts)).encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return raw.hex()


def js(*files: str, **subst: str) -> str:
    """Concatenate JavaScript files from ``netsim_zabbix/js`` and replace ``__NAME__`` placeholders."""
    pkg = resources.files("netsim_zabbix") / "js"
    code = "\n".join(pkg.joinpath(f"{f}.js").read_text(encoding="utf-8").rstrip("\n") + "\n" for f in files)
    for key, val in subst.items():
        code = code.replace(f"__{key}__", val)
    leftover = re.findall(r"__[A-Z_]+__", code)
    if leftover:
        raise ValueError(f"unreplaced placeholders in {files}: {sorted(set(leftover))}")
    return code


def ref(key: str) -> str:
    return f"/{T}/{key}"


def tags(**kv: str) -> list[dict[str, str]]:
    return [{"tag": k, "value": v} for k, v in kv.items()]


# ------------------------------------------------------------------------------------------------ OIDs
class Oids:
    def __init__(self, reg: OidRegistry) -> None:
        self.reg = reg
        self.entry = reg.oid("cpwVcEntry")
        col = {c.name: c.oid.rsplit(".", 1)[1] for c in reg.columns("cpwVcTable")}
        self.col = col
        self.pw_columns = [
            "cpwVcPeerAddr",
            "cpwVcID",
            "cpwVcRemoteIfMtu",
            "cpwVcName",
            "cpwVcDescr",
            "cpwVcAdminStatus",
            "cpwVcOperStatus",
        ]
        self.if_columns = ["ifDescr", "ifMtu", "ifAdminStatus", "ifOperStatus", "ifName", "ifAlias"]

    def __call__(self, name: str) -> str:
        return self.reg.oid(name)

    def walk(self) -> str:
        return "walk[" + ",".join(self(c) for c in [*self.pw_columns, *self.if_columns]) + "]"

    def notification_regex(self, name: str) -> str:
        """``snmptrap[]`` regex: the snmpTrapOID.0 varbind of a trap as zabbix-snmptraps writes it (-On)."""
        trap_oid = re.escape("." + self.reg.notification(name).oid)
        varbind = re.escape("." + self.reg.oid("snmpTrapOID") + ".0")
        return f"{varbind} = {trap_oid}(\\s|$)"


# ------------------------------------------------------------------------------------------------ value maps
def valuemaps(o: Oids) -> list[dict[str, Any]]:
    def vm(name: str, mapping: dict[int, str]) -> dict[str, Any]:
        return {
            "uuid": zbx_uuid("valuemap", name),
            "name": name,
            "mappings": [{"value": str(k), "newvalue": v} for k, v in sorted(mapping.items())],
        }

    def enum(obj: str) -> dict[int, str]:
        return {v: k for k, v in o.reg.enum(obj).items()}

    return sorted(
        [
            vm("NetSim PW oper status", {0: "absent", **enum("cpwVcOperStatus")}),
            vm("NetSim PW admin status", {0: "absent", **enum("cpwVcAdminStatus")}),
            vm("NetSim PW presence", {0: "absent", 1: "present"}),
            vm("NetSim NetBox spec membership", {0: "not in NetBox", 1: "in NetBox"}),
            vm("IF-MIB::ifOperStatus", enum("ifOperStatus")),
            vm("IF-MIB::ifAdminStatus", enum("ifAdminStatus")),
            vm("Zabbix SNMP availability", {0: "not available", 1: "available", 2: "unknown"}),
        ],
        key=lambda m: m["name"],
    )


# ------------------------------------------------------------------------------------------------ items
def walk_item(o: Oids) -> dict[str, Any]:
    cols = ", ".join([*o.pw_columns, *o.if_columns])
    return {
        "uuid": zbx_uuid("item", N.WALK_KEY),
        "name": "SNMP walk: PW table and interfaces",
        "type": "SNMP_AGENT",
        "snmp_oid": o.walk(),
        "key": N.WALK_KEY,
        "delay": "{$NETSIM.WALK.INTERVAL}",
        "history": "1d",
        "value_type": "TEXT",
        "description": (
            "Master item (contract 9.1): one SNMP walk of the cpwVcTable columns and IF-MIB every "
            "{$NETSIM.WALK.INTERVAL}; every PW and interface item is dependent on it. An SNMP trap runs it "
            f'immediately (action "{N.TRAP_ACTION}").\nColumns: {cols}.'
        ),
        "tags": tags(component="raw"),
    }


def availability_item() -> dict[str, Any]:
    return {
        "uuid": zbx_uuid("item", N.SNMP_AVAILABLE_KEY),
        "name": "SNMP agent availability",
        "type": "INTERNAL",
        "key": N.SNMP_AVAILABLE_KEY,
        "delay": "10s",
        "history": "7d",
        "description": "Availability of the SNMP interface (Zabbix internal item): 0 not available, 1 available.",
        "valuemap": {"name": "Zabbix SNMP availability"},
        "tags": tags(component="health"),
        "triggers": [
            {
                "uuid": zbx_uuid("trigger", "snmp-unavailable"),
                "expression": f"max({ref(N.SNMP_AVAILABLE_KEY)},{{$NETSIM.SNMP.UNAVAILABLE}})=0",
                "name": "{HOST.HOST}: SNMP agent unreachable",
                "event_name": "{HOST.HOST}: SNMP agent unreachable for {$NETSIM.SNMP.UNAVAILABLE} (device down?)",
                "priority": "HIGH",
                "description": (
                    "No SNMP response from the device. Its own PW items stop updating (stale), but each PW is "
                    "still checked from its other end, so the BSM sees the outage through the peers."
                ),
                "tags": tags(scope=N.SCOPE_AVAILABILITY, host="{HOST.HOST}"),
            }
        ],
    }


def trap_item(o: Oids, kind: str, key_name: str, notification: str | None, title: str) -> dict[str, Any]:
    if notification is None:
        key = "snmptrap.fallback"
        desc = "Every SNMP trap from this host that no other snmptrap[] item matched (kept for inspection)."
        regex_note = ""
    else:
        regex = o.notification_regex(notification)
        key = f'snmptrap["{regex}"]'
        desc = (
            f"SNMP trap {notification} ({o.reg.notification(notification).oid}), matched by numeric OID in the "
            "trap text written by zabbix-snmptraps (snmptrapd -On; the mounted MIBs only decode the values)."
        )
        regex_note = regex
    item: dict[str, Any] = {
        "uuid": zbx_uuid("item", key_name),
        "name": title,
        "type": "SNMP_TRAP",
        "key": key,
        "history": "7d",
        "value_type": "LOG",
        "description": desc,
        "tags": tags(component="trap", **({N.TRAP_TAG: kind} if kind else {})),
    }
    if notification is not None:
        item["triggers"] = [
            {
                "uuid": zbx_uuid("trigger", "trap", key_name),
                # No time-based function: Zabbix evaluates the trigger only when a trap value arrives. (With a
                # nodata() recovery the timer re-evaluated it every 30 s and, in MULTIPLE mode, every evaluation
                # made another problem event: two "trap" events and two re-polls per trap.)
                "expression": f"length(last({ref(key)}))>0",
                "recovery_mode": "NONE",
                "name": f"{{HOST.HOST}}: SNMP trap {notification}",
                "event_name": f"{{HOST.HOST}}: SNMP trap {notification} (hint, re-polling now)",
                "priority": "INFO",
                "type": "MULTIPLE",
                "manual_close": "YES",
                "description": (
                    "Exactly one problem event per received trap (multiple problem events); it starts the action "
                    f'"{N.TRAP_ACTION}", which runs "{N.CHECK_NOW_SCRIPT}" and then closes the event (OK event '
                    "generation: none). Traps are hints, polling is truth: the PW checks only use polled values.\n"
                    f"Trap regex: {regex_note}"
                ),
                "tags": tags(scope=N.SCOPE_TRAP, **{N.TRAP_TAG: kind}, host="{HOST.HOST}"),
            }
        ]
    return item


# ------------------------------------------------------------------------------------------------ intent LLD
PW_TAGS_COMMON = {
    "scope": N.SCOPE_PW_TAG,
    "service": "{#SERVICE}",
    "pw_role": "{#ROLE}",
    "customer": "{#CUSTOMER}",
    "host": "{HOST.HOST}",
}


def pw_trigger_tags(check: str) -> list[dict[str, str]]:
    return tags(**PW_TAGS_COMMON, check=check, vcid="{#VC_ID}", peer="{#EXPECTED_PEER}")


def spec_lld(o: Oids) -> dict[str, Any]:
    k = N.pw_key
    present, oper, peer, vcid = k("present"), k("oper"), k("peer"), k("vcid")
    missing_expr = f"last({ref(present)})=0"
    missing_name = "{#SERVICE} {#ROLE} on {HOST.HOST}: PW missing"
    who = "PW {#SERVICE} {#ROLE}"
    row_js = js(
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

    def field(
        name: str, fkey: str, path: str, value_type: str, desc: str, valuemap: str | None = None
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "uuid": zbx_uuid("item_prototype", k(fkey)),
            "name": f"{who}: {name}",
            "type": "DEPENDENT",
            "key": k(fkey),
            "history": "7d",
            **({} if value_type == "UNSIGNED" else {"value_type": value_type}),
            "description": desc,
            "preprocessing": [{"type": "JSONPATH", "parameters": [path]}],
            "master_item": {"key": k("row")},
            "tags": tags(component="pw", service="{#SERVICE}", pw_role="{#ROLE}"),
        }
        if valuemap:
            item["valuemap"] = {"name": valuemap}
        return item

    present_item = field(
        "present on device",
        "present",
        "$.present",
        "UNSIGNED",
        "1 if the device has a cpwVcTable row named {#SERVICE}:{#ROLE}, 0 (absent) if not.",
        "NetSim PW presence",
    )
    present_item["trigger_prototypes"] = [
        {
            "uuid": zbx_uuid("trigger_prototype", "missing"),
            "expression": missing_expr,
            "name": missing_name,
            "event_name": (
                "{#SERVICE} {#ROLE} on {HOST.HOST}: PW missing, NetBox expects VC ID {#VC_ID} to "
                "{#EXPECTED_PEER} ({#EXPECTED_PEER_HOST}) on {#AC}"
            ),
            "priority": "HIGH",
            "description": (
                "check=missing (contract 9.2): NetBox expects this PW on this host, the device has no row named "
                "{#SERVICE}:{#ROLE}. Right after an order in NetBox this means 'ordered but not delivered'."
            ),
            "tags": pw_trigger_tags("missing"),
        }
    ]
    items = [
        {
            "uuid": zbx_uuid("item_prototype", k("row")),
            "name": f"{who}: device row",
            "type": "DEPENDENT",
            "key": k("row"),
            "history": "7d",
            "value_type": "JSON",
            "description": (
                "The cpwVcTable row named {#SERVICE}:{#ROLE} as JSON (present, index, oper, admin, peer as dotted "
                "quad, vc_id, remote_mtu, customer), or present=0 with defined 'absent' values. Found by "
                "JavaScript in every walk value. NetBox spec: VC ID {#VC_ID}, peer {#EXPECTED_PEER} "
                "({#EXPECTED_PEER_HOST}), AC {#AC}, customer {#CUSTOMER}."
            ),
            "preprocessing": [{"type": "JAVASCRIPT", "parameters": [row_js]}],
            "master_item": {"key": N.WALK_KEY},
            "tags": tags(component="pw", service="{#SERVICE}", pw_role="{#ROLE}"),
        },
        present_item,
        field(
            "oper status",
            "oper",
            "$.oper",
            "UNSIGNED",
            "cpwVcOperStatus of the row (0 = absent).",
            "NetSim PW oper status",
        ),
        field(
            "admin status",
            "admin",
            "$.admin",
            "UNSIGNED",
            "cpwVcAdminStatus of the row (0 = absent).",
            "NetSim PW admin status",
        ),
        field(
            "peer",
            "peer",
            "$.peer",
            "CHAR",
            "cpwVcPeerAddr as dotted quad ('absent' without a row). NetBox expects {#EXPECTED_PEER} "
            "({#EXPECTED_PEER_HOST}).",
        ),
        field("VC ID", "vcid", "$.vc_id", "UNSIGNED", "cpwVcID (0 = absent). NetBox expects {#VC_ID}."),
        field(
            "remote MTU",
            "remote_mtu",
            "$.remote_mtu",
            "UNSIGNED",
            "cpwVcRemoteIfMtu learned from the peer (0 = unknown or absent).",
        ),
        {
            "uuid": zbx_uuid("item_prototype", k("spec_diff")),
            "name": f"{who}: difference from NetBox",
            "type": "DEPENDENT",
            "key": k("spec_diff"),
            "history": "7d",
            "value_type": "CHAR",
            "description": (
                "How the row differs from its NetBox spec, in words (used in the 'spec' event name): "
                "'matches NetBox', 'absent', or e.g. 'peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)'."
            ),
            "preprocessing": [{"type": "JAVASCRIPT", "parameters": [js("pw_spec_diff")]}],
            "master_item": {"key": k("row")},
            "tags": tags(component="pw", service="{#SERVICE}", pw_role="{#ROLE}"),
        },
    ]
    missing_dep = [{"name": missing_name, "expression": missing_expr}]
    triggers = [
        {
            "uuid": zbx_uuid("trigger_prototype", "oper"),
            "expression": f"last({ref(present)})=1 and last({ref(oper)})<>1",
            "name": "{#SERVICE} {#ROLE} on {HOST.HOST}: PW down",
            "event_name": (
                "{#SERVICE} {#ROLE} on {HOST.HOST}: PW {ITEM.VALUE2}, VC ID {#VC_ID} to {#EXPECTED_PEER} "
                "({#EXPECTED_PEER_HOST})"
            ),
            "opdata": "oper {ITEM.LASTVALUE2}",
            "priority": "HIGH",
            "description": (
                "check=oper (contract 9.2): the row is present and cpwVcOperStatus is not up(1). Depends on "
                "'missing'. The device's simd API (/state) gives the reason (contract 7)."
            ),
            "dependencies": missing_dep,
            "tags": pw_trigger_tags("oper"),
        },
        {
            "uuid": zbx_uuid("trigger_prototype", "spec"),
            "expression": (
                f'last({ref(present)})=1 and (last({ref(peer)})<>"{{#EXPECTED_PEER}}" or last({ref(vcid)})<>{{#VC_ID}})'
            ),
            "name": "{#SERVICE} {#ROLE} on {HOST.HOST}: PW differs from NetBox",
            "event_name": f"{{#SERVICE}} {{#ROLE}} on {{HOST.HOST}}: {{?last(/{{HOST.HOST}}/{k('spec_diff')})}}",
            "opdata": "peer {ITEM.LASTVALUE2}, VC ID {ITEM.LASTVALUE3}",
            "priority": "AVERAGE",
            "description": (
                "check=spec (contract 9.2): the row is present but its peer or VC ID is not what NetBox says "
                "(expected peer {#EXPECTED_PEER} ({#EXPECTED_PEER_HOST}), VC ID {#VC_ID}). The PW may run, but "
                "not on the path NetBox intends, so redundancy may be fake. Depends on 'missing'."
            ),
            "dependencies": missing_dep,
            "tags": pw_trigger_tags("spec"),
        },
    ]
    params = [{"name": f"spec{n}", "value": macro} for n, macro in enumerate(N.SPEC_MACROS, start=1)]
    return {
        "uuid": zbx_uuid("discovery_rule", N.SPEC_LLD_KEY),
        "name": "Expected PWs (NetBox spec)",
        "type": "SCRIPT",
        "key": N.SPEC_LLD_KEY,
        "delay": "{$NETSIM.SPEC.LLD.INTERVAL}",
        "params": js("pw_spec_discovery"),
        "parameters": params,
        "timeout": "5s",
        "lifetime_type": "DELETE_IMMEDIATELY",
        "description": (
            "Intent view (contract 9.2): one row per PW that NetBox expects on this host, read from the host macro "
            f"{N.SPEC_MACRO} (JSON list written by zabbix-sync from the BSM spec). Hosts whose spec exceeds one "
            f"macro value ({N.SPEC_MACRO_MAX_LEN} characters in Zabbix 8.0) continue in "
            f"{N.SPEC_OVERFLOW_MACROS[0]} .. {N.SPEC_OVERFLOW_MACROS[-1]}. A PW removed from NetBox disappears "
            "immediately with its items and triggers."
        ),
        "item_prototypes": items,
        "trigger_prototypes": triggers,
        "lld_macro_paths": [
            {"lld_macro": m, "path": p}
            for m, p in [
                ("{#AC}", "$.ac"),
                ("{#CUSTOMER}", "$.customer"),
                ("{#EXPECTED_PEER}", "$.expected_peer"),
                ("{#EXPECTED_PEER_HOST}", "$.expected_peer_host"),
                ("{#ROLE}", "$.role"),
                ("{#SERVICE}", "$.service"),
                ("{#VCNAME}", "$.vcname"),
                ("{#VC_ID}", "$.vc_id"),
            ]
        ],
    }


# ------------------------------------------------------------------------------------------------ observed LLD
def observed_lld(o: Oids) -> dict[str, Any]:
    def key(f: str) -> str:
        return f"netsim.pw.observed.{f}[{{#SNMPINDEX}}]"

    def column(name: str) -> str:
        return f"{o(name)}.{{#SNMPINDEX}}"

    who = "Observed PW {#VCNAME}"
    spec_macros = ",".join(N.SPEC_MACROS)

    def proto(name: str, f: str, value_type: str, steps: list[dict[str, Any]], desc: str, vm: str | None = None):
        item: dict[str, Any] = {
            "uuid": zbx_uuid("item_prototype", key(f)),
            "name": f"{who}: {name}",
            "type": "DEPENDENT",
            "key": key(f),
            "history": "7d",
            **({} if value_type == "UNSIGNED" else {"value_type": value_type}),
            "description": desc,
            "preprocessing": steps,
            "master_item": {"key": N.WALK_KEY},
            "tags": tags(component="pw-observed", vcname="{#VCNAME}"),
        }
        if vm:
            item["valuemap"] = {"name": vm}
        return item

    expected = proto(
        "in NetBox spec",
        "expected",
        "UNSIGNED",
        [
            {"type": "SNMP_WALK_VALUE", "parameters": [column("cpwVcName"), "0"]},
            {"type": "JAVASCRIPT", "parameters": [js("observed_expected", SPEC_MACROS=spec_macros)]},
        ],
        f"1 if cpwVcName {{#VCNAME}} is in this host's {N.SPEC_MACRO}, else 0.",
        "NetSim NetBox spec membership",
    )
    expected["trigger_prototypes"] = [
        {
            "uuid": zbx_uuid("trigger_prototype", "unexpected"),
            "expression": f"last({ref(key('expected'))})=0",
            "name": "{#VCNAME} on {HOST.HOST}: PW not in NetBox",
            "event_name": "{#VCNAME} on {HOST.HOST}: PW not in NetBox (VC ID {#VCID} to {#PEER})",
            "priority": "WARNING",
            "description": (
                "check=unexpected (contract 9.2): the device has a PW that NetBox does not know about. "
                "Not mapped into the BSM (scope=l2vpn-unexpected)."
            ),
            "tags": tags(
                scope=N.SCOPE_UNEXPECTED_TAG,
                check="unexpected",
                vcname="{#VCNAME}",
                service="{#SERVICE}",
                pw_role="{#ROLE}",
                customer="{#CUSTOMER}",
                host="{HOST.HOST}",
                vcid="{#VCID}",
                peer="{#PEER}",
            ),
        }
    ]
    items = [
        proto(
            "oper status",
            "oper",
            "UNSIGNED",
            [{"type": "SNMP_WALK_VALUE", "parameters": [column("cpwVcOperStatus"), "0"]}],
            "cpwVcOperStatus.",
            "NetSim PW oper status",
        ),
        proto(
            "peer",
            "peer",
            "CHAR",
            [
                {"type": "SNMP_WALK_VALUE", "parameters": [column("cpwVcPeerAddr"), "0"]},
                {"type": "JAVASCRIPT", "parameters": [js("hex_lib") + "return nsHexAddress(value);\n"]},
            ],
            "cpwVcPeerAddr as dotted quad.",
        ),
        proto(
            "VC ID",
            "vcid",
            "UNSIGNED",
            [{"type": "SNMP_WALK_VALUE", "parameters": [column("cpwVcID"), "0"]}],
            "cpwVcID.",
        ),
        expected,
    ]
    walk_to_json: list[str] = []
    for macro, obj in [
        ("{#VCNAME}", "cpwVcName"),
        ("{#VCID}", "cpwVcID"),
        ("{#PEER}", "cpwVcPeerAddr"),
        ("{#CUSTOMER}", "cpwVcDescr"),
    ]:
        walk_to_json += [macro, o(obj), "0"]
    return {
        "uuid": zbx_uuid("discovery_rule", N.OBSERVED_LLD_KEY),
        "name": "PW table (device)",
        "type": "DEPENDENT",
        "key": N.OBSERVED_LLD_KEY,
        "lifetime_type": "DELETE_IMMEDIATELY",
        "description": (
            "Observed view (contract 9.2): every cpwVcTable row the device has ({#SNMPINDEX}, {#VCNAME}, {#VCID}, "
            "{#PEER} as dotted quad, {#CUSTOMER}; {#SERVICE} and {#ROLE} split from {#VCNAME}). Raw visibility; "
            "flags rows that NetBox does not know (check=unexpected, tagged service/pw_role like the PW checks). "
            "Not mapped into the BSM."
        ),
        "item_prototypes": items,
        "master_item": {"key": N.WALK_KEY},
        "preprocessing": [
            {"type": "SNMP_WALK_TO_JSON", "parameters": walk_to_json},
            {"type": "JAVASCRIPT", "parameters": [js("hex_lib", "observed_lld")]},
        ],
    }


# ------------------------------------------------------------------------------------------------ interfaces
def interfaces_lld(o: Oids) -> dict[str, Any]:
    def key(f: str) -> str:
        return f"netsim.if.{f}[{{#SNMPINDEX}}]"

    def proto(name: str, f: str, obj: str, desc: str, vm: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "uuid": zbx_uuid("item_prototype", key(f)),
            "name": f"Interface {{#IFNAME}}: {name}",
            "type": "DEPENDENT",
            "key": key(f),
            "history": "7d",
            "description": desc,
            "preprocessing": [{"type": "SNMP_WALK_VALUE", "parameters": [f"{o(obj)}.{{#SNMPINDEX}}", "0"]}],
            "master_item": {"key": N.WALK_KEY},
            "tags": tags(component="interface", interface="{#IFNAME}"),
        }
        if vm:
            item["valuemap"] = {"name": vm}
        return item

    oper, admin = key("oper"), key("admin")
    walk_to_json: list[str] = []
    for macro, obj in [("{#IFNAME}", "ifName"), ("{#IFDESCR}", "ifDescr"), ("{#IFALIAS}", "ifAlias")]:
        walk_to_json += [macro, o(obj), "0"]
    return {
        "uuid": zbx_uuid("discovery_rule", N.IF_LLD_KEY),
        "name": "Interfaces",
        "type": "DEPENDENT",
        "key": N.IF_LLD_KEY,
        "lifetime_type": "DELETE_IMMEDIATELY",
        "description": "IF-MIB interfaces of the device config (ifName, ifDescr, ifAlias = description).",
        "item_prototypes": [
            proto("oper status", "oper", "ifOperStatus", "ifOperStatus.", "IF-MIB::ifOperStatus"),
            proto("admin status", "admin", "ifAdminStatus", "ifAdminStatus.", "IF-MIB::ifAdminStatus"),
            proto("MTU", "mtu", "ifMtu", "ifMtu (effective MTU, contract 6)."),
        ],
        "trigger_prototypes": [
            {
                "uuid": zbx_uuid("trigger_prototype", "if-down"),
                "expression": f"last({ref(admin)})=1 and last({ref(oper)})<>1",
                "name": "Interface {#IFNAME} on {HOST.HOST}: down",
                "event_name": "Interface {#IFNAME} ({#IFALIAS}) on {HOST.HOST}: {ITEM.VALUE2}, admin up",
                "priority": "AVERAGE",
                "description": (
                    "The interface is administratively up but not operationally up (physical fault, or its parent "
                    "is down). Not mapped into the BSM; the PW checks show the service impact."
                ),
                "tags": tags(scope=N.SCOPE_INTERFACE, interface="{#IFNAME}", host="{HOST.HOST}"),
            }
        ],
        "master_item": {"key": N.WALK_KEY},
        "preprocessing": [{"type": "SNMP_WALK_TO_JSON", "parameters": walk_to_json}],
    }


# ------------------------------------------------------------------------------------------------ template
def build_template(reg: OidRegistry | None = None) -> dict[str, Any]:
    o = Oids(reg or load_oids())
    macros = [
        {
            "macro": N.SPEC_MACRO,
            "value": "[]",
            "description": (
                "NetBox spec of this host's PWs (contract 9.2): compact JSON list of {service, role, vc_id, "
                "expected_peer, expected_peer_host, customer, ac}. Written per host by zabbix-sync; never edit "
                "by hand."
            ),
        },
        *[
            {
                "macro": m,
                "value": "[]",
                "description": f"Overflow chunk of {N.SPEC_MACRO} for hosts whose spec exceeds "
                f"{N.SPEC_MACRO_MAX_LEN} characters (written by zabbix-sync).",
            }
            for m in N.SPEC_OVERFLOW_MACROS
        ],
        {"macro": "{$NETSIM.WALK.INTERVAL}", "value": "60s", "description": "Polling interval of the walk."},
        {
            "macro": "{$NETSIM.SPEC.LLD.INTERVAL}",
            "value": "1m",
            "description": "Fallback interval of the spec discovery (zabbix-sync runs it right after a change).",
        },
        {
            "macro": "{$NETSIM.SNMP.UNAVAILABLE}",
            "value": "30s",
            "description": "SNMP unreachable this long -> problem.",
        },
    ]
    macros.sort(key=lambda m: m["macro"])
    items = [
        walk_item(o),
        availability_item(),
        trap_item(o, "pw", "trap-cpwVcDown", "cpwVcDown", "SNMP trap: cpwVcDown (PW down)"),
        trap_item(o, "pw", "trap-cpwVcUp", "cpwVcUp", "SNMP trap: cpwVcUp (PW up)"),
        trap_item(o, "link", "trap-linkDown", "linkDown", "SNMP trap: linkDown (interface left up)"),
        trap_item(o, "link", "trap-linkUp", "linkUp", "SNMP trap: linkUp (interface up)"),
        trap_item(o, "config", "trap-ccm", "ciscoConfigManEvent", "SNMP trap: ciscoConfigManEvent (config change)"),
        trap_item(o, "boot", "trap-coldStart", "coldStart", "SNMP trap: coldStart (device restart)"),
        trap_item(o, "", "trap-fallback", None, "SNMP trap: unmatched (fallback)"),
    ]
    return canonicalize(
        {
            "zabbix_export": {
                "version": EXPORT_VERSION,
                "template_groups": [{"uuid": zbx_uuid("template_group", N.TEMPLATE_GROUP), "name": N.TEMPLATE_GROUP}],
                "templates": [
                    {
                        "uuid": zbx_uuid("template", T),
                        "template": T,
                        "name": T,
                        "description": (
                            "NetSim L2VPN PE (Clixon + simd + snmpsim) monitored by SNMP v2c and traps, against its "
                            "NetBox spec. Generated by `zabbix-template` from contract/oids.yaml "
                            "(zabbix/template/netsim_l2vpn_pe_snmp.yaml); do not edit in the UI.\n"
                            "Traps are hints, polling is truth: a trap runs the walk now, the checks only use polled "
                            "values."
                        ),
                        "groups": [{"name": N.TEMPLATE_GROUP}],
                        "items": items,
                        "discovery_rules": [spec_lld(o), observed_lld(o), interfaces_lld(o)],
                        "tags": tags(**{"class": "network", "target": "netsim-pe"}),
                        "macros": macros,
                        "valuemaps": valuemaps(o),
                    }
                ],
            }
        }
    )


# ------------------------------------------------------------------------------------------------ YAML
class _Dumper(yaml.SafeDumper):
    """Zabbix export style: lists indented under their key, multi-line strings as literal blocks."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str)


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)


def render(reg: OidRegistry | None = None) -> str:
    header = (
        "# GENERATED by `zabbix-template` (netsim_zabbix.template) from contract/oids.yaml - DO NOT EDIT.\n"
        "# Owner: 04-zabbix. Imported by `make zabbix-provision` (configuration.import, Zabbix 8.0 format).\n"
    )
    return header + dump_yaml(build_template(reg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zabbix-template", description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed YAML is stale")
    parser.add_argument("--out", type=Path, default=None, help="output file (default: the committed template)")
    args = parser.parse_args(argv)
    path = args.out or template_path()
    text = render()
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            print(f"STALE: {path} differs from the generator; run `make zabbix-template`", file=sys.stderr)
            return 1
        print(f"OK: {path} is up to date")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
