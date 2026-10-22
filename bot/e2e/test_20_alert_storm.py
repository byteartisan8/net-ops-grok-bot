"""An alert storm: hub-a powered off -> one incident and one NOC Engineer page for its six customers -> take, ack in
Zabbix, triage, field ticket -> a human approves -> dispatched, on site, repaired -> one recovered page -> closed."""

from __future__ import annotations

import time

from netsim_bot.settings import NOCENG, NOCMGR

ACTOR = "grok-bot/NOCENG"
CUSTOMERS = ["Customer-A", "Customer-B", "Customer-C", "Customer-D", "Customer-E", "Customer-F"]


def test_hub_a_power_off_is_one_incident_one_page(
    bot, roles, sink, green, fault, lab_api, zabbix_admin, timing, wait
) -> None:
    green()
    t0 = fault("device", "hub-a")
    page = sink.wait(NOCENG, "incident.opened", t0, timeout=60)
    timing("hub-a power-off -> NOC Engineer page sent", page["received"] - t0, 30)
    inc_id = page["body"]["incident"]["id"]
    assert sorted(sink.customers(page)) == sorted(CUSTOMERS)
    counts = page["body"]["incident"]["counts"]
    assert counts == {"down": 1, "degraded": 5, "ok": 0}  # Customer-F (bronze, unprotected) down; the rest on backups
    assert {"incident", "how_to_respond", "links"} <= set(page["body"]) and inc_id in page["body"]["how_to_respond"]

    # the storm stays one incident and one page: no other page arrives in the next batch window
    time.sleep(12)
    assert sink.events(t0) == [(NOCENG, "incident.opened", inc_id)]  # no gold customer is down: no NOC Manager page
    assert [i["id"] for i in bot.incidents()] == [inc_id]
    alerts = bot.alerts_since(t0)
    assert {a["customer"] for a in alerts} == set(CUSTOMERS)
    assert {a["incident_id"] for a in alerts} == {inc_id}

    eng = roles[NOCENG]
    assert eng.call("take_incident", incident_id=inc_id)["incident"]["assignee"] == "NOCENG"
    view = eng.call("get_incident", incident_id=inc_id)
    assert sorted(view["customers"]) == sorted(CUSTOMERS)
    ack = eng.call("ack_zabbix_problems", incident_id=inc_id, message="bot-e2e: hub-a down, field ticket next")
    assert len(ack["acknowledged"]) >= 6, ack
    events = zabbix_admin.call(
        "event.get", {"eventids": ack["acknowledged"], "output": ["eventid"], "selectAcknowledges": "extend"}
    )
    assert len(events) == len(ack["acknowledged"])
    for e in events:  # what the Zabbix frontend shows: acknowledged by noc-engineer, with the message
        assert any(a["username"] == "noc-engineer" and "bot-e2e" in a["message"] for a in e["acknowledges"]), e
    audit = lab_api.get("/audit", params={"since": t0, "action": "zabbix.acknowledge"}).json()
    assert [e["actor"] for e in audit] == [ACTOR]
    eng.call(
        "submit_triage",
        incident_id=inc_id,
        summary="hub-a is unreachable: every PW to it is down. Gold and silver ride their backups through hub-b; "
        "Customer-F (bronze, unprotected) is down.",
        suspected_cause="hub-a powered off",
        category="physical-fault",
        customer_impact="gold: 3 degraded; silver: 2 degraded; bronze: 1 down",
        next_step="field ticket: power hub-a on",
    )
    t_ticket = time.time()
    chg = eng.call("propose_field_repair", incident_id=inc_id, kind="device", host="hub-a", reason="hub-a is off")
    chg_id = chg["proposal_id"]
    assert chg["status"] == "pending"

    # the NOC Manager hears about the ticket (batched) and recommends; it stays pending until the human decides
    mgr_page = sink.wait(NOCMGR, "tickets.filed", t_ticket, timeout=45)
    assert chg_id in [t["id"] for t in mgr_page["body"]["tickets"]]
    roles[NOCMGR].call("recommend_ticket", proposal_id=chg_id, recommendation="approve", reason="the only fix")
    p = bot.proposal(chg_id)
    assert p["status"] == "pending" and p["recommendations"][0]["recommendation"] == "approve"

    t_approve = time.time()
    bot.approve(chg_id)
    done = bot.until_decided(chg_id, timeout=120)
    assert done["status"] == "applied", done
    stages = [(s["stage"], s["ts"]) for s in done["progress"]]
    assert [s for s, _ in stages] == ["dispatched", "on-site", "repaired"]
    assert stages[1][1] - stages[0][1] >= 5  # BOT_FIELD_DELAY_S on the way to the site
    restored = lab_api.get("/audit", params={"since": t_approve, "action": "fault.restore"}).json()
    assert [(e["actor"], e["target"]) for e in restored] == [(f"{ACTOR} approved-by:bot-e2e", "hub-a")]

    rec = sink.wait(NOCENG, "incident.recovered", t_approve, timeout=150, incident_id=inc_id)
    timing("hub-a field ticket approved -> recovered page", rec["received"] - t_approve, note="10 s field delay + boot")
    time.sleep(5)
    recovered = [e for e in sink.events(t0) if e[1] == "incident.recovered"]
    assert recovered == [(NOCENG, "incident.recovered", inc_id)]
    assert eng.call("close_incident", incident_id=inc_id, summary="hub-a powered off; field tech powered it on")["ok"]
    assert bot.incident(inc_id)["status"] == "closed"
    wait(lambda: not bot.incidents(), 10, "no active incident")
    green()
