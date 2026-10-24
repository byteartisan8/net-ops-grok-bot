"""Approvals on the operator's phone: BOT_PHONE_URL, signing a phone in, the phone's pages and their guards, the
plan the operator saw, the NOC room's side (/api/phone), the NOC Manager's links, the two listeners."""

from __future__ import annotations

import asyncio
import re
import time
import warnings
from typing import Any

import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about httpx at import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from netsim_bot.api.app import create_app
from netsim_bot.api.phone import COOKIE, Listeners, create_phone_app
from netsim_bot.api.server import main as server_main
from netsim_bot.engine import Engine
from netsim_bot.models import Incident, Proposal
from netsim_bot.phone import PAIR_TTL_S, Phones, clean_label, form_token
from netsim_bot.settings import phone_url_of
from netsim_bot.store import Store

OP = {"X-Operator-Token": "op-token", "X-Operator": "pytest"}
PHONE_URL = "https://box.example.ts.net:8097"
TAILNET = {"Host": "box.example.ts.net:8097", "Origin": PHONE_URL}  # what tailscale serve passes on
LOCAL = {"Host": "127.0.0.1:8097", "Origin": "http://127.0.0.1:8097"}


@pytest.fixture
def made(fake_lab, settings_factory, tmp_path):
    """``made(**env)``: an engine with its office app and its phone app (as grok-bot serves them)."""

    def make(**env: str) -> Any:
        s = settings_factory(**{"BOT_PHONE_URL": PHONE_URL, **env})
        eng = Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=fake_lab("healthy").client())
        office = create_app(engine=eng, start_engine=False)
        phone = create_phone_app(lambda: eng)
        return eng, office, phone

    return make


def tickets(eng: Engine) -> None:
    """INC-0007 with two tickets the NOC Manager proposed (a field repair, a config change) and one in its review."""
    eng._save(Incident(id="INC-0007", title="hub-a powered off", status="awaiting-approval"))
    for pid, kind, plan in (
        (
            "CHG-0011",
            "apply-intent",
            {"plan_id": "plan-a", "diff": [{"change": "added", "message": "pw 1001 to hub-b"}]},
        ),
        ("CHG-0012", "field-repair", None),
    ):
        p = Proposal(id=pid, incident_id="INC-0007", kind=kind, host="hub-a", reason="the fix", proposed_by="NETENG")
        p.status, p.plan, p.params = "pending", plan, {"kind": "device"} if plan is None else {}
        p.reviews = [{"by": "NOCMGR", "decision": "propose", "reason": f"{pid}: fixes the root cause", "ts": 1.0}]
        eng.store.save_proposal(p)


