"""Approvals on the operator's phone, on the live lab: hub-a's AC of SVC-0001 goes down -> the NOC Engineer files a
field ticket -> the NOC Manager proposes it, and its result carries the phone's links -> a phone signed in with a
one-time link approves it on grok-bot's phone listener (127.0.0.1:8097, with the Host and Origin tailscale serve
passes on) -> repaired -> recovered -> closed. On the way: no sign-in, the internet (Funnel), another site's form,
the office's tokens and the office's port are all refused."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx
import pytest
from netsim_bot.settings import NOCENG, NOCMGR, load_settings

HOST, IFACE = "hub-a", "GigabitEthernet0/0/2.101"  # SVC-0001's AC on its primary hub: the backup PW carries it
PHONE_LOCAL = "http://127.0.0.1:8097"  # grok-bot's phone listener on this machine (bot/compose.yaml)
ACTOR = "grok-bot/NOCENG"


class Phone:
    """A phone signed in to the approvals: what its browser sends through tailscale serve."""

    def __init__(self, http: httpx.Client, cookie: str) -> None:
        self.http = http
        self.cookie = {"Cookie": f"noc_phone={cookie}"}

    def get(self, path: str, **headers: str) -> httpx.Response:
        return self.http.get(path, headers={**self.cookie, **headers})

    def post(self, path: str, data: dict[str, str], **headers: str) -> httpx.Response:
        return self.http.post(path, data=data, headers={**self.cookie, **headers})

    def form_token(self, path: str) -> str:
        page = self.get(path)
        assert page.status_code == 200, page.text[:300]
        return re.search(r'name="t" value="([^"]+)"', page.text).group(1)


@pytest.fixture
def phone(bot) -> Iterator[Phone]:
    """A phone signed in with a sign-in link from the operator API (the NOC room's button), signed out at the end."""
    s = load_settings()
    origin = s.phone_url or PHONE_LOCAL
    headers = {"Host": urlsplit(origin).netloc, "Origin": origin, "User-Agent": "Mozilla/5.0 (Linux; Android 15) e2e"}
    with httpx.Client(base_url=PHONE_LOCAL, headers=headers, timeout=30) as http:
        assert http.get("/m/ping").text == "ok", f"no phone listener at {PHONE_LOCAL} (make bot-up)"
        offer = bot.post("/api/phone/pair", {"label": "bot-e2e"})
        assert offer["tailnet"] is bool(s.phone_url) and offer["qr_svg"].startswith("data:image/svg+xml")
        r = http.post(urlsplit(offer["url"]).path)
        assert r.status_code == 303, r.text[:300]
        cookie = re.search(r"noc_phone=([^;]+)", r.headers["set-cookie"]).group(1)
        assert http.post(urlsplit(offer["url"]).path).status_code == 410  # the link works once
        signed = Phone(http, cookie)
        try:
            yield signed
        finally:
            for p in bot.get("/api/phone")["phones"]:
                if p["label"] == "bot-e2e":
                    assert bot.http.delete(f"/api/phone/{p['id']}").status_code == 200
            assert signed.get("/m/").status_code == 401  # signed out: the cookie is dead


def test_a_field_ticket_approved_on_the_phone(bot, roles, sink, green, fault, lab_api, phone, timing, wait) -> None:
    s = load_settings()
    green()
    # the guards first: the office's port has no /m/, the phone's port nothing of the office
    assert bot.http.get("/m/").status_code == 404
    assert phone.get("/api/state").status_code == 404 and phone.get("/mcp").status_code == 404
    bare = phone.http.get("/m/")  # no cookie
    assert bare.status_code == 401 and "Sign in this phone" in bare.text
    office_tokens = {"Authorization": f"Bearer {s.role_tokens[NOCMGR]}", "X-Operator-Token": s.operator_token or ""}
    assert phone.http.get("/m/", headers=office_tokens).status_code == 401
    assert phone.get("/m/", **{"Tailscale-Funnel-Request": "?1"}).status_code == 403  # never from the internet
    assert phone.get("/m/ping", Host="evil.example").status_code == 400

    t0 = fault("ac", HOST, interface=IFACE)
    page = sink.wait(NOCENG, "incident.opened", t0, timeout=60)
    inc_id = page["body"]["incident"]["id"]
    eng = roles[NOCENG]
    eng.call("take_incident", incident_id=inc_id)
    eng.call("ack_zabbix_problems", incident_id=inc_id, message="bot-e2e: AC down on hub-a, field ticket")
    t_ticket = time.time()
    chg = eng.call(
        "propose_field_repair", incident_id=inc_id, kind="ac", host=HOST, interface=IFACE, reason="AC down, admin up"
    )["proposal_id"]
    review = sink.wait(NOCMGR, "tickets.filed", t_ticket, timeout=45)
    assert chg in [t["id"] for t in review["body"]["tickets"]]
    out = roles[NOCMGR].call("review_ticket", proposal_id=chg, decision="propose", reason="bot-e2e: the field fix")
    if s.phone_url:  # what the NOC Manager puts at the end of its report
        assert out["phone"]["ticket"] == f"{s.phone_url}/m/t/{chg}"
        assert out["phone"]["incident"] == f"{s.phone_url}/m/i/{inc_id}"
    else:
        assert "phone" not in out

    waiting = phone.get("/m/").text
    assert inc_id in waiting and chg in waiting and f"Approve {inc_id}" in waiting
    token = phone.form_token(f"/m/t/{chg}")
    form = {"t": token, "decision": "approve", "plan": "", "back": f"/m/i/{inc_id}"}
    evil = phone.post(f"/m/t/{chg}", form, Origin="https://evil.example")
    assert evil.status_code == 403  # another site's form
    assert phone.post(f"/m/t/{chg}", {**form, "t": "forged"}).status_code == 403
    assert bot.proposal(chg)["status"] == "pending"

    t_approve = time.time()
    r = phone.post(f"/m/t/{chg}", form)
    assert r.status_code == 303 and r.headers["location"] == f"/m/i/{inc_id}?done=approved", r.text[:300]
    done = bot.until_decided(chg, timeout=90)
    timing("phone approval -> field ticket applied", time.time() - t_approve, note="incl. 10 s field delay")
    assert done["status"] == "applied" and done["decided_by"] == "phone:bot-e2e"
    restored = lab_api.get("/audit", params={"since": t_approve, "action": "fault.restore"}).json()
    assert [(e["actor"], e["target"]) for e in restored] == [(f"{ACTOR} approved-by:phone:bot-e2e", HOST)]
    assert "applied" in phone.get(f"/m/t/{chg}").text
    rec = sink.wait(NOCENG, "incident.recovered", t_approve, timeout=90, incident_id=inc_id)
    assert rec["body"]["incident"]["id"] == inc_id
    assert eng.call("close_incident", incident_id=inc_id, summary="AC of SVC-0001 on hub-a repaired")["ok"]
    wait(lambda: not bot.incidents(), 10, "no active incident")
    [listed] = [p for p in bot.get("/api/phone")["phones"] if p["label"] == "bot-e2e"]
    assert listed["decisions"] == 1
    green()
