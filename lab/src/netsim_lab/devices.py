"""Device access: Clixon RESTCONF (config) and simd (state, events), synchronous httpx.

RESTCONF facts from session 02 (contract change log): read config with ``GET /restconf/data?content=config``;
``PUT /restconf/data`` with ``{"ietf-restconf:data": ...}`` replaces the whole config in one commit (one
``ciscoConfigManEvent``); interface names in URLs are percent-encoded. simd answers on :9000 (``/state``,
``/events``, ``/health``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import httpx

from netsim_common.canonical import canonical_dict, canonical_json
from netsim_common.device_config import DeviceConfig
from netsim_lab.configpath import restconf_body, restconf_path
from netsim_lab.settings import DeviceEndpoint

YANG_JSON = "application/yang-data+json"
RESTCONF_DATA = "/restconf/data"

T = TypeVar("T")


class DeviceError(RuntimeError):
    """A device did not answer, or refused a request."""

    def __init__(self, host: str, message: str, status: int | None = None) -> None:
        self.host = host
        self.status = status
        super().__init__(f"{host}: {message}")


def config_hash(config: DeviceConfig | Mapping[str, Any]) -> str:
    """SHA-256 of the canonical config text: the value simd reports as ``config_hash`` in ``/state``."""
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def _restconf_error(resp: httpx.Response) -> str:
    try:
        errors = resp.json()["ietf-restconf:errors"]["error"]
        return "; ".join(f"{e.get('error-tag')}: {e.get('error-message', '').strip()}" for e in errors)
    except (ValueError, KeyError, TypeError):
        return resp.text.strip()[:300]


class Device:
    """One PE: RESTCONF + simd. Thread-safe (one pooled httpx client per device)."""

    def __init__(self, endpoint: DeviceEndpoint, timeout: float = 5.0) -> None:
        self.endpoint = endpoint
        self.host = endpoint.hostname
        self._restconf = httpx.Client(base_url=endpoint.restconf_url, timeout=timeout, headers={"Accept": YANG_JSON})
        self._simd = httpx.Client(base_url=endpoint.simd_url, timeout=timeout)

    def close(self) -> None:
        self._restconf.close()
        self._simd.close()

    # ------------------------------------------------------------------ RESTCONF
    def _request(self, client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            return client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise DeviceError(self.host, f"{method} {client.base_url}{url} failed: {exc}") from exc

    def running_payload(self) -> dict[str, Any]:
        """Running config as RESTCONF JSON (``{}`` for an empty datastore)."""
        resp = self._request(self._restconf, "GET", RESTCONF_DATA, params={"content": "config"})
        if resp.status_code in (204, 404):
            return {}
        if resp.status_code != 200:
            raise DeviceError(self.host, f"GET running config: HTTP {resp.status_code} {_restconf_error(resp)}")
        data = resp.json() if resp.content else {}
        return data.get("ietf-restconf:data", data) if isinstance(data, dict) else {}

    def running(self) -> DeviceConfig:
        return DeviceConfig.from_restconf(self.running_payload())

    def running_canonical(self) -> dict[str, Any]:
        return canonical_dict(self.running())

    def replace(self, config: DeviceConfig | Mapping[str, Any]) -> None:
        """Replace the whole running config in one commit (``PUT /restconf/data``)."""
        body = json.dumps({"ietf-restconf:data": canonical_dict(config)})
        resp = self._request(self._restconf, "PUT", RESTCONF_DATA, content=body, headers={"Content-Type": YANG_JSON})
        if resp.status_code not in (200, 201, 204):
            raise DeviceError(
                self.host, f"PUT config refused: HTTP {resp.status_code} {_restconf_error(resp)}", resp.status_code
            )

    def get_path(self, path: str) -> Any:
        """``GET`` one node by config path (``l2vpn/xconnect[SVC-0001]``); ``None`` if it does not exist."""
        url = f"{RESTCONF_DATA}/{restconf_path(path)}"
        resp = self._request(self._restconf, "GET", url, params={"content": "config"})
        if resp.status_code in (204, 404):
            return None
        if resp.status_code != 200:
            raise DeviceError(
                self.host, f"GET {path}: HTTP {resp.status_code} {_restconf_error(resp)}", resp.status_code
            )
        return resp.json()

    def put_path(self, path: str, value: Any) -> None:
        """Create or replace one node (leaf, list entry or container) in one commit, as an engineer would."""
        url = f"{RESTCONF_DATA}/{restconf_path(path)}"
        body = json.dumps(restconf_body(path, value))
        resp = self._request(self._restconf, "PUT", url, content=body, headers={"Content-Type": YANG_JSON})
        if resp.status_code not in (200, 201, 204):
            raise DeviceError(
                self.host, f"PUT {path}: HTTP {resp.status_code} {_restconf_error(resp)}", resp.status_code
            )

    def delete_path(self, path: str) -> None:
        url = f"{RESTCONF_DATA}/{restconf_path(path)}"
        resp = self._request(self._restconf, "DELETE", url)
        if resp.status_code not in (200, 204):
            raise DeviceError(
                self.host, f"DELETE {path}: HTTP {resp.status_code} {_restconf_error(resp)}", resp.status_code
            )

    def restconf_get(self, path: str, content: str = "config") -> tuple[int, Any]:
        """A read-only RESTCONF GET: ``(status, JSON or None)`` of ``/restconf/data/<path>?content=<content>``.

        ``path`` is a RESTCONF data path as the device expects it, list keys percent-encoded
        (``netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F1.101``); empty = the whole datastore."""
        url = RESTCONF_DATA + (f"/{path}" if path else "")
        resp = self._request(self._restconf, "GET", url, params={"content": content})
        if resp.status_code in (204, 404):
            return resp.status_code, None
        if resp.status_code != 200:
            raise DeviceError(
                self.host, f"GET {url}: HTTP {resp.status_code} {_restconf_error(resp)}", resp.status_code
            )
        return 200, resp.json() if resp.content else None

    # ------------------------------------------------------------------ simd
    def _simd_get(self, url: str, **params: Any) -> dict[str, Any]:
        resp = self._request(self._simd, "GET", url, params={k: v for k, v in params.items() if v is not None})
        if resp.status_code != 200:
            raise DeviceError(self.host, f"simd {url}: HTTP {resp.status_code}", resp.status_code)
        return resp.json()

    def state(self) -> dict[str, Any]:
        """simd ``/state``: PWs (oper, reason, rule, remote), interfaces, uplink, faults, ``config_hash``."""
        return self._simd_get("/state")

    def events(self, since: str | float | None = None, limit: int | None = None) -> dict[str, Any]:
        return self._simd_get("/events", since=since, limit=limit)

    def health(self) -> dict[str, Any]:
        return self._simd_get("/health")


class Devices:
    """All PEs of the lab, with parallel helpers."""

    def __init__(self, endpoints: Mapping[str, DeviceEndpoint], timeout: float = 5.0) -> None:
        self._devices = {h: Device(ep, timeout) for h, ep in endpoints.items()}

    def __getitem__(self, host: str) -> Device:
        try:
            return self._devices[host]
        except KeyError:
            raise DeviceError(host, f"unknown device (devices: {', '.join(self._devices)})") from None

    def __iter__(self):
        return iter(self._devices.values())

    @property
    def hosts(self) -> list[str]:
        return list(self._devices)

    def close(self) -> None:
        for d in self._devices.values():
            d.close()

    def map(self, fn: Callable[[Device], T], hosts: Iterable[str] | None = None) -> dict[str, T | DeviceError]:
        """``{host: fn(device)}`` in parallel; a failing device maps to its :class:`DeviceError`."""
        selected = [self[h] for h in (hosts if hosts is not None else self._devices)]

        def run(device: Device) -> T | DeviceError:
            try:
                return fn(device)
            except DeviceError as exc:
                return exc
            except (httpx.HTTPError, ValueError) as exc:
                return DeviceError(device.host, str(exc))

        with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            return dict(zip([d.host for d in selected], pool.map(run, selected), strict=True))

    def states(self, hosts: Iterable[str] | None = None) -> dict[str, dict[str, Any] | DeviceError]:
        return self.map(lambda d: d.state(), hosts)

    def running(self, hosts: Iterable[str] | None = None) -> dict[str, DeviceConfig | DeviceError]:
        return self.map(lambda d: d.running(), hosts)
