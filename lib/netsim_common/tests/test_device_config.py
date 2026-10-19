from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from netsim_common import DeviceConfig, Interface, InterfaceType, PwRole, load_topology, render_device_config


@pytest.fixture
def spoke2() -> DeviceConfig:
    return render_device_config(load_topology(), "spoke-2")


def test_defaults_are_applied():
    iface = Interface.model_validate({"name": "Gi0/0/1", "type": "access"})
    assert iface.enabled is True and iface.mtu is None


def test_ints_and_bools_are_strict_like_rfc7951():
    with pytest.raises(ValidationError):
        Interface.model_validate({"name": "x", "type": "access", "mtu": "1500"})
    with pytest.raises(ValidationError):
        Interface.model_validate({"name": "x", "type": "access", "enabled": "true"})


def test_field_names_and_aliases_both_work_and_dump_uses_aliases():
    a = Interface(name="Gi0/0/1.5", type=InterfaceType.SUBINTERFACE, parent="Gi0/0/1", vlan_id=5)
    b = Interface.model_validate({"name": "Gi0/0/1.5", "type": "subinterface", "parent": "Gi0/0/1", "vlan-id": 5})
    assert a == b
    assert "vlan-id" in a.model_dump() and "vlan_id" not in a.model_dump()


def test_from_restconf_unwraps_and_ignores_other_modules(spoke2):
    payload = {"ietf-restconf:data": {**spoke2.to_restconf(), "clixon-restconf:restconf": {"enable": True}}}
    assert DeviceConfig.from_restconf(json.dumps(payload)) == spoke2


def test_partial_payloads_need_partial_flag(spoke2):
    l2vpn_only = {"netsim-device:l2vpn": spoke2.to_restconf()["netsim-device:l2vpn"]}
    with pytest.raises(ValidationError, match="does not exist"):
        DeviceConfig.from_restconf(l2vpn_only)
    partial = DeviceConfig.from_restconf(l2vpn_only, partial=True)
    assert partial.xconnect("SVC-0003").pw(PwRole.BACKUP).vc_id == 2003


def test_lookups_and_effective_mtu(spoke2):
    assert spoke2.hostname == "spoke-2"
    assert spoke2.core_uplink().name == "GigabitEthernet0/0/0"
    assert spoke2.effective_mtu("GigabitEthernet0/0/0") == 9000
    assert spoke2.effective_mtu("GigabitEthernet0/0/1.102") == 1500
    data = spoke2.to_restconf()
    data["netsim-device:interfaces"]["interface"][1]["mtu"] = 9000  # access interface
    assert DeviceConfig.from_restconf(data).effective_mtu("GigabitEthernet0/0/1.102") == 9000
    data["netsim-device:interfaces"]["interface"][1].pop("mtu")
    assert DeviceConfig.from_restconf(data).effective_mtu("GigabitEthernet0/0/1.102") == 1500
    with pytest.raises(KeyError):
        spoke2.effective_mtu("nope")


def test_admin_up_follows_parent(spoke2):
    data = spoke2.to_restconf()
    data["netsim-device:interfaces"]["interface"][1]["enabled"] = False
    cfg = DeviceConfig.from_restconf(data)
    assert cfg.admin_up("GigabitEthernet0/0/1.102") is False
    assert spoke2.admin_up("GigabitEthernet0/0/1.102") is True


def test_duplicate_pseudowires_across_xconnects_are_valid_but_reported(spoke2):
    data = spoke2.to_restconf()
    xcs = data["netsim-device:l2vpn"]["xconnect"]
    xcs[1]["pseudowire"][0]["vc-id"] = xcs[0]["pseudowire"][0]["vc-id"]  # SVC-0003 primary := 1002
    cfg = DeviceConfig.from_restconf(data)
    assert cfg.duplicate_pseudowires() == {
        ("10.255.0.1", 1002): [("SVC-0002", PwRole.PRIMARY), ("SVC-0003", PwRole.PRIMARY)]
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["netsim-device:interfaces"]["interface"].append(d["netsim-device:interfaces"]["interface"][0]),
         "duplicate interface name"),
        (lambda d: d["netsim-device:l2vpn"]["xconnect"].append(d["netsim-device:l2vpn"]["xconnect"][0]),
         "duplicate xconnect service-id"),
        (lambda d: d["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"].append(
            d["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"][0]), "duplicate pseudowire role"),
        (lambda d: d["netsim-device:system"].update(hostname="-bad"), "pattern"),
    ],
)  # fmt: skip
def test_list_keys_and_patterns(spoke2, mutate, message):
    data = spoke2.to_restconf()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        DeviceConfig.from_restconf(data)
