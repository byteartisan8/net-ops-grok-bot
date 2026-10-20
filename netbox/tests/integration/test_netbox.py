"""NetBox integration tests on a real NetBox 4.7.3 (throwaway stack, see conftest.py). Tests run in file order.

Covers the session-03 acceptance criteria: token bootstrap, seeder idempotency, golden equality (+ yanglint), an
intent change moving exactly one vc-id, blast radius, BSM spec equality, a NetBox-added L2VPN appearing in the spec
and a deleted one disappearing, prune, webhooks and the CLIs.
"""

from __future__ import annotations

import json
import subprocess

import jsonschema
import pytest
from netsim_netbox.bootstrap import bootstrap
from netsim_netbox.client import NetBoxError
from netsim_netbox.intent import (
    blast_radius,
    bsm_spec,
    expected_pws,
    load_intent,
    render_all,
    service_detail,
    services_on_device,
    write_journal,
)
from netsim_netbox.intent import render as render_mod
from netsim_netbox.seed import WebhookConfig, seed
from netsim_netbox.seed.desired import EVENT_RULE_NAME, WEBHOOK_NAME, default_webhook_url

from netsim_common import bsm_spec_from_topology, canonical_json, diff_configs, load_topology
from netsim_common.paths import contract_dir, repo_root

pytestmark = pytest.mark.integration

GOLDEN = contract_dir() / "golden"
HOSTS = sorted(p.stem for p in GOLDEN.glob("*.json"))
TOPOLOGY = load_topology()


def golden(host: str) -> str:
    return (GOLDEN / f"{host}.json").read_text(encoding="utf-8")


def intent_of(netbox):
    with netbox.ro() as ro:
        return load_intent(ro, public_url=netbox.url)


def run_seed(netbox, **kwargs):
    with netbox.rw() as rw:
        return seed(rw, TOPOLOGY, webhook=WebhookConfig(default_webhook_url(), netbox.webhook_secret), **kwargs)


def one(client, endpoint, **params):
    [obj] = client.list(endpoint, **params)
    return obj


@pytest.fixture(scope="module")
def first_seed(netbox):
    """The first seed run of the session (on a fresh stack: creates everything)."""
    return run_seed(netbox)


# --------------------------------------------------------------------------------------------- tokens
def test_netbox_version_and_tokens(netbox):
    with netbox.ro() as ro:
        assert ro.status()["netbox-version"] == "4.7.3"
        assert ro.get("authentication-check")["username"] == "netsim-bot"
        assert ro.list("dcim/devices", limit=1) is not None
        for endpoint, body in (("tenancy/tenants", {"name": "x", "slug": "x"}), ("extras/tags", {})):
            with pytest.raises(NetBoxError) as err:
                ro.create(endpoint, body)
            assert err.value.status == 403
        for endpoint in ("users/users", "extras/webhooks"):
            with pytest.raises(NetBoxError) as err:
                ro.list(endpoint)
            assert err.value.status == 403
    assert netbox.token_rw.startswith("nbt_") and netbox.token_ro.startswith("nbt_")


def test_bootstrap_is_idempotent(netbox, tmp_path):
    if not netbox.env:
        pytest.skip("lab NetBox: bootstrap is exercised by make netbox-up")
    env_file = tmp_path / "tokens.env"
    env_file.write_text(f"NETBOX_TOKEN_RW={netbox.token_rw}\nNETBOX_TOKEN_RO={netbox.token_ro}\n", encoding="utf-8")
    env = {"NETBOX_TOKEN_RW": netbox.token_rw, "NETBOX_TOKEN_RO": netbox.token_ro}
    result = bootstrap(
        netbox.url,
        netbox.env["NETBOX_SUPERUSER_NAME"],
        netbox.env["NETBOX_SUPERUSER_PASSWORD"],
        env,
        env_file=env_file,
        quiet=True,
    )
    assert [t.action for t in result.tokens] == ["kept", "kept"] and result.written == []


