"""lab-api on the running lab: reads, token-protected writes, the audit trail and the verified change loop."""

from __future__ import annotations

import json
import time

import httpx

from netsim_common.paths import repo_root

WRITES = [
    ("POST", "/faults", {"kind": "uplink", "host": "hub-a"}),
    ("DELETE", "/faults", None),
    ("POST", "/devices/spoke-1/config", {"patch": [{"op": "delete", "path": "l2vpn/xconnect[SVC-0001]"}]}),
    ("POST", "/devices/spoke-1/apply-intent", {}),
    ("POST", "/scenarios/primary-fail/apply", None),
    ("POST", "/reset", {}),
    ("POST", "/sync", {}),
]
PW = "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id"


def test_served_openapi_equals_the_exported_file(lab):
    served = httpx.get(f"{lab.settings.lab_api_url}/openapi.json", timeout=10).json()
    assert served == json.loads((repo_root() / "lab" / "openapi.json").read_text(encoding="utf-8"))


def expected_impact(topology, host: str) -> dict[str, int]:
    """Blast radius of ``host`` by the topology: down if every PW of a service ends there, degraded if some do."""
    out: dict[str, int] = {}
    for s in topology.services:
        on_host = [r for r in s.roles if host in (s.spoke, s.pw(r).hub)]
        impact = "unaffected" if not on_host else "down" if len(on_host) == len(s.roles) else "degraded"
        out[impact] = out.get(impact, 0) + 1
    return out


def test_reads(lab, lab_api):
    get = lambda path, **kw: lab_api.get(path, **kw)  # noqa: E731
    topology = lab.topology
    topo = get("/topology").json()
    assert topo["source"] == "netbox"
    assert len(topo["devices"]) == len(topology.devices) and len(topo["services"]) == len(topology.services)
    assert len(topo["links"]) == len({(s.spoke, s.pw(r).hub) for s in topology.services for r in s.roles})
    assert topo["regions"] == {r.name: r.sites for r in topology.regions}
    assert {d["hostname"]: d["region"] for d in topo["devices"]} == {h: topology.region_of(h) for h in lab.hosts}
    rules = {s["service"]: s["status_rules"] for s in topo["services"]}
    assert rules == {s.id: "redundant-pair" if s.protected else "unprotected" for s in topology.services}
    services = get("/services").json()
    assert [s["live"] for s in services["services"]] == ["up"] * len(topology.services)
    assert {s["zabbix"] for s in services["services"]} == {"OK"}
    svc = get("/services/SVC-0002").json()
    assert svc["intent"]["spoke"] == "spoke-2" and svc["zabbix"]["service"]["status"] == "OK" and svc["drift"] == []
    assert svc["intent"]["status_rules"] == "redundant-pair"
    bronze = get("/services/SVC-0009").json()["intent"]
    assert bronze["status_rules"] == "unprotected" and [p["role"] for p in bronze["pseudowires"]] == ["primary"]
    assert get("/drift").json()["drift"] is False
    hub_a = get("/impact/hub-a").json()
    assert hub_a["summary"] == expected_impact(topology, "hub-a")
    assert hub_a["by_tier"] == {"bronze": {"down": 1}, "gold": {"degraded": 5}, "silver": {"degraded": 2}}
    assert get("/zabbix/problems").status_code == 200
    since = time.time() - 3600
    assert "events" in get("/events", params={"since": since}).json()
    assert get("/devices/spoke-1/running").json()["config"]["netsim-device:system"]["hostname"] == "spoke-1"
    assert get("/devices/spoke-1/state").json()["hostname"] == "spoke-1"
    assert get("/devices/spoke-1/intent").json()["device"]["status"] == "in-sync"
    assert len(get("/scenarios").json()) == len(list(lab.settings.scenarios_dir.glob("*.yaml")))
    assert get("/scenarios/healthy/check").json()["ok"] is True
    assert get("/devices/nope/state").status_code == 404


def test_writes_reject_requests_without_the_token(lab):
    for method, path, body in WRITES:
        for headers in ({}, {"X-Lab-Token": "wrong"}):
            r = httpx.request(method, f"{lab.settings.lab_api_url}{path}", json=body, headers=headers, timeout=30)
            assert r.status_code == 401, (method, path, r.status_code)
    # nothing happened
    assert lab.core.faults() == []


