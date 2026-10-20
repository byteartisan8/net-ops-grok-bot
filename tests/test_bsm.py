"""BSM spec (contract section 9.3): derived from the topology, schema-valid, and consistent per host."""

from __future__ import annotations

import json

import jsonschema
import pytest
from netsim_testlib import CONTRACT, contract_example

from netsim_common import BsmSpec, bsm_spec_from_topology, load_topology, pw_spec_for_host, pw_spec_macro_value
from netsim_common.bsm import PwEnd, StatusRules, build_customer, build_pseudowire, build_service


@pytest.fixture(scope="module")
def spec() -> BsmSpec:
    return bsm_spec_from_topology(load_topology())


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads((CONTRACT / "bsm-spec.schema.json").read_text(encoding="utf-8"))


def test_spec_counts(spec):
    assert len(spec.customers) == 6
    assert len(spec.services()) == 11
    assert len(spec.pseudowires()) == 20  # 9 protected services x 2 + 2 unprotected x 1


def test_status_rules_follow_the_number_of_pws(spec):
    rules = {s.service_id: s.status_rules for s in spec.services()}
    assert {s for s, r in rules.items() if r is StatusRules.UNPROTECTED} == {"SVC-0009", "SVC-0010"}
    assert {r for s, r in rules.items() if s not in ("SVC-0009", "SVC-0010")} == {StatusRules.REDUNDANT_PAIR}
    bronze = spec.service("SVC-0009")
    assert [pw.name for pw in bronze.pseudowires] == ["SVC-0009 primary (hub-c)"]
    assert "Status rule unprotected: >=1 PW at High or worse -> Disaster; >=1 PW at Average or worse -> Warning" in (
        bronze.description
    )
    assert "Status rule redundant-pair: >=2 PWs at High or worse -> Disaster; >=1 PW at Average or worse" in (
        spec.service("SVC-0007").description
    )


def test_status_rules_must_match_the_pw_leaves(spec):
    two = spec.service("SVC-0001")
    with pytest.raises(ValueError, match="need status rules unprotected"):
        type(two).model_validate({**two.model_dump(), "pseudowires": [two.pseudowires[0].model_dump()]})
    with pytest.raises(ValueError, match="need status rules redundant-pair"):
        type(two).model_validate({**two.model_dump(), "status_rules": "unprotected"})


