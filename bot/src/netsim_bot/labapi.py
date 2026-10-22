"""Async client for lab-api, the office's only way into the lab (contract section 4: 172.30.10.50:8090).

Reads need no token, the direct reads included (Zabbix allow-list, NetBox GET, RESTCONF GET). Writes (dry runs,
approved changes, field repairs, acknowledgments) send ``X-Lab-Token`` and ``X-Lab-Actor`` (``grok-bot/<ROLE>``, plus
``approved-by:<who>`` on an approved change) so lab-api's audit log names the role and the human who approved.
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

    # ------------------------------------------------------------------ direct reads
    async def zabbix_read(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """``POST /zabbix/api``: one allow-listed Zabbix read; returns Zabbix's result."""
        out = await self.request("POST", "/zabbix/api", json={"method": method, "params": params or {}}, timeout=60)
        return out.get("result") if isinstance(out, dict) else out

    async def netbox_get(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        """``GET /netbox/<path>``: NetBox's REST API with the read-only token."""
        out = await self.request("GET", f"/netbox/{path.lstrip('/')}", params=params or None, timeout=60)
        return out.get("data") if isinstance(out, dict) else out

    async def restconf_get(self, host: str, path: str = "", content: str = "config") -> Any:
        """``GET /devices/<host>/restconf/<path>``: a RESTCONF GET on the device (list keys percent-encoded)."""
        suffix = f"/{path.lstrip('/')}" if path else ""
        out = await self.request("GET", f"/devices/{host}/restconf{suffix}", params={"content": content}, timeout=30)
        return out.get("data") if isinstance(out, dict) else out

    async def acknowledge(self, eventids: list[str], message: str, role: str, actor: str) -> dict[str, Any]:
        """``POST /zabbix/acknowledge``: the trigger problems behind ``eventids``, as the role's Zabbix user."""
        body = {"eventids": eventids, "message": message, "role": role}
        return await self.request("POST", "/zabbix/acknowledge", write=True, actor=actor, json=body, timeout=60)

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