def sign_in(eng: Engine, phone_client: TestClient, label: str = "pixel", headers: dict[str, str] = TAILNET) -> str:
    """Pair a phone as the NOC room does; returns the form token of its pages."""
    code = eng.phones.pair(label)["code"]
    r = phone_client.post(f"/m/pair/{code}", headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/?done=signed-in", r.text
    phone_client.cookies.set(COOKIE, r.cookies[COOKIE])
    page = phone_client.get("/m/", headers=headers)
    assert page.status_code == 200
    return re.search(r'name="t" value="([^"]+)"', page.text).group(1)


# ---------------------------------------------------------------------------------------------- settings
def test_the_phone_url_is_an_https_origin_on_a_tailnet_only_port(settings_factory) -> None:
    assert phone_url_of(None) == (None, None)
    assert phone_url_of("https://Box.Example.ts.net:8097/") == ("https://box.example.ts.net:8097", None)
    assert phone_url_of("http://127.0.0.1:8097") == ("http://127.0.0.1:8097", None)
    for bad in ("http://box.example.ts.net:8097", "https://box.example.ts.net:8097/m/", "ftp://x", "https://:1"):
        url, why = phone_url_of(bad)
        assert url is None and "BOT_PHONE_URL ignored" in why, bad
    url, why = phone_url_of("https://box.example.ts.net")  # 443: a port Funnel can publish
    assert url == "https://box.example.ts.net" and "Funnel" in why
    s = settings_factory(BOT_PHONE_URL="http://box.example.ts.net:8097")
    assert s.phone_url is None and any("must be https" in w for w in s.warnings)  # the office still starts
    s = settings_factory(BOT_PHONE_URL=PHONE_URL)
    assert s.phone_host == "box.example.ts.net" and s.public()["phone"] is True
    assert s.phone_links("CHG-0011", "INC-0007") == {
        "ticket": f"{PHONE_URL}/m/t/CHG-0011",
        "incident": f"{PHONE_URL}/m/i/INC-0007",
    }
    assert settings_factory().phone_links("CHG-0011", None) == {}
    assert settings_factory().public()["phone"] is False


# ---------------------------------------------------------------------------------------------- signing in
def test_a_sign_in_code_works_once_and_not_for_long(settings_factory, tmp_path, monkeypatch) -> None:
    s = settings_factory(BOT_PHONE_SESSION_DAYS="1")
    phones = Phones(Store(tmp_path / "p.db"), lambda: s)
    offer = phones.pair("My Pixel 8!")
    assert offer["label"] == "my-pixel-8" and offer["ttl_s"] == PAIR_TTL_S
    phone, token = phones.redeem(offer["code"], "Mozilla/5.0 (Linux; Android 15)", "op@example.com")
    assert phone.by == "phone:my-pixel-8" and phone.tailnet_user == "op@example.com"
    assert phones.redeem(offer["code"]) is None  # used up
    assert phones.signed_in(token).id == phone.id and phones.signed_in("nope") is None
    assert phone.token_hash not in str(phones.listed()) and token not in str(phones.listed())
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15"
    second, second_token = phones.redeem(phones.pair()["code"], iphone)
    assert second.label == "iphone"  # no name given: what its browser says it is
    third, third_token = phones.redeem(phones.pair()["code"])
    assert third.label == "browser"
    assert phones.sign_out(third.id).id == third.id
    assert phones.signed_in(third_token) is None  # its cookie stops working at once
    assert [p["id"] for p in phones.listed()] == [second.id, phone.id]  # signed out: forgotten
    with pytest.raises(KeyError):
        phones.sign_out(third.id)
    for _ in range(7):
        phones.pair()
    assert phones.open_pairings() == 5  # the oldest links go first
    old = phones.pair()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + PAIR_TTL_S + 1)
    assert phones.redeem(old["code"]) is None  # too old
    assert phones.open_pairings() == 0
    monkeypatch.setattr(time, "time", lambda: real() + 86_400 + 1)
    assert phones.signed_in(token) is None  # BOT_PHONE_SESSION_DAYS=1 ran out
    assert phones.listed() == []  # and the phones whose time ran out are forgotten
    assert phones.signed_in(second_token) is None
    assert clean_label("  ") == "" and clean_label("a" * 40) == "a" * 24


def test_signing_in_sets_a_cookie_secure_over_the_tailnet(made) -> None:
    eng, _office, phone = made()
    with TestClient(phone, base_url="http://127.0.0.1:8097") as m:
        code = eng.phones.pair("pixel")["code"]
        page = m.get(f"/m/pair/{code}", headers=TAILNET)
        assert page.status_code == 200 and "phone:pixel" in page.text and "Sign in" in page.text
        r = m.post(f"/m/pair/{code}", headers=TAILNET, follow_redirects=False)
        cookie = r.headers["set-cookie"]
        for part in ("HttpOnly", "Secure", "Path=/m", "SameSite=lax", "Max-Age=2592000"):
            assert part in cookie, cookie
        assert m.post(f"/m/pair/{code}", headers=TAILNET).status_code == 410  # works once
        assert m.get(f"/m/pair/{code}", headers=TAILNET).status_code == 410
        assert m.get("/m/pair/x", headers=TAILNET).status_code == 410
        code = eng.phones.pair()["code"]
        android = {**LOCAL, "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8)"}
        assert "as <b>phone:android</b>" in m.get(f"/m/pair/{code}", headers=android).text  # named by its browser
        local = m.post(f"/m/pair/{code}", headers=LOCAL, follow_redirects=False)
        assert "Secure" not in local.headers["set-cookie"]  # http on this machine
    assert [a.text for a in eng.store.activities()][-1] == "phone:browser signed in to the approvals"


