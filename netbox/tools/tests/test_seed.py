"""Seeder: desired state from the topology (pure) and the reconcile engine against the recorded NetBox."""

from __future__ import annotations

from collections import Counter

import pytest
from netsim_netbox.objects import KINDS, LAB_TAG, PW_TAG
from netsim_netbox.seed import PHASES, Ref, SeedError, WebhookConfig, desired_state, seed
from netsim_netbox.seed.desired import default_webhook_url
from netsim_netbox.seed.engine import natural_key, normalize, same

TEST_WEBHOOK_SECRET = "test-webhook-secret"
WEBHOOK = WebhookConfig(url="http://172.30.10.50:8090/hooks/netbox", secret=TEST_WEBHOOK_SECRET)


def run(fake, topology, **kwargs):
    kwargs.setdefault("webhook", WEBHOOK)
    return seed(fake, topology, **kwargs)


def refs(value):
    if isinstance(value, Ref):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)


# --------------------------------------------------------------------------------------------- desired state
def test_desired_state_covers_contract_section_10(topology):
    counts = Counter(s.kind for s in desired_state(topology, webhook=WEBHOOK) if not s.update_only)
    assert counts == {
        "extras.tag": 2,
        "extras.customfieldchoiceset": 3,
        "extras.customfield": 5,
        "dcim.region": 3,  # Lab and its child regions North and South
        "dcim.site": 10,
        "dcim.manufacturer": 1,
        "dcim.devicetype": 1,
        "dcim.platform": 1,
        "dcim.devicerole": 2,
        "tenancy.tenantgroup": 1,
        "tenancy.tenant": 6,
        "ipam.vlangroup": 1,
        "ipam.vlan": 11,
        "dcim.device": 10,
        "dcim.interface": 71,  # 10 x (Mgmt0, Loopback0, 2 physical) + 31 subinterfaces
        "ipam.ipaddress": 20,
        "vpn.l2vpn": 20,  # one per PW: 9 protected services x 2 + 2 unprotected x 1
        "vpn.l2vpntermination": 20,  # hub ends only (the spoke end is the spoke_ac custom field)
        "extras.webhook": 1,
        "extras.eventrule": 1,
    }


def test_references_only_point_to_earlier_phases(topology):
    order = {phase: i for i, (phase, _) in enumerate(PHASES)}
    defined: dict[tuple[str, tuple], int] = {}
    for spec in desired_state(topology, webhook=WEBHOOK):
        if not spec.update_only:
            defined.setdefault((spec.kind, spec.key), order[spec.phase])
    for spec in desired_state(topology, webhook=WEBHOOK):
        for value in [*spec.fields.values(), *spec.custom_fields.values()]:
            for ref in refs(value):
                assert (ref.kind, ref.key) in defined, f"{spec.label}: dangling {ref.kind} {ref}"
                assert defined[(ref.kind, ref.key)] < order[spec.phase], f"{spec.label} -> {ref.kind} {ref}"


def test_keys_are_unique_and_lab_objects_tagged(topology):
    specs = [s for s in desired_state(topology, webhook=WEBHOOK) if not s.update_only]
    assert len({(s.kind, s.key) for s in specs}) == len(specs)
    for s in specs:
        assert (LAB_TAG in s.tags) == KINDS[s.kind].taggable, s.label


def test_pseudowire_model(topology):
    specs = desired_state(topology)
    l2vpns = {s.key[0]: s for s in specs if s.kind == "vpn.l2vpn"}
    svc = topology.service("SVC-0002")
    primary = l2vpns["svc-0002-primary"]
    assert primary.fields["name"] == "SVC-0002-primary" and primary.fields["type"] == "vpws"
    assert primary.fields["identifier"] == svc.pw("primary").vc_id
    assert primary.custom_fields["spoke_ac"] == Ref("dcim.interface", ("spoke-2", svc.spoke_ac))
    assert set(primary.tags) == {LAB_TAG, PW_TAG}
    terminations = [s for s in specs if s.kind == "vpn.l2vpntermination" and s.fields["l2vpn"].key == primary.key]
    assert [t.key for t in terminations] == [("hub-a", svc.hub_ac)]
    sub = next(s for s in specs if s.kind == "dcim.interface" and s.key == ("spoke-2", svc.spoke_ac))
    assert (sub.fields["mode"], sub.fields["tagged_vlans"]) == ("tagged", [Ref("ipam.vlan", ("l2vpn-acs", "SVC-0002"))])


