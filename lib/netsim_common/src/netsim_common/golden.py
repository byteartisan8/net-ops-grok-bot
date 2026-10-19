"""Render golden device configs from the topology (contract section 6 rules).

For device ``H``:

* ``system``: ``hostname = H``, ``loopback-ipv4`` = its loopback;
* ``interfaces``: every physical interface (``type``, ``mtu``, ``description``, ``enabled: true``) plus, per
  service terminating on ``H``, the subinterface ``<access>.<vlan>`` (``type: subinterface``, ``parent``,
  ``vlan-id``, ``description: "<customer> <service-id>"``, ``enabled: true``, no ``mtu`` = inherit);
* ``l2vpn``: per service terminating on ``H``, xconnect ``<service-id>`` with ``customer``,
  ``attachment-circuit`` and ``enabled: true``; its PWs are the ones terminating on ``H`` (both roles on
  the spoke; on a hub only the role(s) it is the hub for), with ``peer-ipv4`` = the other end's loopback.

Golden files are ``canonical_json(render_device_config(topology, H))``. NetBox's intent renderer
(session 03) must produce the same canonical JSON from NetBox data alone.
"""

from __future__ import annotations

from netsim_common.canonical import canonical_json
from netsim_common.device_config import DeviceConfig, Interface, Interfaces, L2vpn, Pseudowire, System, Xconnect
from netsim_common.enums import InterfaceType
from netsim_common.topology import Topology


def render_device_config(topology: Topology, hostname: str) -> DeviceConfig:
    """Golden config of ``hostname``."""
    device = topology.device(hostname)
    interfaces = [
        Interface(name=i.name, type=i.type, enabled=True, mtu=i.mtu, description=i.description)
        for i in device.interfaces
    ]
    interfaces += [
        Interface(
            name=sub.name,
            type=InterfaceType.SUBINTERFACE,
            parent=sub.parent,
            vlan_id=sub.vlan,
            enabled=True,
            description=sub.description,
        )
        for sub in topology.subinterfaces(hostname)
    ]
    pws_by_service: dict[str, list[Pseudowire]] = {}
    for pw in topology.pseudowires_on(hostname):
        pws_by_service.setdefault(pw.service_id, []).append(
            Pseudowire(role=pw.role, peer_ipv4=pw.peer_loopback, vc_id=pw.vc_id, enabled=True)
        )
    xconnects = [
        Xconnect(
            service_id=s.id,
            customer=s.customer,
            attachment_circuit=topology.ac_on(s, hostname),
            enabled=True,
            pseudowire=pws_by_service[s.id],
        )
        for s in topology.services_on(hostname)
    ]
    return DeviceConfig(
        system=System(hostname=device.hostname, loopback_ipv4=device.loopback),
        interfaces=Interfaces(interface=interfaces),
        l2vpn=L2vpn(xconnect=xconnects),
    )


def render_golden(topology: Topology) -> dict[str, str]:
    """``{hostname: canonical JSON}`` for every device."""
    return {h: canonical_json(render_device_config(topology, h)) for h in topology.hostnames}