# ---------------------------------------------------------------------------------------------- the guards
def test_the_phone_pages_need_a_signed_in_phone_and_refuse_the_internet(made) -> None:
    eng, _office, phone = made()
    tickets(eng)
    with TestClient(phone, base_url="http://127.0.0.1:8097") as m:
        assert m.get("/m/ping", headers=TAILNET).text == "ok"  # reachability, nothing else
        assert m.get("/m/phone.css", headers=TAILNET).headers["content-type"].startswith("text/css")
        for path in ("/m/", "/m/t/CHG-0011", "/m/i/INC-0007"):
            r = m.get(path, headers=TAILNET)
            assert r.status_code == 401 and "Sign in this phone" in r.text and "CHG-0011" not in r.text, path
        role = {**TAILNET, "Authorization": "Bearer nocmgr-token", "X-Operator-Token": "op-token"}
        assert m.get("/m/", headers=role).status_code == 401  # no token of the office opens the phone's pages
        assert m.get("/m/", headers={**TAILNET, "Tailscale-Funnel-Request": "?1"}).status_code == 403
        assert m.get("/m/ping", headers={"Host": "evil.example"}).status_code == 400
        assert m.get("/m/ping", headers={"Host": "box.example.ts.net.evil.example:8097"}).status_code == 400
        for path in ("/api/state", "/mcp", "/", "/static/js/main.js"):
            assert m.get(path, headers=TAILNET, follow_redirects=False).status_code in (303, 404), path
        assert m.get("/", headers=TAILNET, follow_redirects=False).headers["location"] == "/m/"
        r = m.get("/m/", headers={**TAILNET, "User-Agent": "Mozilla/5.0 (Linux; Android 15; wv) Grok Bot"})
        assert "intent://box.example.ts.net:8097/m/#Intent;scheme=https;package=com.android.chrome;end" in r.text
        token = sign_in(eng, m)
        r = m.get("/m/", headers=TAILNET)
        assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["referrer-policy"] == "same-origin"  # keeps Origin on the page's own POSTs
        form = {"t": token, "decision": "approve", "plan": "plan-a"}
        assert m.post("/m/t/CHG-0011", data=form, headers={"Host": TAILNET["Host"]}).status_code == 403  # no Origin
        evil = {**TAILNET, "Origin": "https://evil.example"}
        assert m.post("/m/t/CHG-0011", data=form, headers=evil).status_code == 403
        assert m.post("/m/t/CHG-0011", data={**form, "t": "forged"}, headers=TAILNET).status_code == 403
        assert m.post("/m/t/CHG-0011", data=form, headers={**TAILNET, "Content-Length": "999999"}).status_code == 413
        assert m.post("/m/t/CHG-0011", data=form, headers={**TAILNET, "Content-Length": "-1"}).status_code == 413
        assert eng.store.proposal("CHG-0011").status == "pending"