def test_change_loop_dry_run_confirm_verify_and_audit(lab, lab_api, timing, green):
    t0 = time.time()
    hash_before = lab.devices["spoke-1"].state()["config_hash"]
    body = {"patch": [{"op": "set", "path": PW, "value": 2010}], "comment": "e2e: typo"}
    plan = lab_api.post("/devices/spoke-1/config", json=body).json()
    assert plan["applied"] is False and plan["validation"]["valid"]
    assert [d["message"] for d in plan["diff"]] == [f"{PW}: 2001 -> 2010"]
    assert [f["kind"] for f in plan["drift_after_predicted"]] == ["vcid-mismatch"]
    assert lab.devices["spoke-1"].state()["config_hash"] == hash_before  # a dry run changes nothing

    wrong = lab_api.post("/devices/spoke-1/config", json={**body, "confirm": True, "plan_id": "0" * 16})
    assert wrong.status_code == 409

    start = time.monotonic()
    applied = lab_api.post("/devices/spoke-1/config", json={**body, "confirm": True, "plan_id": plan["plan_id"]}).json()
    timing("lab-api config apply -> verified (simd + Zabbix settled)", time.monotonic() - start)
    v = applied["verification"]
    assert applied["applied"] and v["verdict"] == "degraded"
    assert v["zabbix"]["services"] == {"SVC-0001": "Warning"} and all(v["zabbix"]["walk_refreshed"].values())
    assert v["simd"]["pseudowires"]["SVC-0001"]["backup"] == {
        "hub-b": "down/remote-not-configured",
        "spoke-1": "down/remote-not-configured",
    }
    assert [f["kind"] for f in v["drift_after"]] == ["vcid-mismatch"]

    fix = lab_api.post("/devices/spoke-1/apply-intent", json={}).json()
    assert [d["message"] for d in fix["diff"]] == [f"{PW}: 2010 -> 2001"]
    start = time.monotonic()
    done = lab_api.post("/devices/spoke-1/apply-intent", json={"confirm": True, "plan_id": fix["plan_id"]}).json()
    timing("lab-api apply-intent -> verified OK", time.monotonic() - start)
    assert done["verification"]["verdict"] == "ok", done["verification"]["problems"]
    assert lab_api.get("/drift").json()["drift"] is False

    fault = lab_api.post("/faults", json={"kind": "cut", "host": "spoke-3", "peer": "hub-b"})
    assert fault.status_code == 200 and fault.json()["faults"][0]["kind"] == "cut"
    assert (
        lab_api.post("/faults", json={"kind": "cut", "host": "spoke-3", "peer": "hub-b", "action": "restore"}).json()[
            "faults"
        ]
        == []
    )
    sync = lab_api.post("/sync", json={"wait": True, "reason": "e2e"}).json()
    assert sync["last"]["ok"]

    entries = lab_api.get("/audit", params={"since": t0, "limit": 100}).json()
    mine = [(e["action"], e["status"]) for e in entries if e["actor"] == "e2e"]
    assert mine == [
        ("config.plan", 200),
        ("config.apply", 409),
        ("config.apply", 200),
        ("apply-intent.plan", 200),
        ("apply-intent.apply", 200),
        ("fault.inject", 200),
        ("fault.restore", 200),
        ("sync.request", 200),
    ]
    applied_entry = next(
        e for e in entries if e["actor"] == "e2e" and e["action"] == "config.apply" and e["status"] == 200
    )
    assert applied_entry["diff"][0]["path"] == PW and applied_entry["result"]["verdict"] == "degraded"
    green("after the API test")


def test_reset_through_the_api(lab, lab_api, timing):
    lab.core.set_uplink("spoke-4")
    start = time.monotonic()
    result = lab_api.post("/reset", json={"wait": True}).json()
    timing("lab-api POST /reset (one fault) -> green", time.monotonic() - start)
    assert result["green"] and result["faults_cleared"] == ["netsim:fault:uplink:spoke-4"]
