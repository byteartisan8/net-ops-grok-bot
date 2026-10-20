"""Device power off/on through the Docker Engine API (unix socket), limited to the lab's PE containers.

Contract section 7: a device "power off" is ``docker stop <host>`` (containers are named after their hostnames,
session 02). lab-api runs in a container with the host's Docker socket mounted; labctl uses the same socket.
Only the PE containers of the topology may be stopped or started through this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, socket_path: str, allowed: Iterable[str], timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.allowed = frozenset(allowed)
        self._http = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path), base_url="http://docker", timeout=timeout
        )

    def close(self) -> None:
        self._http.close()

    def _check(self, name: str) -> None:
        if name not in self.allowed:
            raise DockerError(f"{name!r} is not a lab device (allowed: {', '.join(sorted(self.allowed))})")

    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            return self._http.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise DockerError(f"Docker API at {self.socket_path} unreachable: {exc}") from exc

    def inspect(self, name: str) -> dict[str, Any] | None:
        """The container's state (``None`` if it does not exist)."""
        self._check(name)
        resp = self._request("GET", f"/containers/{name}/json")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DockerError(f"inspect {name}: HTTP {resp.status_code} {resp.text[:200]}")
        state = resp.json().get("State", {})
        return {
            "name": name,
            "status": state.get("Status"),
            "running": bool(state.get("Running")),
            "health": (state.get("Health") or {}).get("Status"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
        }

    def running(self, name: str) -> bool:
        info = self.inspect(name)
        return bool(info and info["running"])

    def stop(self, name: str, timeout_s: int = 10) -> bool:
        """``docker stop``; returns False if it was already stopped."""
        self._check(name)
        resp = self._request("POST", f"/containers/{name}/stop", params={"t": timeout_s}, timeout=timeout_s + 30)
        if resp.status_code == 304:
            return False
        if resp.status_code != 204:
            raise DockerError(f"stop {name}: HTTP {resp.status_code} {resp.text[:200]}")
        return True

    def start(self, name: str) -> bool:
        """``docker start``; returns False if it was already running."""
        self._check(name)
        resp = self._request("POST", f"/containers/{name}/start")
        if resp.status_code == 304:
            return False
        if resp.status_code != 204:
            raise DockerError(f"start {name}: HTTP {resp.status_code} {resp.text[:200]}")
        return True

    def stopped(self) -> list[str]:
        """Lab devices whose container exists but is not running."""
        out = []
        for name in sorted(self.allowed):
            info = self.inspect(name)
            if info is not None and not info["running"]:
                out.append(name)
        return out
