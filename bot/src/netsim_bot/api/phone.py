"""The approvals page for the operator's phone: its own listener (``BOT_PHONE_PORT``, 8097), reached from this
machine (``127.0.0.1``) and from the user's tailnet (``tailscale serve --https=8097``), never from the internet.

A Bot of the Grok Bot app proposes nothing here and approves nothing: the NOC Manager's ``review_ticket`` result
carries links to these pages (``BOT_PHONE_URL``), the operator taps one in the app on their phone, and the page asks
for what only the operator has, a phone signed in to the office (:mod:`netsim_bot.phone`). The Bot's cloud computer
is not on the tailnet, and the role tokens are not accepted here.

* ``GET /m/``: what waits for the operator, by incident; ``GET /m/t/<CHG>``: one ticket (the diff, the services it
  touches, the predicted drift, the NOC Manager's reason) with Approve and Reject; ``GET /m/i/<INC>``: an incident
  and its tickets, with Approve all (make before break). Every decision names the plan the page showed: a ticket
  whose plan changed since, or an incident with other tickets waiting now, is refused (409) and shown again.
* ``GET|POST /m/pair/<code>``: sign a phone in with a one-time code from the NOC room or ``botctl phone pair``;
  ``POST /m/sign-out``. ``GET /m/ping``: ``ok`` (is the listener reachable through the tailnet?).
* The guards: a request that came through Tailscale Funnel (``Tailscale-Funnel-Request``) is refused; the ``Host``
  must be ``BOT_PHONE_URL``'s host or a loopback name; a POST must come from this page (``Origin``) and carry the
  phone's form value; every page is ``no-store``, cannot be framed and loads nothing from elsewhere.

:class:`Listeners` sends each connection to the office's app or to this one by the port it came in on.
"""

from __future__ import annotations

import hmac
import html
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from netsim_bot.engine import Engine, change_order
from netsim_bot.incidents import customer_order
from netsim_bot.models import Activity, Incident, Phone, Proposal
from netsim_bot.phone import device_label, form_token
from netsim_bot.settings import LOOPBACK_NAMES

COOKIE = "noc_phone"
LOCAL_URL = "http://127.0.0.1:8097"  # the phone listener on this machine (bot/compose.yaml)
CSS = Path(__file__).resolve().parent.parent / "web" / "css" / "phone.css"
TICKET_ID = re.compile(r"^CHG-\d{1,8}$")
INCIDENT_ID = re.compile(r"^INC-\d{1,8}$")
CODE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
BACK = re.compile(r"^/m/(t/CHG-\d{1,8}|i/INC-\d{1,8})?$")  # where a decision may return to
MAX_BODY = 64_000
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
ICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect x='2' y='3' width='12' height='9' rx='2' fill='%23e7ebf2'/%3E"
    "%3Crect x='4' y='5' width='8' height='5' fill='%23121d2c'/%3E"
    "%3Crect x='5' y='6' width='2' height='2' fill='%237ff6ff'/%3E"
    "%3Crect x='9' y='6' width='2' height='2' fill='%237ff6ff'/%3E"
    "%3Crect x='7' y='0' width='2' height='3' fill='%234fc3f7'/%3E%3C/svg%3E"
)
STATUS_TEXT = {
    "review": "in NOCMGR's review",
    "pending": "waits for you",
    "approved": "approved",
    "applying": "applying",
    "applied": "applied",
    "failed": "failed",
    "rejected": "rejected",
    "returned": "returned",
    "obsolete": "obsolete",
    "awaiting-approval": "awaiting approval",
}
DONE_TEXT = {
    "approved": "Approved. grok-bot applies it and verifies it now: this page follows it.",
    "rejected": "Rejected. The ticket stays as it is; the engineers see your reason.",
    "signed-in": "This phone is signed in. Links from the NOC Manager open here.",
}


def qr_data_uri(url: str) -> str:
    """The QR code of a sign-in link, as an SVG ``data:`` URI for an ``<img>``."""
    import segno

    return segno.make(url, error="m").svg_data_uri(scale=6, border=2, dark="#000", light="#fff")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86_400:
        return f"{s // 3600} h ago"
    return f"{s // 86_400} d ago"


def texts(items: Any) -> list[str]:
    """Drift lists as text: the engine keeps their messages (``slim_plan``); a raw lab-api item has ``message``."""
    return [i if isinstance(i, str) else str((i or {}).get("message", "")) for i in items or []]


