"""The bot's records: incidents (grouped Zabbix problems), proposals (changes waiting for a human), activity, crew.

Plain dataclasses with ``to_dict``/``from_dict`` (stored as JSON documents in SQLite, sent as-is to the NOC page).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

SEVERITIES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

IncidentStatus = Literal[
    "open",  # detected, waiting for a free responder
    "investigating",  # a responder (Grok or rules) is working on it
    "diagnosed",  # report written, nothing to approve (observe mode, or no fix the bot can make)
    "awaiting-approval",  # proposals wait for a human
    "remediating",  # approved changes / field repairs are running
    "monitoring",  # remediation done, waiting for Zabbix to clear
    "resolved",  # every problem cleared
]
ACTIVE_STATUSES = ("open", "investigating", "diagnosed", "awaiting-approval", "remediating", "monitoring")

ProposalKind = Literal["apply-intent", "config-patch", "field-repair"]
ProposalStatus = Literal[
    "pending",  # waiting for a human (or the auto policy)
    "approved",  # queued for execution
    "applying",  # running (config commit + verification, or the technician is on site)
    "applied",  # done; result holds the verification
    "failed",  # lab-api refused or the verification failed
    "rejected",  # a human said no
    "obsolete",  # the incident cleared before anyone decided
]


def now() -> float:
    return round(time.time(), 3)


def worst(severities: list[str] | tuple[str, ...] | set[str]) -> str:
    known = [s for s in severities if s in SEVERITY_RANK]
    return max(known, key=SEVERITY_RANK.__getitem__) if known else "Not classified"


class _Doc:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]):  # type: ignore[no-untyped-def]
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class ProblemRef(_Doc):
    """A Zabbix problem as an incident remembers it (``cleared`` once Zabbix no longer lists it)."""

    eventid: str
    name: str
    severity: str
    host: str | None
    service: str | None
    pw_role: str | None
    check: str | None
    scope: str | None
    clock: float
    opdata: str | None = None
    first_seen: float = field(default_factory=now)
    cleared: float | None = None

    @classmethod
    def from_problem(cls, p: dict[str, Any]) -> ProblemRef:
        return cls(
            eventid=str(p.get("eventid")),
            name=str(p.get("name") or ""),
            severity=str(p.get("severity") or "Not classified"),
            host=p.get("host"),
            service=p.get("service"),
            pw_role=p.get("pw_role"),
            check=p.get("check"),
            scope=p.get("scope"),
            clock=float(p.get("clock") or 0.0),
            opdata=p.get("opdata"),
        )


@dataclass
class Incident(_Doc):
    id: str
    status: str = "open"
    severity: str = "Not classified"
    title: str = ""
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)
    resolved: float | None = None
    problems: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    assignee: str | None = None
    brain: str | None = None
    model: str | None = None
    report: dict[str, Any] | None = None
    proposals: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    impact: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    investigations: int = 0
    monitoring_since: float | None = None

    # ------------------------------------------------------------------ problems
    def problem_refs(self) -> list[ProblemRef]:
        return [ProblemRef.from_dict(p) for p in self.problems.values()]

    def active_problems(self) -> list[ProblemRef]:
        return [p for p in self.problem_refs() if p.cleared is None]

    def add_problem(self, ref: ProblemRef) -> None:
        self.problems[ref.eventid] = ref.to_dict()
        if ref.service and ref.service not in self.services:
            self.services.append(ref.service)
            self.services.sort()
        if ref.host and ref.host not in self.hosts:
            self.hosts.append(ref.host)
            self.hosts.sort()
        self.refresh_severity()

    def refresh_severity(self) -> None:
        active = self.active_problems()
        refs = active or self.problem_refs()
        self.severity = worst([p.severity for p in refs])

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def summary(self) -> dict[str, Any]:
        """The incident without its full problem list (lists, SSE)."""
        data = self.to_dict()
        active = self.active_problems()
        data["problem_count"] = len(self.problems)
        data["active_problem_count"] = len(active)
        data.pop("problems")
        return data


@dataclass
class Proposal(_Doc):
    id: str
    incident_id: str | None
    kind: str
    host: str
    reason: str
    proposed_by: str
    brain: str
    status: str = "pending"
    params: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)
    decided_by: str | None = None
    decided: float | None = None
    comment: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    order: int = 0

    def headline(self) -> str:
        if self.kind == "field-repair":
            what = self.params.get("kind", "?")
            target = self.host
            if self.params.get("peer"):
                target = f"{self.host} <-> {self.params['peer']}"
            if self.params.get("interface"):
                target = f"{self.host} {self.params['interface']}"
            verb = "power on" if what == "device" else f"repair {what}"
            return f"field: {verb} {target}"
        diff = (self.plan or {}).get("diff") or []
        verb = "apply NetBox intent to" if self.kind == "apply-intent" else "config change on"
        return f"{verb} {self.host} ({len(diff)} change{'s' if len(diff) != 1 else ''})"


@dataclass
class Activity(_Doc):
    """One thing a bot did or said; the NOC page animates these (``station`` is where in the room it happens)."""

    bot: str
    kind: str  # think, say, tool, result, propose, report, approve, apply, travel, alarm, resolve, chat, error, idle
    text: str
    incident_id: str | None = None
    chat_id: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    station: str | None = None
    target: str | None = None
    ok: bool = True
    duration_s: float | None = None
    ts: float = field(default_factory=now)
    seq: int | None = None


@dataclass
class CrewMember(_Doc):
    name: str
    role: str  # watcher, responder, field
    title: str
    color: str
    status: str = "idle"  # idle, working, waiting, away, paused
    task: str | None = None
    activity: str = ""
    since: float = field(default_factory=now)


@dataclass
class ChatMessage(_Doc):
    id: str
    chat_id: str
    role: str  # user, bot
    text: str
    bot: str | None = None
    ts: float = field(default_factory=now)
    proposals: list[str] = field(default_factory=list)
    pending: bool = False
