"""Desired NetBox state of the lab, derived from ``contract/topology.yaml`` (contract section 10). Pure: no I/O.

Objects per phase, with the natural key the seeder matches existing objects on:

* tags (slug): ``netsim-lab`` on every seeded object, ``l2vpn-pw`` on pseudowire L2VPNs;
* choice sets, custom fields (name): ``sla_tier`` (tenant), ``service_id`` / ``pw_role`` / ``spoke_ac`` (L2VPN),
  ``if_role`` (interface);
* regions, sites, manufacturer, device type, platform, device roles (slug): ``Lab`` with its child regions
  (``North``, ``South``; each site is in its child region), ``DC-A``..., ``NetSim``, ``NetSim PE``, ``netsim-clixon``,
  ``hub`` / ``spoke``;
* tenant group and tenants (slug): ``L2VPN Customers``, one tenant per customer with ``sla_tier``;
* VLAN group (slug) ``l2vpn-acs`` scoped to the region, one VLAN per service named after it (group, name);
* devices (name); their ``primary_ip4`` is set in the later phase ``primary-ips``;
* interfaces (device, name): ``Mgmt0``, ``Loopback0``, the physical ones (``if_role``), then the subinterfaces;
* IP addresses (host address): ``Mgmt0`` ``/24`` (labnet), ``Loopback0`` ``/32`` with role ``loopback``;
* L2VPNs (slug): one ``vpws`` L2VPN per PW (two per protected service, one per unprotected service);
  terminations (device, interface): the hub end only;
* webhook and event rule (name): NetBox -> lab-api change notifications (``--no-webhooks`` skips them).

VLAN storage on subinterfaces: the VLAN is a real IPAM VLAN object, assigned as the subinterface's single *tagged*
VLAN (802.1Q mode ``tagged``, no untagged VLAN): a dot1q subinterface matches frames tagged with that VID on its
parent, which is what NetBox's ``tagged`` mode means (``access`` would say the frames are untagged).

Pseudowire ends: NetBox allows one L2VPN termination per interface, and a service's two PWs share the spoke
subinterface, so the spoke end is the object custom field ``spoke_ac`` (-> ``dcim.interface``) and the hub end is
the L2VPN's only termination (see :mod:`netsim_netbox.objects`).
"""

from __future__ import annotations

from dataclasses import dataclass

from netsim_common.datatypes import SERVICE_ID_PATTERN
from netsim_common.enums import DeviceRole, InterfaceType, PwRole, SlaTier
from netsim_common.topology import Topology
from netsim_netbox.objects import (
    CF_IF_ROLE,
    CF_PW_ROLE,
    CF_SERVICE_ID,
    CF_SLA_TIER,
    CF_SPOKE_AC,
    DEVICE_TYPE,
    IF_TYPE_PHYSICAL,
    IF_TYPE_VIRTUAL,
    INTERFACE_OBJECT,
    IP_ROLE_LOOPBACK,
    L2VPN_TYPE,
    LAB_TAG,
    LOOPBACK_IF,
    MANUFACTURER,
    MGMT_IF,
    MODE_TAGGED,
    PLATFORM,
    PW_TAG,
    TENANT_GROUP,
    VLAN_GROUP,
    slugify,
)
from netsim_netbox.seed.model import Ref, Spec

#: Processing order: (phase, kind). Every phase only references objects of earlier phases.
PHASES: tuple[tuple[str, str], ...] = (
    ("tags", "extras.tag"),
    ("choice-sets", "extras.customfieldchoiceset"),
    ("custom-fields", "extras.customfield"),
    ("regions", "dcim.region"),
    ("child-regions", "dcim.region"),
    ("sites", "dcim.site"),
    ("manufacturers", "dcim.manufacturer"),
    ("device-types", "dcim.devicetype"),
    ("platforms", "dcim.platform"),
    ("device-roles", "dcim.devicerole"),
    ("tenant-groups", "tenancy.tenantgroup"),
    ("tenants", "tenancy.tenant"),
    ("vlan-groups", "ipam.vlangroup"),
    ("vlans", "ipam.vlan"),
    ("devices", "dcim.device"),
    ("interfaces", "dcim.interface"),
    ("subinterfaces", "dcim.interface"),
    ("ip-addresses", "ipam.ipaddress"),
    ("primary-ips", "dcim.device"),
    ("l2vpns", "vpn.l2vpn"),
    ("l2vpn-terminations", "vpn.l2vpntermination"),
    ("webhooks", "extras.webhook"),
    ("event-rules", "extras.eventrule"),
)

