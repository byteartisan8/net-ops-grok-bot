"""Where labctl and lab-api find the lab (devices, core, NetBox, Zabbix, Docker, lab-api itself).

Every value comes from the process environment, else the repository's ``.env``, else a default derived from
``contract/topology.yaml``. ``.env`` is read again on every :func:`load_settings` call, so tokens that the
NetBox/Zabbix bootstraps write later are picked up without restarting lab-api (its compose service mounts the
repository read-only instead of copying ``.env`` into its environment).

* ``NETSIM_LAB_NETWORK``: ``host`` (default; the 127.0.0.1 ports published by every container) or ``labnet``
  (container-to-container addresses; lab-api runs with this);
* ``CORE_URL``: the signaling bus (default from the topology, ``redis://172.30.10.5:6379/0``);
* ``DOCKER_SOCKET``: Docker Engine API socket for device power off/on (default ``/var/run/docker.sock``);
* ``LAB_API_URL`` (default ``http://127.0.0.1:8090``) and ``LAB_API_TOKEN`` (header ``X-Lab-Token`` on writes);
* ``NETBOX_WEBHOOK_SECRET``: HMAC-SHA512 key of the NetBox -> lab-api webhook (contract change log, 03-netbox);
* ``LAB_DATA_DIR``: lab-api's audit log and webhook journal (default ``build/lab-api`` in the repository);
* ``NETSIM_SCENARIOS_DIR``: scenario files (default ``lab/scenarios`` in the repository);
* NetBox and Zabbix settings (URLs, tokens) are read by their own packages (``netsim_netbox.settings``,
  ``netsim_zabbix.settings``) with the same environment-then-``.env`` rule.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from netsim_netbox.envfile import default_env_file, read_env_file

from netsim_common.constants import lab_constants
from netsim_common.paths import repo_root
from netsim_common.topology import Topology, load_topology

Network = Literal["host", "labnet"]
NETWORKS: tuple[Network, ...] = ("host", "labnet")


class SettingsError(RuntimeError):
    """A setting is missing or invalid."""


@dataclass(frozen=True, slots=True)
class DeviceEndpoint:
    hostname: str
    role: str
    site: str
    mgmt_ip: str
    loopback: str
    restconf_url: str
    simd_url: str
    snmp: tuple[str, int]


@dataclass(frozen=True)
class LabSettings:
    network: Network
    devices: dict[str, DeviceEndpoint]
    core_url: str
    docker_socket: str
    lab_api_url: str
    lab_api_token: str | None
    webhook_secret: str | None
    data_dir: Path
    scenarios_dir: Path
    snmp_community: str
    env_file: Path | None
    timeout: float = 5.0

    @property
    def hosts(self) -> list[str]:
        return list(self.devices)

    def device(self, host: str) -> DeviceEndpoint:
        try:
            return self.devices[host]
        except KeyError:
            raise SettingsError(f"unknown device {host!r} (devices: {', '.join(self.devices)})") from None


def _default_data_dir() -> Path:
    try:
        return repo_root() / "build" / "lab-api"
    except FileNotFoundError:
        return Path("/var/lib/lab-api")


def _default_scenarios_dir() -> Path:
    return repo_root() / "lab" / "scenarios"


def load_settings(
    environ: Mapping[str, str] | None = None, env_file: Path | None = None, topology: Topology | None = None
) -> LabSettings:
    env = os.environ if environ is None else environ
    file = env_file if env_file is not None else default_env_file()
    from_file = read_env_file(file)

    def get(name: str) -> str | None:
        return env.get(name) or from_file.get(name) or None

    network = get("NETSIM_LAB_NETWORK") or "host"
    if network not in NETWORKS:
        raise SettingsError(f"NETSIM_LAB_NETWORK must be one of {', '.join(NETWORKS)}, not {network!r}")
    topo = topology or load_topology()
    lab = lab_constants(topo)
    devices = {}
    for d in topo.devices:
        ep = lab.devices[d.hostname]
        labnet = network == "labnet"
        devices[d.hostname] = DeviceEndpoint(
            hostname=d.hostname,
            role=d.role.value,
            site=d.site,
            mgmt_ip=d.mgmt_ip,
            loopback=d.loopback,
            restconf_url=ep.restconf_url_labnet if labnet else ep.restconf_url,
            simd_url=ep.simd_url_labnet if labnet else ep.simd_url,
            snmp=ep.snmp_labnet if labnet else ep.snmp,
        )
    return LabSettings(
        network=network,  # type: ignore[arg-type]
        devices=devices,
        core_url=get("CORE_URL") or lab.core_url,
        docker_socket=get("DOCKER_SOCKET") or "/var/run/docker.sock",
        lab_api_url=(get("LAB_API_URL") or lab.lab_api_url).rstrip("/"),
        lab_api_token=get("LAB_API_TOKEN"),
        webhook_secret=get("NETBOX_WEBHOOK_SECRET"),
        data_dir=Path(get("LAB_DATA_DIR") or _default_data_dir()),
        scenarios_dir=Path(get("NETSIM_SCENARIOS_DIR") or _default_scenarios_dir()),
        snmp_community=get("SNMP_COMMUNITY") or lab.snmp_community,
        env_file=file,
        timeout=float(get("LAB_TIMEOUT") or 5.0),
    )
