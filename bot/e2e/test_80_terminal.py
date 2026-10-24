"""The roles' terminals on the running lab: noc-controller installed in a throwaway home from the block that
`botctl terminal-setup` prints (as a Bot runs it on its own computer), reading the Clixon controller through /mcp with
the Network Engineer's terminal token. The room's terminal shows each command, marked as coming from its computer."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from netsim_bot import cli
from netsim_bot.settings import NETENG, load_settings
from typer.testing import CliRunner

REFUSED = ("pull spoke-1", "commit push")


@pytest.fixture(scope="module")
def terminal(bot, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """The Network Engineer's terminal: the setup block run by ``sh`` in a fresh home, then ``run(*words)``."""
    token = load_settings().terminal_tokens.get(NETENG)
    assert token, "BOT_NETENG_TERMINAL_TOKEN missing in .env (make env)"
    home = tmp_path_factory.mktemp("bot-computer")
    url = f"{str(bot.http.base_url).rstrip('/')}/mcp"
    setup = CliRunner().invoke(cli.app, ["terminal-setup", "--role", NETENG, "--url", url, "--show-token"])
    assert setup.exit_code == 0, "botctl terminal-setup failed"  # its output holds the token: not shown
    lines = setup.stdout.splitlines()
    script = "\n".join(lines[lines.index("```sh") + 1 : len(lines) - 1 - lines[::-1].index("```")]) + "\n"
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}

    def checked(r: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
        leaked = token in r.stdout + r.stderr
        assert not leaked, "noc-controller printed its token"
        return r

    smoke = checked(subprocess.run(["sh"], input=script, env=env, capture_output=True, text=True, timeout=120))
    command = home / ".local" / "bin" / "noc-controller"

    def run(*words: str) -> subprocess.CompletedProcess[str]:
        return checked(subprocess.run([str(command), *words], env=env, capture_output=True, text=True, timeout=90))

    return SimpleNamespace(token=token, home=home, smoke=smoke, run=run, command=command)


def test_the_setup_block_installs_the_command_and_its_smoke_test_answers(terminal: SimpleNamespace) -> None:
    config = terminal.home / ".config" / "noc-controller" / "network-engineer.conf"
    assert terminal.command.stat().st_mode & 0o777 == 0o755 and config.stat().st_mode & 0o777 == 0o600
    assert terminal.smoke.returncode == 0, terminal.smoke.stderr
    lines = terminal.smoke.stdout.splitlines()
    assert lines[0] == "network-engineer@controller> show connections" and lines[-1].startswith("read only")


def test_the_network_engineer_reads_the_controller_from_its_computer(
    terminal: SimpleNamespace, bot, lab_api: httpx.Client, timing: Callable[..., None]
) -> None:
    after, t0 = bot.last_event_id(), time.time()
    started = time.monotonic()
    table = terminal.run("show", "connections")
    timing("terminal: noc-controller show connections", time.monotonic() - started)
    assert table.returncode == 0, table.stderr
    lines = table.stdout.splitlines()
    assert lines[0] == "network-engineer@controller> show connections"  # the prompt line the room's CRT types
    assert sum(" OPEN" in line for line in lines) == 10 and lines[-1].startswith("read only · as network-engineer")
    check = terminal.run("show", "devices", "spoke-1", "check")
    assert check.returncode == 0, check.stderr
    assert check.stdout.splitlines()[0] == "lab-api@controller> show devices spoke-1 check"
    assert check.stdout.splitlines()[-1].endswith(" · in sync")
    for command in REFUSED:
        refused = terminal.run(*command.split())
        assert refused.returncode == 1, (command, refused.stdout, refused.stderr)
        assert refused.stdout.splitlines()[0] == f"network-engineer@controller> {command}"
        assert "Allowed commands (one per call):" in refused.stdout and "  show devices <router> diff" in refused.stdout

    events = bot.stream(after, {"terminal"}, lambda got: len(got) >= 2 + len(REFUSED))
    assert [(e["bot"], e["command"], e["allowed"], e["via"]) for e in events] == [
        ("NETENG", "show connections", True, "terminal"),
        ("NETENG", "show devices spoke-1 check", True, "terminal"),
        *[("NETENG", c, False, "terminal") for c in REFUSED],
    ]
    assert [t["via"] for t in bot.get("/api/terminal", params={"limit": 4})] == ["terminal"] * 4
    audit = lab_api.get("/audit", params={"since": t0, "action": "controller.cli", "limit": 20}).json()
    assert [(e["actor"], e["request"]["command"], e["status"]) for e in audit] == [
        ("grok-bot/NETENG", "show connections", 200),  # the role's actor, as through its connector
        ("grok-bot/NETENG", "show devices spoke-1 check", 200),
        *[("grok-bot/NETENG", c, 403) for c in REFUSED],
    ]


def test_the_terminal_token_opens_controller_cli_and_nothing_else(terminal: SimpleNamespace, bot) -> None:
    base = str(bot.http.base_url).rstrip("/")
    auth = {"Authorization": f"Bearer {terminal.token}", "Accept": "application/json, text/event-stream"}

    def rpc(method: str, params: dict | None = None) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        r = httpx.post(f"{base}/mcp", json=body, headers=auth, timeout=60)
        assert r.status_code == 200, f"{method}: HTTP {r.status_code}"
        return r.json()["result"]

    assert [t["name"] for t in rpc("tools/list")["tools"]] == ["controller_cli"]
    drift = rpc("tools/call", {"name": "get_drift", "arguments": {}})
    assert drift["isError"] is True
    assert "is refused: this is the Network Engineer's terminal token" in drift["structuredContent"]["error"]
    # outside /mcp the token is nobody (calls made before the asserts: a failing assert shows no header)
    control = httpx.post(f"{base}/api/control", json={"paused": True}, headers={"X-Operator-Token": terminal.token})
    hook = httpx.post(f"{base}/hooks/zabbix", json={}, headers=auth)
    assert (control.status_code, hook.status_code) == (401, 401)
    assert bot.get("/api/state")["control"]["paused"] is False  # nothing changed
    holds = terminal.token in Path(terminal.command).read_text(encoding="utf-8")
    assert not holds  # the command itself holds no token: only the config does