#: Object types whose changes are sent to lab-api (which re-runs zabbix-sync so the BSM follows NetBox).
EVENT_OBJECT_TYPES = (
    "dcim.device",
    "dcim.interface",
    "ipam.ipaddress",
    "tenancy.tenant",
    "vpn.l2vpn",
    "vpn.l2vpntermination",
)
WEBHOOK_NAME = "netsim lab-api: intent changed"
EVENT_RULE_NAME = "netsim: L2VPN intent changes -> lab-api"
WEBHOOK_PATH = "/hooks/netbox"
CF_GROUP = "NetSim L2VPN"

SLA_CHOICES = "netsim SLA tiers"
PW_ROLE_CHOICES = "netsim PW roles"
IF_ROLE_CHOICES = "netsim interface roles"
_SLA_LABELS = {SlaTier.GOLD: "Gold", SlaTier.SILVER: "Silver", SlaTier.BRONZE: "Bronze"}
_PW_ROLE_LABELS = {PwRole.PRIMARY: "Primary", PwRole.BACKUP: "Backup"}
_IF_ROLE_LABELS = {InterfaceType.CORE_UPLINK: "Core uplink", InterfaceType.ACCESS: "Customer access"}
_ROLE_COLORS = {DeviceRole.HUB: "1565c0", DeviceRole.SPOKE: "2e7d32"}


@dataclass(frozen=True)
class WebhookConfig:
    """Where NetBox sends change notifications. ``secret`` signs each body (HMAC-SHA512, header X-Hook-Signature)."""

    url: str
    secret: str


def default_webhook_url() -> str:
    from netsim_common.constants import lab_constants

    return lab_constants().lab_api_url_labnet + WEBHOOK_PATH


def l2vpn_name(service_id: str, role: PwRole | str) -> str:
    """``SVC-0001-primary`` (contract section 10)."""
    return f"{service_id}-{PwRole(role).value}"


def _ref(kind: str, *key: object) -> Ref:
    return Ref(kind, tuple(key))


def _schema() -> list[Spec]:
    specs = [
        Spec(
            "extras.tag",
            (LAB_TAG,),
            {"name": LAB_TAG, "slug": LAB_TAG, "color": "607d8b", "description": "Created by netbox-seed (NetSim lab)"},
            tags=(),
            phase="tags",
        ),
        Spec(
            "extras.tag",
            (PW_TAG,),
            {"name": PW_TAG, "slug": PW_TAG, "color": "9c27b0", "description": "L2VPN object = one NetSim pseudowire"},
            tags=(),
            phase="tags",
        ),
    ]
    for name, labels in (
        (SLA_CHOICES, _SLA_LABELS),
        (PW_ROLE_CHOICES, _PW_ROLE_LABELS),
        (IF_ROLE_CHOICES, _IF_ROLE_LABELS),
    ):
        specs.append(
            Spec(
                "extras.customfieldchoiceset",
                (name,),
                {
                    "name": name,
                    "extra_choices": [[str(value), label] for value, label in labels.items()],
                    "order_alphabetically": False,
                },
                tags=(),
                phase="choice-sets",
            )
        )
    fields = [
        (CF_SLA_TIER, "SLA tier", "tenancy.tenant", "select", SLA_CHOICES, None, "Customer SLA tier (BSM SLA)"),
        (CF_SERVICE_ID, "Service ID", "vpn.l2vpn", "text", None, SERVICE_ID_PATTERN, "Service this PW belongs to"),
        (
            CF_PW_ROLE,
            "PW role",
            "vpn.l2vpn",
            "select",
            PW_ROLE_CHOICES,
            None,
            "Pseudowire role: primary (to the service's primary hub) or backup (to a second hub)",
        ),
        (
            CF_SPOKE_AC,
            "Spoke AC",
            "vpn.l2vpn",
            "object",
            None,
            None,
            "Spoke end of the PW (its subinterface). The hub end is the termination: NetBox allows one termination "
            "per interface and both PWs of a service share the spoke subinterface",
        ),
        (
            CF_IF_ROLE,
            "Interface role",
            INTERFACE_OBJECT,
            "select",
            IF_ROLE_CHOICES,
            None,
            "netsim-device YANG type of a physical interface (subinterfaces: derived from parent)",
        ),
    ]
    for name, label, object_type, cf_type, choice_set, regex, description in fields:
        wanted = {
            "name": name,
            "label": label,
            "type": cf_type,
            "object_types": [object_type],
            "related_object_type": INTERFACE_OBJECT if cf_type == "object" else None,
            "group_name": CF_GROUP,
            "description": description,
            "required": False,
            "choice_set": _ref("extras.customfieldchoiceset", choice_set) if choice_set else None,
            "validation_regex": regex or "",
        }
        specs.append(Spec("extras.customfield", (name,), wanted, tags=(), phase="custom-fields"))
    return specs


