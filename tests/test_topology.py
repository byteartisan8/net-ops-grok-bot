"""contract/topology.yaml encodes contract sections 4, 5 and 10 exactly, and validates against its schema."""

from __future__ import annotations

import json

import jsonschema
import pytest
import yaml
from netsim_testlib import CONTRACT, load_tool

from netsim_common.enums import DeviceRole, PwRole
from netsim_common.topology import Topology, load_topology

# Contract section 4 table: host -> (role, site, mgmt IP, loopback)
DEVICES = {
    "hub-a": ("hub", "DC-A", "172.30.10.11", "10.255.0.1"),
    "hub-b": ("hub", "DC-B", "172.30.10.12", "10.255.0.2"),
    "hub-c": ("hub", "DC-C", "172.30.10.13", "10.255.0.3"),
    "hub-d": ("hub", "DC-D", "172.30.10.14", "10.255.0.4"),
    "spoke-1": ("spoke", "BR-01", "172.30.10.21", "10.255.1.1"),
    "spoke-2": ("spoke", "BR-02", "172.30.10.22", "10.255.1.2"),
    "spoke-3": ("spoke", "BR-03", "172.30.10.23", "10.255.1.3"),
    "spoke-4": ("spoke", "BR-04", "172.30.10.24", "10.255.1.4"),
    "spoke-5": ("spoke", "BR-05", "172.30.10.25", "10.255.1.5"),
    "spoke-6": ("spoke", "BR-06", "172.30.10.26", "10.255.1.6"),
}
# Contract section 10: child regions of "Lab" and their sites.
REGIONS = {
    "North": ["DC-A", "DC-B", "BR-01", "BR-02", "BR-03", "BR-04"],
    "South": ["DC-C", "DC-D", "BR-05", "BR-06"],
}
INFRA = {
    "core": "172.30.10.5",
    "netbox": "172.30.10.30",
    "zabbix-server": "172.30.10.40",
    "zabbix-web": "172.30.10.41",
    "zabbix-snmptraps": "172.30.10.42",
    "lab-api": "172.30.10.50",
}
# Contract section 5 table: service -> (customer, spoke, vlan, primary hub, backup hub or None = unprotected)
SERVICES = {
    "SVC-0001": ("Customer-A", "spoke-1", 101, "hub-a", "hub-b"),
    "SVC-0002": ("Customer-A", "spoke-2", 102, "hub-a", "hub-b"),
    "SVC-0003": ("Customer-B", "spoke-2", 201, "hub-a", "hub-b"),
    "SVC-0004": ("Customer-C", "spoke-3", 301, "hub-a", "hub-b"),
    "SVC-0005": ("Customer-D", "spoke-4", 401, "hub-a", "hub-b"),
    "SVC-0006": ("Customer-B", "spoke-4", 202, "hub-a", "hub-b"),
    "SVC-0007": ("Customer-E", "spoke-5", 501, "hub-c", "hub-d"),
    "SVC-0008": ("Customer-E", "spoke-1", 502, "hub-a", "hub-b"),
    "SVC-0009": ("Customer-F", "spoke-5", 601, "hub-c", None),
    "SVC-0010": ("Customer-F", "spoke-4", 602, "hub-a", None),
    "SVC-0011": ("Customer-C", "spoke-6", 302, "hub-c", "hub-d"),
}
# Contract section 10 default tiers; section 9.4 SLOs.
TIERS = {
    "Customer-A": "gold",
    "Customer-B": "gold",
    "Customer-C": "silver",
    "Customer-D": "silver",
    "Customer-E": "gold",
    "Customer-F": "bronze",
}
SLO = {"gold": 99.95, "silver": 99.9, "bronze": 99.5}


@pytest.fixture(scope="module")
def topo() -> Topology:
    return load_topology()


