"""The office as the Grok Bot app sees it: three roles on /mcp, their tools, the refusals, Ask the NOC."""

from __future__ import annotations

import time

from netsim_bot.settings import NETENG, NOCENG, NOCMGR

NEVER = ("approve", "reject", "apply_change", "confirm", "reset", "scenario", "inject", "restore", "mute", "control")


def test_the_room_has_exactly_three_robots(bot) -> None:
    state = bot.get("/api/state")
    assert [(r["name"], r["role"]) for r in state["roles"]] == [
        ("NOCENG", NOCENG),
        ("NETENG", NETENG),
        ("NOCMGR", NOCMGR),
    ]
    html = bot.http.get("/").text
    for name in ("NOCENG", "NETENG", "NOCMGR"):
        assert name in html
    for gone in ("POLL", "PATCH", "PING", "TRACE", "visitor"):
        assert gone not in html


def test_each_role_gets_its_own_tools_and_none_approves(roles) -> None:
    eng, net, mgr = (set(roles[r].tool_names) for r in (NOCENG, NETENG, NOCMGR))
    common = {"get_noc_status", "get_incident", "zabbix_get", "netbox_get", "device_restconf_get", "add_incident_note"}
    assert common <= eng & net & mgr
    assert {"take_incident", "ack_zabbix_problems", "submit_triage", "propose_field_repair", "escalate"} <= eng
    assert "close_incident" in eng
    assert {"submit_root_cause", "propose_apply_intent", "propose_config_patch", "propose_field_repair"} <= net
    assert {"set_priority", "review_ticket", "write_customer_update", "write_shift_report", "answer_question"} <= mgr
    assert not eng & {"submit_root_cause", "propose_apply_intent", "set_priority", "answer_question"}
    assert not net & {"take_incident", "ack_zabbix_problems", "close_incident", "review_ticket"}
    assert not mgr & {"propose_apply_intent", "propose_field_repair", "ack_zabbix_problems", "close_incident"}
    for name in eng | net | mgr:
        assert not any(word in name for word in NEVER), name
    for role, player in roles.items():
        assert player.instructions and "approve" in player.instructions.lower(), role


def test_a_wrong_role_tool_is_refused_naming_the_role_that_has_it(roles) -> None:
    err = roles[NETENG].refused("close_incident", incident_id="INC-0001", summary="x")
    assert "NOC Engineer" in err
    err = roles[NOCMGR].refused("propose_apply_intent", incident_id="INC-0001", host="spoke-1", reason="x")
    assert "Network Engineer" in err
    err = roles[NOCENG].refused("answer_question", question_id="MSG-000001", answer="x")
    assert "NOC Manager" in err
    assert "no tool" in roles[NOCENG].refused("approve_ticket", proposal_id="CHG-0001")
    assert "is now review_ticket" in roles[NOCMGR].refused("recommend_ticket", proposal_id="CHG-0001")


def test_direct_reads_cannot_write(roles, lab_api, zread) -> None:
    eng = roles[NOCENG]
    out = eng.call(
        "zabbix_get",
        method="service.get",
        params={"output": ["name"], "tags": [{"tag": "bsm_level", "value": "customer", "operator": 1}]},
    )
    assert sorted(s["name"] for s in out["result"]) == [
        "Customer-A",
        "Customer-B",
        "Customer-C",
        "Customer-D",
        "Customer-E",
        "Customer-F",
    ]
    assert "method must be one of" in eng.refused("zabbix_get", method="host.update", params={"hostid": "1"})
    # lab-api itself, as anything else on the lab network would call it
    for method in ("host.update", "event.acknowledge", "user.update", "action.delete", "usermacro.updateglobal"):
        r = lab_api.post("/zabbix/api", json={"method": method, "params": {}})
        assert r.status_code == 403, (method, r.status_code, r.text[:200])
    assert lab_api.post("/netbox/dcim/devices/", json={"name": "x"}).status_code == 405
    assert lab_api.put("/devices/spoke-1/restconf/netsim-device:l2vpn", json={}).status_code == 405
    devices = eng.call("netbox_get", path="dcim/devices/", query={"role": "hub"})
    assert devices["data"]["count"] == 4
    running = eng.call("device_restconf_get", host="spoke-1", path="netsim-device:l2vpn")
    assert "netsim-device:l2vpn" in running["data"]
    assert zread("problem.get", {"output": ["eventid"], "limit": 1}) is not None


def test_ask_the_noc_pages_the_noc_manager_and_the_answer_shows(bot, roles, sink) -> None:
    t0 = time.time()
    q = bot.post("/api/chat", {"message": "bot-e2e: which gold customers are on hub-a?"})
    page = sink.wait(NOCMGR, "question", t0, timeout=30)
    assert page["body"]["question"]["question_id"] == q["id"]
    assert page["authorization"] == "Bearer test-sink"
    mgr = roles[NOCMGR]
    open_ids = [x["question_id"] for x in mgr.call("get_noc_status")["open_questions"]]
    assert q["id"] in open_ids
    mgr.call("answer_question", question_id=q["id"], answer="Customer-A, Customer-B and Customer-E (bot-e2e)")
    chat = bot.get("/api/chat", params={"chat_id": q["chat_id"]})
    answer = next(m for m in chat if m.get("answer_to") == q["id"])
    assert answer["bot"] == "NOCMGR" and "Customer-E" in answer["text"]
    assert next(m for m in chat if m["id"] == q["id"])["status"] == "answered"
    assert "answered already" in mgr.refused("answer_question", question_id=q["id"], answer="again")
    assert [e for e in sink.events(t0) if e[0] != NOCMGR] == []  # nobody else was paged
