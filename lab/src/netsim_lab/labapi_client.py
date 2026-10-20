"""Small client for lab-api, used by labctl, scenarios and ``reset`` (the bot will use the OpenAPI instead)."""

from __future__ import annotations

from typing import Any

import httpx


class LabApiError(RuntimeError):
    pass


class LabApiClient:
    def __init__(self, url: str, token: str | None, actor: str = "labctl", timeout: float = 180.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.actor = actor
        self._http = httpx.Client(base_url=self.url, timeout=timeout)

    def __enter__(self) -> LabApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _headers(self, write: bool) -> dict[str, str]:
        headers = {"X-Lab-Actor": self.actor}
        if write:
            if not self.token:
                raise LabApiError("LAB_API_TOKEN is not set (run `make env`)")
            headers["X-Lab-Token"] = self.token
        return headers

    def request(self, method: str, path: str, *, write: bool = False, **kw: Any) -> Any:
        try:
            resp = self._http.request(method, path, headers=self._headers(write), **kw)
        except httpx.HTTPError as exc:
            raise LabApiError(f"lab-api {self.url} unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise LabApiError(f"lab-api {method} {path}: HTTP {resp.status_code} {resp.text[:400]}")
        return resp.json() if resp.content else None

    def healthy(self) -> bool:
        try:
            return self._http.get("/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def sync(self, wait: bool = True, reason: str = "requested") -> dict[str, Any]:
        return self.request("POST", "/sync", write=True, json={"wait": wait, "reason": reason})

    def sync_status(self) -> dict[str, Any]:
        return self.request("GET", "/sync")

    def apply_intent(self, host: str, verify: bool = True) -> dict[str, Any]:
        """Dry run, then confirm with the returned ``plan_id`` (the propose -> approve -> apply loop)."""
        plan = self.request("POST", f"/devices/{host}/apply-intent", write=True, json={"confirm": False})
        if plan.get("no_change"):
            return {**plan, "applied": False, "verification": {"verdict": "no-change"}}
        return self.request(
            "POST",
            f"/devices/{host}/apply-intent",
            write=True,
            json={"confirm": True, "plan_id": plan["plan_id"], "verify": verify},
        )
