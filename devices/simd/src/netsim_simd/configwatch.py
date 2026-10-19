"""Running-config watch: commit notifications first, a content-hash poll as the safety net.

* Primary: the RESTCONF ``NETCONF`` event stream. Every ``netconf-config-change`` triggers an immediate
  ``GET /restconf/data?content=config`` (typically < 50 ms after the commit). The plugin emits the
  notification at transaction end, after the running datastore was written.
* Safety net: the same GET every ``poll_interval`` seconds (default 2 s), compared by the SHA-256 of the
  canonical JSON. It covers a dropped subscription and the reconnect gap; a full refresh also runs on every
  (re)subscription.

Only a change of the canonical config counts: a commit that nets to no change produces nothing. Updates
are serialized, so the callback sees each config exactly once and in order.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from netsim_common.canonical import canonical_json
from netsim_common.device_config import DeviceConfig
from netsim_simd.clixon import ConfigCommit, Restconf, RestconfError, parse_config_change

log = logging.getLogger("simd.config")


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    config: DeviceConfig
    canonical: str
    digest: str

    @classmethod
    def from_payload(cls, payload: dict) -> ConfigSnapshot:
        config = DeviceConfig.from_restconf(payload)
        text = canonical_json(config)
        return cls(config, text, hashlib.sha256(text.encode("utf-8")).hexdigest())

    @property
    def empty(self) -> bool:
        c = self.config
        return (
            not c.interfaces.interface
            and not c.l2vpn.xconnect
            and c.system.hostname is None
            and c.system.loopback_ipv4 is None
        )


@dataclass(frozen=True, slots=True)
class ConfigUpdate:
    old: ConfigSnapshot | None
    new: ConfigSnapshot
    source: str
    commit: ConfigCommit | None
    detected_at: float


class ConfigWatcher:
    def __init__(
        self,
        restconf: Restconf,
        on_update: Callable[[ConfigUpdate], Awaitable[None]],
        poll_interval: float = 2.0,
    ) -> None:
        self.restconf = restconf
        self.on_update = on_update
        self.poll_interval = poll_interval
        self.current: ConfigSnapshot | None = None
        self.stream_connected = False
        self.last_fetch_ok: bool = False
        self.notifications = 0
        self.invalid_configs = 0
        self._lock = asyncio.Lock()
        self._pending_commit: tuple[float, ConfigCommit] | None = None

    async def fetch(self) -> ConfigSnapshot:
        try:
            payload = await self.restconf.get_running()
        except (httpx.HTTPError, RestconfError):
            self.last_fetch_ok = False
            raise
        self.last_fetch_ok = True
        return ConfigSnapshot.from_payload(payload)

    async def refresh(self, source: str, commit: ConfigCommit | None = None) -> bool:
        """Fetch the running config; call ``on_update`` if it changed. Returns whether it changed."""
        async with self._lock:
            if commit is not None:
                self._pending_commit = (time.monotonic(), commit)
            try:
                snap = await self.fetch()
            except (httpx.HTTPError, RestconfError) as exc:
                log.warning("cannot read running config: %s", exc, extra={"fields": {"event": "config-read-error"}})
                return False
            except ValidationError as exc:
                self.invalid_configs += 1
                log.error(
                    "running config does not match the netsim-device model: %s",
                    exc,
                    extra={"fields": {"event": "config-invalid"}},
                )
                return False
            if self.current is not None and snap.digest == self.current.digest:
                return False
            attached = None
            if self._pending_commit is not None and time.monotonic() - self._pending_commit[0] < 2 * self.poll_interval:
                attached = self._pending_commit[1]
            self._pending_commit = None
            update = ConfigUpdate(self.current, snap, source, attached, time.time())
            self.current = snap
            await self.on_update(update)
            return True

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._stream_loop(stop)), asyncio.create_task(self._poll_loop(stop))]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self.poll_interval)
            await self.refresh("poll")

    async def _stream_loop(self, stop: asyncio.Event) -> None:
        backoff = 0.2

        async def opened() -> None:
            nonlocal backoff
            self.stream_connected = True
            backoff = 0.2
            log.info("subscribed to the config notification stream", extra={"fields": {"event": "stream-up"}})
            await self.refresh("resync")  # commits made while we were not subscribed

        while not stop.is_set():
            reason = "cancelled"
            try:
                async for event in self.restconf.notifications(on_open=opened):
                    commit = parse_config_change(event)
                    if commit is None:
                        continue
                    self.notifications += 1
                    await self.refresh("notification", commit)
                reason = "closed by the server"
            except (httpx.HTTPError, RestconfError) as exc:
                reason = str(exc) or type(exc).__name__
            finally:
                if self.stream_connected:
                    log.warning(
                        "config notification stream lost (%s); polling until it is back",
                        reason,
                        extra={"fields": {"event": "stream-down"}},
                    )
                self.stream_connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
