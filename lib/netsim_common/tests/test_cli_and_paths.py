from __future__ import annotations

import json

import pytest

from netsim_common import canonical_json, contract_dir, lab_constants, load_topology, render_device_config
from netsim_common.cli import main
from netsim_common.paths import ENV_CONTRACT_DIR, ContractNotFoundError


def test_cli_golden_and_canonical(capsys, tmp_path):
    assert main(["golden", "spoke-1"]) == 0
    golden = capsys.readouterr().out
    assert golden == (contract_dir() / "golden" / "spoke-1.json").read_text(encoding="utf-8")
    shuffled = json.loads(golden)
    shuffled["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"].reverse()
    path = tmp_path / "running.json"
    path.write_text(json.dumps(shuffled), encoding="utf-8")
    assert main(["canonical", str(path)]) == 0
    assert capsys.readouterr().out == golden


def test_cli_diff_exit_codes(capsys, tmp_path):
    golden = contract_dir() / "golden" / "spoke-1.json"
    running = json.loads(golden.read_text(encoding="utf-8"))
    running["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"][1]["vc-id"] = 2010
    path = tmp_path / "running.json"
    path.write_text(json.dumps(running), encoding="utf-8")
    assert main(["diff", str(golden), str(golden)]) == 0
    assert "no differences" in capsys.readouterr().out
    assert main(["diff", str(golden), str(path), "--json"]) == 1
    (diff,) = json.loads(capsys.readouterr().out)
    assert diff["path"] == "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id"
    assert (diff["expected"], diff["actual"]) == (2001, 2010)


def test_cli_lookups(capsys):
    assert main(["oid", "cpwVcOperStatus", "4"]) == 0
    assert capsys.readouterr().out.strip() == "1.3.6.1.4.1.9.10.106.1.2.1.26.4"
    assert main(["pw-spec", "spoke-1"]) == 0
    assert [e["role"] for e in json.loads(capsys.readouterr().out)] == ["primary", "backup"]
    assert main(["hosts", "--json"]) == 0
    hosts = {h["host"]: h for h in json.loads(capsys.readouterr().out)}
    assert hosts["spoke-1"]["restconf"] == "http://127.0.0.1:18021"
    assert main(["golden", "nope"]) == 2


def test_lab_constants():
    lab = lab_constants()
    assert lab.core_url == "redis://172.30.10.5:6379/0"
    assert (lab.netbox_url, lab.zabbix_url, lab.lab_api_url) == (
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8090",
    )
    assert lab.lab_api_url_labnet == "http://172.30.10.50:8090"
    assert lab.trap_receiver_ip == "172.30.10.42"
    hub_a = lab.devices["hub-a"]
    assert (hub_a.restconf_url, hub_a.simd_url, hub_a.snmp) == (
        "http://127.0.0.1:18011",
        "http://127.0.0.1:19011",
        ("127.0.0.1", 16111),
    )
    assert (hub_a.restconf_url_labnet, hub_a.snmp_labnet) == ("http://172.30.10.11:8080", ("172.30.10.11", 161))


def test_contract_dir_env_override(monkeypatch, tmp_path):
    real = contract_dir()
    monkeypatch.setenv(ENV_CONTRACT_DIR, str(real))
    assert contract_dir() == real
    monkeypatch.setenv(ENV_CONTRACT_DIR, str(tmp_path))
    with pytest.raises(ContractNotFoundError):
        contract_dir()


def test_render_matches_cli_output():
    assert canonical_json(render_device_config(load_topology(), "hub-b")) == (
        contract_dir() / "golden" / "hub-b.json"
    ).read_text(encoding="utf-8")
