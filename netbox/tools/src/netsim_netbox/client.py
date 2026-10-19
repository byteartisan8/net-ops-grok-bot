"""Minimal NetBox REST client (httpx), verified against NetBox 4.7.3.

* Authentication: v2 tokens (``nbt_<key>.<token>``, NetBox >= 4.5) are sent as ``Authorization: Bearer <token>``;
  anything else is treated as a legacy v1 token (``Authorization: Token <token>``).
* :meth:`NetBoxClient.list` follows the ``next`` links (``limit=1000`` = NetBox's default ``MAX_PAGE_SIZE``).
* Bulk writes use NetBox's list forms: ``POST``/``PATCH`` a JSON list, ``DELETE`` a list of ``{"id": n}``.

Endpoints are written without ``/api/`` and slashes, e.g. ``client.list("dcim/interfaces", device_id=[1, 2])``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

TOKEN_PREFIX = "nbt_"
PAGE_SIZE = 1000
BULK_CHUNK = 100

JSON = Any
Params = Mapping[str, Any]


class NetBoxError(RuntimeError):
    """An HTTP error from NetBox (or NetBox unreachable). ``status`` is ``None`` when there was no response."""

    def __init__(self, method: str, url: str, status: int | None, detail: Any) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.detail = detail
        where = f"{method} {url}"
        super().__init__(f"{where}: HTTP {status}: {detail}" if status is not None else f"{where}: {detail}")


def auth_header(token: str) -> str:
    """``Authorization`` header value for a v2 (``nbt_...``) or legacy v1 token."""
    return f"Bearer {token}" if token.startswith(TOKEN_PREFIX) else f"Token {token}"


def token_key(token: str | None) -> str | None:
    """The non-secret key of a v2 token (``nbt_<key>.<secret>`` -> ``<key>``), else ``None``."""
    if not token or not token.startswith(TOKEN_PREFIX) or "." not in token:
        return None
    return token[len(TOKEN_PREFIX) :].split(".", 1)[0]


def describe_token(token: str | None) -> str:
    """Printable reference to a token that never reveals its secret part."""
    key = token_key(token)
    if key:
        return f"nbt_{key}.***"
    return "<unset>" if not token else "<legacy v1 token>"


class NetBoxClient:
    """Synchronous NetBox REST API client. Use as a context manager or call :meth:`close`."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = auth_header(token)
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout, transport=transport)

    # ------------------------------------------------------------------ plumbing
    def __enter__(self) -> NetBoxClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _path(self, endpoint: str) -> str:
        if endpoint.startswith(("http://", "https://")):
            parts = urlsplit(endpoint)  # rebase NetBox's absolute links onto our base URL
            return parts.path + (f"?{parts.query}" if parts.query else "")
        endpoint = endpoint.strip("/")
        if endpoint.startswith("api/"):
            endpoint = endpoint[4:]
        return f"/api/{endpoint}/"

    def request(self, method: str, endpoint: str, *, params: Params | None = None, json: JSON = None) -> JSON:
        path = self._path(endpoint)
        try:
            resp = self._http.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise NetBoxError(method, f"{self.url}{path}", None, f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail: Any = resp.json()
            except ValueError:
                detail = resp.text[:500]
            raise NetBoxError(method, str(resp.request.url), resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ------------------------------------------------------------------ reads
    def get(self, endpoint: str, object_id: int | None = None, **params: Any) -> JSON:
        path = endpoint if object_id is None else f"{endpoint.strip('/')}/{object_id}"
        return self.request("GET", path, params=params or None)

    def list(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        """Every object of a list endpoint (all pages). List values become repeated query parameters."""
        query: dict[str, Any] = {"limit": PAGE_SIZE, **params}
        page = self.request("GET", endpoint, params=query)
        results = list(page["results"])
        while page.get("next"):
            page = self.request("GET", page["next"])
            results += page["results"]
        return results

    def status(self) -> dict[str, Any]:
        return self.request("GET", "status")

    # ------------------------------------------------------------------ writes
    def create(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", endpoint, json=data)

    def bulk_create(self, endpoint: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for chunk in _chunks(items):
            created += self.request("POST", endpoint, json=chunk)
        return created

    def update(self, endpoint: str, object_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"{endpoint.strip('/')}/{object_id}", json=data)

    def bulk_update(self, endpoint: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``items`` are partial objects, each with its ``id``."""
        updated: list[dict[str, Any]] = []
        for chunk in _chunks(items):
            updated += self.request("PATCH", endpoint, json=chunk)
        return updated

    def delete(self, endpoint: str, object_id: int) -> None:
        self.request("DELETE", f"{endpoint.strip('/')}/{object_id}")

    def bulk_delete(self, endpoint: str, ids: Iterable[int]) -> None:
        for chunk in _chunks([{"id": i} for i in ids]):
            self.request("DELETE", endpoint, json=chunk)


def _chunks(items: list[Any], size: int = BULK_CHUNK) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
