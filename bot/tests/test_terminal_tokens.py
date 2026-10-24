"""The roles' terminals on grok-bot's side: the terminal tokens (each one its role, controller_cli only, nothing outside
/mcp), the transcript's source mark, and ``botctl terminal-setup`` (the block a Bot runs on its own computer)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_bot import cli
from netsim_bot.api.app import create_app
from netsim_bot.engine import Engine
from netsim_bot.roles import COMMON, TOOLSETS
from netsim_bot.settings import SettingsError, load_settings
from netsim_bot.store import Store

ROLE_TOKENS = {"noc-engineer": "noceng-token", "network-engineer": "neteng-token", "noc-manager": "nocmgr-token"}
TERMINAL = {"noc-engineer": "noceng-terminal", "network-engineer": "neteng-terminal", "noc-manager": "nocmgr-terminal"}
BOTS = {"noc-engineer": "NOCENG", "network-engineer": "NETENG", "noc-manager": "NOCMGR"}
SCRIPT = Path(__file__).resolve().parents[1] / "grokbot-app" / "terminal" / "noc-controller"


def controller(request: httpx.Request) -> tuple[int, Any]:
    """lab-api's ``POST /controller/cli``: ``pull ...`` refused, anything else answered as the role."""
    body = json.loads(request.content)
    if body["command"].startswith("pull"):
        refused = {"role": body["role"], "user": None, "command": body["command"], "allowed": False}
        return 403, {"detail": {**refused, "output": "refused: not an allowed command", "duration_ms": 0}}
    return 200, {
        "role": body["role"],
        "user": body["role"],
        "command": body["command"],
        "allowed": True,
        "output": "hub-a                   OPEN",
        "truncated": False,
        "duration_ms": 90,
    }


@pytest.fixture
def client(fake_lab, settings_factory, tmp_path):
    s = settings_factory(BOT_MCP_HOSTS="*.trycloudflare.com")
    lab = fake_lab("healthy")
    lab.set("POST /controller/cli", controller)
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())
    with TestClient(create_app(engine=eng, start_engine=False)) as c:
        c.engine, c.lab = eng, lab  # type: ignore[attr-defined]
        yield c


def rpc(client, token: str, method: str, params: dict | None = None, **headers: str) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}", **headers})


def call(client, token: str, tool: str, **args: Any) -> dict[str, Any]:
    r = rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]


