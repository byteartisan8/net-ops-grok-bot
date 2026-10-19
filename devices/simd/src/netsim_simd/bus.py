"""The ``core`` Redis bus (contract section 7): adverts, faults, cuts and the ``netsim:events`` channel.

* Adverts: ``SET netsim:adv:<host> <json> PX <hold>`` every hello, plus the optional loopback index
  ``netsim:lo:<loopback>`` with the same TTL. A key that exists is a fresh advert.
* Faults/cuts are read, never written (labctl / lab-api own them). Only the value ``1`` means "set".
* Wake-ups: simd recomputes on every ``netsim:events`` message and, when the server has keyspace
  notifications enabled (the lab's ``core`` runs ``--notify-keyspace-events KEg$x``), on every change of a
  fault or cut key and on every advert expiry, so a fault set with a plain ``SET`` acts immediately. Without
  them, the 1 s periodic recompute still picks everything up.

simd publishes ``{"type": "state", "host", "boot_id", "seq", "ts"}`` on ``netsim:events`` whenever its advert
content changes. Any message on the channel triggers a recompute on every device, so labctl/lab-api can
publish ``{"type": "fault", ...}`` after setting a fault for the fastest reaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import redis.asyncio as aioredis
from pydantic import ValidationError
from redis.exceptions import RedisError

from netsim_common.signaling import (
    ADVERT_PREFIX,
    CUT_PATTERN,
    EVENTS_CHANNEL,
    FAULT_ON,
    FAULT_PATTERN,
    KEY_PREFIX,
    Advert,
    advert_key,
    loopback_index_key,
    parse_fault_key,
)
from netsim_simd.evaluate import LocalFaults

log = logging.getLogger("simd.bus")


@dataclass(frozen=True, slots=True)
class BusSnapshot:
    adverts: dict[str, Advert] = field(default_factory=dict)
    ages: dict[str, float] = field(default_factory=dict)
    faults: LocalFaults = field(default_factory=LocalFaults)


def redis_db(url: str) -> int:
    path = urlparse(url).path.strip("/")
    return int(path) if path.isdigit() else 0


class Bus:
    def __init__(self, url: str, hostname: str, hold_time: float) -> None:
        self.url = url
        self.hostname = hostname
        self.hold_ms = int(hold_time * 1000)
        self.db = redis_db(url)
        self.redis = aioredis.from_url(
            url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=1.0, retry_on_timeout=False
        )
        self._bad_adverts: set[str] = set()

    async def close(self) -> None:
        await self.redis.aclose()

    async def write_advert(self, advert: Advert) -> None:
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.set(advert_key(self.hostname), advert.model_dump_json(), px=self.hold_ms)
            pipe.set(loopback_index_key(advert.loopback), self.hostname, px=self.hold_ms)
            await pipe.execute()

    async def publish(self, message: dict) -> None:
        await self.redis.publish(EVENTS_CHANNEL, json.dumps(message, separators=(",", ":")))

    async def snapshot(self) -> BusSnapshot:
        """Fresh adverts (with age) and the faults/cuts that concern this device, in two round trips."""
        keys = [k async for k in self.redis.scan_iter(match=f"{KEY_PREFIX}*", count=1000)]
        adv_keys, fault_keys = relevant_keys(self.hostname, keys)
        async with self.redis.pipeline(transaction=False) as pipe:
            for key in adv_keys:
                pipe.get(key)
                pipe.pttl(key)
            for key in fault_keys:
                pipe.get(key)
            results = await pipe.execute()
        n = len(adv_keys)
        adverts = list(zip(adv_keys, results[0 : 2 * n : 2], results[1 : 2 * n : 2], strict=True))
        faults = list(zip(fault_keys, results[2 * n :], strict=True))
        return build_snapshot(self.hostname, self.hold_ms, adverts, faults, self._bad_adverts)

    async def listen(self, wake: Callable[[str], None], stop: asyncio.Event) -> None:
        """Call ``wake(reason)`` for every bus message that may change our evaluation (reconnects forever)."""
        backoff = 0.2
        channels = [EVENTS_CHANNEL, f"__keyevent@{self.db}__:expired"]
        patterns = [f"__keyspace@{self.db}__:{FAULT_PATTERN}", f"__keyspace@{self.db}__:{CUT_PATTERN}"]
        while not stop.is_set():
            client = aioredis.from_url(self.url, decode_responses=True, socket_connect_timeout=1.0)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(*channels)
                await pubsub.psubscribe(*patterns)
                backoff = 0.2
                wake("bus-subscribed")
                while not stop.is_set():
                    msg = await pubsub.get_message(timeout=1.0)
                    if msg is None:
                        continue
                    channel = msg.get("channel") or ""
                    if channel.endswith(":expired"):
                        if str(msg.get("data", "")).startswith(ADVERT_PREFIX):
                            wake("advert-expired")
                    elif channel == EVENTS_CHANNEL:
                        wake("event")
                    else:
                        wake("fault-key")
            except (RedisError, OSError) as exc:
                log.warning("bus subscription lost: %s", exc, extra={"fields": {"event": "bus-error"}})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 2.0)
            finally:
                try:
                    await pubsub.aclose()
                    await client.aclose()
                except (RedisError, OSError):
                    pass


def relevant_keys(hostname: str, keys: list[str]) -> tuple[list[str], list[str]]:
    """Advert keys, and the fault/cut keys that name ``hostname``."""
    adverts = sorted(k for k in keys if k.startswith(ADVERT_PREFIX))
    faults = sorted(k for k in keys if (p := parse_fault_key(k)) is not None and hostname in p.hosts)
    return adverts, faults


def build_snapshot(
    hostname: str,
    hold_ms: int,
    adverts: list[tuple[str, str | None, int | None]],
    faults: list[tuple[str, str | None]],
    bad: set[str] | None = None,
) -> BusSnapshot:
    """Interpret raw bus values: ``adverts`` = (key, json, pttl ms), ``faults`` = (key, value)."""
    bad = set() if bad is None else bad
    fresh: dict[str, Advert] = {}
    ages: dict[str, float] = {}
    for key, raw, ttl in adverts:
        if raw is None:
            continue  # expired between SCAN and GET
        host = key.removeprefix(ADVERT_PREFIX)
        try:
            advert = Advert.model_validate_json(raw)
        except ValidationError as exc:
            if key not in bad:
                bad.add(key)
                log.warning("ignoring invalid advert %s: %s", key, exc, extra={"fields": {"event": "bad-advert"}})
            continue
        if advert.hostname != host:
            continue
        fresh[host] = advert
        ages[host] = max(0.0, (hold_ms - ttl) / 1000) if isinstance(ttl, int) and ttl >= 0 else 0.0
    uplink = False
    ifaces: set[str] = set()
    cuts: set[str] = set()
    for key, value in faults:
        parsed = parse_fault_key(key)
        if value != FAULT_ON or parsed is None or hostname not in parsed.hosts:
            continue
        if parsed.kind == "uplink":
            uplink = True
        elif parsed.kind == "ac" and parsed.ifname:
            ifaces.add(parsed.ifname)
        elif parsed.kind == "cut":
            cuts.update(h for h in parsed.hosts if h != hostname)
    return BusSnapshot(fresh, ages, LocalFaults(uplink, frozenset(ifaces), frozenset(cuts)))


def state_message(hostname: str, boot_id: str, seq: int) -> dict:
    return {"type": "state", "host": hostname, "boot_id": boot_id, "seq": seq, "ts": round(time.time(), 6)}
