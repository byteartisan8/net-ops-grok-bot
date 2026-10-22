"""From Zabbix customer alerts to incidents and pages (pure logic): parsing, batching, the rules of a closed batch."""

from __future__ import annotations

import itertools

import pytest
from netsim_bot.alerts import AlertError, is_customer_alert, parse_alert, parse_root_cause
from netsim_bot.incidents import Batcher, apply_batch, auto_title
from netsim_bot.models import Incident

_ids = itertools.count(1)
BY_LOOPBACK = {"10.255.0.1": "hub-a", "10.255.0.2": "hub-b", "10.255.0.3": "hub-c", "10.255.0.4": "hub-d"} | {
    f"10.255.1.{n}": f"spoke-{n}" for n in range(1, 7)
}


def alert(body: dict, t: float = 100.0):
    a = parse_alert(body, f"ALR-{next(_ids):06d}", BY_LOOPBACK)
    a.received = t
    return a


def new_incident_factory():
    n = itertools.count(1)
    return lambda: Incident(id=f"INC-{next(n):04d}")


# ------------------------------------------------------------------------------------------------ parsing
def test_root_cause_lines_become_problems_with_both_pw_ends(kit) -> None:
    text = kit.zabbix_body("Customer-B")["root_cause"]
    problems = parse_root_cause(text, BY_LOOPBACK)
    assert [(p["host"], p["service"], p["pw_role"], p["check"]) for p in problems] == [
        ("spoke-2", "SVC-0003", "primary", "oper"),
        ("spoke-4", "SVC-0006", "primary", "oper"),
    ]
    assert problems[0]["hosts"] == ["hub-a", "spoke-2"]  # the powered-off hub is the peer of the spokes' problems
    assert problems[0]["severity"] == "High" and problems[0]["name"].startswith("SVC-0003 primary on spoke-2")
    assert parse_root_cause("garbage\n\n", BY_LOOPBACK) == []


def test_a_real_root_cause_from_the_running_server(kit) -> None:
    real = (
        'Host: "spoke-4" Problem name: "SVC-0005 primary on spoke-4: PW down (2), VC ID 1005 to 10.255.0.1 (hub-a)" '
        'Severity: "High" Age: 2s Problem tags: "check:oper, class:network, component:pw, customer:Customer-D, '
        "host:spoke-4, managed_by:netsim-zabbix, peer:10.255.0.1, pw_role:primary, role:spoke, scope:l2vpn-pw, "
        'service:SVC-0005, site:BR-04, target:netsim-pe, vcid:1005"'
    )
    [p] = parse_root_cause(real, BY_LOOPBACK)
    assert (p["service"], p["hosts"], p["scope"]) == ("SVC-0005", ["hub-a", "spoke-4"], "l2vpn-pw")


def test_parse_alert_fields_and_kinds(kit) -> None:
    a = alert(kit.zabbix_body("Customer-F", severity="Disaster", event_id="4688"))
    assert (a.role, a.kind, a.customer, a.tier, a.severity, a.state, a.event_id) == (
        "noc-engineer",
        "problem",
        "Customer-F",
        "bronze",
        "Disaster",
        "down",
        "4688",
    )
    assert a.services == ["SVC-0010"] and a.hosts == ["hub-a", "spoke-4"] and is_customer_alert(a)
    up = alert(kit.zabbix_body("Customer-A", kind="update", severity="Disaster"))
    assert (up.kind, up.severity, up.state) == ("update", "Disaster", "down")
    rec = alert(kit.zabbix_body("Customer-A", kind="recovery", recovery_event_id="4691"))
    assert (rec.kind, rec.severity, rec.state, rec.recovery_event_id) == ("recovery", "OK", "ok", "4691")
    esc = alert(kit.zabbix_body("Customer-A", kind="escalation", role="noc-manager"))
    assert (esc.kind, esc.role) == ("escalation", "noc-manager")
    assert not is_customer_alert(alert(kit.zabbix_body("SVC-0001 (spoke-1)", level="service")))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"role": "admin"}, "role must be one of"),
        ({"kind": "reboot"}, "kind must be one of"),
        ({"event_id": ""}, "event_id is missing"),
        ({"service": {"name": "", "tags": {}}}, "the customer"),
        ({"severity": "Catastrophic"}, "not a Zabbix severity"),
    ],
)
def test_bad_bodies_are_refused(kit, change: dict, message: str) -> None:
    with pytest.raises(AlertError, match=message):
        parse_alert({**kit.zabbix_body("Customer-A"), **change}, "ALR-1")
    with pytest.raises(AlertError):
        parse_alert(["not", "an", "object"], "ALR-1")


