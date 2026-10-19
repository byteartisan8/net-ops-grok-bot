"""simd settings, read from the environment (the image is configured only by env, see devices/README.md).

| Variable | Default | Meaning |
|---|---|---|
| ``HOSTNAME`` | required | Device identity: Redis keys, fault keys, golden config file name |
| ``MGMT_IP`` | required | Management (labnet) address, published in adverts; traps leave from it |
| ``CORE_URL`` | topology ``core`` | Redis URL of the signaling bus |
| ``TRAP_TARGETS`` | contract section 8 default | ``host[:port],...`` (port 162 if omitted); set but empty = no traps |
| ``SNMP_COMMUNITY`` | topology ``snmp.community`` | SNMP v2c read community |
| ``TRAP_COMMUNITY`` | topology ``snmp.trap_community`` | SNMP v2c trap community |
| ``SITE`` | none | Reported as ``sysLocation`` |
| ``RESTCONF_URL`` | ``http://127.0.0.1:8080`` | Local Clixon RESTCONF |
| ``SIMD_PORT`` | ``9000`` | simd HTTP API port |
| ``NETSIM_STATE_DIR`` | ``/var/lib/netsim/simd`` | Persistent simd state (index map, first-boot marker) |
| ``NETSIM_SNMP_DB`` | ``/run/netsim/snmp/agent.db`` | SQLite file read by the snmpsim variation module |
| ``NETSIM_GOLDEN_DIR`` | ``<contract>/golden`` | Golden configs loaded on first boot |
| ``CONFIG_POLL_INTERVAL`` | ``2.0`` | Safety-net poll of the running config (seconds) |
| ``TRAP_BATCH_WINDOW`` | ``0.1`` | Trap batching window (seconds), see :mod:`netsim_simd.traps` |
| ``CLIXON_VERSION`` | ``unknown`` | Reported in ``sysDescr`` (set by the image) |
| ``LOG_LEVEL`` | ``INFO`` | Python log level |
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from netsim_common.constants import lab_constants
from netsim_common.paths import contract_dir
from netsim_common.signaling import HELLO_INTERVAL_S, HOLD_TIME_S

#: Default trap port, from contract section 8 (``.env.example`` ships ``172.30.10.42:1162``).
CONTRACT_TRAP_PORT = 1162
#: Standard SNMP trap port, used when a TRAP_TARGETS entry has no port.
SNMP_TRAP_PORT = 162


class SettingsError(ValueError):
    """Invalid or missing environment configuration."""


@dataclass(frozen=True, slots=True)
class TrapTarget:
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def parse_trap_targets(value: str) -> tuple[TrapTarget, ...]:
    """Parse ``host[:port],host[:port]`` (empty string = no targets)."""
    targets: list[TrapTarget] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep:
            host, port = item, str(SNMP_TRAP_PORT)
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise SettingsError(f"invalid TRAP_TARGETS entry {item!r} (expected host[:port])")
        targets.append(TrapTarget(host, int(port)))
    return tuple(targets)


@dataclass(frozen=True)
class Settings:
    hostname: str
    mgmt_ip: str
    core_url: str
    trap_targets: tuple[TrapTarget, ...]
    snmp_community: str
    trap_community: str
    site: str | None = None
    restconf_url: str = "http://127.0.0.1:8080"
    api_host: str = "0.0.0.0"
    api_port: int = 9000
    state_dir: Path = Path("/var/lib/netsim/simd")
    snmp_db: Path = Path("/run/netsim/snmp/agent.db")
    golden_dir: Path | None = None
    config_poll_interval: float = 2.0
    hello_interval: float = HELLO_INTERVAL_S
    hold_time: float = HOLD_TIME_S
    trap_batch_window: float = 0.1
    clixon_version: str = "unknown"
    event_buffer: int = 1000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        lab = lab_constants()

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise SettingsError(f"environment variable {name} is required")
            return value

        def number(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                raise SettingsError(f"{name}={raw!r} is not a number") from None

        mgmt_ip = required("MGMT_IP")
        try:
            ipaddress.IPv4Address(mgmt_ip)
        except ValueError:
            raise SettingsError(f"MGMT_IP={mgmt_ip!r} is not an IPv4 address") from None
        if "TRAP_TARGETS" in env:
            targets = parse_trap_targets(env["TRAP_TARGETS"])
        else:
            targets = (TrapTarget(lab.trap_receiver_ip, CONTRACT_TRAP_PORT),)
        snmp_community = env.get("SNMP_COMMUNITY", "").strip() or lab.snmp_community
        golden = env.get("NETSIM_GOLDEN_DIR", "").strip()
        return cls(
            hostname=required("HOSTNAME"),
            mgmt_ip=mgmt_ip,
            core_url=env.get("CORE_URL", "").strip() or lab.core_url,
            trap_targets=targets,
            snmp_community=snmp_community,
            trap_community=env.get("TRAP_COMMUNITY", "").strip() or lab.trap_community,
            site=env.get("SITE", "").strip() or None,
            restconf_url=(env.get("RESTCONF_URL", "").strip() or cls.restconf_url).rstrip("/"),
            api_port=int(number("SIMD_PORT", cls.api_port)),
            state_dir=Path(env.get("NETSIM_STATE_DIR", "").strip() or cls.state_dir),
            snmp_db=Path(env.get("NETSIM_SNMP_DB", "").strip() or cls.snmp_db),
            golden_dir=Path(golden) if golden else contract_dir() / "golden",
            config_poll_interval=number("CONFIG_POLL_INTERVAL", cls.config_poll_interval),
            trap_batch_window=number("TRAP_BATCH_WINDOW", cls.trap_batch_window),
            clixon_version=env.get("CLIXON_VERSION", "").strip() or cls.clixon_version,
            log_level=(env.get("LOG_LEVEL", "").strip() or cls.log_level).upper(),
        )
