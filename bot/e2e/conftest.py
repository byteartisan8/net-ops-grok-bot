"""grok-bot end to end on the running lab (``make bot-e2e``, also run by ``make e2e``): the three NOC roles, no cloud.

* **The routine sink** (container ``routine-sink``, 172.30.10.61:8096; ``make bot-e2e`` starts it) stands in for the
  three Grok Bot routines. For the session grok-bot pages it (``sink_url``), out of maintenance mode, pages unmuted,
  autonomy ``propose``; the previous control comes back at the end.
* **The scripted role player** (:class:`Role`) drives ``/mcp`` with the three role tokens as the app does
  (``initialize``, ``tools/list``, ``tools/call``) and learns about its work from its pages in the sink.
* Faults go in through lab-api (actor ``bot-e2e``). Zabbix decides who is paged: its service action posts the
  customer alerts to ``/hooks/zabbix``, grok-bot batches them and pages. The human's approvals go through the
  operator API.
* Every test starts with :func:`green`: reset, every incident recovered and closed by the NOC Engineer, the sink
  emptied. Timings are printed at the end and written to ``build/bot-e2e/timings.json``.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from netsim_bot.settings import NETENG, NOCENG, NOCMGR, load_settings

from netsim_common.paths import repo_root

BOT_URL = os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")
LAB_API_URL = os.environ.get("LAB_API_URL", "http://127.0.0.1:8090").rstrip("/")
SINK_URL = os.environ.get("SINK_URL", "http://127.0.0.1:8096").rstrip("/")  # from this host
SINK_ON_LABNET = "http://172.30.10.61:8096"  # from grok-bot (bot/compose.yaml)
TIMINGS: list[dict[str, Any]] = []
OUT = repo_root() / "build" / "bot-e2e"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "bot/e2e" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.e2e)


def record(path: str, value: float | None, budget: float | None = None, note: str = "") -> None:
    """One row of the timing summary (seconds)."""
    TIMINGS.append({"path": path, "value": None if value is None else round(value, 2), "budget": budget, "note": note})


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if not TIMINGS:
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "timings.json").write_text(json.dumps(TIMINGS, indent=2) + "\n", encoding="utf-8")
    tr = terminalreporter
    tr.section("timing summary (seconds)")
    tr.write_line(f"{'path':<64} {'measured':>9} {'budget':>7}  ok")
    by_path: dict[str, list[float]] = {}
    for row in TIMINGS:
        value, budget = row["value"], row["budget"]
        ok = "" if budget is None or value is None else ("yes" if value <= budget else "NO")
        shown = "-" if value is None else f"{value:.2f}"
        tr.write_line(f"{row['path'][:64]:<64} {shown:>9} {'' if budget is None else budget:>7}  {ok}  {row['note']}")
        if value is not None:
            by_path.setdefault(row["path"].split(" (")[0], []).append(value)
    for path, values in by_path.items():
        if len(values) > 1:
            tr.write_line(f"  {path}: median {statistics.median(values):.2f}, max {max(values):.2f}, n={len(values)}")
    tr.write_line(f"(also in {OUT / 'timings.json'})")


def wait_for(fn: Callable[[], Any], timeout: float, what: str, every: float = 1.0) -> Any:
    """Poll ``fn`` until it returns something truthy; fail naming ``what`` (and the last value) on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what} (last: {str(last)[:600]})")


# ---------------------------------------------------------------------------------------------- the operator
class Bot:
    """grok-bot's operator API (``X-Operator-Token``): what the human at the operator desk does."""

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def get(self, path: str, **kw: Any) -> Any:
        r = self.http.get(path, **kw)
        assert r.status_code == 200, f"GET {path}: HTTP {r.status_code} {r.text[:300]}"
        return r.json()

    def post(self, path: str, body: Any = None) -> Any:
        r = self.http.post(path, json=body)
        assert r.status_code == 200, f"POST {path}: HTTP {r.status_code} {r.text[:300]}"
        return r.json()

    def control(self, **kw: Any) -> dict[str, Any]:
        return self.post("/api/control", kw)

    def incident(self, incident_id: str) -> dict[str, Any]:
        return self.get(f"/api/incidents/{incident_id}")

    def incidents(self, active: bool = True) -> list[dict[str, Any]]:
        return self.get("/api/incidents", params={"active": active, "limit": 100})

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        return self.get(f"/api/proposals/{proposal_id}")

    def approve(self, proposal_id: str) -> dict[str, Any]:
        return self.post(f"/api/proposals/{proposal_id}/approve", {"comment": "bot-e2e"})

    def approve_all(self, incident_id: str) -> list[dict[str, Any]]:
        return self.post(f"/api/incidents/{incident_id}/approve-all", {"comment": "bot-e2e"})

    def until_status(self, incident_id: str, statuses: tuple[str, ...], timeout: float = 120) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            inc = self.incident(incident_id)
            return inc if inc["status"] in statuses else None

        return wait_for(check, timeout, f"{incident_id} to become {'/'.join(statuses)}")

    def until_decided(self, proposal_id: str, timeout: float = 150) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            p = self.proposal(proposal_id)
            return p if p["status"] in ("applied", "failed", "rejected", "obsolete") else None

        return wait_for(check, timeout, f"{proposal_id} applied or failed", every=0.5)

    def alerts_since(self, t: float) -> list[dict[str, Any]]:
        return [a for a in self.get("/api/alerts", params={"limit": 500}) if a["received"] >= t]


