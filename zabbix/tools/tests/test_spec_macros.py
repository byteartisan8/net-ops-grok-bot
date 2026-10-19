"""{$NETSIM.PW.SPEC} values (contract 9.2) and the overflow chunks for hosts beyond one 2048-character macro."""

from __future__ import annotations

import json

import pytest
from netsim_testlib import load_tool
from netsim_zabbix import names as N
from netsim_zabbix.spec_macros import SpecTooLarge, host_spec_macros, spec_chunks

from netsim_common import bsm_spec_from_topology, load_topology, pw_spec_for_host, pw_spec_macro_value


def test_every_lab_host_fits_one_macro_and_equals_the_contract_value() -> None:
    spec = bsm_spec_from_topology(load_topology())
    for host in spec.hosts():
        value = pw_spec_macro_value(spec, host)
        assert host_spec_macros(spec, host) == {N.SPEC_MACRO: value}
        assert len(value) <= N.SPEC_MACRO_MAX_LEN
    hub = host_spec_macros(spec, "hub-a")[N.SPEC_MACRO]
    assert len(json.loads(hub)) == 6 and 900 < len(hub) < 1100  # "a hub carries 6 entries (about 1 KB)"


def test_host_without_pws_gets_an_empty_list() -> None:
    spec = bsm_spec_from_topology(load_topology())
    assert host_spec_macros(spec, "no-such-host") == {N.SPEC_MACRO: "[]"}


@pytest.fixture(scope="module")
def big_spec():
    gen = load_tool("gen_topology")
    topo = gen.extend(load_topology(), spokes=20, services=60, customers=8)
    return bsm_spec_from_topology(topo)


def test_a_hub_with_60_pws_spills_into_overflow_chunks(big_spec) -> None:
    entries = pw_spec_for_host(big_spec, "hub-a")
    assert len(entries) == 60
    macros = host_spec_macros(big_spec, "hub-a")
    assert list(macros) == N.SPEC_MACROS[: len(macros)] and len(macros) > 1
    rebuilt = [e for chunk in macros.values() for e in json.loads(chunk)]
    assert rebuilt == json.loads(pw_spec_macro_value(big_spec, "hub-a"))
    assert all(len(chunk) <= N.SPEC_MACRO_MAX_LEN for chunk in macros.values())
    assert all(chunk.startswith("[{") and "," not in chunk[-2:] for chunk in macros.values())


def test_too_large_for_eight_chunks_is_refused(big_spec) -> None:
    entries = pw_spec_for_host(big_spec, "hub-a")
    with pytest.raises(SpecTooLarge):
        spec_chunks(entries, limit=400)


def test_single_entry_larger_than_a_macro_is_refused(big_spec) -> None:
    entries = pw_spec_for_host(big_spec, "hub-a")[:1]
    with pytest.raises(SpecTooLarge):
        spec_chunks(entries, limit=50)
