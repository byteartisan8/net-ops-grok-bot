"""Drift checker semantics on hand-made inputs (golden configs + the scenarios' changes)."""

from __future__ import annotations

from netsim_lab.drift import analyze, baseline_updates, plans_from_topology
from netsim_lab.netbox_ops import NewService, topology_with_service

from netsim_common.canonical import canonical_dict
from netsim_common.golden import render_device_config
from netsim_common.signaling import FaultKey

PW = "l2vpn/xconnect[{svc}]/pseudowire[{role}]"


def kinds(report):
    return sorted((f.service, f.cls, f.kind) for f in report.findings)


def test_golden_lab_has_no_findings(topology, golden, make_inputs, patched, up_state):
    report = analyze(make_inputs())
    assert report.findings == []
    assert not report.drift
    assert {d.status for d in report.devices.values()} == {"in-sync"}


def test_vcid_typo_is_config_drift_with_the_contract_message(topology, golden, make_inputs, patched, up_state):
    spoke1 = patched(
        golden["spoke-1"], [{"op": "set", "path": PW.format(svc="SVC-0001", role="backup") + "/vc-id", "value": 2010}]
    )
    states = {
        "spoke-1": up_state("spoke-1", {("SVC-0001", "backup"): "down/remote-not-configured"}),
        "hub-b": up_state("hub-b", {("SVC-0001", "backup"): "down/remote-not-configured"}),
    }
    report = analyze(make_inputs(running={"spoke-1": spoke1}, states=states))
    assert kinds(report) == [("SVC-0001", "config-drift", "vcid-mismatch")]
    finding = report.findings[0]
    assert finding.message == "SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001"
    assert finding.hosts == ["spoke-1"]
    assert finding.paths == ["spoke-1:l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id"]
    assert finding.live["backup"] == {"spoke-1": "down/remote-not-configured", "hub-b": "down/remote-not-configured"}
    assert report.drift
    assert report.devices["spoke-1"].status == "drift"
    assert report.devices["spoke-1"].diffs[0].cls == "config-drift"


def test_fake_redundancy_is_one_redundancy_lost_finding(topology, golden, make_inputs, patched, up_state):
    spoke2 = patched(
        golden["spoke-2"],
        [{"op": "set", "path": PW.format(svc="SVC-0002", role="primary") + "/peer-ipv4", "value": "10.255.0.2"}],
    )
    hubb = patched(
        golden["hub-b"],
        [
            {
                "op": "set",
                "path": PW.format(svc="SVC-0002", role="primary"),
                "value": {"peer-ipv4": "10.255.1.2", "vc-id": 1002},
            }
        ],
    )
    huba = patched(
        golden["hub-a"],
        [
            {"op": "delete", "path": "l2vpn/xconnect[SVC-0002]"},
            {"op": "delete", "path": "interfaces/interface[GigabitEthernet0/0/2.102]"},
        ],
    )
    report = analyze(make_inputs(running={"spoke-2": spoke2, "hub-b": hubb, "hub-a": huba}))
    assert kinds(report) == [("SVC-0002", "config-drift", "redundancy-lost")]
    finding = report.findings[0]
    assert finding.message == "SVC-0002: both PWs terminate on hub-b (intent: hub-a + hub-b) — redundancy lost"
    assert finding.hosts == ["hub-a", "hub-b", "spoke-2"]
    assert any("missing on hub-a" in d for d in finding.details)
    assert any("unexpected primary PW on hub-b" in d for d in finding.details)
    assert "hub-a:interfaces/interface[GigabitEthernet0/0/2.102]" in finding.paths


def test_mtu_change_on_hub_access_hits_every_backup(topology, golden, make_inputs, patched, up_state):
    hubb = patched(
        golden["hub-b"], [{"op": "set", "path": "interfaces/interface[GigabitEthernet0/0/2]/mtu", "value": 9000}]
    )
    report = analyze(make_inputs(running={"hub-b": hubb}))
    assert kinds(report) == [(f"SVC-000{n}", "config-drift", "mtu-mismatch") for n in range(1, 7)]
    first = report.findings[0]
    assert first.message == "SVC-0001: backup MTU mismatch hub-b=9000 vs spoke-1=1500 (intent 1500)"
    assert first.hosts == ["hub-b"]
    assert first.paths == ["hub-b:interfaces/interface[GigabitEthernet0/0/2]/mtu"]


def test_stale_backup_peer_and_vcid(topology, golden, make_inputs, patched, up_state):
    spoke1 = patched(
        golden["spoke-1"],
        [{"op": "set", "path": PW.format(svc="SVC-0001", role="backup") + "/peer-ipv4", "value": "10.255.0.12"}],
    )
    hubb = patched(
        golden["hub-b"], [{"op": "set", "path": PW.format(svc="SVC-0005", role="backup") + "/vc-id", "value": 2055}]
    )
    report = analyze(make_inputs(running={"spoke-1": spoke1, "hub-b": hubb}))
    assert kinds(report) == [
        ("SVC-0001", "config-drift", "peer-mismatch"),
        ("SVC-0005", "config-drift", "vcid-mismatch"),
    ]
    assert report.findings[0].message == (
        "SVC-0001: backup PW on spoke-1 points to 10.255.0.12 (no PE has this loopback), intent 10.255.0.2 (hub-b)"
    )
    assert report.findings[1].message == "SVC-0005: backup vc-id mismatch hub-b=2055 vs spoke-4=2005"


