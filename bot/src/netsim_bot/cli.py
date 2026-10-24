"""``botctl``: the NOC office from the terminal (status, incidents, approvals, pages, the roles' tools, the tunnel,
the setup of the roles' terminals, the operator's phone).

Talks to grok-bot's API (``BOT_URL``, default ``http://127.0.0.1:8095``) with ``BOT_OPERATOR_TOKEN`` from ``.env``;
``call`` and ``tools`` speak MCP to ``/mcp`` with a role's token, exactly as the Grok Bot app does.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from netsim_bot.settings import ROLE_BY_BOT, ROLES, BotSettings, find_repo_root, host_matches, load_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
console = Console()
notes = Console(stderr=True)  # what goes next to a block meant for copying: stdout stays the block alone

SEV_STYLE = {
    "Disaster": "bold white on red",
    "High": "red",
    "Average": "dark_orange",
    "Warning": "yellow",
    "Information": "cyan",
    "OK": "green",
}
STATUS_STYLE = {
    "new": "bold yellow",
    "working": "cyan",
    "awaiting-approval": "bold magenta",
    "remediating": "bold blue",
    "recovered": "green",
    "closed": "dim",
    "review": "magenta",
    "pending": "bold magenta",
    "approved": "blue",
    "applying": "bold blue",
    "applied": "green",
    "failed": "bold red",
    "rejected": "dim",
    "returned": "dim",
    "obsolete": "dim",
    "sent": "green",
    "skipped": "dim",
    "queued": "blue",
}
SINK_URL = "http://172.30.10.61:8096"
SINK_HOST_URL = "http://127.0.0.1:8096"
QUICK_TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def base_url() -> str:
    return os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")


def role_key(value: str) -> str:
    """``noc-engineer``, ``NOCENG`` or ``noceng`` -> ``noc-engineer``."""
    v = value.strip()
    if v in ROLES:
        return v
    spec = ROLE_BY_BOT.get(v.upper())
    if spec is None:
        console.print(f"[red]no role {value!r}[/]: {', '.join(ROLES)} (or {', '.join(ROLE_BY_BOT)})")
        raise typer.Exit(2)
    return spec.key


def _client() -> httpx.Client:
    s = load_settings()
    headers = {"X-Operator": f"botctl:{os.environ.get('USER', 'operator')}"}
    token = s.operator_token
    if not token and (s.data_dir / "operator-token").is_file():  # a bot run on this host without .env's token
        token = (s.data_dir / "operator-token").read_text(encoding="utf-8").strip()
    if token:
        headers["X-Operator-Token"] = token
    return httpx.Client(base_url=base_url(), headers=headers, timeout=60)


def _call(method: str, path: str, **kw: Any) -> Any:
    with _client() as c:
        try:
            r = c.request(method, path, **kw)
        except httpx.HTTPError as exc:
            console.print(f"[red]grok-bot not reachable at {c.base_url}: {exc}[/] (make bot-up)")
            raise typer.Exit(2) from exc
    if r.status_code >= 400:
        try:
            detail = r.json().get("error")
        except ValueError:
            detail = r.text
        console.print(f"[red]HTTP {r.status_code}: {detail}[/]")
        raise typer.Exit(1)
    return r.json()


def _age(ts: float | None) -> str:
    if not ts:
        return "-"
    s = max(0, int(time.time() - ts))
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def _customers(inc: dict[str, Any]) -> str:
    out = []
    for name, c in (inc.get("customers") or {}).items():
        state = c.get("state")
        mark = {"down": "[red]down[/]", "degraded": "[yellow]degraded[/]", "ok": "[green]ok[/]"}.get(state, state)
        out.append(f"{name} ({c.get('tier') or '?'}) {mark}")
    return ", ".join(out)


@app.command()
def status() -> None:
    """The roles, open incidents, change tickets in review and waiting for a human, batches, pages."""
    st = _call("GET", "/api/state")
    eff = st["effective"]
    console.print(
        f"autonomy [bold]{eff['autonomy']}[/] | "
        + ("[bold yellow]MAINTENANCE MODE[/] | " if eff["paused"] else "")
        + ("[bold yellow]PAGES MUTED[/] | " if eff["pages_muted"] else "")
        + ("[bold cyan]TEST SINK[/] | " if eff.get("sink") else "")
        + f"lab {'ok' if st['lab']['ok'] else '[red]' + str(st['lab']['error']) + '[/]'} | "
        f"alert intake {'on' if eff['hook'] else '[red]off (BOT_ZABBIX_HOOK_TOKEN)[/]'}"
    )
    for w in eff.get("warnings") or []:
        console.print(f"[yellow]warning:[/] {w}")
    roles = Table(box=None, header_style="bold")
    for col in ("robot", "role", "routine", "terminal", "status", "doing"):
        roles.add_column(col)
    for m in st["roles"]:
        terminal = ((st.get("settings") or {}).get("roles") or {}).get(m.get("role"), {}).get("terminal")
        roles.add_row(
            m["name"],
            m["title"],
            "[green]set[/]" if m.get("routine") else "[dim]not set[/]",
            "[green]on[/]" if terminal else "[dim]off[/]",
            m["status"],
            (m.get("activity") or "")[:80],
        )
    console.print(roles)
    if st.get("batches"):
        for b in st["batches"]:
            console.print(
                f"[blue]batch {b['id']}[/]: {b['alerts']} alert(s) for {b['role']}, closes in "
                f"{max(0, b['closes_at'] - time.time()):.0f} s"
            )
    _incident_table([i for i in st["incidents"] if i["status"] != "closed"], "open incidents")
    review = [p for p in st["proposals"] if p["status"] == "review"]
    if review:
        _proposal_table(review, "in the NOC Manager's review")
    pending = [p for p in st["proposals"] if p["status"] == "pending"]
    if pending:
        _proposal_table(pending, "proposed to you (botctl approve <id>, or --incident INC-xxxx)")


def _incident_table(rows: list[dict[str, Any]], title: str) -> None:
    if not rows:
        console.print(f"[green]{title}: none[/]")
        return
    t = Table(title=title, header_style="bold", title_justify="left")
    for col in ("id", "sev", "prio", "status", "title", "customers", "owner", "age"):
        t.add_column(col)
    for i in rows:
        t.add_row(
            i["id"],
            f"[{SEV_STYLE.get(i['severity'], '')}]{i['severity']}[/]",
            i.get("priority") or "-",
            f"[{STATUS_STYLE.get(i['status'], '')}]{i['status']}[/]",
            i["title"][:60],
            _customers(i)[:120],
            i.get("assignee") or "-",
            _age(i["created"]),
        )
    console.print(t)


def _proposal_table(rows: list[dict[str, Any]], title: str) -> None:
    t = Table(title=title, header_style="bold", title_justify="left")
    for col in ("id", "incident", "status", "kind", "host", "change", "by", "review"):
        t.add_column(col)
    for p in rows:
        diff = (p.get("plan") or {}).get("diff") or []
        change = "; ".join(d.get("message", "") for d in diff[:2]) or json.dumps(p.get("params"))
        rev = (p.get("reviews") or [{}])[-1]  # tickets from before the review step hold a "recommendation"
        verdict = rev.get("decision") or rev.get("recommendation")
        said = f"{rev.get('by')} {verdict}: {rev.get('reason', '')[:40]}" if rev else "-"
        t.add_row(
            p["id"],
            p.get("incident_id") or "-",
            f"[{STATUS_STYLE.get(p['status'], '')}]{p['status']}[/]" + (f" ({p['stage']})" if p.get("stage") else ""),
            p["kind"],
            p["host"],
            change[:70],
            p["proposed_by"],
            said,
        )
    console.print(t)


@app.command()
def incidents(all_: Annotated[bool, typer.Option("--all", help="Include closed ones")] = False) -> None:
    """Incidents (open ones, or all recent with --all)."""
    rows = _call("GET", "/api/incidents", params={"limit": 50, "active": not all_})
    _incident_table(rows, "incidents" if all_ else "open incidents")


@app.command()
def show(incident_id: str) -> None:
    """One incident: customers, triage, root cause, tickets, pages and the timeline."""
    inc = _call("GET", f"/api/incidents/{incident_id}")
    console.print(
        f"[bold]{inc['id']}[/] [{SEV_STYLE.get(inc['severity'], '')}]{inc['severity']}[/] "
        f"[{STATUS_STYLE.get(inc['status'], '')}]{inc['status']}[/] {inc.get('priority') or ''}  {inc['title']}"
    )
    console.print(f"[bold]customers[/] {_customers(inc)}")
    if inc.get("triage"):
        console.print(f"[bold]triage[/] ({inc['triage'].get('by')}) {inc['triage'].get('summary')}")
    if inc.get("root_cause"):
        r = inc["root_cause"]
        console.print(f"[bold]root cause[/] ({r.get('by')}) {r.get('root_cause')}  [dim]({r.get('category')})[/]")
    for e in inc.get("escalations") or []:
        console.print(f"[bold]escalated[/] {e['from']} -> {e['to']}: {e['note']}")
    for a in inc.get("acks") or []:
        console.print(f"[bold]acknowledged[/] by {a.get('zabbix_user')}: {len(a.get('acknowledged') or [])} problem(s)")
    for u in inc.get("customer_updates") or []:
        console.print(f"[bold]customer update[/] ({u['by']}): {u['text'][:200]}")
    if inc.get("proposals"):
        _proposal_table(inc["proposals"], "change tickets")
    t = Table(title="pages", header_style="bold", title_justify="left", box=None)
    for col in ("time", "id", "to", "event", "status"):
        t.add_column(col)
    for p in inc.get("page_log") or []:
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(p["created"])),
            p["id"],
            p["role"],
            p["event"],
            f"{p['status']}{' (' + p['why'] + ')' if p.get('why') else ''}",
        )
    console.print(t)
    t = Table(title="timeline", header_style="bold", title_justify="left", box=None)
    for col in ("time", "who", "what"):
        t.add_column(col)
    for a in inc.get("activities") or []:
        if a["kind"] in ("result",):
            continue
        t.add_row(time.strftime("%H:%M:%S", time.localtime(a["ts"])), a["bot"], f"{a['kind']}: {a['text']}"[:120])
    console.print(t)


@app.command()
def approve(
    proposal_id: Annotated[str | None, typer.Argument(help="CHG-xxxx")] = None,
    incident: Annotated[
        str | None, typer.Option(help="Approve every ticket of this incident the NOC Manager proposed")
    ] = None,
    comment: Annotated[str | None, typer.Option(help="Why")] = None,
) -> None:
    """Approve a change ticket the NOC Manager proposed (it is applied and verified), or all of an incident's."""
    if incident:
        done = _call("POST", f"/api/incidents/{incident}/approve-all", json={"comment": comment})
        console.print(f"approved {len(done)}: {', '.join(p['id'] for p in done)}")
        tickets = _call("GET", f"/api/incidents/{incident}")["proposals"]
        review = [p["id"] for p in tickets if p["status"] == "review"]
        if review:
            console.print(f"[yellow]still in the NOC Manager's review:[/] {', '.join(review)}")
    elif proposal_id:
        p = _call("POST", f"/api/proposals/{proposal_id}/approve", json={"comment": comment})
        console.print(f"[green]{p['id']} approved[/]: the executor applies and verifies it now")
    else:
        console.print("[red]give a ticket id or --incident[/]")
        raise typer.Exit(1)


