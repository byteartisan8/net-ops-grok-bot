"""Async client for lab-api, the bot's only way into the lab (contract section 4: 172.30.10.50:8090).

Reads need no token. Writes (dry runs, approved changes, field repairs) send ``X-Lab-Token`` and ``X-Lab-Actor``
(``grok-bot/<bot>``, plus ``approved-by:<who>`` on an approved change) so lab-api's audit log names the bot and the
human who approved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx


class LabApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), "status": self.status, "detail": self.detail}


class LabApi:
    def __init__(
        self,
        url: str,
        token: Callable[[], str | None] | str | None = None,
        actor: str = "grok-bot",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.actor = actor
        self._http = httpx.AsyncClient(base_url=self.url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    def token(self) -> str | None:
        return self._token() if callable(self._token) else self._token

    async def request(
        self,
        method: str,
        path: str,
        *,
        write: bool = False,
        actor: str | None = None,
        timeout: float | None = None,
        **kw: Any,
    ) -> Any:
        headers = {"X-Lab-Actor": actor or self.actor}
        if write:
            token = self.token()
            if not token:
                raise LabApiError("LAB_API_TOKEN is not set: lab-api writes are impossible (run `make env`)")
            headers["X-Lab-Token"] = token
        try:
            resp = await self._http.request(
                method,
                path,
                headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                **kw,
            )
        except httpx.HTTPError as exc:
            raise LabApiError(f"lab-api {self.url} unreachable: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail: Any = resp.json()
            except ValueError:
                detail = resp.text[:600]
            if isinstance(detail, dict) and "detail" in detail:
                detail = detail["detail"]
            raise LabApiError(f"lab-api {method} {path}: HTTP {resp.status_code}", resp.status_code, detail)
        return resp.json() if resp.content else None

    # ------------------------------------------------------------------ reads
    async def get(self, path: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return await self.request("GET", path, params=clean or None, timeout=timeout)

    async def health(self) -> bool:
        try:
            await self.get("/health", timeout=3)
            return True
        except LabApiError:
            return False

    async def problems(self, service: str | None = None, scope: str | None = None) -> dict[str, Any]:
        return await self.get("/zabbix/problems", {"service": service, "scope": scope})

    async def services(self) -> dict[str, Any]:
        return await self.get("/services")

    async def devices(self) -> dict[str, Any]:
        return await self.get("/devices")

    async def topology(self) -> dict[str, Any]:
        return await self.get("/topology")

    # ------------------------------------------------------------------ writes
    async def plan_apply_intent(self, host: str, actor: str, comment: str | None = None) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/devices/{host}/apply-intent",
            write=True,
            actor=actor,
            json={"confirm": False, "comment": comment},
        )

    async def apply_intent(self, host: str, plan_id: str, actor: str, comment: str | None = None) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/devices/{host}/apply-intent",
            write=True,
            actor=actor,
            timeout=200,
            json={"confirm": True, "plan_id": plan_id, "verify": True, "timeout_s": 90, "comment": comment},
        )

    async def plan_patch(
        self, host: str, patch: list[dict[str, Any]], actor: str, comment: str | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/devices/{host}/config",
            write=True,
            actor=actor,
            json={"patch": patch, "confirm": False, "comment": comment},
        )

    async def apply_patch(
        self, host: str, patch: list[dict[str, Any]], plan_id: str, actor: str, comment: str | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/devices/{host}/config",
            write=True,
            actor=actor,
            timeout=200,
            json={
                "patch": patch,
                "confirm": True,
                "plan_id": plan_id,
                "verify": True,
                "timeout_s": 90,
                "comment": comment,
            },
        )

    async def restore_fault(
        self, kind: str, host: str, actor: str, peer: str | None = None, interface: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": kind, "host": host, "action": "restore"}
        if peer:
            body["peer"] = peer
        if interface:
            body["interface"] = interface
        return await self.request("POST", "/faults", write=True, actor=actor, timeout=90, json=body)
