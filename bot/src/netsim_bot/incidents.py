"""From Zabbix problem snapshots to incidents (pure logic; the engine feeds it a poll of lab-api every few seconds).

* Only problems with a scope that means "something is wrong" count: ``l2vpn-pw`` (missing / oper / spec),
  ``l2vpn-unexpected``, ``interface``, ``availability``. Trap events (``l2vpn-trap``) are hints and never open an
  incident (contract: traps are hints, polling is truth).
* New problems wait in a settle window (``settle_s`` after the newest one, at most ``max_settle_s`` after the first)
  so that one fault's problems, which Zabbix raises over a few seconds, become one incident.
* A new problem joins an active incident that already has its service (or, for a problem without a service, its
  host); the rest of a batch opens one new incident together.
* An incident is resolved once every one of its problems has been gone for ``resolve_hold_s`` and nothing is
  running for it (the engine's ``busy`` predicate: an approved change still verifying, an investigation that should
  first report). A problem that comes back during the hold keeps it open.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from netsim_bot.models import Incident, ProblemRef, now

INCIDENT_SCOPES = ("l2vpn-pw", "l2vpn-unexpected", "interface", "availability")


@dataclass
class Observation:
    created: list[Incident] = field(default_factory=list)
    updated: list[Incident] = field(default_factory=list)
    resolved: list[Incident] = field(default_factory=list)

    def touched(self) -> list[Incident]:
        seen: dict[str, Incident] = {}
        for inc in (*self.created, *self.updated, *self.resolved):
            seen[inc.id] = inc
        return list(seen.values())


class IncidentTracker:
    def __init__(
        self,
        new_id: Callable[[], str],
        settle_s: float = 6.0,
        max_settle_s: float = 20.0,
        resolve_hold_s: float = 8.0,
    ) -> None:
        self.new_id = new_id
        self.settle_s = settle_s
        self.max_settle_s = max(max_settle_s, settle_s)
        self.resolve_hold_s = resolve_hold_s
        self.incidents: dict[str, Incident] = {}
        self.pending: dict[str, ProblemRef] = {}
        self.owner: dict[str, str] = {}
        self._clear_since: dict[str, float] = {}

    def load(self, incidents: Iterable[Incident]) -> None:
        """Active incidents from the store (after a restart): their problems are known again."""
        for inc in incidents:
            if not inc.active:
                continue
            self.incidents[inc.id] = inc
            for eventid in inc.problems:
                self.owner[eventid] = inc.id

    def forget(self, incident_id: str) -> None:
        self.incidents.pop(incident_id, None)
        self._clear_since.pop(incident_id, None)

    @property
    def settling(self) -> list[ProblemRef]:
        return list(self.pending.values())

    def observe(
        self,
        problems: list[dict[str, Any]],
        at: float | None = None,
        busy: Callable[[Incident], bool] = lambda _i: False,
        accept_new: bool = True,
    ) -> Observation:
        t = now() if at is None else at
        obs = Observation()
        current = {str(p.get("eventid")): p for p in problems if p.get("scope") in INCIDENT_SCOPES}
        updated: dict[str, Incident] = {}

        # 1. problems of known incidents: cleared, back again, or changed severity
        for inc in self.incidents.values():
            changed = False
            for eventid, ref in inc.problems.items():
                live = current.get(eventid)
                if live is None and ref.get("cleared") is None:
                    ref["cleared"] = t
                    changed = True
                elif live is not None and ref.get("cleared") is not None:
                    ref["cleared"] = None
                    changed = True
                if live is not None and live.get("severity") and live.get("severity") != ref.get("severity"):
                    ref["severity"] = live["severity"]
                    changed = True
            if changed:
                inc.refresh_severity()
                inc.updated = t
                updated[inc.id] = inc

        # 2. new problems wait in the settle window; ones that vanished before it closed are dropped
        if accept_new:
            for eventid, p in current.items():
                if eventid not in self.owner and eventid not in self.pending:
                    ref = ProblemRef.from_problem(p)
                    ref.first_seen = t
                    self.pending[eventid] = ref
        else:
            self.pending.clear()  # maintenance mode: new problems open nothing
        for eventid in [e for e in self.pending if e not in current]:
            del self.pending[eventid]

        if self.pending:
            first = min(r.first_seen for r in self.pending.values())
            last = max(r.first_seen for r in self.pending.values())
            if t - last >= self.settle_s or t - first >= self.max_settle_s:
                batch = sorted(self.pending.values(), key=lambda r: (r.clock, r.eventid))
                self.pending.clear()
                leftovers: list[ProblemRef] = []
                for ref in batch:
                    inc = self._match(ref)
                    if inc is None:
                        leftovers.append(ref)
                        continue
                    self._attach(inc, ref, t)
                    updated[inc.id] = inc
                if leftovers:
                    inc = Incident(id=self.new_id(), created=t, updated=t)
                    for ref in leftovers:
                        self._attach(inc, ref, t)
                    self.incidents[inc.id] = inc
                    obs.created.append(inc)
                    updated.pop(inc.id, None)

        # 3. resolution: every problem gone for resolve_hold_s, nothing running
        for inc in list(self.incidents.values()):
            if inc.active_problems() or any(_matches(inc, r) for r in self.pending.values()):
                self._clear_since.pop(inc.id, None)
                continue
            since = self._clear_since.setdefault(inc.id, t)
            if t - since >= self.resolve_hold_s and not busy(inc):
                inc.status = "resolved"
                inc.resolved = t
                inc.updated = t
                obs.resolved.append(inc)
                updated.pop(inc.id, None)
                self.forget(inc.id)
        obs.updated.extend(i for i in updated.values() if i.id in self.incidents)
        return obs

    def _match(self, ref: ProblemRef) -> Incident | None:
        """The newest active incident with the problem's service (or its host, for a problem without service)."""
        candidates = sorted(self.incidents.values(), key=lambda i: i.created, reverse=True)
        return next((inc for inc in candidates if _matches(inc, ref)), None)

    def _attach(self, inc: Incident, ref: ProblemRef, t: float) -> None:
        inc.add_problem(ref)
        inc.updated = t
        self.owner[ref.eventid] = inc.id


def _matches(inc: Incident, ref: ProblemRef) -> bool:
    if ref.service:
        return ref.service in inc.services
    return bool(ref.host) and ref.host in inc.hosts


def auto_title(inc: Incident, services: dict[str, dict[str, Any]] | None = None) -> str:
    """A title before the diagnosis: what Zabbix shows, by service (with its tier) or host."""
    services = services or {}
    active = inc.active_problems() or inc.problem_refs()
    if not active:
        return f"{inc.id}: no problems"
    by_service: dict[str, list[ProblemRef]] = {}
    hosts = sorted({p.host for p in active if p.host})
    for p in active:
        if p.service:
            by_service.setdefault(p.service, []).append(p)
    if len(by_service) == 1:
        svc, probs = next(iter(by_service.items()))
        tier = (services.get(svc) or {}).get("sla_tier")
        roles = sorted({p.pw_role for p in probs if p.pw_role})
        checks = {p.check for p in probs}
        what = "PW down" if "oper" in checks else "PW missing" if "missing" in checks else "PW off-spec"
        role = f"{'/'.join(roles)} " if roles else ""
        return f"{svc}{f' ({tier})' if tier else ''}: {role}{what}"
    if by_service:
        names = sorted(by_service)
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        return f"{len(names)} services: {shown}"
    return f"{', '.join(hosts[:3]) or 'lab'}: {len(active)} problem{'s' if len(active) != 1 else ''}"