def test_bootstrap_replaces_a_stale_token(netbox, tmp_path):
    """E.g. after the database was recreated: the .env value no longer authenticates."""
    if not netbox.env:
        pytest.skip("lab NetBox: would replace the lab's tokens")
    env_file = tmp_path / "tokens.env"
    stale = "nbt_" + "A" * 12 + "." + "B" * 40
    env_file.write_text(f"NETBOX_TOKEN_RW={stale}\nNETBOX_TOKEN_RO={netbox.token_ro}\n", encoding="utf-8")
    result = bootstrap(
        netbox.url,
        netbox.env["NETBOX_SUPERUSER_NAME"],
        netbox.env["NETBOX_SUPERUSER_PASSWORD"],
        {"NETBOX_TOKEN_RW": stale, "NETBOX_TOKEN_RO": netbox.token_ro},
        env_file=env_file,
        quiet=True,
    )
    rw, ro = result.tokens
    assert (rw.action, ro.action) == ("created", "kept") and "rejected" in rw.replaced
    assert env_file.read_text(encoding="utf-8").startswith(f"NETBOX_TOKEN_RW={rw.token}\n")
    assert result.deleted_tokens == 1  # the previous RW token, no longer in the file
    with pytest.raises(NetBoxError) as err, netbox.rw() as old:
        old.status()
    assert err.value.status in (401, 403)
    netbox.token_rw = rw.token  # the rest of the session uses the new token
    with netbox.rw() as rw_client:
        assert rw_client.status()["netbox-version"] == "4.7.3"


# --------------------------------------------------------------------------------------------- seeder
def test_seed_twice_second_run_reports_zero_changes(netbox, first_seed):
    if netbox.env:  # fresh stack
        assert first_seed.count("create") > 100 and first_seed.journal_entries == len(HOSTS)
    second = run_seed(netbox)
    assert second.total == 0, [str(c) for c in second.changes]
    assert second.journal_entries == 0


def test_every_device_has_a_seed_journal_entry(netbox, first_seed):
    with netbox.ro() as ro:
        for host in HOSTS:
            device = one(ro, "dcim/devices", name=host)
            entries = ro.list(
                "extras/journal-entries", assigned_object_type="dcim.device", assigned_object_id=device["id"]
            )
            assert any("Seeded by `netbox-seed`" in e["comments"] for e in entries), host


def test_dry_run_finds_drift_and_seed_repairs_it(netbox, first_seed):
    with netbox.rw() as rw:
        uplink = one(rw, "dcim/interfaces", device="spoke-3", name="GigabitEthernet0/0/0")
        rw.update("dcim/interfaces", uplink["id"], {"mtu": 1400, "description": "changed by hand"})
        plan = run_seed(netbox, dry_run=True)
        assert [c.label for c in plan.changes] == ["interface spoke-3/GigabitEthernet0/0/0"]
        assert one(rw, "dcim/interfaces", id=uplink["id"])["mtu"] == 1400  # dry run wrote nothing
        assert run_seed(netbox).total == 1
        assert one(rw, "dcim/interfaces", id=uplink["id"])["mtu"] == 9000
    assert run_seed(netbox, dry_run=True).total == 0


# --------------------------------------------------------------------------------------------- intent
def test_render_all_equals_golden_and_passes_yanglint(netbox, first_seed):
    configs = render_all(intent_of(netbox))
    assert sorted(configs) == HOSTS
    for host, config in configs.items():
        assert canonical_json(config) == golden(host), host
    paths = render_mod.write_configs(configs, repo_root() / "build" / "test-netbox-it")
    result = render_mod.yanglint(paths)
    assert result.returncode == 0, result.stdout + result.stderr


def test_identifier_change_moves_exactly_that_vc_id(netbox, first_seed):
    with netbox.rw() as rw:
        l2vpn = one(rw, "vpn/l2vpns", name="SVC-0002-primary")
        rw.update("vpn/l2vpns", l2vpn["id"], {"identifier": 1102})
        try:
            intent = intent_of(netbox)
            expected = ["l2vpn/xconnect[SVC-0002]/pseudowire[primary]/vc-id: expected 1002, got 1102"]
            for host in ("spoke-2", "hub-a"):
                assert [d.message for d in diff_configs(golden(host), render_all(intent)[host])] == expected
            for host in set(HOSTS) - {"spoke-2", "hub-a"}:
                assert diff_configs(golden(host), render_all(intent)[host]) == []
        finally:
            rw.update("vpn/l2vpns", l2vpn["id"], {"identifier": 1002})
    assert all(canonical_json(c) == golden(h) for h, c in render_all(intent_of(netbox)).items())


