"""``botctl``: the NOC bot from the terminal (status, incidents, approvals, questions, live activity, control).

Talks to grok-bot's API (``BOT_URL``, default ``http://127.0.0.1:8095``) with ``BOT_OPERATOR_TOKEN`` from ``.env``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from netsim_bot.settings import load_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
console = Console()

SEV_STYLE = {
    "Disaster": "bold white on red",
    "High": "red",
    "Average": "dark_orange",
    "Warning": "yellow",
    "Information": "cyan",
    "Not classified": "dim",
}
STATUS_STYLE = {
    "open": "bold yellow",
    "investigating": "cyan",
    "diagnosed": "magenta",
    "awaiting-approval": "bold magenta",
    "remediating": "bold blue",
    "monitoring": "blue",
    "resolved": "green",
    "pending": "bold magenta",
    "approved": "blue",
    "applying": "bold blue",
    "applied": "green",
    "failed": "bold red",
    "rejected": "dim",
    "obsolete": "dim",
}


def _client() -> httpx.Client:
    s = load_settings()
    url = os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")
    headers = {"X-Operator": f"botctl:{os.environ.get('USER', 'operator')}"}
    token = s.operator_token
    if not token and (s.data_dir / "operator-token").is_file():  # a bot run on this host without .env's token
        token = (s.data_dir / "operator-token").read_text(encoding="utf-8").strip()
    if token:
        headers["X-Operator-Token"] = token
    return httpx.Client(base_url=url, headers=headers, timeout=60)


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


@app.command()
def status() -> None:
    """Brain, autonomy, crew, open incidents and pending approvals."""
    st = _call("GET", "/api/state")
    eff = st["effective"]
    brain = f"[bold]{eff['model']}[/]" if eff["brain"] == "grok" else f"[yellow]rules[/] ({eff.get('why')})"
    console.print(
        f"brain {brain} | autonomy [bold]{eff['autonomy']}[/] | "
        + ("[bold yellow]MAINTENANCE MODE[/] | " if eff["paused"] else "")
        + f"lab {'ok' if st['lab']['ok'] else '[red]' + str(st['lab']['error']) + '[/]'} | "
        f"Grok today ${st['usage'].get('usd_day', 0):.4f}"
    )
    crew = Table(box=None, header_style="bold")
    for col in ("bot", "role", "status", "doing"):
        crew.add_column(col)
    for m in st["crew"]:
        crew.add_row(m["name"], m["role"], m["status"], (m.get("activity") or "")[:90])
    console.print(crew)
    _incident_table([i for i in st["incidents"] if i["status"] != "resolved"], "open incidents")
    pending = [p for p in st["proposals"] if p["status"] == "pending"]
    if pending:
        _proposal_table(pending, "waiting for approval (botctl approve <id>)")


def _incident_table(rows: list[dict[str, Any]], title: str) -> None:
    if not rows:
        console.print(f"[green]{title}: none[/]")
        return
    t = Table(title=title, header_style="bold", title_justify="left")
    for col in ("id", "sev", "status", "title", "services", "brain", "age"):
        t.add_column(col)
    for i in rows:
        t.add_row(
            i["id"],
            f"[{SEV_STYLE.get(i['severity'], '')}]{i['severity']}[/]",
            f"[{STATUS_STYLE.get(i['status'], '')}]{i['status']}[/]",
            i["title"][:70],
            ", ".join(i["services"][:4]) + ("…" if len(i["services"]) > 4 else ""),
            i.get("model") or "-",
            _age(i["created"]),
        )
    console.print(t)


def _proposal_table(rows: list[dict[str, Any]], title: str) -> None:
    t = Table(title=title, header_style="bold", title_justify="left")
    for col in ("id", "incident", "status", "kind", "host", "change", "by"):
        t.add_column(col)
    for p in rows:
        diff = (p.get("plan") or {}).get("diff") or []
        change = "; ".join(d.get("message", "") for d in diff[:2]) or json.dumps(p.get("params"))
        t.add_row(
            p["id"],
            p.get("incident_id") or "-",
            f"[{STATUS_STYLE.get(p['status'], '')}]{p['status']}[/]",
            p["kind"],
            p["host"],
            change[:80],
            p["proposed_by"],
        )
    console.print(t)


@app.command()
def incidents(all_: Annotated[bool, typer.Option("--all", help="Include resolved ones")] = False) -> None:
    """Incidents (open ones, or all recent with --all)."""
    rows = _call("GET", "/api/incidents", params={"limit": 50, "active": not all_})
    _incident_table(rows, "incidents" if all_ else "open incidents")


@app.command()
def show(incident_id: str) -> None:
    """One incident: report, proposals and timeline."""
    inc = _call("GET", f"/api/incidents/{incident_id}")
    console.print(
        f"[bold]{inc['id']}[/] [{SEV_STYLE.get(inc['severity'], '')}]{inc['severity']}[/] "
        f"[{STATUS_STYLE.get(inc['status'], '')}]{inc['status']}[/]  {inc['title']}"
    )
    r = inc.get("report")
    if r:
        console.print(f"[bold]summary[/] {r.get('summary')}")
        console.print(
            f"[bold]root cause[/] {r.get('root_cause')}  [dim]({r.get('category')}, {r.get('confidence')})[/]"
        )
        if r.get("impact_by_tier"):
            console.print(f"[bold]impact by tier[/] {json.dumps(r['impact_by_tier'])}")
        for e in r.get("evidence") or []:
            console.print(f"  • {e}")
        console.print(f"[bold]recommendation[/] {r.get('recommendation')}")
    if inc.get("proposals"):
        _proposal_table(inc["proposals"], "proposals")
    t = Table(title="timeline", header_style="bold", title_justify="left", box=None)
    for col in ("time", "bot", "what"):
        t.add_column(col)
    for a in inc.get("activities") or []:
        if a["kind"] in ("think",):
            continue
        t.add_row(time.strftime("%H:%M:%S", time.localtime(a["ts"])), a["bot"], f"{a['kind']}: {a['text']}"[:120])
    console.print(t)
    if inc.get("usage"):
        u = inc["usage"]
        console.print(
            f"[dim]Grok: {u.get('calls', 0)} calls, {u.get('input_tokens', 0)} in / "
            f"{u.get('output_tokens', 0)} out, ${u.get('usd', 0):.4f}[/]"
        )


@app.command()
def approve(
    proposal_id: Annotated[str | None, typer.Argument(help="CHG-xxxx")] = None,
    incident: Annotated[str | None, typer.Option(help="Approve every pending proposal of this incident")] = None,
    comment: Annotated[str | None, typer.Option(help="Why")] = None,
) -> None:
    """Approve a proposal (it is applied and verified), or every pending one of an incident."""
    if incident:
        done = _call("POST", f"/api/incidents/{incident}/approve-all", json={"comment": comment})
        console.print(f"approved {len(done)}: {', '.join(p['id'] for p in done)}")
    elif proposal_id:
        p = _call("POST", f"/api/proposals/{proposal_id}/approve", json={"comment": comment})
        console.print(f"[green]{p['id']} approved[/]: the executor applies and verifies it now")
    else:
        console.print("[red]give a proposal id or --incident[/]")
        raise typer.Exit(1)


@app.command()
def reject(proposal_id: str, comment: Annotated[str | None, typer.Option(help="Why")] = None) -> None:
    """Reject a proposal."""
    p = _call("POST", f"/api/proposals/{proposal_id}/reject", json={"comment": comment})
    console.print(f"{p['id']} rejected")


@app.command()
def ask(
    message: str,
    chat: Annotated[str | None, typer.Option(help="Continue this conversation id")] = None,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for the answer")] = 180,
) -> None:
    """Ask the NOC bots a question (e.g. 'what happens if hub-c goes down for maintenance?')."""
    msg = _call("POST", "/api/chat", json={"message": message, "chat_id": chat})
    chat_id = msg["chat_id"]
    console.print(f"[dim]{chat_id}: asked; waiting for a bot…[/]")
    deadline = time.time() + timeout
    seen_activity = None
    while time.time() < deadline:
        time.sleep(1.0)
        acts = _call("GET", "/api/activities", params={"after": seen_activity, "limit": 50}) if seen_activity else []
        for a in acts:
            if a.get("chat_id") == chat_id and a["kind"] in ("tool", "say"):
                console.print(f"[dim]  {a['bot']}: {a['text'][:110]}[/]")
        if acts:
            seen_activity = acts[-1]["seq"]
        elif seen_activity is None:
            last = _call("GET", "/api/activities", params={"limit": 1})
            seen_activity = last[-1]["seq"] if last else 0
        msgs = _call("GET", "/api/chat", params={"chat_id": chat_id})
        answers = [m for m in msgs if m["role"] == "bot" and m["ts"] >= msg["ts"]]
        if answers:
            a = answers[-1]
            console.print(f"[bold]{a['bot']}[/]: {a['text']}")
            if a.get("proposals"):
                console.print(f"proposals: {', '.join(a['proposals'])} (botctl approve <id>)")
            console.print(f'[dim]follow up with: botctl ask --chat {chat_id} "..."[/]')
            return
    console.print("[yellow]no answer yet; see the NOC page[/]")


@app.command()
def watch() -> None:
    """Live activity of the crew (Ctrl-C to stop)."""
    s = os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")
    with httpx.stream("GET", f"{s}/api/stream", timeout=None) as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event in ("activity", "incident", "proposal"):
                ev = json.loads(line[5:])
                d = ev.get("data", {})
                stamp = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", time.time())))
                if event == "activity" and d.get("kind") != "think":
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
                    )


@app.command()
def pause(reason: Annotated[str | None, typer.Option(help="Why (shown in the room)")] = None) -> None:
    """Maintenance mode: the bots watch, but open no incidents."""
    _call("POST", "/api/control", json={"paused": True, "reason": reason})
    console.print("maintenance mode on")


@app.command()
def resume() -> None:
    """Leave maintenance mode."""
    _call("POST", "/api/control", json={"paused": False})
    console.print("maintenance mode off")


@app.command()
def brain(choice: Annotated[str, typer.Argument(help="auto, grok, rules or default (.env)")]) -> None:
    """Switch the responders' brain at runtime."""
    r = _call("POST", "/api/control", json={"brain": choice})
    console.print(f"brain now: {r['effective']['model']} ({r['effective'].get('why') or 'ok'})")


