"""devices/compose.yaml matches the topology; image inputs (pins, vendored YANG, supervisor) are consistent."""

from __future__ import annotations

import hashlib
import re

import yaml
from netsim_simd import __version__
from netsim_simd.devtool import IMAGE, KEYSPACE_EVENTS, RenderOptions, render_compose

from netsim_common.constants import lab_constants
from netsim_common.paths import repo_root

DEVICES = repo_root() / "devices"


def test_compose_file_is_generated_from_topology():
    assert (DEVICES / "compose.yaml").read_text(encoding="utf-8") == render_compose(), "run `make devices-compose`"


def test_compose_services_match_topology(topology):
    doc = yaml.safe_load((DEVICES / "compose.yaml").read_text(encoding="utf-8"))
    lab = lab_constants(topology)
    services = doc["services"]
    assert list(services) == ["core", *topology.hostnames]
    assert doc["networks"] == {"labnet": {"external": True}}
    core = services["core"]
    assert core["networks"]["labnet"]["ipv4_address"] == lab.core_ip
    assert "ports" not in core  # the bus is not published
    assert core["command"][-1] == KEYSPACE_EVENTS.replace("$", "$$")
    for d in topology.devices:
        svc = services[d.hostname]
        assert svc["image"] == IMAGE == f"netsim/netsim-pe:{__version__}"
        assert svc["container_name"] == svc["hostname"] == d.hostname  # `docker stop hub-a` works
        assert svc["networks"]["labnet"]["ipv4_address"] == d.mgmt_ip
        env = svc["environment"]
        assert (env["HOSTNAME"], env["MGMT_IP"], env["SITE"]) == (d.hostname, d.mgmt_ip, d.site)
        assert env["CORE_URL"] == f"${{CORE_URL:-{lab.core_url}}}"
        assert env["TRAP_TARGETS"].startswith(f"${{TRAP_TARGETS-{lab.trap_receiver_ip}:")
        ep = lab.devices[d.hostname]
        assert svc["ports"] == [
            f"{ep.restconf_url.removeprefix('http://')}:8080/tcp",
            f"{ep.simd_url.removeprefix('http://')}:9000/tcp",
            f"{ep.snmp[0]}:{ep.snmp[1]}:161/udp",
        ]
        assert all(p.startswith("127.0.0.1:") for p in svc["ports"])
        assert svc["volumes"] == [f"{d.hostname}-data:/var/lib/netsim", "controller-keys:/etc/netsim/keys:ro"]
        assert svc["depends_on"] == {"core": {"condition": "service_healthy"}}
    assert set(doc["volumes"]) == {f"{h}-data" for h in topology.hostnames} | {"controller-keys"}
    assert all("830" not in port for svc in services.values() for port in svc.get("ports", []))  # NETCONF: labnet only


def test_isolated_stack_rendering(topology):
    text = render_compose(
        topology,
        RenderOptions(
            hosts=("hub-a", "spoke-1"),
            network="it",
            external_network=False,
            subnet="172.30.99.0/24",
            container_prefix="it-",
            publish="ephemeral",
            trapsink=True,
            trap_targets="172.30.99.42:1162",
        ),
    )
    doc = yaml.safe_load(text)
    assert list(doc["services"]) == ["core", "hub-a", "spoke-1", "trapsink"]
    assert doc["networks"] == {"it": {"ipam": {"config": [{"subnet": "172.30.99.0/24"}]}}}
    spoke = doc["services"]["spoke-1"]
    assert spoke["environment"]["MGMT_IP"] == "172.30.99.21"
    assert spoke["environment"]["CORE_URL"] == "redis://172.30.99.5:6379/0"
    assert spoke["ports"] == ["127.0.0.1::8080/tcp", "127.0.0.1::9000/tcp", "127.0.0.1::161/udp"]
    assert doc["services"]["trapsink"]["networks"]["it"]["ipv4_address"] == "172.30.99.42"


def test_clixon_pins_match_the_foundation_check_image():
    """The device image builds the same Clixon/CLIgen commits that 01-foundation validated the YANG with."""
    ours = (DEVICES / "Dockerfile").read_text(encoding="utf-8")
    theirs = (repo_root() / "tools" / "docker" / "clixon-check.Dockerfile").read_text(encoding="utf-8")
    for arg in ("CLIGEN_VERSION", "CLIGEN_COMMIT", "CLIXON_VERSION", "CLIXON_COMMIT"):
        pattern = rf"^ARG {arg}=(\S+)$"
        assert re.findall(pattern, ours, re.M)[:1] == re.findall(pattern, theirs, re.M), arg


def test_vendored_yang_hash():
    path = DEVICES / "clixon" / "yang" / "ietf-netconf-notifications@2012-02-06.yang"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest in (DEVICES / "clixon" / "yang" / "SOURCES.md").read_text(encoding="utf-8")


def test_supervisor_runs_the_four_processes():
    conf = (DEVICES / "supervisord.conf").read_text(encoding="utf-8")
    assert re.findall(r"^\[program:([a-z_]+)\]", conf, re.M) == [
        "clixon_backend",
        "clixon_restconf",
        "sshd",
        "snmpsim",
        "simd",
    ]
    assert "--variation-module-options=netsim:db:/run/netsim/snmp/agent.db" in conf


def test_dockerignore_keeps_what_the_dockerfile_copies():
    ignore = (DEVICES / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    allowed = [line[1:].rstrip("/") for line in ignore if line.startswith("!")]
    copies = re.findall(r"^COPY (?!--from)(?:--chmod=\S+ )?(.+) \S+$", (DEVICES / "Dockerfile").read_text(), re.M)
    for sources in copies:
        for src in sources.split():
            assert any(src.rstrip("/") == a or src.startswith(a + "/") for a in allowed), src
