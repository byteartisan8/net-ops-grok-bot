"""Template generator: committed YAML is current, canonical, contract-conformant (sections 8, 9.1, 9.2)."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
import yaml
from netsim_zabbix import names as N
from netsim_zabbix import template as T
from netsim_zabbix.canonical import canonicalize

from netsim_common import load_oids


@pytest.fixture(scope="module")
def tpl() -> dict[str, Any]:
    return T.build_template()["zabbix_export"]["templates"][0]


def walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def rule(tpl: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in tpl["discovery_rules"] if r["key"] == key)


def test_committed_yaml_is_current() -> None:
    assert T.template_path().read_text(encoding="utf-8") == T.render(), "run `make zabbix-template`"


def test_build_is_deterministic_and_in_export_order() -> None:
    built = T.build_template()
    assert built == T.build_template()
    assert built == canonicalize(built)
    assert yaml.safe_load(T.render()) == built


def test_uuids_are_unique_uuid4() -> None:
    ids = [n["uuid"] for n in walk(T.build_template()) if "uuid" in n]
    assert len(ids) == len(set(ids)) > 40
    for i in ids:
        assert re.fullmatch(r"[0-9a-f]{32}", i)
        assert uuid.UUID(i).version == 4 and uuid.UUID(i).variant == uuid.RFC_4122


def test_no_placeholder_left_in_scripts() -> None:
    assert not re.search(r"__[A-Z_]+__", T.render())


def test_walk_master_covers_every_column_the_checks_read(tpl: dict[str, Any]) -> None:
    oids = load_oids()
    [master] = [i for i in tpl["items"] if i["key"] == N.WALK_KEY]
    assert master["type"] == "SNMP_AGENT" and master["delay"] == "{$NETSIM.WALK.INTERVAL}"
    walked = master["snmp_oid"].removeprefix("walk[").removesuffix("]").split(",")
    for obj in (
        "cpwVcPeerAddr",
        "cpwVcID",
        "cpwVcRemoteIfMtu",
        "cpwVcName",
        "cpwVcDescr",
        "cpwVcAdminStatus",
        "cpwVcOperStatus",
        "ifName",
        "ifDescr",
        "ifAlias",
        "ifAdminStatus",
        "ifOperStatus",
        "ifMtu",
    ):
        assert oids.oid(obj) in walked, obj
    macros = {m["macro"]: m["value"] for m in tpl["macros"]}
    assert macros["{$NETSIM.WALK.INTERVAL}"] == "60s"  # contract 9.1


def test_every_other_item_is_dependent_or_trap(tpl: dict[str, Any]) -> None:
    for item in tpl["items"]:
        assert item["type"] in ("SNMP_AGENT", "SNMP_TRAP", "INTERNAL"), item["key"]
    snmp_agent = [i for i in tpl["items"] if i["type"] == "SNMP_AGENT"]
    assert [i["key"] for i in snmp_agent] == [N.WALK_KEY]
    for r in tpl["discovery_rules"]:
        for proto in r["item_prototypes"]:
            assert proto["type"] == "DEPENDENT", proto["key"]


@pytest.mark.parametrize(
    ("notification", "kind"),
    [("cpwVcDown", "pw"), ("cpwVcUp", "pw"), ("ciscoConfigManEvent", "config"), ("coldStart", "boot")],
)
def test_trap_items_match_their_numeric_oid_only(tpl: dict[str, Any], notification: str, kind: str) -> None:
    oids = load_oids()
    trap_oid = oids.notification(notification).oid
    # as zabbix-snmptraps writes it (snmptrapd -On, varbinds "oid = value", see docs/reports/04-zabbix.md)
    text = f".1.3.6.1.2.1.1.3.0 = 12345\n.1.3.6.1.6.3.1.1.4.1.0 = .{trap_oid}\n.1.3.6.1.4.1.9.10.106.1.2.1.26.1 = down"
    matching = []
    for item in tpl["items"]:
        m = re.fullmatch(r'snmptrap\["(.*)"\]', item["key"])
        if m and re.search(m.group(1), text):
            matching.append(item)
    assert len(matching) == 1, [i["key"] for i in matching]
    [item] = matching
    assert {"tag": N.TRAP_TAG, "value": kind} in item["tags"]
    [trigger] = item["triggers"]
    assert trigger["type"] == "MULTIPLE" and trigger["priority"] == "INFO"
    assert {"tag": N.TRAP_TAG, "value": kind} in trigger["tags"]
    # one event per trap: no time-based function (the timer would re-evaluate and, in MULTIPLE mode, add events);
    # the check-now action closes the event
    assert trigger["expression"] == f"length(last(/{N.TEMPLATE}/{item['key']}))>0"
    assert (trigger["recovery_mode"], trigger["manual_close"]) == ("NONE", "YES")
    assert "nodata(" not in trigger["expression"]


def test_trap_regex_does_not_match_longer_oids(tpl: dict[str, Any]) -> None:
    keys = [i["key"] for i in tpl["items"] if i["key"].startswith("snmptrap[")]
    text = ".1.3.6.1.6.3.1.1.4.1.0 = .1.3.6.1.4.1.9.10.106.2.10\n"
    assert not any(re.search(re.fullmatch(r'snmptrap\["(.*)"\]', k).group(1), text) for k in keys)


def _trigger_prototypes(r: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = list(r.get("trigger_prototypes", []))
    for proto in r["item_prototypes"]:
        found += proto.get("trigger_prototypes", [])
    return {
        next(t["value"] for t in tp["tags"] if t["tag"] == "check"): tp
        for tp in found
        if any(t["tag"] == "check" for t in tp["tags"])
    }


def test_intent_checks_follow_contract_9_2(tpl: dict[str, Any]) -> None:
    spec_rule = rule(tpl, N.SPEC_LLD_KEY)
    assert spec_rule["type"] == "SCRIPT" and spec_rule["lifetime_type"] == "DELETE_IMMEDIATELY"
    assert [p["value"] for p in spec_rule["parameters"]] == N.SPEC_MACROS
    checks = _trigger_prototypes(spec_rule)
    assert {c: t["priority"] for c, t in checks.items()} == {"missing": "HIGH", "oper": "HIGH", "spec": "AVERAGE"}
    for check, trig in checks.items():
        tags = {t["tag"]: t["value"] for t in trig["tags"]}
        assert tags == {
            "scope": "l2vpn-pw",
            "service": "{#SERVICE}",
            "pw_role": "{#ROLE}",
            "customer": "{#CUSTOMER}",
            "host": "{HOST.HOST}",
            "check": check,
            "vcid": "{#VC_ID}",
            "peer": "{#EXPECTED_PEER}",
        }
    missing = checks["missing"]
    assert missing["expression"].endswith(f"/{N.pw_key('present')})=0")
    for dependent in ("oper", "spec"):
        assert checks[dependent]["dependencies"] == [{"name": missing["name"], "expression": missing["expression"]}]
        assert checks[dependent]["expression"].startswith(f"last(/{N.TEMPLATE}/{N.pw_key('present')})=1 and")
    assert '<>"{#EXPECTED_PEER}"' in checks["spec"]["expression"] and "<>{#VC_ID}" in checks["spec"]["expression"]
    assert "NetBox expects" in checks["missing"]["event_name"]
    assert checks["spec"]["event_name"].endswith(f"{{?last(/{{HOST.HOST}}/{N.pw_key('spec_diff')})}}")


def test_intent_items_hang_off_the_walk(tpl: dict[str, Any]) -> None:
    protos = {p["key"]: p for p in rule(tpl, N.SPEC_LLD_KEY)["item_prototypes"]}
    row = protos[N.pw_key("row")]
    assert row["master_item"] == {"key": N.WALK_KEY} and row["value_type"] == "JSON"
    assert "'{#SERVICE}:{#ROLE}'" in row["preprocessing"][0]["parameters"][0]
    for field in N.PW_FIELDS:
        assert protos[N.pw_key(field)]["master_item"] == {"key": N.pw_key("row")}, field


def test_observed_view_and_unexpected(tpl: dict[str, Any]) -> None:
    obs = rule(tpl, N.OBSERVED_LLD_KEY)
    assert obs["type"] == "DEPENDENT" and obs["master_item"] == {"key": N.WALK_KEY}
    macros = obs["preprocessing"][0]["parameters"][0::3]
    assert macros == ["{#VCNAME}", "{#VCID}", "{#PEER}", "{#CUSTOMER}"]
    [unexpected] = _trigger_prototypes(obs).values()
    tags = {t["tag"]: t["value"] for t in unexpected["tags"]}
    assert unexpected["priority"] == "WARNING" and tags["scope"] == "l2vpn-unexpected" and tags["check"] == "unexpected"
    # same service/pw_role tags as the PW checks, so problems of one moved PW correlate (05-integration)
    assert (tags["service"], tags["pw_role"]) == ("{#SERVICE}", "{#ROLE}")
    expected_item = next(p for p in obs["item_prototypes"] if p["key"].startswith("netsim.pw.observed.expected["))
    script = expected_item["preprocessing"][1]["parameters"][0]
    assert all(m in script for m in N.SPEC_MACROS)


def test_spec_macro_defaults_and_overflow(tpl: dict[str, Any]) -> None:
    macros = {m["macro"]: m["value"] for m in tpl["macros"]}
    for m in N.SPEC_MACROS:
        assert macros[m] == "[]"
    assert len(N.SPEC_MACROS) == N.SPEC_CHUNKS == 8


def test_availability_trigger(tpl: dict[str, Any]) -> None:
    [item] = [i for i in tpl["items"] if i["key"] == N.SNMP_AVAILABLE_KEY]
    [trig] = item["triggers"]
    assert trig["priority"] == "HIGH" and {"tag": "scope", "value": "availability"} in trig["tags"]


def test_zbx_uuid_is_stable() -> None:
    assert T.zbx_uuid("item", "x") == T.zbx_uuid("item", "x") != T.zbx_uuid("item", "y")
