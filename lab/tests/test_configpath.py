"""Config paths (the drift notation), patches and validation."""

from __future__ import annotations

import pytest
from netsim_lab.configpath import (
    PathError,
    apply_patch,
    get_path,
    normalize_patch,
    parse_path,
    restconf_body,
    restconf_path,
    validate,
)

from netsim_common.canonical import canonical_dict, diff_configs


def test_parse_keeps_slashes_inside_keys():
    segs = parse_path("interfaces/interface[GigabitEthernet0/0/1.101]/mtu")
    assert [(s.name, s.key) for s in segs] == [
        ("interfaces", None),
        ("interface", "GigabitEthernet0/0/1.101"),
        ("mtu", None),
    ]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("", "empty path"),
        ("routing/x", "must start with"),
        ("l2vpn/xconnect[SVC-0001/pseudowire", "unterminated"),
        ("l2vpn/foo[x]", "is not a list"),
        ("l2vpn/xconnect/pseudowire[primary]", "needs a key"),
    ],
)
def test_bad_paths(path, message):
    with pytest.raises(PathError, match=message):
        parse_path(path)


def test_restconf_path_and_body():
    path = "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id"
    assert restconf_path(path) == "netsim-device:l2vpn/xconnect=SVC-0001/pseudowire=backup/vc-id"
    assert restconf_body(path, 2010) == {"netsim-device:vc-id": 2010}
    iface = "interfaces/interface[GigabitEthernet0/0/1.101]"
    assert restconf_path(iface) == "netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F1.101"
    assert restconf_body(iface, {"mtu": 1400}) == {
        "netsim-device:interface": [{"mtu": 1400, "name": "GigabitEthernet0/0/1.101"}]
    }


def test_set_and_delete_round_trip_with_diff_paths(golden):
    cfg = canonical_dict(golden["spoke-1"])
    patch = [{"op": "set", "path": "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id", "value": 2010}]
    changed = apply_patch(cfg, patch)
    [diff] = diff_configs(cfg, changed)
    assert diff.path == patch[0]["path"] and diff.actual == 2010
    # the drift path turns into the fix
    fixed = apply_patch(changed, [{"op": "set", "path": diff.path, "value": diff.expected}])
    assert diff_configs(cfg, fixed) == []
    assert cfg == canonical_dict(golden["spoke-1"])  # input not modified


def test_set_creates_list_entries_and_delete_removes_them(golden):
    cfg = canonical_dict(golden["hub-b"])
    out = apply_patch(
        cfg,
        [
            {
                "op": "set",
                "path": "l2vpn/xconnect[SVC-0002]/pseudowire[primary]",
                "value": {"peer-ipv4": "10.255.1.2", "vc-id": 1002},
            },
            {"op": "delete", "path": "l2vpn/xconnect[SVC-0001]"},
        ],
    )
    assert get_path(out, "l2vpn/xconnect[SVC-0002]/pseudowire[primary]")["vc-id"] == 1002
    with pytest.raises(KeyError):
        get_path(out, "l2vpn/xconnect[SVC-0001]")
    with pytest.raises(PathError, match="does not exist"):
        apply_patch(cfg, [{"op": "delete", "path": "l2vpn/xconnect[SVC-0099]"}])


def test_json_patch_rfc6902(golden):
    cfg = canonical_dict(golden["spoke-1"])
    out = apply_patch(
        cfg,
        [
            {"op": "test", "path": "/netsim-device:system/hostname", "value": "spoke-1"},
            {"op": "replace", "path": "/netsim-device:l2vpn/xconnect/0/pseudowire/1/vc-id", "value": 2010},
        ],
    )
    assert get_path(out, "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id") == 2010
    with pytest.raises(PathError, match="test failed"):
        apply_patch(cfg, [{"op": "test", "path": "/netsim-device:system/hostname", "value": "x"}])


def test_normalize_rejects_mixed_or_incomplete_ops():
    with pytest.raises(PathError, match="non-empty"):
        normalize_patch([])
    with pytest.raises(PathError, match="needs a 'value'"):
        normalize_patch([{"op": "set", "path": "system/hostname"}])
    with pytest.raises(PathError, match="JSON Patch op"):
        normalize_patch([{"op": "set", "path": "/netsim-device:system/hostname", "value": "x"}])


def test_validate_errors_and_warnings(golden):
    cfg = canonical_dict(golden["spoke-2"])
    bad = apply_patch(cfg, [{"op": "set", "path": "l2vpn/xconnect[SVC-0002]/pseudowire[backup]/vc-id", "value": 0}])
    result = validate(bad)
    assert not result.valid and result.errors
    dangling = apply_patch(cfg, [{"op": "delete", "path": "interfaces/interface[GigabitEthernet0/0/1.102]"}])
    assert not validate(dangling).valid
    dup = apply_patch(
        cfg,
        [
            {"op": "set", "path": "l2vpn/xconnect[SVC-0003]/pseudowire[backup]/vc-id", "value": 2002},
        ],
    )
    result = validate(dup)
    assert result.valid and result.warnings and "duplicate PW" in result.warnings[0]
