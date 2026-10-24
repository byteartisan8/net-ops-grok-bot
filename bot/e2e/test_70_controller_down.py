"""The controller is optional: with `controller` stopped, the roles' controller_cli answers that it is unavailable and
a drift incident (vcid-typo on spoke-1) still goes from page to closed; started again, the controller connects every
router by itself and shows them in sync."""

from __future__ import annotations

import subprocess
import time

from netsim_bot.settings import NETENG, NOCENG


def docker(*args: str) -> None:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"docker {' '.join(args)}: {r.stderr}"


def test_the_drift_case_closes_with_the_controller_stopped(
    bot, roles, sink, green, scenario, lab_api, proposed, wait, timing
) -> None:
    green()
    docker("stop", "controller")
    try:
        t0 = scenario("vcid-typo")
        page = sink.wait(NOCENG, "incident.opened", t0, timeout=60)
        inc_id = page["body"]["incident"]["id"]
        assert sink.customers(page) == ["Customer-A"]

        eng, net = roles[NOCENG], roles[NETENG]
        eng.call("take_incident", incident_id=inc_id)
        down = eng.refused("controller_cli", command="show connections")
        assert "the controller is unavailable" in down and "carry on without it" in down
        eng.call(
            "submit_triage",
            incident_id=inc_id,
            summary="SVC-0001 degraded: its backup PW is down on spoke-1 and hub-b, no physical fault.",
            category="config-drift",
            next_step="escalate to the Network Engineer",
        )
        t_esc = time.time()
        eng.call("escalate", incident_id=inc_id, to=NETENG, note="SVC-0001 backup: config? The controller is down.")
        sink.wait(NETENG, "incident.escalated", t_esc, timeout=20, incident_id=inc_id)

        assert "unavailable" in net.refused("controller_cli", command="show devices spoke-1 check")
        finding = next(f for f in net.call("get_drift")["findings"] if f["service"] == "SVC-0001")
        assert finding["kind"] == "vcid-mismatch" and finding["hosts"] == ["spoke-1"]
        net.call(
            "submit_root_cause",
            incident_id=inc_id,
            title="spoke-1 backup VC ID typo: SVC-0001 degraded",
            summary="spoke-1's backup PW of SVC-0001 has VC ID 2010; hub-b and NetBox have 2001.",
            root_cause="spoke-1 l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id = 2010 (NetBox: 2001)",
            category="config-drift",
            confidence="high",
        )
        t_ticket = time.time()
        chg = net.call("propose_apply_intent", incident_id=inc_id, host="spoke-1", reason="SVC-0001 back to 2001")
        proposed(chg["proposal_id"], t_ticket)
        t_approve = time.time()
        bot.approve(chg["proposal_id"])
        done = bot.until_decided(chg["proposal_id"], timeout=150)
        assert done["status"] == "applied" and done["result"]["verdict"] == "ok", done
        resync = lab_api.get("/audit", params={"since": t_approve, "action": "controller.resync"}).json()
        assert [(e["target"], e["ok"]) for e in resync] == [("spoke-1", False)]  # tried, and the change stands
        assert "unavailable" in str(resync[0]["result"])
        sink.wait(NOCENG, "incident.recovered", t_approve, timeout=150, incident_id=inc_id)
        assert eng.call("close_incident", incident_id=inc_id, summary="spoke-1 VC ID typo fixed")["ok"]
    finally:
        t_start = time.time()
        docker("start", "controller")

    def reconnected() -> dict | None:
        st = lab_api.get("/controller/status", params={"max_age": 0}).json()
        return st if st.get("available") and st["connected"] == 10 and st["out_of_sync"] == [] else None

    wait(reconnected, 120, "the controller connecting the ten routers again, all in sync", every=2)
    timing("controller started -> ten routers connected, in sync", time.time() - t_start)
    assert roles[NOCENG].call("controller_cli", command="show connections")["summary"] == "10 OPEN"
    green()
