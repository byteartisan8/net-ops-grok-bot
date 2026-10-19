"""cpwVcIndex / ifIndex persistence (contract section 7): stable, max+1, reused after delete, survives restarts."""

from __future__ import annotations

import json
import stat

from netsim_simd.indexes import IndexStore, atomic_write_text

from netsim_common.enums import PwRole

P, B = PwRole.PRIMARY, PwRole.BACKUP


def test_first_allocation_is_deterministic(tmp_path):
    store = IndexStore(tmp_path / "indexes.json")
    got = store.allocate_pws([("SVC-0002", P), ("SVC-0001", B), ("SVC-0001", P)])
    assert got == {("SVC-0001", P): 1, ("SVC-0001", B): 2, ("SVC-0002", P): 3}


def test_new_keys_get_max_plus_one_and_survive_restart(tmp_path):
    path = tmp_path / "indexes.json"
    store = IndexStore(path)
    store.allocate_pws([("SVC-0001", P), ("SVC-0001", B)])
    reloaded = IndexStore(path)
    assert reloaded.pw_index("SVC-0001", "backup") == 2
    assert reloaded.allocate_pws([("SVC-0005", P)]) == {("SVC-0005", P): 3}
    assert json.loads(path.read_text())["pw"] == {"SVC-0001:primary": 1, "SVC-0001:backup": 2, "SVC-0005:primary": 3}


def test_deleted_pw_keeps_its_index(tmp_path):
    store = IndexStore(tmp_path / "indexes.json")
    store.allocate_pws([("SVC-0001", P), ("SVC-0002", P), ("SVC-0003", P)])
    # SVC-0002 disappears from config for a while: nothing is allocated or freed ...
    assert store.allocate_pws([("SVC-0001", P), ("SVC-0003", P)]) == {("SVC-0001", P): 1, ("SVC-0003", P): 3}
    # ... a brand-new PW gets max+1, and SVC-0002 comes back with its old index
    assert store.allocate_pws([("SVC-0009", B)]) == {("SVC-0009", B): 4}
    assert IndexStore(tmp_path / "indexes.json").allocate_pws([("SVC-0002", P)]) == {("SVC-0002", P): 2}


def test_interfaces_physical_first(tmp_path):
    store = IndexStore(tmp_path / "indexes.json")
    got = store.allocate_ifs([("Gi0/0/1.101", True), ("Gi0/0/1", False), ("Gi0/0/0", False)])
    assert got == {"Gi0/0/0": 1, "Gi0/0/1": 2, "Gi0/0/1.101": 3}


def test_no_write_when_nothing_new(tmp_path):
    path = tmp_path / "indexes.json"
    store = IndexStore(path)
    store.allocate_pws([("SVC-0001", P)])
    mtime = path.stat().st_mtime_ns
    store.allocate_pws([("SVC-0001", P)])
    assert path.stat().st_mtime_ns == mtime


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "sub" / "file.json"
    atomic_write_text(target, "one\n")
    atomic_write_text(target, "two\n")
    assert target.read_text() == "two\n"
    assert [p.name for p in target.parent.iterdir()] == ["file.json"]
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0
