"""An AC fault caught by linkDown: hub-a's attachment circuit of SVC-0001 goes down -> linkDown trap -> Zabbix re-polls
hub-a -> the interface problem and the PW problems -> Customer-A degraded -> NOC Engineer page -> field ticket (ac)
-> the NOC Manager proposes it -> repaired -> linkUp -> recovered page."""

from __future__ import annotations

import json
import time

from netsim_bot.settings import NOCENG

HOST, IFACE = "hub-a", "GigabitEthernet0/0/2.101"  # SVC-0001's AC on its primary hub: the backup PW carries it


def _interface_problems(zread, hostid: str) -> list[dict]:
    probs = zread(
        "problem.get",
        {
            "hostids": [hostid],
            "output": ["eventid", "name", "clock"],
            "tags": [{"tag": "scope", "value": "interface", "operator": 1}],
        },
    )
    return [p for p in probs if IFACE in p["name"]]


def test_ac_fault_is_caught_by_link_down(bot, roles, sink, green, fault, zread, timing, wait, proposed) -> None:
    green()
    hostid = zread("host.get", {"output": ["hostid"], "filter": {"host": [HOST]}})[0]["hostid"]
    t0 = fault("ac", HOST, interface=IFACE)
    problems = wait(lambda: _interface_problems(zread, hostid), 30, f"the {IFACE} problem", every=0.2)
    timing("AC fault -> interface problem in Zabbix", time.time() - t0, 10)
    assert "admin up" in problems[0]["name"]
    traps = zread(
        "event.get",
        {
            "hostids": [hostid],
            "time_from": int(t0) - 1,
            "output": ["eventid", "name", "clock"],
            "tags": [{"tag": "trap", "value": "link", "operator": 1}],
        },
    )
    assert any("linkDown" in e["name"] for e in traps), traps  # the trap item's hint event, then the re-poll

    page = sink.wait(NOCENG, "incident.opened", t0, timeout=60)
    timing("AC fault -> NOC Engineer page sent", page["received"] - t0, 30)
    assert sink.customers(page) == ["Customer-A"]
    customer = page["body"]["incident"]["customers"][0]
    assert customer["state"] == "degraded" and [s["service"] for s in customer["services"]] == ["SVC-0001"]
    inc_id = page["body"]["incident"]["id"]

    eng = roles[NOCENG]
    eng.call("take_incident", incident_id=inc_id)
    assert "ac-down" in json.dumps(eng.call("get_device_state", host=HOST))
    eng.call("ack_zabbix_problems", incident_id=inc_id, message="bot-e2e: AC down on hub-a, field ticket")
    t_ticket = time.time()
    chg = eng.call(
        "propose_field_repair", incident_id=inc_id, kind="ac", host=HOST, interface=IFACE, reason="AC down, admin up"
    )
    proposed(chg["proposal_id"], t_ticket)
    t_approve = time.time()
    bot.approve(chg["proposal_id"])
    assert bot.until_decided(chg["proposal_id"], timeout=90)["status"] == "applied"
    wait(lambda: not _interface_problems(zread, hostid), 30, f"the {IFACE} problem to resolve", every=0.2)
    timing(
        "AC repaired (field ticket approved) -> interface problem resolved",
        time.time() - t_approve,
        note="incl. 10 s field delay",
    )
    sink.wait(NOCENG, "incident.recovered", t_approve, timeout=90, incident_id=inc_id)
    assert eng.call("close_incident", incident_id=inc_id, summary="AC of SVC-0001 on hub-a repaired")["ok"]
    green()