@app.command()
def reject(proposal_id: str, comment: Annotated[str | None, typer.Option(help="Why")] = None) -> None:
    """Reject a change ticket."""
    p = _call("POST", f"/api/proposals/{proposal_id}/reject", json={"comment": comment})
    console.print(f"{p['id']} rejected")


@app.command()
def ask(
    message: str,
    chat: Annotated[str | None, typer.Option(help="Continue this conversation id")] = None,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for the answer")] = 600,
) -> None:
    """Ask the NOC (the NOC Manager's routine is paged: one run) and wait for the answer."""
    q = _call("POST", "/api/chat", json={"message": message, "chat_id": chat})
    console.print(f"[dim]{q['id']} asked; the NOC Manager is paged (page {q.get('page_id')})…[/]")
    deadline = time.time() + timeout
    shown: set[str] = set()
    while time.time() < deadline:
        time.sleep(2.0)
        for m in _call("GET", "/api/chat", params={"chat_id": q["chat_id"]}):
            if m["id"] in shown or m["ts"] < q["ts"]:
                continue
            shown.add(m["id"])
            if m["role"] == "system":
                console.print(f"[yellow]{m['text']}[/]")
            elif m["role"] == "bot" and m.get("answer_to") == q["id"]:
                console.print(f"[bold]{m['bot']}[/]: {m['text']}")
                return
    console.print("[yellow]no answer yet; see the NOC page[/]")