def test_no_webhook_without_config(topology):
    assert not [s for s in desired_state(topology) if s.kind in ("extras.webhook", "extras.eventrule")]
    assert default_webhook_url() == "http://172.30.10.50:8090/hooks/netbox"


# --------------------------------------------------------------------------------------------- normalization
def test_normalize_netbox_read_format(fake):
    l2vpn = fake.find("vpn/l2vpns", name="SVC-0001-primary")
    assert normalize(l2vpn["type"]) == "vpws"  # choice field
    assert normalize(l2vpn["custom_fields"]["pw_role"]) == "primary"  # 4.7 selection custom field
    assert normalize(l2vpn["tenant"]) == l2vpn["tenant"]["id"]  # nested object
    sub = fake.find("dcim/interfaces", **{"device.name": "spoke-1", "name": "GigabitEthernet0/0/1.101"})
    assert normalize(sub["tagged_vlans"]) == [sub["tagged_vlans"][0]["id"]]
    assert same("object_types", ["b", "a"], ["a", "b"]) and not same("extra_choices", [[1], [2]], [[2], [1]])


def test_natural_keys_of_recorded_objects(fake):
    assert natural_key("dcim.interface", fake.find("dcim/interfaces", id=1)) == ("hub-a", "Mgmt0")
    term = fake.find("vpn/l2vpn-terminations", **{"l2vpn.name": "SVC-0001-primary"})
    assert natural_key("vpn.l2vpntermination", term) == ("hub-a", "GigabitEthernet0/0/2.101")
    vlan = fake.find("ipam/vlans", name="SVC-0001")
    assert natural_key("ipam.vlan", vlan) == ("l2vpn-acs", "SVC-0001")
    assert natural_key("ipam.ipaddress", fake.find("ipam/ip-addresses", address="10.255.1.1/32")) == ("10.255.1.1",)


# --------------------------------------------------------------------------------------------- engine
def test_seeded_netbox_needs_no_change(fake, topology):
    report = run(fake, topology)
    assert report.total == 0, [str(c) for c in report.changes]
    assert fake.writes == []
    assert report.journal_entries == 0
    assert report.summary() == "0 changes (NetBox matches the topology)"


def test_drifted_field_is_patched_alone_and_journaled(fake, topology):
    uplink = fake.find("dcim/interfaces", **{"device.name": "spoke-1", "name": "GigabitEthernet0/0/0"})
    uplink["mtu"] = 1400
    report = run(fake, topology)
    assert [str(c) for c in report.changes] == ["update  interface spoke-1/GigabitEthernet0/0/0: mtu: 1400 -> 9000"]
    patch, journal = fake.writes
    assert patch == ("PATCH", "dcim/interfaces", [{"id": uplink["id"], "mtu": 9000}])
    method, endpoint, [entry] = journal
    assert (method, endpoint, entry["assigned_object_type"], entry["kind"]) == (
        "POST",
        "extras/journal-entries",
        "dcim.device",
        "info",
    )
    assert entry["assigned_object_id"] == fake.find("dcim/devices", name="spoke-1")["id"]
    assert "Seeded by `netbox-seed`" in entry["comments"] and "mtu: 1400 -> 9000" in entry["comments"]


def test_dry_run_reports_but_never_writes(fake, topology):
    fake.find("vpn/l2vpns", name="SVC-0003-backup")["identifier"] = 9999
    report = run(fake, topology, dry_run=True)
    assert [str(c) for c in report.changes] == ["update  L2VPN svc-0003-backup: identifier: 9999 -> 2003"]
    assert fake.writes == []
    assert report.summary() == "1 change(s): 1 would be updated"


