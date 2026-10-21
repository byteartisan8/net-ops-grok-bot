"""Outside agents in the NOC: the tools the Grok Bot app (or any MCP client) gets, and its visitor robot in the room.

An outside agent gets what the crew has, through the same code:
* the responders' 11 lab-api reads;
* the 3 proposal tools, with an optional ``incident_id`` so a ticket lands on the incident;
* NOC tools of its own: the bot's status, incidents, proposals, waiting for a decision, and notes on an incident.

The same rules hold as for the crew. The agent never holds a write token and has nothing that approves or applies.
A proposal is a lab-api dry run that waits for a human in the NOC room or ``botctl approve``, even under
``BOT_AUTONOMY=auto``: policy approval is for the crew only. The lab's answer key (scenario files, fault injection,
reset) is not a tool.

Every call is an activity of the visitor (``BOT_AGENT_NAME``, default ``GROK``): it comes in through the door, walks
to the station of each tool like the crew does, and leaves again when it has been quiet for a while.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from netsim_bot.brains import Job, call_tool
from netsim_bot.models import Activity
from netsim_bot.tools import PROPOSE_TOOLS, READ_TOOLS, Tool, ToolContext, ToolError, clip

if TYPE_CHECKING:
    from netsim_bot.engine import Engine

VISITOR_LINGER_S = 45.0
WAIT_MAX_S = 300.0
DECIDED = ("applied", "failed", "rejected", "obsolete")
INCIDENT_PARAM = {
    "type": "string",
    "description": "The incident this fix is for, e.g. INC-0007 (the ticket then shows on that incident)",
}


def _with_incident(tool: Tool) -> Tool:
    params = copy.deepcopy(tool.parameters)
    params["properties"]["incident_id"] = INCIDENT_PARAM
    return replace(tool, parameters=params)


AGENT_READ: list[Tool] = list(READ_TOOLS)
AGENT_PROPOSE: list[Tool] = [_with_incident(t) for t in PROPOSE_TOOLS]


@dataclass(frozen=True)
class NocTool:
    name: str
    description: str
    parameters: dict[str, Any]
    station: str
    label: Callable[[dict[str, Any]], str]
    handler: Callable[[AgentGateway, dict[str, Any]], Awaitable[Any]]
    read_only: bool = True


def _obj(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _proposal_brief(p: Any) -> dict[str, Any]:
    plan = p.plan or {}
    result = p.result or {}
    return {
        "id": p.id,
        "incident_id": p.incident_id,
        "status": p.status,
        "headline": p.headline(),
        "proposed_by": p.proposed_by,
        "reason": p.reason,
        "diff": [d.get("message") for d in plan.get("diff") or []][:20],
        "affected_services": plan.get("affected_services"),
        "decided_by": p.decided_by,
        "comment": p.comment,
        "verdict": result.get("verdict") or result.get("done"),
        "zabbix_after": result.get("zabbix_services"),
        "drift_after": result.get("drift_after"),
        "error": p.error,
    }


def _incident_brief(i: Any) -> dict[str, Any]:
    return {
        "id": i.id,
        "status": i.status,
        "severity": i.severity,
        "title": i.title,
        "services": i.services,
        "hosts": i.hosts,
        "assignee": i.assignee,
        "brain": i.brain,
        "age_s": round(time.time() - i.created),
        "resolved_after_s": round(i.resolved - i.created) if i.resolved else None,
        "proposals": i.proposals,
    }


class AgentGateway:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.last_call = 0.0

    @property
    def name(self) -> str:
        return self.engine.agent_name

    # ------------------------------------------------------------------ the tool list
    def tools(self) -> list[dict[str, Any]]:
        """MCP tool definitions (name, description, inputSchema, annotations)."""
        observe = self.engine.autonomy == "observe"
        out = []
        for t in AGENT_READ:
            out.append(self._schema(t.name, t.description, t.parameters, read_only=True))
        if not observe:
            for t in AGENT_PROPOSE:
                out.append(self._schema(t.name, t.description, t.parameters, read_only=False))
        for n in NOC_TOOLS:
            out.append(self._schema(n.name, n.description, n.parameters, read_only=n.read_only))
        return out

    @staticmethod
    def _schema(name: str, description: str, params: dict[str, Any], read_only: bool) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "inputSchema": params,
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
        }

    # ------------------------------------------------------------------ calling
    async def call(self, name: str, args: dict[str, Any] | None) -> tuple[bool, Any]:
        """``(ok, result)``; wrong tools and arguments come back as ``{"error": ...}``, like the crew's tools."""
        args = dict(args or {})
        eng = self.engine
        self.arrive(name)
        noc = NOC_BY_NAME.get(name)
        if noc is not None:
            return await self._call_noc(noc, args)
        tools = AGENT_READ + ([] if eng.autonomy == "observe" else AGENT_PROPOSE)
        tool = next((t for t in tools if t.name == name), None)
        if tool is None:
            allowed = sorted([t.name for t in tools] + list(NOC_BY_NAME))
            eng._emit(Activity(bot=self.name, kind="error", text=f"{name}: not available", tool=name, ok=False))
            return False, {"error": f"no tool {name!r}; available: {', '.join(allowed)}"}
        incident_id = None
        if tool.kind == "propose":
            incident_id = str(args.pop("incident_id", "") or "").strip() or None
            if incident_id and eng.store.incident(incident_id) is None:
                return False, {"error": f"no incident {incident_id}; list_incidents shows them"}
        ctx = ToolContext(
            lab=eng.lab,
            bot=self.name,
            hosts=eng.snapshot.hosts,
            incident_id=incident_id,
            autonomy=eng.autonomy,
            propose=lambda **kw: eng._propose(incident_id, self.name, "external", allow_auto=False, **kw),
        )
        job = Job(bot=self.name, ctx=ctx, tools=tools, emit=eng._emit)
        return await call_tool(job, name, args)

    async def _call_noc(self, tool: NocTool, args: dict[str, Any]) -> tuple[bool, Any]:
        eng = self.engine
        incident_id = args.get("incident_id") if isinstance(args.get("incident_id"), str) else None
        eng._emit(
            Activity(
                bot=self.name,
                kind="tool",
                text=tool.label(args),
                tool=tool.name,
                args={k: v for k, v in args.items() if k != "text"},
                station=tool.station,
                incident_id=incident_id,
            )
        )
        started = time.monotonic()
        try:
            ok, result = True, clip(await tool.handler(self, args))
        except ToolError as exc:
            ok, result = False, {"error": str(exc)}
        summary = result.get("summary") if ok and isinstance(result, dict) else None
        eng._emit(
            Activity(
                bot=self.name,
                kind="result",
                text=str(summary or ("ok" if ok else f"error: {result.get('error')}"))[:200],
                tool=tool.name,
                station=tool.station,
                incident_id=incident_id,
                ok=ok,
                duration_s=round(time.monotonic() - started, 2),
            )
        )
        return ok, result

    # ------------------------------------------------------------------ the visitor
    def arrive(self, doing: str) -> None:
        self.last_call = time.monotonic()
        member = self.engine.crew.get(self.name)
        if member is not None and member.status != "working":
            member.status = "working"
            member.activity = f"visiting: {doing}"
            member.since = time.time()
            self.engine._publish_crew()

    def tick(self) -> None:
        """Called by the watcher: the visitor leaves after ``VISITOR_LINGER_S`` without calls."""
        member = self.engine.crew.get(self.name)
        if member is None or member.status != "working":
            return
        if time.monotonic() - self.last_call > VISITOR_LINGER_S:
            member.status, member.activity, member.since = "away", "", time.time()
            self.engine._publish_crew()


