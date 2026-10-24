"""A gold customer down: spoke-2's core uplink fails -> SVC-0002 and SVC-0003 down -> Customer-A and Customer-B
(gold) down -> the NOC Engineer and the NOC Manager are paged -> with a short ``{$NETSIM.ESCALATE.AFTER}``, Zabbix's
escalation step pages the NOC Manager again -> the field ticket, proposed by the NOC Manager, fixes the uplink ->
recovered page."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from netsim_bot.settings import NOCENG, NOCMGR

MACRO = "{$NETSIM.ESCALATE.AFTER}"
ESCALATE_AFTER_S = 60  # Zabbix's shortest escalation step


@pytest.fixture
def escalate_after_1m(zabbix_admin) -> Iterator[int]:
    """``{$NETSIM.ESCALATE.AFTER}`` = 1m for the test (Super admin), then back to the operator's value."""
    macro = zabbix_admin.call(
        "usermacro.get", {"globalmacro": True, "filter": {"macro": MACRO}, "output": ["globalmacroid", "value"]}
    )[0]
    zabbix_admin.call("usermacro.updateglobal", {"globalmacroid": macro["globalmacroid"], "value": "1m"})
    time.sleep(7)  # the server's configuration cache (CacheUpdateFrequency 5 s)
    try:
        yield ESCALATE_AFTER_S
    finally:
        zabbix_admin.call("usermacro.updateglobal", {"globalmacroid": macro["globalmacroid"], "value": macro["value"]})


def test_gold_down_pages_the_manager_and_zabbix_escalates(
    bot, roles, sink, green, fault, escalate_after_1m, timing, proposed
) -> None:
    green()
    t0 = fault("uplink", "spoke-2")
    page = sink.wait(NOCENG, "incident.opened", t0, timeout=60)
    timing("spoke-2 uplink down (gold down) -> NOC Engineer page sent", page["received"] - t0, 30)
    inc_id = page["body"]["incident"]["id"]
    assert sorted(sink.customers(page)) == ["Customer-A", "Customer-B"]
    assert {c["name"]: c["state"] for c in page["body"]["incident"]["customers"]} == {
        "Customer-A": "down",
        "Customer-B": "down",
    }
    gold = sink.wait(NOCMGR, "incident.gold-down", t0, timeout=30, incident_id=inc_id)
    timing("gold customer down -> NOC Manager page sent", gold["received"] - t0, 30)
    assert sorted(gold["body"]["customers"]) == ["Customer-A", "Customer-B"]

    mgr, eng = roles[NOCMGR], roles[NOCENG]
    mgr.call("set_priority", incident_id=inc_id, priority="P1", reason="two gold customers down")
    mgr.call("write_customer_update", incident_id=inc_id, text="bot-e2e: we are on it; next update in 30 min")
    eng.call("take_incident", incident_id=inc_id)
    eng.call("ack_zabbix_problems", incident_id=inc_id, message="bot-e2e: spoke-2 isolated")

    # nobody fixes it within {$NETSIM.ESCALATE.AFTER}: Zabbix's step 2 pages the NOC Manager (one page, two customers)
    esc = sink.wait(NOCMGR, "incident.zabbix-escalation", t0, timeout=escalate_after_1m + 60, incident_id=inc_id)
    timing("Zabbix escalation step -> NOC Manager page (ESCALATE.AFTER=1m)", esc["received"] - t0, 100)
    assert sorted(esc["body"]["customers"]) == ["Customer-A", "Customer-B"]
    time.sleep(12)
    assert [e for e in sink.events(t0) if e[1] == "incident.zabbix-escalation"] == [
        (NOCMGR, "incident.zabbix-escalation", inc_id)
    ]
    inc = bot.incident(inc_id)
    assert inc["priority"] == "P1" and inc["customer_updates"]

    t_ticket = time.time()
    chg = eng.call("propose_field_repair", incident_id=inc_id, kind="uplink", host="spoke-2", reason="uplink down")
    proposed(chg["proposal_id"], t_ticket)
    t_approve = time.time()
    bot.approve(chg["proposal_id"])
    assert bot.until_decided(chg["proposal_id"], timeout=90)["status"] == "applied"
    sink.wait(NOCENG, "incident.recovered", t_approve, timeout=120, incident_id=inc_id)
    assert eng.call("close_incident", incident_id=inc_id, summary="spoke-2 uplink repaired")["ok"]
    green()