def test_new_service_in_netbox_is_intent_only_not_delivered(topology, golden, make_inputs, patched, up_state):
    topo7 = topology_with_service(topology, NewService("SVC-0007", "Customer-C", "spoke-3", 701, 1007, 2007))
    intended = {h: render_device_config(topo7, h) for h in ("spoke-3", "hub-a", "hub-b")}
    report = analyze(make_inputs(intended=intended, plans=plans_from_topology(topo7)))
    assert kinds(report) == [("SVC-0007", "intent-only", "not-delivered")]
    assert report.findings[0].message == (
        "SVC-0007: in NetBox but not configured on hub-a, hub-b, spoke-3 (ordered, not delivered)"
    )
    assert report.drift
    assert {d.cls for h in ("spoke-3", "hub-a", "hub-b") for d in report.devices[h].diffs} == {"intent-only"}


def test_service_removed_from_netbox_depends_on_the_baseline(topology, golden, make_inputs, patched, up_state):
    topo7 = topology_with_service(topology, NewService("SVC-0007", "Customer-C", "spoke-3", 701, 1007, 2007))
    delivered = {h: render_device_config(topo7, h) for h in ("spoke-3", "hub-a", "hub-b")}
    # baseline = the delivered config (lab-api recorded the agreement): NetBox moved -> intent-only
    inputs = make_inputs(running=delivered, baselines={h: canonical_dict(c) for h, c in delivered.items()})
    report = analyze(inputs)
    assert kinds(report) == [("SVC-0007", "intent-only", "not-removed")]
    # baseline = golden: the device moved -> config drift
    report = analyze(make_inputs(running=delivered))
    assert kinds(report) == [("SVC-0007", "config-drift", "not-removed")]


def test_uplink_fault_without_drift_is_fault_per_service(topology, golden, make_inputs, patched, up_state):
    down = {(s.id, "primary"): "down/peer-unreachable" for s in topology.services}
    states = {h: up_state(h, down) for h in topology.hostnames}
    states["hub-a"] = up_state("hub-a", dict.fromkeys(down, "lowerLayerDown/local-uplink-down"))
    report = analyze(make_inputs(states=states, faults=[FaultKey("uplink", ("hub-a",))]))
    assert kinds(report) == [(f"SVC-000{n}", "fault", "uplink-down") for n in range(1, 7)]
    assert report.findings[0].message == "SVC-0001: primary PW down: hub-a core uplink down (fault); service degraded"
    assert not report.drift


def test_stopped_device_and_double_failure(topology, golden, make_inputs, patched, up_state):
    states = {
        h: up_state(h, {(s.id, "backup"): "down/peer-unreachable" for s in topology.services})
        for h in topology.hostnames
    }
    states["hub-b"] = RuntimeError("hub-b: simd unreachable")
    running = {"hub-b": RuntimeError("hub-b: connection refused")}
    report = analyze(make_inputs(running=running, states=states, stopped=["hub-b"]))
    assert kinds(report) == [(f"SVC-000{n}", "fault", "device-down") for n in range(1, 7)]
    assert report.devices["hub-b"].status == "unreachable"
    assert report.incomplete and not report.drift
    # + hub-a uplink fault: both PWs down -> service down, two causes
    for h in topology.hostnames:
        if h != "hub-b":
            states[h] = up_state(
                h,
                {(s.id, r): "down/peer-unreachable" for s in topology.services for r in ("primary", "backup")},
            )
    report = analyze(
        make_inputs(running=running, states=states, stopped=["hub-b"], faults=[FaultKey("uplink", ("hub-a",))])
    )
    svc1 = [f for f in report.findings if f.service == "SVC-0001"]
    assert sorted(f.kind for f in svc1) == ["device-down", "uplink-down"]
    assert all(f.message.endswith("service down") for f in svc1)


def test_unrelated_device_change_is_device_level_drift(topology, golden, make_inputs, patched, up_state):
    huba = patched(
        golden["hub-a"],
        [{"op": "set", "path": "interfaces/interface[GigabitEthernet0/0/0]/description", "value": "CORE-X"}],
    )
    report = analyze(make_inputs(running={"hub-a": huba}))
    assert kinds(report) == [(None, "config-drift", "device-config")]
    assert report.findings[0].message.startswith("hub-a: interfaces/interface[GigabitEthernet0/0/0]/description")


def test_baseline_updates_record_new_agreements(topology, golden, make_inputs, patched, up_state):
    topo7 = topology_with_service(topology, NewService("SVC-0007", "Customer-C", "spoke-3", 701, 1007, 2007))
    delivered = {h: render_device_config(topo7, h) for h in ("spoke-3", "hub-a", "hub-b")}
    states = {h: up_state(h, topo=topo7) for h in topology.hostnames}
    inputs = make_inputs(running=delivered, intended=delivered, plans=plans_from_topology(topo7), states=states)
    report = analyze(inputs)
    assert report.findings == []
    assert sorted(baseline_updates(inputs, report)) == ["hub-a", "hub-b", "spoke-3"]
