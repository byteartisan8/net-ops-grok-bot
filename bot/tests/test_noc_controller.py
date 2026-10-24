"""``noc-controller``, the roles' terminal on a Bot's own computer (``bot/grokbot-app/terminal/``), run as the Bot runs
it: a subprocess with only a config in its home. Against a fake ``/mcp`` (every answer and failure on the way, the exit
codes, the configs) and against grok-bot itself (the terminal token, the transcript). The token never shows."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from netsim_bot.api.app import create_app
from netsim_bot.engine import Engine
from netsim_bot.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "grokbot-app" / "terminal" / "noc-controller"
SECRET = "tt-0f5ec4e7-netEng"  # the fake terminal tokens
OTHER = "tt-91d2aa08-nocMgr"
REFUSAL = (
    "refused: not an allowed command: 'commit push'. Allowed commands (one per call): show connections; show "
    "connections <router>; show connections <router> detail; show devices <router> check; show devices <router> diff; "
    "show configuration devices device <router> config; show configuration devices device <router> config <path>. "
    "<router>: hub-a, hub-b, spoke-1. <path>: a subtree of the router's config, e.g. 'l2vpn xconnect SVC-0001'."
)
DIFF = 'spoke-1:\n         pseudowire backup {\n-           vc-id "2010";\n+           vc-id "2001";\n         }'


def result(structured: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    """A ``tools/call`` answer as grok-bot's /mcp sends it."""
    text = json.dumps(structured)
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": is_error,
                                                  "structuredContent": structured}}  # fmt: skip


def answered(command: str, output: str, user: str = "network-engineer", **extra: Any) -> dict[str, Any]:
    body = {"role": "network-engineer", "user": user, "command": command, "allowed": True, "output": output}
    return result({**body, "truncated": False, "duration_ms": 120, **extra})