# ------------------------------------------------------------------------------------------------ batching
def test_a_batch_closes_after_quiet_and_is_capped(kit) -> None:
    b = Batcher(10, 30, lambda: "BAT-1")
    b.add("noc-engineer", "a", t=0)
    assert b.due(9.9) == [] and b.next_deadline() == 10
    b.add("noc-engineer", "b", t=8)  # extended while alerts keep coming
    assert b.due(17.9) == []
    [batch] = b.due(18)
    assert batch.items == ["a", "b"] and b.open == {}
    for t in range(0, 40, 5):  # a steady stream: still closed 30 s after the first alert
        b.add("noc-engineer", t, t=t)
        if b.due(t):
            break
    assert b.open == {} and t == 30
    b.add("noc-engineer", "x", t=100)
    b.add("noc-manager", "y", t=101)  # per role
    assert sorted(x.key for x in b.due(111)) == ["noc-engineer", "noc-manager"]


# ------------------------------------------------------------------------------------------------ the rules
def test_hub_a_power_off_opens_one_incident_with_one_page_for_six_customers(kit) -> None:
    incidents: dict[str, Incident] = {}
    alerts = [alert(b) for b in kit.hub_a_storm()]
    out = apply_batch(alerts, incidents, new_incident_factory(), t=110)
    [inc] = out.created
    assert sorted(inc.customers) == sorted(kit.HUB_A_DOWN) and inc.status == "new"
    assert {n: c["state"] for n, c in inc.customers.items()}["Customer-F"] == "down"
    assert inc.counts() == {"down": 1, "degraded": 5, "ok": 0} and inc.severity == "Disaster"
    assert [(p.role, p.event) for p in out.pages] == [("noc-engineer", "incident.opened")]
    # every gold customer is only degraded (hub-b carries their backups): no gold-down page for the NOC Manager
    assert out.pages[0].customers[:3] == ["Customer-A", "Customer-B", "Customer-E"]  # gold first
    assert "hub-a" in inc.hosts and {"SVC-0001", "SVC-0010"} <= set(inc.services)
    assert inc.title == "hub-a: 6 customers (1 down, 5 degraded)" and auto_title(inc) == inc.title
    assert {a.outcome for a in alerts} == {"opened"} and {a.incident_id for a in alerts} == {inc.id}


def test_gold_down_pages_the_noc_manager_once(kit) -> None:
    incidents: dict[str, Incident] = {}
    out = apply_batch([alert(kit.zabbix_body("Customer-A", severity="Disaster"))], incidents, new_incident_factory())
    assert [(p.role, p.event, p.customers) for p in out.pages] == [
        ("noc-engineer", "incident.opened", ["Customer-A"]),
        ("noc-manager", "incident.gold-down", ["Customer-A"]),
    ]
    again = apply_batch(
        [alert(kit.zabbix_body("Customer-A", kind="update", severity="Disaster"))], incidents, new_incident_factory()
    )
    assert again.pages == []  # the same state: joined, no page


