"""lab-api and the Clixon controller (no containers): the allow-list (wall 1), the SSH session it runs commands in (a
fake one here), the answer, the audit, the status and the resync."""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_lab.api.app import AppState, create_app
from netsim_lab.controller import (
    ALLOWED,
    MAX_OUTPUT,
    Controller,
    ControllerError,
    Refused,
    allow,
    cap,
    clean_output,
    parse_connections,
    parse_diff,
)
from netsim_lab.settings import load_settings

TOKEN = "t0ken-for-tests"
HEADERS = {"X-Lab-Token": TOKEN, "X-Lab-Actor": "grok-bot/NETENG"}
HOSTS = ["hub-a", "hub-b", "hub-c", "hub-d", "spoke-1", "spoke-2", "spoke-3", "spoke-4", "spoke-5", "spoke-6"]
TABLE = (
    "Name                    State      Time                   Logmsg               \n"
    "================================================================================\n"
    + "".join(f"{h:<24}OPEN{' ' * 30}\n" for h in HOSTS if h != "hub-d")
    + "hub-d                   CLOSED     2026-10-24T05:00:44    Connection to 172.30.\n"
)
DIFF = (
    "Warning: device 'hub-d' skipped (closed)\n"
    "spoke-1:\n"
    "         pseudowire backup {\n"
    '-           vc-id "2010";\n'
    '+           vc-id "2001";\n'
    "         }\n"
)