# ---------------------------------------------------------------------------------------------- the routines
class Sink:
    """The routine sink: every page grok-bot sent the three routines, as the routines would have received it."""

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def clear(self) -> None:
        assert self.http.delete("/pages").status_code == 200

    def pages(self, role: str | None = None, since: float = 0.0) -> list[dict[str, Any]]:
        rows = self.http.get("/pages", params={"role": role} if role else None).json()
        return [p for p in rows if p["received"] >= since]

    @staticmethod
    def customers(page: dict[str, Any]) -> list[str]:
        """The customers a page lists (its incident's, gold first)."""
        return [c["name"] for c in page["body"]["incident"]["customers"]]

    def events(self, since: float = 0.0) -> list[tuple[str, str, str | None]]:
        """``[(role, event, incident id)]``, oldest first: what was paged, in order."""
        return [
            (p["role"], p["body"].get("event"), (p["body"].get("incident") or {}).get("id"))
            for p in self.pages(since=since)
        ]

    def wait(
        self, role: str, event: str, since: float, timeout: float = 90, incident_id: str | None = None
    ) -> dict[str, Any]:
        """The first page ``event`` to ``role`` received after ``since`` (for ``incident_id``, if given)."""

        def find() -> dict[str, Any] | None:
            for p in self.pages(role, since):
                body = p["body"]
                if body.get("event") == event and (
                    incident_id is None or (body.get("incident") or {}).get("id") == incident_id
                ):
                    return p
            return None

        return wait_for(find, timeout, f"a {event} page to the {role} routine", every=0.5)


