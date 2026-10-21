"""The pager: one webhook per incident event, for an outside agent that should hear about faults on its own.

Built for a Grok Bot routine triggered "when a webhook fires" (Grok Bot routines are Cursor automations: the
webhook URL takes a JSON body and ``Authorization: Bearer <key>``), but any webhook receiver works.

Why the bot pages and not a Zabbix action: one fault raises 7-14 Zabbix problems over a few seconds (a hub power-off),
and every page starts a paid agent run. The watcher's settle window and service joining already turn those into one
incident, so the pager sends one page per incident, not one per problem.

Pages are sent in the background (three tries, the first after 0 s, then 2 s and 6 s), never block the watcher, are
capped per hour (``BOT_PAGER_MAX_PER_HOUR``) and are skipped in maintenance mode or while muted (``botctl pager
mute``; both e2e suites mute it). The last deliveries are kept for ``botctl pager status``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from netsim_bot import __version__
from netsim_bot.models import now
from netsim_bot.settings import SEVERITIES, BotSettings

log = logging.getLogger("netsim_bot.pager")

RETRY_DELAYS_S = (0.0, 2.0, 6.0)
KEEP = 30
HOW_TO = (
    "You are paged by the NetSim NOC (an L2VPN lab: NetBox intent, simulated PEs, Zabbix). Investigate with the "
    "netsim-noc MCP tools (or `uv run botctl call <tool> '<json>'` in the repository on the operator's computer): "
    "start with get_incident for this incident, then get_open_problems, list_services, get_drift, get_device_state. "
    "Fixes are proposals (dry runs) that a human approves in the NOC room; you cannot approve or apply anything. "
    "Pass incident_id when you propose, and add_incident_note to leave your findings on the incident board."
)


def severity_rank(severity: str | None) -> int:
    try:
        return SEVERITIES.index(severity or "Not classified")
    except ValueError:
        return 0


class Pager:
    def __init__(
        self,
        settings: Callable[[], BotSettings],
        on_delivery: Callable[[dict[str, Any]], None] = lambda _d: None,
        history: list[dict[str, Any]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._on_delivery = on_delivery
        self._transport = transport
        self.history: deque[dict[str, Any]] = deque(history or [], maxlen=KEEP)
        self._sent: deque[float] = deque()
        self._paged: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.delays = RETRY_DELAYS_S

    # ------------------------------------------------------------------ policy
    def decide(self, event: str, incident: dict[str, Any], *, muted: bool, paused: bool) -> str | None:
        """``None`` when this event should be paged, else why not (shown in ``botctl pager status``)."""
        s = self._settings()
        if not s.pager_url:
            return "BOT_PAGER_URL is not set"
        if event not in s.pager_events:
            return f"event {event} is not in BOT_PAGER_EVENTS"
        if muted:
            return "muted"
        if paused and event != "resolved":
            return "maintenance mode"
        if event != "opened" and incident.get("id") in self._paged:
            return None  # its opening was paged: follow-ups go out whatever the severity is now
        if severity_rank(incident.get("severity")) < severity_rank(s.pager_min_severity):
            return f"severity {incident.get('severity')} below BOT_PAGER_MIN_SEVERITY={s.pager_min_severity}"
        if event != "opened" and "opened" in s.pager_events:
            return "its opening was not paged"
        return None

    def _rate_ok(self) -> bool:
        limit = self._settings().pager_max_per_hour
        t = time.monotonic()
        while self._sent and t - self._sent[0] > 3600:
            self._sent.popleft()
        return limit > 0 and len(self._sent) < limit

    # ------------------------------------------------------------------ sending
    def page(
        self, event: str, incident: dict[str, Any], body: dict[str, Any], *, muted: bool = False, paused: bool = False
    ) -> str | None:
        """Schedule a page; returns why it was skipped, or ``None`` when it was queued."""
        why = self.decide(event, incident, muted=muted, paused=paused)
        if why is None and not self._rate_ok():
            why = f"rate limit: BOT_PAGER_MAX_PER_HOUR={self._settings().pager_max_per_hour}"
        if why is not None:
            if why not in ("BOT_PAGER_URL is not set",) and not why.startswith("event "):
                self._record({"event": event, "incident_id": incident.get("id"), "status": "skipped", "why": why})
            return why
        self._sent.append(time.monotonic())
        if event == "opened" and incident.get("id"):
            self._paged.add(incident["id"])
        payload = self.payload(event, body)
        task = asyncio.ensure_future(self.deliver(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    def payload(self, event: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "netsim-grok-bot",
            "version": __version__,
            "event": f"incident.{event}",
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **body,
            "how_to_respond": HOW_TO,
        }

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        s = self._settings()
        headers = {"Content-Type": "application/json", "User-Agent": f"netsim-grok-bot/{__version__}"}
        if s.pager_token:
            headers["Authorization"] = f"Bearer {s.pager_token}"
        incident = payload.get("incident") or {}
        rec: dict[str, Any] = {
            "event": payload.get("event", "").removeprefix("incident."),
            "incident_id": incident.get("id"),
            "status": "failed",
            "attempts": 0,
        }
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            for delay in self.delays:
                if delay:
                    await asyncio.sleep(delay)
                rec["attempts"] += 1
                try:
                    r = await client.post(s.pager_url or "", json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                rec["http"] = r.status_code
                if r.status_code < 300:
                    rec["status"] = "sent"
                    rec.pop("error", None)
                    break
                rec["error"] = r.text[:200]
                if r.status_code < 500 and r.status_code != 429:
                    break  # 4xx: the URL or the key is wrong; retrying will not help
        rec["seconds"] = round(time.monotonic() - started, 2)
        self._record(rec)
        return rec

    def _record(self, rec: dict[str, Any]) -> None:
        rec.setdefault("ts", now())
        self.history.append(rec)
        try:
            self._on_delivery(rec)
        except Exception:  # reporting must never break paging
            log.exception("pager delivery hook failed")

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
