"""NetBox changes used by scenarios and by ``reset``, built on session 03's seeder (same objects, same tags).

* :func:`add_service` puts a new service into NetBox exactly as ``netbox-seed`` would have, had it been in
  ``contract/topology.yaml`` (VLAN, spoke/hub subinterfaces, one L2VPN per PW, hub terminations, ``spoke_ac``);
* :func:`remove_service` deletes those objects again;
* :func:`restore` makes NetBox match the topology (``netbox-seed --prune``), used by ``reset``.

Every change fires NetBox's webhook to lab-api, which re-runs ``zabbix-sync`` (debounced).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from netsim_netbox.client import NetBoxClient
from netsim_netbox.seed import seed
from netsim_netbox.settings import load_settings as netbox_settings

from netsim_common.canonical import canonical_dict
from netsim_common.topology import Service, Topology, load_topology

L2VPN_ENDPOINT = "vpn/l2vpns"
VLAN_ENDPOINT = "ipam/vlans"
TERMINATION_ENDPOINT = "vpn/l2vpn-terminations"
INTERFACE_ENDPOINT = "dcim/interfaces"
VLAN_GROUP = "l2vpn-acs"


@dataclass(frozen=True)
class NewService:
    """A service to add in NetBox: a primary PW to ``primary_hub`` and, unless ``backup_vc`` is None (unprotected),
    a backup PW to ``backup_hub`` (contract section 5: the spoke's region's hub pair, hub-a/hub-b in North)."""

    id: str
    customer: str
    spoke: str
    vlan: int
    primary_vc: int
    backup_vc: int | None = None
    primary_hub: str = "hub-a"
    backup_hub: str = "hub-b"


def topology_with_service(topology: Topology, svc: NewService) -> Topology:
    """The topology plus ``svc`` (its ACs follow the topology's naming: ``<access interface>.<vlan>``)."""
    if any(s.id == svc.id for s in topology.services):
        raise ValueError(f"{svc.id} is already in the topology")
    spoke = topology.device(svc.spoke)
    hub = topology.device(svc.primary_hub)
    spoke_access = next(i.name for i in spoke.interfaces if i.type.value == "access")
    hub_access = next(i.name for i in hub.interfaces if i.type.value == "access")
    service = Service.model_validate(
        {
            "id": svc.id,
            "customer": svc.customer,
            "spoke": svc.spoke,
            "vlan": svc.vlan,
            "spoke_ac": f"{spoke_access}.{svc.vlan}",
            "hub_ac": f"{hub_access}.{svc.vlan}",
            "pseudowires": {
                "primary": {"hub": svc.primary_hub, "vc_id": svc.primary_vc},
                **({"backup": {"hub": svc.backup_hub, "vc_id": svc.backup_vc}} if svc.backup_vc is not None else {}),
            },
        }
    )
    data = topology.model_dump(mode="json", by_alias=True)
    data["services"] = [*data["services"], service.model_dump(mode="json", by_alias=True)]
    return Topology.model_validate(data)


def _rw_client() -> NetBoxClient:
    return netbox_settings().client(write=True)


def add_service(svc: NewService, topology: Topology | None = None) -> dict[str, Any]:
    """Create the service's NetBox objects (nothing else changes: no prune, webhooks left alone)."""
    topo = topology_with_service(topology or load_topology(), svc)
    with _rw_client() as client:
        report = seed(client, topo, source=f"scenario: add {svc.id}")
    return {"service": svc.id, "changes": [str(c) for c in report.changes], "summary": report.summary()}


def remove_service(service_id: str) -> dict[str, Any]:
    """Delete a service from NetBox: its L2VPNs (their terminations go with them), its subinterfaces and its VLAN.

    The subinterfaces are taken from the L2VPNs themselves (``spoke_ac`` and the hub termination) before those are
    deleted, plus any interface still carrying the service VLAN (``?vlan_id=`` is the VLAN *object* id in NetBox).
    """
    deleted: list[str] = []
    with _rw_client() as client:
        interface_ids: set[int] = set()
        l2vpns = client.list(L2VPN_ENDPOINT, cf_service_id=service_id)
        for obj in l2vpns:
            spoke_ac = (obj.get("custom_fields") or {}).get("spoke_ac") or {}
            if spoke_ac.get("id"):
                interface_ids.add(spoke_ac["id"])
            for term in client.list(TERMINATION_ENDPOINT, l2vpn_id=obj["id"]):
                if term.get("assigned_object_type") == "dcim.interface" and term.get("assigned_object_id"):
                    interface_ids.add(term["assigned_object_id"])
        vlans = [
            v for v in client.list(VLAN_ENDPOINT, name=service_id) if (v.get("group") or {}).get("slug") == VLAN_GROUP
        ]
        for vlan in vlans:
            interface_ids.update(i["id"] for i in client.list(INTERFACE_ENDPOINT, vlan_id=vlan["id"]))
        for obj in l2vpns:
            client.delete(L2VPN_ENDPOINT, obj["id"])
            deleted.append(f"l2vpn {obj['name']}")
        for iface_id in sorted(interface_ids):
            iface = client.get(INTERFACE_ENDPOINT, iface_id)
            if iface.get("parent"):  # only subinterfaces; never a physical interface
                client.delete(INTERFACE_ENDPOINT, iface_id)
                deleted.append(f"interface {iface['device']['name']} {iface['name']}")
        for vlan in vlans:
            client.delete(VLAN_ENDPOINT, vlan["id"])
            deleted.append(f"vlan {vlan['name']}")
    return {"service": service_id, "deleted": deleted}


def restore(dry_run: bool = False) -> dict[str, Any]:
    """Make NetBox match ``contract/topology.yaml`` (``netbox-seed --prune``, webhooks left alone)."""
    with _rw_client() as client:
        report = seed(client, load_topology(), dry_run=dry_run, prune=True, source="lab reset")
    return {"changes": [str(c) for c in report.changes], "summary": report.summary(), "dry_run": dry_run}


def intent_equals_golden(intent: Any, golden: dict[str, Any]) -> list[str]:
    """Hosts whose NetBox-rendered intent differs from golden, plus services NetBox has that golden has not."""
    from netsim_netbox.intent import IntentError, render_device

    out = []
    for host, cfg in golden.items():
        try:
            if canonical_dict(render_device(host, intent)) != canonical_dict(cfg):
                out.append(host)
        except IntentError:
            out.append(host)
    return out