def test_spec_validates_against_schema(spec, schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(json.loads(spec.to_json()), schema)


def test_contract_example_shape_matches(spec):
    """The contract's example (one customer/service/PW, with NetBox IDs) carries the same spec as ours."""
    example = contract_example("### 9.3 BSM spec")
    ours = spec.service("SVC-0001")
    ex_svc = example["customers"][0]["services"][0]
    ex_pw = ex_svc["pseudowires"][0]
    pw = ours.pw("primary")
    assert (ex_svc["name"], ex_svc["service_id"], ex_svc["spoke"], ex_svc["status_rules"]) == (
        ours.name, ours.service_id, ours.spoke, ours.status_rules.value,
    )  # fmt: skip
    assert ex_svc["tags"] == ours.tags
    assert (ex_pw["name"], ex_pw["role"], ex_pw["vc_id"]) == (pw.name, pw.role.value, pw.vc_id)
    assert ex_pw["ends"] == [e.model_dump() for e in pw.ends]
    assert ex_pw["tags"] == pw.tags
    assert example["root"] == {"name": spec.root.name, "tags": spec.root.tags}
    customer = spec.customer_of("SVC-0001")
    assert {k: v for k, v in example["customers"][0]["tags"].items() if not k.startswith("netbox_")} == customer.tags


def test_pw_spec_per_host(spec):
    hub_a = pw_spec_for_host(spec, "hub-a")
    assert len(hub_a) == 8 and {e.role.value for e in hub_a} == {"primary"}
    assert len(pw_spec_for_host(spec, "hub-b")) == 7
    assert [(e.service, e.role.value, e.expected_peer_host) for e in pw_spec_for_host(spec, "spoke-5")] == [
        ("SVC-0007", "primary", "hub-c"),
        ("SVC-0007", "backup", "hub-d"),
        ("SVC-0009", "primary", "hub-c"),
    ]
    spoke2 = pw_spec_for_host(spec, "spoke-2")
    assert [(e.service, e.role.value) for e in spoke2] == [
        ("SVC-0002", "primary"),
        ("SVC-0002", "backup"),
        ("SVC-0003", "primary"),
        ("SVC-0003", "backup"),
    ]
    first = spoke2[0]
    assert (first.expected_peer, first.expected_peer_host, first.ac, first.customer, first.vc_id) == (
        "10.255.0.1", "hub-a", "GigabitEthernet0/0/1.102", "Customer-A", 1002,
    )  # fmt: skip
    assert pw_spec_for_host(spec, "hub-a")[1].expected_peer == "10.255.1.2"
    assert sum(len(pw_spec_for_host(spec, h)) for h in spec.hosts()) == 40


def test_pw_spec_macro_is_compact_json(spec):
    value = pw_spec_macro_value(spec, "spoke-1")
    assert " " not in value
    assert json.loads(value) == [e.model_dump(mode="json") for e in pw_spec_for_host(spec, "spoke-1")]
    assert set(json.loads(value)[0]) == {
        "service", "role", "vc_id", "expected_peer", "expected_peer_host", "customer", "ac",
    }  # fmt: skip


def test_netbox_flavoured_spec_normalizes_to_topology_spec(spec, schema):
    """What 03-netbox will emit (IDs, URLs, netbox_* tags, any order) compares equal after normalized()."""
    customers = []
    for c_index, c in enumerate(reversed(spec.customers)):
        services = []
        for s in c.services:
            pws = [
                build_pseudowire(
                    s.service_id, pw.role, pw.vc_id, pw.a_end, pw.z_end,
                    netbox_l2vpn_id=100 + i, netbox_url=f"http://127.0.0.1:8000/vpn/l2vpns/{100 + i}/",
                )
                for i, pw in enumerate(reversed(s.pseudowires))
            ]  # fmt: skip
            services.append(build_service(s.service_id, c.name, s.spoke, pws, netbox_url="http://127.0.0.1:8000/x/"))
        customers.append(
            build_customer(
                c.name, c.sla_tier, services,
                netbox_url=f"http://127.0.0.1:8000/tenancy/tenants/{c_index}/",
                extra_tags={"netbox_tenant_id": str(c_index)},
            )
        )  # fmt: skip
    netbox_spec = BsmSpec(root=spec.root, customers=customers)
    jsonschema.validate(json.loads(netbox_spec.to_json()), schema)
    assert netbox_spec != spec
    assert netbox_spec.normalized() == spec.normalized() == spec
    assert (
        "NetBox L2VPN: http://127.0.0.1:8000/vpn/l2vpns/" in netbox_spec.service("SVC-0001").pseudowires[0].description
    )


def test_spec_rejects_names_and_tags_that_disagree_with_fields(spec):
    pw = spec.service("SVC-0001").pw("primary")
    data = pw.model_dump()
    with pytest.raises(ValueError, match="must be 'SVC-0001 primary \\(hub-a\\)'"):
        type(pw).model_validate({**data, "name": "SVC-0001 primary (hub-b)"})
    with pytest.raises(ValueError, match="tags must include"):
        type(pw).model_validate({**data, "tags": {**data["tags"], "vcid": "9999"}})
    svc = spec.service("SVC-0001")
    swapped = [
        build_pseudowire(
            "SVC-0001", "primary", 1001,
            PwEnd(host="hub-a", ac="GigabitEthernet0/0/2.101", expected_peer="10.255.1.1"),
            PwEnd(host="spoke-1", ac="GigabitEthernet0/0/1.101", expected_peer="10.255.0.1"),
        )
    ]  # fmt: skip
    with pytest.raises(ValueError, match="a-end must be the spoke"):
        build_service("SVC-0001", "Customer-A", svc.spoke, swapped)
