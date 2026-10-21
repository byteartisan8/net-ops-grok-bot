"""The NOC engine: the watcher loop, the crew, investigations, proposals, approvals and the change executor.

* **Watcher (POLL)** polls lab-api every ``BOT_POLL_S``: Zabbix problems (incidents), services and devices (the
  wall screen, the rack LEDs). Polling is truth; trap hints never open an incident.
* **Responders (PING, TRACE, DIG)** take open incidents by severity; each investigation runs a brain (Grok, or the
  rules triage) with the tools of :mod:`netsim_bot.tools`.
* **Proposals** are lab-api dry runs; the engine keeps their ``plan_id``. A human approves (NOC page, ``botctl``)
  or, with ``BOT_AUTONOMY=auto``, the policy does. The executor runs approved changes one at a time, in
  make-before-break order when several are approved together, and lab-api verifies each through simd and Zabbix.
  A plan that changed since the dry run (HTTP 409) goes back to "pending" with the new plan: what is applied is
  always exactly what a human saw.
* **Field technician (PATCH)** carries out approved field repairs (lab-api fault restore / power on) after
  ``BOT_FIELD_DELAY_S`` on site.
* Everything is an event on the bus (SSE for the NOC page) and a row in SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from netsim_bot import __version__, prompts
from netsim_bot.agent import AgentGateway
from netsim_bot.brains import GrokBrain, Job, RulesBrain
from netsim_bot.bus import EventBus
from netsim_bot.grok import GrokClient, GrokError, Usage
from netsim_bot.incidents import IncidentTracker, auto_title
from netsim_bot.labapi import LabApi, LabApiError
from netsim_bot.models import (
    SEVERITY_RANK,
    Activity,
    ChatMessage,
    CrewMember,
    Incident,
    Proposal,
    now,
)
from netsim_bot.pager import Pager
from netsim_bot.pricing import cost_usd, price_for
from netsim_bot.settings import AUTONOMIES, BRAINS, BotSettings, load_settings
from netsim_bot.store import Store
from netsim_bot.tools import ToolContext, compact_service, toolset

log = logging.getLogger("netsim_bot")

CREW_SPEC = [
    ("POLL", "watcher", "Watcher: polls Zabbix, opens incidents", "#f5b041"),
    ("PING", "responder", "Responder", "#4fc3f7"),
    ("TRACE", "responder", "Responder", "#f06292"),
    ("DIG", "responder", "Responder", "#9ccc65"),
    ("PATCH", "field", "Field technician", "#ff8a3d"),
]
VISITOR = ("visitor", "Grok Bot app: an outside agent (MCP)", "#e6e6f0")
MONITOR_TIMEOUT_S = 120.0
MAX_INVESTIGATIONS = 3


@dataclass
class Control:
    paused: bool = False
    reason: str | None = None
    brain: str | None = None
    autonomy: str | None = None
    pager_muted: bool = False
    by: str | None = None
    since: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


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


class Engine:
    def __init__(
        self,
        settings_loader: Callable[[], BotSettings] = load_settings,
        store: Store | None = None,
        bus: EventBus | None = None,
        lab: LabApi | None = None,
        grok_factory: Callable[[BotSettings], GrokClient] | None = None,
    ) -> None:
        self._load = settings_loader
        self._settings = settings_loader()
        self._settings_at = time.monotonic()
        s = self._settings
        self.store = store or Store(s.data_dir / "bot.db")
        self.bus = bus or EventBus()
        self.lab = lab or LabApi(s.lab_api_url, token=lambda: self.settings.lab_api_token, actor=s.actor)
        self._grok_factory = grok_factory or (
            lambda st: GrokClient(st.xai_api_key or "", st.xai_base_url, st.model, st.reasoning_effort)
        )
        self._grok: GrokClient | None = None
        self._closing: set[asyncio.Future[Any]] = set()
        self._grok_key: tuple[Any, ...] | None = None
        self._grok_error: str | None = None
        self._grok_error_key: tuple[Any, ...] | None = None
        self.control = Control(**self.store.get_meta("control", {}))
        self.usage: dict[str, Any] = self.store.get_meta("usage", {})
        self._roll_usage_day()
        n = s.responders
        self.crew: dict[str, CrewMember] = {}
        responders = 0
        for name, role, title, color in CREW_SPEC:
            if role == "responder":
                responders += 1
                if responders > n:
                    continue
            self.crew[name] = CrewMember(name=name, role=role, title=title, color=color)
        self.crew["POLL"].status = "working"
        self.agent_name = s.agent_name
        role, title, color = VISITOR
        visitor = CrewMember(name=self.agent_name, role=role, title=title, color=color, status="away")
        self.crew[self.agent_name] = visitor
        self.agent = AgentGateway(self)
        self.pager = Pager(lambda: self.settings, self._paged, self.store.get_meta("pager_log", []))
        self.tracker = IncidentTracker(
            new_id=lambda: self.store.next_id("INC"),
            settle_s=s.settle_s,
            max_settle_s=s.max_settle_s,
            resolve_hold_s=s.resolve_hold_s,
        )
        self.snapshot = LabSnapshot()
        self.snapshot_raw_problems: list[dict[str, Any]] = []
        self._lab_hash = ""
        self._lab_published = 0.0
        self._topology_at = 0.0
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._loops: list[asyncio.Task[Any]] = []
        self._exec_queue: asyncio.Queue[str] = asyncio.Queue()
        self._propose_lock = asyncio.Lock()
        self._chat_queue: list[tuple[str, str]] = []
        self._chat_prev: dict[str, str | None] = self.store.get_meta("chat_prev", {})
        self._investigation_seen: dict[str, set[str]] = {}
        self._recover()

    # ------------------------------------------------------------------ settings, state
    @property
    def settings(self) -> BotSettings:
        if time.monotonic() - self._settings_at > 2.0:
            try:
                self._settings = self._load()
            except Exception as exc:  # keep the last good settings
                log.warning("settings reload failed: %s", exc)
            self._settings_at = time.monotonic()
        return self._settings

    @property
    def autonomy(self) -> str:
        return self.control.autonomy or self.settings.autonomy

    def effective(self) -> dict[str, Any]:
        s = self.settings
        want = s.effective_brain(self.control.brain)
        why = None
        brain = want
        if want == "grok":
            if not s.xai_api_key:
                brain, why = "rules", "no XAI_API_KEY in .env"
            elif s.daily_budget_usd > 0 and self.usage.get("usd_day", 0.0) >= s.daily_budget_usd:
                brain, why = "rules", f"daily Grok budget (${s.daily_budget_usd:g}) reached"
            elif self._grok_error and self._grok_error_key == self._grok_config(s):
                brain, why = "rules", f"Grok unavailable: {self._grok_error}"
        elif (self.control.brain or s.brain) == "rules":
            why = "rules brain selected"
        else:
            why = "no XAI_API_KEY in .env"
        price, listed = price_for(s.model)
        return {
            "brain": brain,
            "model": s.model if brain == "grok" else "rules",
            "grok_model": s.model,
            "price_listed": listed,
            "price_per_m": {"input": price.input, "cached": price.cached, "output": price.output},
            "why": why,
            "autonomy": self.autonomy,
            "paused": self.control.paused,
        }

    def state(self) -> dict[str, Any]:
        incidents = self.store.incidents(limit=40)
        pending = self.store.proposals(status="pending", limit=50)
        recent = self.store.proposals(limit=40)
        proposals = {p.id: p for p in [*pending, *recent]}
        return {
            "version": __version__,
            "settings": self.settings.public(),
            "control": self.control.to_dict(),
            "effective": self.effective(),
            "crew": [m.to_dict() for m in self.crew.values()],
            "incidents": [i.summary() for i in incidents],
            "proposals": [p.to_dict() for p in sorted(proposals.values(), key=lambda p: p.created, reverse=True)],
            "lab": self.snapshot.to_dict(),
            "settling": len(self.tracker.pending),
            "usage": self.usage,
            "activities": [a.to_dict() for a in self.store.activities(limit=120)],
            "chat": [m.to_dict() for m in self.store.chat(limit=60)],
            "last_event_id": self.bus.last_id,
        }

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loops = [
            asyncio.create_task(self._watch_loop(), name="watcher"),
            asyncio.create_task(self._executor_loop(), name="executor"),
        ]

    async def stop(self) -> None:
        for task in [*self._loops, *self._tasks.values()]:
            task.cancel()
        for task in [*self._loops, *self._tasks.values()]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.pager.aclose()
        await self.lab.aclose()
        if self._grok is not None:
            await self._grok.aclose()
        self.store.close()

    def _recover(self) -> None:
        """After a restart: interrupted investigations start over; interrupted changes are marked failed."""
        active = self.store.incidents(limit=200, active_only=True)
        for inc in active:
            if inc.status == "investigating":
                inc.status = "open"
                inc.assignee = None
                inc.notes.append("the bot restarted during the investigation; starting over")
                self.store.save_incident(inc)
        for p in self.store.proposals(limit=500):
            if p.status in ("approved", "applying"):
                p.status = "failed"
                p.error = "interrupted by a bot restart (check drift before re-proposing)"
                p.updated = now()
                self.store.save_proposal(p)
        self.tracker.load(active)

    async def _watch_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # the watcher must never die
                log.exception("watcher tick failed")
            await asyncio.sleep(max(0.2, self.settings.poll_s - (time.monotonic() - started)))

    # ------------------------------------------------------------------ the watcher
    async def tick(self) -> None:
        """One poll: lab state for the room, problems for the incident tracker, then dispatch work."""
        await self._poll_lab()
        if self.snapshot.ok:
            obs = self.tracker.observe(self.snapshot_raw_problems, busy=self._busy, accept_new=not self.control.paused)
            services = self.snapshot.service_map()
            for inc in obs.created:
                inc.title = auto_title(inc, services)
                self._save(inc)
                self._emit(
                    Activity(
                        bot="POLL",
                        kind="alarm",
                        text=f"{inc.id} opened: {inc.title} ({len(inc.problems)} problem(s), {inc.severity})",
                        incident_id=inc.id,
                        station="board",
                    )
                )
                self._page("opened", inc)
            for inc in obs.updated:
                if inc.report is None:
                    inc.title = auto_title(inc, services)
                self._save(inc)
                self._maybe_reopen(inc)
            for inc in obs.resolved:
                self._resolved(inc)
            self._check_monitoring()
        self.agent.tick()
        self._dispatch()

    async def _poll_lab(self) -> None:
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
            self.snapshot_raw_problems = problems
            snap.problems = [
                {
                    k: p.get(k)
                    for k in ("eventid", "severity", "name", "host", "service", "pw_role", "check", "scope", "clock")
                }
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
        poll = self.crew["POLL"]
        active = sum(1 for p in snap.problems)
        text = (
            f"lab-api unreachable: {snap.error}"
            if not snap.ok
            else ("maintenance mode: watching only" if self.control.paused else f"watching: {active} open problem(s)")
        )
        if poll.activity != text:
            poll.activity = text
            poll.status = "paused" if self.control.paused else "working"
            self._publish_crew()

    def _busy(self, inc: Incident) -> bool:
        task = self._tasks.get(inc.id)
        if task is not None and not task.done():
            return True
        return any(p.status in ("approved", "applying") for p in self.store.proposals(incident_id=inc.id))

    def _maybe_reopen(self, inc: Incident) -> None:
        """New services joined a diagnosed incident: look again (bounded)."""
        if inc.status not in ("diagnosed", "awaiting-approval") or inc.investigations >= MAX_INVESTIGATIONS:
            return
        covered = set((inc.report or {}).get("covered_services") or [])
        if covered and set(inc.services) - covered:
            inc.status = "open"
            inc.notes.append(f"new services joined: {', '.join(sorted(set(inc.services) - covered))}; re-investigating")
            self._save(inc)

    def _check_monitoring(self) -> None:
        t = now()
        for inc in list(self.tracker.incidents.values()):
            if inc.status != "monitoring" or inc.monitoring_since is None or self._busy(inc):
                continue
            if t - inc.monitoring_since < MONITOR_TIMEOUT_S or not inc.active_problems():
                continue
            if inc.investigations < MAX_INVESTIGATIONS:
                inc.status = "open"
                inc.notes.append(f"problems persist {MONITOR_TIMEOUT_S:.0f} s after the fix; re-investigating")
            else:
                inc.status = "diagnosed"
                inc.notes.append("problems persist after the fix; needs a human")
            inc.monitoring_since = None
            self._save(inc)

    def _resolved(self, inc: Incident) -> None:
        task = self._tasks.pop(inc.id, None)
        if task is not None and not task.done():
            task.cancel()
            inc.notes.append("problems cleared before the investigation finished")
        for p in self.store.proposals(incident_id=inc.id, status="pending"):
            p.status = "obsolete"
            p.updated = now()
            p.comment = "the incident cleared before a decision"
            self.store.save_proposal(p)
            self.bus.publish("proposal", p.to_dict())
        took = (inc.resolved or now()) - inc.created
        self._save(inc)
        self._page("resolved", inc)
        self._emit(
            Activity(
                bot=inc.assignee or "POLL",
                kind="resolve",
                text=f"{inc.id} resolved after {took:.0f} s: every problem cleared in Zabbix",
                incident_id=inc.id,
                station="board",
            )
        )

    # ------------------------------------------------------------------ dispatch
    def _idle(self, role: str = "responder") -> list[CrewMember]:
        return [m for m in self.crew.values() if m.role == role and m.status in ("idle", "paused")]

    def _dispatch(self) -> None:
        for m in self.crew.values():
            if m.role == "responder" and m.status in ("idle", "paused"):
                want = "paused" if self.control.paused else "idle"
                if m.status != want:
                    m.status = want
                    m.activity = "maintenance mode" if self.control.paused else ""
                    self._publish_crew()
        while self._chat_queue and self._idle():
            chat_id, msg_id = self._chat_queue.pop(0)
            bot = self._idle()[0]
            self._start(f"chat:{msg_id}", bot, self._answer(chat_id, msg_id, bot.name))
        if self.control.paused:
            return
        waiting = sorted(
            (i for i in self.tracker.incidents.values() if i.status == "open"),
            key=lambda i: (-SEVERITY_RANK.get(i.severity, 0), i.created),
        )
        for inc in waiting:
            idle = self._idle()
            if not idle:
                break
            bot = idle[0]
            inc.status = "investigating"
            inc.assignee = bot.name
            self._save(inc)
            self._start(inc.id, bot, self._investigate(inc.id, bot.name))

    def _start(self, key: str, bot: CrewMember, coro: Any) -> None:
        bot.status = "working"
        bot.task = key
        bot.since = now()
        self._publish_crew()
        task = asyncio.create_task(coro, name=key)
        self._tasks[key] = task

        def done(t: asyncio.Task[Any], key: str = key, bot: CrewMember = bot) -> None:
            if self._tasks.get(key) is t:
                self._tasks.pop(key, None)
            if bot.task == key:
                bot.status = "paused" if self.control.paused else "idle"
                bot.task = None
                bot.activity = ""
                bot.since = now()
                self._publish_crew()
            if not t.cancelled() and t.exception() is not None:
                log.error("task %s failed", key, exc_info=t.exception())

        task.add_done_callback(done)

    # ------------------------------------------------------------------ investigations
    def _grok_config(self, s: BotSettings) -> tuple[Any, ...]:
        return (s.xai_api_key, s.xai_base_url, s.model, s.reasoning_effort)

    def _grok_client(self) -> GrokClient:
        s = self.settings
        key = self._grok_config(s)
        if self._grok is None or self._grok_key != key:
            old = self._grok
            self._grok = self._grok_factory(s)
            self._grok_key = key
            if old is not None:
                closing = asyncio.ensure_future(old.aclose())
                self._closing.add(closing)
                closing.add_done_callback(self._closing.discard)
        return self._grok

    def _brain(self) -> tuple[GrokBrain | RulesBrain, str | None]:
        eff = self.effective()
        if eff["brain"] == "grok":
            return GrokBrain(self._grok_client()), None
        return RulesBrain(), eff["why"]

    def _job(self, bot: str, ctx: ToolContext, mode: str) -> Job:
        return Job(
            bot=bot,
            ctx=ctx,
            tools=toolset(mode, ctx.autonomy),
            emit=self._emit,
            on_usage=lambda u, m, inc=ctx.incident_id: self._account(u, m, inc),
            max_steps=self.settings.max_steps,
        )

    async def _investigate(self, incident_id: str, bot: str) -> None:
        inc = self.tracker.incidents.get(incident_id) or self.store.incident(incident_id)
        if inc is None:
            return
        brain, why = self._brain()
        inc.brain = "grok" if isinstance(brain, GrokBrain) else "rules"
        inc.model = brain.name
        inc.investigations += 1
        self._save(inc)
        ctx = ToolContext(
            lab=self.lab,
            bot=bot,
            hosts=self.snapshot.hosts,
            incident_id=incident_id,
            autonomy=self.autonomy,
            propose=lambda **kw: self._propose(incident_id, bot, inc.brain or "rules", **kw),
        )
        job = self._job(bot, ctx, "incident")
        seen = set(inc.problems)
        cleared_seen = {e for e, p in inc.problems.items() if p.get("cleared")}

        def updates() -> str | None:
            nonlocal seen, cleared_seen
            cur = self.tracker.incidents.get(incident_id)
            if cur is None:
                return None
            new = [p for e, p in cur.problems.items() if e not in seen]
            cleared = [e for e, p in cur.problems.items() if p.get("cleared") and e not in cleared_seen]
            seen = set(cur.problems)
            cleared_seen |= set(cleared)
            return prompts.update_note(
                [{k: p.get(k) for k in ("severity", "name", "host", "service", "check")} for p in new],
                [cur.problems[e].get("name", e) for e in cleared],
            )

        job.updates = updates
        note = None
        if inc.investigations > 1:
            note = "This is a follow-up investigation. Earlier: " + "; ".join(inc.notes[-3:])
            if inc.report:
                note += f"\nYour previous report: {json.dumps({k: inc.report.get(k) for k in ('title', 'root_cause')})}"
            done = [p for p in self.store.proposals(incident_id=incident_id) if p.status in ("applied", "failed")]
            if done:
                note += "\nAlready executed: " + "; ".join(
                    f"{p.id} {p.headline()} -> {p.status} {(p.result or {}).get('verdict') or p.error or ''}"
                    for p in done
                )
        brief = prompts.incident_brief(inc, self.snapshot.service_map(), note)
        self._emit(
            Activity(
                bot=bot,
                kind="assign",
                text=f"taking {incident_id} ({inc.brain}{': ' + why if why and inc.brain == 'rules' else ''})",
                incident_id=incident_id,
                station="board",
            )
        )
        try:
            try:
                report = await brain.investigate(job, brief)
            except GrokError as exc:
                self._grok_failed(exc)
                self._emit(
                    Activity(
                        bot=bot,
                        kind="error",
                        text=f"Grok failed ({exc}); the rules brain takes over",
                        incident_id=incident_id,
                        ok=False,
                    )
                )
                inc.brain, inc.model = "rules", "rules"
                inc.notes.append(f"Grok failed: {exc}")
                job = self._job(bot, ctx, "incident")
                report = await RulesBrain().investigate(job, brief)
        except asyncio.CancelledError:
            self._emit(Activity(bot=bot, kind="idle", text=f"{incident_id} cleared; stopping", incident_id=incident_id))
            raise
        except LabApiError as exc:
            report = {
                "title": inc.title,
                "summary": f"lab-api failed: {exc}",
                "root_cause": "not determined",
                "category": "unknown",
                "confidence": "low",
                "proposals": list(ctx.proposals),
            }
        inc = self.tracker.incidents.get(incident_id) or inc
        report["covered_services"] = list(inc.services)
        inc.report = report
        if report.get("title"):
            inc.title = str(report["title"])[:120]
        pending = [
            p for p in self.store.proposals(incident_id=incident_id) if p.status in ("pending", "approved", "applying")
        ]
        if inc.status == "investigating":
            if any(p.status == "pending" for p in pending):
                inc.status = "awaiting-approval"
            elif pending:
                inc.status = "remediating"
            else:
                inc.status = "diagnosed"
        self._save(inc)
        if inc.status != "resolved":
            self._page("diagnosed", inc)
        self._emit(
            Activity(
                bot=bot,
                kind="report",
                text=f"{incident_id}: {report.get('title')} "
                f"[{report.get('category')}, confidence {report.get('confidence')}]",
                incident_id=incident_id,
                station="desk",
            )
        )
        if any(p.status == "pending" for p in pending):
            self._emit(
                Activity(
                    bot=bot,
                    kind="await",
                    text=f"{incident_id}: {sum(p.status == 'pending' for p in pending)} change(s) "
                    "wait for your approval",
                    incident_id=incident_id,
                    station="operator",
                )
            )

    def _grok_failed(self, exc: GrokError) -> None:
        if exc.kind in ("auth", "billing"):
            self._grok_error = f"{exc.kind}: {exc}"
            self._grok_error_key = self._grok_config(self.settings)
            self.bus.publish("control", {**self.control.to_dict(), "effective": self.effective()})

    # ------------------------------------------------------------------ proposals
    async def _propose(
        self,
        incident_id: str | None,
        bot: str,
        brain: str,
        *,
        kind: str,
        host: str,
        reason: str,
        params: dict[str, Any],
        allow_auto: bool = True,
    ) -> dict[str, Any]:
        # One proposal at a time: the duplicate check and the ticket must not straddle another proposer's dry run
        # (two responders, or a responder and an outside agent, filing the same change at once).
        async with self._propose_lock:
            return await self._propose_locked(
                incident_id, bot, brain, kind=kind, host=host, reason=reason, params=params, allow_auto=allow_auto
            )

    async def _propose_locked(
        self,
        incident_id: str | None,
        bot: str,
        brain: str,
        *,
        kind: str,
        host: str,
        reason: str,
        params: dict[str, Any],
        allow_auto: bool,
    ) -> dict[str, Any]:
        existing = (
            [
                p
                for p in self.store.proposals(incident_id=incident_id, status="pending")
                if p.kind == kind and p.host == host and p.params == params
            ]
            if incident_id
            else []
        )
        actor = f"{self.settings.actor}/{bot}"
        comment = f"{incident_id or 'chat'}: {reason}"[:400]
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
        if existing:
            p = existing[0]
            p.plan = slim_plan(plan)
            if p.proposed_by == bot:  # someone else's ticket for the same change keeps its author's reason
                p.reason = reason
            p.updated = now()
        else:
            p = Proposal(
                id=self.store.next_id("CHG"),
                incident_id=incident_id,
                kind=kind,
                host=host,
                reason=reason,
                proposed_by=bot,
                brain=brain,
                params=params,
                plan=slim_plan(plan),
            )
        self.store.save_proposal(p)
        inc = self.tracker.incidents.get(incident_id or "")
        if inc is not None and p.id not in inc.proposals:
            inc.proposals.append(p.id)
            self._save(inc)
        self.bus.publish("proposal", p.to_dict())
        self._emit(
            Activity(
                bot=bot,
                kind="propose",
                text=f"{p.id}: {p.headline()}",
                incident_id=incident_id,
                station="operator",
                target=host,
            )
        )
        auto = self.autonomy == "auto" and allow_auto
        if auto:
            await self.approve(p.id, by="policy:auto", comment="BOT_AUTONOMY=auto")
        diff = (p.plan or {}).get("diff") or []
        return {
            "proposal_id": p.id,
            "status": p.status,
            "headline": p.headline(),
            "proposed_by": p.proposed_by,
            "diff": [d.get("message") for d in diff],
            "affected_services": (p.plan or {}).get("affected_services"),
            "drift_after_predicted": (p.plan or {}).get("drift_after_predicted"),
            "note": "applied by policy" if auto else "waits for the operator's approval",
        }

    async def approve(self, proposal_id: str, by: str, comment: str | None = None) -> Proposal:
        p = self.store.proposal(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != "pending":
            raise ValueError(f"{proposal_id} is {p.status}, not pending")
        p.status, p.decided_by, p.decided, p.comment, p.updated = "approved", by, now(), comment, now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        self._emit(
            Activity(
                bot="operator" if not by.startswith("policy") else "POLICY",
                kind="approve",
                text=f"{p.id} approved by {by}: {p.headline()}",
                incident_id=p.incident_id,
                station="operator",
                target=p.host,
            )
        )
        inc = self.tracker.incidents.get(p.incident_id or "")
        if inc is not None and inc.status in ("awaiting-approval", "diagnosed"):
            inc.status = "remediating"
            self._save(inc)
        self._exec_queue.put_nowait(p.id)
        return p

    async def approve_incident(self, incident_id: str, by: str, comment: str | None = None) -> list[Proposal]:
        pending = sorted(self.store.proposals(incident_id=incident_id, status="pending"), key=change_order)
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
        self._emit(
            Activity(
                bot="operator",
                kind="reject",
                text=f"{p.id} rejected by {by}{': ' + comment if comment else ''}",
                incident_id=p.incident_id,
                station="operator",
            )
        )
        inc = self.tracker.incidents.get(p.incident_id or "")
        if inc is not None and inc.status == "awaiting-approval":
            left = self.store.proposals(incident_id=inc.id, status="pending")
            if not left:
                inc.status = "diagnosed"
                inc.notes.append(f"{p.id} rejected{': ' + comment if comment else ''}")
                self._save(inc)
        return p

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
        p.status, p.updated = "applying", now()
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        inc = self.tracker.incidents.get(p.incident_id or "")
        if inc is not None:
            inc.status = "remediating"
            self._save(inc)
        actor = f"{self.settings.actor}/{p.proposed_by} approved-by:{p.decided_by}"
        comment = f"{p.incident_id or 'chat'} {p.id}: {p.reason}"[:400]
        try:
            if p.kind == "field-repair":
                result = await self._field_repair(p, actor)
            else:
                plan_id = (p.plan or {}).get("plan_id")
                bot = p.proposed_by if p.proposed_by in self.crew else "PING"
                self._emit(
                    Activity(
                        bot=bot,
                        kind="apply",
                        text=f"committing {p.id} on {p.host} (approved by {p.decided_by})",
                        incident_id=p.incident_id,
                        station="rack",
                        target=p.host,
                    )
                )
                if p.kind == "apply-intent":
                    result = await self.lab.apply_intent(p.host, plan_id or "", actor, comment)
                else:
                    result = await self.lab.apply_patch(
                        p.host, p.params.get("patch") or [], plan_id or "", actor, comment
                    )
                verdict = (result.get("verification") or {}).get("verdict")
                self._emit(
                    Activity(
                        bot=bot,
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
                self.store.save_proposal(p)
                self.bus.publish("proposal", p.to_dict())
                if inc is not None and not [
                    x for x in self.store.proposals(incident_id=inc.id) if x.status in ("approved", "applying")
                ]:
                    waiting = self.store.proposals(incident_id=inc.id, status="pending")
                    inc.status = "awaiting-approval" if waiting else "monitoring"
                    inc.monitoring_since = None if waiting else now()
                    self._save(inc)
                return
            if isinstance(new_plan, dict):
                p.plan = slim_plan(new_plan)
                p.status, p.decided_by, p.decided, p.updated = "pending", None, None, now()
                p.comment = "the device or NetBox changed since the dry run: review the new plan"
                self.store.save_proposal(p)
                self.bus.publish("proposal", p.to_dict())
                self._emit(
                    Activity(
                        bot="POLL",
                        kind="error",
                        text=f"{p.id}: plan changed since approval; re-approve",
                        incident_id=p.incident_id,
                        ok=False,
                    )
                )
                if inc is not None:
                    inc.status = "awaiting-approval"
                    self._save(inc)
                return
            p.status, p.error, p.updated = "failed", f"{exc}: {str(exc.detail)[:300]}", now()
            self._emit(
                Activity(bot="POLL", kind="error", text=f"{p.id} failed: {exc}", incident_id=p.incident_id, ok=False)
            )
        self.store.save_proposal(p)
        self.bus.publish("proposal", p.to_dict())
        if inc is not None:
            left = [x for x in self.store.proposals(incident_id=inc.id) if x.status in ("approved", "applying")]
            if not left:
                waiting = self.store.proposals(incident_id=inc.id, status="pending")
                inc.status = "awaiting-approval" if waiting else "monitoring"
                inc.monitoring_since = now() if not waiting else None
                self._save(inc)

    async def _field_repair(self, p: Proposal, actor: str) -> dict[str, Any]:
        tech = self.crew.get("PATCH")
        if tech is not None:
            tech.status, tech.task, tech.since = "away", p.id, now()
            tech.activity = f"on the way to {p.host}"
            self._publish_crew()
        self._emit(
            Activity(
                bot="PATCH",
                kind="travel",
                text=f"{p.id}: driving to {p.host}'s site ({p.headline()})",
                incident_id=p.incident_id,
                station="door",
                target=p.host,
            )
        )
        try:
            await asyncio.sleep(self.settings.field_delay_s)
            result = await self.lab.restore_fault(
                p.params.get("kind", "device"),
                p.host,
                actor,
                peer=p.params.get("peer"),
                interface=p.params.get("interface"),
            )
            self._emit(
                Activity(
                    bot="PATCH",
                    kind="apply",
                    text=f"{p.id}: {result.get('done', 'repaired')}",
                    incident_id=p.incident_id,
                    station="door",
                    target=p.host,
                )
            )
            return result
        finally:
            if tech is not None:
                tech.status, tech.task, tech.activity, tech.since = "idle", None, "", now()
                self._publish_crew()
                self._emit(
                    Activity(
                        bot="PATCH", kind="return", text="back at the bench", incident_id=p.incident_id, station="bench"
                    )
                )

    # ------------------------------------------------------------------ chat
    async def ask(self, message: str, chat_id: str | None = None, by: str = "operator") -> ChatMessage:
        chat_id = chat_id or self.store.next_id("CHAT")
        msg = ChatMessage(id=self.store.next_id("MSG", 6), chat_id=chat_id, role="user", text=message[:4000], bot=by)
        self.store.save_chat(msg)
        self.bus.publish("chat", msg.to_dict())
        self._chat_queue.append((chat_id, msg.id))
        self._dispatch()
        return msg

    async def _answer(self, chat_id: str, msg_id: str, bot: str) -> None:
        question = next((m for m in self.store.chat(chat_id) if m.id == msg_id), None)
        if question is None:
            return
        brain, _why = self._brain()
        ctx = ToolContext(
            lab=self.lab,
            bot=bot,
            hosts=self.snapshot.hosts,
            chat_id=chat_id,
            autonomy=self.autonomy,
            propose=lambda **kw: self._propose(None, bot, "grok" if isinstance(brain, GrokBrain) else "rules", **kw),
        )
        job = self._job(bot, ctx, "chat")
        self._emit(
            Activity(bot=bot, kind="chat", text=f"question: {question.text[:120]}", chat_id=chat_id, station="operator")
        )
        prev = self._chat_prev.get(chat_id) if isinstance(brain, GrokBrain) else None
        try:
            try:
                try:
                    text, prev = await brain.chat(job, question.text, prev)
                except GrokError as exc:
                    if prev is None or exc.kind in ("auth", "billing"):
                        raise
                    # the stored conversation may be gone (xAI keeps responses 30 days): start a fresh one
                    text, prev = await brain.chat(self._job(bot, ctx, "chat"), question.text, None)
            except GrokError as exc:
                self._grok_failed(exc)
                text, prev = await RulesBrain().chat(self._job(bot, ctx, "chat"), question.text, None)
                text = f"(Grok failed: {exc}; the rules autopilot answers)\n{text}"
        except LabApiError as exc:
            text = f"lab-api failed: {exc}"
        if prev:
            self._chat_prev[chat_id] = prev
            self.store.set_meta("chat_prev", self._chat_prev)
        answer = ChatMessage(
            id=self.store.next_id("MSG", 6),
            chat_id=chat_id,
            role="bot",
            text=text,
            bot=bot,
            proposals=list(ctx.proposals),
        )
        self.store.save_chat(answer)
        self.bus.publish("chat", answer.to_dict())
        self._emit(
            Activity(
                bot=bot,
                kind="say",
                text=text.splitlines()[0][:200] if text else "",
                chat_id=chat_id,
                station="operator",
            )
        )

    # ------------------------------------------------------------------ control
    async def set_control(
        self,
        by: str,
        paused: bool | None = None,
        brain: str | None = None,
        autonomy: str | None = None,
        reason: str | None = None,
        pager_muted: bool | None = None,
    ) -> Control:
        if brain is not None and brain not in (*BRAINS, "default"):
            raise ValueError(f"brain must be one of {', '.join(BRAINS)} or default")
        if autonomy is not None and autonomy not in (*AUTONOMIES, "default"):
            raise ValueError(f"autonomy must be one of {', '.join(AUTONOMIES)} or default")
        c = self.control
        if paused is not None:
            c.paused = paused
            c.reason = reason if paused else None
        if brain is not None:
            c.brain = None if brain == "default" else brain
            self._grok_error = None
        if autonomy is not None:
            c.autonomy = None if autonomy == "default" else autonomy
        if pager_muted is not None:
            c.pager_muted = pager_muted
        c.by, c.since = by, now()
        self.store.set_meta("control", c.to_dict())
        self.bus.publish("control", {**c.to_dict(), "effective": self.effective()})
        self._emit(
            Activity(
                bot="operator",
                kind="control",
                text=f"{by}: "
                + ", ".join(
                    x
                    for x in (
                        None
                        if paused is None
                        else (
                            "maintenance mode on" + (f" ({reason})" if reason else "")
                            if paused
                            else "maintenance mode off"
                        ),
                        None if brain is None else f"brain {brain}",
                        None if autonomy is None else f"autonomy {autonomy}",
                        None if pager_muted is None else ("pager muted" if pager_muted else "pager on"),
                    )
                    if x
                ),
                station="operator",
            )
        )
        self._dispatch()
        return c

    async def reinvestigate(self, incident_id: str, by: str) -> Incident:
        inc = self.tracker.incidents.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        task = self._tasks.get(incident_id)
        if task is not None and not task.done():
            raise ValueError(f"{incident_id} is being investigated right now")
        inc.status = "open"
        inc.notes.append(f"{by} asked for a new investigation")
        self._save(inc)
        self._dispatch()
        return inc

    async def grok_check(self) -> dict[str, Any]:
        """A tiny Grok round trip with one tool call: is the key valid, the model there, function calling working,
        and are the responders' real tool schemas accepted (they are sent along, unused)?"""
        from netsim_bot.grok import function_tool, tool_output
        from netsim_bot.tools import ALL_TOOLS

        s = self.settings
        if not s.xai_api_key:
            return {"ok": False, "error": "XAI_API_KEY is not set in .env"}
        client = self._grok_client()
        tool = function_tool(
            "lab_time",
            "Returns the lab's current time.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        t0 = time.monotonic()
        try:
            first = await client.respond(
                input=[
                    {"role": "user", "content": "Call lab_time, then answer with the time it returns, nothing else."}
                ],
                tools=[tool, *(t.schema() for t in ALL_TOOLS.values())],
            )
            self._account(first.usage, client.model, None)
            calls = [c.name for c in first.calls]
            answer = first.text
            if first.calls:
                second = await client.respond(
                    input=[tool_output(first.calls[0].call_id, {"time": time.strftime("%H:%M:%S")})],
                    tools=[tool],
                    previous_response_id=first.id,
                )
                self._account(second.usage, client.model, None)
                answer = second.text
        except GrokError as exc:
            self._grok_failed(exc)
            return {"ok": False, "error": str(exc), "kind": exc.kind, "model": s.model}
        self._grok_error = None
        return {
            "ok": bool(calls),
            "model": s.model,
            "response_model": first.model,
            "tool_calls": calls,
            "answer": answer,
            "seconds": round(time.monotonic() - t0, 2),
            "usage": self.usage,
        }

    # ------------------------------------------------------------------ the pager
    def _page(self, event: str, inc: Incident) -> str | None:
        services = self.snapshot.service_map()
        active = inc.active_problems() or inc.problem_refs()
        s = self.settings
        summary = f"{inc.id} {event} [{inc.severity}]: {inc.title}"
        if event == "diagnosed" and inc.report:
            summary += f" (root cause: {str(inc.report.get('root_cause'))[:200]})"
        body: dict[str, Any] = {
            "summary": summary,
            "incident": {
                "id": inc.id,
                "title": inc.title,
                "severity": inc.severity,
                "status": inc.status,
                "services": inc.services,
                "hosts": inc.hosts,
                "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(inc.created)),
                "resolved_after_s": round(inc.resolved - inc.created) if inc.resolved else None,
                "problems": [
                    {"severity": p.severity, "name": p.name, "host": p.host, "service": p.service} for p in active[:20]
                ],
            },
            "services": [
                {k: services[sid].get(k) for k in ("service", "customer", "tier", "region", "zabbix")}
                for sid in inc.services
                if sid in services
            ],
            "links": {"noc_room": s.public_url, **s.public()["links"]},
        }
        if event != "opened":
            body["report"] = inc.report
            body["proposals"] = [
                {"id": p.id, "status": p.status, "headline": p.headline()}
                for p in self.store.proposals(incident_id=inc.id)
            ]
        return self.pager.page(event, inc.summary(), body, muted=self.control.pager_muted, paused=self.control.paused)

    def _paged(self, rec: dict[str, Any]) -> None:
        self.store.set_meta("pager_log", list(self.pager.history))
        if rec.get("status") == "skipped":
            return
        ok = rec.get("status") == "sent"
        http = f"HTTP {rec['http']}" if rec.get("http") else rec.get("error", "no answer")
        self._emit(
            Activity(
                bot="POLL",
                kind="page",
                text=f"paged {self.agent_name}: {rec.get('event')} {rec.get('incident_id') or ''} ({http})"
                if ok
                else f"page to {self.agent_name} failed: {rec.get('event')} {rec.get('incident_id') or ''} ({http})",
                incident_id=rec.get("incident_id"),
                station="console",
                ok=ok,
            )
        )

    async def pager_test(self) -> dict[str, Any]:
        """One test page now (whatever the events, severity, mute and rate settings say)."""
        if not self.settings.pager_url:
            return {"status": "failed", "error": "BOT_PAGER_URL is not set in .env"}
        payload = self.pager.payload(
            "test",
            {
                "summary": "test page from the NetSim NOC: no incident, nothing to do but answer that it arrived",
                "incident": None,
                "links": {"noc_room": self.settings.public_url, **self.settings.public()["links"]},
            },
        )
        return await self.pager.deliver(payload)

    def pager_state(self) -> dict[str, Any]:
        return {
            **self.settings.public()["pager"],
            "muted": self.control.pager_muted,
            "paused": self.control.paused,
            "history": list(self.pager.history)[::-1],
        }

    # ------------------------------------------------------------------ plumbing
    def _save(self, inc: Incident) -> None:
        inc.updated = now()
        self.store.save_incident(inc)
        self.bus.publish("incident", inc.summary())

    def _emit(self, a: Activity) -> None:
        self.store.add_activity(a)
        self.bus.publish("activity", a.to_dict())
        if a.bot == self.agent_name:
            self.agent.arrive(a.tool or a.kind)  # e.g. its approved ticket being applied brings it back in
        member = self.crew.get(a.bot)
        if member is not None and a.kind in ("tool", "say", "assign", "propose", "report", "apply", "travel", "chat"):
            member.activity = a.text[:160]
            self._publish_crew()

    def _publish_crew(self) -> None:
        self.bus.publish("crew", [m.to_dict() for m in self.crew.values()])

    def _roll_usage_day(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.usage.get("day") != day:
            self.usage["day"] = day
            self.usage["usd_day"] = 0.0
        for k in ("usd_total", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "calls"):
            self.usage.setdefault(k, 0 if k != "usd_total" else 0.0)

    def _account(self, usage: Usage, model: str, incident_id: str | None) -> None:
        self._roll_usage_day()
        usd = cost_usd(model, usage)
        self.usage["usd_day"] = round(self.usage["usd_day"] + usd, 6)
        self.usage["usd_total"] = round(self.usage["usd_total"] + usd, 6)
        for k, v in usage.to_dict().items():
            self.usage[k] = self.usage.get(k, 0) + v
        self.usage["model"] = model
        self.store.set_meta("usage", self.usage)
        self.bus.publish("usage", self.usage)
        inc = self.tracker.incidents.get(incident_id or "")
        if inc is not None:
            u = inc.usage
            u["usd"] = round(u.get("usd", 0.0) + usd, 6)
            for k, v in usage.to_dict().items():
                u[k] = u.get(k, 0) + v
            u["model"] = model
            self.store.save_incident(inc)