def hostname(header: str) -> str:
    """The host of a ``Host`` header, without the port (``[::1]:8097`` -> ``::1``)."""
    h = header.strip().lower()
    if h.startswith("["):
        return h[1 : h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class Listeners:
    """The ASGI app grok-bot serves: a connection that came in on ``phone_port`` goes to the phone's app, any other
    one (and the lifespan) to the office's. The phone's app never sees the office's routes, nor the other way round."""

    def __init__(self, office: ASGIApp, phone: ASGIApp, phone_port: int) -> None:
        self.office = office
        self.phone = phone
        self.phone_port = phone_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        server = scope.get("server") or (None, None)
        if scope["type"] != "lifespan" and self.phone_port and server[1] == self.phone_port:
            await self.phone(scope, receive, send)
        else:
            await self.office(scope, receive, send)


# ---------------------------------------------------------------------------------------------- the pages
def page(
    title: str,
    body: str,
    phone: Phone | None = None,
    token: str | None = None,
    refresh: int | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    who = f'<span class="who">{esc(phone.by)}</span>' if phone else ""
    out = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
{f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""}
<title>{esc(title)} · NetSim NOC</title>
<link rel="icon" href="{ICON}">
<link rel="stylesheet" href="/m/phone.css">
</head>
<body>
<header class="bar"><a class="brand" href="/m/">NetSim NOC <small>approvals</small></a>{who}</header>
<main>
{body}
</main>
{footer(token) if phone and token else ""}
</body>
</html>
"""
    return HTMLResponse(out, status_code=status_code, headers=HEADERS)


def footer(token: str) -> str:
    return (
        '<footer><form method="post" action="/m/sign-out">'
        f'<input type="hidden" name="t" value="{esc(form_token(token))}">'
        '<button class="link" type="submit">Sign this phone out</button></form>'
        "<p>Only you approve: the roles file and review tickets, this page is yours.</p></footer>"
    )


def flash(text: str, kind: str = "ok") -> str:
    return f'<div class="flash {esc(kind)}" role="status">{esc(text)}</div>'


def pill(status: str) -> str:
    return f'<span class="pill s-{esc(status)}">{esc(STATUS_TEXT.get(status, status))}</span>'


def field_text(p: Proposal) -> str:
    kind = p.params.get("kind")
    if kind == "device":
        return f"power on {p.host} (the field technician goes on site)"
    if kind == "uplink":
        return f"repair the core uplink of {p.host}"
    if kind == "cut":
        return f"repair the core path {p.host} <-> {p.params.get('peer')}"
    if kind == "ac":
        return f"repair attachment circuit {p.params.get('interface')} on {p.host}"
    return json.dumps(p.params)


def review_line(p: Proposal) -> str:
    rev = p.reviews[-1] if p.reviews else None
    if p.status == "review":
        filer = esc(p.proposed_by)
        return f'<p class="rev wait">🗳️ In NOCMGR\'s review: it comes to you once proposed, or goes back to {filer}.</p>'
    if not rev:
        return ""
    by, reason = esc(rev.get("by")), esc(rev.get("reason"))
    if rev.get("recommendation"):
        return f'<p class="rev">🗳️ {by} recommended to {esc(rev["recommendation"])}: {reason}</p>'
    if rev.get("decision") == "skip":
        return f'<p class="rev skip">⏭️ <b>Not reviewed</b> by NOCMGR: {reason}</p>'
    if rev.get("decision") == "return":
        return f'<p class="rev ret">↩️ {by} returned it to {esc(p.proposed_by)}: {reason}</p>'
    return f'<p class="rev">🗳️ <b>{by} proposes it:</b> {reason}</p>'


def diff_block(p: Proposal, limit: int | None = None) -> str:
    if p.kind == "field-repair":
        return f'<div class="diff field">🚐 {esc(field_text(p))}</div>'
    rows = (p.plan or {}).get("diff") or []
    if not rows:
        return ""
    lines = []
    for d in rows[:limit] if limit else rows:
        change = d.get("change")
        cls, sign = {"added": ("add", "+"), "removed": ("del", "-")}.get(change, ("chg", "~"))
        lines.append(f'<span class="{cls}">{sign}&nbsp;{esc(d.get("message") or d.get("path"))}</span>')
    more = f'<span class="more">… {len(rows) - limit} more</span>' if limit and len(rows) > limit else ""
    return f'<div class="diff">{"".join(lines)}{more}</div>'


def stages(p: Proposal) -> str:
    if p.kind != "field-repair" or not p.progress:
        return ""
    done = {x.get("stage"): x for x in p.progress}
    out = []
    for s in ("dispatched", "on-site", "repaired"):
        cls = "done" if s in done else ""
        out.append(f'<span class="stage {cls}">{esc(s.replace("-", " "))}{" ✓" if s in done else ""}</span>')
    return f'<div class="stages">{"<i>→</i>".join(out)}</div>'


def outcome(p: Proposal) -> str:
    out = []
    if p.decided_by:
        verb = "rejected" if p.status == "rejected" else "approved"
        comment = f": {p.comment}" if p.comment else ""
        out.append(f'<p class="meta">{verb} by <b>{esc(p.decided_by)}</b> {esc(ago(p.decided))}{esc(comment)}</p>')
    result = p.result or {}
    if result.get("verdict"):
        drift = texts(result.get("drift_after"))
        out.append(
            f'<p class="verdict v-{esc(result["verdict"])}">verdict <b>{esc(result["verdict"])}</b>'
            f"{' · drift after: ' + esc('; '.join(drift)) if drift else ''}</p>"
        )
    if result.get("done"):
        out.append(f'<p class="verdict v-ok">{esc(result["done"])}</p>')
    if p.error:
        out.append(f'<p class="verdict v-failed">{esc(p.error)}</p>')
    return "".join(out)


def ticket_card(p: Proposal, token: str | None, *, full: bool, back: str) -> str:
    """One ticket; with ``token`` and status pending, its Approve and Reject (the form names the plan shown)."""
    plan = p.plan or {}
    services = plan.get("affected_services") or []
    drift = texts(plan.get("drift_after_predicted"))
    facts = [f"filed by <b>{esc(p.proposed_by)}</b> {esc(ago(p.created))}"]
    if p.incident_id:
        facts.append(f'<a href="/m/i/{esc(p.incident_id)}">{esc(p.incident_id)}</a>')
    body = [
        f'<article class="card ticket s-{esc(p.status)}" id="{esc(p.id)}">',
        f'<div class="row"><a class="id" href="/m/t/{esc(p.id)}">{esc(p.id)}</a>{pill(p.status)}</div>',
        f'<h2 class="headline">{esc(p.headline())}</h2>',
        f'<p class="meta">{" · ".join(facts)}</p>',
        review_line(p),
        diff_block(p, None if full else 4),
    ]
    if full:
        body.append(f'<p class="why"><b>Why ({esc(p.proposed_by)}):</b> {esc(p.reason)}</p>')
        if services:
            body.append(f'<p class="meta">touches {esc(", ".join(services))}</p>')
        if p.kind != "field-repair":
            body.append(f'<p class="meta">drift after (predicted): {esc("; ".join(drift) if drift else "none")}</p>')
    body.append(stages(p))
    body.append(outcome(p))
    if p.status == "pending" and token:
        if full:
            body.append(decision_form(p, token, back))
        else:
            body.append(f'<a class="btn go" href="/m/t/{esc(p.id)}">Review and decide &rsaquo;</a>')
    body.append("</article>")
    return "".join(body)


def decision_form(p: Proposal, token: str, back: str) -> str:
    plan_id = (p.plan or {}).get("plan_id") or ""
    return f"""<form class="decide" method="post" action="/m/t/{esc(p.id)}">
<input type="hidden" name="t" value="{esc(form_token(token))}">
<input type="hidden" name="plan" value="{esc(plan_id)}">
<input type="hidden" name="back" value="{esc(back)}">
<label for="c-{esc(p.id)}">Comment (optional)</label>
<textarea id="c-{esc(p.id)}" name="comment" rows="2" maxlength="500" placeholder="Shown on the ticket"></textarea>
<button class="btn ok" type="submit" name="decision" value="approve">Approve {esc(p.id)}</button>
<button class="btn no" type="submit" name="decision" value="reject">Reject</button>
<p class="fine">plan {esc(plan_id or "-")}</p>
</form>"""


def approve_all_form(inc_id: str, pending: list[Proposal], token: str) -> str:
    expect = {p.id: (p.plan or {}).get("plan_id") for p in pending}
    what = f"{len(pending)} tickets, make before break" if len(pending) > 1 else pending[0].id
    return f"""<form method="post" action="/m/i/{esc(inc_id)}">
<input type="hidden" name="t" value="{esc(form_token(token))}">
<input type="hidden" name="expect" value="{esc(json.dumps(expect))}">
<button class="btn ok" type="submit">Approve {esc(inc_id)} ({esc(what)})</button>
</form>"""


def customers_line(inc: Incident) -> str:
    bad = [n for n in customer_order(inc) if (inc.customers[n].get("state") or "ok") != "ok"]
    if not bad:
        return "every customer OK"
    shown = [f"{n} ({inc.customers[n].get('tier') or '?'}) {inc.customers[n].get('state')}" for n in bad[:4]]
    more = f" and {len(bad) - 4} more" if len(bad) > 4 else ""
    return ", ".join(shown) + more


def incident_head(inc: Incident, link: bool) -> str:
    title = f'<a href="/m/i/{esc(inc.id)}">{esc(inc.id)}</a>' if link else esc(inc.id)
    pills = [pill(inc.status)]
    if inc.priority:
        pills.append(f'<span class="pill prio">{esc(inc.priority)}</span>')
    return (
        f'<div class="row"><span class="id">{title}</span><span>{"".join(pills)}</span></div>'
        f'<p class="title">{esc(inc.title)}</p><p class="meta">{esc(customers_line(inc))}</p>'
    )


def sign_in_help(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    in_app = "; wv)" in ua  # an Android WebView: an app's own browser, without Chrome's cookies
    chrome = ""
    if in_app:
        where = f"{request.headers.get('host', '')}{request.url.path}"
        target = f"intent://{where}#Intent;scheme=https;package=com.android.chrome;end"
        chrome = (
            "<p>This page opened in an app's own browser, which does not have your sign-in. "
            f'<a class="btn go" href="{esc(target)}">Open it in Chrome</a></p>'
        )
    return f"""<h1>Sign in this phone</h1>
<p>This browser is not signed in to the NOC's approvals.</p>
{chrome}
<ol class="steps">
<li>On your computer, open the NOC room, tab <b>Approvals</b>, and press <b>Sign in a phone</b>
(or run <code>uv run botctl phone pair</code>).</li>
<li>Scan the QR code with this phone's camera and press <b>Sign in</b>.</li>
</ol>
<p class="fine">The phone must be on your tailnet (the Tailscale app, signed in).
Nothing here is on the internet.</p>"""


# ---------------------------------------------------------------------------------------------- the app
def create_phone_app(engine_of: Callable[[], Engine | None]) -> FastAPI:
    """The phone's app; ``engine_of`` gives the office's engine (it lives in the office app's state)."""
    app = FastAPI(
        title="NetSim NOC: approvals for the operator's phone", docs_url=None, redoc_url=None, openapi_url=None
    )

    def engine() -> Engine:
        eng = engine_of()
        if eng is None:
            raise StarletteHTTPException(503, "grok-bot is starting")
        return eng

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> Response:
        if request.headers.get("tailscale-funnel-request"):
            return PlainTextResponse("The phone page is not on the internet.", status_code=403, headers=HEADERS)
        eng = engine_of()
        phone_host = eng.settings.phone_host if eng else None
        host = hostname(request.headers.get("host", ""))
        if host not in LOOPBACK_NAMES and host != phone_host:
            return PlainTextResponse("Invalid host header", status_code=400, headers=HEADERS)
        length = request.headers.get("content-length") or "0"
        if not length.isdigit() or int(length) > MAX_BODY:
            return PlainTextResponse("Too large.", status_code=413, headers=HEADERS)
        if request.method == "POST" and not same_origin(request, eng.settings.phone_url if eng else None):
            return PlainTextResponse(
                "This form must be sent from the NOC's own page.", status_code=403, headers=HEADERS
            )
        return await call_next(request)

    def signed_in(request: Request) -> tuple[Phone, str] | None:
        token = request.cookies.get(COOKIE)
        phone = engine().phones.signed_in(token)
        return (phone, token) if phone and token else None

    def must_sign_in(request: Request) -> HTMLResponse:
        return page("Sign in", sign_in_help(request), status_code=401)

    async def form(request: Request) -> dict[str, str]:
        raw = (await request.body())[:20_000].decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def form_ok(fields: dict[str, str], token: str) -> bool:
        return hmac.compare_digest(fields.get("t", "").encode(), form_token(token).encode())

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_req: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return page("Not found", '<h1>Not here</h1><p><a href="/m/">What waits for you</a></p>', status_code=404)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=HEADERS)

    @app.get("/", include_in_schema=False)
    @app.get("/m", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/m/", status_code=303, headers=HEADERS)

    @app.get("/m/ping")
    async def ping() -> PlainTextResponse:
        return PlainTextResponse("ok", headers=HEADERS)

    @app.get("/m/phone.css")
    async def css() -> Response:
        return Response(
            CSS.read_text(encoding="utf-8"),
            media_type="text/css",
            headers={**HEADERS, "Cache-Control": "max-age=300"},
        )

    # ------------------------------------------------------------------ what waits
    @app.get("/m/")
    async def waiting(request: Request, done: str | None = None) -> Response:
        who = signed_in(request)
        if who is None:
            return must_sign_in(request)
        phone, token = who
        eng = engine()
        pending = sorted(eng.store.proposals(status="pending", limit=200), key=change_order)
        review = eng.store.proposals(status="review", limit=200)
        out = [flash(DONE_TEXT[done]) if done in DONE_TEXT else "", "<h1>Waiting for you</h1>"]
        if not pending:
            out.append('<div class="empty"><span>✅</span>Nothing waits for your approval.</div>')
        by_incident: dict[str, list[Proposal]] = {}
        for p in pending:
            by_incident.setdefault(p.incident_id or "-", []).append(p)
        groups = [(i, eng.incidents.get(i) or eng.store.incident(i), t) for i, t in by_incident.items()]
        groups.sort(key=lambda g: ((g[1].priority or "P9") if g[1] else "P9", -(g[1].created if g[1] else 0)))
        for inc_id, inc, tickets in groups:  # P1 first, then the newest
            out.append('<section class="card incident">')
            if inc is not None:
                out.append(incident_head(inc, link=True))
                if not [p for p in review if p.incident_id == inc_id]:
                    out.append(approve_all_form(inc_id, tickets, token))
                else:
                    out.append('<p class="rev wait">🗳️ NOCMGR still reviews more tickets of this incident.</p>')
            else:
                out.append('<div class="row"><span class="id">Tickets without an incident</span></div>')
            out.extend(ticket_card(p, token, full=False, back="/m/") for p in tickets)
            out.append("</section>")
        if review:
            more = f"{len(review)} more ticket{'s' if len(review) != 1 else ''}"
            out.append(f'<p class="fine">🗳️ NOCMGR reviews {more}: they come here once proposed.</p>')
        decided = [p for p in eng.store.proposals(limit=40) if p.status not in ("pending", "review")][:6]
        if decided:
            out.append('<h2 class="section">Recent</h2><ul class="recent">')
            for p in decided:
                by = esc(p.decided_by or p.proposed_by)
                out.append(
                    f'<li><a href="/m/t/{esc(p.id)}">{esc(p.id)}</a> {pill(p.status)} '
                    f'<span class="meta">{esc(p.headline())} · {by} · {esc(ago(p.updated))}</span></li>'
                )
            out.append("</ul>")
        return page("Waiting for you", "".join(out), phone, token, refresh=30)

    # ------------------------------------------------------------------ one ticket
    @app.get("/m/t/{proposal_id}")
    async def ticket(request: Request, proposal_id: str, done: str | None = None) -> Response:
        who = signed_in(request)
        if who is None:
            return must_sign_in(request)
        phone, token = who
        p = engine().store.proposal(proposal_id) if TICKET_ID.match(proposal_id) else None
        if p is None:
            raise StarletteHTTPException(404)
        return ticket_page(p, phone, token, flash(DONE_TEXT[done]) if done in DONE_TEXT else "")

    def ticket_page(p: Proposal, phone: Phone, token: str, note: str, status_code: int = 200) -> HTMLResponse:
        back = (
            f'<a class="back" href="/m/i/{esc(p.incident_id)}">&lsaquo; {esc(p.incident_id)}</a>'
            if p.incident_id
            else ""
        )
        back = back or '<a class="back" href="/m/">&lsaquo; Waiting for you</a>'
        refresh = 3 if p.status in ("approved", "applying") else None
        body = f"{back}{note}{ticket_card(p, token, full=True, back=f'/m/t/{p.id}')}"
        return page(p.id, body, phone, token, refresh=refresh, status_code=status_code)

    @app.post("/m/t/{proposal_id}")
    async def decide(request: Request, proposal_id: str) -> Response:
        who = signed_in(request)
        if who is None:
            return must_sign_in(request)
        phone, token = who
        fields = await form(request)
        if not form_ok(fields, token):
            return PlainTextResponse("This form is out of date: open the page again.", status_code=403, headers=HEADERS)
        eng = engine()
        if not TICKET_ID.match(proposal_id) or eng.store.proposal(proposal_id) is None:
            raise StarletteHTTPException(404)
        decision = fields.get("decision")
        comment = (fields.get("comment") or "").strip()[:500] or None
        try:
            if decision == "approve":
                await eng.approve(proposal_id, phone.by, comment, plan_id=fields.get("plan") or None)
            elif decision == "reject":
                await eng.reject(proposal_id, phone.by, comment)
            else:
                return PlainTextResponse("decision must be approve or reject", status_code=422, headers=HEADERS)
        except ValueError as exc:
            p = eng.store.proposal(proposal_id)
            assert p is not None
            return ticket_page(p, phone, token, flash(str(exc), "bad"), status_code=409)
        eng.phones.decided(phone)
        done = "approved" if decision == "approve" else "rejected"
        back = fields.get("back") or ""
        back = back if BACK.match(back) else f"/m/t/{quote(proposal_id)}"
        return RedirectResponse(f"{back}?done={done}", status_code=303, headers=HEADERS)

    # ------------------------------------------------------------------ an incident
    @app.get("/m/i/{incident_id}")
    async def incident(request: Request, incident_id: str, done: str | None = None) -> Response:
        who = signed_in(request)
        if who is None:
            return must_sign_in(request)
        phone, token = who
        return incident_page(incident_id, phone, token, flash(DONE_TEXT[done]) if done in DONE_TEXT else "")

    def incident_page(incident_id: str, phone: Phone, token: str, note: str, status_code: int = 200) -> HTMLResponse:
        eng = engine()
        inc = (
            (eng.incidents.get(incident_id) or eng.store.incident(incident_id))
            if INCIDENT_ID.match(incident_id)
            else None
        )
        if inc is None:
            raise StarletteHTTPException(404)
        tickets = sorted(eng.store.proposals(incident_id=inc.id, limit=200), key=change_order)
        pending = [p for p in tickets if p.status == "pending"]
        in_review = [p for p in tickets if p.status == "review"]
        out = ['<a class="back" href="/m/">&lsaquo; Waiting for you</a>', note, '<section class="card incident">']
        out.append(incident_head(inc, link=False))
        if inc.triage:
            out.append(
                f'<p class="why"><b>Triage ({esc(inc.assignee or "NOCENG")}):</b> {esc(inc.triage.get("summary"))}</p>'
            )
        if inc.root_cause:
            out.append(f'<p class="why"><b>Root cause:</b> {esc(inc.root_cause.get("summary"))}</p>')
        if inc.priority_reason:
            out.append(f'<p class="why"><b>{esc(inc.priority)} (NOCMGR):</b> {esc(inc.priority_reason)}</p>')
        if pending and not in_review:
            out.append(approve_all_form(inc.id, pending, token))
        elif in_review:
            n = len(in_review)
            out.append(
                f'<p class="rev wait">🗳️ NOCMGR still reviews {n} ticket{"s" if n != 1 else ""} of {esc(inc.id)}.</p>'
            )
        out.append("</section>")
        if not tickets:
            out.append('<div class="empty"><span>📭</span>No change ticket for this incident.</div>')
        open_ = [p for p in tickets if p.status in ("pending", "review", "approved", "applying")]
        closed = [p for p in tickets if p not in open_]
        out.extend(ticket_card(p, token, full=True, back=f"/m/i/{inc.id}") for p in open_)
        if closed:
            out.append('<h2 class="section">Done or set aside</h2>')
            out.extend(ticket_card(p, token, full=False, back=f"/m/i/{inc.id}") for p in closed)
        busy = any(p.status in ("approved", "applying") for p in tickets)
        return page(inc.id, "".join(out), phone, token, refresh=3 if busy else None, status_code=status_code)

    @app.post("/m/i/{incident_id}")
    async def approve_all(request: Request, incident_id: str) -> Response:
        who = signed_in(request)
        if who is None:
            return must_sign_in(request)
        phone, token = who
        fields = await form(request)
        if not form_ok(fields, token):
            return PlainTextResponse("This form is out of date: open the page again.", status_code=403, headers=HEADERS)
        eng = engine()
        if not INCIDENT_ID.match(incident_id):
            raise StarletteHTTPException(404)
        try:
            expect = json.loads(fields.get("expect") or "null")
        except ValueError:
            expect = None
        if not isinstance(expect, dict) or not expect:
            return PlainTextResponse("the form must name the tickets you saw", status_code=422, headers=HEADERS)
        try:
            done = await eng.approve_incident(
                incident_id, phone.by, (fields.get("comment") or "").strip() or None, expect
            )
        except KeyError as exc:
            raise StarletteHTTPException(404) from exc
        except ValueError as exc:
            return incident_page(incident_id, phone, token, flash(str(exc), "bad"), status_code=409)
        for _ in done:
            eng.phones.decided(phone)
        return RedirectResponse(f"/m/i/{quote(incident_id)}?done=approved", status_code=303, headers=HEADERS)

    # ------------------------------------------------------------------ sign in, sign out
    @app.get("/m/pair/{code}")
    async def pair_page(request: Request, code: str) -> Response:
        offer = engine().phones.pairing(code) if CODE.match(code) else None
        if offer is None:
            return page("Sign in", gone_text(), status_code=410)
        name = f"phone:{offer['label'] or device_label(request.headers.get('user-agent'))}"
        minutes = max(1, round((offer["expires"] - time.time()) / 60))
        body = f"""<h1>Sign in this phone</h1>
<p>Approve and reject the NOC's change tickets on this phone, as <b>{esc(name)}</b>.</p>
<form method="post" action="/m/pair/{esc(code)}"><button class="btn ok" type="submit">Sign in</button></form>
<p class="fine">This link works once, for {minutes} more minute{"s" if minutes != 1 else ""}.</p>"""
        return page("Sign in", body)

    @app.post("/m/pair/{code}")
    async def pair(request: Request, code: str) -> Response:
        eng = engine()
        got = (
            eng.phones.redeem(code, request.headers.get("user-agent"), request.headers.get("tailscale-user-login"))
            if CODE.match(code)
            else None
        )
        if got is None:
            return page("Sign in", gone_text(), status_code=410)
        phone, token = got
        host = hostname(request.headers.get("host", ""))
        s = eng.settings
        secure = bool(s.phone_url and s.phone_url.startswith("https://") and host == s.phone_host)
        eng.emit(
            Activity(bot="operator", kind="control", text=f"{phone.by} signed in to the approvals", station="operator")
        )
        r = RedirectResponse("/m/?done=signed-in", status_code=303, headers=HEADERS)
        r.set_cookie(
            COOKIE,
            token,
            max_age=int(s.phone_session_days * 86_400),
            path="/m",
            httponly=True,
            samesite="lax",
            secure=secure,
        )
        return r

    @app.post("/m/sign-out")
    async def sign_out(request: Request) -> Response:
        who = signed_in(request)
        if who is None:
            return must_sign_in(request)
        phone, token = who
        if not form_ok(await form(request), token):
            return PlainTextResponse("This form is out of date: open the page again.", status_code=403, headers=HEADERS)
        eng = engine()
        eng.phones.sign_out(phone.id)
        eng.emit(
            Activity(bot="operator", kind="control", text=f"{phone.by} signed out of the approvals", station="operator")
        )
        r = page("Signed out", "<h1>Signed out</h1><p>This phone no longer approves anything.</p>")
        r.delete_cookie(COOKIE, path="/m")
        return r

    return app


def gone_text() -> str:
    return (
        "<h1>This sign-in link is used up</h1><p>A sign-in link works once, for 10 minutes. Make a new one in the "
        "NOC room (Approvals, <b>Sign in a phone</b>) or with <code>uv run botctl phone pair</code>.</p>"
    )


def same_origin(request: Request, phone_url: str | None) -> bool:
    """A POST's ``Origin`` (or, without one, its ``Referer``) is this page: the request's own host, or
    ``BOT_PHONE_URL``. Browsers set both; another site cannot."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if parts.netloc.lower() == request.headers.get("host", "").lower():
        return True
    return bool(phone_url) and f"{parts.scheme}://{parts.netloc.lower()}" == phone_url