# ---------------------------------------------------------------------------------------------- NOC tools
def _need_id(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolError(f"'{key}' is required")
    return value


async def _status(gw: AgentGateway, a: dict[str, Any]) -> Any:
    eng = gw.engine
    active = eng.store.incidents(limit=50, active_only=True)
    pending = eng.store.proposals(status="pending", limit=50)
    eff = eng.effective()
    return {
        "summary": f"{len(active)} active incident(s), {len(pending)} change(s) waiting for approval",
        "lab_ok": eng.snapshot.ok,
        "lab_error": eng.snapshot.error,
        "open_problems": len(eng.snapshot.problems),
        "crew_brain": eff["brain"],
        "autonomy": eff["autonomy"],
        "maintenance_mode": eff["paused"],
        "active_incidents": [_incident_brief(i) for i in active],
        "pending_proposals": [_proposal_brief(p) for p in pending],
        "noc_room": eng.settings.public_url,
        "links": eng.settings.public()["links"],
    }


async def _incidents(gw: AgentGateway, a: dict[str, Any]) -> Any:
    active_only = a.get("active_only", True) is not False
    limit = min(max(int(a.get("limit") or 20), 1), 100)
    rows = gw.engine.store.incidents(limit=limit, active_only=active_only)
    return {"summary": f"{len(rows)} incident(s)", "incidents": [_incident_brief(i) for i in rows]}


async def _incident(gw: AgentGateway, a: dict[str, Any]) -> Any:
    eng = gw.engine
    iid = _need_id(a, "incident_id")
    inc = eng.tracker.incidents.get(iid) or eng.store.incident(iid)
    if inc is None:
        raise ToolError(f"no incident {iid}")
    problems = [
        {k: p.get(k) for k in ("severity", "name", "host", "service", "pw_role", "check", "cleared")}
        for p in inc.problems.values()
    ]
    timeline = [
        {"bot": x.bot, "kind": x.kind, "text": x.text}
        for x in eng.store.activities(incident_id=iid, limit=400)
        if x.kind in ("alarm", "assign", "propose", "report", "approve", "reject", "apply", "verify", "resolve", "say")
    ][-40:]
    return {
        **_incident_brief(inc),
        "summary": f"{inc.id} {inc.status}: {inc.title}",
        "problems": problems,
        "report": inc.report,
        "impact": inc.impact,
        "notes": inc.notes[-20:],
        "proposal_details": [_proposal_brief(p) for p in eng.store.proposals(incident_id=iid)],
        "timeline": timeline,
    }


async def _proposal(gw: AgentGateway, a: dict[str, Any]) -> Any:
    p = gw.engine.store.proposal(_need_id(a, "proposal_id"))
    if p is None:
        raise ToolError(f"no proposal {a.get('proposal_id')}")
    return {**_proposal_brief(p), "summary": f"{p.id} {p.status}: {p.headline()}"}


async def _wait(gw: AgentGateway, a: dict[str, Any]) -> Any:
    pid = _need_id(a, "proposal_id")
    timeout = min(max(float(a.get("timeout_s") or 60), 1.0), WAIT_MAX_S)
    deadline = time.monotonic() + timeout
    store = gw.engine.store
    p = store.proposal(pid)
    if p is None:
        raise ToolError(f"no proposal {pid}")
    while p.status not in DECIDED and time.monotonic() < deadline:
        gw.arrive("waiting")  # still here: keeps the visitor in the room while it waits
        await asyncio.sleep(1.0)
        p = store.proposal(pid) or p
    done = p.status in DECIDED
    return {
        **_proposal_brief(p),
        "decided": done,
        "summary": f"{p.id} {p.status}" + ("" if done else f" (still {p.status} after {timeout:g} s)"),
    }


async def _note(gw: AgentGateway, a: dict[str, Any]) -> Any:
    eng = gw.engine
    iid = _need_id(a, "incident_id")
    text = str(a.get("text") or "").strip()
    if not text:
        raise ToolError("'text' is required")
    inc = eng.tracker.incidents.get(iid) or eng.store.incident(iid)
    if inc is None:
        raise ToolError(f"no incident {iid}")
    inc.notes.append(f"{gw.name}: {text[:2000]}")
    eng._save(inc)
    eng._emit(Activity(bot=gw.name, kind="say", text=text.splitlines()[0][:200], incident_id=iid, station="board"))
    return {"ok": True, "summary": f"note added to {iid}"}


NOC_TOOLS: list[NocTool] = [
    NocTool(
        "get_noc_status",
        "The NOC bot right now: lab reachable, open Zabbix problems, the crew's brain and autonomy, maintenance "
        "mode, active incidents and the change tickets waiting for a human. Start here.",
        _obj(),
        "board",
        lambda a: "NOC status",
        _status,
    ),
    NocTool(
        "list_incidents",
        "Incidents the watcher opened from Zabbix problems (one per fault: problems joined by service and time). "
        "Status: open, investigating, diagnosed, awaiting-approval, remediating, monitoring, resolved.",
        _obj(
            {
                "active_only": {"type": "boolean", "description": "Only unresolved ones (default true)"},
                "limit": {"type": "integer", "description": "At most this many (default 20)"},
            }
        ),
        "board",
        lambda a: "incident board",
        _incidents,
    ),
    NocTool(
        "get_incident",
        "One incident: its Zabbix problems, the crew's report (root cause, impact by tier, evidence), change tickets "
        "with their diffs and verdicts, notes and a short timeline.",
        _obj({"incident_id": {"type": "string", "description": "e.g. INC-0007"}}, ["incident_id"]),
        "board",
        lambda a: f"{a.get('incident_id', '?')} on the board",
        _incident,
    ),
    NocTool(
        "get_proposal",
        "One change ticket: status (pending, approved, applying, applied, failed, rejected, obsolete), the diff, "
        "who decided, and the verification verdict after it was applied.",
        _obj({"proposal_id": {"type": "string", "description": "e.g. CHG-0012"}}, ["proposal_id"]),
        "operator",
        lambda a: f"ticket {a.get('proposal_id', '?')}",
        _proposal,
    ),
    NocTool(
        "wait_for_proposal",
        "Wait until a human decided a change ticket and, if approved, it was applied and verified (or the timeout "
        "passes). Use it after proposing, then confirm with get_open_problems / list_services.",
        _obj(
            {
                "proposal_id": {"type": "string", "description": "e.g. CHG-0012"},
                "timeout_s": {"type": "number", "description": "Seconds to wait (default 60, at most 300)"},
            },
            ["proposal_id"],
        ),
        "operator",
        lambda a: f"waiting on {a.get('proposal_id', '?')}",
        _wait,
    ),
    NocTool(
        "add_incident_note",
        "Leave a note on an incident (your finding, what you told customers, a hand-over line). It shows on the "
        "incident board in the NOC room and in get_incident.",
        _obj(
            {
                "incident_id": {"type": "string", "description": "e.g. INC-0007"},
                "text": {"type": "string", "description": "The note (first line is shown in the room)"},
            },
            ["incident_id", "text"],
        ),
        "board",
        lambda a: f"note on {a.get('incident_id', '?')}",
        _note,
        read_only=False,
    ),
]
NOC_BY_NAME: dict[str, NocTool] = {t.name: t for t in NOC_TOOLS}
