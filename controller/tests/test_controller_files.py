"""The controller component's files (no containers): the startup datastore is generated from the topology, NACM gives
the roles reads only, the pins match the PEs' Clixon, SSH lets in only what the contract says, the compose fragment
publishes nothing, and the agent's and healthcheck's helpers parse what the controller prints."""

from __future__ import annotations

import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from netsim_common.paths import repo_root
from netsim_common.topology import load_topology

REPO = repo_root()
HERE = REPO / "controller"
NS = {"c": "http://clicon.org/controller", "n": "urn:ietf:params:xml:ns:yang:ietf-netconf-acm"}
ROLES = ("noc-engineer", "network-engineer", "noc-manager")
NEVER_FOR_ROLES = (
    "edit-config",
    "commit",
    "config-pull",
    "controller-commit",
    "connection-change",
    "device-rpc",
    "device-template-apply",
    "copy-config",
    "delete-config",
    "kill-session",
    "datastore-diff",
)


@cache
def module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"netsim_controller_{name}", HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def startup() -> ET.Element:
    return ET.fromstring((HERE / "startup_db").read_text(encoding="utf-8"))


def ssh_config(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.strip().partition(" ")
            out.setdefault(key, []).append(value.strip())
    return out


# ------------------------------------------------------------------------------------------------ startup_db
def test_startup_db_is_generated_from_the_topology() -> None:
    assert (HERE / "startup_db").read_text(encoding="utf-8") == module("startup").render_startup(), (
        "run `make controller-config`"
    )


def test_every_pe_is_a_device_at_its_mgmt_ip_through_the_profile(startup: ET.Element) -> None:
    topo = load_topology()
    devices = startup.find("c:devices", NS)
    found = {d.findtext("c:name", namespaces=NS): d for d in devices.findall("c:device", NS)}
    assert list(found) == [d.hostname for d in topo.devices]
    for d in topo.devices:
        dev = found[d.hostname]
        assert dev.findtext("c:addr", namespaces=NS) == d.mgmt_ip
        assert dev.findtext("c:enabled", namespaces=NS) == "true"
        assert dev.findtext("c:device-profile", namespaces=NS) == "netsim-pe"
    profile = devices.find("c:device-profile", NS)
    keys = ("user", "port", "conn-type", "ssh-stricthostkey")
    assert {k: profile.findtext(f"c:{k}", namespaces=NS) for k in keys} == {
        "user": "controller",
        "port": "830",
        "conn-type": "NETCONF_SSH",
        "ssh-stricthostkey": "true",
    }
    groups = {
        g.findtext("c:name", namespaces=NS): [x.text for x in g.findall("c:device-name", NS)]
        for g in devices.findall("c:device-group", NS)
    }
    assert groups == {
        r.name.lower(): [d.hostname for d in topo.devices if topo.region_of(d.hostname) == r.name] for r in topo.regions
    }


def rules(startup: ET.Element, rule_list: str) -> list[dict[str, str | None]]:
    nacm = startup.find("n:nacm", NS)
    rl = next(r for r in nacm.findall("n:rule-list", NS) if r.findtext("n:name", namespaces=NS) == rule_list)
    keys = ("name", "rpc-name", "module-name", "access-operations", "action")
    return [{k: r.findtext(f"n:{k}", namespaces=NS) for k in keys} for r in rl.findall("n:rule", NS)]


def first_match(startup: ET.Element, rule_list: str, rpc: str) -> str:
    """What NACM decides for ``rpc`` in this rule list: the first rule naming it, else exec-default."""
    for r in rules(startup, rule_list):
        if r["rpc-name"] == rpc and r["access-operations"] in ("exec", "*"):
            return str(r["action"])
        if r["rpc-name"] is None and r["module-name"] == "*" and r["access-operations"] == "*":
            return str(r["action"])
    return str(startup.find("n:nacm", NS).findtext("n:exec-default", namespaces=NS))


def test_nacm_is_on_and_denies_writes_and_unlisted_rpcs(startup: ET.Element) -> None:
    nacm = startup.find("n:nacm", NS)
    assert nacm.findtext("n:enable-nacm", namespaces=NS) == "true"
    assert nacm.findtext("n:write-default", namespaces=NS) == "deny"
    assert nacm.findtext("n:exec-default", namespaces=NS) == "deny"
    groups = {
        g.findtext("n:name", namespaces=NS): sorted(u.text for u in g.findall("n:user-name", NS))
        for g in nacm.find("n:groups", NS).findall("n:group", NS)
    }
    assert groups == {"noc-roles": sorted(ROLES), "lab-api": ["lab-api"], "admin": ["admin"]}


@pytest.mark.parametrize("rpc", NEVER_FOR_ROLES)
def test_the_roles_may_not_edit_commit_pull_push_or_connect(startup: ET.Element, rpc: str) -> None:
    assert first_match(startup, "noc-roles", rpc) == "deny"


def test_the_roles_may_read_and_lab_api_may_pull_but_not_push(startup: ET.Element) -> None:
    for rpc in ("get", "get-config", "clixon-cache", "config-path-info", "translate-format", "create-subscription"):
        assert first_match(startup, "noc-roles", rpc) == "permit", rpc
        assert first_match(startup, "lab-api", rpc) == "permit", rpc
    for rpc in ("config-pull", "connection-change", "datastore-diff", "device-rpc-result", "transaction-error"):
        assert first_match(startup, "lab-api", rpc) == "permit", rpc
    for rpc in ("controller-commit", "edit-config", "commit", "device-rpc"):
        assert first_match(startup, "lab-api", rpc) == "deny", rpc
    assert first_match(startup, "admin", "controller-commit") == "permit"
    assert {"name": "hide-nacm", "rpc-name": None, "module-name": "ietf-netconf-acm"} | {
        "access-operations": "read",
        "action": "deny",
    } in rules(startup, "noc-roles")


# ------------------------------------------------------------------------------------------------ config, image
def test_clixon_settings() -> None:
    root = ET.fromstring((HERE / "clixon" / "netsim.xml").read_text(encoding="utf-8"))
    get = {re.sub(r"^\{.*\}", "", e.tag): (e.text or "") for e in root}
    assert get["CLICON_AUTOLOCK"] == "true"
    assert get["CLICON_NACM_MODE"] == "internal" and get["CLICON_NACM_CREDENTIALS"] == "exact"
    assert get["CLICON_NACM_DISABLED_ON_EMPTY"] == "false" and get["CLICON_NACM_RECOVERY_USER"] == "root"
    assert get["CLICON_BACKEND_RESTCONF_PROCESS"] == "false"
    assert get["CONTROLLER_ACTION_COMMAND"] == ""  # no services daemon
    assert get["CONTROLLER_SSH_IDENTITYFILE"].startswith("/var/lib/controller/")  # the volume
    assert "autocli" not in get  # a sub-structure here would replace the controller's own autocli.xml


def args(dockerfile: Path) -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", dockerfile.read_text(encoding="utf-8"), re.M))


def test_the_controller_runs_the_pes_clixon_release_and_a_pinned_controller() -> None:
    ours, pes = args(HERE / "Dockerfile"), args(REPO / "devices" / "Dockerfile")
    for arg in ("CLIGEN_VERSION", "CLIGEN_COMMIT", "CLIXON_VERSION", "CLIXON_COMMIT", "PYTHON_IMAGE"):
        assert ours[arg] == pes[arg], arg
    assert ours["CONTROLLER_VERSION"] == "1.9.0" and re.fullmatch(r"[0-9a-f]{40}", ours["CONTROLLER_COMMIT"])
    text = (HERE / "Dockerfile").read_text(encoding="utf-8")
    assert 'test "$(git -C /src/controller rev-parse HEAD)" = "${CONTROLLER_COMMIT}"' in text


def test_supervisor_is_the_locked_wheel() -> None:
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    block = lock.split('[[package]]\nname = "supervisor"\n', 1)[1].split("[[package]]", 1)[0]
    version = re.search(r'version = "([^"]+)"', block).group(1)
    wheel_hash = re.search(r'\.whl", hash = "(sha256:[0-9a-f]+)"', block).group(1)
    assert f"supervisor=={version} --hash={wheel_hash}" in (HERE / "Dockerfile").read_text(encoding="utf-8")


def test_dockerignore_keeps_what_the_dockerfile_copies() -> None:
    ignore = (HERE / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    allowed = [line[1:].rstrip("/") for line in ignore if line.startswith("!")]
    copies = re.findall(r"^COPY (?!--from)(?:--chmod=\S+ )?(.+) \S+$", (HERE / "Dockerfile").read_text(), re.M)
    assert copies
    for sources in copies:
        for src in sources.split():
            assert any(src.rstrip("/") == a or src.startswith(a + "/") for a in allowed), src


def test_supervisord_runs_the_backend_sshd_and_the_agent() -> None:
    conf = (HERE / "supervisord.conf").read_text(encoding="utf-8")
    assert re.findall(r"^\[program:([a-z_]+)\]", conf, re.M) == ["clixon_backend", "sshd", "controller_agent"]
    assert "clixon_backend -F -f /etc/clixon/controller.xml -s startup" in conf


# ------------------------------------------------------------------------------------------------ SSH
def test_the_controllers_sshd_lets_in_only_lab_api_and_only_into_the_cli() -> None:
    topo = load_topology()
    conf = ssh_config(HERE / "sshd_config")
    lab_api = topo.infra("lab-api").mgmt_ip
    assert conf["AllowUsers"] == [" ".join(f"{u}@{lab_api}" for u in (*ROLES, "lab-api"))]
    assert conf["ForceCommand"] == ["/usr/local/bin/controller-cli"]
    assert conf["AuthorizedKeysFile"] == ["/etc/netsim/keys/lab-api.pub"]
    for key, value in {
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "PermitRootLogin": "no",
        "AuthenticationMethods": "publickey",
        "DisableForwarding": "yes",
        "PermitTTY": "no",
        "StrictModes": "yes",
        "Port": "22",
    }.items():
        assert conf[key] == [value], key
    assert "Subsystem" not in conf  # no sftp
    login = (HERE / "bin" / "controller-cli").read_text(encoding="utf-8")
    assert login.count("exec /opt/clixon/bin/clixon_cli") == 2 and "$@" not in login and "$*" not in login


def test_the_pes_sshd_lets_in_only_the_controller_and_only_into_netconf() -> None:
    topo = load_topology()
    conf = ssh_config(REPO / "devices" / "sshd_config")
    assert conf["Port"] == ["830"]
    assert conf["AllowUsers"] == [f"controller@{topo.infra('controller').mgmt_ip}"]
    assert conf["AuthorizedKeysFile"] == ["/etc/netsim/keys/controller.pub"]
    assert conf["Subsystem"] == ["netconf /opt/clixon/bin/clixon_netconf -f /etc/clixon.xml -l e"]
    assert conf["ForceCommand"] == ["/usr/local/bin/netconf-session"]
    for key, value in {"PasswordAuthentication": "no", "PermitRootLogin": "no", "DisableForwarding": "yes"}.items():
        assert conf[key] == [value], key
    session = (REPO / "devices" / "netconf-session").read_text(encoding="utf-8")
    assert "exec /opt/clixon/bin/clixon_netconf -f /etc/clixon.xml" in session and "$@" not in session


# ------------------------------------------------------------------------------------------------ compose, make
def test_the_compose_fragment_publishes_nothing() -> None:
    topo = load_topology()
    doc = yaml.safe_load((HERE / "compose.yaml").read_text(encoding="utf-8"))
    svc = doc["services"]["controller"]
    assert svc["container_name"] == svc["hostname"] == "controller"
    assert svc["networks"]["labnet"]["ipv4_address"] == topo.infra("controller").mgmt_ip
    assert "ports" not in svc
    assert svc["volumes"] == ["controller-data:/var/lib/controller", "controller-keys:/etc/netsim/keys"]
    assert doc["networks"] == {"labnet": {"external": True}}
    assert svc["image"] == f"netsim/clixon-controller:{args(HERE / 'Dockerfile')['CONTROLLER_VERSION']}"
    lab_api = yaml.safe_load((REPO / "lab" / "compose.yaml").read_text(encoding="utf-8"))["services"]["lab-api"]
    assert "controller-keys:/etc/netsim/keys" in lab_api["volumes"]
    pes = yaml.safe_load((REPO / "devices" / "compose.yaml").read_text(encoding="utf-8"))["services"]
    assert all("controller-keys:/etc/netsim/keys:ro" in pes[h]["volumes"] for h in topo.hostnames)


def test_the_root_files_include_the_controller() -> None:
    assert "controller/compose.yaml" in (REPO / "compose.yaml").read_text(encoding="utf-8")
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "controller/controller.mk" in makefile and "controller/compose.yaml" in makefile
    mk = (HERE / "controller.mk").read_text(encoding="utf-8")
    for target in ("controller-build", "controller-up", "controller-shell", "controller-test", "controller-config"):
        assert re.search(rf"^{target}:", mk, re.M), target
    assert re.search(r"^e2e: controller-test$", mk, re.M)


# ------------------------------------------------------------------------------------------------ agent, healthcheck
def test_the_agent_reads_the_pes_from_the_startup_db() -> None:
    agent = module("agent")
    pes = agent.pes(str(HERE / "startup_db"))
    topo = load_topology()
    assert [(p.name, p.addr, p.port) for p in pes] == [(d.hostname, d.mgmt_ip, 830) for d in topo.devices]
    assert pes[0].known_hosts_key == "[172.30.10.11]:830"


def test_the_agent_reads_the_connection_table() -> None:
    table = (
        "Name                    State      Time                   Logmsg\n"
        "================================================================================\n"
        "hub-a                   OPEN\n"
        "hub-d                   CLOSED     2026-10-24T05:00:44    Connection to 172.30.\n"
    )
    assert module("agent").parse_connections(table) == {"hub-a": "OPEN", "hub-d": "CLOSED"}


TCP_TABLE = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    "   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1\n"
    "   1: 460A1EAC:033E 0B0A1EAC:D2F0 01 00000000:00000000 00:00000000 00000000     0        0 2\n"
)


def test_the_healthchecks_read_listening_sockets_without_connecting(tmp_path: Path) -> None:
    table = tmp_path / "tcp"
    table.write_text(TCP_TABLE)
    hc = module("healthcheck")
    assert hc.listening(22, (str(table),)) is True
    assert hc.listening(830, (str(table),)) is False  # an established connection on 830 is not a listener
    assert hc.listening(22, (str(tmp_path / "missing"),)) is False
    from netsim_simd.healthcheck import listening

    assert listening(22, (str(table),)) and not listening(830, (str(table),))
