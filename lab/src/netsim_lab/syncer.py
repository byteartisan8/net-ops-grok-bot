"""Debounced ``zabbix-sync`` for lab-api: NetBox webhooks and ``POST /sync`` request it, one thread runs it.

NetBox sends one webhook per changed object (a seed run sends dozens), and a sync with refresh takes 15-25 s
(04 report), so requests are coalesced: the sync starts ``debounce_s`` after the last request, never runs twice
at once, and a request that arrives during a run schedules exactly one more run afterwards.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any


class SyncRunner:
    def __init__(
        self,
        run: Callable[[], dict[str, Any]],
        debounce_s: float = 2.0,
        on_done: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._run = run
        self.debounce_s = debounce_s
        self._on_done = on_done
        self._cond = threading.Condition()
        self._due: float | None = None
        self._reasons: list[str] = []
        self._running = False
        self._runs = 0
        self._last: dict[str, Any] | None = None
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="zabbix-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def request(self, reason: str, delay: float | None = None) -> None:
        """Ask for a sync ``delay`` (default ``debounce_s``) seconds after the latest request."""
        with self._cond:
            self._due = time.monotonic() + (self.debounce_s if delay is None else delay)
            self._reasons.append(reason)
            self._cond.notify_all()

    def wait(self, after_run: int, timeout: float) -> bool:
        """Wait until run number ``after_run`` has finished (``runs`` counts finished runs)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._runs < after_run:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def run_now(self, reason: str, wait: bool, timeout: float = 120.0) -> dict[str, Any]:
        """Run as soon as possible (a run in progress finishes first); ``wait`` for the run that includes this."""
        with self._cond:
            target = self._runs + (2 if self._running else 1)
            self._due = time.monotonic()
            self._reasons.append(reason)
            self._cond.notify_all()
        if wait:
            self.wait(target, timeout)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {
                "pending": self._due is not None,
                "due_in_s": round(max(0.0, self._due - time.monotonic()), 2) if self._due is not None else None,
                "running": self._running,
                "runs": self._runs,
                "debounce_s": self.debounce_s,
                "last": self._last,
            }

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._stop and (self._due is None or time.monotonic() < self._due):
                    timeout = None if self._due is None else max(0.0, self._due - time.monotonic())
                    self._cond.wait(timeout)
                if self._stop:
                    return
                reasons, self._reasons = self._reasons, []
                self._due = None
                self._running = True
            started = time.time()
            try:
                result = {"ok": True, **self._run()}
            except Exception as exc:  # recorded and reported; the next request retries
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            result.update(started=started, finished=time.time(), reasons=sorted(set(reasons)), requests=len(reasons))
            with self._cond:
                self._running = False
                self._runs += 1
                self._last = result
                self._cond.notify_all()
            if self._on_done:
                with contextlib.suppress(Exception):  # auditing must never stop the sync thread
                    self._on_done(result)
