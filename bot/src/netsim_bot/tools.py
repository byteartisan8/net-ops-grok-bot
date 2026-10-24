"""The lab tools every NOC role has: lab-api reads (compacted for an agent), the direct reads (Zabbix, NetBox, a
device's RESTCONF), and the proposals (dry runs the NOC Manager reviews and a human approves). :mod:`netsim_bot.roles`
decides which role gets which.

Every tool has a *station*: where in the NOC room the role's robot walks to use it (Zabbix console, wall screen,
NetBox cabinet, server rack, its desk, the whiteboard, the printer and the operator's desk). The NOC page animates
exactly the calls a role makes, so the room shows the real work, not a canned one.

A role never holds a write token and has no tool that applies anything: a proposal is a lab-api dry run whose
``plan_id`` the engine keeps; only a human's approval lets the engine confirm it. Scenario definitions, fault
injection and reset are deliberately not tools: the roles diagnose from what a NOC can see (Zabbix, NetBox, the
devices), not from the lab's answer key.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from netsim_bot.labapi import LabApi, LabApiError

MAX_RESULT_CHARS = 32_000
ZABBIX_READS = [
    "problem.get",
    "event.get",
    "trigger.get",
    "item.get",
    "history.get",
    "trend.get",
    "host.get",
    "hostgroup.get",
    "service.get",
    "sla.get",
    "sla.getsli",
    "alert.get",
    "maintenance.get",
]
SERVICE_RE = re.compile(r"^SVC-[0-9]{4}$")
SCOPES = ["l2vpn-pw", "l2vpn-unexpected", "l2vpn-trap", "interface", "availability"]
FAULT_KINDS = ["uplink", "ac", "cut", "device"]


class ToolError(Exception):
    """A tool call the model got wrong (bad host, missing argument): returned to it as an error, not raised."""


@dataclass
class ToolContext:
    lab: LabApi
    bot: str
    hosts: list[str]
    incident_id: str | None = None
    chat_id: str | None = None
    autonomy: str = "propose"
    propose: Callable[..., Awaitable[dict[str, Any]]] | None = None
    proposals: list[str] = field(default_factory=list)

    def host(self, args: dict[str, Any], key: str = "host") -> str:
        value = str(args.get(key) or "").strip()
        if not value:
            raise ToolError(f"'{key}' is required")
        if self.hosts and value not in self.hosts:
            raise ToolError(f"unknown device {value!r}; devices: {', '.join(self.hosts)}")
        return value


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    station: str
    kind: str  # read, propose
    label: Callable[[dict[str, Any]], str]
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]

    def target(self, args: dict[str, Any]) -> str | None:
        """What the room highlights (a rack unit, a service tile)."""
        return args.get("host") or args.get("service_id") or args.get("service")


def _obj(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


HOST_PARAM = {"type": "string", "description": "Device (PE) name, e.g. hub-a, spoke-5 (list them with get_topology)"}
REASON_PARAM = {"type": "string", "description": "Why: the root cause this fixes, in one or two sentences"}


# ---------------------------------------------------------------------------------------------- result shaping
def _end(e: dict[str, Any] | None) -> str:
    if not e:
        return "?"
    parts = [f"{e.get('oper')}/{e.get('reason')}"]
    if e.get("peer"):
        parts.append(f"peer {e['peer']}")
    if e.get("vc_id") is not None:
        parts.append(f"vc {e['vc_id']}")
    return " ".join(parts)


def compact_service(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": s.get("service"),
        "customer": s.get("customer"),
        "tier": s.get("sla_tier"),
        "rules": s.get("status_rules"),
        "spoke": s.get("spoke"),
        "live": s.get("live"),
        "zabbix": s.get("zabbix"),
        "in_netbox": s.get("in_netbox"),
        "pws": [
            {
                "role": p.get("role"),
                "hub": p.get("hub"),
                "vc_id": p.get("vc_id"),
                "up": p.get("up"),
                "spoke_end": f"{(p.get('spoke_end') or {}).get('host')}: {_end(p.get('spoke_end'))}",
                "hub_end": f"{(p.get('hub_end') or {}).get('host')}: {_end(p.get('hub_end'))}",
                "zabbix_leaf": p.get("zabbix_leaf"),
            }
            for p in s.get("pseudowires") or []
        ],
    }


def compact_problem(p: dict[str, Any], t: float | None = None) -> dict[str, Any]:
    t = time.time() if t is None else t
    return {
        "eventid": p.get("eventid"),
        "severity": p.get("severity"),
        "name": p.get("name"),
        "host": p.get("host"),
        "service": p.get("service"),
        "pw_role": p.get("pw_role"),
        "check": p.get("check"),
        "scope": p.get("scope"),
        "opdata": p.get("opdata"),
        "age_s": round(t - float(p.get("clock") or t)),
    }


def compact_devices(devices: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for host, d in (devices or {}).items():
        faults = d.get("faults") or {}
        out[host] = {
            "reachable": d.get("reachable"),
            "powered_off": d.get("stopped"),
            "uplink": d.get("uplink"),
            "faults": {k: v for k, v in faults.items() if v},
            "pws_up": f"{d.get('pws_up')}/{d.get('pws')}",
        }
    return out


def shape_problems(raw: dict[str, Any], include_traps: bool = False) -> dict[str, Any]:
    t = time.time()
    problems = [p for p in raw.get("problems") or [] if include_traps or p.get("scope") != "l2vpn-trap"]
    return {
        "count": len(problems),
        "problems": [compact_problem(p, t) for p in sorted(problems, key=lambda p: float(p.get("clock") or 0))],
        "correlation": [
            {
                "service": g.get("service"),
                "summary": g.get("summary"),
                "severity": g.get("severity"),
                "hosts": g.get("hosts"),
                "roles": g.get("roles"),
                "checks": g.get("checks"),
            }
            for g in raw.get("groups") or []
        ],
    }


def shape_services(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "zabbix_root": raw.get("zabbix_root"),
        "services": [compact_service(s) for s in raw.get("services") or []],
        "devices": compact_devices(raw.get("devices") or {}),
        "errors": raw.get("errors") or {},
    }


def shape_topology(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": raw.get("source"),
        "regions": raw.get("regions"),
        "customers": raw.get("customers"),
        "devices": [
            {k: d.get(k) for k in ("hostname", "role", "site", "region", "loopback", "running")}
            for d in raw.get("devices") or []
        ],
        "services": [
            {
                "service": s.get("service"),
                "customer": s.get("customer"),
                "tier": s.get("sla_tier"),
                "rules": s.get("status_rules"),
                "spoke": s.get("spoke"),
                "pws": [
                    {k: p.get(k) for k in ("role", "hub", "vc_id", "spoke_ac", "hub_ac")}
                    for p in s.get("pseudowires") or []
                ],
            }
            for s in raw.get("services") or []
        ],
        "errors": raw.get("errors") or {},
    }


def shape_state(raw: dict[str, Any]) -> dict[str, Any]:
    pws = []
    for p in raw.get("pseudowires") or []:
        remote = p.get("remote") or {}
        pws.append(
            {
                "service": p.get("service_id"),
                "role": p.get("role"),
                "ac": p.get("ac"),
                "peer": p.get("peer"),
                "peer_host": p.get("peer_hostname"),
                "vc_id": p.get("vc_id"),
                "mtu": p.get("local_mtu"),
                "admin": p.get("admin"),
                "oper": p.get("oper"),
                "reason": p.get("reason"),
                "since": p.get("since"),
                "remote": {k: remote.get(k) for k in ("hostname", "role", "mtu", "service_id")} if remote else None,
            }
        )
    return {
        "host": raw.get("hostname"),
        "loopback": raw.get("loopback"),
        "uptime_s": raw.get("uptime_s"),
        "uplink": raw.get("uplink"),
        "faults": raw.get("faults"),
        "pseudowires": pws,
    }


def shape_drift(raw: dict[str, Any]) -> dict[str, Any]:
    devices = {}
    for host, d in (raw.get("devices") or {}).items():
        entry: dict[str, Any] = {"status": d.get("status"), "baseline": d.get("baseline")}
        if d.get("error"):
            entry["error"] = d["error"]
        if d.get("diffs"):
            entry["diffs"] = [
                {k: x.get(k) for k in ("path", "class", "message") if x.get(k) is not None} for x in d["diffs"]
            ]
        devices[host] = entry
    return {
        "drift": raw.get("drift"),
        "incomplete": raw.get("incomplete"),
        "summary": raw.get("summary"),
        "findings": raw.get("findings") or [],
        "devices": devices,
        "faults": raw.get("faults") or [],
        "powered_off": raw.get("stopped") or [],
        "intent_problems": raw.get("intent_problems") or [],
    }


def shape_impact(raw: dict[str, Any]) -> dict[str, Any]:
    affected, unaffected = [], []
    for s in raw.get("services") or []:
        if s.get("live_impact") == "unaffected" and s.get("intent_impact") == "unaffected":
            unaffected.append(s.get("service"))
            continue
        affected.append(
            {
                "service": s.get("service"),
                "customer": s.get("customer"),
                "tier": s.get("sla_tier"),
                "rules": s.get("status_rules"),
                "intent_impact": s.get("intent_impact"),
                "live_impact": s.get("live_impact"),
                "lost_roles": s.get("lost_roles"),
                "surviving": [
                    {"role": x.get("role"), "path": x.get("path"), "up": x.get("up")} for x in s.get("surviving") or []
                ],
                "zabbix_now": s.get("zabbix_now"),
                "explanation": s.get("explanation"),
            }
        )
    return {
        "host": raw.get("host"),
        "summary": raw.get("summary"),
        "by_tier": raw.get("by_tier"),
        "worse_than_intent": raw.get("worse_than_intent"),
        "affected": affected,
        "unaffected": unaffected,
    }


def shape_events(raw: dict[str, Any], include_traps: bool = False) -> dict[str, Any]:
    out = []
    for e in raw.get("events") or []:
        kind = e.get("type")
        if kind == "trap-sent" and not include_traps:
            continue
        item: dict[str, Any] = {"time": e.get("time"), "host": e.get("host"), "type": kind}
        if kind in ("oper-change", "reason-change", "pw-added", "pw-removed"):
            for k in ("service_id", "role", "old", "new", "old_reason", "reason", "oper"):
                if e.get(k) is not None:
                    item[k] = e[k]
        elif kind == "config-change":
            item["changed_by"] = e.get("changed_by")
            item["diff"] = [d.get("message") for d in e.get("diff") or []][:12]
        elif kind in ("neighbor-up", "neighbor-down"):
            item["neighbor"] = e.get("neighbor")
        elif kind == "trap-sent":
            item["trap"] = e.get("name")
        else:
            item.update({k: v for k, v in e.items() if k not in ("id", "ts", "boot_id", "time", "host", "type")})
        out.append(item)
    return {"events": out, "errors": raw.get("errors") or {}}


def clip(result: Any) -> Any:
    """Results stay below ``MAX_RESULT_CHARS`` (a note tells the model it was cut and how to narrow the query)."""
    text = json.dumps(result, default=str, separators=(",", ":"))
    if len(text) <= MAX_RESULT_CHARS:
        return result
    return {
        "truncated": True,
        "note": f"result was {len(text)} characters; shown the first {MAX_RESULT_CHARS}. Narrow the query.",
        "partial": text[:MAX_RESULT_CHARS],
    }


# ---------------------------------------------------------------------------------------------- handlers
async def _problems(ctx: ToolContext, a: dict[str, Any]) -> Any:
    scope = a.get("scope")
    if scope and scope not in SCOPES:
        raise ToolError(f"scope must be one of {', '.join(SCOPES)}")
    service = a.get("service")
    if service and not SERVICE_RE.match(service):
        raise ToolError("service must look like SVC-0001")
    return shape_problems(await ctx.lab.problems(service=service, scope=scope), include_traps=scope == "l2vpn-trap")


async def _services(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return shape_services(await ctx.lab.services())


async def _service(ctx: ToolContext, a: dict[str, Any]) -> Any:
    sid = str(a.get("service_id") or "").strip()
    if not SERVICE_RE.match(sid):
        raise ToolError("service_id must look like SVC-0001")
    raw = await ctx.lab.get(f"/services/{sid}")
    zabbix = raw.get("zabbix") or {}
    intent = raw.get("intent") or {}
    return {
        "service": sid,
        "intent": {
            "customer": intent.get("customer"),
            "tier": intent.get("sla_tier"),
            "rules": intent.get("status_rules"),
            "spoke": intent.get("spoke"),
            "pseudowires": [
                {
                    "role": p.get("role"),
                    "vc_id": p.get("vc_id"),
                    "spoke_end": p.get("a_end"),
                    "hub_end": p.get("z_end"),
                }
                for p in intent.get("pseudowires") or []
            ],
        }
        if intent
        else None,
        "live": compact_service(raw["live"]) if raw.get("live") else None,
        "zabbix": {
            "service": zabbix.get("service"),
            "problems": [compact_problem(p) for p in zabbix.get("problems") or []],
            "correlation": [g.get("summary") for g in zabbix.get("groups") or []],
            "error": zabbix.get("error"),
        },
        "drift": raw.get("drift") or [],
    }


async def _topology(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return shape_topology(await ctx.lab.topology())


async def _devices(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return compact_devices(await ctx.lab.devices())


async def _state(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return shape_state(await ctx.lab.get(f"/devices/{ctx.host(a)}/state"))


async def _running(ctx: ToolContext, a: dict[str, Any]) -> Any:
    raw = await ctx.lab.get(f"/devices/{ctx.host(a)}/running")
    return {"host": raw.get("host"), "config": raw.get("config")}


async def _intent(ctx: ToolContext, a: dict[str, Any]) -> Any:
    raw = await ctx.lab.get(f"/devices/{ctx.host(a)}/intent")
    return {
        "host": raw.get("host"),
        "netbox_intent": raw.get("intent"),
        "intent_error": raw.get("intent_error"),
        "device_vs_netbox": raw.get("device"),
        "findings": raw.get("findings") or [],
    }


async def _drift(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return shape_drift(await ctx.lab.get("/drift", timeout=60))


async def _impact(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return shape_impact(await ctx.lab.get(f"/impact/{ctx.host(a)}", timeout=60))


async def _events(ctx: ToolContext, a: dict[str, Any]) -> Any:
    minutes = float(a.get("minutes") or 15)
    minutes = min(max(minutes, 0.5), 240)
    params: dict[str, Any] = {"since": round(time.time() - minutes * 60, 3), "limit": int(a.get("limit") or 80)}
    if a.get("host"):
        params["host"] = ctx.host(a)
    raw = await ctx.lab.get("/events", params)
    return shape_events(raw, include_traps=bool(a.get("include_traps")))


async def _zabbix_get(ctx: ToolContext, a: dict[str, Any]) -> Any:
    method = str(a.get("method") or "").strip()
    if method not in ZABBIX_READS:
        raise ToolError(f"method must be one of {', '.join(ZABBIX_READS)}")
    params = a.get("params") or {}
    if not isinstance(params, dict):
        raise ToolError("'params' must be an object (the method's parameters, Zabbix 8.0 API)")
    return {"method": method, "result": await ctx.lab.zabbix_read(method, params)}


async def _netbox_get(ctx: ToolContext, a: dict[str, Any]) -> Any:
    path = str(a.get("path") or "").strip().lstrip("/")
    if path.startswith("api/"):
        path = path[4:]
    query = a.get("query") or {}
    if not isinstance(query, dict):
        raise ToolError('\'query\' must be an object of query parameters, e.g. {"role": "hub"}')
    params: list[tuple[str, str]] = []
    for key, value in query.items():
        for v in value if isinstance(value, list) else [value]:
            params.append((str(key), str(v).lower() if isinstance(v, bool) else str(v)))
    return {"path": path, "data": await ctx.lab.netbox_get(path, params)}


async def _restconf(ctx: ToolContext, a: dict[str, Any]) -> Any:
    host = ctx.host(a)
    content = str(a.get("content") or "config")
    if content not in ("config", "nonconfig", "all"):
        raise ToolError("'content' must be config, nonconfig or all")
    path = str(a.get("path") or "").strip().lstrip("/")
    return {"host": host, "path": path, "content": content, "data": await ctx.lab.restconf_get(host, path, content)}


async def _propose(ctx: ToolContext, kind: str, a: dict[str, Any]) -> Any:
    if ctx.propose is None or ctx.autonomy == "observe":
        raise ToolError("proposals are off (BOT_AUTONOMY=observe): describe the fix in your notes instead")
    reason = str(a.get("reason") or "").strip()
    if not reason:
        raise ToolError("'reason' is required")
    host = ctx.host(a)
    params: dict[str, Any] = {}
    if kind == "config-patch":
        patch = a.get("patch")
        if not isinstance(patch, list) or not patch:
            raise ToolError("'patch' must be a non-empty list of operations")
        params["patch"] = patch
    elif kind == "field-repair":
        fault = a.get("kind")
        if fault not in FAULT_KINDS:
            raise ToolError(f"'kind' must be one of {', '.join(FAULT_KINDS)}")
        params["kind"] = fault
        if fault == "cut":
            params["peer"] = ctx.host(a, "peer")
        if fault == "ac":
            iface = str(a.get("interface") or "").strip()
            if not iface:
                raise ToolError("kind=ac needs 'interface'")
            params["interface"] = iface
    result = await ctx.propose(kind=kind, host=host, reason=reason, params=params)
    if result.get("proposal_id"):
        ctx.proposals.append(result["proposal_id"])
    return result


async def _apply_intent(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return await _propose(ctx, "apply-intent", a)


async def _patch(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return await _propose(ctx, "config-patch", a)


async def _field(ctx: ToolContext, a: dict[str, Any]) -> Any:
    return await _propose(ctx, "field-repair", a)


# ---------------------------------------------------------------------------------------------- registry
def _h(a: dict[str, Any]) -> str:
    return str(a.get("host") or "?")


READ_TOOLS: list[Tool] = [
    Tool(
        "get_open_problems",
        "Open Zabbix problems on the lab hosts (trap hints excluded unless scope=l2vpn-trap), with lab-api's "
        "correlation groups (same service tag + close start times = one root cause). PW checks: missing (NetBox "
        "expects the PW, the device lacks it, High), oper (PW down, High), spec (PW runs but peer/VC ID differ from "
        "NetBox, Average), unexpected (device has a PW NetBox does not know, Warning).",
        _obj(
            {
                "service": {"type": "string", "description": "Only problems of this service, e.g. SVC-0002"},
                "scope": {"type": "string", "enum": SCOPES, "description": "Only this problem scope"},
            }
        ),
        "console",
        "read",
        lambda a: f"Zabbix problems{' ' + a['service'] if a.get('service') else ''}",
        _problems,
    ),
    Tool(
        "list_services",
        "Every service in one table: customer, SLA tier (gold > silver > bronze), status rules (redundant-pair = "
        "primary + backup PW, one down = degraded; unprotected = primary only, down = service down), live state of "
        "both ends of each PW (oper/reason, peer, VC ID) and the Zabbix business service status (OK, Warning = "
        "degraded, Disaster = down). Also a per-device summary (reachable, powered off, uplink, faults).",
        _obj(),
        "screen",
        "read",
        lambda a: "service board",
        _services,
    ),
    Tool(
        "get_service",
        "One service in every system: NetBox intent (its PWs, hubs, VC IDs, expected peers), live state, the Zabbix "
        "service with its leaves and open problems, and the drift/fault findings for it.",
        _obj({"service_id": {"type": "string", "description": "e.g. SVC-0002"}}, ["service_id"]),
        "netbox",
        "read",
        lambda a: f"{a.get('service_id', '?')} in every system",
        _service,
    ),
    Tool(
        "get_topology",
        "The lab as NetBox describes it: regions with their sites, every device (role hub/spoke, region, loopback = "
        "PW peer address, container running), every service with its PWs (role, hub, VC ID, ACs) and the customers' "
        "SLA tiers. Each region has its own hub pair; never assume which hub a service uses: read it here.",
        _obj(),
        "netbox",
        "read",
        lambda a: "NetBox topology",
        _topology,
    ),
    Tool(
        "list_devices",
        "Every device: reachable, powered off, core uplink up/down, active physical faults (uplink, attachment "
        "circuit, core cut) and PWs up/total.",
        _obj(),
        "rack",
        "read",
        lambda a: "device rack",
        _devices,
    ),
    Tool(
        "get_device_state",
        "Live control-plane state of one device (simd): uplink, faults, and per PW: peer, peer host, VC ID, MTU, "
        "admin, oper and the reason (ok, peer-unreachable, remote-not-configured, mtu-mismatch, ac-down, "
        "local-uplink-down, admin-down, duplicate-pw, remote-admin-down, remote-ac-down), with what the remote end "
        "advertises.",
        _obj({"host": HOST_PARAM}, ["host"]),
        "rack",
        "read",
        lambda a: f"{_h(a)} state",
        _state,
    ),
    Tool(
        "get_device_running_config",
        "The device's running config (RESTCONF JSON, canonical): interfaces (MTU, enabled), xconnects with their "
        "pseudowires (role, peer-ipv4, vc-id, enabled).",
        _obj({"host": HOST_PARAM}, ["host"]),
        "rack",
        "read",
        lambda a: f"{_h(a)} running config",
        _running,
    ),
    Tool(
        "get_device_intent",
        "What NetBox says the device should run, the differences from its running config (each classified "
        "config-drift = the device moved, or intent-only = NetBox changed and the device was not updated yet) and "
        "the findings that involve the device.",
        _obj({"host": HOST_PARAM}, ["host"]),
        "netbox",
        "read",
        lambda a: f"{_h(a)} NetBox intent",
        _intent,
    ),
    Tool(
        "get_drift",
        "NetBox vs every device. One finding per root cause and service, classified config-drift (device differs "
        "from NetBox), intent-only (NetBox changed, devices not updated: e.g. a service ordered but not delivered) "
        "or fault (physical: uplink down, AC down, core cut, device powered off; config matches NetBox). Findings "
        "name the hosts where the cause is and the config paths behind it.",
        _obj(),
        "desk",
        "read",
        lambda a: "drift check",
        _drift,
    ),
    Tool(
        "get_impact",
        "What losing (or maintaining) one host would do right now: NetBox's blast radius combined with the live "
        "state of the PWs that would survive. A protected service whose backup is already broken goes down, not "
        "degraded; an unprotected service whose only PW ends on the host goes down. Counted per SLA tier.",
        _obj({"host": HOST_PARAM}, ["host"]),
        "whiteboard",
        "read",
        lambda a: f"blast radius of {_h(a)}",
        _impact,
    ),
    Tool(
        "get_events",
        "Recent device events, merged and time-ordered: config changes (who committed, the diff), PW oper "
        "transitions with reasons, PWs added/removed, neighbors up, boots. Use it to see what changed when.",
        _obj(
            {
                "minutes": {"type": "number", "description": "How far back (default 15)"},
                "host": {"type": "string", "description": "Only this device"},
                "limit": {"type": "integer", "description": "At most this many events (default 80)"},
                "include_traps": {"type": "boolean", "description": "Also list the SNMP traps sent (default false)"},
            }
        ),
        "console",
        "read",
        lambda a: f"event log{' ' + a['host'] if a.get('host') else ''}",
        _events,
    ),
]

PROPOSE_TOOLS: list[Tool] = [
    Tool(
        "propose_apply_intent",
        "Propose pushing NetBox's intent to one device (NetBox wins): lab-api makes a dry run (diff, validation, "
        "affected services, predicted drift after); the NOC Manager reviews the ticket, then a human approves it. "
        "Use it for config-drift and intent-only findings, one proposal per host. Nothing changes until approved.",
        _obj({"host": HOST_PARAM, "reason": REASON_PARAM}, ["host", "reason"]),
        "printer",
        "propose",
        lambda a: f"change ticket: NetBox intent -> {_h(a)}",
        _apply_intent,
    ),
    Tool(
        "propose_config_patch",
        "Propose a specific config change on one device (only when pushing NetBox's whole intent is wrong for it). "
        "Operations: {op: set, path, value} / {op: delete, path}, paths written like drift paths, e.g. "
        "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id. Dry run first; the NOC Manager reviews it, a human "
        "approves it.",
        _obj(
            {
                "host": HOST_PARAM,
                "patch": {
                    "type": "array",
                    "description": "Operations applied in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["set", "delete"]},
                            "path": {"type": "string"},
                            "value": {"description": "New value (set only)"},
                        },
                        "required": ["op", "path"],
                    },
                },
                "reason": REASON_PARAM,
            },
            ["host", "patch", "reason"],
        ),
        "printer",
        "propose",
        lambda a: f"change ticket: patch {_h(a)}",
        _patch,
    ),
    Tool(
        "propose_field_repair",
        "Propose dispatching the field technician for a physical fault config cannot fix: kind=uplink (core uplink "
        "of host down), ac (attachment circuit 'interface' of host down), cut (core path between host and peer "
        "broken) or device (host powered off: power it on). The NOC Manager reviews it, then a human approves it.",
        _obj(
            {
                "kind": {"type": "string", "enum": FAULT_KINDS},
                "host": HOST_PARAM,
                "peer": {"type": "string", "description": "kind=cut: the other end"},
                "interface": {"type": "string", "description": "kind=ac: the interface, e.g. GigabitEthernet0/0/1"},
                "reason": REASON_PARAM,
            },
            ["kind", "host", "reason"],
        ),
        "printer",
        "propose",
        lambda a: f"field ticket: {a.get('kind', '?')} {_h(a)}",
        _field,
    ),
]

DIRECT_READ_TOOLS: list[Tool] = [
    Tool(
        "zabbix_get",
        "Read Zabbix 8.0 directly (lab-api's token): method problem.get, event.get, trigger.get, item.get, "
        "history.get, trend.get, host.get, hostgroup.get, service.get, sla.get, sla.getsli, alert.get or "
        "maintenance.get, with params as the Zabbix 8.0 API documents them (get calls default to limit 1000). "
        "Customer nodes of the BSM carry the service tag bsm_level=customer; their events need source 4 and object 6 "
        "(alert.get shows service alerts to Zabbix Super admins only, so not here). Nothing that writes is possible.",
        _obj(
            {
                "method": {"type": "string", "enum": ZABBIX_READS},
                "params": {"type": "object", "description": "The method's parameters, e.g. {output: [name, status]}"},
            },
            ["method"],
        ),
        "console",
        "read",
        lambda a: f"Zabbix {a.get('method', '?')}",
        _zabbix_get,
    ),
    Tool(
        "netbox_get",
        "Read NetBox's REST API directly (GET only, read-only token): path after /api/, e.g. dcim/devices/, "
        "vpn/l2vpns/, ipam/vlans/, tenancy/tenants/; query = the filters, e.g. {role: hub} or {cf_service_id: "
        "SVC-0002}. Paginated lists come with count and next.",
        _obj(
            {
                "path": {"type": "string", "description": "e.g. vpn/l2vpns/ or dcim/interfaces/"},
                "query": {"type": "object", "description": "Query parameters (a list value repeats the parameter)"},
            },
            ["path"],
        ),
        "netbox",
        "read",
        lambda a: f"NetBox {a.get('path', '?')}",
        _netbox_get,
    ),
    Tool(
        "device_restconf_get",
        "A RESTCONF GET on one device (Clixon, read-only): path under /restconf/data, e.g. netsim-device:l2vpn or "
        "netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F1.101 (list keys percent-encoded); empty = the "
        "whole datastore. content: config (default), nonconfig, all.",
        _obj(
            {
                "host": HOST_PARAM,
                "path": {"type": "string", "description": "RESTCONF data path; empty for everything"},
                "content": {"type": "string", "enum": ["config", "nonconfig", "all"]},
            },
            ["host"],
        ),
        "rack",
        "read",
        lambda a: f"{_h(a)} RESTCONF {a.get('path') or '/'}",
        _restconf,
    ),
]

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in (*READ_TOOLS, *DIRECT_READ_TOOLS, *PROPOSE_TOOLS)}


async def run_tool(tool: Tool, ctx: ToolContext, args: dict[str, Any]) -> tuple[bool, Any]:
    """``(ok, result)``; lab-api errors and wrong arguments come back as ``{"error": ...}`` for the model."""
    try:
        return True, clip(await tool.handler(ctx, args))
    except ToolError as exc:
        return False, {"error": str(exc)}
    except LabApiError as exc:
        return False, exc.to_dict()
