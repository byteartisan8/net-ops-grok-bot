"""Problem snapshots -> incidents: settle window, joining, resolution, maintenance mode, titles."""

from __future__ import annotations

import itertools

from netsim_bot.incidents import IncidentTracker, auto_title
from netsim_bot.models import Incident


def tracker(**kw) -> IncidentTracker:
    ids = (f"INC-{n:04d}" for n in itertools.count(1))
    return IncidentTracker(new_id=lambda: next(ids), **kw)


def p(eventid: str, service: str | None, host: str, sev: str = "High", scope: str = "l2vpn-pw", check: str = "oper"):
    return {
        "eventid": eventid,
        "name": f"{service} on {host}",
        "severity": sev,
        "host": host,
        "service": service,
        "pw_role": "backup",
        "check": check,
        "scope": scope,
        "clock": 100.0,
    }


def test_one_fault_becomes_one_incident_after_the_settle_window() -> None:
    t = tracker(settle_s=6, max_settle_s=20, resolve_hold_s=8)
    first = [p("1", "SVC-0001", "hub-b"), p("2", "SVC-0001", "spoke-1")]
    assert not t.observe(first, at=0).created
    assert not t.observe([*first, p("3", "SVC-0002", "hub-b")], at=3).created  # a newcomer extends the window
    obs = t.observe([*first, p("3", "SVC-0002", "hub-b")], at=9.5)
    assert [i.id for i in obs.created] == ["INC-0001"]
    inc = obs.created[0]
    assert sorted(inc.problems) == ["1", "2", "3"]
    assert inc.services == ["SVC-0001", "SVC-0002"] and inc.hosts == ["hub-b", "spoke-1"]
    assert inc.severity == "High"


def test_max_settle_caps_a_storm() -> None:
    t = tracker(settle_s=6, max_settle_s=10)
    probs = []
    for i in range(6):
        probs.append(p(str(i), f"SVC-000{i}", "hub-a"))
        obs = t.observe(list(probs), at=i * 2.5)
        if obs.created:
            break
    assert obs.created and i == 4  # the window closed at t=10 although problems kept coming


def test_trap_hints_never_open_incidents_and_recorded_hub_down_is_one_incident(recorded) -> None:
    t = tracker(settle_s=0, max_settle_s=0)
    assert not t.observe([p("t1", None, "hub-a", "Information", scope="l2vpn-trap")], at=0).created
    raw = recorded("hub-down")["GET /zabbix/problems"]["problems"]
    obs = t.observe(raw, at=1)
    assert len(obs.created) == 1
    inc = obs.created[0]
    assert len(inc.services) == 7 and "SVC-0010" not in inc.services
    assert all(r.scope != "l2vpn-trap" for r in inc.problem_refs())


def test_new_problem_of_a_known_service_joins_its_incident() -> None:
    t = tracker(settle_s=0, max_settle_s=0)
    inc = t.observe([p("1", "SVC-0002", "spoke-2")], at=0).created[0]
    obs = t.observe([p("1", "SVC-0002", "spoke-2"), p("2", "SVC-0002", "hub-b", "Warning", check="unexpected")], at=5)
    assert not obs.created and [i.id for i in obs.updated] == [inc.id]
    assert sorted(inc.problems) == ["1", "2"]
    # a problem of another service opens its own incident
    obs = t.observe([p("1", "SVC-0002", "spoke-2"), p("3", "SVC-0009", "spoke-5")], at=6)
    assert [i.id for i in obs.created] == ["INC-0002"]


def test_problem_without_service_joins_by_host() -> None:
    t = tracker(settle_s=0, max_settle_s=0)
    inc = t.observe([p("1", "SVC-0001", "hub-a")], at=0).created[0]
    obs = t.observe([p("1", "SVC-0001", "hub-a"), p("9", None, "hub-a", scope="availability", check=None)], at=1)
    assert not obs.created and "9" in inc.problems


def test_resolution_waits_for_the_hold_and_for_busy_work() -> None:
    t = tracker(settle_s=0, max_settle_s=0, resolve_hold_s=8)
    inc = t.observe([p("1", "SVC-0001", "hub-b")], at=0).created[0]
    assert not t.observe([], at=10).resolved  # cleared: hold starts
    assert inc.active_problems() == []
    assert not t.observe([], at=17).resolved
    busy = {"yes": True}
    assert not t.observe([], at=19, busy=lambda _i: busy["yes"]).resolved
    busy["yes"] = False
    obs = t.observe([], at=19.5, busy=lambda _i: busy["yes"])
    assert [i.id for i in obs.resolved] == [inc.id] and inc.status == "resolved" and inc.resolved == 19.5
    assert inc.id not in t.incidents


def test_a_problem_coming_back_keeps_the_incident_open() -> None:
    t = tracker(settle_s=0, max_settle_s=0, resolve_hold_s=8)
    inc = t.observe([p("1", "SVC-0001", "hub-b")], at=0).created[0]
    t.observe([], at=1)
    t.observe([p("1", "SVC-0001", "hub-b")], at=5)  # flaps back
    assert inc.active_problems() and not t.observe([], at=12).resolved
    assert t.observe([], at=20.5).resolved


def test_maintenance_mode_opens_nothing_but_still_resolves() -> None:
    t = tracker(settle_s=0, max_settle_s=0, resolve_hold_s=0)
    inc = t.observe([p("1", "SVC-0001", "hub-b")], at=0).created[0]
    assert not t.observe([p("1", "SVC-0001", "hub-b"), p("2", "SVC-0007", "hub-c")], at=1, accept_new=False).created
    assert "2" not in inc.problems and not t.pending
    assert t.observe([], at=2, accept_new=False).resolved


def test_load_restores_active_incidents() -> None:
    t = tracker(settle_s=0, max_settle_s=0)
    inc = t.observe([p("1", "SVC-0001", "hub-b")], at=0).created[0]
    t2 = tracker(settle_s=0, max_settle_s=0)
    t2.load([Incident.from_dict(inc.to_dict()), Incident(id="INC-0099", status="resolved")])
    assert list(t2.incidents) == [inc.id] and t2.owner == {"1": inc.id}
    assert not t2.observe([p("1", "SVC-0001", "hub-b")], at=1).created


def test_auto_titles(recorded) -> None:
    t = tracker(settle_s=0, max_settle_s=0)
    one = t.observe([p("1", "SVC-0001", "hub-b"), p("2", "SVC-0001", "spoke-1")], at=0).created[0]
    assert auto_title(one, {"SVC-0001": {"sla_tier": "gold"}}) == "SVC-0001 (gold): backup PW down"
    raw = recorded("hub-down")["GET /zabbix/problems"]["problems"]
    many = tracker(settle_s=0, max_settle_s=0).observe(raw, at=0).created[0]
    assert auto_title(many).startswith("7 services: SVC-0001, SVC-0002, SVC-0003 +4")
