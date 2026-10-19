"""Lab addresses and ports, derived from ``contract/topology.yaml`` (no duplicated literals).

``*_url`` / ``snmp`` are reachable from the Docker host (published on ``host_bind``);
``*_labnet`` variants are the in-network addresses used container-to-container.

    from netsim_common.constants import lab_constants
    lab = lab_constants()
    lab.devices["spoke-1"].restconf_url     # 'http://127.0.0.1:18021'
    lab.core_url                            # 'redis://172.30.10.5:6379/0'
"""

from __future__ import annotations

from dataclasses import dataclass

from netsim_common.topology import Topology, load_topology

CORE = "core"
NETBOX = "netbox"
ZABBIX_SERVER = "zabbix-server"
ZABBIX_WEB = "zabbix-web"
ZABBIX_SNMPTRAPS = "zabbix-snmptraps"
LAB_API = "lab-api"


@dataclass(frozen=True, slots=True)
class DeviceEndpoints:
    hostname: str
    mgmt_ip: str
    loopback: str
    restconf_url: str
    restconf_url_labnet: str
    simd_url: str
    simd_url_labnet: str
    snmp: tuple[str, int]
    snmp_labnet: tuple[str, int]


@dataclass(frozen=True, slots=True)
class LabConstants:
    labnet_name: str
    labnet_subnet: str
    labnet_gateway: str
    host_bind: str
    snmp_community: str
    trap_community: str
    core_ip: str
    core_url: str
    netbox_url: str
    zabbix_url: str
    lab_api_url: str
    lab_api_url_labnet: str
    trap_receiver_ip: str
    devices: dict[str, DeviceEndpoints]
    sla_slo: dict[str, float]


def device_endpoints(topology: Topology, hostname: str) -> DeviceEndpoints:
    d = topology.device(hostname)
    ports = topology.device_ports
    bind = topology.host_bind
    return DeviceEndpoints(
        hostname=d.hostname,
        mgmt_ip=d.mgmt_ip,
        loopback=d.loopback,
        restconf_url=f"http://{bind}:{topology.host_port(hostname, 'restconf')}",
        restconf_url_labnet=f"http://{d.mgmt_ip}:{ports.restconf.container}",
        simd_url=f"http://{bind}:{topology.host_port(hostname, 'simd')}",
        simd_url_labnet=f"http://{d.mgmt_ip}:{ports.simd.container}",
        snmp=(bind, topology.host_port(hostname, "snmp")),
        snmp_labnet=(d.mgmt_ip, ports.snmp.container),
    )


def _host_url(topology: Topology, name: str) -> str:
    host = topology.infra(name)
    if host.host_port is None:
        raise ValueError(f"{name} publishes no host port")
    return f"http://{topology.host_bind}:{host.host_port}"


def lab_constants(topology: Topology | None = None) -> LabConstants:
    topo = topology or load_topology()
    core = topo.infra(CORE)
    lab_api = topo.infra(LAB_API)
    return LabConstants(
        labnet_name=topo.network.name,
        labnet_subnet=topo.network.subnet,
        labnet_gateway=topo.network.gateway,
        host_bind=topo.host_bind,
        snmp_community=topo.snmp.community,
        trap_community=topo.snmp.trap_community,
        core_ip=core.mgmt_ip,
        core_url=f"redis://{core.mgmt_ip}:{core.container_port}/0",
        netbox_url=_host_url(topo, NETBOX),
        zabbix_url=_host_url(topo, ZABBIX_WEB),
        lab_api_url=_host_url(topo, LAB_API),
        lab_api_url_labnet=f"http://{lab_api.mgmt_ip}:{lab_api.container_port}",
        trap_receiver_ip=topo.infra(ZABBIX_SNMPTRAPS).mgmt_ip,
        devices={h: device_endpoints(topo, h) for h in topo.hostnames},
        sla_slo={tier.value: spec.slo for tier, spec in topo.sla_tiers.items()},
    )
