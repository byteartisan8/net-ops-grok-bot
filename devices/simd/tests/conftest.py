"""Shared fixtures for simd unit tests: contract data, an in-memory bus and in-process devices."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from netsim_simd.bus import BusSnapshot, build_snapshot, relevant_keys
from netsim_simd.configwatch import ConfigSnapshot, ConfigUpdate
from netsim_simd.daemon import Simd
from netsim_simd.settings import Settings
from netsim_simd.snmpdb import SnmpDbWriter
from netsim_simd.traps import Trap

from netsim_common.device_config import DeviceConfig
from netsim_common.oids import OidRegistry, load_oids
from netsim_common.paths import contract_dir
from netsim_common.signaling import Advert, advert_key
from netsim_common.topology import Topology, load_topology


@pytest.fixture(scope="session")
def oids() -> OidRegistry:
    return load_oids()


@pytest.fixture(scope="session")
def topology() -> Topology:
    return load_topology()


def golden_payload(host: str) -> dict[str, Any]:
    return json.loads((contract_dir() / "golden" / f"{host}.json").read_text(encoding="utf-8"))


def golden_config(host: str) -> DeviceConfig:
    return DeviceConfig.from_restconf(golden_payload(host))


class FakeStore:
    """Just enough of Redis for the bus: string keys with millisecond TTLs and a publish log."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float | None]] = {}
        self.published: list[dict[str, Any]] = []

    def set(self, key: str, value: str, px: int | None = None) -> None:
        self.data[key] = (value, time.monotonic() + px / 1000 if px else None)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def get(self, key: str) -> str | None:
        item = self.data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= time.monotonic():
            del self.data[key]
            return None
        return value

    def pttl(self, key: str) -> int:
        item = self.data.get(key)
        if item is None:
            return -2
        return -1 if item[1] is None else max(0, int((item[1] - time.monotonic()) * 1000))

    def keys(self) -> list[str]:
        return [k for k in list(self.data) if self.get(k) is not None]


class FakeBus:
    def __init__(self, store: FakeStore, hostname: str, hold_time: float = 3.0) -> None:
        self.store = store
        self.hostname = hostname
        self.hold_ms = int(hold_time * 1000)
        self.down = False

    async def snapshot(self) -> BusSnapshot:
        if self.down:
            raise OSError("core unreachable (test)")
        adv_keys, fault_keys = relevant_keys(self.hostname, self.store.keys())
        adverts = [(k, self.store.get(k), self.store.pttl(k)) for k in adv_keys]
        faults = [(k, self.store.get(k)) for k in fault_keys]
        return build_snapshot(self.hostname, self.hold_ms, adverts, faults)

    async def write_advert(self, advert: Advert) -> None:
        if self.down:
            raise OSError("core unreachable (test)")
        self.store.set(advert_key(self.hostname), advert.model_dump_json(), px=self.hold_ms)

    async def publish(self, message: dict[str, Any]) -> None:
        self.store.published.append(message)

    async def close(self) -> None:
        pass


class FakeRestconf:
    """Stands in for Clixon: serves whatever config the test last set."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}

    async def ready(self) -> bool:
        return True

    async def get_running(self) -> dict[str, Any]:
        return self.payload

    async def replace_running(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def aclose(self) -> None:
        pass


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[Trap] = []

    async def send(self, trap: Trap) -> list[str]:
        self.sent.append(trap)
        return ["test-sink:1162"]

    def close(self) -> None:
        pass

    def names(self) -> list[str]:
        return [t.name for t in self.sent]


DeviceFactory = Callable[..., Any]


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def make_device(tmp_path: Path, store: FakeStore, topology: Topology) -> DeviceFactory:
    """``await make_device("spoke-1")`` -> an in-process Simd on the fake bus, golden config applied."""

    async def factory(host: str, config: DeviceConfig | dict | None = None) -> Simd:
        d = topology.device(host)
        settings = Settings(
            hostname=host,
            mgmt_ip=d.mgmt_ip,
            core_url="redis://fake:6379/0",
            trap_targets=(),
            snmp_community="netsim",
            trap_community="netsim",
            site=d.site,
            state_dir=tmp_path / host / "state",
            snmp_db=tmp_path / host / "agent.db",
            golden_dir=contract_dir() / "golden",
        )
        simd = Simd(settings, bus=FakeBus(store, host), restconf=FakeRestconf(), sender=FakeSender())  # type: ignore[arg-type]
        simd.snmpdb = SnmpDbWriter(settings.snmp_db)
        payload = config if isinstance(config, dict) else (config or golden_config(host)).to_restconf()
        simd.restconf.payload = payload  # type: ignore[attr-defined]
        snap = await simd.watcher.fetch()
        simd.watcher.current = snap
        await simd._on_config_update(ConfigUpdate(None, snap, "initial", None, time.time()))
        return simd

    return factory


@pytest.fixture
def settle() -> Callable[..., Any]:
    """``await settle(a, b)``: recompute every device a few times so adverts propagate, then send traps."""

    async def _settle(*devices: Simd, rounds: int = 2) -> None:
        for _ in range(rounds):
            for d in devices:
                await d.recompute()
        for d in devices:
            await d.flush_traps()

    return _settle


@pytest.fixture
def change_config() -> Callable[..., Any]:
    """``await change_config(device, mutate)``: a commit as the watcher reports it (notification + user)."""
    from netsim_simd.clixon import ConfigCommit

    async def _change(device: Simd, mutate: Callable[[dict[str, Any]], None], user: str = "restconf") -> None:
        assert device.watcher.current is not None
        payload = json.loads(device.watcher.current.canonical)
        mutate(payload)
        snap = ConfigSnapshot.from_payload(payload)
        commit = ConfigCommit(event_time=None, username=user, server=False, datastore="running", edits=())
        update = ConfigUpdate(device.watcher.current, snap, "notification", commit, time.time())
        device.watcher.current = snap
        await device._on_config_update(update)

    return _change


@pytest.fixture
def pw_state() -> Callable[[Simd, str, str], Any]:
    """``pw_state(device, "SVC-0001", "primary")`` -> the PseudowireState from ``/state``."""

    def _pw(device: Simd, service_id: str, role: str) -> Any:
        return next(p for p in device.state().pseudowires if p.service_id == service_id and p.role.value == role)

    return _pw


@pytest.fixture(scope="session")
def golden() -> Callable[[str], DeviceConfig]:
    """``golden("spoke-1")`` -> the contract golden config as a DeviceConfig."""
    return golden_config
