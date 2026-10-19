"""In-memory ring buffer of device events, served by ``GET /events?since=<ts>``.

Event types (``type``): ``boot``, ``config-change``, ``oper-change`` (PW transition), ``reason-change`` (same
oper, new reason), ``pw-added``, ``pw-removed``, ``if-oper-change``, ``fault-change``, ``neighbor-up``,
``neighbor-down``, ``trap-sent``. Every event has ``id`` (monotonic per boot), ``ts`` (epoch seconds),
``time`` (ISO 8601 UTC), ``host``, ``boot_id`` and ``type``.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from datetime import datetime
from typing import Any

from netsim_simd.jsonlog import iso_ts


def parse_since(value: str | float | None) -> float | None:
    """``since`` as epoch seconds: a number, or an ISO 8601 timestamp (``Z`` or offset)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ValueError(f"since={value!r}: expected epoch seconds or an ISO 8601 timestamp") from None


class EventLog:
    def __init__(self, host: str, boot_id: str, maxlen: int = 1000) -> None:
        self.host = host
        self.boot_id = boot_id
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)

    @property
    def capacity(self) -> int:
        return self._events.maxlen or 0

    def add(self, type_: str, **fields: Any) -> dict[str, Any]:
        now = time.time()
        event = {
            "id": next(self._ids),
            "ts": round(now, 6),
            "time": iso_ts(now),
            "host": self.host,
            "boot_id": self.boot_id,
            "type": type_,
            **fields,
        }
        self._events.append(event)
        return event

    def since(
        self, ts: float | None = None, limit: int | None = None, types: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Events with ``ts > since`` (all if None), oldest first; ``limit`` keeps the newest ones."""
        out = [e for e in self._events if (ts is None or e["ts"] > ts) and (not types or e["type"] in types)]
        if limit is not None and limit >= 0:
            out = out[-limit:] if limit else []
        return out

    def __len__(self) -> int:
        return len(self._events)