# ---------------------------------------------------------------------------------------------- deciding
def test_approve_and_reject_on_the_phone(made) -> None:
    eng, _office, phone = made()
    tickets(eng)
    with TestClient(phone, base_url="http://127.0.0.1:8097") as m:
        token = sign_in(eng, m)
        waiting = m.get("/m/", headers=TAILNET).text
        assert "Waiting for you" in waiting and "Approve INC-0007 (2 tickets, make before break)" in waiting
        page = m.get("/m/t/CHG-0011", headers=TAILNET).text
        assert "CHG-0011: fixes the root cause" in page and "+&nbsp;pw 1001 to hub-b" in page
        assert 'name="plan" value="plan-a"' in page and "Approve CHG-0011" in page
        stale = m.post("/m/t/CHG-0011", data={"t": token, "decision": "approve", "plan": "plan-b"}, headers=TAILNET)
        assert stale.status_code == 409 and "new plan since you looked" in stale.text
        assert eng.store.proposal("CHG-0011").status == "pending"
        r = m.post(
            "/m/t/CHG-0011",
            data={"t": token, "decision": "approve", "plan": "plan-a", "comment": "go", "back": "/m/i/INC-0007"},
            headers=TAILNET,
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/m/i/INC-0007?done=approved"
        p = eng.store.proposal("CHG-0011")
        assert (p.status, p.decided_by, p.comment) == ("approved", "phone:pixel", "go")
        assert eng._exec_queue.get_nowait() == "CHG-0011"  # the executor applies it next
        again = m.post("/m/t/CHG-0011", data={"t": token, "decision": "approve", "plan": "plan-a"}, headers=TAILNET)
        assert again.status_code == 409 and "approved, not pending" in again.text
        r = m.post(
            "/m/t/CHG-0012",
            data={"t": token, "decision": "reject", "plan": "", "comment": "wait", "back": "https://evil.example/"},
            headers=TAILNET,
            follow_redirects=False,
        )
        assert r.headers["location"] == "/m/t/CHG-0012?done=rejected"  # back only to the phone's own pages
        p = eng.store.proposal("CHG-0012")
        assert (p.status, p.decided_by, p.comment) == ("rejected", "phone:pixel", "wait")
        assert m.post("/m/t/CHG-0099", data={"t": token, "decision": "approve"}, headers=TAILNET).status_code == 404
        bogus = m.post("/m/t/CHG-0011", data={"t": token, "decision": "maybe"}, headers=TAILNET)
        assert bogus.status_code == 422
        followed = m.get("/m/t/CHG-0011?done=approved", headers=TAILNET).text
        assert "Approved. grok-bot applies it" in followed and 'http-equiv="refresh"' in followed
    assert eng.phones.listed()[0]["decisions"] == 2
    texts = [a.text for a in eng.store.activities()]
    assert any(t.startswith("CHG-0011 approved by phone:pixel") for t in texts)


def test_approve_an_incident_takes_exactly_the_tickets_the_phone_showed(made) -> None:
    eng, _office, phone = made()
    tickets(eng)
    with TestClient(phone, base_url="http://127.0.0.1:8097") as m:
        token = sign_in(eng, m)
        page = m.get("/m/i/INC-0007", headers=TAILNET).text
        expect = re.search(r'name="expect" value="([^"]+)"', page).group(1)
        assert expect == "{&quot;CHG-0012&quot;: null, &quot;CHG-0011&quot;: &quot;plan-a&quot;}"  # make before break
        late = Proposal(
            id="CHG-0013", incident_id="INC-0007", kind="field-repair", host="hub-b", reason="r", proposed_by="NOCENG"
        )
        late.status = "pending"
        eng.store.save_proposal(late)  # proposed after the page was shown
        shown = '{"CHG-0012": null, "CHG-0011": "plan-a"}'
        r = m.post("/m/i/INC-0007", data={"t": token, "expect": shown}, headers=TAILNET)
        assert r.status_code == 409 and "changed since you looked" in r.text
        assert eng.store.proposal("CHG-0011").status == "pending"
        assert m.post("/m/i/INC-0007", data={"t": token, "expect": "[]"}, headers=TAILNET).status_code == 422
        all_three = '{"CHG-0012": null, "CHG-0013": null, "CHG-0011": "plan-a"}'
        r = m.post("/m/i/INC-0007", data={"t": token, "expect": all_three}, headers=TAILNET, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/m/i/INC-0007?done=approved"
        assert {eng.store.proposal(i).decided_by for i in ("CHG-0011", "CHG-0012", "CHG-0013")} == {"phone:pixel"}
        assert eng.phones.listed()[0]["decisions"] == 3  # one per ticket
        assert m.post("/m/i/INC-0404", data={"t": token, "expect": all_three}, headers=TAILNET).status_code == 404
        assert m.get("/m/i/INC-0404", headers=TAILNET).status_code == 404


def test_a_ticket_in_review_has_no_buttons_and_signing_out_ends_it(made) -> None:
    eng, office, phone = made()
    tickets(eng)
    p = eng.store.proposal("CHG-0011")
    p.status = "review"
    eng.store.save_proposal(p)
    with TestClient(phone, base_url="http://127.0.0.1:8097") as m, TestClient(office) as c:
        token = sign_in(eng, m)
        page = m.get("/m/t/CHG-0011", headers=TAILNET).text
        assert "In NOCMGR's review" in page and "Approve CHG-0011" not in page
        assert "NOCMGR still reviews 1 ticket of INC-0007" in m.get("/m/i/INC-0007", headers=TAILNET).text
        [listed] = c.get("/api/phone", headers=OP).json()["phones"]
        assert c.delete(f"/api/phone/{listed['id']}", headers=OP).json()["by"] == "phone:pixel"  # the NOC room
        assert c.get("/api/phone", headers=OP).json()["phones"] == []
        assert m.get("/m/", headers=TAILNET).status_code == 401
        token = sign_in(eng, m, "pixel")
        r = m.post("/m/sign-out", data={"t": token}, headers=TAILNET)
        assert r.status_code == 200 and f"{COOKIE}=" in r.headers["set-cookie"]  # the cookie is cleared
        m.cookies.clear()
        assert m.get("/m/", headers=TAILNET).status_code == 401


# ---------------------------------------------------------------------------------------------- the NOC room's side
def test_the_room_signs_phones_in_with_the_operator_token(made) -> None:
    _eng, office, _phone = made()
    with TestClient(office) as c:
        assert c.get("/api/phone").status_code == 401
        assert c.post("/api/phone/pair", json={}).status_code == 401
        assert c.post("/api/phone/pair", json={}, headers={"X-Operator-Token": "nocmgr-token"}).status_code == 401
        r = c.post("/api/phone/pair", headers=OP, json={"label": "Pixel"}).json()
        assert r["url"].startswith(f"{PHONE_URL}/m/pair/") and r["tailnet"] is True and r["label"] == "pixel"
        assert r["qr_svg"].startswith("data:image/svg+xml") and r["ttl_s"] == PAIR_TTL_S
        st = c.get("/api/phone", headers=OP).json()
        assert st["url"] == PHONE_URL and st["open_links"] == 1 and st["phones"] == []
        assert c.delete("/api/phone/PHONE-99", headers=OP).status_code == 404
        assert c.post("/api/phone/pair", headers=OP, json={"bogus": 1}).status_code == 422
        # the office's page never answers on /m/
        assert c.get("/m/").status_code == 404
    _eng, office2, _phone = made(BOT_PHONE_URL="")
    with TestClient(office2) as c:
        r = c.post("/api/phone/pair", headers=OP).json()
        assert r["url"].startswith("http://127.0.0.1:8097/m/pair/") and r["tailnet"] is False


# ---------------------------------------------------------------------------------------------- the NOC Manager
def test_the_managers_proposal_carries_the_phone_links(made, settings_factory, fake_lab, tmp_path) -> None:
    async def review(eng: Engine) -> dict[str, Any]:
        p = Proposal(
            id="CHG-0021", incident_id="INC-0009", kind="field-repair", host="hub-a", reason="r", proposed_by="NOCENG"
        )
        eng._save(Incident(id="INC-0009"))
        eng.store.save_proposal(p)
        ok, out = await eng.gateway.call(
            "noc-manager", "review_ticket", {"proposal_id": "CHG-0021", "decision": "propose", "reason": "power on"}
        )
        assert ok, out
        return out

    eng, _office, _phone = made()
    out = asyncio.run(review(eng))
    assert out["phone"]["ticket"] == f"{PHONE_URL}/m/t/CHG-0021"
    assert out["phone"]["incident"] == f"{PHONE_URL}/m/i/INC-0009" and "approves nothing" in out["phone"]["note"]
    s = settings_factory()
    plain = Engine(settings_loader=lambda: s, store=Store(tmp_path / "plain.db"), lab=fake_lab("healthy").client())
    assert "phone" not in asyncio.run(review(plain))


# ---------------------------------------------------------------------------------------------- two listeners
def test_each_connection_goes_to_the_app_of_its_port() -> None:
    seen: list[str] = []

    def app(name: str):
        async def run(scope, receive, send) -> None:
            seen.append(f"{name}:{scope['type']}")

        return run

    split = Listeners(app("office"), app("phone"), 8097)
    for scope in (
        {"type": "http", "server": ("172.30.10.60", 8095)},
        {"type": "http", "server": ("172.30.10.60", 8097)},
        {"type": "websocket", "server": ("127.0.0.1", 8097)},
        {"type": "lifespan"},
        {"type": "http"},
    ):
        asyncio.run(split(scope, None, None))
    assert seen == ["office:http", "phone:http", "phone:websocket", "office:lifespan", "office:http"]
    assert form_token("a") != form_token("b") and len(form_token("a")) == 32
    with pytest.raises(SystemExit):
        server_main(["--port", "8095", "--phone-port", "8095"])
