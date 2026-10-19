"""netbox/compose.yaml, netbox/netbox.mk and the NetBox part of .env.example agree with the contract."""

from __future__ import annotations

import re

import yaml
from netsim_testlib import REPO, load_tool

from netsim_common import load_topology

COMPOSE = yaml.safe_load((REPO / "netbox" / "compose.yaml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
NETBOX_IMAGE = "netboxcommunity/netbox:v4.7.3-5.1.1"


def _env_example() -> dict[str, str]:
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_images_are_pinned():
    assert SERVICES["netbox"]["image"] == SERVICES["netbox-worker"]["image"] == NETBOX_IMAGE
    assert re.fullmatch(r"postgres:16\.\d+-alpine\d+\.\d+", SERVICES["netbox-postgres"]["image"])
    devices = yaml.safe_load((REPO / "devices" / "compose.yaml").read_text(encoding="utf-8"))
    core_image = devices["services"]["core"]["image"]  # one pinned Valkey for the whole lab
    assert SERVICES["netbox-redis"]["image"] == SERVICES["netbox-redis-cache"]["image"] == core_image


def test_addresses_and_ports_match_the_topology():
    topo = load_topology()
    netbox = topo.infra("netbox")
    assert SERVICES["netbox"]["networks"]["labnet"]["ipv4_address"] == netbox.mgmt_ip
    assert SERVICES["netbox"]["ports"] == [f"{topo.host_bind}:{netbox.host_port}:8080/tcp"]
    worker_ip = SERVICES["netbox-worker"]["networks"]["labnet"]["ipv4_address"]
    used = {d.mgmt_ip for d in topo.devices} | {h.mgmt_ip for h in topo.infrastructure}
    assert worker_ip == "172.30.10.31" and worker_ip not in used
    for name in ("netbox-postgres", "netbox-redis", "netbox-redis-cache"):
        assert SERVICES[name]["networks"] == ["netbox-internal"], name
    assert COMPOSE["networks"]["netbox-internal"]["internal"] is True
    assert COMPOSE["networks"]["labnet"]["external"] is True


def test_every_service_has_a_healthcheck_and_restarts():
    for name, service in SERVICES.items():
        assert "test" in service["healthcheck"], name
        assert service["restart"] == "unless-stopped", name


def test_compose_variables_come_from_env_example():
    text = (REPO / "netbox" / "compose.yaml").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{([A-Z0-9_]+)\}", text))
    assert used and used <= set(_env_example())


def test_env_example_generates_a_long_enough_pepper(tmp_path):
    gen = load_tool("gen_env")
    target = tmp_path / ".env"
    gen.main(REPO / ".env.example", target)
    values = dict(line.split("=", 1) for line in target.read_text().splitlines() if line and not line.startswith("#"))
    assert len(values["NETBOX_API_TOKEN_PEPPER"]) >= 50  # NetBox refuses to start below 50 characters
    assert len(values["NETBOX_WEBHOOK_SECRET"]) >= 32
    assert values["NETBOX_TOKEN_RW"] == values["NETBOX_TOKEN_RO"] == ""  # written by netbox-bootstrap


def test_make_targets():
    mk = (REPO / "netbox" / "netbox.mk").read_text(encoding="utf-8")
    targets = set(re.findall(r"^(netbox-[a-z-]+):", mk, re.MULTILINE))
    assert {"netbox-up", "netbox-down", "netbox-bootstrap", "netbox-seed", "netbox-test"} <= targets
