"""Bring-up helpers: wait until every compose service is running and healthy, with clear errors.

``make seed``, ``make reset`` and ``make e2e`` call this first (``labctl wait-healthy``), so ``make up && make seed``
works on a clean host where NetBox needs a few minutes for its first start (migrations) and Zabbix creates its
database schema. On timeout it names every container that is not ready, with its state and last log lines.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerState:
    service: str
    name: str
    state: str
    health: str

    @property
    def ready(self) -> bool:
        return self.state == "running" and self.health in ("", "healthy")

    def describe(self) -> str:
        return f"{self.service} ({self.state}{', ' + self.health if self.health else ''})"


class BringupError(RuntimeError):
    pass


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as exc:
        raise BringupError("docker not found") from exc
    except subprocess.CalledProcessError as exc:
        raise BringupError(f"{' '.join(args)}: {exc.stderr.strip() or exc}") from exc


def _compose() -> list[str]:
    from netsim_common.paths import repo_root

    return ["docker", "compose", "-f", str(repo_root() / "compose.yaml")]


def compose_services() -> list[str]:
    return [s for s in _run([*_compose(), "config", "--services"]).split() if s]


def compose_ps() -> dict[str, ContainerState]:
    """``{service: state}`` from ``docker compose ps --all --format json`` (NDJSON or a JSON array)."""
    text = _run([*_compose(), "ps", "--all", "--format", "json"]).strip()
    if not text:
        return {}
    rows = (
        json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    )
    return {
        r["Service"]: ContainerState(r["Service"], r.get("Name", ""), r.get("State", ""), r.get("Health", "") or "")
        for r in rows
    }


def wait_healthy(timeout: float = 600.0, interval: float = 3.0, log: Callable[[str], None] = print) -> float:
    """Wait until every service of the root compose project is running (and healthy, if it has a healthcheck)."""
    services = compose_services()
    start = time.monotonic()
    last_report = 0.0
    while True:
        states = compose_ps()
        missing = [s for s in services if s not in states]
        waiting = [states[s] for s in services if s in states and not states[s].ready]
        if not missing and not waiting:
            elapsed = time.monotonic() - start
            log(f"all {len(services)} containers running and healthy ({elapsed:.0f} s)")
            return elapsed
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            lines = [f"not ready after {timeout:.0f} s:"]
            lines += [f"  {s}: no container (run `make up`)" for s in missing]
            for c in waiting:
                lines.append(f"  {c.describe()}; last log lines of {c.name}:")
                try:
                    tail = _run(["docker", "logs", "--tail", "15", c.name])
                except BringupError as exc:
                    tail = str(exc)
                lines += [f"    {line}" for line in tail.splitlines()[-15:]]
            raise BringupError("\n".join(lines))
        if elapsed - last_report >= 15:
            pending = [f"{s} (not created)" for s in missing] + [c.describe() for c in waiting]
            log(f"waiting for {len(pending)} container(s): {', '.join(pending)}")
            last_report = elapsed
        time.sleep(interval)
