"""The ``core`` signaling bus (Valkey): physical faults, core cuts, and lab-api's drift baselines.

Faults (contract section 7) are plain keys with value ``1``; simd reacts to ``SET``/``DEL`` within ~0.1 s
through keyspace notifications. Keys are built only with :mod:`netsim_common.signaling`.

**Drift baselines** (05-integration, contract change log): ``netsim:lab:baseline:<host>`` holds the canonical
config on which the device and NetBox last agreed. The drift checker compares running config and intent against it
to tell *config drift* (the device moved) from *intent-only* changes (NetBox moved). No key = the golden config
(what ``make seed`` and ``make reset`` establish). ``core`` keeps no data across restarts, so after a restart of
``core`` the baseline is golden again. simd never reads these keys (it only watches ``netsim:fault:*`` /
``netsim:cut:*``).
"""

from __future__ import annotations

import json
from typing import Any

import redis

from netsim_common.canonical import canonical_dict
from netsim_common.device_config import DeviceConfig
from netsim_common.signaling import (
    CUT_PATTERN,
    FAULT_ON,
    FAULT_PATTERN,
    FaultKey,
    ac_fault_key,
    cut_key,
    parse_fault_key,
    uplink_fault_key,
)

BASELINE_PREFIX = "netsim:lab:baseline:"


class CoreError(RuntimeError):
    """The core bus is unreachable."""


class Core:
    def __init__(self, url: str, timeout: float = 3.0) -> None:
        self.url = url
        self._r = redis.Redis.from_url(
            url, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True
        )

    def close(self) -> None:
        self._r.close()

    def _call(self, fn, *args: Any) -> Any:
        try:
            return fn(*args)
        except redis.RedisError as exc:
            raise CoreError(f"core {self.url}: {exc}") from exc

    def ping(self) -> bool:
        return bool(self._call(self._r.ping))

    # ------------------------------------------------------------------ faults
    def set_uplink(self, host: str, down: bool = True) -> str:
        return self._set(uplink_fault_key(host), down)

    def set_ac(self, host: str, ifname: str, down: bool = True) -> str:
        return self._set(ac_fault_key(host, ifname), down)

    def set_cut(self, host_a: str, host_b: str, cut: bool = True) -> str:
        return self._set(cut_key(host_a, host_b), cut)

    def _set(self, key: str, on: bool) -> str:
        if on:
            self._call(self._r.set, key, FAULT_ON)
        else:
            self._call(self._r.delete, key)
        return key

    def faults(self) -> list[FaultKey]:
        """Active faults and cuts (value ``1``), sorted by key."""
        keys = sorted(
            k
            for pattern in (FAULT_PATTERN, CUT_PATTERN)
            for k in self._call(lambda p: list(self._r.scan_iter(p)), pattern)
        )
        values = self._call(self._r.mget, keys) if keys else []
        out = []
        for key, value in zip(keys, values, strict=True):
            parsed = parse_fault_key(key)
            if parsed is not None and value == FAULT_ON:
                out.append(parsed)
        return out

    def clear_faults(self) -> list[str]:
        """Delete every fault and cut key; returns the deleted keys."""
        keys = sorted(
            k
            for pattern in (FAULT_PATTERN, CUT_PATTERN)
            for k in self._call(lambda p: list(self._r.scan_iter(p)), pattern)
        )
        if keys:
            self._call(self._r.delete, *keys)
        return keys

    # ------------------------------------------------------------------ drift baselines
    def baseline(self, host: str) -> dict[str, Any] | None:
        raw = self._call(self._r.get, BASELINE_PREFIX + host)
        return json.loads(raw) if raw else None

    def set_baseline(self, host: str, config: DeviceConfig | dict[str, Any]) -> None:
        self._call(self._r.set, BASELINE_PREFIX + host, json.dumps(canonical_dict(config), sort_keys=True))

    def clear_baseline(self, host: str) -> None:
        self._call(self._r.delete, BASELINE_PREFIX + host)

    def clear_baselines(self) -> list[str]:
        keys = sorted(self._call(lambda: list(self._r.scan_iter(BASELINE_PREFIX + "*"))))
        if keys:
            self._call(self._r.delete, *keys)
        return [k.removeprefix(BASELINE_PREFIX) for k in keys]


def describe_fault(fault: FaultKey) -> str:
    if fault.kind == "uplink":
        return f"uplink down on {fault.hosts[0]}"
    if fault.kind == "ac":
        return f"attachment circuit {fault.ifname} down on {fault.hosts[0]}"
    return f"core path cut between {fault.hosts[0]} and {fault.hosts[1]}"


def fault_dict(fault: FaultKey) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": fault.kind, "key": fault.key, "description": describe_fault(fault)}
    if fault.kind == "cut":
        out["hosts"] = list(fault.hosts)
    else:
        out["host"] = fault.hosts[0]
    if fault.ifname:
        out["interface"] = fault.ifname
    return out
