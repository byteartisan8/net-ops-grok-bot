"""End-to-end tests on the running lab (``make e2e``): fixtures, helpers and the timing summary.

Files run in name order: API -> every scenario -> timings (traps, polling only) -> status --watch -> persistence
(``make down && make up``) -> final green check. Every scenario test starts and ends with ``reset`` and asserts green.
Timings are printed as a table at the end and written to ``build/lab-e2e/timings.json``.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from netsim_lab.lab import Lab
from netsim_lab.reset import reset
from netsim_lab.scenarios import Scenario, load_scenarios

from netsim_common.paths import repo_root

TIMINGS: list[dict[str, Any]] = []
OUT = repo_root() / "build" / "lab-e2e"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "lab/e2e" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.e2e)


def record(path: str, value: float | list[float] | None, budget: float | None = None, note: str = "") -> None:
    """One row of the timing summary (``value`` in seconds; a list is summarized as median/max)."""
    TIMINGS.append({"path": path, "value": value, "budget": budget, "note": note})


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if not TIMINGS:
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "timings.json").write_text(json.dumps(TIMINGS, indent=2) + "\n", encoding="utf-8")
    tr = terminalreporter
    tr.section("timing summary (seconds)")
    tr.write_line(f"{'path':<64} {'measured':>20} {'budget':>8}  ok")
    for row in TIMINGS:
        value = row["value"]
        if isinstance(value, list):
            shown = f"med {statistics.median(value):.2f} max {max(value):.2f} n={len(value)}" if value else "-"
            check = max(value) if value else None
        else:
            shown = "-" if value is None else f"{value:.2f}"
            check = value
        budget = row["budget"]
        ok = "" if budget is None or check is None else ("yes" if check <= budget else "NO")
        tr.write_line(f"{row['path'][:64]:<64} {shown:>20} {'' if budget is None else budget:>8}  {ok}  {row['note']}")
    tr.write_line(f"(also in {OUT / 'timings.json'})")


@pytest.fixture(scope="session")
def lab() -> Iterator[Lab]:
    with Lab() as lab:
        api = httpx.get(f"{lab.settings.lab_api_url}/health", timeout=5)
        assert api.status_code == 200, f"lab-api not answering at {lab.settings.lab_api_url} (make up)"
        start = time.monotonic()
        result = reset(lab, log=None)
        assert result.get("green"), f"the lab is not green before the e2e run: {result.get('wait')}"
        record("initial reset -> green", time.monotonic() - start)
        yield lab


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Scenario]:
    from netsim_lab.settings import load_settings

    return load_scenarios(load_settings().scenarios_dir)


@pytest.fixture(scope="session")
def lab_api(lab: Lab) -> Iterator[httpx.Client]:
    token = lab.settings.lab_api_token
    assert token, "LAB_API_TOKEN missing in .env (make env)"
    with httpx.Client(
        base_url=lab.settings.lab_api_url, timeout=180, headers={"X-Lab-Token": token, "X-Lab-Actor": "e2e"}
    ) as client:
        yield client


def _reset_green(lab: Lab, what: str) -> dict[str, Any]:
    result = reset(lab, log=None)
    assert result.get("green"), f"not green after reset ({what}): {result.get('wait', {}).get('why')}"
    return result


@pytest.fixture(scope="session")
def timing():
    """``timing(path, value, budget=None, note="")``: a row of the timing summary."""
    return record


@pytest.fixture(scope="session")
def green(lab: Lab):
    """``green(what)``: reset the lab and assert it is green (simd and Zabbix)."""
    return lambda what: _reset_green(lab, what)
