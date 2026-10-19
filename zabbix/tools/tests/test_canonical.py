"""PHP strnatcasecmp port and the exporter's list order (ground truth: Zabbix 8.0.0rc1's own export of the template)."""

from __future__ import annotations

import functools

from netsim_zabbix.canonical import canonicalize, php_sort, sort_macros, strnatcasecmp


def natcasesort(values: list[str]) -> list[str]:
    return sorted(values, key=functools.cmp_to_key(strnatcasecmp))


def test_php_manual_natcasesort_example() -> None:
    # php.net natcasesort() example
    files = ["IMG0.png", "img12.png", "img10.png", "img2.png", "img1.png", "IMG3.png"]
    assert natcasesort(files) == ["IMG0.png", "img1.png", "img2.png", "IMG3.png", "img10.png", "img12.png"]


def test_basic_cases() -> None:
    assert strnatcasecmp("a2", "a10") == -1
    assert strnatcasecmp("A10", "a10") == 0
    assert strnatcasecmp("x", "x.1") == -1
    assert strnatcasecmp("", "") == 0 and strnatcasecmp("", "a") == -1
    assert strnatcasecmp("v1.02", "v1.1") == -1  # leading zero: fractional comparison


def test_item_key_order_as_zabbix_exports_it() -> None:
    exported = [
        "netsim.snmp.walk",
        "snmptrap.fallback",
        r'snmptrap["\.1\.3\.6\.1\.6\.3\.1\.1\.4\.1\.0 = \.1\.3\.6\.1\.4\.1\.9\.9\.43\.2\.0\.1(\s|$)"]',
        r'snmptrap["\.1\.3\.6\.1\.6\.3\.1\.1\.4\.1\.0 = \.1\.3\.6\.1\.4\.1\.9\.10\.106\.2\.1(\s|$)"]',
        r'snmptrap["\.1\.3\.6\.1\.6\.3\.1\.1\.4\.1\.0 = \.1\.3\.6\.1\.4\.1\.9\.10\.106\.2\.2(\s|$)"]',
        r'snmptrap["\.1\.3\.6\.1\.6\.3\.1\.1\.4\.1\.0 = \.1\.3\.6\.1\.6\.3\.1\.1\.5\.1(\s|$)"]',
        "zabbix[host,snmp,available]",
    ]
    rows = [{"key": k} for k in reversed(exported)]
    assert [r["key"] for r in php_sort(rows, "key")] == exported


def test_lld_macro_paths_fold_to_upper_case() -> None:
    # 'N' (0x4E) < '_' (0x5F) < '}' (0x7D): the order Zabbix exported
    macros = ["{#VC_ID}", "{#EXPECTED_PEER}", "{#VCNAME}", "{#EXPECTED_PEER_HOST}", "{#AC}"]
    rows = php_sort([{"lld_macro": m} for m in macros], "lld_macro")
    assert [r["lld_macro"] for r in rows] == [
        "{#AC}",
        "{#EXPECTED_PEER_HOST}",
        "{#EXPECTED_PEER}",
        "{#VCNAME}",
        "{#VC_ID}",
    ]


def test_macros_sort_by_inner_name() -> None:
    rows = sort_macros([{"macro": m} for m in ["{$NETSIM.PW.SPEC.2}", "{$NETSIM.SNMP.X}", "{$NETSIM.PW.SPEC}"]])
    assert [r["macro"] for r in rows] == ["{$NETSIM.PW.SPEC}", "{$NETSIM.PW.SPEC.2}", "{$NETSIM.SNMP.X}"]


def test_canonicalize_sorts_known_lists_only() -> None:
    data = {
        "tags": [{"tag": "b", "value": "1"}, {"tag": "a", "value": "2"}],
        "preprocessing": [{"type": "Z"}, {"type": "A"}],
        "triggers": [{"name": "y", "expression": "1"}, {"name": "x", "expression": "2"}],
    }
    out = canonicalize(data)
    assert [t["tag"] for t in out["tags"]] == ["a", "b"]
    assert [p["type"] for p in out["preprocessing"]] == ["Z", "A"]  # order matters: never sorted
    assert [t["name"] for t in out["triggers"]] == ["x", "y"]
