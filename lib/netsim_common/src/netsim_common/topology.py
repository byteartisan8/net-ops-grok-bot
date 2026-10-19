"""Lab topology model and loader (``contract/topology.yaml``, contract sections 4, 5 and 10).

The YAML holds the physical facts (devices, physical interfaces, customers, services). Subinterfaces,
expected pseudowires and host ports are derived here so every consumer derives them the same way:

* a service's AC on a device is the subinterface ``<access interface>.<vlan>``;
* the spoke terminates both PWs of its services; each hub terminates the PWs whose ``hub`` it is;
* a PW's peer address is the other end's loopback;
* published host port = ``host_base + last octet of the mgmt IP`` (e.g. RESTCONF of spoke-1:
  18000 + 21 = 18021), which is the contract's ``180<last-octet>`` scheme written numerically.
"""

from __future__ import annotations

import functools
import ipaddress
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from netsim_common.datatypes import CustomerName, Hostname, InterfaceName, IPv4, Mtu, Port, ServiceId, VcId, VlanId
from netsim_common.enums import DeviceRole, InterfaceType, PwRole, SlaTier
from netsim_common.paths import contract_dir

TOPOLOGY_FILE = "topology.yaml"
DeviceService = Literal["restconf", "simd", "snmp"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _dups(values: list[str]) -> list[str]:
    return sorted(v for v, n in Counter(values).items() if n > 1)


class Network(_Model):
    """The external Docker network every compose fragment attaches to."""

    name: str = Field(description="Docker network name", examples=["labnet"])
    subnet: str = Field(description="IPv4 subnet in CIDR notation", examples=["172.30.10.0/24"])
    gateway: IPv4

    @model_validator(mode="after")
    def _gateway_in_subnet(self) -> Self:
        net = ipaddress.IPv4Network(self.subnet, strict=True)
        if ipaddress.IPv4Address(self.gateway) not in net:
            raise ValueError(f"gateway {self.gateway} is not in {self.subnet}")
        return self

    @property
    def ipv4_network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet)


class PortScheme(_Model):
    """A device container port and the base of its published host port."""

    container: Port
    protocol: Literal["tcp", "udp"]
    host_base: Port = Field(description="Host port = host_base + last octet of the device's mgmt IP")


class DevicePorts(_Model):
    restconf: PortScheme = Field(description="Clixon native RESTCONF (HTTP, no auth)")
    simd: PortScheme = Field(description="simd HTTP API")
    snmp: PortScheme = Field(description="SNMP v2c agent (snmpsim)")


class Snmp(_Model):
    version: Literal["2c"]
    community: str = Field(description="Read community")
    trap_community: str


class InfraHost(_Model):
    """A non-PE container with a fixed address on the lab network."""

    name: Hostname
    mgmt_ip: IPv4
    owner: str = Field(description="Owning build session")
    description: str
    container_port: Port | None = Field(default=None, description="Service port inside the container, if fixed")
    host_port: Port | None = Field(default=None, description="Port published on host_bind, if any")


class PhysicalInterface(_Model):
    name: InterfaceName
    type: Literal[InterfaceType.CORE_UPLINK, InterfaceType.ACCESS]
    mtu: Mtu
    description: str = Field(max_length=64)


class Device(_Model):
    """A simulated PE router."""

    hostname: Hostname
    role: DeviceRole
    site: str = Field(description="NetBox site", examples=["BR-01"])
    mgmt_ip: IPv4 = Field(description="Container IP on the lab network (SNMP/RESTCONF/simd)")
    loopback: IPv4 = Field(description="Simulated Loopback0: router ID and PW peer address")
    interfaces: list[PhysicalInterface] = Field(min_length=2)

    @model_validator(mode="after")
    def _one_uplink_one_access(self) -> Self:
        if _dups([i.name for i in self.interfaces]):
            raise ValueError(f"{self.hostname}: duplicate interface names")
        for kind in (InterfaceType.CORE_UPLINK, InterfaceType.ACCESS):
            if sum(1 for i in self.interfaces if i.type is kind) != 1:
                raise ValueError(f"{self.hostname}: exactly one {kind} interface is required")
        return self

    @property
    def core_uplink(self) -> PhysicalInterface:
        return next(i for i in self.interfaces if i.type is InterfaceType.CORE_UPLINK)

    @property
    def access_interface(self) -> PhysicalInterface:
        return next(i for i in self.interfaces if i.type is InterfaceType.ACCESS)

    @property
    def last_octet(self) -> int:
        return int(self.mgmt_ip.rsplit(".", 1)[1])


class Customer(_Model):
    name: CustomerName
    sla_tier: SlaTier


class SlaTierSpec(_Model):
    slo: float = Field(gt=0, le=100, description="SLA objective in percent", examples=[99.95])


class ServicePw(_Model):
    hub: Hostname
    vc_id: VcId


class ServicePseudowires(_Model):
    primary: ServicePw = Field(description="Primary PW (spoke <-> hub-a), VC ID 1000+n")
    backup: ServicePw = Field(description="Backup PW (spoke <-> hub-b), VC ID 2000+n")

    def get(self, role: PwRole | str) -> ServicePw:
        return getattr(self, PwRole(role).value)


