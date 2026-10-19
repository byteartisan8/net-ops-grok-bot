"""What zabbix-provision / zabbix-sync build Zabbix from: the BSM spec (contract 9.3) and the PE hosts.

* ``netbox`` (default): one consistent NetBox snapshot (read-only token) -> ``netsim_netbox.intent.bsm_spec`` and
  the devices with platform ``netsim-clixon`` (name, primary IPv4 = SNMP address, role, site, inventory, URL).
* ``topology``: the same from ``contract/topology.yaml`` (``bsm_spec_from_topology``), for parallel development.

Either way the spec is validated against ``contract/bsm-spec.schema.json`` before anything is written to Zabbix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import jsonschema

from netsim_common.bsm import BsmSpec, bsm_spec_from_topology
from netsim_common.paths import contract_dir
from netsim_common.topology import Topology, load_topology


class SpecError(ValueError):
    """The BSM spec or the host list cannot be used."""


@dataclass(frozen=True)
class HostSpec:
    """One PE as Zabbix should monitor it."""

    name: str
    mgmt_ip: str
    role: str
    site: str | None
    netbox_url: str | None = None
    inventory: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LabSpec:
    source: str
    hosts: list[HostSpec]
    bsm: BsmSpec
    sla_slo: dict[str, float]

    def host(self, name: str) -> HostSpec | None:
        return next((h for h in self.hosts if h.name == name), None)


@cache
def _schema() -> dict[str, Any]:
    return json.loads((contract_dir() / "bsm-spec.schema.json").read_text(encoding="utf-8"))


def validate_bsm(spec: BsmSpec) -> None:
    """Raise :class:`SpecError` unless ``spec`` validates against ``contract/bsm-spec.schema.json``."""
    try:
        jsonschema.validate(json.loads(spec.to_json()), _schema())
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        raise SpecError(f"BSM spec does not match contract/bsm-spec.schema.json at /{where}: {exc.message}") from exc


def _sla_slo(topology: Topology) -> dict[str, float]:
    return {tier.value: tier_spec.slo for tier, tier_spec in topology.sla_tiers.items()}


# ------------------------------------------------------------------------------------------------ topology
def from_topology(topology: Topology | None = None) -> LabSpec:
    topo = topology or load_topology()
    hosts = [
        HostSpec(
            name=d.hostname,
            mgmt_ip=d.mgmt_ip,
            role=d.role.value,
            site=d.site,
            inventory=_inventory(d.hostname, d.role.value, d.site, "NetSim", "NetSim PE", "netsim-clixon", d.loopback),
        )
        for d in topo.devices
    ]
    spec = bsm_spec_from_topology(topo)
    validate_bsm(spec)
    return LabSpec(source="topology", hosts=hosts, bsm=spec, sla_slo=_sla_slo(topo))


# ------------------------------------------------------------------------------------------------ NetBox
def from_netbox() -> LabSpec:
    from netsim_netbox.intent import bsm_spec, build_intent, load_snapshot
    from netsim_netbox.intent.model import IntentError
    from netsim_netbox.settings import load_settings

    settings = load_settings()
    with settings.client() as ro:
        snapshot = load_snapshot(ro)
    intent = build_intent(snapshot, settings.public_url)
    try:
        spec = bsm_spec(intent)
    except IntentError as exc:
        raise SpecError(str(exc)) from exc
    validate_bsm(spec)
    hosts = []
    for dev in snapshot.devices:
        name = dev["name"]
        ip = (dev.get("primary_ip4") or {}).get("address", "").split("/")[0]
        if not ip:
            raise SpecError(f"NetBox device {name} has no primary IPv4 address (needed as its SNMP interface)")
        intent_dev = intent.devices.get(name)
        role = (dev.get("role") or {}).get("slug") or ""
        site = (dev.get("site") or {}).get("name")
        dtype = dev.get("device_type") or {}
        hosts.append(
            HostSpec(
                name=name,
                mgmt_ip=ip,
                role=role,
                site=site,
                netbox_url=intent_dev.url if intent_dev else None,
                inventory=_inventory(
                    name,
                    role,
                    site,
                    (dtype.get("manufacturer") or {}).get("name", ""),
                    dtype.get("model", ""),
                    (dev.get("platform") or {}).get("name", ""),
                    intent_dev.loopback if intent_dev else None,
                    serial=dev.get("serial") or "",
                    asset_tag=dev.get("asset_tag") or "",
                    status=(dev.get("status") or {}).get("value", ""),
                    url=intent_dev.url if intent_dev else "",
                ),
            )
        )
    return LabSpec(
        source="netbox", hosts=sorted(hosts, key=lambda h: h.name), bsm=spec, sla_slo=_sla_slo(load_topology())
    )


def _inventory(
    name: str,
    role: str,
    site: str | None,
    vendor: str,
    model: str,
    platform: str,
    loopback: str | None,
    *,
    serial: str = "",
    asset_tag: str = "",
    status: str = "",
    url: str = "",
) -> dict[str, str]:
    """Zabbix host inventory fields (manual mode) filled from the source."""
    inv = {
        "name": name,
        "type": role,
        "vendor": vendor,
        "model": model,
        "os": platform,
        "location": site or "",
        "serialno_a": serial,
        "asset_tag": asset_tag,
        "deployment_status": status,
        "url_a": url,
        "notes": f"Loopback0 {loopback}" if loopback else "",
    }
    return {k: v for k, v in inv.items() if v}


def load_lab_spec(source: str = "netbox") -> LabSpec:
    if source == "netbox":
        return from_netbox()
    if source == "topology":
        return from_topology()
    raise SpecError(f"unknown spec source {source!r} (netbox or topology)")