@app.command()
def watch() -> None:
    """Live activity of the office (Ctrl-C to stop)."""
    with httpx.stream("GET", f"{base_url()}/api/stream", timeout=None) as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event in ("activity", "incident", "proposal", "page"):
                ev = json.loads(line[5:])
                d = ev.get("data", {})
                stamp = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", time.time())))
                if event == "activity" and d.get("kind") != "result":
                    console.print(f"[dim]{stamp}[/] [bold]{d.get('bot')}[/] {d.get('kind')}: {d.get('text')}")
                elif event == "incident":
                    console.print(
                        f"[dim]{stamp}[/] [{STATUS_STYLE.get(d.get('status'), '')}]{d.get('id')} "
                        f"{d.get('status')}[/] {d.get('title')}"
                    )
                elif event == "proposal":
                    console.print(
                        f"[dim]{stamp}[/] [{STATUS_STYLE.get(d.get('status'), '')}]{d.get('id')} "
                        f"{d.get('status')}[/] {d.get('kind')} {d.get('host')}"
                        + (f" ({d['stage']})" if d.get("stage") else "")
                    )
                elif event == "page" and d.get("status") != "queued":
                    console.print(
                        f"[dim]{stamp}[/] page {d.get('id')} -> {d.get('role')}: {d.get('event')} "
                        f"[{STATUS_STYLE.get(d.get('status'), '')}]{d.get('status')}[/] {d.get('why') or ''}"
                    )


@app.command()
def pause(reason: Annotated[str | None, typer.Option(help="Why (shown in the room)")] = None) -> None:
    """Maintenance mode: alerts are recorded, no incident opens, nobody is paged."""
    _call("POST", "/api/control", json={"paused": True, "reason": reason})
    console.print("maintenance mode on")