def _organization(topo: Topology) -> list[Spec]:
    region = slugify(topo.region)
    specs = [
        Spec(
            "dcim.region",
            (region,),
            {"name": topo.region, "slug": region, "parent": None, "description": "NetSim L2VPN lab"},
            phase="regions",
        )
    ]
    # Child regions get their own phase: the parent must exist (have an ID) before they reference it.
    for child in topo.regions:
        specs.append(
            Spec(
                "dcim.region",
                (slugify(child.name),),
                {
                    "name": child.name,
                    "slug": slugify(child.name),
                    "parent": _ref("dcim.region", region),
                    "description": f"NetSim L2VPN lab region {child.name} ({', '.join(child.sites)})",
                },
                phase="child-regions",
            )
        )
    for site in topo.sites:
        hosts = ", ".join(d.hostname for d in topo.devices if d.site == site)
        specs.append(
            Spec(
                "dcim.site",
                (slugify(site),),
                {
                    "name": site,
                    "slug": slugify(site),
                    "status": "active",
                    "region": _ref("dcim.region", slugify(topo.region_of_site(site))),
                    "description": f"NetSim lab site ({hosts})",
                },
                phase="sites",
            )
        )
    specs += [
        Spec(
            "dcim.manufacturer",
            (MANUFACTURER,),
            {"name": "NetSim", "slug": MANUFACTURER, "description": "NetSim lab (simulated hardware)"},
            phase="manufacturers",
        ),
        Spec(
            "dcim.devicetype",
            (DEVICE_TYPE,),
            {
                "manufacturer": _ref("dcim.manufacturer", MANUFACTURER),
                "model": "NetSim PE",
                "slug": DEVICE_TYPE,
                "description": "Simulated PE router (netsim/netsim-pe container)",
            },
            phase="device-types",
        ),
        Spec(
            "dcim.platform",
            (PLATFORM,),
            {
                "name": PLATFORM,
                "slug": PLATFORM,
                "manufacturer": _ref("dcim.manufacturer", MANUFACTURER),
                "description": "Clixon PE: netsim-device YANG over RESTCONF (intent rendered by netbox-intent)",
            },
            phase="platforms",
        ),
    ]
    for role in DeviceRole:
        specs.append(
            Spec(
                "dcim.devicerole",
                (role.value,),
                {
                    "name": role.value,
                    "slug": role.value,
                    "color": _ROLE_COLORS[role],
                    "vm_role": False,
                    "description": f"NetSim L2VPN {role.value}",
                },
                phase="device-roles",
            )
        )
    return specs


def _tenancy(topo: Topology) -> list[Spec]:
    specs = [
        Spec(
            "tenancy.tenantgroup",
            (TENANT_GROUP,),
            {"name": "L2VPN Customers", "slug": TENANT_GROUP, "description": "Customers of the NetSim L2VPN service"},
            phase="tenant-groups",
        )
    ]
    for c in topo.customers:
        specs.append(
            Spec(
                "tenancy.tenant",
                (slugify(c.name),),
                {
                    "name": c.name,
                    "slug": slugify(c.name),
                    "group": _ref("tenancy.tenantgroup", TENANT_GROUP),
                    "description": "L2VPN customer",
                },
                custom_fields={CF_SLA_TIER: c.sla_tier.value},
                phase="tenants",
            )
        )
    return specs


