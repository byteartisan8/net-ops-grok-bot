"""The NOC office: the lab feed, the alert intake, incidents, pages, the roles' work, change tickets and the executor.

* **The lab feed** polls lab-api every ``BOT_POLL_S`` (problems, services, devices; the topology every minute): the
  wall screen, the rack LEDs and the incident board. Polling is truth; nothing here opens an incident.
* **The alert intake**: Zabbix's service action ``NetSim: customer alert`` posts customer alerts to
  ``/hooks/zabbix``. Each one is stored, then batched per role (:mod:`netsim_bot.incidents`): a closed batch opens
  one incident with one page, joins an open incident (same fault), or updates one (worse, better, recovered). In
  maintenance mode, or with pages muted, alerts are only recorded.
* **Pages** (:mod:`netsim_bot.paging`) wake the roles' routines: the NOC Engineer for a new incident, a customer
  getting worse and "all recovered"; the Network Engineer for an escalation; the NOC Manager for the Zabbix
  escalation step, a gold customer down, change tickets to review (batched), an escalation and Ask-the-NOC
  questions; the engineer who filed a ticket when the NOC Manager returns it.
* **The roles** work through ``/mcp`` (:mod:`netsim_bot.roles`); the methods below are what their tools do.
* **Change tickets** are lab-api dry runs (or field repairs) an engineer files. They wait for the NOC Manager's
  review: it proposes each one to the operator or returns it to its engineer. Only a proposed ticket can be approved.
  A ticket goes to the operator without a review when the NOC Manager cannot be paged for it, or has not reviewed it
  within ``BOT_REVIEW_TIMEOUT_S``. A human approves (NOC page, ``botctl approve``); the executor runs approved tickets
  one at a time, make before break when several are approved together, confirming exactly the approved ``plan_id``
  (a changed device or NetBox: HTTP 409, the ticket goes back to the NOC Manager's review with the new plan). The
  actor is ``grok-bot/<ROLE> approved-by:<who>``.
* **Field repairs** happen off-screen: an approved field ticket goes dispatched -> on site (``BOT_FIELD_DELAY_S``) ->
  repaired (lab-api restores the fault), on the ticket and the board.
* **Ask the NOC**: a question pages the NOC Manager; ``answer_question`` brings the answer to the panel; an
  unanswered question times out after ``BOT_ASK_TIMEOUT_S``.
* Everything is an event on the bus (SSE for the NOC page) and a row in SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from netsim_bot import __version__
from netsim_bot.alerts import is_customer_alert, parse_alert
from netsim_bot.bus import EventBus
from netsim_bot.incidents import Batcher, PageRequest, apply_batch, customer_order
from netsim_bot.labapi import LabApi, LabApiError
from netsim_bot.models import (
    Activity,
    Alert,
    ChatMessage,
    Incident,
    Page,
    Proposal,
    RoleMember,
    now,
)
from netsim_bot.paging import Paging, sink_allowed
from netsim_bot.roles import RoleGateway
from netsim_bot.settings import AUTONOMIES, NETENG, NOCENG, NOCMGR, ROLE_BY_BOT, ROLES, BotSettings, load_settings
from netsim_bot.store import Store
from netsim_bot.tools import compact_service

log = logging.getLogger("netsim_bot")

TICKETS = "tickets"
REBATCH_S = 600.0  # alerts still "batched" after a restart are batched again if younger than this
ESCALATION_TARGETS = {NOCENG: (NETENG, NOCMGR), NETENG: (NOCMGR,), NOCMGR: ()}

HOW_TO: dict[str, str] = {
    "incident.opened": "You are the NOC Engineer and you are on call for this incident. take_incident {id}, then "
    "look at it (get_incident, get_open_problems, list_services, get_impact on the host they share), acknowledge it "
    "in Zabbix (ack_zabbix_problems) and file submit_triage. A physical fault: propose_field_repair. Config drift or "
    "intent: escalate to network-engineer. Customers or priorities: escalate to noc-manager. Your tickets go to the "
    "NOC Manager's review, then to a human in the NOC room.",
    "incident.worse": "You are the NOC Engineer. Customers of {id} got worse (degraded -> down, or bad again): "
    "check get_incident and get_impact, update your triage, escalate to noc-manager when a gold customer is down.",
    "incident.recovered": "You are the NOC Engineer. Zabbix shows every customer of {id} OK again. Verify "
    "(get_noc_status; zabbix_get service.get with tags bsm_level=customer), then close_incident {id} with a summary.",
    "incident.escalated:network-engineer": "You are the Network Engineer. {id} was escalated to you (the note and "
    "the triage are below). Find the root cause (get_drift, get_device_state, get_device_intent, "
    "device_restconf_get, netbox_get), submit_root_cause, then propose_apply_intent or propose_config_patch, one per "
    "host, additions before removals. The NOC Manager reviews every ticket before a human can approve it.",
    "incident.escalated:noc-manager": "You are the NOC Manager. {id} was escalated to you (the note is below): "
    "set_priority, write_customer_update, and review_ticket on its change tickets. You never approve.",
    "incident.gold-down": "You are the NOC Manager. A gold customer of {id} is down: set_priority (P1 for gold down), "
    "write_customer_update; the NOC Engineer is working on it (get_incident shows who).",
    "incident.zabbix-escalation": "You are the NOC Manager. Zabbix escalated {id}: its customers are still not OK "
    "after {{$NETSIM.ESCALATE.AFTER}}. Check the progress (get_incident), set_priority, write_customer_update, and "
    "chase the owner with add_incident_note.",
    "tickets.filed": "You are the NOC Manager. Change tickets wait for your review; nobody can approve them before "
    "it. For each: get_proposal (the diff, the services it touches, the predicted drift after) and get_incident (does "
    "it fix the root cause?), then review_ticket: propose sends it to the operator, who decides; return sends it back "
    "to the engineer who filed it, with your reason. Review an incident's tickets together, make before break. You "
    "never approve.",
    "ticket.returned": "The NOC Manager returned your change ticket (below) from its review, with the reason. Fix what "
    "it says and file a new ticket (propose_*), or escalate to noc-manager if you disagree. The returned ticket can no "
    "longer be approved.",
    "question": "You are the NOC Manager. The operator asked the NOC a question: look it up with your tools, then "
    "answer_question with its question_id. Keep it short: at most 8 sentences or a short list.",
    "test": "A test page from the NetSim NOC: nothing to do, the page arrived.",
}


@dataclass
class Control:
    paused: bool = False
    reason: str | None = None
    autonomy: str | None = None
    pages_muted: bool = False
    sink_url: str | None = None
    by: str | None = None
    since: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_meta(cls, data: dict[str, Any]) -> Control:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


@dataclass
class LabSnapshot:
    ts: float = 0.0
    ok: bool = False
    error: str | None = None
    root: str | None = None
    services: list[dict[str, Any]] = field(default_factory=list)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[dict[str, Any]] = field(default_factory=list)
    traps: int = 0
    regions: dict[str, list[str]] = field(default_factory=dict)
    device_info: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def hosts(self) -> list[str]:
        return sorted(self.device_info) or sorted(self.devices)

    @property
    def loopbacks(self) -> dict[str, str]:
        return {d["loopback"]: h for h, d in self.device_info.items() if d.get("loopback")}

    def service_map(self) -> dict[str, dict[str, Any]]:
        return {s["service"]: s for s in self.services}

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for p in self.problems:
            counts[p.get("severity") or "?"] = counts.get(p.get("severity") or "?", 0) + 1
        devices = {}
        for host in self.hosts:
            d = self.devices.get(host) or {}
            info = self.device_info.get(host) or {}
            devices[host] = {
                "role": info.get("role") or ("hub" if host.startswith("hub") else "spoke"),
                "region": info.get("region"),
                "site": info.get("site"),
                "reachable": d.get("reachable"),
                "powered_off": d.get("stopped"),
                "uplink": d.get("uplink"),
                "faults": {k: v for k, v in (d.get("faults") or {}).items() if v},
                "pws_up": d.get("pws_up"),
                "pws": d.get("pws"),
            }
        return {
            "ts": self.ts,
            "ok": self.ok,
            "error": self.error,
            "root": self.root,
            "services": self.services,
            "devices": devices,
            "problems": self.problems,
            "counts": counts,
            "traps": self.traps,
            "regions": self.regions,
        }


def slim_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not plan:
        return None

    def messages(items: list[dict[str, Any]] | None) -> list[str]:
        return [i.get("message", "") for i in items or []]

    return {
        "plan_id": plan.get("plan_id"),
        "no_change": plan.get("no_change"),
        "diff": [
            {k: d.get(k) for k in ("path", "change", "from", "to", "message", "service_id")}
            for d in plan.get("diff") or []
        ],
        "validation": plan.get("validation"),
        "affected_services": plan.get("affected_services") or [],
        "affected_hosts": plan.get("affected_hosts") or [],
        "drift_before": messages(plan.get("drift_before")),
        "drift_after_predicted": messages(plan.get("drift_after_predicted")),
        "running_hash": (plan.get("running_hash") or "")[:12],
        "candidate_hash": (plan.get("candidate_hash") or "")[:12],
    }


def slim_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    if "verification" not in result:
        return result
    v = result.get("verification") or {}
    z = v.get("zabbix") or {}
    return {
        "applied": result.get("applied"),
        "verdict": v.get("verdict"),
        "problems": v.get("problems") or [],
        "pseudowires": (v.get("simd") or {}).get("pseudowires"),
        "zabbix_services": z.get("services"),
        "zabbix_settled": z.get("settled"),
        "drift_after": [d.get("message", "") for d in v.get("drift_after") or []],
        "elapsed_s": result.get("elapsed_s"),
    }


def change_order(p: Proposal) -> tuple[int, float]:
    """Field repairs first, then config changes that only add, then changes, then pure removals."""
    if p.kind == "field-repair":
        return (0, p.created)
    kinds = {d.get("change") for d in (p.plan or {}).get("diff") or []}
    if kinds and kinds <= {"added"}:
        return (1, p.created)
    if kinds and kinds <= {"removed"}:
        return (3, p.created)
    return (2, p.created)


def _iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


class Engine:
    def __init__(
        self,
        settings_loader: Callable[[], BotSettings] = load_settings,
        store: Store | None = None,
        bus: EventBus | None = None,
        lab: LabApi | None = None,
        page_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._load = settings_loader
        self._settings = settings_loader()
        self._settings_at = time.monotonic()
        s = self._settings
        self.store = store or Store(s.data_dir / "bot.db")
        self.bus = bus or EventBus()
        self.lab = lab or LabApi(s.lab_api_url, token=lambda: self.settings.lab_api_token, actor=s.actor)
        self._sleep = sleep or asyncio.sleep
        self.control = Control.from_meta(self.store.get_meta("control", {}))
        if self.control.autonomy not in (None, *AUTONOMIES):
            self.control.autonomy = None  # e.g. "auto" from the earlier crew
        self.roles: dict[str, RoleMember] = {
            key: RoleMember(name=spec.bot, role=key, title=spec.title, color=spec.color, hat=spec.hat)
            for key, spec in ROLES.items()
        }
        self.gateway = RoleGateway(self)
        self.paging = Paging(lambda: self.settings, self.store, self._paged, transport=page_transport)
        self.batcher = Batcher(s.batch_s, s.max_batch_s, lambda: self.store.next_id("BAT", 5))
        self.ticket_batcher = Batcher(s.batch_s, s.max_batch_s, lambda: self.store.next_id("BAT", 5))
        self.incidents: dict[str, Incident] = {}
        self.snapshot = LabSnapshot()
        self._lab_hash = ""
        self._lab_published = 0.0
        self._topology_at = 0.0
        self._loops: list[asyncio.Task[Any]] = []
        self._exec_queue: asyncio.Queue[str] = asyncio.Queue()
        self._propose_lock = asyncio.Lock()
        self._recover()

    # ------------------------------------------------------------------ settings, state
    @property
    def settings(self) -> BotSettings:
        if time.monotonic() - self._settings_at > 2.0:
            try:
                self._settings = self._load()
                self.batcher.configure(self._settings.batch_s, self._settings.max_batch_s)
                self.ticket_batcher.configure(self._settings.batch_s, self._settings.max_batch_s)
            except Exception as exc:  # keep the last good settings
                log.warning("settings reload failed: %s", exc)
            self._settings_at = time.monotonic()
        return self._settings

    @property
    def autonomy(self) -> str:
        return self.control.autonomy or self.settings.autonomy

    def effective(self) -> dict[str, Any]:
        s = self.settings
        return {
            "autonomy": self.autonomy,
            "paused": self.control.paused,
            "pages_muted": self.control.pages_muted,
            "sink": bool(self.control.sink_url),
            "hook": bool(s.hook_token),
            "routines": {k: s.routine(k).configured for k in ROLES},
            "warnings": list(s.warnings),
        }

    def batches(self) -> list[dict[str, Any]]:
        out = []
        for b in self.batcher.open.values():
            out.append({"id": b.id, "role": b.key, "alerts": len(b.items), "closes_at": self.batcher.deadline(b)})
        return out

    def state(self) -> dict[str, Any]:
        incidents = self.store.incidents(limit=40)
        open_ = [*self.store.proposals(status="review", limit=50), *self.store.proposals(status="pending", limit=50)]
        recent = self.store.proposals(limit=40)
        proposals = {p.id: p for p in [*open_, *recent]}
        return {
            "version": __version__,
            "settings": self.settings.public(),
            "control": self.public_control(),
            "effective": self.effective(),
            "roles": self.roles_now(),  # right after a start too, before any role published anything
            "incidents": [i.summary() for i in incidents],
            "proposals": [p.to_dict() for p in sorted(proposals.values(), key=lambda p: p.created, reverse=True)],
            "lab": self.snapshot.to_dict(),
            "batches": self.batches(),
            "alerts": [a.to_dict() for a in self.store.alerts(limit=40)],
            "pages": [self.page_brief(p) for p in self.store.pages(limit=40)],
            "activities": [a.to_dict() for a in self.store.activities(limit=150)],
            "chat": [m.to_dict() for m in self.store.chat(limit=60)],
            "shift_reports": self.store.get_meta("shift_reports", [])[-5:],
            "last_event_id": self.bus.last_id,
        }

    def public_control(self) -> dict[str, Any]:
        c = self.control.to_dict()
        c["sink_url"] = bool(c.get("sink_url"))  # the page shows that a sink is set, not where
        return c

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loops = [
            asyncio.create_task(self._feed_loop(), name="feed"),
            asyncio.create_task(self._office_loop(), name="office"),
            asyncio.create_task(self._executor_loop(), name="executor"),
        ]

    async def stop(self) -> None:
        for task in self._loops:
            task.cancel()
        for task in self._loops:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.paging.aclose()
        await self.lab.aclose()
        self.store.close()

    async def sleep(self, seconds: float) -> None:
        await self._sleep(seconds)

    def _recover(self) -> None:
        """After a restart: open incidents are known again, interrupted tickets failed, unclosed batches redone."""
        for inc in self.store.incidents(limit=500, active_only=True):
            self.incidents[inc.id] = inc
        for p in self.store.proposals(limit=500):
            if p.status in ("approved", "applying"):
                p.status = "failed"
                p.error = "interrupted by a bot restart (check drift before re-proposing)"
                p.updated = now()
                self.store.save_proposal(p)
        t = now()
        for a in reversed(self.store.alerts(limit=500)):
            if a.outcome == "batched" and t - a.received < REBATCH_S:
                self.batcher.add(a.role, a, a.received)

    async def _feed_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.poll_lab()
            except asyncio.CancelledError:
                raise
            except Exception:  # the feed must never die
                log.exception("lab feed failed")
            await asyncio.sleep(max(0.2, self.settings.poll_s - (time.monotonic() - started)))

    async def _office_loop(self) -> None:
        while True:
            try:
                self.office_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("office tick failed")
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------------ the lab feed
    async def poll_lab(self) -> None:
        snap = self.snapshot
        t = time.time()
        try:
            if time.monotonic() - self._topology_at > 60 or not snap.device_info:
                topo = await self.lab.topology()
                snap.regions = topo.get("regions") or {}
                snap.device_info = {d["hostname"]: d for d in topo.get("devices") or []}
                self._topology_at = time.monotonic()
            raw = await self.lab.problems()
            problems = raw.get("problems") or []
            snap.problems = [
                {
                    k: p.get(k)
                    for k in ("eventid", "severity", "name", "host", "service", "pw_role", "check", "scope", "clock")
                }
                | {"acknowledged": bool(p.get("acknowledged"))}
                for p in problems
                if p.get("scope") != "l2vpn-trap"
            ]
            snap.traps = sum(1 for p in problems if p.get("scope") == "l2vpn-trap")
            services = await self.lab.services()
            snap.root = services.get("zabbix_root")
            snap.devices = services.get("devices") or {}
            region_of = {h: d.get("region") for h, d in snap.device_info.items()}
            snap.services = []
            for s in services.get("services") or []:
                c = compact_service(s)
                c["region"] = region_of.get(c.get("spoke") or "")
                snap.services.append(c)
            snap.ok, snap.error, snap.ts = True, None, round(t, 3)
        except LabApiError as exc:
            snap.ok, snap.error, snap.ts = False, str(exc), round(t, 3)
        payload = snap.to_dict()
        digest = hashlib.sha1(
            json.dumps({k: v for k, v in payload.items() if k != "ts"}, sort_keys=True, default=str).encode()
        ).hexdigest()
        if digest != self._lab_hash or t - self._lab_published > 15:
            self._lab_hash = digest
            self._lab_published = t
            self.bus.publish("lab", payload)

    # ------------------------------------------------------------------ the alert intake
    def receive_alert(self, body: Any) -> Alert:
        """One Zabbix customer alert (``POST /hooks/zabbix``): stored, then batched (or only recorded)."""
        alert = parse_alert(body, self.store.next_id("ALR", 6), self.snapshot.loopbacks)
        why = None
        if not is_customer_alert(alert):
            why = f"recorded: not a customer node (bsm_level={alert.tags.get('bsm_level')})"
        elif self.control.paused:
            why = "recorded: maintenance mode"
        elif self.control.pages_muted:
            why = "recorded: pages muted"
        if why:
            alert.outcome = why
        else:
            batch = self.batcher.add(alert.role, alert, alert.received)
            alert.outcome, alert.batch_id = "batched", batch.id
            self.bus.publish("batches", self.batches())
        self.store.save_alert(alert)
        self.bus.publish("alert", alert.to_dict())
        state = {"ok": "OK again", "down": "down", "degraded": "degraded"}[alert.state]
        self.emit(
            Activity(
                bot="zabbix",
                kind="alert",
                text=f"Zabbix: {alert.customer}{f' ({alert.tier})' if alert.tier else ''} {state}"
                + (f" [{alert.kind}]" if alert.kind != "problem" else "")
                + (f" - {why}" if why else ""),
                station="screen",
                ok=alert.state == "ok",
            )
        )
        return alert

    def office_tick(self) -> None:
        """Every 0.25 s: close due batches, time out questions and reviews, send the robots back to their desks."""
        t = now()
        due = self.batcher.due(t)
        for batch in due:
            self._close_batch(batch.items, t)
        if due:
            self.bus.publish("batches", self.batches())
        for batch in self.ticket_batcher.due(t):
            self._tickets_filed(batch.items)
        self._question_timeouts(t)
        self._review_timeouts(t)
        self.gateway.tick()

    def _close_batch(self, alerts: list[Alert], t: float) -> None:
        if self.control.paused or self.control.pages_muted:
            why = "recorded: maintenance mode" if self.control.paused else "recorded: pages muted"
            for a in alerts:
                a.outcome = why
                self.store.save_alert(a)
            return
        out = apply_batch(alerts, self.incidents, lambda: Incident(id=self.store.next_id("INC")), t)
        for a in out.alerts:
            self.store.save_alert(a)
        for inc in out.created:
            self._restatus(inc)
            self.emit(
                Activity(
                    bot="zabbix",
                    kind="alarm",
                    text=f"{inc.id} opened: {inc.title}",
                    incident_id=inc.id,
                    station="board",
                )
            )
        for inc in out.changed.values():
            self._restatus(inc)
        for inc in [*out.created, *out.changed.values()]:
            self._save(inc)
        for req in out.pages:
            self._page_incident(req)

    # ------------------------------------------------------------------ incidents
    def _restatus(self, inc: Incident) -> None:
        """The board column: closed > recovered > remediating > awaiting-approval > working > new."""
        if inc.status == "closed":
            return
        if not inc.bad_customers():
            inc.status = "recovered"
            inc.recovered = inc.recovered or now()
            return
        inc.recovered = None
        tickets = self.store.proposals(incident_id=inc.id)
        if any(p.status in ("approved", "applying") for p in tickets):
            inc.status = "remediating"
        elif any(p.status == "pending" for p in tickets):
            inc.status = "awaiting-approval"
        elif inc.assignee or inc.triage or inc.root_cause or inc.escalations or inc.acks or tickets:
            inc.status = "working"  # tickets in the NOC Manager's review are still the NOC's work
        else:
            inc.status = "new"

    def _save(self, inc: Incident) -> None:
        inc.updated = now()
        self.store.save_incident(inc)
        if inc.status == "closed":
            self.incidents.pop(inc.id, None)
        else:
            self.incidents[inc.id] = inc
        self.bus.publish("incident", inc.summary())

    def _incident(self, incident_id: str) -> Incident:
        inc = self.incidents.get(incident_id) or self.store.incident(incident_id)
        if inc is None:
            raise KeyError(f"no incident {incident_id}; list_incidents shows them")
        return inc

    def _open(self, incident_id: str) -> Incident:
        inc = self._incident(incident_id)
        if inc.status == "closed":
            raise ValueError(f"{incident_id} is closed")
        return inc

    def incident_brief(self, inc: Incident) -> dict[str, Any]:
        return {
            "id": inc.id,
            "status": inc.status,
            "severity": inc.severity,
            "priority": inc.priority,
            "title": inc.title,
            "assignee": inc.assignee,
            "customers": {
                n: {k: inc.customers[n].get(k) for k in ("tier", "state", "severity")} for n in customer_order(inc)
            },
            "counts": inc.counts(),
            "services": inc.services,
            "hosts": inc.hosts,
            "age_s": round(time.time() - inc.created),
            "proposals": inc.proposals,
        }

    def proposal_brief(self, p: Proposal) -> dict[str, Any]:
        plan = p.plan or {}
        result = p.result or {}
        return {
            "id": p.id,
            "incident_id": p.incident_id,
            "status": p.status,
            "kind": p.kind,
            "host": p.host,
            "headline": p.headline(),
            "proposed_by": p.proposed_by,
            "reason": p.reason,
            "diff": [d.get("message") for d in plan.get("diff") or []][:20],
            "affected_services": plan.get("affected_services"),
            "drift_after_predicted": plan.get("drift_after_predicted"),
            "stage": p.stage,
            "progress": p.progress,
            "reviews": p.reviews,
            "decided_by": p.decided_by,
            "comment": p.comment,
            "verdict": result.get("verdict") or result.get("done"),
            "zabbix_after": result.get("zabbix_services"),
            "drift_after": result.get("drift_after"),
            "error": p.error,
        }

    def page_brief(self, p: Page) -> dict[str, Any]:
        return {k: v for k, v in p.to_dict().items() if k != "payload"}

    def incident_view(self, incident_id: str) -> dict[str, Any]:
        inc = self._incident(incident_id)
        timeline = [
            {"ts": x.ts, "bot": x.bot, "kind": x.kind, "text": x.text}
            for x in self.store.activities(incident_id=incident_id, limit=400)
            if x.kind not in ("tool", "result")
        ][-40:]
        return {
            **self.incident_brief(inc),
            "summary": f"{inc.id} {inc.status}: {inc.title}",
            "customers": {n: self._customer_view(inc, n) for n in customer_order(inc)},
            "problems": inc.problems[:60],
            "triage": inc.triage,
            "root_cause": inc.root_cause,
            "priority_reason": inc.priority_reason,
            "escalations": inc.escalations,
            "acknowledgments": inc.acks,
            "customer_updates": inc.customer_updates,
            "closing": inc.closing,
            "notes": inc.notes[-20:],
            "tickets": [self.proposal_brief(p) for p in self.store.proposals(incident_id=incident_id)],
            "pages": [
                {k: p.to_dict()[k] for k in ("id", "role", "event", "status", "summary")}
                for p in self.store.pages(incident_id=incident_id, limit=40)
            ],
            "timeline": timeline,
        }

    def _customer_view(self, inc: Incident, name: str) -> dict[str, Any]:
        c = inc.customers[name]
        services = self.snapshot.service_map()
        return {
            "tier": c.get("tier"),
            "state": c.get("state"),
            "severity": c.get("severity"),
            "zabbix_event": c.get("event_id"),
            "worst": c.get("worst"),
            "recovered_at": _iso(c.get("recovered")),
            "hosts": c.get("hosts"),
            "services": [
                {k: (services.get(sid) or {}).get(k) for k in ("service", "spoke", "rules", "live", "zabbix")}
                | {"service": sid}
                for sid in c.get("services") or []
            ],
        }

    # ------------------------------------------------------------------ pages
    def _incident_body(self, inc: Incident, about: list[str]) -> dict[str, Any]:
        s = self.settings
        services = self.snapshot.service_map()
        customers = []
        for name in customer_order(inc):
            c = inc.customers[name]
            customers.append(
                {
                    "name": name,
                    "tier": c.get("tier"),
                    "state": c.get("state"),
                    "severity": c.get("severity"),
                    "zabbix_event": c.get("event_id"),
                    "this_page": name in about,
                    "services": [
                        {
                            "service": sid,
                            "tier": c.get("tier"),
                            "state": (services.get(sid) or {}).get("zabbix"),
                            "live": (services.get(sid) or {}).get("live"),
                            "rules": (services.get(sid) or {}).get("rules"),
                            "spoke": (services.get(sid) or {}).get("spoke"),
                        }
                        for sid in c.get("services") or []
                    ],
                    "hosts": c.get("hosts"),
                }
            )
        tickets = [
            {
                "id": p.id,
                "status": p.status,
                "headline": p.headline(),
                "proposed_by": p.proposed_by,
                "stage": p.stage,
                "review": (p.reviews[-1] if p.reviews else None),
            }
            for p in self.store.proposals(incident_id=inc.id)
        ]
        return {
            "incident": {
                "id": inc.id,
                "status": inc.status,
                "severity": inc.severity,
                "priority": inc.priority,
                "title": inc.title,
                "opened_at": _iso(inc.created),
                "assignee": inc.assignee,
                "counts": inc.counts(),
                "customers": customers,
                "services": inc.services,
                "hosts": inc.hosts,
                "problems": [
                    {k: p.get(k) for k in ("host", "name", "severity", "service", "pw_role", "check", "customer")}
                    for p in inc.problems[:40]
                ],
                "triage": inc.triage,
                "root_cause": {k: (inc.root_cause or {}).get(k) for k in ("title", "summary", "category")}
                if inc.root_cause
                else None,
                "escalations": inc.escalations[-3:],
                "tickets": tickets,
            },
            "customers": about,
            "links": {
                **s.links(),
                "incident": f"{s.public_url}/#{inc.id}",
                "zabbix_problems": f"{s.zabbix_public_url}/zabbix.php?action=problem.view",
                "zabbix_services": f"{s.zabbix_public_url}/zabbix.php?action=service.list",
            },
        }

    def _summary(self, inc: Incident, event: str, about: list[str], extra: str = "") -> str:
        def who(names: list[str]) -> str:
            return ", ".join(
                f"{n} ({inc.customers[n].get('tier') or '?'} {inc.customers[n].get('state')})" for n in names[:6]
            ) + (f" +{len(names) - 6}" if len(names) > 6 else "")

        counts = inc.counts()
        if event == "incident.opened":
            return f"{inc.id} opened: {inc.title}; {who(about)}"
        if event == "incident.worse":
            return f"{inc.id} worse: {who(about)}"
        if event == "incident.recovered":
            return f"{inc.id} recovered: Zabbix shows every customer OK ({counts['ok']}); verify and close it"
        if event == "incident.gold-down":
            return f"{inc.id}: gold customer down: {who(about)}"
        if event == "incident.zabbix-escalation":
            return f"{inc.id}: Zabbix escalation, still not OK: {who(about)}"
        if event == "incident.escalated":
            return f"{inc.id} escalated to you: {extra}"[:400]
        if event == "ticket.returned":
            return f"{inc.id}: {extra}"[:400]
        return f"{inc.id}: {event}"

    def _page_incident(self, req: PageRequest, extra: dict[str, Any] | None = None, note: str = "") -> Page | None:
        inc = self.incidents.get(req.incident_id) or self.store.incident(req.incident_id)
        if inc is None:
            return None
        body = self._incident_body(inc, req.customers)
        if extra:
            body.update(extra)
        key = req.event if req.event != "incident.escalated" else f"{req.event}:{req.role}"
        body["how_to_respond"] = HOW_TO[key].format(id=inc.id)
        p = self._page(req.role, req.event, self._summary(inc, req.event, req.customers, note), body, inc.id)
        inc.pages.append(p.id)
        self._save(inc)
        return p

    def _page(
        self,
        role: str,
        event: str,
        summary: str,
        body: dict[str, Any],
        incident_id: str | None = None,
        force: bool = False,
    ) -> Page:
        return self.paging.page(
            role,
            event,
            summary,
            body,
            incident_id=incident_id,
            muted=self.control.pages_muted,
            paused=self.control.paused,
            sink_url=self.control.sink_url,
            force=force,
        )

    def _paged(self, p: Page) -> None:
        """A page was recorded (queued, then sent / failed; or skipped): the room's phone and the activity log."""
        self.bus.publish("page", self.page_brief(p))
        m = self.roles[p.role]
        if p.status == "queued":
            return
        if p.status == "sent":
            if m.status != "working":
                m.status = "paged"
            m.paged_at = time.time()
            m.task = p.incident_id or m.task
            m.activity = f"paged: {p.summary}"[:160]
            self.publish_roles()
            text = f"phone rings: {p.event} {p.incident_id or ''} ({'test sink' if p.sink else f'HTTP {p.http}'})"
            self.emit(Activity(bot=m.name, kind="page", text=text, incident_id=p.incident_id, station="phone"))
        elif p.status == "failed":
            text = f"page failed: {p.event} {p.incident_id or ''} (HTTP {p.http or '-'}: {p.error or ''})"[:200]
            self.emit(Activity(bot=m.name, kind="error", text=text, incident_id=p.incident_id, ok=False))
        else:
            text = f"not paged: {p.event} {p.incident_id or ''} ({p.why})"
            self.emit(Activity(bot=m.name, kind="page", text=text, incident_id=p.incident_id, ok=False))
        if p.event == "tickets.filed" and p.status in ("failed", "skipped"):
            # nobody reviews these tickets: they go to the operator as they are
            why = p.why if p.status == "skipped" else f"HTTP {p.http or '-'}"
            ids = [t.get("id") for t in p.payload.get("tickets") or []]
            self._unreviewed([t for t in (self.store.proposal(i) for i in ids if i) if t], f"not paged: {why}")

    def _tickets_filed(self, ticket_ids: list[str]) -> None:
        """A batch of tickets to review closed: one page to the NOC Manager."""
        tickets = [p for p in (self.store.proposal(i) for i in dict.fromkeys(ticket_ids)) if p and p.status == "review"]
        if not tickets:
            return
        listed = [
            {
                **self.proposal_brief(p),
                "incident_title": (self.incidents.get(p.incident_id or "") or Incident(id="?")).title,
            }
            for p in tickets
        ]
        first = ", ".join(f"{p.id} ({p.incident_id}) {p.headline()}" for p in tickets[:4])
        summary = f"{len(tickets)} change ticket{'s wait' if len(tickets) != 1 else ' waits'} for your review: {first}"
        body = {"tickets": listed, "how_to_respond": HOW_TO["tickets.filed"], "links": self.settings.links()}
        incident_ids = sorted({p.incident_id for p in tickets if p.incident_id})
        p = self._page(NOCMGR, "tickets.filed", summary, body, incident_ids[0] if len(incident_ids) == 1 else None)
        for iid in incident_ids:
            inc = self.incidents.get(iid)
            if inc is not None:
                inc.pages.append(p.id)
                self._save(inc)

    async def page_test(self, role: str) -> Page:
        """One test page to the role's routine now (whatever mute, maintenance and the cap say)."""
        if role not in ROLES:
            raise KeyError(f"no role {role!r} (roles: {', '.join(ROLES)})")
        body = {"how_to_respond": HOW_TO["test"], "links": self.settings.links()}
        p = self._page(role, "test", f"test page for the {ROLES[role].title}", body, force=True)
        if p.status == "queued":
            for _ in range(200):
                await asyncio.sleep(0.05)
                fresh = self.store.page(p.id)
                if fresh is not None and fresh.status != "queued":
                    return fresh
        return self.store.page(p.id) or p

    def pages_state(self, role: str | None = None, limit: int = 30) -> dict[str, Any]:
        s = self.settings
        return {
            "roles": {
                k: {
                    "bot": spec.bot,
                    "title": spec.title,
                    "routine": s.routine(k).configured,
                    "sent_last_hour": len(
                        [
                            p
                            for p in self.store.pages(role=k, since=time.time() - 3600, limit=1000)
                            if p.status == "sent" and not p.sink
                        ]
                    ),
                }
                for k, spec in ROLES.items()
            },
            "max_per_hour": s.page_max_per_hour,
            "muted": self.control.pages_muted,
            "paused": self.control.paused,
            "sink": bool(self.control.sink_url),
            "history": [self.page_brief(p) for p in self.store.pages(role=role, limit=limit)],
        }

    # ------------------------------------------------------------------ the roles' work
    def _bot(self, role: str) -> str:
        return ROLES[role].bot

    def _working_on(self, role: str, incident_id: str) -> None:
        m = self.roles[role]
        if m.task != incident_id:
            m.task = incident_id
            self.publish_roles()

    def noc_status(self, role: str) -> dict[str, Any]:
        active = self.store.incidents(limit=50, active_only=True)
        review = self.store.proposals(status="review", limit=50)
        pending = self.store.proposals(status="pending", limit=50)
        questions = [m for m in self.store.chat(limit=50) if m.role == "user" and m.status == "open"]
        return {
            "summary": f"{len(active)} active incident(s), {len(review)} change ticket(s) in the NOC Manager's review, "
            f"{len(pending)} proposed to the operator, {len(questions)} open question(s)",
            "you": {"role": role, "bot": self._bot(role), "title": ROLES[role].title},
            "lab_ok": self.snapshot.ok,
            "lab_error": self.snapshot.error,
            "open_problems": len(self.snapshot.problems),
            "maintenance_mode": self.control.paused,
            "pages_muted": self.control.pages_muted,
            "autonomy": self.autonomy,
            "active_incidents": [self.incident_brief(i) for i in active],
            "tickets_in_review": [self.proposal_brief(p) for p in review],
            "tickets_proposed": [self.proposal_brief(p) for p in pending],
            "open_questions": [{"question_id": q.id, "text": q.text, "asked": _iso(q.ts)} for q in questions],
            "noc_room": self.settings.public_url,
            "links": self.settings.links(),
        }

    def add_note(self, role: str, incident_id: str, text: str) -> dict[str, Any]:
        inc = self._incident(incident_id)
        bot = self._bot(role)
        inc.notes.append(f"{bot}: {text[:2000]}")
        self._save(inc)
        self.emit(Activity(bot=bot, kind="say", text=text.splitlines()[0][:200], incident_id=inc.id, station="board"))
        return {"ok": True, "summary": f"note added to {inc.id}"}

    def take_incident(self, role: str, incident_id: str) -> dict[str, Any]:
        inc = self._open(incident_id)
        bot = self._bot(role)
        before = inc.assignee
        inc.assignee = bot
        self._restatus(inc)
        self._save(inc)
        self._working_on(role, inc.id)
        self.emit(
            Activity(bot=bot, kind="take", text=f"took {inc.id}: {inc.title}", incident_id=inc.id, station="board")
        )
        note = f" (from {before})" if before and before != bot else ""
        return {"ok": True, "summary": f"{inc.id} is yours{note}: {inc.title}", "incident": self.incident_brief(inc)}

    async def acknowledge(
        self, role: str, incident_id: str, message: str, eventids: list[str] | None = None
    ) -> dict[str, Any]:
        inc = self._open(incident_id)
        bot = self._bot(role)
        ids = eventids or [
            c["event_id"] for c in inc.customers.values() if c.get("state") != "ok" and c.get("event_id")
        ]
        if not ids:
            raise ValueError(f"{inc.id} has no customer that is not OK: nothing to acknowledge")
        text = f"{bot} ({inc.id}): {message}"[:2048]
        try:
            result = await self.lab.acknowledge(ids, text, role, f"{self.settings.actor}/{bot}")
        except LabApiError as exc:
            raise ValueError(f"lab-api refused the acknowledgment: {exc} {exc.detail}") from exc
        record = {
            "by": bot,
            "ts": now(),
            "message": message[:500],
            "eventids": ids,
            "acknowledged": result.get("acknowledged") or [],
            "commented": result.get("commented") or [],
            "zabbix_user": (result.get("zabbix_user") or {}).get("username"),
        }
        inc.acks.append(record)
        self._restatus(inc)
        self._save(inc)
        n = len(record["acknowledged"]) + len(record["commented"])
        self.emit(
            Activity(
                bot=bot,
                kind="ack",
                text=f"acknowledged {n} Zabbix problem(s) of {inc.id} as {record['zabbix_user']}",
                incident_id=inc.id,
                station="console",
            )
        )
        return {
            "ok": True,
            "summary": f"{n} trigger problem(s) acknowledged in Zabbix as {record['zabbix_user']}",
            **{k: result.get(k) for k in ("acknowledged", "commented", "skipped", "problems", "zabbix_user")},
        }

    def submit_triage(self, role: str, incident_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        inc = self._open(incident_id)
        bot = self._bot(role)
        inc.triage = {**{k: v for k, v in fields.items() if v}, "by": bot, "ts": now()}
        inc.assignee = inc.assignee or bot
        self._restatus(inc)
        self._save(inc)
        self.emit(
            Activity(
                bot=bot,
                kind="triage",
                text=f"{inc.id} triage: {fields['summary']}"[:200],
                incident_id=inc.id,
                station="desk",
            )
        )
        return {"ok": True, "summary": f"triage filed on {inc.id}"}

    def escalate(self, role: str, incident_id: str, to: str, note: str) -> dict[str, Any]:
        inc = self._open(incident_id)
        allowed = ESCALATION_TARGETS[role]
        if to not in allowed:
            names = " or ".join(allowed) or "nobody"
            raise ValueError(f"the {ROLES[role].title} escalates to {names}, not {to!r}")
        bot = self._bot(role)
        inc.escalations.append({"from": bot, "to": to, "note": note[:2000], "ts": now()})
        self._restatus(inc)
        self._save(inc)
        self.emit(
            Activity(
                bot=bot,
                kind="escalate",
                text=f"escalated {inc.id} to the {ROLES[to].title}: {note}"[:200],
                incident_id=inc.id,
                station="board",
                target=ROLES[to].bot,
            )
        )
        extra = {"escalation": {"from": bot, "to": to, "note": note}, "triage": inc.triage}
        p = self._page_incident(
            PageRequest(to, "incident.escalated", inc.id, inc.bad_customers()), extra, note=f"{bot}: {note}"
        )
        status = p.status if p else "?"
        return {"ok": True, "summary": f"{inc.id} escalated to {to} (page {p.id if p else '-'} {status})"}

    async def close_incident(self, role: str, incident_id: str, summary: str) -> dict[str, Any]:
        inc = self._open(incident_id)
        names = sorted(inc.customers)
        try:
            nodes = await self.lab.zabbix_read(
                "service.get",
                {
                    "output": ["name", "status"],
                    "filter": {"name": names},
                    "tags": [{"tag": "bsm_level", "value": "customer", "operator": 1}],
                },
            )
        except LabApiError as exc:
            raise ValueError(f"cannot read Zabbix to verify: {exc}") from exc
        status = {n["name"]: int(n["status"]) for n in nodes or []}
        bad = [
            f"{n} {'missing in Zabbix' if n not in status else 'status ' + str(status[n])}"
            for n in names
            if status.get(n, 0) != -1
        ]
        if bad:
            raise ValueError(f"Zabbix does not show every customer OK yet: {', '.join(bad)} (-1 = OK)")
        bot = self._bot(role)
        inc.closing = {"by": bot, "summary": summary[:2000], "ts": now()}
        inc.status, inc.closed = "closed", now()
        for p in self.store.proposals(incident_id=inc.id):
            if p.status not in ("review", "pending"):
                continue
            p.status, p.updated, p.comment = "obsolete", now(), f"{inc.id} closed before a decision"
            self.store.save_proposal(p)
            self.bus.publish("proposal", p.to_dict())
        self._save(inc)
        self.emit(
            Activity(
                bot=bot, kind="close", text=f"closed {inc.id}: {summary}"[:200], incident_id=inc.id, station="board"
            )
        )
        took = inc.closed - inc.created
        return {"ok": True, "summary": f"{inc.id} closed after {took:.0f} s"}

    def submit_root_cause(self, role: str, incident_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        inc = self._open(incident_id)
        bot = self._bot(role)
        tickets = [p.id for p in self.store.proposals(incident_id=inc.id)]
        inc.root_cause = {
            **{k: v for k, v in fields.items() if v is not None},
            "by": bot,
            "ts": now(),
            "tickets": tickets,
        }
        inc.title = str(fields["title"])[:120]
        self._restatus(inc)
        self._save(inc)
        self.emit(
            Activity(
                bot=bot,
                kind="report",
                text=f"{inc.id} root cause: {fields['title']} [{fields.get('category')}]"[:200],
                incident_id=inc.id,
                station="desk",
            )
        )
        return {"ok": True, "summary": f"root cause filed on {inc.id}; its title is now {inc.title!r}"}

    def set_priority(self, role: str, incident_id: str, priority: str, reason: str) -> dict[str, Any]:
        inc = self._open(incident_id)
        bot = self._bot(role)
        inc.priority, inc.priority_reason = priority, reason[:500]
        self._save(inc)
        self.emit(
            Activity(
                bot=bot,
                kind="priority",
                text=f"{inc.id} is {priority}: {reason}"[:200],
                incident_id=inc.id,
                station="board",
            )
        )
        return {"ok": True, "summary": f"{inc.id} priority {priority}"}

    def review_ticket(self, role: str, proposal_id: str, decision: str, reason: str) -> dict[str, Any]:
        """The NOC Manager's review: ``propose`` sends the ticket to the operator, ``return`` sends it back to the
        engineer who filed it (paged). A ticket the operator already has can still be returned until a human decided
        it; a review of a decided ticket is kept, marked late."""
        p = self.store.proposal(proposal_id)
        if p is None:
            raise KeyError(f"no change ticket {proposal_id}")
        bot = self._bot(role)
        review = {"by": bot, "decision": decision, "reason": reason[:1000], "ts": now()}
        was = p.status
        if decision == "propose" and was == "review":
            p.status = "pending"
        elif decision == "return" and was in ("review", "pending"):
            p.status = "returned"
        elif was != "pending":
            review["late"] = was  # a human (or the timeout) got there before the review
        p.reviews.append(review)
        p.updated = now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        if p.status == "returned" and was != "returned":
            filer = p.role or ROLE_BY_BOT[p.proposed_by].key
            text = f"returns {p.id} to {p.proposed_by}: {reason}"
            self.emit(
                Activity(
                    bot=bot,
                    kind="return",
                    text=text[:200],
                    incident_id=p.incident_id,
                    station=f"desk:{p.proposed_by}",
                    target=p.proposed_by,
                )
            )
            self._restatus_of(p)
            page = None
            if p.incident_id:
                inc = self._incident(p.incident_id)
                extra = {"ticket": self.proposal_brief(p), "review": review}
                req = PageRequest(filer, "ticket.returned", inc.id, inc.bad_customers())
                page = self._page_incident(req, extra, note=f"{p.id} returned by {bot}: {reason}")
            paged = f"page {page.id} {page.status}" if page else "nobody paged"
            summary = f"{p.id} returned to {p.proposed_by} ({paged}): it can no longer be approved"
            return {"ok": True, "summary": summary}
        late = review.get("late")
        what = f"proposes {p.id} to the operator" if decision == "propose" else f"returns {p.id}"
        text = f"late, {p.id} already {late}: {what}" if late else what
        self.emit(
            Activity(
                bot=bot,
                kind="review",
                text=f"{text}: {reason}"[:200],
                incident_id=p.incident_id,
                station="operator",
                target=p.host,
            )
        )
        if was == "review":
            self._restatus_of(p)
            return {"ok": True, "summary": f"{p.id} proposed to the operator: it waits for a human's decision"}
        state = "waits for a human's decision" if p.status == "pending" else f"is already {p.status}"
        return {"ok": True, "summary": f"review of {p.id} recorded ({decision}); the ticket {state}"}

    def write_customer_update(self, role: str, incident_id: str, text: str, customer: str | None) -> dict[str, Any]:
        inc = self._incident(incident_id)
        if customer and customer not in inc.customers:
            raise ValueError(f"{customer} is not a customer of {inc.id} ({', '.join(inc.customers)})")
        bot = self._bot(role)
        inc.customer_updates.append({"by": bot, "customer": customer, "text": text[:4000], "ts": now()})
        self._save(inc)
        self.emit(
            Activity(
                bot=bot,
                kind="update",
                text=f"customer update {inc.id}{' for ' + customer if customer else ''}: {text}"[:200],
                incident_id=inc.id,
                station="desk",
            )
        )
        return {"ok": True, "summary": f"customer update on {inc.id} recorded"}

    def write_shift_report(self, role: str, text: str, title: str | None) -> dict[str, Any]:
        bot = self._bot(role)
        reports = self.store.get_meta("shift_reports", [])
        entry = {
            "by": bot,
            "title": title or time.strftime("Shift report %Y-%m-%d %H:%M UTC", time.gmtime()),
            "text": text[:20_000],
            "ts": now(),
        }
        reports = [*reports, entry][-50:]
        self.store.set_meta("shift_reports", reports)
        self.bus.publish("shift_report", entry)
        self.emit(Activity(bot=bot, kind="report", text=f"shift report: {entry['title']}", station="desk"))
        return {"ok": True, "summary": f"shift report filed: {entry['title']}"}

    # ------------------------------------------------------------------ Ask the NOC
    async def ask(self, message: str, chat_id: str | None = None, by: str = "operator") -> ChatMessage:
        chat_id = chat_id or self.store.next_id("CHAT")
        q = ChatMessage(
            id=self.store.next_id("MSG", 6), chat_id=chat_id, role="user", text=message[:4000], bot=by, status="open"
        )
        self.store.save_chat(q)
        self.bus.publish("chat", q.to_dict())
        body = {
            "question": {
                "question_id": q.id,
                "chat_id": chat_id,
                "text": q.text,
                "asked_by": by,
                "asked_at": _iso(q.ts),
            },
            "history": [
                {"role": m.role, "text": m.text[:800], "bot": m.bot}
                for m in self.store.chat(chat_id, limit=12)
                if m.id != q.id
            ],
            "how_to_respond": HOW_TO["question"],
            "links": self.settings.links(),
        }
        p = self._page(NOCMGR, "question", f"Ask the NOC: {q.text[:300]}", body)
        q.page_id = p.id
        self.store.save_chat(q)
        self.emit(
            Activity(bot="operator", kind="question", text=f"asked the NOC Manager: {q.text[:160]}", chat_id=chat_id)
        )
        if p.status == "skipped":
            self._system(
                chat_id,
                f"The NOC Manager was not paged ({p.why}); the question stays open for "
                f"{self.settings.ask_timeout_s / 60:g} min.",
            )
        return q

    def _system(self, chat_id: str, text: str) -> ChatMessage:
        m = ChatMessage(id=self.store.next_id("MSG", 6), chat_id=chat_id, role="system", text=text)
        self.store.save_chat(m)
        self.bus.publish("chat", m.to_dict())
        return m

    def answer_question(self, role: str, question_id: str, answer: str) -> dict[str, Any]:
        q = self.store.chat_message(question_id)
        if q is None or q.role != "user":
            raise KeyError(f"no question {question_id}; get_noc_status lists the open ones")
        if q.status == "answered":
            raise ValueError(f"{question_id} is answered already")
        bot = self._bot(role)
        a = ChatMessage(
            id=self.store.next_id("MSG", 6), chat_id=q.chat_id, role="bot", text=answer[:8000], bot=bot, answer_to=q.id
        )
        late = q.status == "timed-out"
        q.status = "answered"
        self.store.save_chat(q)
        self.store.save_chat(a)
        self.bus.publish("chat", q.to_dict())
        self.bus.publish("chat", a.to_dict())
        self.emit(
            Activity(bot=bot, kind="answer", text=answer.splitlines()[0][:200], chat_id=q.chat_id, station="operator")
        )
        return {
            "ok": True,
            "summary": f"answer to {q.id} shown in the panel" + (" (after the timeout)" if late else ""),
        }

    def _question_timeouts(self, t: float) -> None:
        limit = self.settings.ask_timeout_s
        for q in self.store.chat(limit=100):
            if q.role == "user" and q.status == "open" and t - q.ts > limit:
                q.status = "timed-out"
                self.store.save_chat(q)
                self.bus.publish("chat", q.to_dict())
                self._system(
                    q.chat_id,
                    f"No answer from the NOC Manager within {limit / 60:g} min. A late answer "
                    "still shows here; or ask again.",
                )

    # ------------------------------------------------------------------ change tickets
    async def propose(
        self, incident_id: str | None, role: str, *, kind: str, host: str, reason: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        # One proposal at a time: the duplicate check and the ticket must not straddle another role's dry run.
        async with self._propose_lock:
            return await self._propose_locked(incident_id, role, kind=kind, host=host, reason=reason, params=params)

    async def _propose_locked(
        self, incident_id: str | None, role: str, *, kind: str, host: str, reason: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        bot = self._bot(role)
        existing = [
            p
            for p in self.store.proposals(incident_id=incident_id)
            if p.status in ("review", "pending") and p.kind == kind and p.host == host and p.params == params
        ]
        actor = f"{self.settings.actor}/{bot}"
        comment = f"{incident_id}: {reason}"[:400]
        plan: dict[str, Any] | None = None
        if kind == "apply-intent":
            plan = await self.lab.plan_apply_intent(host, actor, comment)
        elif kind == "config-patch":
            plan = await self.lab.plan_patch(host, params["patch"], actor, comment)
        if plan is not None:
            if plan.get("no_change"):
                return {"no_change": True, "note": f"{host} already runs that config: nothing to change"}
            validation = plan.get("validation") or {}
            if not validation.get("valid", True):
                return {"invalid": True, "errors": validation.get("errors"), "note": "the device would refuse it"}
        again = False
        if existing:
            p = existing[0]
            before = (p.plan or {}).get("plan_id")
            p.plan = slim_plan(plan)
            if p.proposed_by == bot:  # someone else's ticket for the same change keeps its author's reason
                p.reason = reason
            p.updated = now()
            # the operator has it, but the dry run came out different: the new plan needs the NOC Manager's review
            again = p.status == "pending" and (p.plan or {}).get("plan_id") != before
            if again:
                self._to_review(p)
            new = False
        else:
            p = Proposal(
                id=self.store.next_id("CHG"),
                incident_id=incident_id,
                kind=kind,
                host=host,
                reason=reason,
                proposed_by=bot,
                role=role,
                params=params,
                plan=slim_plan(plan),
                review_since=now(),
            )
            new = True
        self.store.save_proposal(p)
        inc = self.incidents.get(incident_id or "")
        if inc is not None:
            if p.id not in inc.proposals:
                inc.proposals.append(p.id)
            self._restatus(inc)
            self._save(inc)
        self.bus.publish("proposal", p.to_dict())
        where = "to the NOC Manager for review" if p.status == "review" else "already with the operator"
        self.emit(
            Activity(
                bot=bot,
                kind="propose",
                text=f"{p.id} {where}: {p.headline()}",
                incident_id=incident_id,
                station="desk:NOCMGR",
                target=host,
            )
        )
        if new or again:
            self.ticket_batcher.add(TICKETS, p.id)
        diff = (p.plan or {}).get("diff") or []
        return {
            "proposal_id": p.id,
            "status": p.status,
            "headline": p.headline(),
            "proposed_by": p.proposed_by,
            "diff": [d.get("message") for d in diff],
            "affected_services": (p.plan or {}).get("affected_services"),
            "drift_after_predicted": (p.plan or {}).get("drift_after_predicted"),
            "note": "waits for the NOC Manager's review: it proposes the ticket to the operator, or returns it to "
            "you with the reason (you are paged)"
            if p.status == "review"
            else "the NOC Manager proposed this change already: it waits for a human",
            "summary": f"{p.id} {where}: {p.headline()}",
        }

    def _to_review(self, p: Proposal) -> None:
        """Back to the NOC Manager's review (a new plan): nobody can approve it until it is proposed again."""
        p.status, p.review_since, p.decided_by, p.decided = "review", now(), None, None

    def _unreviewed(self, tickets: list[Proposal], why: str) -> None:
        """Tickets the NOC Manager will not review go to the operator as they are, marked so."""
        for p in tickets:
            if p.status != "review":
                continue
            p.status, p.updated = "pending", now()
            p.reviews.append({"by": self.settings.actor, "decision": "skip", "reason": why, "ts": now()})
            self.store.save_proposal(p)
            self.bus.publish("proposal", p.to_dict())
            self.emit(
                Activity(
                    bot=self.settings.actor,
                    kind="review",
                    text=f"{p.id} goes to the operator without the NOC Manager's review ({why})"[:200],
                    incident_id=p.incident_id,
                    station="operator",
                    ok=False,
                )
            )
            self._restatus_of(p)

    def _review_timeouts(self, t: float) -> None:
        limit = self.settings.review_timeout_s
        waiting = self.store.proposals(status="review", limit=100)
        late = [p for p in waiting if t - (p.review_since or p.created) > limit]
        if late:
            self._unreviewed(late, f"no review within {limit / 60:g} min")

    async def approve(self, proposal_id: str, by: str, comment: str | None = None) -> Proposal:
        p = self.store.proposal(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status == "review":
            raise ValueError(f"{proposal_id} is in the NOC Manager's review: it can be approved once it is proposed")
        if p.status != "pending":
            raise ValueError(f"{proposal_id} is {p.status}, not pending")
        p.status, p.decided_by, p.decided, p.comment, p.updated = "approved", by, now(), comment, now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        self.emit(
            Activity(
                bot="operator",
                kind="approve",
                text=f"{p.id} approved by {by}: {p.headline()}",
                incident_id=p.incident_id,
                station="operator",
                target=p.host,
            )
        )
        self._restatus_of(p)
        self._exec_queue.put_nowait(p.id)
        return p

    async def approve_incident(
        self, incident_id: str, by: str, comment: str | None = None, expect: dict[str, str | None] | None = None
    ) -> list[Proposal]:
        """Every ticket of the incident that waits for a human, make before break. ``expect`` (ticket id -> plan id,
        as the operator saw them): approve only if exactly those tickets with those plans wait, else nothing."""
        self._incident(incident_id)
        pending = sorted(self.store.proposals(incident_id=incident_id, status="pending"), key=change_order)
        if expect is not None:
            waiting = {p.id: (p.plan or {}).get("plan_id") for p in pending}
            if waiting != expect:
                now_waiting = ", ".join(waiting) or "none"
                raise ValueError(f"the tickets of {incident_id} changed since you looked (now: {now_waiting})")
        return [await self.approve(p.id, by, comment) for p in pending]

    async def reject(self, proposal_id: str, by: str, comment: str | None = None) -> Proposal:
        p = self.store.proposal(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != "pending":
            raise ValueError(f"{proposal_id} is {p.status}, not pending")
        p.status, p.decided_by, p.decided, p.comment, p.updated = "rejected", by, now(), comment, now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        self.emit(
            Activity(
                bot="operator",
                kind="reject",
                text=f"{p.id} rejected by {by}{': ' + comment if comment else ''}",
                incident_id=p.incident_id,
                station="operator",
            )
        )
        self._restatus_of(p)
        return p

    def _restatus_of(self, p: Proposal) -> None:
        inc = self.incidents.get(p.incident_id or "")
        if inc is not None:
            self._restatus(inc)
            self._save(inc)

    # ------------------------------------------------------------------ the executor
    async def _executor_loop(self) -> None:
        while True:
            pid = await self._exec_queue.get()
            try:
                await self._execute(pid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("executing %s failed", pid)

    async def _execute(self, proposal_id: str) -> None:
        p = self.store.proposal(proposal_id)
        if p is None or p.status != "approved":
            return
        actor = f"{self.settings.actor}/{p.proposed_by} approved-by:{p.decided_by}"
        comment = f"{p.incident_id} {p.id}: {p.reason}"[:400]
        if p.kind == "field-repair":
            # off-screen (no robot): dispatched -> on site -> repaired, on the ticket and the board. The executor
            # waits for it: tickets approved together keep their make-before-break order.
            self._set_stage(p, "dispatched", f"field technician dispatched to {p.host}'s site")
            await self._field_repair(p.id, actor)
            return
        p.status, p.updated = "applying", now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        self._restatus_of(p)
        try:
            plan_id = (p.plan or {}).get("plan_id") or ""
            self.emit(
                Activity(
                    bot="executor",
                    kind="apply",
                    text=f"committing {p.id} on {p.host} (approved by {p.decided_by})",
                    incident_id=p.incident_id,
                    station="rack",
                    target=p.host,
                )
            )
            if p.kind == "apply-intent":
                result = await self.lab.apply_intent(p.host, plan_id, actor, comment)
            else:
                result = await self.lab.apply_patch(p.host, p.params.get("patch") or [], plan_id, actor, comment)
            verdict = (result.get("verification") or {}).get("verdict")
            self.emit(
                Activity(
                    bot="executor",
                    kind="verify",
                    text=f"{p.id} on {p.host}: verdict {verdict} ({result.get('elapsed_s')} s; simd + Zabbix)",
                    incident_id=p.incident_id,
                    station="screen",
                    target=p.host,
                    ok=verdict in ("ok", "degraded"),
                )
            )
            p.status, p.result, p.updated = "applied", slim_result(result), now()
        except LabApiError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            new_plan = detail.get("plan") if exc.status == 409 else None
            if isinstance(new_plan, dict) and (new_plan.get("no_change") or new_plan.get("diff") == []):
                p.status, p.updated = "obsolete", now()
                p.comment = "nothing left to change: the device already runs this config"
            elif isinstance(new_plan, dict):
                p.plan = slim_plan(new_plan)
                self._to_review(p)
                p.updated = now()
                p.comment = "the device or NetBox changed since the dry run: the new plan goes back to review"
                self.ticket_batcher.add(TICKETS, p.id)
                self.emit(
                    Activity(
                        bot="executor",
                        kind="error",
                        text=f"{p.id}: plan changed since approval; back to the NOC Manager's review",
                        incident_id=p.incident_id,
                        ok=False,
                    )
                )
            else:
                p.status, p.error, p.updated = "failed", f"{exc}: {str(exc.detail)[:300]}", now()
                self.emit(
                    Activity(
                        bot="executor", kind="error", text=f"{p.id} failed: {exc}", incident_id=p.incident_id, ok=False
                    )
                )
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        self._restatus_of(p)

    def _set_stage(self, p: Proposal, stage: str, text: str) -> None:
        p.stage = stage
        p.progress.append({"stage": stage, "ts": now(), "text": text})
        if stage == "dispatched":
            p.status = "applying"
        p.updated = now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        self.emit(
            Activity(
                bot="field",
                kind="field",
                text=f"{p.id}: {text}",
                incident_id=p.incident_id,
                station="board",
                target=p.host,
            )
        )
        self._restatus_of(p)

    async def _field_repair(self, proposal_id: str, actor: str) -> None:
        p = self.store.proposal(proposal_id)
        if p is None:
            return
        try:
            await self.sleep(self.settings.field_delay_s)
            self._set_stage(p, "on-site", f"field technician on site at {p.host}")
            result = await self.lab.restore_fault(
                p.params.get("kind", "device"),
                p.host,
                actor,
                peer=p.params.get("peer"),
                interface=p.params.get("interface"),
            )
            p.result = result
            p.status = "applied"
            self._set_stage(p, "repaired", f"repaired: {result.get('done', p.headline())}")
        except LabApiError as exc:
            p.status, p.error = "failed", f"{exc}: {str(exc.detail)[:300]}"
            p.updated = now()
            self.store.save_proposal(p)
            self.bus.publish("proposal", p.to_dict())
            self.emit(
                Activity(bot="field", kind="error", text=f"{p.id} failed: {exc}", incident_id=p.incident_id, ok=False)
            )
            self._restatus_of(p)

    # ------------------------------------------------------------------ control
    async def set_control(
        self,
        by: str,
        paused: bool | None = None,
        autonomy: str | None = None,
        reason: str | None = None,
        pages_muted: bool | None = None,
        sink_url: str | None = None,
    ) -> Control:
        if autonomy is not None and autonomy not in (*AUTONOMIES, "default"):
            raise ValueError(f"autonomy must be one of {', '.join(AUTONOMIES)} or default")
        if sink_url and not sink_allowed(sink_url):
            raise ValueError("sink_url must be an http URL on labnet (172.30.10.0/24) or loopback")
        c = self.control
        changes = []
        if paused is not None:
            c.paused = paused
            c.reason = reason if paused else None
            changes.append(
                "maintenance mode on" + (f" ({reason})" if reason else "") if paused else "maintenance mode off"
            )
        if autonomy is not None:
            c.autonomy = None if autonomy == "default" else autonomy
            changes.append(f"autonomy {autonomy}")
        if pages_muted is not None:
            c.pages_muted = pages_muted
            changes.append("pages muted" if pages_muted else "pages on")
        if sink_url is not None:
            c.sink_url = sink_url or None
            changes.append("pages go to the test sink" if sink_url else "pages go to the routines")
        c.by, c.since = by, now()
        self.store.set_meta("control", c.to_dict())
        self.bus.publish("control", {**self.public_control(), "effective": self.effective()})
        self.emit(Activity(bot="operator", kind="control", text=f"{by}: " + ", ".join(changes), station="operator"))
        return c

    # ------------------------------------------------------------------ plumbing
    def emit(self, a: Activity) -> None:
        self.store.add_activity(a)
        self.bus.publish("activity", a.to_dict())
        spec = ROLE_BY_BOT.get(a.bot)
        if spec is not None and a.kind not in ("result", "page", "error"):
            m = self.roles[spec.key]
            m.activity = a.text[:160]
            if a.incident_id:
                m.task = a.incident_id
            self.publish_roles()

    def roles_now(self) -> list[dict[str, Any]]:
        """The robots, each with whether its role has a routine to page (``.env`` is read live)."""
        s = self.settings
        for key, m in self.roles.items():
            m.routine = s.routine(key).configured
        return [m.to_dict() for m in self.roles.values()]

    def publish_roles(self) -> None:
        self.bus.publish("roles", self.roles_now())