# ---------------------------------------------------------------------------------------------- the token kinds
@pytest.mark.parametrize("role", list(TERMINAL))
def test_a_terminal_token_is_its_role_with_one_tool(client, role: str) -> None:
    init = rpc(client, TERMINAL[role], "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["serverInfo"]["name"] == f"netsim-noc-{BOTS[role].lower()}-terminal"
    assert "terminal" in init["instructions"] and "controller_cli" in init["instructions"]
    assert "Every other tool is refused" in init["instructions"]
    tools = rpc(client, TERMINAL[role], "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["controller_cli"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    out = call(client, TERMINAL[role], "controller_cli", command="show connections")
    assert out["isError"] is False and out["structuredContent"]["role"] == role
    [(_path, body, headers)] = client.lab.posts("/controller/cli")
    assert body == {"role": role, "command": "show connections"}
    assert headers["x-lab-actor"] == f"grok-bot/{BOTS[role]}"  # the same actor as through the connector


@pytest.mark.parametrize("tool", ["get_drift", "get_noc_status", "take_incident", "review_ticket", "approve_ticket"])
def test_a_terminal_token_is_refused_every_other_tool(client, tool: str) -> None:
    eng: Engine = client.engine
    before = (eng.bus.last_id, [m.status for m in eng.roles.values()])
    out = call(client, TERMINAL["network-engineer"], tool, incident_id="INC-0001")
    assert out["isError"] is True
    error = out["structuredContent"]["error"]
    assert error.startswith(f"{tool} is refused: this is the Network Engineer's terminal token")
    assert "controller_cli only" in error
    assert client.lab.requests == []  # nothing reached lab-api
    assert (eng.bus.last_id, [m.status for m in eng.roles.values()]) == before  # no activity, no robot moved


def test_nothing_outside_mcp_takes_a_terminal_token(client, kit) -> None:
    token = TERMINAL["noc-engineer"]
    for path in ("/api/control", "/api/proposals/CHG-0001/approve", "/api/pages/test/noc-engineer", "/api/chat"):
        body = {"paused": True} if path == "/api/control" else {"message": "x"} if path == "/api/chat" else None
        r = client.post(path, json=body, headers={"X-Operator-Token": token, "Authorization": f"Bearer {token}"})
        assert r.status_code == 401, (path, r.status_code)
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.post("/hooks/zabbix", json=kit.zabbix_body("Customer-A"), headers=bearer).status_code == 401
    tunnel = {"Host": "x-y.trycloudflare.com", "Authorization": f"Bearer {token}"}
    for path in ("/", "/api/state", "/api/terminal"):
        assert client.get(path, headers=tunnel).status_code == 404, path  # a tunnel host reaches /mcp only
    assert rpc(client, token, "ping", Host="x-y.trycloudflare.com").status_code == 200


def test_role_and_operator_tokens_work_as_before(client) -> None:
    for role, token in ROLE_TOKENS.items():
        init = rpc(client, token, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
        assert init["serverInfo"]["name"] == f"netsim-noc-{BOTS[role].lower()}"
        names = [t["name"] for t in rpc(client, token, "tools/list").json()["result"]["tools"]]
        assert names == [*COMMON, *TOOLSETS[role]]
    assert call(client, ROLE_TOKENS["noc-engineer"], "get_noc_status")["isError"] is False
    r = client.post("/api/control", json={"pages_muted": True}, headers={"X-Operator-Token": "op-token"})
    assert r.status_code == 200 and r.json()["pages_muted"] is True
    assert rpc(client, "op-token", "ping").status_code == 401  # the operator token is no MCP token


def test_the_transcript_and_the_event_say_where_a_command_came_from(client) -> None:
    eng: Engine = client.engine
    after = eng.bus.last_id
    call(client, ROLE_TOKENS["network-engineer"], "controller_cli", command="show connections")
    call(client, TERMINAL["network-engineer"], "controller_cli", command="show connections hub-a")
    call(client, TERMINAL["network-engineer"], "controller_cli", command="pull spoke-1")
    events = [e["data"] for e in eng.bus.recent(after) if e["type"] == "terminal"]
    assert [(e["bot"], e["command"], e["allowed"], e["via"]) for e in events] == [
        ("NETENG", "show connections", True, "connector"),
        ("NETENG", "show connections hub-a", True, "terminal"),
        ("NETENG", "pull spoke-1", False, "terminal"),
    ]
    transcript = client.get("/api/terminal").json()
    assert [t["via"] for t in transcript] == ["connector", "terminal", "terminal"]
    assert [t["via"] for t in client.get("/api/state").json()["terminal"]] == ["connector", "terminal", "terminal"]
    acts = [a for a in eng.store.activities(limit=20) if a.tool == "controller_cli" and a.kind == "tool"]
    assert {a.station for a in acts} == {"terminal"}  # through its terminal too, the robot walks to the CRT


def test_the_page_shows_from_its_computer() -> None:
    web = Path(__file__).resolve().parents[1] / "src" / "netsim_bot" / "web"
    panel = (web / "js" / "panel.js").read_text(encoding="utf-8")
    main = (web / "js" / "main.js").read_text(encoding="utf-8")
    assert "s.via === 'terminal'" in panel and "from its computer" in panel
    assert "from its computer" in main and ".term-meta .via" in (web / "css" / "noc.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------- settings
def test_terminal_tokens_are_read_live(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("BOT_NETENG_TERMINAL_TOKEN=one\n")
    s = load_settings(environ={}, env_file=f)
    assert s.terminal_tokens == {"noc-engineer": None, "network-engineer": "one", "noc-manager": None}
    assert s.public()["roles"]["network-engineer"]["terminal"] is True
    assert s.public()["roles"]["noc-engineer"]["terminal"] is False  # empty: that role's terminal is off
    f.write_text("BOT_NETENG_TERMINAL_TOKEN=tt-rotated-9f3\n")  # rotated after a demo
    assert load_settings(environ={}, env_file=f).terminal_tokens["network-engineer"] == "tt-rotated-9f3"
    assert "tt-rotated-9f3" not in json.dumps(load_settings(environ={}, env_file=f).public())


@pytest.mark.parametrize(
    "same_as",
    [
        "BOT_NETENG_TOKEN",
        "BOT_NOCMGR_TOKEN",
        "BOT_NOCENG_TERMINAL_TOKEN",
        "BOT_OPERATOR_TOKEN",
        "BOT_ZABBIX_HOOK_TOKEN",
        "LAB_API_TOKEN",
    ],
)
def test_a_terminal_token_differs_from_every_other_token(settings_factory, same_as: str) -> None:
    with pytest.raises(SettingsError, match="must differ") as exc:
        settings_factory(**{"BOT_NETENG_TERMINAL_TOKEN": "s3cret-same", same_as: "s3cret-same"})
    assert "BOT_NETENG_TERMINAL_TOKEN" in str(exc.value) and same_as in str(exc.value)
    assert "s3cret-same" not in str(exc.value)


def test_a_role_token_differs_from_the_operator_token(settings_factory) -> None:
    with pytest.raises(SettingsError, match="must differ"):
        settings_factory(BOT_NOCENG_TOKEN="op-token")  # the operator's token (make_settings) as a role token


# ---------------------------------------------------------------------------------------------- terminal-setup
runner = CliRunner()


def setup(*args: str, env: dict[str, str] | None = None) -> Any:
    """``botctl terminal-setup`` with fake settings only (never the repository's .env)."""
    base = {
        "BOT_ENV_FILE": "/nonexistent/.env",
        "BOT_NETENG_TERMINAL_TOKEN": "neteng-terminal-$HOME`id`\\x",
        "BOT_MCP_HOSTS": "box.example.ts.net",
    }
    return runner.invoke(cli.app, ["terminal-setup", *args], env={**base, **(env or {})})


def script_of(block: str) -> str:
    """The shell part of the block (between the code fence lines)."""
    lines = block.splitlines()
    return "\n".join(lines[lines.index("```sh") + 1 : len(lines) - 1 - lines[::-1].index("```")]) + "\n"


def test_terminal_setup_prints_a_placeholder_without_show_token() -> None:
    r = setup("--role", "network-engineer", "--url", "https://box.example.ts.net/mcp")
    assert r.exit_code == 0, r.output
    assert "token=<BOT_NETENG_TERMINAL_TOKEN from .env>" in r.stdout
    assert "neteng-terminal" not in r.output and "neteng-token" not in r.output
    assert "--show-token" in r.stderr  # the note says how to get the real one


def test_terminal_setup_quotes_everything_and_ends_with_the_smoke_test() -> None:
    r = setup("--role", "NETENG", "--url", "https://box.example.ts.net/mcp", "--show-token")
    assert r.exit_code == 0, r.output
    block = r.stdout
    assert block.count("neteng-terminal-$HOME`id`\\x") == 1  # once, in the config, as it is
    assert "cat > \"$bin/noc-controller\" <<'NOC_CONTROLLER_SCRIPT'" in block  # quoted: the shell expands nothing
    assert "cat > \"$config/network-engineer.conf\" <<'NOC_CONTROLLER_CONFIG'" in block
    assert "umask 077" in block and 'chmod 600 "$config/network-engineer.conf"' in block
    assert SCRIPT.read_text(encoding="utf-8") in block  # the command, verbatim
    assert block.rstrip().splitlines()[-2] == "noc-controller --role network-engineer show connections"
    assert "never read, print or copy ~/.config/noc-controller/network-engineer.conf" in block
    assert r.stderr.count("warning") == 0  # the host is in BOT_MCP_HOSTS, https, /mcp


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_block_installs_with_the_right_modes_and_a_new_url_replaces_the_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def install(url: str) -> subprocess.CompletedProcess[str]:
        r = setup("--role", "network-engineer", "--url", url, "--show-token")
        assert r.exit_code == 0, r.output
        return subprocess.run(["sh"], input=script_of(r.stdout), env=env, capture_output=True, text=True, timeout=60)

    first = install("http://127.0.0.1:9/mcp")  # nothing listens there: the smoke test fails at once
    command = home / ".local" / "bin" / "noc-controller"
    config = home / ".config" / "noc-controller" / "network-engineer.conf"
    assert command.read_bytes() == SCRIPT.read_bytes()
    assert (command.stat().st_mode & 0o777, config.stat().st_mode & 0o777) == (0o755, 0o600)
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert config.read_text() == "url=http://127.0.0.1:9/mcp\ntoken=neteng-terminal-$HOME`id`\\x\n"
    assert first.returncode == 3 and "cannot reach http://127.0.0.1:9/mcp" in first.stderr  # the smoke test ran
    assert "neteng-terminal" not in first.stdout + first.stderr
    config.chmod(0o644)  # whatever happened to it since: a new block makes it private again
    install("http://127.0.0.1:10/mcp")  # a quick tunnel's new URL, say
    assert config.read_text().startswith("url=http://127.0.0.1:10/mcp\n")
    assert config.stat().st_mode & 0o777 == 0o600


def test_terminal_setup_checks_the_url() -> None:
    assert setup("--role", "noc-manager", "--url", "ftp://box.example.ts.net/mcp").exit_code == 2
    assert setup("--role", "noc-manager", "--url", "https://box.example.ts.net/m cp").exit_code == 2
    r = setup("--role", "noc-manager", "--url", "http://box.example.com:8095/api")
    assert r.exit_code == 0
    notes = " ".join(r.stderr.split())
    assert "does not end in /mcp" in notes and "plain http" in notes and "BOT_MCP_HOSTS" in notes
    v6 = setup("--role", "noc-manager", "--url", "https://[2001:db8::1]/mcp")  # brackets are no markup
    assert v6.exit_code == 0 and "lists 2001:db8::1:" in " ".join(v6.stderr.split())
    lab = setup("--role", "noc-manager", "--url", "http://172.30.10.60:8095/mcp")  # labnet: allowed, no warning
    assert lab.exit_code == 0 and "warning" not in lab.stderr
    assert setup("--role", "boss", "--url", "https://box.example.ts.net/mcp").exit_code == 2


def test_terminal_setup_with_the_terminal_off() -> None:
    off = {"BOT_NETENG_TERMINAL_TOKEN": ""}
    r = setup("--role", "network-engineer", "--url", "https://box.example.ts.net/mcp", "--show-token", env=off)
    assert r.exit_code == 1 and "terminal is off" in " ".join(r.stderr.split())
    r = setup("--role", "network-engineer", "--url", "https://box.example.ts.net/mcp", env=off)
    assert r.exit_code == 0 and "<BOT_NETENG_TERMINAL_TOKEN from .env>" in r.stdout


def test_the_block_refuses_what_would_break_its_quoting() -> None:
    with pytest.raises(ValueError, match="NOC_CONTROLLER_SCRIPT"):
        cli.terminal_block("noc-engineer", "https://h/mcp", "t", "print(1)\nNOC_CONTROLLER_SCRIPT\n")
    with pytest.raises(ValueError, match="one line"):
        cli.terminal_block("noc-engineer", "https://h/mcp", "t\nrm -rf /", "print(1)\n")


# ---------------------------------------------------------------------------------------------- the skills, the docs
def test_the_skills_teach_the_terminal_first_and_the_tool_as_fallback() -> None:
    app = SCRIPT.parents[1]
    shared = (app / "SKILL.md").read_text(encoding="utf-8")
    for phrase in (
        "command -v noc-controller",
        "noc-controller --role <your role> <command>",
        "Otherwise use `controller_cli`",
        "Never read, print or copy its config file",
        "Never try to reach the lab or the controller any other way",
        "1 refused",
        "2 the controller is unavailable",
    ):
        assert phrase in shared, phrase
    for role in ("noc-engineer", "network-engineer", "noc-manager"):
        text = (app / "skills" / role / "SKILL.md").read_text(encoding="utf-8")
        assert f"noc-controller --role {role}" in text, role
        assert text.index("noc-controller") < text.index("controller_cli"), role  # the command first, then the tool
    demos = (app / "DEMOS.md").read_text(encoding="utf-8")
    assert "noc-controller --role network-engineer commit push" in demos and "from its computer" in demos
    readme = (app / "README.md").read_text(encoding="utf-8")
    assert "## 5. The Bot's terminal: `noc-controller`" in readme
    assert "curl -s -o /dev/null -w '%{http_code}\\n' https://<tunnel-host>/mcp" in readme
    assert "Not verified in the app:" in readme and "keeps files" in readme
