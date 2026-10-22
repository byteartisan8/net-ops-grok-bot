"""The office's records: alerts (from Zabbix), incidents (the customers they hit), change tickets, pages, activity,
the role robots, Ask-the-NOC questions.

Plain dataclasses with ``to_dict``/``from_dict`` (stored as JSON documents in SQLite, sent as-is to the NOC page).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

SEVERITIES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)} | {"OK": -1}
TIER_RANK = {"gold": 0, "silver": 1, "bronze": 2}

#: A customer's state from its Zabbix BSM node: Warning = degraded, Disaster = down (contract 9.4).
CustomerState = Literal["degraded", "down", "ok"]

IncidentStatus = Literal[
    "new",  # opened from a batch of alerts, the NOC Engineer is paged; nobody took it yet
    "working",  # a role took it (triage, root cause, escalations)
    "awaiting-approval",  # change tickets wait for a human
    "remediating",  # approved tickets are running (a config change, or a field repair on its way)
    "recovered",  # Zabbix shows every customer OK again; the NOC Engineer verifies and closes
    "closed",  # closed by the NOC Engineer
]
ACTIVE_STATUSES = ("new", "working", "awaiting-approval", "remediating", "recovered")

ProposalKind = Literal["apply-intent", "config-patch", "field-repair"]
ProposalStatus = Literal[
    "pending",  # waiting for a human
    "approved",  # queued for execution
    "applying",  # running (config commit + verification, or the field technician is out)
    "applied",  # done; result holds the verification (or the repair)
    "failed",  # lab-api refused or the verification failed
    "rejected",  # a human said no
    "obsolete",  # nothing left to change, or the incident closed before a decision
]
FieldStage = Literal["dispatched", "on-site", "repaired"]


def now() -> float:
    return round(time.time(), 3)


def worst(severities: list[str] | tuple[str, ...] | set[str]) -> str:
    known = [s for s in severities if s in SEVERITY_RANK]
    return max(known, key=SEVERITY_RANK.__getitem__) if known else "OK"


def state_of(severity: str | None) -> CustomerState:
    if severity in (None, "", "OK"):
        return "ok"
    return "down" if severity == "Disaster" else "degraded"


class _Doc:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]):  # type: ignore[no-untyped-def]
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Alert(_Doc):
    """One customer alert from Zabbix (the media type ``NetSim NOC (grok-bot)``), as received and as decided."""

    id: str
    role: str  # the media's send-to: noc-engineer (steps, updates, recoveries) or noc-manager (escalation step)
    kind: str  # problem, update, recovery, escalation
    event_id: str
    customer: str
    severity: str  # Warning, Disaster, ... or OK (recovery)
    state: str  # degraded, down, ok
    tier: str | None = None
    service_id: str | None = None
    recovery_event_id: str | None = None
    event_time: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    problems: list[dict[str, Any]] = field(default_factory=list)  # the root cause, parsed
    hosts: list[str] = field(default_factory=list)  # hosts of the problems (PW ends included)
    services: list[str] = field(default_factory=list)
    subject: str | None = None
    received: float = field(default_factory=now)
    outcome: str = "pending"  # batched, opened, joined, worse, better, recovered, escalated, recorded (why)
    incident_id: str | None = None
    batch_id: str | None = None


@dataclass
class Incident(_Doc):
    id: str
    status: str = "new"
    severity: str = "OK"  # the worst current customer severity
    title: str = ""
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)
    recovered: float | None = None
    closed: float | None = None
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> tier, state, severity, event_id...
    services: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)  # root-cause problems, newest view per host+name
    assignee: str | None = None  # the role that took it
    priority: str | None = None  # P1..P4 (NOC Manager)
    priority_reason: str | None = None
    triage: dict[str, Any] | None = None  # NOC Engineer
    root_cause: dict[str, Any] | None = None  # Network Engineer
    escalations: list[dict[str, Any]] = field(default_factory=list)
    acks: list[dict[str, Any]] = field(default_factory=list)
    customer_updates: list[dict[str, Any]] = field(default_factory=list)
    closing: dict[str, Any] | None = None
    proposals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def bad_customers(self) -> list[str]:
        return [n for n, c in self.customers.items() if c.get("state") != "ok"]

    def refresh(self) -> None:
        """Severity, services and hosts from the customers."""
        self.severity = worst([c.get("severity") or "OK" for c in self.customers.values()])
        self.services = sorted({s for c in self.customers.values() for s in c.get("services") or []})
        self.hosts = sorted({h for c in self.customers.values() for h in c.get("hosts") or []})

    def counts(self) -> dict[str, int]:
        out = {"down": 0, "degraded": 0, "ok": 0}
        for c in self.customers.values():
            out[c.get("state") or "ok"] = out.get(c.get("state") or "ok", 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        """The incident without its problem list (lists, SSE)."""
        data = self.to_dict()
        data["problem_count"] = len(self.problems)
        data["counts"] = self.counts()
        data.pop("problems")
        return data


@dataclass
class Proposal(_Doc):
    """A change ticket: a lab-api dry run (or a field repair) that a human approves."""

    id: str
    incident_id: str | None
    kind: str
    host: str
    reason: str
    proposed_by: str  # the robot: NOCENG, NETENG
    role: str = ""  # its role key
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
    stage: str | None = None  # field repairs: dispatched, on-site, repaired
    progress: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)  # NOC Manager: approve/reject + reason

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
class Page(_Doc):
    """One page to a role's routine (a webhook POST)."""

    id: str
    role: str
    event: str  # incident.opened, incident.worse, incident.recovered, incident.escalated, incident.gold-down,
    # incident.zabbix-escalation, tickets.filed, question, test
    summary: str
    incident_id: str | None = None
    created: float = field(default_factory=now)
    status: str = "queued"  # queued, sent, failed, skipped
    why: str | None = None  # why it was skipped
    attempts: int = 0
    http: int | None = None
    error: str | None = None
    seconds: float | None = None
    sink: bool = False  # delivered to the test sink instead of the routine
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Activity(_Doc):
    """One thing a role did, or that happened to it; the NOC page animates these (``station``: where in the room)."""

    bot: str
    kind: str  # page, take, tool, result, propose, triage, report, escalate, ack, approve, reject, apply, verify,
    # field, alarm, recover, close, answer, question, control, error, idle
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
class RoleMember(_Doc):
    """A role's robot in the room: idle at its desk, paged (its phone rings), or working (calling tools)."""

    name: str  # NOCENG, NETENG, NOCMGR
    role: str  # noc-engineer, network-engineer, noc-manager
    title: str
    color: str
    hat: str
    status: str = "idle"  # idle, paged, working
    task: str | None = None  # the incident (or question) it works on
    activity: str = ""
    since: float = field(default_factory=now)
    last_call: float | None = None
    paged_at: float | None = None
    routine: bool = False  # a routine URL is configured


@dataclass
class ChatMessage(_Doc):
    """Ask the NOC: the operator's question (``role=user``) and the NOC Manager's answer (``role=bot``)."""

    id: str
    chat_id: str
    role: str  # user, bot, system
    text: str
    bot: str | None = None
    ts: float = field(default_factory=now)
    status: str | None = None  # questions: open, answered, timed-out
    page_id: str | None = None
    answer_to: str | None = None