def test_topology_yaml_validates_against_schema():
    schema = json.loads((CONTRACT / "topology.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(yaml.safe_load((CONTRACT / "topology.yaml").read_text(encoding="utf-8")), schema)


def test_schema_files_are_current():
    gen = load_tool("gen_schemas")
    for rel, spec in gen.SCHEMAS.items():
        assert (CONTRACT.parent / rel).read_text(encoding="utf-8") == gen.render(*spec), f"{rel}: run `make contract`"


def test_schema_rejects_structural_errors():
    schema = json.loads((CONTRACT / "topology.schema.json").read_text(encoding="utf-8"))
    data = yaml.safe_load((CONTRACT / "topology.yaml").read_text(encoding="utf-8"))
    data["services"][0]["pseudowires"].pop("primary")
    with pytest.raises(jsonschema.ValidationError, match="primary"):
        jsonschema.validate(data, schema)


def test_schema_allows_an_unprotected_service():
    schema = json.loads((CONTRACT / "topology.schema.json").read_text(encoding="utf-8"))
    data = yaml.safe_load((CONTRACT / "topology.yaml").read_text(encoding="utf-8"))
    assert [s["id"] for s in data["services"] if "backup" not in s["pseudowires"]] == ["SVC-0009", "SVC-0010"]
    jsonschema.validate(data, schema)


def test_network(topo):
    assert (topo.network.name, topo.network.subnet, topo.network.gateway) == ("labnet", "172.30.10.0/24", "172.30.10.1")
    assert topo.host_bind == "127.0.0.1"
    assert topo.region == "Lab"


def test_devices_match_contract_section_4(topo):
    assert {d.hostname: (d.role.value, d.site, d.mgmt_ip, d.loopback) for d in topo.devices} == DEVICES
    assert {h.name: h.mgmt_ip for h in topo.infrastructure} == INFRA


def test_regions(topo):
    assert {r.name: r.sites for r in topo.regions} == REGIONS
    assert {h: topo.region_of(h) for h in topo.hostnames} == {
        h: "North" if site in REGIONS["North"] else "South" for h, (_, site, _, _) in DEVICES.items()
    }


def test_physical_interfaces(topo):
    for d in topo.devices:
        assert (d.core_uplink.name, d.core_uplink.mtu) == ("GigabitEthernet0/0/0", 9000)
        access = "GigabitEthernet0/0/2" if d.role is DeviceRole.HUB else "GigabitEthernet0/0/1"
        assert (d.access_interface.name, d.access_interface.mtu) == (access, 1500)


def test_services_match_contract_section_5(topo):
    got = {
        s.id: (s.customer, s.spoke, s.vlan, s.pw("primary").hub, s.pw("backup").hub if s.protected else None)
        for s in topo.services
    }
    assert got == SERVICES
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    seen: dict[str, int] = {}
    for s in topo.services:
        n = int(s.id.removeprefix("SVC-"))
        seen[s.customer] = seen.get(s.customer, 0) + 1
        assert s.vlan == 100 * (letters.index(s.customer[-1]) + 1) + seen[s.customer]  # VLAN 100*c + j
        assert (s.spoke_ac, s.hub_ac) == (f"GigabitEthernet0/0/1.{s.vlan}", f"GigabitEthernet0/0/2.{s.vlan}")
        assert s.pw(PwRole.PRIMARY).vc_id == 1000 + n
        assert not s.protected or s.pw(PwRole.BACKUP).vc_id == 2000 + n
        # the spoke's region's hub pair: primary to hub-a/hub-c, backup to hub-b/hub-d
        region = topo.region_of(s.spoke)
        assert all(topo.region_of(h) == region for h in s.hubs)
        assert s.pw(PwRole.PRIMARY).hub == {"North": "hub-a", "South": "hub-c"}[region]
        assert not s.protected or s.pw(PwRole.BACKUP).hub == {"North": "hub-b", "South": "hub-d"}[region]
    assert {s.id for s in topo.services if not s.protected} == {
        s for s, (customer, *_) in SERVICES.items() if TIERS[customer] == "bronze"
    }


def test_customers_and_sla_tiers(topo):
    assert {c.name: c.sla_tier.value for c in topo.customers} == TIERS
    assert {t.value: spec.slo for t, spec in topo.sla_tiers.items()} == SLO


def test_pseudowire_totals(topo):
    per_host = {h: len(topo.pseudowires_on(h)) for h in topo.hostnames}
    assert sum(per_host.values()) == 40  # 20 PWs (9 protected services x 2 + 2 unprotected x 1), two ends each
    assert per_host == {
        "hub-a": 8,
        "hub-b": 7,
        "hub-c": 3,
        "hub-d": 2,
        "spoke-1": 4,
        "spoke-2": 4,
        "spoke-3": 2,
        "spoke-4": 5,
        "spoke-5": 3,
        "spoke-6": 2,
    }
    for hub, role in (("hub-a", PwRole.PRIMARY), ("hub-b", PwRole.BACKUP), ("hub-c", PwRole.PRIMARY)):
        assert all(pw.role is role for pw in topo.pseudowires_on(hub))
    assert all(pw.role is PwRole.BACKUP for pw in topo.pseudowires_on("hub-d"))


def test_host_ports(topo):
    assert [topo.host_port("hub-a", k) for k in ("restconf", "simd", "snmp")] == [18011, 19011, 16111]
    assert [topo.host_port("spoke-4", k) for k in ("restconf", "simd", "snmp")] == [18024, 19024, 16124]
    assert [topo.host_port("hub-c", k) for k in ("restconf", "simd", "snmp")] == [18013, 19013, 16113]
    assert {h.name: h.host_port for h in topo.infrastructure if h.host_port} == {
        "netbox": 8000,
        "zabbix-web": 8080,
        "lab-api": 8090,
    }


def test_generator_reproduces_the_contract_topology(topo):
    gen = load_tool("gen_topology")
    assert gen.extend(topo, spokes=6, services=11, customers=6) == topo


def test_generator_scales_with_the_same_schemes(topo, tmp_path):
    gen = load_tool("gen_topology")
    big = gen.extend(topo, spokes=12, services=60, customers=6)
    out = tmp_path / "big.yaml"
    out.write_text(gen.to_yaml(big, ["--spokes", "12"]), encoding="utf-8")
    loaded = load_topology(out)
    schema = json.loads((CONTRACT / "topology.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(yaml.safe_load(out.read_text(encoding="utf-8")), schema)
    assert len(loaded.spokes) == 12 and len(loaded.services) == 60
    assert loaded.device("spoke-9").mgmt_ip == "172.30.10.29"
    assert loaded.device("spoke-10").mgmt_ip == "172.30.10.60"
    assert loaded.region_of("spoke-12") == "North"
    svc = loaded.service("SVC-0059")  # Customer-E (gold): protected
    assert (svc.pw("primary").vc_id, svc.pw("backup").vc_id) == (1059, 2059)
    assert svc.spoke_ac == f"GigabitEthernet0/0/1.{svc.vlan}"
    assert loaded.service("SVC-0060").roles == (PwRole.PRIMARY,)  # Customer-F (bronze): unprotected
    south = [s for s in loaded.services if s.spoke in ("spoke-5", "spoke-6")]
    assert south and all(set(s.hubs) <= {"hub-c", "hub-d"} for s in south)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"spokes": 5, "services": 11, "customers": 6}, "extends the contract topology"),
        ({"spokes": 6, "services": 1000, "customers": 6}, "<= 999"),
        ({"spokes": 6, "services": 700, "customers": 6}, "more than 99 services"),
    ],
)
def test_generator_rejects_impossible_requests(topo, kwargs, message):
    gen = load_tool("gen_topology")
    with pytest.raises(gen.GenerationError, match=message):
        gen.extend(topo, **kwargs)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["services"][0].update(spoke="hub-a"), "is not a spoke"),
        (lambda d: d["services"][0].update(spoke_ac="GigabitEthernet0/0/1.999"), "spoke_ac must be"),
        (lambda d: d["services"][1].update(vlan=101, spoke="spoke-1", spoke_ac="GigabitEthernet0/0/1.101",
                                           hub_ac="GigabitEthernet0/0/2.101"), "used by more than one service"),
        (lambda d: d["services"][0]["pseudowires"]["backup"].update(hub="hub-a"), "different hubs"),
        (lambda d: d["devices"][0].update(mgmt_ip="172.30.10.30"), "duplicate mgmt IPs"),
        (lambda d: d["devices"][0].update(loopback="172.30.10.99"), "must not be on the lab network"),
        (lambda d: d["customers"][0].update(sla_tier="platinum"), "sla_tier"),
        (lambda d: d["sla_tiers"].pop("bronze"), "not defined in sla_tiers"),
        (lambda d: d["regions"][1]["sites"].remove("BR-06"), r"site\(s\) \['BR-06'\] in no region"),
        (lambda d: d["regions"][0]["sites"].append("BR-05"), "in more than one region"),
        (lambda d: d["regions"][0]["sites"].append("BR-99"), "that no device is at"),
        (lambda d: d["services"][8]["pseudowires"].update(backup={"hub": "hub-c", "vc_id": 2009}), "different hubs"),
    ],
)  # fmt: skip
def test_model_rejects_inconsistent_topologies(mutate, message):
    data = yaml.safe_load((CONTRACT / "topology.yaml").read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(ValueError, match=message):
        Topology.model_validate(data)
