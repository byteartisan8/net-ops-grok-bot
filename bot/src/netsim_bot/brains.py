"""The responders' brains: Grok (xAI Responses API with function calling) and the deterministic rules triage.

Both drive the same tools through :func:`call_tool`, which records every call as an activity (station, target,
result), so the NOC room animates a rules investigation exactly like a Grok one. The rules brain is what runs
without an ``XAI_API_KEY`` (and in the e2e tests, where a deterministic outcome is needed); Grok is the real bot.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from netsim_bot import prompts
from netsim_bot.grok import GrokClient, GrokError, Usage, tool_output
from netsim_bot.models import Activity
from netsim_bot.tools import ALL_TOOLS, Tool, ToolContext, run_tool

TIER_ORDER = {"gold": 0, "silver": 1, "bronze": 2}


@dataclass
class Job:
    """One investigation or one chat answer, as a brain sees it."""

    bot: str
    ctx: ToolContext
    tools: list[Tool]
    emit: Callable[[Activity], None]
    on_usage: Callable[[Usage, str], None] = lambda _u, _m: None
    max_steps: int = 24
    updates: Callable[[], str | None] = lambda: None
    calls: list[tuple[str, dict[str, Any], bool]] = field(default_factory=list)

    def say(self, kind: str, text: str, **kw: Any) -> None:
        self.emit(
            Activity(
                bot=self.bot,
                kind=kind,
                text=text,
                incident_id=self.ctx.incident_id,
                chat_id=self.ctx.chat_id,
                **kw,
            )
        )


def summarize(name: str, ok: bool, result: Any) -> str:
    """One line for the activity log and the speech bubble."""
    if not ok:
        err = result.get("error") if isinstance(result, dict) else result
        detail = result.get("detail") if isinstance(result, dict) else None
        return f"error: {err}{f' ({str(detail)[:120]})' if detail else ''}"
    r = result if isinstance(result, dict) else {}
    try:
        if name == "get_open_problems":
            sev: dict[str, int] = {}
            for p in r.get("problems", []):
                sev[p["severity"]] = sev.get(p["severity"], 0) + 1
            shown = ", ".join(f"{n} {s}" for s, n in sorted(sev.items(), key=lambda kv: -kv[1]))
            return f"{r.get('count', 0)} open problem(s){': ' + shown if shown else ''}"
        if name == "list_services":
            bad = [s for s in r.get("services", []) if s.get("zabbix") not in (None, "OK")]
            return f"{len(r.get('services', []))} services, {len(bad)} not OK" + (
                f": {', '.join(f'{s["service"]} {s["zabbix"]}' for s in bad[:4])}" if bad else ""
            )
        if name == "get_drift":
            f = r.get("findings", [])
            return f"{len(f)} finding(s)" + (f": {f[0].get('message', '')}" if f else ", devices match NetBox")
        if name == "get_impact":
            tiers = r.get("by_tier") or {}
            shown = "; ".join(
                f"{t}: " + ", ".join(f"{n} {k}" for k, n in v.items() if n) for t, v in tiers.items() if any(v.values())
            )
            return f"losing {r.get('host')}: {shown or 'no service hurt'}"
        if name == "get_device_state":
            pws = r.get("pseudowires", [])
            down = [p for p in pws if p.get("oper") != "up"]
            uplink = (r.get("uplink") or {}).get("oper")
            return f"{r.get('host')}: uplink {uplink}, {len(pws) - len(down)}/{len(pws)} PWs up" + (
                f" ({down[0].get('service')} {down[0].get('role')}: {down[0].get('reason')})" if down else ""
            )
        if name == "list_devices":
            off = [h for h, d in r.items() if d.get("powered_off")]
            bad = [h for h, d in r.items() if d.get("uplink") != "up" or d.get("faults")]
            return (
                f"{len(r)} devices"
                + (f", powered off: {', '.join(off)}" if off else "")
                + (f", faults: {', '.join(bad)}" if bad else "")
            )
        if name == "get_events":
            return f"{len(r.get('events', []))} event(s)"
        if name.startswith("propose_"):
            if r.get("proposal_id"):
                return f"{r['proposal_id']} {r.get('status', 'pending')}: {r.get('headline', '')}"
            return r.get("note") or "no proposal"
        if name == "finish_investigation":
            return "report filed"
    except (KeyError, TypeError, AttributeError):
        pass
    return "ok"


async def call_tool(job: Job, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    tool = ALL_TOOLS.get(name)
    allowed = {t.name for t in job.tools}
    if tool is None or name not in allowed:
        result = {"error": f"no tool {name!r}; available: {', '.join(sorted(allowed))}"}
        job.say("error", f"{name}: not available", tool=name, ok=False)
        return False, result
    shown_args = {k: v for k, v in args.items() if k != "reason"}
    job.say("tool", tool.label(args), tool=name, args=shown_args, station=tool.station, target=tool.target(args))
    started = time.monotonic()
    ok, result = await run_tool(tool, job.ctx, args)
    job.calls.append((name, args, ok))
    job.say(
        "result",
        summarize(name, ok, result),
        tool=name,
        station=tool.station,
        target=tool.target(args),
        ok=ok,
        duration_s=round(time.monotonic() - started, 2),
    )
    return ok, result


def fallback_report(text: str, ctx: ToolContext, why: str) -> dict[str, Any]:
    return {
        "title": (text.splitlines()[0][:90] if text else "investigation incomplete"),
        "summary": text[:800] if text else why,
        "root_cause": "not determined",
        "category": "unknown",
        "affected": [],
        "impact_by_tier": {},
        "evidence": [],
        "recommendation": why,
        "confidence": "low",
        "proposals": list(ctx.proposals),
    }


# ---------------------------------------------------------------------------------------------- Grok
class GrokBrain:
    def __init__(self, client: GrokClient) -> None:
        self.client = client
        self.name = client.model

    async def investigate(self, job: Job, brief: str) -> dict[str, Any]:
        schemas = [t.schema() for t in job.tools]
        system = prompts.incident_system(job.bot, job.ctx.incident_id or "?", job.ctx.autonomy)
        pending: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
        previous: str | None = None
        last_text = ""
        nudged = False
        for step in range(job.max_steps):
            note = job.updates() if previous else None
            if note:
                pending.append({"role": "user", "content": note})
                job.say("think", "the watcher has news; taking it into account")
            final = step == job.max_steps - 1
            tools = schemas
            if final:
                # last step: only the report tool is offered, and a call is required
                pending.append({"role": "user", "content": "Step limit reached: call finish_investigation now."})
                tools = [t.schema() for t in job.tools if t.name == "finish_investigation"] or schemas
            job.say("think", "thinking" if step else "reading the incident")
            resp = await self.client.respond(
                input=pending,
                tools=tools,
                previous_response_id=previous,
                tool_choice="required" if final else None,
            )
            job.on_usage(resp.usage, self.client.model)
            if resp.reasoning:
                job.say("think", resp.reasoning[:300])
            if resp.text:
                last_text = resp.text
                job.say("say", resp.text[:400])
            if not resp.calls:
                if job.ctx.report:
                    break
                if nudged:
                    break
                nudged = True
                pending = [{"role": "user", "content": "File your report now with finish_investigation."}]
                previous = resp.id
                continue
            outputs = []
            for call in resp.calls:
                _ok, result = await call_tool(job, call.name, call.args())
                outputs.append(tool_output(call.call_id, result))
            if job.ctx.report:
                break
            pending = outputs
            previous = resp.id
        return job.ctx.report or fallback_report(last_text, job.ctx, "Grok ended without a structured report")

    async def chat(self, job: Job, message: str, previous: str | None) -> tuple[str, str | None]:
        schemas = [t.schema() for t in job.tools]
        pending: list[dict[str, Any]] = []
        if previous is None:
            pending.append({"role": "system", "content": prompts.chat_system(job.bot)})
        pending.append({"role": "user", "content": message})
        answer = ""
        for _ in range(job.max_steps):
            resp = await self.client.respond(input=pending, tools=schemas, previous_response_id=previous)
            job.on_usage(resp.usage, self.client.model)
            previous = resp.id
            if resp.text:
                answer = resp.text
                if resp.calls:
                    job.say("say", resp.text[:400])
            if not resp.calls:
                break
            outputs = []
            for call in resp.calls:
                _ok, result = await call_tool(job, call.name, call.args())
                outputs.append(tool_output(call.call_id, result))
            pending = outputs
        return answer or "(no answer)", previous


# ---------------------------------------------------------------------------------------------- rules
AC_RE = re.compile(r"attachment circuit (\S+) down on (\S+)")
SVC_PREFIX_RE = re.compile(r"^(SVC-[0-9]{4}): (.*)$")
PW_DOWN_RE = re.compile(r"^(primary|backup)( and (primary|backup))? PWs? down: ")


def _reason(messages: list[str]) -> str:
    unique = list(dict.fromkeys(messages))
    more = f" (+{len(unique) - 1} more finding{'s' if len(unique) > 2 else ''})" if len(unique) > 1 else ""
    return (unique[0] + more)[:400] if unique else "drift check finding"


def group_causes(findings: list[dict[str, Any]]) -> list[str]:
    """One line per distinct cause with the services it hits ('hub-b powered off ... (7 services: SVC-0001, ...)')."""
    grouped: dict[str, list[str]] = {}
    for f in findings:
        msg = str(f.get("message", ""))
        m = SVC_PREFIX_RE.match(msg)
        body = m.group(2) if m else msg
        body = re.sub(r"; service (degraded|down)$", "", body)
        body = PW_DOWN_RE.sub("", body)
        service = f.get("service") or (m.group(1) if m else None)
        grouped.setdefault(body, [])
        if service and service not in grouped[body]:
            grouped[body].append(service)
    out = []
    for body, services in grouped.items():
        if not services:
            out.append(body)
            continue
        shown = ", ".join(services[:6]) + ("…" if len(services) > 6 else "")
        out.append(f"{body} ({len(services)} service{'s' if len(services) != 1 else ''}: {shown})")
    return out


class RulesBrain:
    """A fixed triage playbook: problems -> drift -> device state of the culprits -> impact -> proposals -> report.

    It is a baseline (and the e2e harness), not the bot: it can only act on what the drift checker classifies, and it
    writes template reports. Grok reads the same data and has to reason about it.
    """

    name = "rules"

    async def investigate(self, job: Job, brief: str) -> dict[str, Any]:
        ctx = job.ctx
        job.say("say", "Checking what Zabbix sees")
        _, problems = await call_tool(job, "get_open_problems", {})
        services_in_incident = sorted(
            {p.get("service") for p in (problems or {}).get("problems", []) if p.get("service")}
        )
        job.say("say", "Comparing NetBox with every device")
        ok, drift = await call_tool(job, "get_drift", {})
        findings = (drift or {}).get("findings", []) if ok else []
        relevant = [f for f in findings if f.get("service") in services_in_incident or not f.get("service")]
        if not relevant:
            relevant = findings
        config = [f for f in relevant if f.get("class") in ("config-drift", "intent-only")]
        faults = [f for f in relevant if f.get("class") == "fault"]

        config_hosts: dict[str, list[str]] = {}
        for f in config:
            for host in f.get("hosts") or []:
                config_hosts.setdefault(host, []).append(f.get("message", ""))
        repairs: dict[tuple[str, str, str | None, str | None], list[str]] = {}
        for f in faults:
            kind, hosts, msg = f.get("kind"), f.get("hosts") or [], f.get("message", "")
            if kind == "device-down" and hosts:
                repairs.setdefault(("device", hosts[0], None, None), []).append(msg)
            elif kind == "uplink-down" and hosts:
                repairs.setdefault(("uplink", hosts[0], None, None), []).append(msg)
            elif kind == "core-cut" and len(hosts) >= 2:
                repairs.setdefault(("cut", hosts[0], hosts[1], None), []).append(msg)
            elif kind == "ac-down":
                m = AC_RE.search(msg)
                if m:
                    repairs.setdefault(("ac", m.group(2), None, m.group(1)), []).append(msg)

        culprits = list(dict.fromkeys([*config_hosts, *(k[1] for k in repairs)]))
        devices = None
        if culprits:
            for host in culprits[:3]:
                if any(k[0] == "device" and k[1] == host for k in repairs):
                    continue  # a powered-off device has no state to read
                job.say("say", f"Looking at {host} itself")
                await call_tool(job, "get_device_state", {"host": host})
        else:
            job.say("say", "No drift: checking the devices")
            _, devices = await call_tool(job, "list_devices", {})

        impacts: dict[str, dict[str, Any]] = {}
        for host in [k[1] for k in repairs if k[0] in ("device", "uplink")][:2]:
            job.say("say", f"Blast radius of {host}, by tier")
            ok, imp = await call_tool(job, "get_impact", {"host": host})
            if ok:
                impacts[host] = imp

        job.say("say", "Who is hurt, by SLA tier")
        _, services = await call_tool(job, "list_services", {})
        affected = []
        for s in (services or {}).get("services", []):
            if s.get("zabbix") in ("Warning", "Disaster", "High", "Average") or s.get("live") != "up":
                state = "down" if s.get("zabbix") == "Disaster" or s.get("live") == "down" else "degraded"
                affected.append(
                    {"service": s["service"], "customer": s.get("customer"), "tier": s.get("tier"), "state": state}
                )
        affected.sort(key=lambda a: (TIER_ORDER.get(a.get("tier") or "", 9), a["state"] != "down", a["service"]))
        by_tier: dict[str, dict[str, int]] = {}
        for a in affected:
            t = by_tier.setdefault(a.get("tier") or "unknown", {"down": 0, "degraded": 0})
            t[a["state"]] = t.get(a["state"], 0) + 1

        if ctx.autonomy != "observe" and ctx.propose is not None:
            for (kind, host, peer, iface), msgs in repairs.items():
                args: dict[str, Any] = {"kind": kind, "host": host, "reason": _reason(msgs)}
                if peer:
                    args["peer"] = peer
                if iface:
                    args["interface"] = iface
                job.say("say", f"Field ticket for {host}")
                await call_tool(job, "propose_field_repair", args)
            for host, msgs in config_hosts.items():
                job.say("say", f"Change ticket: NetBox intent for {host}")
                await call_tool(job, "propose_apply_intent", {"host": host, "reason": _reason(msgs)})

        classes = {f.get("class") for f in relevant}
        if not relevant:
            category = "no-fault" if not (problems or {}).get("count") else "unknown"
        elif classes == {"fault"}:
            category = "physical-fault"
        elif classes <= {"config-drift"}:
            category = "config-drift"
        elif classes <= {"intent-only"}:
            category = "intent-only"
        else:
            category = "mixed"
        causes = group_causes(relevant)
        if culprits:
            title = f"{', '.join(culprits[:3])}: {category.replace('-', ' ')}"
        elif devices and any(d.get("powered_off") for d in devices.values()):
            title = "device powered off"
        else:
            title = "no cause found in drift"
        worst = affected[0] if affected else None
        impact_line = (
            "; ".join(f"{t}: {v['down']} down, {v['degraded']} degraded" for t, v in by_tier.items())
            or "no service affected"
        )
        summary = (
            f"{len(causes)} cause(s) from the drift check{': ' + causes[0] if causes else ''}. "
            f"Impact {impact_line}."
            + (f" Worst: {worst['service']} ({worst.get('tier')}) {worst['state']}." if worst else "")
        )
        recommendation = (
            "Approve the proposals: NetBox intent for "
            + (", ".join(config_hosts) or "-")
            + "; field repairs: "
            + (", ".join(f"{k[0]} {k[1]}" for k in repairs) or "-")
            if (config_hosts or repairs) and ctx.autonomy != "observe"
            else ("Push NetBox intent / repair the faults listed." if relevant else "Watch: nothing to fix found.")
        )
        evidence = causes[:8]
        for host, imp in impacts.items():
            evidence.append(f"impact of {host}: by tier {json.dumps(imp.get('by_tier'))}")
        _, _ = await call_tool(
            job,
            "finish_investigation",
            {
                "title": title,
                "summary": summary,
                "root_cause": "; ".join(causes) or "not found by the drift check",
                "category": category,
                "affected": affected,
                "impact_by_tier": by_tier,
                "evidence": evidence,
                "recommendation": recommendation,
                "confidence": "medium" if relevant else "low",
            },
        )
        return ctx.report or fallback_report(summary, ctx, "rules brain could not file a report")

    async def chat(self, job: Job, message: str, previous: str | None) -> tuple[str, str | None]:
        text = message.lower()
        hosts = [h for h in job.ctx.hosts if h in text]
        what_if = ("impact", "maintenance", "lose", "lost", "loss", "fail", "down", "what if", "what happens", "outage")
        if hosts and any(w in text for w in what_if):
            ok, imp = await call_tool(job, "get_impact", {"host": hosts[0]})
            if not ok:
                return summarize("get_impact", ok, imp), None
            tiers = imp.get("by_tier") or {}
            per_tier = "; ".join(
                f"{t}: " + ", ".join(f"{n} {k}" for k, n in sorted(v.items()) if n)
                for t, v in sorted(tiers.items(), key=lambda kv: TIER_ORDER.get(kv[0], 9))
                if any(v.values())
            )
            lines = [f"Losing {hosts[0]} now: {per_tier or 'no service affected'}"]
            for s in imp.get("affected", [])[:8]:
                lines.append(f"- {s['service']} ({s.get('tier')}): {s.get('live_impact')} - {s.get('explanation')}")
            return "\n".join(lines), None
        if "drift" in text or "netbox" in text:
            ok, d = await call_tool(job, "get_drift", {})
            f = (d or {}).get("findings", [])
            return (
                "No drift: every device matches NetBox." if not f else "\n".join(x["message"] for x in f[:10])
            ), None
        if any(w in text for w in ("problem", "alarm", "zabbix", "incident")):
            ok, p = await call_tool(job, "get_open_problems", {})
            rows = (p or {}).get("problems", [])
            if not rows:
                return "Zabbix has no open problem on the lab hosts.", None
            return "\n".join(f"- {r['severity']}: {r['name']}" for r in rows[:12]), None
        if any(w in text for w in ("status", "health", "service", "ok", "green", "how")):
            ok, s = await call_tool(job, "list_services", {})
            rows = (s or {}).get("services", [])
            bad = [r for r in rows if r.get("zabbix") != "OK"]
            if not bad:
                return f"All {len(rows)} services are OK in Zabbix and up on the devices.", None
            return "\n".join(
                f"- {r['service']} ({r.get('tier')}): {r.get('zabbix')}, live {r.get('live')}" for r in bad
            ), None
        return (
            "I'm the rules autopilot (no XAI_API_KEY, or BOT_BRAIN=rules), so I only understand: status, problems, "
            "drift, and impact/maintenance of a host (e.g. 'impact of hub-c'). Add an xAI API key to .env for Grok.",
            None,
        )


__all__ = ["GrokBrain", "GrokError", "Job", "RulesBrain", "call_tool", "summarize"]