class Role:
    """One role's Grok Bot routine, scripted: its MCP connector (the role token) and its pages in the sink."""

    def __init__(self, http: httpx.Client, role: str, sink: Sink) -> None:
        self.http = http
        self.role = role
        self.sink = sink
        self.ids = 0
        self.instructions = ""
        self.tool_names: list[str] = []

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ids += 1
        body = {"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}}
        r = self.http.post("/mcp", json=body)
        assert r.status_code == 200, f"{self.role} {method}: HTTP {r.status_code} {r.text[:300]}"
        data = r.json()
        assert "error" not in data, f"{self.role} {method}: {data['error']}"
        return data["result"]

    def connect(self) -> None:
        """What the app does when a routine starts: initialize, initialized, tools/list."""
        init = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "bot-e2e-role-player", "version": "1"},
            },
        )
        self.instructions = init["instructions"]
        r = self.http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202
        self.tool_names = [t["name"] for t in self.rpc("tools/list")["tools"]]

    def call_raw(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        out = self.rpc("tools/call", {"name": tool, "arguments": args})
        return not out["isError"], out.get("structuredContent") or json.loads(out["content"][0]["text"])

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        ok, result = self.call_raw(tool, **args)
        assert ok, f"{self.role} {tool}: {result}"
        return result

    def refused(self, tool: str, **args: Any) -> str:
        ok, result = self.call_raw(tool, **args)
        assert not ok, f"{self.role} {tool} should have been refused: {result}"
        return str(result.get("error"))


# ---------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def lab_api() -> Iterator[httpx.Client]:
    s = load_settings()
    assert s.lab_api_token, "LAB_API_TOKEN missing in .env (make env)"
    headers = {"X-Lab-Token": s.lab_api_token, "X-Lab-Actor": "bot-e2e"}
    with httpx.Client(base_url=LAB_API_URL, timeout=300, headers=headers) as c:
        assert c.get("/health").status_code == 200, f"lab-api not answering at {LAB_API_URL} (make up)"
        yield c


@pytest.fixture(scope="session")
def sink() -> Iterator[Sink]:
    with httpx.Client(base_url=SINK_URL, timeout=10) as http:
        try:
            http.get("/health").raise_for_status()
        except httpx.HTTPError as exc:
            pytest.fail(f"no routine sink at {SINK_URL} (make bot-e2e starts it; or make bot-sink-up): {exc}")
        yield Sink(http)


@pytest.fixture(scope="session")
def bot(lab_api: httpx.Client, sink: Sink) -> Iterator[Bot]:
    s = load_settings()
    assert s.operator_token, "BOT_OPERATOR_TOKEN missing in .env (make env)"
    assert s.hook_token, "BOT_ZABBIX_HOOK_TOKEN missing in .env (make env, then make up: the bootstrap stores it)"
    headers = {"X-Operator-Token": s.operator_token, "X-Operator": "bot-e2e"}
    with httpx.Client(base_url=BOT_URL, timeout=60, headers=headers) as http:
        try:
            http.get("/api/health").raise_for_status()
        except httpx.HTTPError as exc:
            pytest.fail(f"grok-bot not answering at {BOT_URL} (make bot-up): {exc}")
        wait_for(lambda: http.get("/api/health").json()["lab_ok"], 60, "grok-bot reading lab-api (lab_ok)")
        b = Bot(http)
        before = b.get("/api/state")["control"]
        b.control(paused=False, pages_muted=False, autonomy="propose", sink_url=SINK_ON_LABNET)
        try:
            yield b
        finally:
            b.control(
                paused=bool(before.get("paused")),
                reason=before.get("reason"),
                pages_muted=bool(before.get("pages_muted")),
                autonomy=before.get("autonomy") or "default",
                sink_url=SINK_ON_LABNET if before.get("sink_url") else "",
            )


@pytest.fixture(scope="session")
def roles(bot: Bot, sink: Sink) -> Iterator[dict[str, Role]]:
    """The three scripted routines, connected: ``roles[NOCENG]``, ``roles[NETENG]``, ``roles[NOCMGR]``."""
    s = load_settings()
    clients = []
    out: dict[str, Role] = {}
    for role in (NOCENG, NETENG, NOCMGR):
        token = s.role_tokens.get(role)
        assert token, f"no {role} token in .env (make env)"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
        http = httpx.Client(base_url=BOT_URL, timeout=330, headers=headers)
        clients.append(http)
        out[role] = Role(http, role, sink)
        out[role].connect()
    try:
        yield out
    finally:
        for http in clients:
            http.close()


@pytest.fixture(scope="session")
def zabbix_admin() -> Any:
    """Zabbix as Super admin (``ZABBIX_ADMIN_PASSWORD``), for what a test checks or tunes beyond the NOC's reads."""
    from netsim_zabbix.api import ZabbixApi
    from netsim_zabbix.settings import load_settings as zabbix_settings

    zs = zabbix_settings()
    assert zs.admin_password, "ZABBIX_ADMIN_PASSWORD missing in .env"
    api = ZabbixApi(zs.url, timeout=30)
    api.login("Admin", zs.admin_password)
    return api


@pytest.fixture
def green(lab_api: httpx.Client, bot: Bot, roles: dict[str, Role], sink: Sink) -> Callable[[], None]:
    """Reset the lab (lab-api waits until simd and Zabbix are green); every incident recovers and the NOC Engineer
    closes it; the sink starts empty."""

    def run() -> None:
        r = lab_api.post("/reset", json={"wait": True, "timeout_s": 240})
        assert r.status_code == 200 and r.json().get("green"), f"reset not green: {r.text[:400]}"

        def settled() -> bool:
            active = bot.incidents()
            for inc in active:
                if inc["status"] == "recovered":
                    roles[NOCENG].call("close_incident", incident_id=inc["id"], summary="bot-e2e: lab reset")
            return not [i for i in active if i["status"] != "recovered"] and not bot.incidents()

        wait_for(settled, 150, "every incident recovered and closed", every=2)
        sink.clear()

    return run


@pytest.fixture
def fault(lab_api: httpx.Client) -> Callable[..., float]:
    """``fault(kind, host, ...)``: inject a physical fault through lab-api; returns the time just before."""

    def run(kind: str, host: str, action: str = "inject", **kw: Any) -> float:
        t0 = time.time()
        r = lab_api.post("/faults", json={"kind": kind, "host": host, "action": action, **kw})
        assert r.status_code == 200, f"{kind} {host} {action}: {r.text[:300]}"
        return t0

    return run


@pytest.fixture
def scenario(lab_api: httpx.Client) -> Callable[[str], float]:
    """Apply a scenario's main steps through lab-api; returns the time just before."""

    def run(name: str) -> float:
        t0 = time.time()
        r = lab_api.post(f"/scenarios/{name}/apply")
        assert r.status_code == 200, f"{name}: {r.text[:300]}"
        return t0

    return run


@pytest.fixture
def timing() -> Callable[..., None]:
    """``timing(path, seconds, budget=None, note="")``: one row of the timing summary; over its budget, the test
    fails (after the row is recorded, so the summary shows it)."""

    def run(path: str, value: float | None, budget: float | None = None, note: str = "") -> None:
        record(path, value, budget, note)
        if budget is not None:
            assert value is not None and value <= budget, f"{path}: {value} s, budget {budget} s"

    return run


@pytest.fixture
def wait() -> Callable[..., Any]:
    """``wait(fn, timeout, what)``: poll ``fn`` until it returns something truthy."""
    return wait_for


@pytest.fixture
def zread(lab_api: httpx.Client) -> Callable[[str, dict[str, Any]], Any]:
    """``zread(method, params)``: a direct Zabbix read through lab-api, as the roles' ``zabbix_get`` makes it."""

    def run(method: str, params: dict[str, Any]) -> Any:
        r = lab_api.post("/zabbix/api", json={"method": method, "params": params})
        assert r.status_code == 200, f"{method}: {r.text[:300]}"
        return r.json()["result"]

    return run
