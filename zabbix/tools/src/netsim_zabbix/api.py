"""Minimal Zabbix JSON-RPC client (httpx), verified against Zabbix 8.0.0rc1.

* Endpoint: ``<url>/api_jsonrpc.php``; authentication with ``Authorization: Bearer <API token or session ID>``
  (the request-body ``auth`` property is deprecated). :meth:`ZabbixApi.login` opens a session for a user/password
  (only the bootstrap uses that, as Admin); everything else uses an API token.
* Errors come back as :class:`ZabbixApiError` with Zabbix's code, message and data. Secrets are never logged.

    with ZabbixApi("http://127.0.0.1:8080", token=...) as api:
        api.call("host.get", {"output": ["hostid", "host"]})
"""

from __future__ import annotations

import contextlib
import itertools
import time
from collections.abc import Callable
from typing import Any

import httpx

JSON = Any
ENDPOINT = "/api_jsonrpc.php"


class ZabbixApiError(RuntimeError):
    """A JSON-RPC error from Zabbix, or Zabbix unreachable (``code`` is ``None`` then)."""

    def __init__(self, method: str, code: int | None, message: str, data: str = "") -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        detail = f"{message} {data}".strip()
        super().__init__(f"{method}: {detail}" if code is None else f"{method}: [{code}] {detail}")


def describe_token(token: str | None) -> str:
    """Printable reference to a Zabbix API token (64 hex characters) that never reveals it."""
    if not token:
        return "<unset>"
    return f"{token[:4]}…({len(token)} chars)"


class ZabbixApi:
    """Synchronous JSON-RPC client. Use as a context manager or call :meth:`close`."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = token
        self._ids = itertools.count(1)
        self._http = httpx.Client(
            base_url=self.url,
            headers={"Content-Type": "application/json-rpc", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._session = False

    def __enter__(self) -> ZabbixApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._session:
            with contextlib.suppress(ZabbixApiError):
                self.call("user.logout", [])
            self._session = False
        self._http.close()

    # ------------------------------------------------------------------ core
    def call(self, method: str, params: JSON = None, *, auth: bool = True) -> JSON:
        """Call ``method`` and return its ``result``. ``auth=False`` for methods that must not carry credentials."""
        body = {"jsonrpc": "2.0", "method": method, "params": {} if params is None else params, "id": next(self._ids)}
        headers = {}
        if auth and self._auth:
            headers["Authorization"] = f"Bearer {self._auth}"
        try:
            response = self._http.post(ENDPOINT, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ZabbixApiError(method, None, f"{self.url}{ENDPOINT} unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ZabbixApiError(method, None, f"HTTP {response.status_code} from {self.url}{ENDPOINT}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZabbixApiError(method, None, f"invalid JSON from {self.url}{ENDPOINT}") from exc
        if "error" in payload:
            err = payload["error"]
            raise ZabbixApiError(method, err.get("code"), err.get("message", ""), err.get("data", ""))
        return payload.get("result")

    # ------------------------------------------------------------------ helpers
    def version(self) -> str:
        return self.call("apiinfo.version", [], auth=False)

    def login(self, username: str, password: str) -> None:
        """Open a session (``user.login``); later calls use it. Raises :class:`ZabbixApiError` on bad credentials."""
        self._auth = self.call("user.login", {"username": username, "password": password}, auth=False)
        self._session = True

    def use_token(self, token: str) -> None:
        self._auth = token
        self._session = False

    def get(self, method: str, params: JSON = None) -> list[dict]:
        """``<object>.get`` returning a list (``params`` defaults to ``{"output": "extend"}``)."""
        return self.call(method, {"output": "extend"} if params is None else params)

    def get_one(self, method: str, params: JSON) -> dict | None:
        """The single object matching ``params``; ``None`` if none; error if several."""
        result = self.call(method, params)
        if len(result) > 1:
            raise ZabbixApiError(method, None, f"expected at most one object, got {len(result)}")
        return result[0] if result else None


def wait_for_api(
    url: str, timeout: float = 300.0, interval: float = 2.0, log: Callable[[str], None] | None = None
) -> str:
    """Wait until the API answers ``apiinfo.version``; returns the version."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with ZabbixApi(url, timeout=10) as api:
                return api.version()
        except ZabbixApiError as exc:
            last = exc
        time.sleep(interval)
    raise ZabbixApiError("apiinfo.version", None, f"Zabbix API at {url} not ready after {timeout:.0f} s: {last}")