def test_selection_and_object_custom_fields_are_repaired(fake, topology):
    tenant = fake.find("tenancy/tenants", name="Customer-A")
    tenant["custom_fields"]["sla_tier"] = {"value": "silver", "label": "Silver"}
    l2vpn = fake.find("vpn/l2vpns", name="SVC-0005-backup")
    l2vpn["custom_fields"]["spoke_ac"] = None
    run(fake, topology)
    patches = {(e, i["id"]): i for m, e, items in fake.writes if m == "PATCH" for i in items}
    assert patches[("tenancy/tenants", tenant["id"])]["custom_fields"] == {"sla_tier": "gold"}
    spoke_ac = fake.find("dcim/interfaces", **{"device.name": "spoke-4", "name": "GigabitEthernet0/0/1.401"})
    assert patches[("vpn/l2vpns", l2vpn["id"])]["custom_fields"] == {"spoke_ac": spoke_ac["id"]}


def test_missing_object_is_created_with_resolved_references(fake, topology):
    fake.remove("ipam/ip-addresses", address="10.255.1.3/32")
    report = run(fake, topology)
    assert [str(c) for c in report.changes] == ["create  IP address 10.255.1.3"]
    [(method, endpoint, [body]), _journal] = fake.writes
    loopback = fake.find("dcim/interfaces", **{"device.name": "spoke-3", "name": "Loopback0"})
    assert (method, endpoint) == ("POST", "ipam/ip-addresses")
    assert body == {
        "address": "10.255.1.3/32",
        "status": "active",
        "role": "loopback",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": loopback["id"],
        "description": "spoke-3 Loopback0",
        "tags": [{"slug": LAB_TAG}],
    }


def test_foreign_tags_and_custom_fields_are_kept(fake, topology):
    site = fake.find("dcim/sites", slug="br-01")
    site["tags"].append({"id": 999, "slug": "operator-note", "name": "operator-note"})
    assert run(fake, topology).total == 0
    site["tags"] = [t for t in site["tags"] if t["slug"] != LAB_TAG]
    run(fake, topology)
    [(_, endpoint, [body]), *_] = fake.writes
    assert endpoint == "dcim/sites" and body["tags"] == [{"slug": LAB_TAG}, {"slug": "operator-note"}]


def test_prune_deletes_only_stale_lab_objects(fake, topology):
    lab_tag = fake.find("extras/tags", slug=LAB_TAG)
    stale = {"id": 5001, "name": "Customer-Z", "slug": "customer-z", "tags": [lab_tag], "custom_fields": {}}
    foreign = {"id": 5002, "name": "Customer-Y", "slug": "customer-y", "tags": [], "custom_fields": {}}
    fake.objects("tenancy/tenants").extend([stale, foreign])
    assert run(fake, topology).total == 0  # without --prune nothing is deleted
    dry = run(fake, topology, prune=True, dry_run=True)
    assert [str(c) for c in dry.changes] == ["delete  tenant customer-z"] and fake.writes == []
    run(fake, topology, prune=True)
    assert fake.writes == [("DELETE", "tenancy/tenants", [5001])]


def test_webhook_is_managed_only_when_enabled(fake, topology):
    hook = fake.find("extras/webhooks", name="netsim lab-api: intent changed")
    hook["payload_url"] = "http://example.invalid/"
    hook["secret"] = "leaked?"
    assert run(fake, topology, webhook=None).total == 0
    report = run(fake, topology)
    [change] = report.changes
    assert "payload_url: http://example.invalid/ -> http://172.30.10.50:8090/hooks/netbox" in change.details
    assert "secret: *** -> ***" in change.details  # never printed


def test_empty_netbox_dry_run_plans_everything(empty_netbox, topology):
    report = run(empty_netbox, topology, dry_run=True, prune=True)
    specs = desired_state(topology, webhook=WEBHOOK)
    assert report.count("create") == sum(1 for s in specs if not s.update_only)
    assert report.count("update") == 10  # primary_ip4 of each device, set once its IP exists
    assert report.count("delete") == 0 and empty_netbox.writes == []


def test_apply_fails_cleanly_on_a_missing_reference(empty_netbox, topology):
    specs = [s for s in desired_state(topology) if s.kind == "dcim.site"]
    from netsim_netbox.seed import Seeder

    with pytest.raises(SeedError, match=r"region (north|south) does not exist"):
        Seeder(empty_netbox).run(specs)
