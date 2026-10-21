"""The bot's loop on the live lab: notice -> diagnose -> propose -> (a human approves) -> fix -> verify -> resolved."""

from __future__ import annotations

import time


def _approve_all(bot, incident_id: str) -> list[dict]:
    return bot.post(f"/api/incidents/{incident_id}/approve-all", {"comment": "bot-e2e"})


def test_vcid_typo_is_fixed_with_netbox_intent(bot, lab_api, green, scenario) -> None:
    green()
    t0 = scenario("vcid-typo")
    inc = bot.new_incident(t0)
    assert "SVC-0001" in inc["services"]
    inc = bot.until_status(inc["id"], ("awaiting-approval",))
    report = inc["report"]
    assert report["category"] == "config-drift"
    assert "vc-id" in report["root_cause"] and "spoke-1" in report["root_cause"]
    pending = [p for p in inc["proposals"] if p["status"] == "pending"]
    assert [(p["kind"], p["host"]) for p in pending] == [("apply-intent", "spoke-1")]
    diff = pending[0]["plan"]["diff"]
    assert [(d["path"], d["from"], d["to"]) for d in diff] == [
        ("l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id", 2010, 2001)
    ]
    t_approve = time.time() - 1
    _approve_all(bot, inc["id"])
    inc = bot.until_status(inc["id"], ("resolved",), timeout=150)
    done = next(p for p in inc["proposals"] if p["id"] == pending[0]["id"])
    assert done["status"] == "applied" and done["result"]["verdict"] == "ok"
    applied = list(lab_api.get("/audit", params={"since": t_approve, "action": "apply-intent.apply"}).json())
    assert (
        len(applied) == 1 and applied[0]["target"] == "spoke-1" and applied[0]["actor"].endswith("approved-by:bot-e2e")
    )
    assert lab_api.get("/drift").json()["drift"] is False
    kinds = {a["kind"] for a in inc["activities"]}
    assert {"alarm", "assign", "tool", "propose", "report", "approve", "apply", "verify", "resolve"} <= kinds
    green()


def test_fake_redundancy_is_fixed_make_before_break(bot, lab_api, green, scenario) -> None:
    green()
    t0 = scenario("fake-redundancy")
    inc = bot.new_incident(t0)
    inc = bot.until_status(inc["id"], ("awaiting-approval",))
    assert inc["report"]["category"] == "config-drift" and "redundancy lost" in inc["report"]["root_cause"]
    pending = sorted(p["host"] for p in inc["proposals"] if p["status"] == "pending")
    assert pending == ["hub-a", "hub-b", "spoke-2"]
    t_approve = time.time() - 1
    approved = _approve_all(bot, inc["id"])
    assert [p["host"] for p in approved] == ["hub-a", "spoke-2", "hub-b"]
    bot.until_status(inc["id"], ("resolved",), timeout=200)
    applied = lab_api.get("/audit", params={"since": t_approve, "action": "apply-intent.apply"}).json()
    assert [e["target"] for e in applied] == ["hub-a", "spoke-2", "hub-b"]  # the order the executor ran them
    assert applied[-1]["result"]["verdict"] == "ok"
    assert lab_api.get("/drift").json()["drift"] is False
    green()


def test_hub_down_sends_the_field_technician(bot, lab_api, green, scenario) -> None:
    green()
    t0 = scenario("hub-down")
    inc = bot.new_incident(t0)
    inc = bot.until_status(inc["id"], ("awaiting-approval",))
    assert inc["report"]["category"] == "physical-fault"
    assert inc["report"]["impact_by_tier"] == {"gold": {"down": 0, "degraded": 5}, "silver": {"down": 0, "degraded": 2}}
    pending = [p for p in inc["proposals"] if p["status"] == "pending"]
    assert [(p["kind"], p["host"], p["params"]) for p in pending] == [("field-repair", "hub-b", {"kind": "device"})]
    _approve_all(bot, inc["id"])
    inc = bot.until_status(inc["id"], ("resolved",), timeout=180)
    assert lab_api.get("/faults").json() == {"faults": [], "stopped": []}
    kinds = [(a["bot"], a["kind"]) for a in inc["activities"]]
    assert ("PATCH", "travel") in kinds and ("PATCH", "return") in kinds
    green()


def test_rejected_fix_and_maintenance_mode(bot, lab_api, green, scenario) -> None:
    green()
    t0 = scenario("spoke-isolated")
    inc = bot.new_incident(t0)
    inc = bot.until_status(inc["id"], ("awaiting-approval",))
    p = next(p for p in inc["proposals"] if p["status"] == "pending")
    assert (p["kind"], p["host"], p["params"]) == ("field-repair", "spoke-2", {"kind": "uplink"})
    bot.post(f"/api/proposals/{p['id']}/reject", {"comment": "e2e: not now"})
    inc = bot.until_status(inc["id"], ("diagnosed",), timeout=30)
    green()  # the fault is repaired by hand: the incident resolves, nothing was applied by the bot
    assert bot.incident(inc["id"])["status"] == "resolved"
    # maintenance mode: faults open no incident
    bot.control(paused=True, reason="bot-e2e maintenance")
    try:
        t1 = scenario("primary-fail")
        time.sleep(30)
        assert not [i for i in bot.get("/api/incidents") if i["created"] >= t1]
    finally:
        bot.control(paused=False)
    inc = bot.new_incident(t1, timeout=60)  # the problems are still there: now the watcher takes them
    assert "SVC-0010" in inc["services"]
    green()


def test_chat_answers_with_the_lab_data(bot, green, wait) -> None:
    green()
    msg = bot.post("/api/chat", {"message": "What happens if hub-c goes down for maintenance?"})

    def answer():
        rows = bot.get("/api/chat", params={"chat_id": msg["chat_id"]})
        return next((m for m in rows if m["role"] == "bot"), None)

    a = wait(answer, 60, "a chat answer")
    assert "hub-c" in a["text"] and "SVC-0009" in a["text"] and "bronze" in a["text"]


def test_the_noc_page(bot) -> None:
    r = bot.http.get("/")
    assert r.status_code == 200 and "NetSim NOC" in r.text and '"token"' in r.text
    for path in ("/static/js/main.js", "/static/js/room.js", "/static/css/noc.css"):
        assert bot.http.get(path).status_code == 200
    with bot.http.stream("GET", "/api/stream", timeout=20) as s:
        for line in s.iter_lines():
            if line.startswith("event: hello"):
                break
    st = bot.get("/api/state")
    assert st["lab"]["ok"] and len(st["lab"]["services"]) == 11 and len(st["lab"]["devices"]) == 10
