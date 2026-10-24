"""Repo scaffolding: compose aggregation, Makefile, .env template and workspace agree with the topology."""

from __future__ import annotations

import re
import subprocess
import tomllib

import pytest
import yaml
from netsim_testlib import REPO, docker_available, load_tool

from netsim_common import lab_constants, load_topology

FRAGMENTS = {
    "devices": "02-device-simulator",
    "netbox": "03-netbox",
    "zabbix": "04-zabbix",
    "lab": "05-integration",
    "bot": "07-grok-bot",
    "controller": "10-clixon-controller",
}
CONTRACT_ENV = ["NETBOX_TOKEN_RW", "NETBOX_TOKEN_RO", "ZABBIX_API_TOKEN", "LAB_API_TOKEN"]


def _env_example() -> dict[str, str]:
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_root_compose_only_includes_the_fragments():
    root = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    assert set(root) == {"name", "include"}
    assert root["name"] == "netsim"
    assert root["include"] == [f"{d}/compose.yaml" for d in FRAGMENTS]


@pytest.mark.parametrize("component", FRAGMENTS)
def test_fragments_attach_to_external_labnet(component):
    fragment = yaml.safe_load((REPO / component / "compose.yaml").read_text(encoding="utf-8"))
    assert fragment["networks"]["labnet"]["external"] is True


@pytest.mark.parametrize(("component", "owner"), FRAGMENTS.items())
def test_placeholder_readmes_state_the_owner(component, owner):
    assert f"owner: session {owner}" in (REPO / component / "README.md").read_text(encoding="utf-8")


def test_makefile_network_matches_topology():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    values = dict(re.findall(r"^(LABNET_\w+)\s*:=\s*(\S+)$", makefile, re.MULTILINE))
    net = load_topology().network
    assert values == {"LABNET_NAME": net.name, "LABNET_SUBNET": net.subnet, "LABNET_GATEWAY": net.gateway}


def test_makefile_has_required_targets():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.MULTILINE))
    required = {"net", "up", "down", "ps", "logs", "build", "seed", "reset", "test", "e2e", "lint", "clean", "env"}
    assert required <= targets
    for prefix in FRAGMENTS:
        assert re.search(rf"^{prefix}-%:", makefile, re.MULTILINE), f"placeholder for {prefix}-*"


def test_env_example_covers_contract_and_matches_topology():
    env = _env_example()
    assert set(CONTRACT_ENV) <= set(env)
    lab = lab_constants()
    assert env["CORE_URL"] == lab.core_url
    assert env["SNMP_COMMUNITY"] == lab.snmp_community
    assert env["TRAP_TARGETS"].split(":")[0] == lab.trap_receiver_ip
    assert env["COMPOSE_PROJECT_NAME"] == yaml.safe_load((REPO / "compose.yaml").read_text())["name"]


def test_gen_env_creates_then_only_appends(tmp_path):
    gen = load_tool("gen_env")
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("# c\nA=__generate__\nB=fixed\nNETBOX_SECRET_KEY=__generate__\nTOKEN=\n", encoding="utf-8")
    gen.main(example, target)
    first = dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line)
    assert first["B"] == "fixed" and first["TOKEN"] == ""
    assert first["A"] != "__generate__" and len(first["A"]) >= 32
    assert len(first["NETBOX_SECRET_KEY"]) >= 50
    assert target.stat().st_mode & 0o077 == 0
    gen.main(example, target)
    assert dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line) == first
    example.write_text(example.read_text() + "NEW=__generate__\n", encoding="utf-8")
    gen.main(example, target)
    third = dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line)
    assert {k: third[k] for k in first} == first and third["NEW"] != "__generate__"


def test_workspace_members_are_declared_and_installed():
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    members = root["tool"]["uv"]["workspace"]["members"]
    assert members == ["lib/netsim_common", "devices/simd", "netbox/tools", "zabbix/tools", "lab", "bot"]
    names = []
    for member in members:
        project = tomllib.loads((REPO / member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        names.append(project["name"])
        assert project["requires-python"] == ">=3.12"
    assert sorted(root["project"]["dependencies"]) == sorted(names)
    assert all(root["tool"]["uv"]["sources"][n] == {"workspace": True} for n in names)
    assert root["project"]["requires-python"] == "==3.12.*"


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="needs a Docker daemon")
def test_docker_compose_config_passes():
    result = subprocess.run(["docker", "compose", "config", "-q"], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