def test_later_alerts_of_the_same_fault_join_without_a_page(kit) -> None:
    incidents: dict[str, Incident] = {}
    apply_batch([alert(kit.zabbix_body("Customer-A"))], incidents, new_incident_factory(), t=100)
    later = alert(kit.zabbix_body("Customer-D", event_id="4690"), t=140)  # spoke-4, peer hub-a: shares hub-a
    out = apply_batch([later], incidents, new_incident_factory(), t=150)
    assert out.created == [] and out.pages == [] and later.outcome == "joined"
    [inc] = incidents.values()
    assert sorted(inc.customers) == ["Customer-A", "Customer-D"]
    other = alert(kit.zabbix_body("Customer-C", problems=[("spoke-6", "SVC-0011", "primary", "hub-c")]), t=160)
    out = apply_batch([other], incidents, new_incident_factory(), t=170)
    assert len(out.created) == 1 and out.pages[0].event == "incident.opened"  # another fault: a new incident


def test_worse_better_back_and_recovered(kit) -> None:
    incidents: dict[str, Incident] = {}
    apply_batch([alert(b) for b in kit.hub_a_storm()], incidents, new_incident_factory(), t=100)
    [inc] = incidents.values()
    worse = alert(kit.zabbix_body("Customer-A", kind="update", severity="Disaster", event_id="4685"))
    out = apply_batch([worse], incidents, new_incident_factory(), t=200)
    assert [(p.role, p.event, p.customers) for p in out.pages] == [
        ("noc-engineer", "incident.worse", ["Customer-A"]),
        ("noc-manager", "incident.gold-down", ["Customer-A"]),
    ]
    better = alert(kit.zabbix_body("Customer-A", kind="update", severity="Warning"))
    assert apply_batch([better], incidents, new_incident_factory(), t=210).pages == [] and better.outcome == "better"
    recoveries = [alert(kit.zabbix_body(c, kind="recovery")) for c in sorted(kit.HUB_A_DOWN) if c != "Customer-F"]
    out = apply_batch(recoveries, incidents, new_incident_factory(), t=220)
    assert out.pages == [] and inc.status != "recovered"  # Customer-F is still down
    last = alert(kit.zabbix_body("Customer-F", kind="recovery"))
    out = apply_batch([last], incidents, new_incident_factory(), t=230)
    assert [(p.event, p.role) for p in out.pages] == [("incident.recovered", "noc-engineer")]
    assert inc.status == "recovered" and inc.recovered == 230 and inc.counts()["ok"] == 6
    back = alert(kit.zabbix_body("Customer-C", severity="Warning", event_id="4700"))
    out = apply_batch([back], incidents, new_incident_factory(), t=240)
    assert back.outcome == "back" and [p.event for p in out.pages] == ["incident.worse"] and inc.status == "working"


def test_what_is_only_recorded(kit) -> None:
    incidents: dict[str, Incident] = {}
    flap = [alert(kit.zabbix_body("Customer-E")), alert(kit.zabbix_body("Customer-E", kind="recovery"))]
    out = apply_batch(flap, incidents, new_incident_factory())
    assert out.created == [] and out.pages == [] and flap[0].outcome.startswith("recorded: OK again")
    lonely = alert(kit.zabbix_body("Customer-B", kind="recovery"))
    apply_batch([lonely], incidents, new_incident_factory())
    assert lonely.outcome == "recorded: a recovery without an open incident"


def test_the_zabbix_escalation_step_pages_the_noc_manager(kit) -> None:
    incidents: dict[str, Incident] = {}
    apply_batch([alert(b) for b in kit.hub_a_storm()], incidents, new_incident_factory(), t=100)
    esc = [alert(kit.zabbix_body(c, kind="escalation", role="noc-manager")) for c in ("Customer-A", "Customer-F")]
    out = apply_batch(esc, incidents, new_incident_factory(), t=700)
    [page] = out.pages
    assert (page.role, page.event, page.customers) == (
        "noc-manager",
        "incident.zabbix-escalation",
        ["Customer-A", "Customer-F"],
    )
    assert {a.outcome for a in esc} == {"escalated"}
    # an escalation for a customer without an incident (e.g. alerted during maintenance) opens one
    fresh: dict[str, Incident] = {}
    out = apply_batch(
        [alert(kit.zabbix_body("Customer-C", kind="escalation", role="noc-manager"))], fresh, new_incident_factory()
    )
    assert [p.event for p in out.pages] == ["incident.opened", "incident.zabbix-escalation"] and len(fresh) == 1