def _vlans(topo: Topology) -> list[Spec]:
    region = slugify(topo.region)
    specs = [
        Spec(
            "ipam.vlangroup",
            (VLAN_GROUP,),
            {
                "name": "L2VPN attachment circuits",
                "slug": VLAN_GROUP,
                "scope_type": "dcim.region",
                "scope_id": _ref("dcim.region", region),
                "description": "dot1q VLANs of the service subinterfaces (one VLAN per service)",
            },
            phase="vlan-groups",
        )
    ]
    for s in topo.services:
        specs.append(
            Spec(
                "ipam.vlan",
                (VLAN_GROUP, s.id),
                {
                    "group": _ref("ipam.vlangroup", VLAN_GROUP),
                    "vid": s.vlan,
                    "name": s.id,
                    "status": "active",
                    "tenant": _ref("tenancy.tenant", slugify(s.customer)),
                    "description": f"{s.customer} {s.id} attachment circuit",
                },
                phase="vlans",
            )
        )
    return specs


def _devices(topo: Topology) -> list[Spec]:
    prefixlen = topo.network.ipv4_network.prefixlen
    specs: list[Spec] = []
    for d in topo.devices:
        host = d.hostname
        device = _ref("dcim.device", host)
        specs.append(
            Spec(
                "dcim.device",
                (host,),
                {
                    "name": host,
                    "device_type": _ref("dcim.devicetype", DEVICE_TYPE),
                    "role": _ref("dcim.devicerole", d.role.value),
                    "site": _ref("dcim.site", slugify(d.site)),
                    "platform": _ref("dcim.platform", PLATFORM),
                    "status": "active",
                },
                phase="devices",
                device=host,
            )
        )
        virtual = {"type": IF_TYPE_VIRTUAL, "enabled": True, "parent": None}
        specs += [
            Spec(
                "dcim.interface",
                (host, MGMT_IF),
                {"device": device, "name": MGMT_IF, **virtual, "mgmt_only": True, "description": "Management (labnet)"},
                custom_fields={CF_IF_ROLE: None},
                phase="interfaces",
                device=host,
            ),
            Spec(
                "dcim.interface",
                (host, LOOPBACK_IF),
                {
                    "device": device,
                    "name": LOOPBACK_IF,
                    **virtual,
                    "mgmt_only": False,
                    "description": "Router ID and PW peer address (simulated)",
                },
                custom_fields={CF_IF_ROLE: None},
                phase="interfaces",
                device=host,
            ),
        ]
        for i in d.interfaces:
            specs.append(
                Spec(
                    "dcim.interface",
                    (host, i.name),
                    {
                        "device": device,
                        "name": i.name,
                        "type": IF_TYPE_PHYSICAL,
                        "enabled": True,
                        "parent": None,
                        "mtu": i.mtu,
                        "mgmt_only": False,
                        "description": i.description,
                    },
                    custom_fields={CF_IF_ROLE: i.type.value},
                    phase="interfaces",
                    device=host,
                )
            )
        for sub in topo.subinterfaces(host):
            specs.append(
                Spec(
                    "dcim.interface",
                    (host, sub.name),
                    {
                        "device": device,
                        "name": sub.name,
                        "type": IF_TYPE_VIRTUAL,
                        "enabled": True,
                        "parent": _ref("dcim.interface", host, sub.parent),
                        "mtu": None,
                        "mgmt_only": False,
                        "description": sub.description,
                        "mode": MODE_TAGGED,
                        "untagged_vlan": None,
                        "tagged_vlans": [_ref("ipam.vlan", VLAN_GROUP, sub.service_id)],
                    },
                    custom_fields={CF_IF_ROLE: None},
                    phase="subinterfaces",
                    device=host,
                )
            )
        for address, ifname, role, what in (
            (f"{d.mgmt_ip}/{prefixlen}", MGMT_IF, None, "management"),
            (f"{d.loopback}/32", LOOPBACK_IF, IP_ROLE_LOOPBACK, LOOPBACK_IF),
        ):
            specs.append(
                Spec(
                    "ipam.ipaddress",
                    (address.split("/")[0],),
                    {
                        "address": address,
                        "vrf": None,
                        "status": "active",
                        "role": role,
                        "assigned_object_type": INTERFACE_OBJECT,
                        "assigned_object_id": _ref("dcim.interface", host, ifname),
                        "description": f"{host} {what}",
                    },
                    phase="ip-addresses",
                    device=host,
                )
            )
        specs.append(
            Spec(
                "dcim.device",
                (host,),
                {"primary_ip4": _ref("ipam.ipaddress", d.mgmt_ip)},
                tags=(),
                phase="primary-ips",
                device=host,
                update_only=True,
            )
        )
    return specs