def test_blast_radius(netbox, first_seed):
    intent = intent_of(netbox)
    radius = {i.service_id: i.impact for i in blast_radius("hub-a", intent)}
    assert len(radius) == 11 and radius["SVC-0010"] == "down" and radius["SVC-0007"] == "unaffected"
    assert {s for s, impact in radius.items() if impact == "degraded"} == {
        "SVC-0001", "SVC-0002", "SVC-0003", "SVC-0004", "SVC-0005", "SVC-0006", "SVC-0008",
    }  # fmt: skip
    down = {i.service_id for i in blast_radius("spoke-2", intent) if i.impact == "down"}
    assert down == {"SVC-0002", "SVC-0003"}


def test_queries_and_journal(netbox, first_seed):
    intent = intent_of(netbox)
    assert [s.service_id for s in services_on_device("spoke-4", intent)] == ["SVC-0005", "SVC-0006", "SVC-0010"]
    assert len(expected_pws("hub-a", intent)) == 8
    detail = service_detail("SVC-0003", intent)
    assert (detail["customer"], [p["vc_id"] for p in detail["pseudowires"]]) == ("Customer-B", [1003, 2003])
    with netbox.rw() as rw:
        entry = write_journal("l2vpn:SVC-0003-backup", "integration test: remediation record", "success", client=rw)
        stored = one(rw, "extras/journal-entries", id=entry["id"])
        assert stored["assigned_object_type"] == "vpn.l2vpn" and stored["kind"]["value"] == "success"
        rw.delete("extras/journal-entries", entry["id"])


