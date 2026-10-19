"""contract/mibs: every pinned file matches the sha256 recorded in SOURCES.md."""

from __future__ import annotations

from netsim_testlib import CONTRACT, load_tool


def test_mib_files_match_sources_md():
    fetch = load_tool("fetch_mibs")
    assert fetch.check() == []


def test_sources_md_records_url_ref_and_sha256_per_file():
    fetch = load_tool("fetch_mibs")
    text = (CONTRACT / "mibs" / "SOURCES.md").read_text(encoding="utf-8")
    rows = fetch.parse_sources_md(text)
    assert len(rows) == len(fetch.MODULES) == 13
    for module in fetch.MODULES:
        assert fetch.url_for(module) in text
        up = fetch.MODULES[module]
        assert up.commit in text and up.tag in text


def test_required_modules_are_pinned():
    fetch = load_tool("fetch_mibs")
    required = {
        "SNMPv2-MIB", "IF-MIB", "INET-ADDRESS-MIB", "CISCO-IETF-PW-MIB", "CISCO-IETF-PW-TC-MIB",
        "CISCO-CONFIG-MAN-MIB", "CISCO-SMI", "CISCO-TC", "IANAifType-MIB",
    }  # fmt: skip
    assert required <= set(fetch.MODULES)