class FakeMcp:
    """grok-bot's /mcp as the command sees it: ``reply`` is what it answers next; ``requests`` what it got."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: tuple[int, Any, dict[str, str]] = (200, answered("show connections", "hub-a  OPEN"), {})
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
                status, payload, headers = fake.reply
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in {"Content-Type": "application/json", **headers}.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"


@pytest.fixture
def mcp() -> Iterator[FakeMcp]:
    fake = FakeMcp()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def install(home: Path, role: str, url: str, token: str = SECRET, mode: int = 0o600, text: str | None = None) -> Path:
    path = home / ".config" / "noc-controller" / f"{role}.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else f"url={url}\ntoken={token}\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def run(home: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """``run(*args)``: the command as a Bot runs it (its home, no tty); no output may ever hold a token."""

    def go(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", **(env or {})}
        r = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
        for token in (SECRET, OTHER, "neteng-terminal"):
            assert token not in r.stdout + r.stderr, (args, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr
        return r

    return go


# ---------------------------------------------------------------------------------------------- the answers
def test_an_answer_prints_like_the_cli(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    mcp.reply = (200, answered("show devices spoke-1 diff", DIFF, user="lab-api"), {})
    r = run("show", "devices", "spoke-1", "diff")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "lab-api@controller> show devices spoke-1 diff"  # the line the room's CRT types
    assert lines[1:-1] == DIFF.splitlines()
    assert lines[-1].startswith("read only · as lab-api · ") and lines[-1].endswith(" s")
    assert "\033[" not in r.stdout  # no colour without a terminal
    [request] = mcp.requests
    assert request["path"] == "/mcp" and request["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["body"] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "controller_cli", "arguments": {"command": "show devices spoke-1 diff"}},
    }


def test_an_empty_answer_says_what_it_means(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    mcp.reply = (200, answered("show devices spoke-1 check", "", user="lab-api", summary="in sync"), {})
    r = run("show devices spoke-1 check")  # one argument or several: the same command
    assert r.returncode == 0
    assert r.stdout.splitlines()[0] == "lab-api@controller> show devices spoke-1 check"
    assert r.stdout.splitlines()[1].endswith(" · in sync")


def test_a_refusal_lists_the_allowed_commands(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    refused = {"role": "network-engineer", "user": None, "command": "commit push", "allowed": False}
    mcp.reply = (200, result({"error": REFUSAL, **refused, "output": REFUSAL}, is_error=True), {})
    r = run("commit", "push")
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert lines[0] == "network-engineer@controller> commit push"
    assert lines[1] == "refused: not an allowed command: 'commit push'."
    assert lines[2] == "Allowed commands (one per call):" and "  show devices <router> diff" in lines
    assert "<router>: hub-a, hub-b, spoke-1" in lines
    assert lines[-1].startswith("refused by lab-api's allow-list · nothing reached the controller")


def test_an_unavailable_controller_says_carry_on(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    down = "the controller is unavailable (ssh: connect to host 172.30.10.70 port 22: No route to host): carry on "
    down += "without it (get_drift, get_device_running_config, get_events)"
    body = {"error": down, "available": False, "role": "network-engineer", "user": "network-engineer"}
    mcp.reply = (200, result({**body, "command": "show connections", "allowed": True}, is_error=True), {})
    r = run("show", "connections")
    assert r.returncode == 2
    assert r.stdout.splitlines()[1] == down
    assert r.stdout.splitlines()[-1].startswith("controller unavailable · carry on without it")
    mcp.reply = (200, result({"error": "lab-api: HTTP 500"}, is_error=True), {})  # any other failure of the tool
    assert run("show", "connections").returncode == 2


# ---------------------------------------------------------------------------------------------- on the way there
@pytest.mark.parametrize(
    ("status", "body", "says"),
    [
        (401, {"error": "missing or wrong Authorization"}, "grok-bot refused the token (HTTP 401)"),
        (404, {"error": "only /mcp is published on this host"}, "no /mcp at"),
        (400, b"Invalid host header", "refused the host name"),
        (502, b"<html><body>Bad gateway</body></html>", "answered HTTP 502"),
        (530, b"<html>error 1033</html>", "answered HTTP 530"),
        (200, b"<html>a captive portal</html>", "did not answer like grok-bot's /mcp"),
        (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}, "grok-bot: nope"),
    ],
)
def test_what_goes_wrong_on_the_way_is_exit_3_with_a_hint(mcp, home, run, status: int, body: Any, says: str) -> None:
    install(home, "network-engineer", mcp.url)
    mcp.reply = (status, body, {})
    r = run("show", "connections")
    assert r.returncode == 3 and r.stdout == ""
    assert says in r.stderr and "\nhint: " in r.stderr
    assert "<html>" not in r.stderr  # a proxy's page is not printed


def test_a_redirect_is_not_followed(mcp: FakeMcp, home: Path, run) -> None:
    elsewhere = FakeMcp()
    try:
        install(home, "network-engineer", mcp.url)
        mcp.reply = (307, b"", {"Location": elsewhere.url})
        r = run("show", "connections")
        assert r.returncode == 3 and "redirects (HTTP 307)" in r.stderr
        assert elsewhere.requests == []  # the token went nowhere else
    finally:
        elsewhere.server.shutdown()


def test_nothing_listening_is_exit_3(home: Path, run) -> None:
    install(home, "network-engineer", "http://127.0.0.1:9/mcp")
    r = run("show", "connections")
    assert r.returncode == 3 and "cannot reach http://127.0.0.1:9/mcp" in r.stderr and "controller_cli" in r.stderr


def test_the_token_is_never_printed(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    mcp.reply = (404, {"error": f"nothing here for {SECRET}"}, {})  # a server echoing it back
    r = run("show", "connections")
    assert r.returncode == 3 and "nothing here for ***" in r.stderr
    mcp.reply = (200, answered("show connections", f"echo {SECRET}"), {})
    assert run("show", "connections").stdout.splitlines()[1] == "echo ***"
    mcp.reply = (200, answered("show connections", "", summary=5), {})  # an answer the command chokes on
    r = run("show", "connections")
    assert r.returncode == 3 and "unexpected error: TypeError" in r.stderr  # one line, no traceback
    assert run("--help").returncode == 0  # the help names the config file, never what is in it


def test_an_answer_cannot_drive_the_terminal(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    mcp.reply = (200, answered("show connections", "hub-a  OPEN\x1b[2J\x1b]0;owned\x07 \x9b31m"), {})
    r = run("show", "connections")
    assert r.returncode == 0 and r.stdout.splitlines()[1] == "hub-a  OPEN[2J]0;owned 31m"  # ESC, BEL, CSI dropped


def test_a_reader_that_goes_away_keeps_the_exit_code(mcp: FakeMcp, home: Path) -> None:
    """``noc-controller ... | head -1``: the pipe closes before the output is written; no error, the real exit code."""
    install(home, "network-engineer", mcp.url)
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    for args, code in ((["--help"], 0), (["show", "connections"], 0), (["commit", "push"], 1)):
        if args == ["commit", "push"]:
            refused = {"role": "network-engineer", "user": None, "command": "commit push", "allowed": False}
            mcp.reply = (200, result({"error": REFUSAL, **refused, "output": REFUSAL}, is_error=True), {})
        read_end, write_end = os.pipe()
        os.close(read_end)  # the reader is gone before the first line
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPT), *args], env=env, stdout=write_end, stderr=subprocess.PIPE, timeout=30
            )
        finally:
            os.close(write_end)
        assert (r.returncode, r.stderr) == (code, b""), (args, r.stderr)


def test_a_token_of_another_role_is_noted(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    body = {"role": "noc-manager", "user": "noc-manager", "command": "show connections", "allowed": True, "output": ""}
    mcp.reply = (200, result(body), {})
    r = run("show", "connections")
    assert r.returncode == 0 and "this token is the NOC Manager's, not the Network Engineer's" in r.stderr


# ---------------------------------------------------------------------------------------------- configs and usage
def test_which_config_is_used(mcp: FakeMcp, home: Path, run) -> None:
    r = run("show", "connections")
    assert r.returncode == 3 and "not set up on this computer" in r.stderr and "terminal-setup" in r.stderr
    install(home, "network-engineer", mcp.url)
    assert run("show", "connections").returncode == 0  # the only one: used
    install(home, "noc-manager", mcp.url, token=OTHER)
    r = run("show", "connections")
    assert r.returncode == 3 and "several roles are set up here: network-engineer, noc-manager" in r.stderr
    for args, token in (
        (["--role", "noc-manager"], OTHER),
        (["--role=network-engineer"], SECRET),
        (["-r", "NETENG"], SECRET),
        (["--role", "nocmgr", "--"], OTHER),
    ):
        assert run(*args, "show", "connections").returncode == 0, args
        assert mcp.requests[-1]["headers"]["Authorization"] == f"Bearer {token}", args
    assert run("show", "connections", "--role", "noc-manager").returncode == 0  # an option after the words, too
    assert mcp.requests[-1]["headers"]["Authorization"] == f"Bearer {OTHER}"
    assert mcp.requests[-1]["body"]["params"]["arguments"] == {"command": "show connections"}
    r = run("--role", "noc-engineer", "show", "connections")
    assert r.returncode == 3 and "set up here: network-engineer, noc-manager" in r.stderr
    r = run("--role", "boss", "show", "connections")
    assert r.returncode == 3 and "unknown role 'boss'" in r.stderr


def test_xdg_config_home_is_where_the_config_is(mcp: FakeMcp, home: Path, run, tmp_path: Path) -> None:
    install(tmp_path / "xdg-parent", "network-engineer", mcp.url)  # <XDG_CONFIG_HOME>/noc-controller/...
    xdg = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-parent" / ".config")}
    assert run("show", "connections", env=xdg).returncode == 0
    assert run("show", "connections").returncode == 3  # not in ~/.config


@pytest.mark.parametrize(
    ("mode", "text", "says"),
    [
        (0o644, None, "can be read by other users"),
        (0o600, "url=https://h/mcp\ntoken=<BOT_NETENG_TERMINAL_TOKEN from .env>\n", "still the placeholder"),
        (0o600, f"token={SECRET}\n", "has no url="),
        (0o600, "url=https://h/mcp\n", "has no token= line"),
    ],
)
def test_a_bad_config_is_exit_3(home: Path, run, mode: int, text: str | None, says: str) -> None:
    install(home, "network-engineer", "https://h/mcp", mode=mode, text=text)
    r = run("show", "connections")
    assert r.returncode == 3 and says in r.stderr and "hint: " in r.stderr


def test_usage_and_help(mcp: FakeMcp, home: Path, run) -> None:
    install(home, "network-engineer", mcp.url)
    for args in ([], ["--role", "network-engineer"], ["--bogus", "show"], ["--role"]):
        r = run(*args)
        assert r.returncode == 3 and r.stderr.startswith("noc-controller: "), args
    assert mcp.requests == []
    r = run("--help")
    assert r.returncode == 0 and r.stdout.startswith("usage: noc-controller [--role <role>] <controller command>")
    assert "  show devices <router> diff" in r.stdout
    assert "  show configuration devices device <router> config [<path>]" in r.stdout
    assert f"Config: {home}/.config/noc-controller/<role>.conf" in r.stdout
    assert "Set up here: network-engineer." in r.stdout
    assert "Exit codes: 0 answered, 1 refused, 2 controller unavailable" in r.stdout
    assert run("--version").stdout.startswith("noc-controller ")


def test_the_help_lists_what_lab_api_allows() -> None:
    lab = pytest.importorskip("netsim_lab.controller")
    loader = importlib.machinery.SourceFileLoader("noc_controller", str(SCRIPT))
    spec = importlib.util.spec_from_loader("noc_controller", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    expanded = set()
    for command in module.ALLOWED:
        base, _, optional = command.partition(" [")
        expanded |= {base, f"{base} {optional.rstrip(']')}"} if optional else {base}
    assert expanded == set(lab.ALLOWED)


def test_it_is_one_python_file_of_the_standard_library() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3\n") and os.access(SCRIPT, os.X_OK)
    imports = {line.split()[1].split(".")[0] for line in text.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "http", "json", "os", "sys", "time", "urllib", "pathlib"}, imports


# ---------------------------------------------------------------------------------------------- against grok-bot
@pytest.fixture
def grok_bot(fake_lab, settings_factory, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """grok-bot's app on a real port (uvicorn in a thread), lab-api faked: the command's real other end."""
    import uvicorn

    def controller(request: httpx.Request) -> tuple[int, Any]:
        body = json.loads(request.content)
        if body["command"] == "commit push":
            detail = {"role": body["role"], "user": None, "command": body["command"], "allowed": False}
            return 403, {"detail": {**detail, "output": REFUSAL, "truncated": False, "duration_ms": 0}}
        user = "lab-api" if body["command"].endswith("check") else body["role"]
        return 200, {**body, "user": user, "allowed": True, "output": "", "truncated": False, "duration_ms": 80}

    s = settings_factory(BOT_NOCMGR_TERMINAL_TOKEN="")
    lab = fake_lab("healthy")
    lab.set("POST /controller/cli", controller)
    eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())
    app = create_app(engine=eng, start_engine=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield SimpleNamespace(engine=eng, lab=lab, url=f"http://127.0.0.1:{port}/mcp")
    finally:
        server.should_exit = True
        thread.join(10)


def test_the_command_against_grok_bot(grok_bot: SimpleNamespace, home: Path, run) -> None:
    install(home, "network-engineer", grok_bot.url, token="neteng-terminal")
    after = grok_bot.engine.bus.last_id
    r = run("show", "devices", "spoke-1", "check")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "lab-api@controller> show devices spoke-1 check"
    assert len(lines) == 2 and lines[1].startswith("read only · as lab-api") and lines[1].endswith(" · in sync")
    r = run("commit", "push")
    assert r.returncode == 1 and "Allowed commands (one per call):" in r.stdout
    calls = [(body["role"], body["command"], h["x-lab-actor"]) for _p, body, h in grok_bot.lab.posts("/controller/cli")]
    assert calls == [
        ("network-engineer", "show devices spoke-1 check", "grok-bot/NETENG"),
        ("network-engineer", "commit push", "grok-bot/NETENG"),
    ]
    events = [e["data"] for e in grok_bot.engine.bus.recent(after) if e["type"] == "terminal"]
    assert [(e["bot"], e["command"], e["allowed"], e["via"]) for e in events] == [
        ("NETENG", "show devices spoke-1 check", True, "terminal"),
        ("NETENG", "commit push", False, "terminal"),
    ]
    install(home, "network-engineer", grok_bot.url, token="nocmgr-terminal")  # the NOC Manager's terminal is off
    r = run("show", "connections")
    assert r.returncode == 3 and "HTTP 401" in r.stderr
