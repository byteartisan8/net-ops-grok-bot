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
    "spoke-1": ("spoke", "BR-01", "172.30.10.21", "10.255.1.1"),
    "spoke-2": ("spoke", "BR-02", "172.30.10.22", "10.255.1.2"),
    "spoke-3": ("spoke", "BR-03", "172.30.10.23", "10.255.1.3"),
    "spoke-4": ("spoke", "BR-04", "172.30.10.24", "10.255.1.4"),
}
INFRA = {
    "core": "172.30.10.5",
    "netbox": "172.30.10.30",
    "zabbix-server": "172.30.10.40",
    "zabbix-web": "172.30.10.41",
    "zabbix-snmptraps": "172.30.10.42",
    "lab-api": "172.30.10.50",
}
# Contract section 5 table: service -> (customer, spoke, vlan, spoke AC, hub AC, primary VC, backup VC)
SERVICES = {
    "SVC-0001": ("Customer-A", "spoke-1", 101, "GigabitEthernet0/0/1.101", "GigabitEthernet0/0/2.101", 1001, 2001),
    "SVC-0002": ("Customer-A", "spoke-2", 102, "GigabitEthernet0/0/1.102", "GigabitEthernet0/0/2.102", 1002, 2002),
    "SVC-0003": ("Customer-B", "spoke-2", 201, "GigabitEthernet0/0/1.201", "GigabitEthernet0/0/2.201", 1003, 2003),
    "SVC-0004": ("Customer-C", "spoke-3", 301, "GigabitEthernet0/0/1.301", "GigabitEthernet0/0/2.301", 1004, 2004),
    "SVC-0005": ("Customer-D", "spoke-4", 401, "GigabitEthernet0/0/1.401", "GigabitEthernet0/0/2.401", 1005, 2005),
    "SVC-0006": ("Customer-B", "spoke-4", 202, "GigabitEthernet0/0/1.202", "GigabitEthernet0/0/2.202", 1006, 2006),
}
# Contract section 10 default tiers; section 9.4 SLOs.
TIERS = {"Customer-A": "gold", "Customer-B": "gold", "Customer-C": "silver", "Customer-D": "silver"}
SLO = {"gold": 99.95, "silver": 99.9}


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
    data["services"][0]["pseudowires"].pop("backup")
    with pytest.raises(jsonschema.ValidationError, match="backup"):
        jsonschema.validate(data, schema)


def test_network(topo):
    assert (topo.network.name, topo.network.subnet, topo.network.gateway) == ("labnet", "172.30.10.0/24", "172.30.10.1")
    assert topo.host_bind == "127.0.0.1"
    assert topo.region == "Lab"


def test_devices_match_contract_section_4(topo):
    assert {d.hostname: (d.role.value, d.site, d.mgmt_ip, d.loopback) for d in topo.devices} == DEVICES
    assert {h.name: h.mgmt_ip for h in topo.infrastructure} == INFRA


def test_physical_interfaces(topo):
    for d in topo.devices:
        assert (d.core_uplink.name, d.core_uplink.mtu) == ("GigabitEthernet0/0/0", 9000)
        access = "GigabitEthernet0/0/2" if d.role is DeviceRole.HUB else "GigabitEthernet0/0/1"
        assert (d.access_interface.name, d.access_interface.mtu) == (access, 1500)


def test_services_match_contract_section_5(topo):
    got = {
        s.id: (s.customer, s.spoke, s.vlan, s.spoke_ac, s.hub_ac, s.pw("primary").vc_id, s.pw("backup").vc_id)
        for s in topo.services
    }
    assert got == SERVICES
    for s in topo.services:
        n = int(s.id.removeprefix("SVC-"))
        assert (s.pw(PwRole.PRIMARY).hub, s.pw(PwRole.PRIMARY).vc_id) == ("hub-a", 1000 + n)
        assert (s.pw(PwRole.BACKUP).hub, s.pw(PwRole.BACKUP).vc_id) == ("hub-b", 2000 + n)


def test_customers_and_sla_tiers(topo):
    assert {c.name: c.sla_tier.value for c in topo.customers} == TIERS
    assert {t.value: spec.slo for t, spec in topo.sla_tiers.items()} == SLO


def test_pseudowire_totals(topo):
    per_host = {h: len(topo.pseudowires_on(h)) for h in topo.hostnames}
    assert sum(per_host.values()) == 24  # 12 PWs, two ends each
    assert per_host["hub-a"] == per_host["hub-b"] == 6
    assert {per_host[s.hostname] for s in topo.spokes} == {2, 4}
    assert all(pw.role is PwRole.PRIMARY for pw in topo.pseudowires_on("hub-a"))
    assert all(pw.role is PwRole.BACKUP for pw in topo.pseudowires_on("hub-b"))


def test_host_ports(topo):
    assert [topo.host_port("hub-a", k) for k in ("restconf", "simd", "snmp")] == [18011, 19011, 16111]
    assert [topo.host_port("spoke-4", k) for k in ("restconf", "simd", "snmp")] == [18024, 19024, 16124]
    assert {h.name: h.host_port for h in topo.infrastructure if h.host_port} == {
        "netbox": 8000,
        "zabbix-web": 8080,
        "lab-api": 8090,
    }


def test_generator_reproduces_the_contract_topology(topo):
    gen = load_tool("gen_topology")
    assert gen.extend(topo, spokes=4, services=6, customers=4) == topo


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
    svc = loaded.service("SVC-0060")
    assert (svc.pw("primary").vc_id, svc.pw("backup").vc_id) == (1060, 2060)
    assert svc.spoke_ac == f"GigabitEthernet0/0/1.{svc.vlan}"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"spokes": 3, "services": 6, "customers": 4}, "extends the contract topology"),
        ({"spokes": 4, "services": 1000, "customers": 4}, "<= 999"),
        ({"spokes": 4, "services": 500, "customers": 4}, "more than 99 services"),
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
        (lambda d: d["customers"][0].update(sla_tier="bronze"), "sla_tier"),
    ],
)  # fmt: skip
def test_model_rejects_inconsistent_topologies(mutate, message):
    data = yaml.safe_load((CONTRACT / "topology.yaml").read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(ValueError, match=message):
        Topology.model_validate(data)