class FakeSsh:
    """The controller as an SSH session shows it: ``answers[command] = (exit status, stdout, stderr)``; the CLI
    echoes the command it reads, as the real one does (wrapped at 80 columns)."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.answers: dict[str, tuple[int, str, str]] = {}
        self.fail: tuple[int, str, str] | None = None

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("PRIVATE\n")
            Path(f"{key}.pub").write_text("ssh-ed25519 AAAAlabapi lab-api@netsim\n")
            return 0, "", ""
        self.calls.append((argv, stdin))
        if self.fail:
            return self.fail
        out, err, code = "", "", 0
        for command in stdin.splitlines():
            echo = "\n\r".join(command[i : i + 80] for i in range(0, len(command), 80))
            c, o, e = self.answers.get(command, (0, "", ""))
            out += f"\r{echo}\n{o}"
            err += e
            code = c
        return code, out + "\r\n\r", err

    def users(self) -> list[str]:
        return [argv[-1].split("@")[0] for argv, _ in self.calls]

    def commands(self) -> list[list[str]]:
        return [stdin.splitlines() for _, stdin in self.calls]


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    shared = tmp_path / "keys"
    shared.mkdir()
    (shared / "controller-host.pub").write_text("ssh-ed25519 AAAAcontrollerhost controller\n")
    return shared


@pytest.fixture
def ssh() -> FakeSsh:
    return FakeSsh()


@pytest.fixture
def controller(tmp_path: Path, keys: Path, ssh: FakeSsh) -> Controller:
    return Controller(
        address="172.30.10.70", hosts=HOSTS, key_dir=tmp_path / "lab-api" / "controller", keys_dir=keys, runner=ssh
    )


class FakeLab:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def forget_intent(self) -> None:
        pass


@pytest.fixture
def client(tmp_path: Path, controller: Controller):
    settings = replace(load_settings(environ={}), lab_api_token=TOKEN, webhook_secret="s", data_dir=tmp_path)
    state = AppState(
        settings=settings,
        lab=FakeLab(settings),  # type: ignore[arg-type]
        sync_fn=lambda: {},
        settings_loader=lambda: settings,
        controller=controller,
    )
    with TestClient(create_app(state)) as c:
        c.state = state  # type: ignore[attr-defined]
        yield c
    state.syncer.stop()


def cli(client: TestClient, role: str, command: str, headers: dict[str, str] | None = None) -> Any:
    return client.post("/controller/cli", json={"role": role, "command": command}, headers=headers or HEADERS)


# ------------------------------------------------------------------------------------------------ wall 1
@pytest.mark.parametrize(
    ("command", "normalized", "runs_as"),
    [
        ("show connections", "show connections", "role"),
        ("  show   connections  ", "show connections", "role"),
        ("show connections spoke-1", "show connections spoke-1", "role"),
        ("show connections hub-a detail", "show connections hub-a detail", "role"),
        ("show devices spoke-1 check", "show devices spoke-1 check", "lab-api"),
        ("show devices hub-b diff", "show devices hub-b diff", "lab-api"),
        (
            "show configuration devices device spoke-1 config",
            "show configuration devices device spoke-1 config",
            "role",
        ),
        (
            "show configuration devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup",
            "show configuration devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup",
            "role",
        ),
        (
            "show configuration devices device hub-a config interfaces interface GigabitEthernet0/0/2.101",
            "show configuration devices device hub-a config interfaces interface GigabitEthernet0/0/2.101",
            "role",
        ),
    ],
)
def test_allowed_commands(command: str, normalized: str, runs_as: str) -> None:
    allowed = allow(command, HOSTS)
    assert (allowed.command, allowed.runs_as) == (normalized, runs_as)


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("pull spoke-1", "not an allowed command"),
        ("push spoke-1", "not an allowed command"),
        ("commit push", "not an allowed command"),
        ("configure", "not an allowed command"),
        ("set devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup vc-id 2010", "not an allowed"),
        ("connection open hub-a", "not an allowed command"),
        ("connection close spoke-1", "not an allowed command"),
        ("rpc stats spoke-1", "not an allowed command"),
        ("processes services start", "not an allowed command"),
        ("debug backend 9", "not an allowed command"),
        ("session kill 1", "not an allowed command"),
        ("save /tmp/x", "not an allowed command"),
        ("shell id", "not an allowed command"),
        ("show devices", "not an allowed command"),
        ("show devices spoke-1", "not an allowed command"),
        ("show devices spoke-1 check detail", "not an allowed command"),
        ("show devices spoke-1 state", "not an allowed command"),
        ("show devices group north check", "not an allowed command"),
        ("show configuration", "not an allowed command"),
        ("show configuration devices device spoke-1", "not an allowed command"),
        ("show configuration nacm", "not an allowed command"),
        ("show devices spoke-9 check", "unknown router 'spoke-9'"),
        ("show connections controller", "unknown router"),
        ("show devices * diff", "a special character"),
        ("show devices; pull", "more than one command"),
        ("show connections\npull spoke-1", "more than one command"),
        ("show connections\rpull spoke-1", "more than one command"),
        ("show connections | display xml", "pipes and redirects"),
        ("show configuration devices device spoke-1 config > /tmp/x", "pipes and redirects"),
        ("show connections < /etc/passwd", "pipes and redirects"),
        ("show connections && pull spoke-1", "a special character"),
        ("show connections $(pull spoke-1)", "a special character"),
        ("show connections `pull spoke-1`", "a special character"),
        ("show configuration devices device spoke-1 config 'l2vpn'", "a special character"),
        ('show configuration devices device spoke-1 config "l2vpn"', "a special character"),
        ("show connections\tspoke-1", "control characters"),
        ("show connections \x1b[2J", "control characters"),
        ("show connections spoke-1\x00", "control characters"),
        ("show connections spöke-1", "outside ASCII"),
        ("show configuration devices device spoke-1 config " + "l2vpn " * 40, "more than 200 characters"),
        ("show configuration devices device spoke-1 config " + " ".join(["l2vpn"] * 13), "longer than 12 words"),
        ("show configuration devices device spoke-1 config ../etc", "not a config path word"),
        ("", "an empty command"),
        ("   ", "an empty command"),
    ],
)
def test_everything_else_is_refused_listing_the_allowed_commands(command: str, reason: str) -> None:
    with pytest.raises(Refused) as exc:
        allow(command, HOSTS)
    text = str(exc.value)
    assert text.startswith("refused: ") and reason in text
    assert all(a in text for a in ALLOWED) and "spoke-6" in text


# ------------------------------------------------------------------------------------------------ the endpoint
def test_a_role_read_runs_as_the_roles_user_with_the_command_on_stdin(client, ssh: FakeSsh) -> None:
    ssh.answers["show connections"] = (0, TABLE, "")
    r = cli(client, "noc-engineer", "show   connections")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "noc-engineer" and body["user"] == "noc-engineer" and body["allowed"] is True
    assert body["command"] == "show connections" and not body["truncated"]
    assert body["output"].startswith("Name") and "hub-d                   CLOSED" in body["output"]
    argv, stdin = ssh.calls[-1]
    assert argv[0] == "ssh" and argv[-1] == "noc-engineer@172.30.10.70" and "-T" in argv
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"):
        assert option in argv
    assert stdin == "show connections\n" and not any("show" in a for a in argv)  # never on a command line


def test_check_and_diff_run_as_lab_api_and_the_actor_is_still_the_role(client, ssh: FakeSsh) -> None:
    ssh.answers["show devices spoke-1 check"] = (0, "Error: device out-of-sync\n", "")
    ssh.answers["show devices spoke-1 diff"] = (0, DIFF.split("\n", 1)[1], "")
    check = cli(client, "network-engineer", "show devices spoke-1 check").json()
    diff = cli(client, "network-engineer", "show devices spoke-1 diff").json()
    assert check["user"] == diff["user"] == "lab-api" and ssh.users() == ["lab-api", "lab-api"]
    assert check["output"] == "Error: device out-of-sync"
    assert '-           vc-id "2010";' in diff["output"] and '+           vc-id "2001";' in diff["output"]
    audit = client.get("/audit", params={"action": "controller.cli"}).json()
    assert [(e["actor"], e["target"], e["result"]["user"]) for e in audit] == [
        ("grok-bot/NETENG", "network-engineer", "lab-api"),
        ("grok-bot/NETENG", "network-engineer", "lab-api"),
    ]


@pytest.mark.parametrize(
    "command",
    [
        "pull spoke-1",
        "commit push",
        "set devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup vc-id 2010",
        "show devices; pull",
        "show devices spoke-9 check",
        "show connections\npull spoke-1",
        "show connections | display json",
    ],
)
def test_a_refusal_is_403_audited_and_reaches_nothing(client, ssh: FakeSsh, command: str) -> None:
    r = cli(client, "noc-manager", command)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["allowed"] is False and detail["user"] is None and detail["output"].startswith("refused: ")
    assert "Allowed commands" in detail["output"]
    assert ssh.calls == []  # no SSH session at all
    entry = client.get("/audit", params={"action": "controller.cli"}).json()[-1]
    assert entry["status"] == 403 and entry["ok"] is False and entry["request"]["command"] == command


def test_writes_need_the_lab_token(client, ssh: FakeSsh) -> None:
    assert cli(client, "noc-engineer", "show connections", headers={"X-Lab-Actor": "x"}).status_code == 401
    bad = {"X-Lab-Token": "wrong", "X-Lab-Actor": "x"}
    assert cli(client, "noc-engineer", "show connections", headers=bad).status_code == 401
    assert client.post("/controller/resync", json={}, headers={"X-Lab-Actor": "x"}).status_code == 401
    admin = {"role": "admin", "command": "show connections"}  # not a NOC role
    assert client.post("/controller/cli", json=admin, headers=HEADERS).status_code == 422
    assert ssh.calls == []


def test_an_unavailable_controller_is_503(client, ssh: FakeSsh) -> None:
    ssh.fail = (255, "", "ssh: connect to host 172.30.10.70 port 22: No route to host\n")
    r = cli(client, "noc-engineer", "show connections")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["allowed"] is True and "unavailable" in detail["output"] and "No route to host" in detail["output"]
    assert client.get("/audit", params={"action": "controller.cli"}).json()[-1]["status"] == 503
    status = client.get("/controller/status", params={"max_age": 0}).json()
    assert status["available"] is False and "unavailable" in status["error"]


def test_a_controller_that_never_started_is_503(client, keys: Path) -> None:
    (keys / "controller-host.pub").unlink()
    r = cli(client, "noc-engineer", "show connections")
    assert r.status_code == 503 and "not started yet" in r.json()["detail"]["output"]


def test_a_timeout_is_503(client, controller: Controller) -> None:
    def slow(argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        if argv[0] == "ssh-keygen":
            return FakeSsh()(argv, stdin, timeout)
        raise ControllerError(f"the controller did not answer within {timeout:g} s")

    controller.runner = slow
    r = cli(client, "noc-engineer", "show connections")
    assert r.status_code == 503 and "did not answer within 15 s" in r.json()["detail"]["output"]


def test_the_answer_is_capped_and_clean(client, ssh: FakeSsh) -> None:
    big = "\x1b[32m" + "\n".join(f"line {i:05d} " + "x" * 90 for i in range(400)) + "\x1b[0m\n"
    ssh.answers["show configuration devices device spoke-1 config"] = (0, big, "")
    body = cli(client, "noc-engineer", "show configuration devices device spoke-1 config").json()
    assert body["truncated"] is True and len(body["output"].encode()) <= MAX_OUTPUT + 200
    assert "\x1b" not in body["output"] and body["output"].startswith("line 00000")
    assert body["output"].endswith("narrow the command, e.g. a config subtree)")


def test_lab_api_makes_its_key_once_and_pins_the_controllers(controller: Controller, keys: Path, ssh) -> None:
    ssh.answers["show connections"] = (0, TABLE, "")
    controller.run("noc-engineer", "show connections")
    first = controller.key.read_text()
    controller.run("noc-engineer", "show connections")
    assert controller.key.read_text() == first and oct(controller.key_dir.stat().st_mode & 0o777) == "0o700"
    assert (keys / "lab-api.pub").read_text() == "ssh-ed25519 AAAAlabapi lab-api@netsim\n"
    assert controller.known_hosts.read_text() == "172.30.10.70 ssh-ed25519 AAAAcontrollerhost\n"
    argv = ssh.calls[-1][0]
    assert f"UserKnownHostsFile={controller.known_hosts}" in argv and str(controller.key) in argv


# ------------------------------------------------------------------------------------------------ status, resync
def test_status_counts_connected_in_sync_and_out_of_sync(client, ssh: FakeSsh) -> None:
    ssh.answers["show connections"] = (0, TABLE, "")
    ssh.answers["show devices * diff"] = (0, DIFF, "")
    status = client.get("/controller/status", params={"max_age": 0}).json()
    assert status["available"] and status["connected"] == 9
    assert status["out_of_sync"] == ["spoke-1"] and status["closed"] == ["hub-d"]
    assert len(status["in_sync"]) == 8 and "hub-d" not in status["in_sync"]
    assert status["routers"]["hub-d"] == {
        "state": "CLOSED",
        "time": "2026-10-24T05:00:44",
        "logmsg": "Connection to 172.30.",
        "in_sync": None,
        "diff": None,
    }
    assert 'vc-id "2010"' in status["routers"]["spoke-1"]["diff"]
    calls = len(ssh.calls)
    client.get("/controller/status")  # cached
    assert len(ssh.calls) == calls


def test_resync_pulls_connected_routers_and_connects_closed_ones(client, ssh: FakeSsh) -> None:
    ssh.answers["show connections"] = (0, TABLE, "")
    r = client.post("/controller/resync", json={"hosts": ["spoke-1", "hub-d"]}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["hosts"]["spoke-1"]["action"] == "pull" and r.json()["hosts"]["hub-d"]["action"] == "connect"
    assert ["pull spoke-1", "connection open hub-d"] in ssh.commands() and set(ssh.users()) == {"lab-api"}
    entry = client.get("/audit", params={"action": "controller.resync"}).json()[-1]
    assert entry["actor"] == "lab-api" and entry["request"] == {"why": "requested by grok-bot/NETENG"}
    assert client.post("/controller/resync", json={"hosts": ["spoke-9"]}, headers=HEADERS).status_code == 404


def test_resync_never_fails_what_lab_api_did(client, ssh: FakeSsh) -> None:
    ssh.fail = (255, "", "ssh: connect to host 172.30.10.70 port 22: Connection refused\n")
    result = client.state.resync_controller(["spoke-1"], "config applied by test")
    assert "unavailable" in result["error"]
    assert client.post("/controller/resync", json={}, headers=HEADERS).status_code == 503


# ------------------------------------------------------------------------------------------------ parsing
def test_clean_output_drops_the_echo_codes_and_log_prefixes() -> None:
    command = "show configuration devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup"
    echo = "\r" + command[:80] + "\n\r" + command[80:] + "\n"
    out = echo + "\r\x1b[1mspoke-1:clixon-controller:devices {\x1b[0m\n}\n\r\n\r"
    err = "Oct 24 04:56:14.477599: cli_rpc_pull: 1069: XML error: Get configuration: application access-denied\n"
    assert clean_output(out, err, command) == (
        "spoke-1:clixon-controller:devices {\n}\nXML error: Get configuration: application access-denied"
    )
    assert clean_output("\rshow connections\n\r\n\r", "", "show connections") == ""


def test_cap_cuts_at_a_line() -> None:
    text, cut = cap("a\n" * 10, limit=9)
    assert cut and text.startswith("a\na\na\na\n") and "cut at" in text
    assert cap("short") == ("short", False)


def test_parsers() -> None:
    rows = parse_connections(TABLE)
    assert rows["hub-a"] == {"state": "OPEN", "time": "", "logmsg": ""} and rows["hub-d"]["state"] == "CLOSED"
    assert len(rows) == 10
    assert set(parse_diff(DIFF, HOSTS)) == {"spoke-1"}
    assert parse_diff("Warning: device 'hub-d' skipped (closed)\n", HOSTS) == {}
