"""Read the NetBox objects the intent is built from (read-only), as the raw REST API JSON.

A :class:`Snapshot` is everything :func:`~netsim_netbox.intent.model.build_intent` needs, fetched in a handful of
list requests (all devices with platform ``netsim-clixon``, their interfaces and IP addresses, the L2VPNs tagged
``l2vpn-pw`` with their terminations, and the customer tenants). It round-trips through JSON
(``netbox-intent snapshot``), which is how the unit tests render real NetBox data without a running NetBox.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from netsim_netbox.client import NetBoxClient
from netsim_netbox.objects import PLATFORM, PW_TAG, TENANT_GROUP

#: Max values of one repeated query parameter (keeps URLs short at scale).
QUERY_CHUNK = 100

Obj = dict[str, Any]


@dataclass
class Snapshot:
    netbox_version: str = ""
    devices: list[Obj] = field(default_factory=list)
    interfaces: list[Obj] = field(default_factory=list)
    ip_addresses: list[Obj] = field(default_factory=list)
    l2vpns: list[Obj] = field(default_factory=list)
    terminations: list[Obj] = field(default_factory=list)
    tenants: list[Obj] = field(default_factory=list)
    tenant_groups: list[Obj] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Snapshot:
        return cls(**json.loads(text))

    @classmethod
    def load(cls, path: Path) -> Snapshot:
        return cls.from_json(path.read_text(encoding="utf-8"))


def _chunked(client: NetBoxClient, endpoint: str, param: str, values: list[int], **extra: Any) -> list[Obj]:
    out: list[Obj] = []
    for start in range(0, len(values), QUERY_CHUNK):
        out += client.list(endpoint, **{param: values[start : start + QUERY_CHUNK]}, **extra)
    return out


def _exists(client: NetBoxClient, endpoint: str, slug: str) -> bool:
    """NetBox answers HTTP 400 to a slug filter naming an unknown object, so look it up first."""
    return bool(client.list(endpoint, slug=slug, limit=1))


def _by_id(objs: list[Obj]) -> list[Obj]:
    return sorted({o["id"]: o for o in objs}.values(), key=lambda o: o["id"])


def load_snapshot(client: NetBoxClient) -> Snapshot:
    """Fetch the intent-relevant NetBox objects (any token with view permission will do)."""
    snap = Snapshot(netbox_version=str(client.status().get("netbox-version", "")))
    if _exists(client, "dcim/platforms", PLATFORM):
        snap.devices = _by_id(client.list("dcim/devices", platform=PLATFORM))
    device_ids = [d["id"] for d in snap.devices]
    if device_ids:
        snap.interfaces = _by_id(_chunked(client, "dcim/interfaces", "device_id", device_ids))
        snap.ip_addresses = _by_id(_chunked(client, "ipam/ip-addresses", "device_id", device_ids))
    if _exists(client, "extras/tags", PW_TAG):
        snap.l2vpns = _by_id(client.list("vpn/l2vpns", tag=PW_TAG))
    l2vpn_ids = [o["id"] for o in snap.l2vpns]
    if l2vpn_ids:
        snap.terminations = _by_id(_chunked(client, "vpn/l2vpn-terminations", "l2vpn_id", l2vpn_ids))
    tenants: list[Obj] = []
    groups = client.list("tenancy/tenant-groups", slug=TENANT_GROUP)
    snap.tenant_groups = groups
    if groups:
        tenants += client.list("tenancy/tenants", group_id=[g["id"] for g in groups])
    owner_ids = sorted({o["tenant"]["id"] for o in snap.l2vpns if o.get("tenant")})
    if owner_ids:
        tenants += _chunked(client, "tenancy/tenants", "id", owner_ids)
    snap.tenants = _by_id(tenants)
    return snap
