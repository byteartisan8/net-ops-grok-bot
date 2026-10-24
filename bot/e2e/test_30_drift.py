"""Config drift: fake redundancy (both PWs of SVC-0002 on hub-b) -> Customer-A degraded -> the NOC Engineer
escalates -> the Network Engineer asks the controller (spoke-2, hub-a, hub-b out of sync, spoke-2's diff), files the
root cause quoting the diff and proposes NetBox's intent on the three hosts -> the NOC Manager returns one (its
engineer is paged and files it again) and proposes them -> a human approves the incident -> applied
make-before-break and verified -> Customer-A OK -> recovered page; the controller shows the three in sync again."""

from __future__ import annotations

import time

from netsim_bot.settings import NETENG, NOCENG, NOCMGR


def test_fake_redundancy_goes_to_the_network_engineer(bot, roles, sink, green, scenario, lab_api, timing, wait):
    green()
    t0 = scenario("fake-redundancy")
    page = sink.wait(NOCENG, "incident.opened", t0, timeout=60)
    timing("fake-redundancy -> NOC Engineer page sent", page["received"] - t0, 30)
    inc_id = page["body"]["incident"]["id"]
    assert sink.customers(page) == ["Customer-A"]
    customer = page["body"]["incident"]["customers"][0]
    assert customer["state"] == "degraded" and [s["service"] for s in customer["services"]] == ["SVC-0002"]

    eng = roles[NOCENG]
    eng.call("take_incident", incident_id=inc_id)
    eng.call("ack_zabbix_problems", incident_id=inc_id, message="bot-e2e: SVC-0002 config drift, escalating")
    eng.call(
        "submit_triage",
        incident_id=inc_id,
        summary="SVC-0002 is degraded: Zabbix says hub-a lost its PW and spoke-2's primary points to hub-b.",
        category="config-drift",
        next_step="escalate to the Network Engineer",
    )
    t_esc = time.time()
    eng.call("escalate", incident_id=inc_id, to=NETENG, note="SVC-0002: both PWs on hub-b? Config, not physical.")
    net_page = sink.wait(NETENG, "incident.escalated", t_esc, timeout=20, incident_id=inc_id)
    assert net_page["body"]["escalation"]["from"] == "NOCENG" and "hub-b" in net_page["body"]["escalation"]["note"]
    assert net_page["body"]["triage"]["category"] == "config-drift"

    net = roles[NETENG]
    finding = next(f for f in net.call("get_drift")["findings"] if f["service"] == "SVC-0002")
    assert finding["kind"] == "redundancy-lost" and sorted(finding["hosts"]) == ["hub-a", "hub-b", "spoke-2"]
    # the controller: the three routers differ from its copy, the others do not; spoke-2's diff names the move
    after = bot.last_event_id()
    for host in ("spoke-2", "hub-a", "hub-b"):
        assert net.call("controller_cli", command=f"show devices {host} check")["summary"] == "out of sync", host
    assert net.call("controller_cli", command="show devices hub-c check")["summary"] == "in sync"
    diff = net.call("controller_cli", command="show devices spoke-2 diff")
    assert diff["user"] == "lab-api" and diff["output"].startswith("spoke-2:")
    assert '-           peer-ipv4 "10.255.0.2";' in diff["output"]  # the router now: hub-b
    assert '+           peer-ipv4 "10.255.0.1";' in diff["output"]  # the controller's copy: hub-a
    seen = bot.stream(after, {"terminal"}, lambda got: len([e for e in got if e["bot"] == "NETENG"]) >= 5)
    assert [e["command"] for e in seen if e["bot"] == "NETENG"][-1] == "show devices spoke-2 diff"
    net.call(
        "submit_root_cause",
        incident_id=inc_id,
        title="SVC-0002 fake redundancy: both PWs on hub-b",
        summary="Someone moved SVC-0002's primary from hub-a to hub-b; one hub failure would take it down.",
        root_cause="spoke-2 l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4 = 10.255.0.2 (hub-b); hub-b has "
        "a primary PW for SVC-0002; hub-a lost l2vpn/xconnect[SVC-0002]",
        category="config-drift",
        affected=[{"service": "SVC-0002", "customer": "Customer-A", "tier": "gold", "state": "degraded"}],
        evidence=["controller: show devices spoke-2 diff\n" + diff["output"]],
        confidence="high",
    )
    t_tickets = time.time()
    tickets = {}
    for host in ("hub-b", "spoke-2", "hub-a"):  # in any order: the executor runs additions before removals
        out = net.call("propose_apply_intent", incident_id=inc_id, host=host, reason="SVC-0002 back to NetBox's intent")
        assert out["status"] == "review" and out["diff"], out
        tickets[host] = out["proposal_id"]
    mgr_page = sink.wait(NOCMGR, "tickets.filed", t_tickets, timeout=45)
    assert sorted(t["id"] for t in mgr_page["body"]["tickets"]) == sorted(tickets.values())  # one page for three

    # the NOC Manager returns one (a stand-in reason) and proposes the others; the engineer is paged and files it again
    mgr = roles[NOCMGR]
    t_return = time.time()
    mgr.call("review_ticket", proposal_id=tickets["hub-b"], decision="return", reason="bot-e2e: file hub-b again")
    back = sink.wait(NETENG, "ticket.returned", t_return, timeout=20, incident_id=inc_id)
    assert back["body"]["ticket"]["id"] == tickets["hub-b"] and back["body"]["review"]["reason"].startswith("bot-e2e")
    for host in ("spoke-2", "hub-a"):
        mgr.call("review_ticket", proposal_id=tickets[host], decision="propose", reason="hub-a gets its PW back")
    t_again = time.time()
    again = net.call("propose_apply_intent", incident_id=inc_id, host="hub-b", reason="SVC-0002: hub-b's extra PW")
    assert again["status"] == "review" and again["proposal_id"] != tickets["hub-b"], again
    assert bot.proposal(tickets["hub-b"])["status"] == "returned"
    tickets["hub-b"] = again["proposal_id"]
    sink.wait(NOCMGR, "tickets.filed", t_again, timeout=45)
    mgr.call("review_ticket", proposal_id=tickets["hub-b"], decision="propose", reason="removes hub-b's PW, last")
    assert {bot.proposal(c)["status"] for c in tickets.values()} == {"pending"}

    # the Approve-incident button sends the tickets on screen: one missing, and nothing is approved
    shown = {c: bot.proposal(c)["plan"]["plan_id"] for c in tickets.values()}
    partial = {tickets["hub-a"]: shown[tickets["hub-a"]]}
    assert bot.post_status(f"/api/incidents/{inc_id}/approve-all", {"tickets": partial}) == 409
    assert {bot.proposal(c)["status"] for c in tickets.values()} == {"pending"}
    t_approve = time.time()
    approved = bot.approve_all(inc_id, shown)
    assert [p["host"] for p in approved] == ["hub-a", "spoke-2", "hub-b"]
    for host in ("hub-a", "spoke-2", "hub-b"):
        done = bot.until_decided(tickets[host], timeout=200)
        assert done["status"] == "applied" and done["result"]["verdict"] in ("ok", "degraded"), done
    assert done["result"]["verdict"] == "ok"  # after the last one: every PW up and as NetBox says
    applied = lab_api.get("/audit", params={"since": t_approve, "action": "apply-intent.apply"}).json()
    assert [e["target"] for e in applied] == ["hub-a", "spoke-2", "hub-b"]  # the order the executor ran them
    assert {e["actor"] for e in applied} == {"grok-bot/NETENG approved-by:bot-e2e"}
    rec = sink.wait(NOCENG, "incident.recovered", t_approve, timeout=150, incident_id=inc_id)
    timing("fake-redundancy tickets approved -> recovered page", rec["received"] - t_approve)
    assert lab_api.get("/drift").json()["drift"] is False

    def in_sync() -> dict | None:
        st = lab_api.get("/controller/status", params={"max_age": 0}).json()
        return st if st["connected"] == 10 and st["out_of_sync"] == [] else None

    wait(in_sync, 30, "the controller showing hub-a, hub-b and spoke-2 in sync again (pulled after each apply)")
    pulls = lab_api.get("/audit", params={"since": t_approve, "action": "controller.resync"}).json()
    assert [e["target"] for e in pulls] == ["hub-a", "spoke-2", "hub-b"] and all(e["ok"] for e in pulls)
    inc = bot.incident(inc_id)
    assert inc["title"] == "SVC-0002 fake redundancy: both PWs on hub-b"
    assert 'peer-ipv4 "10.255.0.2"' in inc["root_cause"]["evidence"][0]
    assert [(e["from"], e["to"]) for e in inc["escalations"]] == [("NOCENG", NETENG)]
    assert eng.call("close_incident", incident_id=inc_id, summary="fake redundancy fixed with NetBox intent")["ok"]
    green()
