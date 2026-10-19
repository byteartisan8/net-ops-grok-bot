from __future__ import annotations

import pytest

from netsim_common import PwOperStatus, PwReason, PwRole
from netsim_common.signaling import (
    Advert,
    ac_fault_key,
    advert_key,
    cut_key,
    parse_fault_key,
    uplink_fault_key,
)


def test_key_formats_match_contract_section_7():
    assert advert_key("hub-a") == "netsim:adv:hub-a"
    assert uplink_fault_key("hub-a") == "netsim:fault:uplink:hub-a"
    assert ac_fault_key("spoke-1", "GigabitEthernet0/0/1.101") == "netsim:fault:ac:spoke-1:GigabitEthernet0/0/1.101"
    assert cut_key("spoke-1", "hub-a") == cut_key("hub-a", "spoke-1") == "netsim:cut:hub-a|spoke-1"


@pytest.mark.parametrize(
    "key",
    [uplink_fault_key("hub-b"), ac_fault_key("spoke-2", "GigabitEthernet0/0/1.201"), cut_key("hub-b", "spoke-4")],
)
def test_fault_keys_round_trip(key):
    parsed = parse_fault_key(key)
    assert parsed is not None and parsed.key == key


def test_non_fault_keys_are_ignored():
    assert parse_fault_key("netsim:adv:hub-a") is None
    assert parse_fault_key("netsim:cut:broken") is None


def test_advert_model_accepts_contract_shape_and_extras():
    advert = Advert.model_validate(
        {
            "hostname": "spoke-1",
            "loopback": "10.255.1.1",
            "mgmt_ip": "172.30.10.21",
            "boot_id": "b1",
            "seq": 3,
            "uplink_oper_up": True,
            "pws": [
                {
                    "service_id": "SVC-0001",
                    "role": "primary",
                    "peer": "10.255.0.1",
                    "vc_id": 1001,
                    "enabled": True,
                    "ac_oper_up": True,
                    "mtu": 1500,
                    "ac_name": "GigabitEthernet0/0/1.101",
                }
            ],
            "debug": {"note": "extra fields are allowed"},
        }
    )
    assert advert.pws[0].role is PwRole.PRIMARY


def test_reason_table_matches_contract_section_7():
    assert [(r.rule, r.value, r.oper_status.name) for r in PwReason] == [
        (1, "admin-down", "down"),
        (2, "duplicate-pw", "down"),
        (3, "ac-down", "lowerLayerDown"),
        (4, "local-uplink-down", "lowerLayerDown"),
        (5, "peer-unreachable", "down"),
        (6, "remote-not-configured", "down"),
        (7, "remote-admin-down", "down"),
        (8, "remote-ac-down", "down"),
        (9, "mtu-mismatch", "down"),
        (10, "ok", "up"),
    ]
    assert PwReason.OK.oper_status is PwOperStatus.up
    assert all(r.condition for r in PwReason)


def test_pw_role_order_is_primary_first():
    assert [r.value for r in sorted(PwRole, key=lambda r: r.sort_index)] == ["primary", "backup"]
