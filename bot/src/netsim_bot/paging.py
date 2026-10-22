"""Paging the roles' routines: one webhook POST per page (a Grok Bot routine "when a webhook fires").

Grok Bot routines are Cursor automations: the webhook URL takes a JSON body and ``Authorization: Bearer <key>``. Each
role has its own routine (``BOT_ROUTINE_<ROLE>_URL`` / ``_KEY``); every page starts a paid run, which is why alerts are
batched before anyone is paged.

* Pages are sent in the background: three tries (after 0 s, 2 s, 6 s), no retry on a 4xx other than 429 (the URL or
  the key is wrong), never blocking the alert intake.
* At most ``BOT_PAGE_MAX_PER_HOUR`` pages per role and hour; the rest are logged as skipped.
* Skipped, and logged: a role without a routine URL, pages muted, maintenance mode.
* The test sink (control ``sink_url``, operator only, a lab address): every page goes to ``<sink_url>/<role>`` with
  ``Authorization: Bearer test-sink`` instead of the routine (never with a real key) and outside the hourly cap. The
  e2e tests and ``make bot-sink-up`` use it.
* Every page is a row in SQLite (``botctl page status``) and an activity: the role's desk phone rings in the room.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from netsim_bot import __version__
from netsim_bot.models import Page
from netsim_bot.settings import ROLES, BotSettings
from netsim_bot.store import Store

log = logging.getLogger("netsim_bot.paging")

RETRY_DELAYS_S = (0.0, 2.0, 6.0)
SINK_KEY = "test-sink"
SINK_NETWORKS = (ipaddress.ip_network("172.30.10.0/24"), ipaddress.ip_network("127.0.0.0/8"))


def sink_allowed(url: str) -> bool:
    """The test sink must be a lab address (labnet or loopback): pages carry incident data."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        if parts.hostname == "localhost":
            return True
        addr = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return False
    return any(addr in net for net in SINK_NETWORKS)


class Paging:
    def __init__(
        self,
        settings: Callable[[], BotSettings],
        store: Store,
        on_record: Callable[[Page], None] = lambda _p: None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self.store = store
        self._on_record = on_record
        self._transport = transport
        self._tasks: set[asyncio.Task[Any]] = set()
        self.delays = RETRY_DELAYS_S

    # ------------------------------------------------------------------ policy
    def skip_reason(self, role: str, *, muted: bool, paused: bool, sink: bool) -> str | None:
        s = self._settings()
        spec = ROLES[role]
        if muted:
            return "pages muted"
        if paused:
            return "maintenance mode"
        if sink:
            return None
        if not s.routine(role).configured:
            return f"{spec.url_env} is not set: no routine to page"
        cap = s.page_max_per_hour
        sent = [p for p in self.store.pages(role=role, since=time.time() - 3600, limit=1000) if not p.sink]
        if len([p for p in sent if p.status in ("queued", "sent", "failed")]) >= cap:
            return f"hourly cap: BOT_PAGE_MAX_PER_HOUR={cap}"
        return None

    # ------------------------------------------------------------------ sending
    def page(
        self,
        role: str,
        event: str,
        summary: str,
        body: dict[str, Any],
        *,
        incident_id: str | None = None,
        muted: bool = False,
        paused: bool = False,
        sink_url: str | None = None,
        force: bool = False,
    ) -> Page:
        """Record a page and send it in the background (or record why it is skipped). ``force``: ignore mute,
        maintenance and the cap (``botctl page test``)."""
        sink = bool(sink_url)
        p = Page(
            id=self.store.next_id("PG"), role=role, event=event, summary=summary, incident_id=incident_id, sink=sink
        )
        p.payload = self.payload(p, body)
        if not force:
            why = self.skip_reason(role, muted=muted, paused=paused, sink=sink)
        elif sink or self._settings().routine(role).configured:
            why = None
        else:
            why = f"{ROLES[role].url_env} is not set: no routine to page"
        if why is not None:
            p.status, p.why = "skipped", why
            self._record(p)
            return p
        self._record(p)
        task = asyncio.ensure_future(self.deliver(p, sink_url))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return p

    def payload(self, p: Page, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "netsim-noc",
            "version": __version__,
            "page_id": p.id,
            "event": p.event,
            "role": p.role,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "summary": p.summary,
            **body,
        }

    async def deliver(self, p: Page, sink_url: str | None = None) -> Page:
        s = self._settings()
        if sink_url:
            url, key = f"{sink_url.rstrip('/')}/{p.role}", SINK_KEY
        else:
            routine = s.routine(p.role)
            url, key = routine.url or "", routine.key
        headers = {"Content-Type": "application/json", "User-Agent": f"netsim-grok-bot/{__version__}"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            for delay in self.delays:
                if delay:
                    await asyncio.sleep(delay)
                p.attempts += 1
                try:
                    r = await client.post(url, json=p.payload, headers=headers)
                except httpx.HTTPError as exc:
                    p.error = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                p.http = r.status_code
                if r.status_code < 300:
                    p.status, p.error = "sent", None
                    break
                p.error = r.text[:200]
                if r.status_code < 500 and r.status_code != 429:
                    break  # 4xx: the URL or the key is wrong; retrying will not help
        if p.status != "sent":
            p.status = "failed"
        p.seconds = round(time.monotonic() - started, 2)
        self._record(p)
        return p

    def _record(self, p: Page) -> None:
        self.store.save_page(p)
        try:
            self._on_record(p)
        except Exception:  # reporting must never break paging
            log.exception("page record hook failed")

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait until every page in flight is delivered (tests)."""
        deadline = time.monotonic() + timeout
        while self._tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