@app.command()
def resume() -> None:
    """Leave maintenance mode."""
    _call("POST", "/api/control", json={"paused": False})
    console.print("maintenance mode off")


@app.command()
def mute() -> None:
    """Mute every page (alerts are only recorded, no incident opens)."""
    _call("POST", "/api/control", json={"pages_muted": True})
    console.print("pages muted")


@app.command()
def unmute() -> None:
    """Page again."""
    _call("POST", "/api/control", json={"pages_muted": False})
    console.print("pages on")


@app.command()
def autonomy(choice: Annotated[str, typer.Argument(help="observe, propose or default (.env)")]) -> None:
    """observe: the roles diagnose and report only; propose: they file change tickets a human approves."""
    r = _call("POST", "/api/control", json={"autonomy": choice})
    console.print(f"autonomy now: {r['effective']['autonomy']}")


# ---------------------------------------------------------------------------------------------- pages
page_app = typer.Typer(no_args_is_help=True, help="Pages to the roles' routines (Grok Bot routines).")
app.add_typer(page_app, name="page")


@page_app.command("status")
def page_status(role: Annotated[str | None, typer.Option(help="Only this role")] = None) -> None:
    """Which routines are set, mute and sink state, the last pages."""
    key = role_key(role) if role else None
    st = _call("GET", "/api/pages", params={"role": key} if key else {})
    flags = [
        "[yellow]muted[/]" if st["muted"] else "",
        "[yellow]maintenance mode[/]" if st["paused"] else "",
        "[cyan]test sink[/]" if st["sink"] else "",
    ]
    console.print(f"pages: max {st['max_per_hour']}/h per role " + " ".join(f for f in flags if f))
    for r in st["roles"].values():
        routine = "[green]routine set[/]" if r["routine"] else "[dim]no routine (BOT_ROUTINE_" + r["bot"] + "_URL)[/]"
        console.print(f"  {r['bot']:<7} {r['title']:<17} {routine}  sent in the last hour: {r['sent_last_hour']}")
    t = Table(header_style="bold", box=None)
    for col in ("time", "id", "to", "event", "incident", "status", "detail"):
        t.add_column(col)
    for p in st["history"][:20]:
        detail = p.get("why") or (f"HTTP {p['http']}" if p.get("http") else "") or (p.get("error") or "")
        if p.get("error") and p.get("status") == "failed":
            detail = f"{detail} {p['error']}"[:90]
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(p["created"])),
            p["id"],
            ROLES[p["role"]].bot,
            p["event"],
            p.get("incident_id") or "-",
            f"[{STATUS_STYLE.get(p['status'], '')}]{p['status']}[/]" + (" (sink)" if p.get("sink") else ""),
            detail,
        )
    console.print(t)


@page_app.command("test")
def page_test(role: Annotated[str, typer.Argument(help="noc-engineer, network-engineer, noc-manager")]) -> None:
    """Send one test page to a role's routine now (checks its URL and key)."""
    rec = _call("POST", f"/api/pages/test/{role_key(role)}")
    if rec.get("status") == "sent":
        console.print(
            f"[green]test page sent[/]: HTTP {rec.get('http')} in {rec.get('seconds')} s"
            + (" (to the test sink)" if rec.get("sink") else "")
        )
    else:
        console.print(
            f"[red]test page {rec.get('status')}[/]: {rec.get('why') or ''} {rec.get('http') or ''} "
            f"{rec.get('error') or ''}"
        )
        raise typer.Exit(1)


@app.command()
def sink(
    action: Annotated[str, typer.Argument(help="on (pages go to the routine sink), off, or show")] = "show",
) -> None:
    """The routine sink (make bot-sink-up): pages go to it instead of the routines; show prints what it got."""
    if action == "on":
        _call("POST", "/api/control", json={"sink_url": SINK_URL})
        console.print(f"pages go to the routine sink {SINK_URL} (see them with botctl sink show)")
    elif action == "off":
        _call("POST", "/api/control", json={"sink_url": ""})
        console.print("pages go to the routines again")
    else:
        try:
            pages = httpx.get(f"{SINK_HOST_URL}/pages", timeout=10).json()
        except httpx.HTTPError as exc:
            console.print(f"[red]no routine sink at {SINK_HOST_URL}: {exc}[/] (make bot-sink-up)")
            raise typer.Exit(2) from exc
        for p in pages[-30:]:
            b = p["body"] if isinstance(p["body"], dict) else {}
            console.print(
                f"[dim]{time.strftime('%H:%M:%S', time.localtime(p['received']))}[/] "
                f"[bold]{p['role']}[/] {b.get('event')}: {b.get('summary')}"
            )