def _l2vpns(topo: Topology) -> list[Spec]:
    specs: list[Spec] = []
    for s in topo.services:
        for role in s.roles:
            pw = s.pw(role)
            name = l2vpn_name(s.id, role)
            specs.append(
                Spec(
                    "vpn.l2vpn",
                    (slugify(name),),
                    {
                        "name": name,
                        "slug": slugify(name),
                        "type": L2VPN_TYPE,
                        "identifier": pw.vc_id,
                        "status": "active",
                        "tenant": _ref("tenancy.tenant", slugify(s.customer)),
                        "description": f"{s.id} {role.value} pseudowire",
                    },
                    custom_fields={
                        CF_SERVICE_ID: s.id,
                        CF_PW_ROLE: role.value,
                        CF_SPOKE_AC: _ref("dcim.interface", s.spoke, s.spoke_ac),
                    },
                    tags=(LAB_TAG, PW_TAG),
                    phase="l2vpns",
                    device=s.spoke,
                )
            )
            specs.append(
                Spec(
                    "vpn.l2vpntermination",
                    (pw.hub, s.hub_ac),
                    {
                        "l2vpn": _ref("vpn.l2vpn", slugify(name)),
                        "assigned_object_type": INTERFACE_OBJECT,
                        "assigned_object_id": _ref("dcim.interface", pw.hub, s.hub_ac),
                    },
                    phase="l2vpn-terminations",
                    device=pw.hub,
                )
            )
    return specs


def _webhooks(webhook: WebhookConfig) -> list[Spec]:
    return [
        Spec(
            "extras.webhook",
            (WEBHOOK_NAME,),
            {
                "name": WEBHOOK_NAME,
                "payload_url": webhook.url,
                "http_method": "POST",
                "http_content_type": "application/json",
                "additional_headers": "",
                "body_template": "",
                "secret": webhook.secret,
                "timeout": 10,
                "description": "lab-api re-runs zabbix-sync so the Zabbix BSM follows NetBox",
            },
            phase="webhooks",
        ),
        Spec(
            "extras.eventrule",
            (EVENT_RULE_NAME,),
            {
                "name": EVENT_RULE_NAME,
                "object_types": list(EVENT_OBJECT_TYPES),
                "event_types": ["object_created", "object_updated", "object_deleted"],
                "enabled": True,
                "conditions": None,
                "action_type": "webhook",
                "action_object_type": "extras.webhook",
                "action_object_id": _ref("extras.webhook", WEBHOOK_NAME),
                "description": "Device, interface, IP, tenant and L2VPN changes -> lab-api /hooks/netbox",
            },
            phase="event-rules",
        ),
    ]


def desired_state(topology: Topology, *, webhook: WebhookConfig | None = None) -> list[Spec]:
    """Every object the lab needs in NetBox, in phase order (``webhook=None`` = no webhook/event rule)."""
    specs = _schema() + _organization(topology) + _tenancy(topology) + _vlans(topology) + _devices(topology)
    specs += _l2vpns(topology)
    if webhook is not None:
        specs += _webhooks(webhook)
    order = {phase: i for i, (phase, _) in enumerate(PHASES)}
    return sorted(specs, key=lambda s: order[s.phase])
