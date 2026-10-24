"""controller_cli: the roles' read-only Clixon controller CLI (lab-api's allow-list behind it), the room's terminal and
its transcript."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_bot.api.app import create_app
from netsim_bot.engine import TERMINAL_EVENT_LINES, TERMINAL_LINE_CHARS, Engine
from netsim_bot.roles import TOOLSETS
from netsim_bot.store import Store
from netsim_bot.tools import _controller_summary

TOKENS = {"noc-engineer": "noceng-token", "network-engineer": "neteng-token", "noc-manager": "nocmgr-token"}
WEB = Path(__file__).resolve().parents[1] / "src" / "netsim_bot" / "web"
DIFF = 'spoke-1:\n         pseudowire backup {\n-           vc-id "2010";\n+           vc-id "2001";\n         }'
REFUSAL = (
    "refused: not an allowed command: 'pull spoke-1'. Allowed commands (one per call): show connections; show "
    "connections <router>; show connections <router> detail; show devices <router> check; show devices <router> diff"
)


def answer(request: httpx.Request) -> tuple[int, Any]:
    """lab-api's ``POST /controller/cli``: check and diff as lab-api, refusals 403, 'hub-z' as a down controller."""
    body = json.loads(request.content)
    command, role = body["command"], body["role"]
    if command.startswith("pull") or "spoke-9" in command:
        return 403, {
            "detail": {
                "role": role,
                "user": None,
                "command": command,
                "allowed": False,
                "output": REFUSAL,
                "truncated": False,
                "duration_ms": 0,
            }
        }
    if "hub-z" in command:
        return 503, {"detail": {"role": role, "user": role, "command": command, "allowed": True,
                                "output": "the controller is unavailable: ssh: connect to host 172.30.10.70 port 22: "
                                "No route to host", "truncated": False, "duration_ms": 0}}  # fmt: skip
    user = "lab-api" if command.endswith(("check", "diff")) else role
    output = DIFF if command.endswith("diff") else "\n".join(f"line {i}" for i in range(40))
    return 200, {
        "role": role,
        "user": user,
        "command": command,
        "allowed": True,
        "output": output,
        "truncated": False,
        "duration_ms": 120,
    }


@pytest.fixture
def client(fake_lab, settings_factory, tmp_path):
    s = settings_factory()
    lab = fake_lab("healthy")
    lab.set("POST /controller/cli", answer)
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())
    with TestClient(create_app(engine=eng, start_engine=False)) as c:
        c.engine, c.lab = eng, lab  # type: ignore[attr-defined]
        yield c


def call(client, role: str, command: str) -> dict[str, Any]:
    body = {"name": "controller_cli", "arguments": {"command": command}}
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": body},
        headers={"Authorization": f"Bearer {TOKENS[role]}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_every_role_has_controller_cli_as_a_read(client) -> None:
    for role, token in TOKENS.items():
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {token}"},
        )
        tools = {t["name"]: t for t in r.json()["result"]["tools"]}
        tool = tools["controller_cli"]
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False
        assert tool["inputSchema"]["required"] == ["command"]
        assert "show devices <router> check" in tool["description"] and "never change" in tool["description"]
        assert "controller_cli" not in TOOLSETS[role]  # a common tool, not one role's


def test_the_tool_asks_lab_api_as_the_role(client) -> None:
    out = call(client, "network-engineer", "show devices spoke-1 diff")
    assert out["isError"] is False
    result = out["structuredContent"]
    assert result["user"] == "lab-api" and result["output"] == DIFF
    assert result["summary"] == "differs from the controller's copy (2 lines)"
    [(_path, body, headers)] = client.lab.posts("/controller/cli")
    assert body == {"role": "network-engineer", "command": "show devices spoke-1 diff"}
    assert headers["x-lab-actor"] == "grok-bot/NETENG" and headers["x-lab-token"] == "lab-token"


def test_a_refusal_and_an_unavailable_controller_come_back_as_errors(client) -> None:
    out = call(client, "noc-manager", "pull spoke-1")
    assert out["isError"] is True
    refused = out["structuredContent"]
    assert refused["allowed"] is False and refused["error"].startswith("refused: ")
    assert "show connections" in refused["error"]  # the allowed commands
    out = call(client, "noc-engineer", "show connections hub-z")
    assert out["isError"] is True
    down = out["structuredContent"]
    assert down["available"] is False and "carry on without it" in down["error"] and "No route to host" in down["error"]
    assert call(client, "noc-engineer", "")["structuredContent"]["error"].startswith("'command' is required")