# ---------------------------------------------------------------------------------------------- the roles' tools
def _mcp(role: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One JSON-RPC call to ``/mcp`` with the role's token: the same door the Grok Bot app uses."""
    s = load_settings()
    spec = ROLES[role]
    token = s.role_tokens.get(role)
    if not token:
        console.print(f"[red]{spec.token_env} is not set in .env[/] (make env adds one)")
        raise typer.Exit(2)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    try:
        r = httpx.post(f"{base_url()}/mcp", json=body, headers=headers, timeout=330)
    except httpx.HTTPError as exc:
        console.print(f"[red]grok-bot not reachable at {base_url()}: {exc}[/] (make bot-up)")
        raise typer.Exit(2) from exc
    if r.status_code >= 400:
        console.print(f"[red]HTTP {r.status_code}: {r.text[:300]}[/]")
        raise typer.Exit(1)
    data = r.json()
    if "error" in data:
        console.print(f"[red]{data['error'].get('message')}[/]")
        raise typer.Exit(1)
    return data["result"]


ROLE_OPTION = typer.Option("--role", "-r", help="noc-engineer, network-engineer, noc-manager (or NOCENG, ...)")


@app.command()
def tools(role: Annotated[str, ROLE_OPTION] = "noc-engineer") -> None:
    """The tools a role gets at /mcp."""
    key = role_key(role)
    t = Table(header_style="bold", box=None, title=f"{ROLES[key].title} ({ROLES[key].bot})", title_justify="left")
    for col in ("tool", "kind", "what"):
        t.add_column(col)
    for tool in _mcp(key, "tools/list")["tools"]:
        kind = "read" if tool["annotations"]["readOnlyHint"] else "write"
        what = tool["description"].split(". ")[0].split(": ")[0]
        t.add_row(tool["name"], kind, what if len(what) <= 90 else what[:88].rsplit(" ", 1)[0] + " …")
    console.print(t)


@app.command()
def call(
    tool: Annotated[str, typer.Argument(help="Tool name (botctl tools --role ... lists them)")],
    args: Annotated[str | None, typer.Argument(help='Arguments as JSON, e.g. \'{"host": "hub-a"}\'')] = None,
    role: Annotated[str, ROLE_OPTION] = "noc-engineer",
    arg: Annotated[list[str] | None, typer.Option("--arg", "-a", help="key=value (JSON value if it parses)")] = None,
) -> None:
    """Call one tool as a role and print its JSON result (exit 1 if the tool reported an error).

    This is how a Grok Bot routine can work from your computer without a tunnel: it runs
    `uv run botctl call --role <role> ...` in the repository (you allow each command)."""
    try:
        arguments: dict[str, Any] = json.loads(args) if args else {}
    except ValueError as exc:
        console.print(f"[red]arguments are not JSON: {exc}[/]")
        raise typer.Exit(2) from exc
    for item in arg or []:
        key, _, value = item.partition("=")
        try:
            arguments[key] = json.loads(value)
        except ValueError:
            arguments[key] = value
    result = _mcp(role_key(role), "tools/call", {"name": tool, "arguments": arguments})
    out = result.get("structuredContent")
    print(json.dumps(out, indent=2) if out is not None else result["content"][0]["text"])
    if result.get("isError"):
        raise typer.Exit(1)


def connector(role: str, url: str, show_token: bool) -> dict[str, Any]:
    s = load_settings()
    spec = ROLES[role]
    token = s.role_tokens.get(role) if show_token and s.role_tokens.get(role) else f"<{spec.token_env} from .env>"
    return {f"netsim-noc-{spec.bot.lower()}": {"url": url, "headers": {"Authorization": f"Bearer {token}"}}}


@app.command("mcp-config")
def mcp_config(
    role: Annotated[str, ROLE_OPTION] = "noc-engineer",
    url: Annotated[str, typer.Option(help="The URL the app reaches /mcp at (a tunnel's, for its cloud PC)")] = (
        "http://127.0.0.1:8095/mcp"
    ),
    show_token: Annotated[bool, typer.Option("--show-token", help="Print the real token, not a placeholder")] = False,
) -> None:
    """The "MCP server JSON" of one role's connector, to paste into the Grok Bot app ("Add a custom MCP")."""
    key = role_key(role)
    print(json.dumps(connector(key, url, show_token), indent=2))
    if not load_settings().role_tokens.get(key):
        console.print(f"[yellow]{ROLES[key].token_env} is not set in .env yet (make env adds one)[/]")


# ---------------------------------------------------------------------------------------------- the roles' terminals
TERMINAL_SCRIPT = ("bot", "grokbot-app", "terminal", "noc-controller")
SCRIPT_END, CONFIG_END = "NOC_CONTROLLER_SCRIPT", "NOC_CONTROLLER_CONFIG"


def terminal_block(role: str, url: str, token: str, script: str) -> str:
    """What the user pastes into the role's Bot's chat: the Bot runs it on its own computer. It writes the command to
    ``~/.local/bin/noc-controller`` (755) and the role's config (600, created under umask 077), then runs a smoke test.
    Both go in quoted heredocs, so the shell expands nothing in the script or the token; a second run replaces both."""
    for end, text in ((SCRIPT_END, script), (CONFIG_END, f"{url}\n{token}")):
        if end in text.splitlines():
            raise ValueError(f"{end} is a line of the text it would end")
    if any(ord(c) < 32 or ord(c) == 127 for c in url + token):
        raise ValueError("the URL and the token are one line each, without control characters")
    spec = ROLES[role]
    config = f'"$config/{role}.conf"'
    return "\n".join(
        [
            f"Install noc-controller, the {spec.title}'s terminal to the NetSim NOC's Clixon controller (read only), "
            "on your computer: run the whole block below once, as one shell command, exactly as written, and show me "
            "what it prints. It writes ~/.local/bin/noc-controller and your config. From then on never read, print or "
            f"copy ~/.config/noc-controller/{role}.conf: it holds your terminal token.",
            "",
            "```sh",
            "(",
            "set -eu",
            'bin="$HOME/.local/bin"',
            'config="${XDG_CONFIG_HOME:-$HOME/.config}/noc-controller"',
            'mkdir -p "$bin" "$config"',
            'chmod 700 "$config"',
            f"cat > \"$bin/noc-controller\" <<'{SCRIPT_END}'",
            script.rstrip("\n"),
            SCRIPT_END,
            'chmod 755 "$bin/noc-controller"',
            "umask 077",
            f"rm -f {config}",
            f"cat > {config} <<'{CONFIG_END}'",
            f"url={url}",
            f"token={token}",
            CONFIG_END,
            f"chmod 600 {config}",
            ")",
            'export PATH="$HOME/.local/bin:$PATH"',
            f"noc-controller --role {role} show connections",
            "```",
        ]
    )


def url_warnings(url: str, s: BotSettings) -> list[str]:
    """Why the Bot's computer might not get through with this URL (empty: nothing to say); a URL that cannot work
    raises ``ValueError``."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() or c in "\"'" for c in url):
        raise ValueError(
            f"{url!r} is not an http(s) URL; give the one the Bot's connector uses, e.g. https://<host>/mcp"
        )
    host, out = parts.hostname, []
    if not parts.path.rstrip("/").endswith("/mcp"):
        out.append(f"{url} does not end in /mcp: grok-bot answers the roles at /mcp only")
    try:
        local = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if parts.scheme == "http" and not local:
        out.append(f"plain http to {host}: the token would travel unencrypted; use the tunnel's https URL")
    if not host_matches(host, [*s.mcp_hosts, *s.allowed_hosts]):
        out.append(
            f"neither BOT_MCP_HOSTS nor BOT_ALLOWED_HOSTS lists {host}: grok-bot would refuse it (HTTP 400). Add it "
            "to BOT_MCP_HOSTS in .env and restart grok-bot (docker compose up -d --force-recreate grok-bot)"
        )
    return out


@app.command("terminal-setup")
def terminal_setup(
    role: Annotated[str, ROLE_OPTION],
    url: Annotated[
        str, typer.Option(help="The /mcp URL the Bot's computer reaches: the tunnel's, as its connector uses it")
    ],
    show_token: Annotated[
        bool, typer.Option("--show-token", help="Put the real terminal token in the block, not a placeholder")
    ] = False,
) -> None:
    """The block to paste into a role's Bot chat: the Bot installs noc-controller (its terminal to the Clixon
    controller, read only) on its own computer and runs a smoke test. Run it again when the URL changed (a quick
    tunnel's changes at every start): the new block replaces the config.

    Only the block goes to stdout; notes go to stderr. The token is the role's terminal token
    (BOT_<ROLE>_TERMINAL_TOKEN: controller_cli only), never its role token."""
    key = role_key(role)
    spec = ROLES[key]
    s = load_settings()
    try:
        for warning in url_warnings(url, s):
            notes.print(f"[yellow]warning:[/] {escape(warning)}")
    except ValueError as exc:
        notes.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(2) from exc
    root = find_repo_root()
    path = root.joinpath(*TERMINAL_SCRIPT) if root else None
    if path is None or not path.is_file():
        notes.print(f"[red]{'/'.join(TERMINAL_SCRIPT)} not found[/]: run botctl in the NetSim repository")
        raise typer.Exit(2)
    token = s.terminal_tokens.get(key)
    if not token:
        notes.print(
            f"[yellow]{spec.terminal_token_env} is empty in .env:[/] the {spec.title}'s terminal is off "
            "(make env adds one; grok-bot reads it live)"
        )
        if show_token:
            raise typer.Exit(1)
    shown = token if show_token and token else f"<{spec.terminal_token_env} from .env>"
    try:
        print(terminal_block(key, url, shown, path.read_text(encoding="utf-8")))
    except ValueError as exc:
        notes.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(2) from exc
    notes.print(
        f"[dim]Paste it into the {spec.title}'s Bot chat"
        + ("" if show_token else " after printing it again with --show-token (this one has a placeholder)")
        + f". Rotate {spec.terminal_token_env} after the demo (a new value in .env), then print and paste it again.[/]"
    )


@app.command()
def tunnel(
    target: Annotated[str, typer.Option(help="What the tunnel publishes")] = "http://127.0.0.1:8095",
    show_token: Annotated[bool, typer.Option("--show-token", help="Print the real tokens in the configs")] = False,
) -> None:
    """A Cloudflare quick tunnel to grok-bot (needs cloudflared): prints the three roles' connector configs and keeps
    running until Ctrl-C. Its URL changes on every start, so BOT_MCP_HOSTS=*.trycloudflare.com."""
    exe = shutil.which("cloudflared")
    if exe is None:
        console.print(
            "[red]cloudflared is not installed.[/] Get it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ "
            "(Fedora: the cloudflared RPM), then run botctl tunnel again."
        )
        raise typer.Exit(2)
    s = load_settings()
    if not any(h == "*.trycloudflare.com" or h.endswith(".trycloudflare.com") for h in s.mcp_hosts):
        console.print(
            "[yellow]BOT_MCP_HOSTS does not allow *.trycloudflare.com:[/] grok-bot would refuse the tunnel's Host. "
            "Put BOT_MCP_HOSTS=*.trycloudflare.com in .env and restart grok-bot (docker compose up -d grok-bot)."
        )
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", target],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    shown = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = QUICK_TUNNEL.search(line)
            if m and not shown:
                shown = True
                mcp = f"{m.group(0)}/mcp"
                console.print(f"[green]tunnel up:[/] {mcp}  (only /mcp is published on it)")
                for key in ROLES:
                    console.print(f"[bold]{ROLES[key].title}[/] connector:")
                    print(json.dumps(connector(key, mcp, show_token), indent=2))
                console.print("[dim]Ctrl-C stops the tunnel (the URL is gone for good then).[/]")
            elif not shown:
                console.print(f"[dim]{line.rstrip()}[/]")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------------------------- the operator's phone
phone_app = typer.Typer(
    no_args_is_help=True,
    help="Approve from your phone: the tailnet-only listener, signing a phone in, the phones signed in.",
)
app.add_typer(phone_app, name="phone")
PHONE_PORT = 8097  # grok-bot's phone listener on this machine (bot/compose.yaml): 127.0.0.1:8097
PHONE_LOCAL = f"http://127.0.0.1:{PHONE_PORT}"


def _tailscale(*args: str) -> Any:
    """``tailscale <args> --json``, parsed; None when Tailscale is not here or does not answer."""
    exe = shutil.which("tailscale")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, *args, "--json"], capture_output=True, text=True, timeout=15, check=False)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _tailnet_name() -> str | None:
    """This computer's name on the tailnet (MagicDNS), e.g. ``box.tail1234.ts.net``."""
    me = (_tailscale("status") or {}).get("Self") or {}
    return str(me.get("DNSName") or "").rstrip(".") or None


def _served(serve: dict[str, Any] | None, host: str, port: int) -> tuple[str | None, bool]:
    """What ``tailscale serve`` proxies ``https://<host>:<port>/`` to, and whether Funnel publishes it."""
    web = ((serve or {}).get("Web") or {}).get(f"{host}:{port}") or {}
    target = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy")
    return target, bool(((serve or {}).get("AllowFunnel") or {}).get(f"{host}:{port}"))


def _ping(url: str) -> str | None:
    """``GET <url>/m/ping``: None when the phone listener answered ``ok``, else what went wrong."""
    try:
        r = httpx.get(f"{url}/m/ping", timeout=8)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if r.status_code == 200 and r.text == "ok" else f"HTTP {r.status_code} {r.text[:80]}"


@phone_app.command("setup")
def phone_setup(
    port: Annotated[int, typer.Option(help="The tailnet port (not 443, 8443 or 10000)")] = PHONE_PORT,
) -> None:
    """What to run once so your phone reaches the approvals over your tailnet (nothing is published on the
    internet)."""
    if port in (443, 8443, 10000):
        console.print(
            f"[red]port {port} is one Tailscale Funnel can publish:[/] take another one (default {PHONE_PORT})"
        )
        raise typer.Exit(2)
    name = _tailnet_name()
    host = name or "<this computer's tailnet name>"
    url = f"https://{host}:{port}"
    if name is None:
        console.print("[yellow]Tailscale does not answer here[/] (tailscale status): install it and sign in first.")
    console.print("[bold]1.[/] On this computer, put the phone listener on your tailnet (tailnet only):")
    print(f"sudo tailscale serve --bg --https={port} {PHONE_LOCAL}")
    console.print(f"   Never [bold]tailscale funnel[/] this port. Undo: sudo tailscale serve --https={port} off")
    console.print("[bold]2.[/] In .env (grok-bot reads it live, no restart):")
    print(f"BOT_PHONE_URL={url}")
    console.print("[bold]3.[/] On your phone: the Tailscale app, signed in to the same tailnet.")
    console.print("[bold]4.[/] Sign the phone in: NOC room, tab Approvals, [bold]Sign in a phone[/], and scan the code")
    console.print("   (or: uv run botctl phone pair). Then check everything with: uv run botctl phone status")


@phone_app.command("status")
def phone_status() -> None:
    """Is the phone listener up, on the tailnet (and not on Funnel), and which phones are signed in."""
    s = load_settings()
    bad = 0
    err = _ping(PHONE_LOCAL)
    if err:
        bad += 1
        console.print(f"[red]phone listener:[/] {PHONE_LOCAL} does not answer ({err}): recreate grok-bot (make bot-up)")
    else:
        console.print(f"[green]phone listener:[/] {PHONE_LOCAL} answers")
    for w in s.warnings:
        if "BOT_PHONE_URL" in w:
            bad += 1
            console.print(f"[red]BOT_PHONE_URL:[/] {w}")
    if not s.phone_url:
        console.print("[yellow]BOT_PHONE_URL is not set:[/] the phone has no address yet (uv run botctl phone setup)")
    else:
        parts = urlsplit(s.phone_url)
        port = parts.port or 443
        target, funnel = _served(_tailscale("serve", "status"), parts.hostname or "", port)
        if funnel:
            bad += 1
            console.print(f"[bold red]Tailscale Funnel publishes {parts.netloc} on the internet:[/] turn it off")
        if target is None:
            bad += 1
            console.print(
                f"[red]tailscale serve:[/] nothing on {parts.netloc} "
                f"(sudo tailscale serve --bg --https={port} {PHONE_LOCAL})"
            )
        elif target.rstrip("/") != PHONE_LOCAL:
            bad += 1
            console.print(f"[red]tailscale serve:[/] {parts.netloc} goes to {target}, not {PHONE_LOCAL}")
        else:
            console.print(f"[green]tailscale serve:[/] {parts.netloc} -> {target}, tailnet only")
        err = _ping(s.phone_url)
        if err:
            bad += 1
            console.print(f"[red]through the tailnet:[/] {s.phone_url}/m/ping fails ({err})")
        else:
            console.print(f"[green]through the tailnet:[/] {s.phone_url}/m/ answers")
    on = _call("GET", "/api/phone")["phones"]
    console.print(f"phones signed in: [bold]{len(on)}[/]" + (f" ({', '.join(p['by'] for p in on)})" if on else ""))
    if bad:
        raise typer.Exit(1)


@phone_app.command("pair")
def phone_pair(label: Annotated[str | None, typer.Option(help="The phone's name, e.g. pixel")] = None) -> None:
    """A one-time sign-in link (10 minutes) and its QR code: scan it with the phone, press Sign in."""
    import segno

    offer = _call("POST", "/api/phone/pair", json={"label": label})
    if not offer["tailnet"]:
        console.print(
            "[yellow]BOT_PHONE_URL is not set:[/] this link works in a browser on this computer only "
            "(uv run botctl phone setup)"
        )
    else:
        segno.make(offer["url"], error="m").terminal(compact=True)
    print(offer["url"])
    console.print(f"[dim]works once, for {int(offer['ttl_s'] // 60)} minutes; then: uv run botctl phone status[/]")


@phone_app.command("list")
def phone_list() -> None:
    """The phones signed in to the approvals."""
    phones = _call("GET", "/api/phone")["phones"]
    t = Table(box=None, header_style="bold")
    for col in ("id", "name", "signed in", "until", "seen", "decisions", "tailnet user"):
        t.add_column(col)
    for p in phones:
        t.add_row(
            p["id"],
            p["by"],
            _age(p["created"]),
            time.strftime("%Y-%m-%d", time.localtime(p["expires"])),
            _age(p["last_seen"]),
            str(p["decisions"]),
            p["tailnet_user"] or "",
        )
    console.print(t if phones else "no phone is signed in (uv run botctl phone pair)")


@phone_app.command("sign-out")
def phone_sign_out(
    phone_id: Annotated[str | None, typer.Argument(help="PHONE-01 (see botctl phone list)")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Every phone")] = False,
) -> None:
    """Sign a phone out (or all of them): its links stop opening the approvals at once."""
    if not phone_id and not all_:
        console.print("[red]name a phone (botctl phone list) or use --all[/]")
        raise typer.Exit(2)
    ids = [p["id"] for p in _call("GET", "/api/phone")["phones"]] if all_ else [phone_id]
    for pid in ids:
        p = _call("DELETE", f"/api/phone/{pid}")
        console.print(f"{p['by']} ({p['id']}) signed out")
    if all_ and not ids:
        console.print("no phone is signed in")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
