"""NetBox object kinds and the names of the lab's data model (contract section 10), shared by seeder and renderer.

The seeder writes these names, the intent renderer reads them; nothing else in NetBox is special:

* lab object: tag ``netsim-lab`` on everything the seeder creates (``--prune`` only ever deletes such objects);
* PE device: device with platform ``netsim-clixon`` (role ``hub`` / ``spoke``);
* loopback: IP address with role ``loopback`` on the device (interface ``Loopback0``) -> ``system/loopback-ipv4``;
* physical config interface: interface with custom field ``if_role`` = ``core-uplink`` / ``access`` (YANG ``type``);
* subinterface: interface with a ``parent``; its VLAN = its single tagged VLAN (802.1Q mode ``tagged``);
* pseudowire: L2VPN tagged ``l2vpn-pw``, type ``vpws``, ``identifier`` = vc-id, tenant = customer, custom fields
  ``service_id``, ``pw_role`` and ``spoke_ac``. Spoke end = ``spoke_ac`` (object custom field -> the spoke
  subinterface); hub end = the L2VPN's termination (the hub subinterface);
* customer: tenant of group ``l2vpn-customers`` with custom field ``sla_tier``.

Why the spoke end is a custom field: NetBox allows one L2VPN termination per interface (4.7.3
``L2VPNTermination.clean()``), and both PWs of a service (primary, backup) share the spoke's subinterface. Each hub
subinterface carries exactly one PW, so the hub end stays a real termination.
"""

from __future__ import annotations

from dataclasses import dataclass

LAB_TAG = "netsim-lab"
PW_TAG = "l2vpn-pw"
PLATFORM = "netsim-clixon"
MANUFACTURER = "netsim"
DEVICE_TYPE = "netsim-pe"
TENANT_GROUP = "l2vpn-customers"
VLAN_GROUP = "l2vpn-acs"

CF_SLA_TIER = "sla_tier"
CF_SERVICE_ID = "service_id"
CF_PW_ROLE = "pw_role"
CF_SPOKE_AC = "spoke_ac"
CF_IF_ROLE = "if_role"

MGMT_IF = "Mgmt0"
LOOPBACK_IF = "Loopback0"
IF_TYPE_VIRTUAL = "virtual"
IF_TYPE_PHYSICAL = "1000base-t"
MODE_TAGGED = "tagged"
L2VPN_TYPE = "vpws"
IP_ROLE_LOOPBACK = "loopback"
INTERFACE_OBJECT = "dcim.interface"


@dataclass(frozen=True)
class Kind:
    """A NetBox object type: ``name`` is the object type (``dcim.interface``), ``endpoint`` its API path."""

    name: str
    endpoint: str
    label: str
    taggable: bool = True

    def ui_url(self, base: str, object_id: int) -> str:
        """UI page of an object (NetBox UI paths mirror the API paths without ``/api``)."""
        return f"{base.rstrip('/')}/{self.endpoint}/{object_id}/"


_KINDS = [
    Kind("extras.tag", "extras/tags", "tag", taggable=False),
    Kind("extras.customfieldchoiceset", "extras/custom-field-choice-sets", "choice set", taggable=False),
    Kind("extras.customfield", "extras/custom-fields", "custom field", taggable=False),
    Kind("dcim.region", "dcim/regions", "region"),
    Kind("dcim.site", "dcim/sites", "site"),
    Kind("dcim.manufacturer", "dcim/manufacturers", "manufacturer"),
    Kind("dcim.devicetype", "dcim/device-types", "device type"),
    Kind("dcim.platform", "dcim/platforms", "platform"),
    Kind("dcim.devicerole", "dcim/device-roles", "device role"),
    Kind("tenancy.tenantgroup", "tenancy/tenant-groups", "tenant group"),
    Kind("tenancy.tenant", "tenancy/tenants", "tenant"),
    Kind("ipam.vlangroup", "ipam/vlan-groups", "VLAN group"),
    Kind("ipam.vlan", "ipam/vlans", "VLAN"),
    Kind("dcim.device", "dcim/devices", "device"),
    Kind("dcim.interface", "dcim/interfaces", "interface"),
    Kind("ipam.ipaddress", "ipam/ip-addresses", "IP address"),
    Kind("vpn.l2vpn", "vpn/l2vpns", "L2VPN"),
    Kind("vpn.l2vpntermination", "vpn/l2vpn-terminations", "L2VPN termination"),
    Kind("extras.webhook", "extras/webhooks", "webhook"),
    Kind("extras.eventrule", "extras/event-rules", "event rule"),
    Kind("extras.journalentry", "extras/journal-entries", "journal entry"),
]
KINDS: dict[str, Kind] = {k.name: k for k in _KINDS}


def kind(name: str) -> Kind:
    return KINDS[name]


def slugify(name: str) -> str:
    """NetBox-style slug of a lab name (``Customer-A`` -> ``customer-a``, ``SVC-0001-primary`` -> ``svc-0001-...``)."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in name.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")