@app.command()
def autonomy(choice: Annotated[str, typer.Argument(help="observe, propose, auto or default (.env)")]) -> None:
    """Switch autonomy at runtime (auto = fixes are applied without a human; lab only)."""
    r = _call("POST", "/api/control", json={"autonomy": choice})
    console.print(f"autonomy now: {r['effective']['autonomy']}")


@app.command("grok-check")
def grok_check() -> None:
    """One function-calling round trip with the configured xAI key and model."""
    r = _call("POST", "/api/grok/check")
    if r.get("ok"):
        console.print(
            f"[green]Grok OK[/]: {r['model']} called {r['tool_calls']} and answered {r.get('answer')!r} "
            f"in {r['seconds']} s"
        )
    else:
        console.print(f"[red]Grok not OK[/]: {r.get('error')}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------------------------- outside agents
def _mcp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One JSON-RPC call to ``/mcp`` with ``BOT_AGENT_TOKEN``: the same door the Grok Bot app uses."""
    s = load_settings()
    if not s.agent_token:
        console.print("[red]BOT_AGENT_TOKEN is not set in .env[/] (make env adds one)")
        raise typer.Exit(2)
    url = os.environ.get("BOT_URL", "http://127.0.0.1:8095").rstrip("/")
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    headers = {"Authorization": f"Bearer {s.agent_token}", "Accept": "application/json, text/event-stream"}
    try:
        r = httpx.post(f"{url}/mcp", json=body, headers=headers, timeout=330)
    except httpx.HTTPError as exc:
        console.print(f"[red]grok-bot not reachable at {url}: {exc}[/] (make bot-up)")
        raise typer.Exit(2) from exc
    if r.status_code >= 400:
        console.print(f"[red]HTTP {r.status_code}: {r.text[:300]}[/]")
        raise typer.Exit(1)
    data = r.json()
    if "error" in data:
        console.print(f"[red]{data['error'].get('message')}[/]")
        raise typer.Exit(1)
    return data["result"]


@app.command()
def tools() -> None:
    """The tools an outside agent (the Grok Bot app, any MCP client) gets at /mcp."""
    t = Table(header_style="bold", box=None)
    for col in ("tool", "kind", "what"):
        t.add_column(col)
    for tool in _mcp("tools/list")["tools"]:
        kind = "read" if tool["annotations"]["readOnlyHint"] else "write"
        what = tool["description"].split(". ")[0].split(": ")[0]
        t.add_row(tool["name"], kind, what if len(what) <= 90 else what[:88].rsplit(" ", 1)[0] + " …")
    console.print(t)


@app.command()
def call(
    tool: Annotated[str, typer.Argument(help="Tool name (botctl tools lists them)")],
    args: Annotated[str | None, typer.Argument(help='Arguments as JSON, e.g. \'{"host": "hub-a"}\'')] = None,
    arg: Annotated[list[str] | None, typer.Option("--arg", "-a", help="key=value (JSON value if it parses)")] = None,
) -> None:
    """Call one NOC tool as the outside agent and print its JSON result (exit 1 if the tool reported an error).

    This is how the Grok Bot app works from your computer without a tunnel: it runs `uv run botctl call ...` in the
    repository (you allow each command), and its visitor robot walks through the NOC room."""
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
    result = _mcp("tools/call", {"name": tool, "arguments": arguments})
    out = result.get("structuredContent")
    print(json.dumps(out, indent=2) if out is not None else result["content"][0]["text"])
    if result.get("isError"):
        raise typer.Exit(1)


@app.command("mcp-config")
def mcp_config(
    url: Annotated[str, typer.Option(help="The URL the agent reaches /mcp at (a tunnel's, for a cloud agent)")] = (
        "http://127.0.0.1:8095/mcp"
    ),
    show_token: Annotated[bool, typer.Option("--show-token", help="Print the real token, not a placeholder")] = False,
) -> None:
    """The "MCP server JSON" to paste into the Grok Bot app (its MCP Servers settings: "Add a custom MCP")."""
    s = load_settings()
    token = s.agent_token if show_token and s.agent_token else "<BOT_AGENT_TOKEN from .env>"
    print(json.dumps({"netsim-noc": {"url": url, "headers": {"Authorization": f"Bearer {token}"}}}, indent=2))
    if not s.agent_token:
        console.print("[yellow]BOT_AGENT_TOKEN is not set in .env yet (make env adds one)[/]")


pager_app = typer.Typer(no_args_is_help=True, help="The pager: one webhook per incident (e.g. a Grok Bot routine).")
app.add_typer(pager_app, name="pager")


@pager_app.command("status")
def pager_status() -> None:
    """Configuration, mute state and the last deliveries."""
    st = _call("GET", "/api/pager")
    state = "[yellow]muted[/]" if st["muted"] else ("[green]on[/]" if st["configured"] else "[dim]off (no URL)[/]")
    console.print(
        f"pager {state} | events {', '.join(st['events'])} | min severity {st['min_severity']} | "
        f"max {st['max_per_hour']}/h" + (" | [yellow]maintenance mode[/]" if st["paused"] else "")
    )
    t = Table(header_style="bold", box=None)
    for col in ("time", "event", "incident", "status", "detail"):
        t.add_column(col)
    for rec in st["history"][:15]:
        detail = rec.get("why") or (f"HTTP {rec['http']}" if rec.get("http") else "") or rec.get("error") or ""
        if rec.get("error") and rec.get("status") == "failed":
            detail = f"{detail} {rec['error']}"[:100]
        style = {"sent": "green", "failed": "red", "skipped": "dim"}.get(rec["status"], "")
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(rec["ts"])),
            rec["event"],
            rec.get("incident_id") or "-",
            f"[{style}]{rec['status']}[/]",
            detail,
        )
    console.print(t)


@pager_app.command("test")
def pager_test() -> None:
    """Send one test page now (checks BOT_PAGER_URL and BOT_PAGER_TOKEN)."""
    rec = _call("POST", "/api/pager/test")
    if rec.get("status") == "sent":
        console.print(f"[green]test page sent[/]: HTTP {rec.get('http')} in {rec.get('seconds')} s")
    else:
        console.print(f"[red]test page failed[/]: {rec.get('http') or ''} {rec.get('error')}")
        raise typer.Exit(1)


@pager_app.command("mute")
def pager_mute() -> None:
    """Stop paging (incidents still open and get worked on in the room)."""
    _call("POST", "/api/control", json={"pager_muted": True})
    console.print("pager muted")


@pager_app.command("unmute")
def pager_unmute() -> None:
    """Page again."""
    _call("POST", "/api/control", json={"pager_muted": False})
    console.print("pager on")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