# --------------------------------------------------------------------------------------------- BSM spec
def test_bsm_spec_equals_topology_spec_and_schema(netbox, first_seed):
    spec = bsm_spec(intent_of(netbox))
    assert spec.normalized() == bsm_spec_from_topology(TOPOLOGY)
    schema = json.loads((contract_dir() / "bsm-spec.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(json.loads(spec.to_json()), schema)
    assert all(pw.netbox_l2vpn_id and pw.netbox_url.startswith(netbox.url) for _, pw in spec.pseudowires())


def test_added_l2vpn_appears_and_deleted_one_disappears(netbox, first_seed):
    with netbox.rw() as rw:
        group = one(rw, "ipam/vlan-groups", slug="l2vpn-acs")
        tenant = one(rw, "tenancy/tenants", slug="customer-a")
        vlan = rw.create("ipam/vlans", {"group": group["id"], "vid": 107, "name": "SVC-0012", "tenant": tenant["id"]})
        created = []
        try:
            subs = {}
            for host, parent_name in (("spoke-1", "GigabitEthernet0/0/1"), ("hub-a", "GigabitEthernet0/0/2")):
                parent = one(rw, "dcim/interfaces", device=host, name=parent_name)
                sub = rw.create(
                    "dcim/interfaces",
                    {
                        "device": parent["device"]["id"],
                        "name": f"{parent_name}.107",
                        "type": "virtual",
                        "parent": parent["id"],
                        "description": "Customer-A SVC-0012",
                        "mode": "tagged",
                        "tagged_vlans": [vlan["id"]],
                    },
                )
                subs[host] = sub
                created.append(("dcim/interfaces", sub["id"]))
            l2vpn = rw.create(
                "vpn/l2vpns",
                {
                    "name": "SVC-0012-primary",
                    "slug": "svc-0007-primary",
                    "type": "vpws",
                    "identifier": 1012,
                    "tenant": tenant["id"],
                    "tags": [{"slug": "l2vpn-pw"}],
                    "custom_fields": {
                        "service_id": "SVC-0012",
                        "pw_role": "primary",
                        "spoke_ac": subs["spoke-1"]["id"],
                    },
                },
            )
            created.append(("vpn/l2vpns", l2vpn["id"]))
            term = rw.create(
                "vpn/l2vpn-terminations",
                {
                    "l2vpn": l2vpn["id"],
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": subs["hub-a"]["id"],
                },
            )
            created.append(("vpn/l2vpn-terminations", term["id"]))
            spec = bsm_spec(intent_of(netbox))
            [leaf] = spec.service("SVC-0012").pseudowires
            assert (leaf.vc_id, leaf.a_end.host, leaf.z_end.host, leaf.netbox_l2vpn_id) == (
                1012,
                "spoke-1",
                "hub-a",
                l2vpn["id"],
            )
            assert len(spec.services()) == 12
        finally:
            for endpoint, ident in reversed(created):
                rw.delete(endpoint, ident)
            rw.delete("ipam/vlans", vlan["id"])
        with pytest.raises(KeyError):
            bsm_spec(intent_of(netbox)).service("SVC-0012")

        # delete a seeded PW in NetBox: its leaf disappears; the seeder brings it back
        backup = one(rw, "vpn/l2vpns", name="SVC-0001-backup")
        rw.delete("vpn/l2vpns", backup["id"])  # cascades its termination
    roles = [pw.role.value for pw in bsm_spec(intent_of(netbox)).service("SVC-0001").pseudowires]
    assert roles == ["primary"]
    restore = run_seed(netbox)
    assert sorted(c.label for c in restore.changes if c.action == "create") == [
        "L2VPN svc-0001-backup",
        "L2VPN termination hub-b/GigabitEthernet0/0/2.101",
    ]
    assert bsm_spec(intent_of(netbox)).normalized() == bsm_spec_from_topology(TOPOLOGY)


def test_prune_removes_only_stale_lab_objects(netbox, first_seed):
    with netbox.rw() as rw:
        stale = rw.create(
            "tenancy/tenants", {"name": "Customer-Z", "slug": "customer-z", "tags": [{"slug": "netsim-lab"}]}
        )
        foreign = rw.create("tenancy/tenants", {"name": "Customer-Y", "slug": "customer-y"})
        try:
            assert run_seed(netbox).total == 0  # without --prune
            report = run_seed(netbox, prune=True)
            assert [(c.action, c.label) for c in report.changes] == [("delete", "tenant customer-z")]
            assert rw.list("tenancy/tenants", id=stale["id"]) == []
            assert rw.list("tenancy/tenants", id=foreign["id"]) != []
        finally:
            rw.delete("tenancy/tenants", foreign["id"])
            if rw.list("tenancy/tenants", id=stale["id"]):
                rw.delete("tenancy/tenants", stale["id"])


def test_webhook_and_event_rule(netbox, first_seed):
    with netbox.rw() as rw:
        hook = one(rw, "extras/webhooks", name=WEBHOOK_NAME)
        assert (hook["payload_url"], hook["http_method"], hook["secret"]) == (
            "http://172.30.10.50:8090/hooks/netbox",
            "POST",
            netbox.webhook_secret,
        )
        rule = one(rw, "extras/event-rules", name=EVENT_RULE_NAME)
        assert {"vpn.l2vpn", "vpn.l2vpntermination", "tenancy.tenant", "dcim.interface", "ipam.ipaddress"} <= set(
            rule["object_types"]
        )
        assert rule["action_object_id"] == hook["id"] and rule["enabled"]


# --------------------------------------------------------------------------------------------- CLIs
def _cli(netbox, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", *args], capture_output=True, text=True, env=netbox.cli_env(), cwd=repo_root(), timeout=300
    )


def test_clis_end_to_end(netbox, first_seed):
    check = _cli(netbox, "netbox-seed", "--check")
    assert check.returncode == 0 and "0 changes" in check.stdout, check.stdout + check.stderr
    render = _cli(netbox, "netbox-intent", "render", "spoke-1", "--json")
    assert render.returncode == 0 and render.stdout == golden("spoke-1"), render.stderr
    radius = _cli(netbox, "netbox-intent", "blast-radius", "spoke-2", "--json")
    assert {i["service_id"] for i in json.loads(radius.stdout) if i["impact"] == "down"} == {"SVC-0002", "SVC-0003"}
    problems = _cli(netbox, "netbox-intent", "problems")
    assert problems.returncode == 0, problems.stdout
