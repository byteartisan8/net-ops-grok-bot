from __future__ import annotations

import copy
import json
import random

import pytest

from netsim_common import DiffKind, canonical_json, diff_configs, format_diffs, load_topology, render_device_config


@pytest.fixture
def spoke2() -> dict:
    return json.loads(canonical_json(render_device_config(load_topology(), "spoke-2")))


def _shuffled(data: dict) -> dict:
    out = copy.deepcopy(data)
    rng = random.Random(7)
    rng.shuffle(out["netsim-device:interfaces"]["interface"])
    rng.shuffle(out["netsim-device:l2vpn"]["xconnect"])
    for xc in out["netsim-device:l2vpn"]["xconnect"]:
        xc["pseudowire"].reverse()
    return {k: out[k] for k in reversed(list(out))}


def test_canonical_json_is_order_independent_and_idempotent(spoke2):
    text = canonical_json(spoke2)
    assert canonical_json(_shuffled(spoke2)) == text
    assert canonical_json(text) == text
    assert text.endswith("}\n")


def test_canonical_list_order(spoke2):
    names = [i["name"] for i in spoke2["netsim-device:interfaces"]["interface"]]
    assert names == sorted(names)
    for xc in spoke2["netsim-device:l2vpn"]["xconnect"]:
        assert [p["role"] for p in xc["pseudowire"]] == ["primary", "backup"]


def test_defaults_and_empty_containers_are_normalized(spoke2):
    stripped = copy.deepcopy(spoke2)
    for iface in stripped["netsim-device:interfaces"]["interface"]:
        iface.pop("enabled")
    stripped["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"][0].pop("enabled")
    assert canonical_json(stripped) == canonical_json(spoke2)
    assert diff_configs(spoke2, stripped) == []
    assert json.loads(canonical_json({"netsim-device:l2vpn": {"xconnect": []}})) == {}


def test_diff_reports_a_changed_leaf_with_the_documented_message(spoke2):
    actual = copy.deepcopy(spoke2)
    actual["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"][0]["peer-ipv4"] = "10.255.0.2"
    (d,) = diff_configs(spoke2, actual)
    assert d.message == "l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4: expected 10.255.0.1, got 10.255.0.2"
    assert (d.kind, d.service_id, d.pw_role, d.leaf) == (DiffKind.CHANGED, "SVC-0002", "primary", "peer-ipv4")
    assert d.to_dict()["expected"] == "10.255.0.1"


def test_diff_reports_missing_and_unexpected_entries(spoke2):
    actual = copy.deepcopy(spoke2)
    xc = actual["netsim-device:l2vpn"]["xconnect"]
    xc[0]["pseudowire"] = xc[0]["pseudowire"][:1]  # SVC-0002 backup removed
    xc.append({"service-id": "SVC-0099", "attachment-circuit": "GigabitEthernet0/0/1", "pseudowire": []})
    diffs = diff_configs(spoke2, actual)
    assert [(d.path, d.kind) for d in diffs] == [
        ("l2vpn/xconnect[SVC-0002]/pseudowire[backup]", DiffKind.MISSING),
        ("l2vpn/xconnect[SVC-0099]", DiffKind.UNEXPECTED),
    ]
    # Entry summaries list leaves in YANG order.
    assert diffs[0].message == (
        "l2vpn/xconnect[SVC-0002]/pseudowire[backup]: "
        "expected {peer-ipv4=10.255.0.2 vc-id=2002 enabled=true}, got <absent>"
    )
    assert diffs[1].message.endswith("got {attachment-circuit=GigabitEthernet0/0/1 enabled=true}")


def test_diff_reports_optional_leaves_and_quotes_text(spoke2):
    actual = copy.deepcopy(spoke2)
    sub = actual["netsim-device:interfaces"]["interface"][2]
    sub["description"] = "Customer-B SVC-0002"
    sub["mtu"] = 1400
    diffs = diff_configs(spoke2, actual)
    assert [d.message for d in diffs] == [
        'interfaces/interface[GigabitEthernet0/0/1.102]/description: expected "Customer-A SVC-0002", '
        'got "Customer-B SVC-0002"',
        "interfaces/interface[GigabitEthernet0/0/1.102]/mtu: expected <absent>, got 1400",
    ]
    assert diffs[1].kind is DiffKind.UNEXPECTED and diffs[1].interface == "GigabitEthernet0/0/1.102"
    assert format_diffs([]) == "no differences"


def test_diff_accepts_models_dicts_and_text(spoke2):
    model = render_device_config(load_topology(), "spoke-2")
    assert diff_configs(model, json.dumps(spoke2)) == []
    assert diff_configs(spoke2, model) == []