class Service(_Model):
    """One customer L2VPN: a spoke attachment circuit, dual-homed to two hubs."""

    id: ServiceId
    customer: CustomerName
    spoke: Hostname
    vlan: VlanId
    spoke_ac: InterfaceName = Field(description="Spoke subinterface <spoke access interface>.<vlan>")
    hub_ac: InterfaceName = Field(description="Hub subinterface <hub access interface>.<vlan> (same on both hubs)")
    pseudowires: ServicePseudowires

    def pw(self, role: PwRole | str) -> ServicePw:
        return self.pseudowires.get(role)

    @property
    def hubs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.pw(role).hub for role in PwRole))


@dataclass(frozen=True, slots=True)
class ExpectedPw:
    """A pseudowire as seen from one of its two ends."""

    service_id: str
    role: PwRole
    customer: str
    vc_id: int
    local_host: str
    local_ac: str
    peer_host: str
    peer_loopback: str


@dataclass(frozen=True, slots=True)
class Subinterface:
    """A service attachment circuit on one device."""

    name: str
    parent: str
    vlan: int
    description: str
    service_id: str


def subinterface_description(customer: str, service_id: str) -> str:
    """Description of a service subinterface, e.g. ``Customer-A SVC-0001`` (golden-config rule)."""
    return f"{customer} {service_id}"


