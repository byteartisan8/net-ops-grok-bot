"""The controller on the running lab (``make controller-test``, also run by ``make e2e``): fixtures and helpers.

The tests drive the lab like the other suites (reset, scenarios through lab-api) and look at the controller three
ways: through lab-api (``POST /controller/cli``, ``GET /controller/status``: wall 1 and the NOC's view), through an
SSH session of a controller user made from lab-api's container with lab-api's key but without lab-api's allow-list
(wall 2 on its own), and as root inside the controller (the recovery user: what the controller itself sees).
grok-bot is in maintenance mode with pages muted for the run (the faults page nobody), as in lab-e2e.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from netsim_lab.lab import Lab
from netsim_lab.reset import reset

CONTROLLER = "172.30.10.70"
LAB_API_KEY = "/var/lib/lab-api/controller"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "controller/tests/integration" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integration)


def wait_for(fn: Callable[[], Any], timeout: float, what: str, every: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what} (last: {str(last)[:600]})")


@pytest.fixture(scope="session", autouse=True)
def noc_bot_paused() -> Iterator[None]:
    """Maintenance mode and muted pages on grok-bot for the run, the previous mode afterwards (as lab-e2e does)."""
    from netsim_netbox.envfile import default_env_file, read_env_file

    url = os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")
    token = os.environ.get("BOT_OPERATOR_TOKEN") or read_env_file(default_env_file()).get("BOT_OPERATOR_TOKEN", "")
    headers = {"X-Operator-Token": token, "X-Operator": "controller-test"}
    try:
        control = httpx.get(f"{url}/api/state", timeout=5).json()["control"]
        was_paused, was_muted = control["paused"], bool(control.get("pages_muted"))
        r = httpx.post(
            f"{url}/api/control",
            json={"paused": True, "reason": "controller test run", "pages_muted": True},
            headers=headers,
            timeout=10,
        )
    except (httpx.HTTPError, ValueError, KeyError):
        yield
        return
    if r.status_code != 200:
        pytest.fail(f"grok-bot at {url} refused maintenance mode (HTTP {r.status_code}): check BOT_OPERATOR_TOKEN")
    try:
        yield
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(
                f"{url}/api/control",
                json={"pages_muted": was_muted} | ({} if was_paused else {"paused": False}),
                headers=headers,
                timeout=30,
            )


@pytest.fixture(scope="session")
def lab() -> Iterator[Lab]:
    with Lab() as lab:
        assert httpx.get(f"{lab.settings.lab_api_url}/health", timeout=5).status_code == 200, "lab-api is not up"
        green(lab)
        yield lab
        green(lab)


def green(lab: Lab) -> dict[str, Any]:
    result = reset(lab, log=None)
    assert result.get("green"), f"not green after reset: {result.get('wait', {}).get('why')}"
    assert "error" not in (result.get("controller") or {}), result.get("controller")
    return result


@pytest.fixture(scope="session")
def lab_api(lab: Lab) -> Iterator[httpx.Client]:
    token = lab.settings.lab_api_token
    assert token, "LAB_API_TOKEN missing in .env (make env)"
    headers = {"X-Lab-Token": token, "X-Lab-Actor": "controller-test"}
    with httpx.Client(base_url=lab.settings.lab_api_url, timeout=120, headers=headers) as client:
        yield client


@pytest.fixture
def cli(lab_api: httpx.Client) -> Callable[..., httpx.Response]:
    """``cli(role, command)``: ``POST /controller/cli`` (wall 1, then the role's controller user)."""

    def run(role: str, command: str) -> httpx.Response:
        return lab_api.post("/controller/cli", json={"role": role, "command": command})

    return run


@pytest.fixture
def status(lab_api: httpx.Client) -> Callable[[], dict[str, Any]]:
    def run() -> dict[str, Any]:
        r = lab_api.get("/controller/status", params={"max_age": 0})
        assert r.status_code == 200, r.text
        return r.json()

    return run


def docker(*args: str, stdin: str = "", timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def ssh_as() -> Callable[..., subprocess.CompletedProcess[str]]:
    """``ssh_as(user, *lines, remote=None)``: an SSH session to the controller from lab-api's container with lab-api's
    key, as ``user``, the lines on stdin: no allow-list in between, only the controller's own rules."""

    def run(user: str, *lines: str, remote: str | None = None) -> subprocess.CompletedProcess[str]:
        argv = [
            "exec",
            "-i",
            "lab-api",
            "ssh",
            "-T",
            "-F",
            "/dev/null",
            "-i",
            f"{LAB_API_KEY}/id_ed25519",
            "-o",
            f"UserKnownHostsFile={LAB_API_KEY}/known_hosts",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            f"{user}@{CONTROLLER}",
        ]
        if remote:
            argv.append(remote)
        return docker(*argv, stdin="".join(f"{line}\n" for line in lines))

    return run


@pytest.fixture
def run_docker() -> Callable[..., subprocess.CompletedProcess[str]]:
    """``run_docker(*args, stdin="")``: the docker CLI."""
    return docker


@pytest.fixture
def wait() -> Callable[..., Any]:
    """``wait(fn, timeout, what)``: poll ``fn`` until it returns something truthy."""
    return wait_for


@pytest.fixture
def reset_green(lab: Lab) -> Callable[[], dict[str, Any]]:
    """Reset the lab (golden, faults cleared, the controller resynced) and assert it is green."""
    return lambda: green(lab)


@pytest.fixture
def root_cli() -> Callable[..., str]:
    """The controller CLI as root inside the container (the NACM recovery user): what the controller itself sees."""

    def run(*command: str) -> str:
        r = docker("exec", "controller", "clixon_cli", "-1", "-f", "/etc/clixon/controller.xml", *command)
        return r.stdout + r.stderr

    return run