def test_the_room_gets_a_terminal_event_and_the_transcript_keeps_everything(client) -> None:
    eng: Engine = client.engine
    after = eng.bus.last_id
    call(client, "noc-engineer", "show connections")
    call(client, "noc-manager", "pull spoke-1")
    events = [e for e in eng.bus.recent(after) if e["type"] == "terminal"]
    assert [(e["data"]["bot"], e["data"]["command"], e["data"]["allowed"]) for e in events] == [
        ("NOCENG", "show connections", True),
        ("NOCMGR", "pull spoke-1", False),
    ]
    first = events[0]["data"]
    assert first["user"] == "noc-engineer" and first["lines"] == 40 and "output" not in first
    assert first["head"] == [f"line {i}" for i in range(TERMINAL_EVENT_LINES)]  # capped in the event
    assert events[1]["data"]["head"] == [REFUSAL[:TERMINAL_LINE_CHARS]]  # lines are capped too
    transcript = client.get("/api/terminal").json()
    assert [t["command"] for t in transcript] == ["show connections", "pull spoke-1"]
    assert transcript[0]["output"].count("\n") == 39 and transcript[1]["output"] == REFUSAL  # whole
    assert client.get("/api/terminal", params={"after": transcript[0]["seq"]}).json() == transcript[1:]
    assert [t["command"] for t in client.get("/api/state").json()["terminal"]] == ["show connections", "pull spoke-1"]
    acts = [a for a in eng.store.activities(limit=10) if a.tool == "controller_cli"]
    assert {(a.kind, a.station) for a in acts} == {("tool", "terminal"), ("result", "terminal")}


@pytest.mark.parametrize(
    ("command", "output", "summary"),
    [
        ("show devices spoke-1 check", "", "in sync"),
        ("show devices spoke-1 check", "Error: device out-of-sync", "out of sync"),
        ("show devices spoke-1 diff", "", "no difference: in sync"),
        (
            "show connections",
            "Name                    State      Time                   Logmsg\n"
            "================================================================================\n"
            "hub-a                   OPEN\n"
            "hub-d                   CLOSED     2026-10-24T05:00:44    Connection to 172.30.",
            "1 OPEN, not open: hub-d",
        ),
        ("show configuration devices device spoke-1 config system", "spoke-1:x {\n  y;\n}", "spoke-1:x { (+2 lines)"),
    ],
)
def test_the_caption_says_what_the_controller_answered(command: str, output: str, summary: str) -> None:
    assert _controller_summary({"command": command, "output": output}) == summary


def test_the_room_has_the_terminal_and_the_panel_its_transcript() -> None:
    room = (WEB / "js" / "room.js").read_text(encoding="utf-8")
    terminal = (WEB / "js" / "terminal.js").read_text(encoding="utf-8")
    main = (WEB / "js" / "main.js").read_text(encoding="utf-8")
    panel = (WEB / "js" / "panel.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "from './terminal.js'" in room and "terminal: [{" in room and "case 'terminal':" in room
    assert "@controller> " in terminal and "'CONTROLLER'" in terminal and "P.red" in terminal
    assert "from './font.js'" in terminal  # the room's own font
    assert "on('terminal'" in main and "/api/terminal" in main and "h.kind === 'terminal'" in main
    assert "export function renderTerminal" in panel
    assert 'data-tab="terminal"' in html


def test_the_skills_teach_the_controller() -> None:
    app = Path(__file__).resolve().parents[1] / "grokbot-app"
    shared = (app / "SKILL.md").read_text(encoding="utf-8")
    assert "controller_cli" in shared and "out of sync" in shared and "carry on without it" in shared
    for command in (
        "show connections",
        "show devices <router> check",
        "show devices <router> diff",
        "show configuration devices device <router> config",
    ):
        assert command in shared, command
    for role in ("noc-engineer", "network-engineer", "noc-manager"):
        text = (app / "skills" / role / "SKILL.md").read_text(encoding="utf-8")
        assert "controller_cli" in text and "carry on" in text, role
    neteng = (app / "skills" / "network-engineer" / "SKILL.md").read_text(encoding="utf-8")
    assert "show devices <host> check" in neteng and "show devices <host> diff" in neteng
    assert "Quote the diff" in neteng
    noceng = (app / "skills" / "noc-engineer" / "SKILL.md").read_text(encoding="utf-8")
    assert "`show connections` belongs to every triage" in noceng
