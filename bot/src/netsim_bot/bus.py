"""In-process event bus: the engine publishes, every NOC page (SSE) and ``botctl watch`` subscribes.

Events are ``{"id", "ts", "type", "data"}`` with a monotonically increasing ``id``; the last ``replay`` events are
kept so a page that reconnects with ``Last-Event-ID`` misses nothing. A slow subscriber's queue drops its oldest
events instead of blocking the engine.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import time
from typing import Any


class Subscription:
    def __init__(self, bus: EventBus, queue: asyncio.Queue[dict[str, Any]], backlog: list[dict[str, Any]], last: int):
        self._bus = bus
        self._queue = queue
        self._backlog = backlog
        self.last = last

    async def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next event, or ``None`` after ``timeout`` seconds without one (time for a keep-alive)."""
        while True:
            if self._backlog:
                event = self._backlog.pop(0)
            else:
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout)
                except TimeoutError:
                    return None
            if event["id"] > self.last:
                self.last = event["id"]
                return event

    def close(self) -> None:
        self._bus._subscribers.discard(self._queue)


class EventBus:
    def __init__(self, replay: int = 500, queue_size: int = 2000) -> None:
        self._ids = itertools.count(1)
        self._recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=replay)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    def publish(self, type_: str, data: Any) -> dict[str, Any]:
        event = {"id": next(self._ids), "ts": round(time.time(), 3), "type": type_, "data": data}
        self._recent.append(event)
        for q in list(self._subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(event)
        return event

    def recent(self, after: int | None = None, types: set[str] | None = None) -> list[dict[str, Any]]:
        return [e for e in self._recent if (after is None or e["id"] > after) and (types is None or e["type"] in types)]

    @property
    def last_id(self) -> int:
        return self._recent[-1]["id"] if self._recent else 0

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def open(self, after: int | None = None) -> Subscription:
        """Subscribe; with ``after``, the kept events newer than that id come first."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        if after is None:
            return Subscription(self, q, [], self.last_id)
        return Subscription(self, q, self.recent(after), after)
