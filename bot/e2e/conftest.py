"""grok-bot end to end on the running lab (``make bot-e2e``, also run by ``make e2e``).

The bot under test is the ``grok-bot`` container. For a deterministic run the session switches it to the rules brain
and propose autonomy (runtime override, restored at the end), out of maintenance mode, with the pager muted (no paid
agent runs for test incidents). Scenarios are applied through
lab-api (actor ``bot-e2e``); the bot must notice the Zabbix problems on its own, diagnose, propose, and after this
suite's approval fix and verify. Every test starts and ends with a reset to a green lab.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from netsim_bot.settings import load_settings

BOT_URL = os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "bot/e2e" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.e2e)


def wait_for(fn: Callable[[], Any], timeout: float, what: str, every: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what} (last: {last!r})")


class Bot:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def get(self, path: str, **kw: Any) -> Any:
        r = self.http.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: Any = None) -> Any:
        r = self.http.post(path, json=json)
        assert r.status_code == 200, f"POST {path}: HTTP {r.status_code} {r.text[:300]}"
        return r.json()

    def control(self, **kw: Any) -> dict[str, Any]:
        return self.post("/api/control", kw)

    def incident(self, incident_id: str) -> dict[str, Any]:
        return self.get(f"/api/incidents/{incident_id}")

    def new_incident(self, since: float, timeout: float = 120) -> dict[str, Any]:
        def find() -> dict[str, Any] | None:
            rows = [i for i in self.get("/api/incidents", params={"limit": 20}) if i["created"] >= since]
            return rows[-1] if rows else None

        return wait_for(find, timeout, "the bot to open an incident")

    def until_status(self, incident_id: str, statuses: tuple[str, ...], timeout: float = 120) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            inc = self.incident(incident_id)
            return inc if inc["status"] in statuses else None

        return wait_for(check, timeout, f"{incident_id} to become {'/'.join(statuses)}")

    def quiet(self, timeout: float = 90) -> None:
        """No active incident (after a reset every incident resolves within the resolve hold)."""
        wait_for(lambda: not self.get("/api/incidents", params={"active": True}), timeout, "every incident resolved")


@pytest.fixture(scope="session")
def lab_api() -> Iterator[httpx.Client]:
    s = load_settings()
    url = os.environ.get("LAB_API_URL", "http://127.0.0.1:8090")
    assert s.lab_api_token, "LAB_API_TOKEN missing in .env (make env)"
    with httpx.Client(
        base_url=url, timeout=300, headers={"X-Lab-Token": s.lab_api_token, "X-Lab-Actor": "bot-e2e"}
    ) as c:
        assert c.get("/health").status_code == 200, f"lab-api not answering at {url} (make up)"
        yield c


@pytest.fixture(scope="session")
def bot(lab_api: httpx.Client) -> Iterator[Bot]:
    s = load_settings()
    assert s.operator_token, "BOT_OPERATOR_TOKEN missing in .env (make env)"
    headers = {"X-Operator-Token": s.operator_token, "X-Operator": "bot-e2e"}
    with httpx.Client(base_url=BOT_URL, timeout=60, headers=headers) as http:
        try:
            health = http.get("/api/health").json()
        except httpx.HTTPError as exc:
            pytest.fail(f"grok-bot not answering at {BOT_URL} (make bot-up): {exc}")
        assert health["lab_ok"], f"grok-bot cannot read lab-api: {health['lab_error']}"
        b = Bot(http)
        before = b.get("/api/state")["control"]
        b.control(brain="rules", autonomy="propose", paused=False, pager_muted=True)
        try:
            yield b
        finally:
            b.control(
                brain=before.get("brain") or "default",
                autonomy=before.get("autonomy") or "default",
                paused=bool(before.get("paused")),
                reason=before.get("reason"),
                pager_muted=bool(before.get("pager_muted")),
            )


@pytest.fixture(scope="session")
def agent() -> Iterator[Callable[..., Any]]:
    """``agent(method, params)``: one MCP call to ``/mcp`` with ``BOT_AGENT_TOKEN``, as the Grok Bot app makes it."""
    s = load_settings()
    assert s.agent_token, "BOT_AGENT_TOKEN missing in .env (make env)"
    headers = {"Authorization": f"Bearer {s.agent_token}", "Accept": "application/json, text/event-stream"}
    with httpx.Client(base_url=BOT_URL, timeout=330, headers=headers) as http:

        def call(method: str, params: dict[str, Any] | None = None) -> Any:
            r = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
            assert r.status_code == 200, f"{method}: HTTP {r.status_code} {r.text[:300]}"
            return r.json()["result"]

        yield call


@pytest.fixture
def green(lab_api: httpx.Client, bot: Bot) -> Callable[[], None]:
    """Reset the lab (lab-api waits until simd and Zabbix are green), then wait until the bot has no open incident."""

    def run() -> None:
        r = lab_api.post("/reset", json={"wait": True, "timeout_s": 240})
        assert r.status_code == 200 and r.json().get("green"), f"reset not green: {r.text[:400]}"
        bot.quiet()

    return run


@pytest.fixture
def scenario(lab_api: httpx.Client) -> Callable[[str], float]:
    """Apply a scenario's main steps through lab-api; returns the time just before."""

    def run(name: str) -> float:
        t0 = time.time() - 1
        r = lab_api.post(f"/scenarios/{name}/apply")
        assert r.status_code == 200, f"{name}: {r.text[:300]}"
        return t0

    return run


@pytest.fixture
def wait() -> Callable[..., Any]:
    """``wait(fn, timeout, what)``: poll ``fn`` until it returns something truthy."""
    return wait_for