class Topology(_Model):
    """The whole lab (``contract/topology.yaml``)."""

    schema_version: Literal[1]
    region: str = Field(description="NetBox region of all sites")
    network: Network
    host_bind: IPv4 = Field(description="Host address that published ports bind to (never beyond localhost)")
    device_ports: DevicePorts
    snmp: Snmp
    sla_tiers: dict[SlaTier, SlaTierSpec]
    infrastructure: list[InfraHost]
    devices: list[Device]
    customers: list[Customer]
    services: list[Service]

    # ------------------------------------------------------------------ validation
    @model_validator(mode="after")
    def _consistency(self) -> Self:
        errors: list[str] = []
        names = [d.hostname for d in self.devices] + [h.name for h in self.infrastructure]
        if dups := _dups(names):
            errors.append(f"duplicate host names: {dups}")
        net = self.network.ipv4_network
        ips = [d.mgmt_ip for d in self.devices] + [h.mgmt_ip for h in self.infrastructure]
        if dups := _dups(ips):
            errors.append(f"duplicate mgmt IPs: {dups}")
        for ip in ips:
            addr = ipaddress.IPv4Address(ip)
            if addr not in net or addr in (net.network_address, net.broadcast_address) or ip == self.network.gateway:
                errors.append(f"mgmt IP {ip} is not a usable host address of {net}")
        if dups := _dups([d.loopback for d in self.devices]):
            errors.append(f"duplicate loopbacks: {dups}")
        for d in self.devices:
            if ipaddress.IPv4Address(d.loopback) in net:
                errors.append(f"{d.hostname}: loopback {d.loopback} must not be on the lab network")
        if not self.devices_by_role(DeviceRole.HUB) or not self.devices_by_role(DeviceRole.SPOKE):
            errors.append("at least one hub and one spoke are required")
        if dups := _dups([c.name for c in self.customers]):
            errors.append(f"duplicate customers: {dups}")
        for c in self.customers:
            if c.sla_tier not in self.sla_tiers:
                errors.append(f"customer {c.name}: sla_tier {c.sla_tier} not defined in sla_tiers")
        if dups := _dups([s.id for s in self.services]):
            errors.append(f"duplicate service ids: {dups}")
        errors += self._service_errors()
        errors += self._port_errors()
        if errors:
            raise ValueError("invalid topology:\n  - " + "\n  - ".join(errors))
        return self

    def _service_errors(self) -> list[str]:
        errors: list[str] = []
        devices = {d.hostname: d for d in self.devices}
        customers = {c.name for c in self.customers}
        for s in self.services:
            where = f"service {s.id}"
            if s.customer not in customers:
                errors.append(f"{where}: unknown customer {s.customer}")
            spoke = devices.get(s.spoke)
            if spoke is None or spoke.role is not DeviceRole.SPOKE:
                errors.append(f"{where}: {s.spoke} is not a spoke")
            elif s.spoke_ac != f"{spoke.access_interface.name}.{s.vlan}":
                errors.append(f"{where}: spoke_ac must be {spoke.access_interface.name}.{s.vlan}")
            hubs = [s.pw(role).hub for role in PwRole]
            if len(set(hubs)) != len(hubs):
                errors.append(f"{where}: primary and backup must terminate on different hubs")
            for role in PwRole:
                hub = devices.get(s.pw(role).hub)
                if hub is None or hub.role is not DeviceRole.HUB:
                    errors.append(f"{where}: {role} hub {s.pw(role).hub} is not a hub")
                elif s.hub_ac != f"{hub.access_interface.name}.{s.vlan}":
                    errors.append(f"{where}: hub_ac must be {hub.access_interface.name}.{s.vlan} on {hub.hostname}")
        if errors:
            return errors
        for d in self.devices:
            if dups := _dups([str(sub.vlan) for sub in self.subinterfaces(d.hostname)]):
                errors.append(f"{d.hostname}: VLAN(s) {dups} used by more than one service")
            pairs = [f"{pw.peer_loopback}/{pw.vc_id}" for pw in self.pseudowires_on(d.hostname)]
            if dups := _dups(pairs):
                errors.append(f"{d.hostname}: duplicate (peer, vc-id) {dups}")
        return errors

    def _port_errors(self) -> list[str]:
        errors: list[str] = []
        ports: list[str] = [f"{h.host_port}/tcp" for h in self.infrastructure if h.host_port is not None]
        for d in self.devices:
            for kind in ("restconf", "simd", "snmp"):
                scheme: PortScheme = getattr(self.device_ports, kind)
                port = scheme.host_base + d.last_octet
                if port > 65535:
                    errors.append(f"{d.hostname}: {kind} host port {port} > 65535")
                ports.append(f"{port}/{scheme.protocol}")
        if dups := _dups(ports):
            errors.append(f"host port collision(s): {dups}")
        return errors

    # ------------------------------------------------------------------ lookups
    def device(self, hostname: str) -> Device:
        for d in self.devices:
            if d.hostname == hostname:
                return d
        raise KeyError(f"unknown device {hostname!r}")

    def infra(self, name: str) -> InfraHost:
        for h in self.infrastructure:
            if h.name == name:
                return h
        raise KeyError(f"unknown infrastructure host {name!r}")

    def customer(self, name: str) -> Customer:
        for c in self.customers:
            if c.name == name:
                return c
        raise KeyError(f"unknown customer {name!r}")

    def service(self, service_id: str) -> Service:
        for s in self.services:
            if s.id == service_id:
                return s
        raise KeyError(f"unknown service {service_id!r}")

    def devices_by_role(self, role: DeviceRole | str) -> list[Device]:
        role = DeviceRole(role)
        return [d for d in self.devices if d.role is role]

    @property
    def hostnames(self) -> list[str]:
        return [d.hostname for d in self.devices]

    @property
    def hubs(self) -> list[Device]:
        return self.devices_by_role(DeviceRole.HUB)

    @property
    def spokes(self) -> list[Device]:
        return self.devices_by_role(DeviceRole.SPOKE)

    @property
    def sites(self) -> list[str]:
        return list(dict.fromkeys(d.site for d in self.devices))

    def services_on(self, hostname: str) -> list[Service]:
        """Services with at least one PW terminating on ``hostname``."""
        self.device(hostname)
        return [s for s in self.services if s.spoke == hostname or hostname in s.hubs]

    def ac_on(self, service: Service, hostname: str) -> str:
        """The service's attachment circuit on ``hostname``."""
        if hostname == service.spoke:
            return service.spoke_ac
        if hostname in service.hubs:
            return service.hub_ac
        raise KeyError(f"service {service.id} does not terminate on {hostname}")

    def pseudowires_on(self, hostname: str) -> list[ExpectedPw]:
        """Every PW terminating on ``hostname`` (sorted by service, then role)."""
        local = self.device(hostname)
        out: list[ExpectedPw] = []
        for s in sorted(self.services, key=lambda s: s.id):
            for role in PwRole:
                pw = s.pw(role)
                if hostname == s.spoke:
                    peer = self.device(pw.hub)
                elif hostname == pw.hub:
                    peer = self.device(s.spoke)
                else:
                    continue
                out.append(
                    ExpectedPw(
                        service_id=s.id,
                        role=role,
                        customer=s.customer,
                        vc_id=pw.vc_id,
                        local_host=local.hostname,
                        local_ac=self.ac_on(s, hostname),
                        peer_host=peer.hostname,
                        peer_loopback=peer.loopback,
                    )
                )
        return out

    def subinterfaces(self, hostname: str) -> list[Subinterface]:
        """Service subinterfaces on ``hostname`` (sorted by name)."""
        device = self.device(hostname)
        subs = [
            Subinterface(
                name=self.ac_on(s, hostname),
                parent=device.access_interface.name,
                vlan=s.vlan,
                description=subinterface_description(s.customer, s.id),
                service_id=s.id,
            )
            for s in self.services_on(hostname)
        ]
        return sorted(subs, key=lambda x: x.name)

    # ------------------------------------------------------------------ addressing
    def host_port(self, hostname: str, service: DeviceService) -> int:
        """Published host port of a device service (bound to :attr:`host_bind`)."""
        scheme: PortScheme = getattr(self.device_ports, service)
        return scheme.host_base + self.device(hostname).last_octet


@functools.cache
def _load(path: str, mtime_ns: int) -> Topology:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Topology.model_validate(data)


def load_topology(path: str | Path | None = None) -> Topology:
    """Load and validate ``contract/topology.yaml`` (or ``path``). Results are cached per file version."""
    file = Path(path) if path is not None else contract_dir() / TOPOLOGY_FILE
    file = file.resolve()
    return _load(str(file), file.stat().st_mtime_ns)
