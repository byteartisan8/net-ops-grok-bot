"""MIB view rows: OIDs from contract/oids.yaml, snmprec encodings checked against snmpsim and pysnmp."""

from __future__ import annotations

import re

import pytest
from netsim_simd.mibview import (
    IF_COLUMNS,
    IFX_COLUMNS,
    PW_COLUMNS,
    SNMPREC_TAG_BY_BASE,
    SYSTEM_OBJECTS,
    ZERO_DOT_ZERO,
    IfRow,
    MibView,
    PwRow,
    SysInfo,
)
from netsim_simd.snmpdb import oid_tuple, sql_oid, unsql_oid
from pysnmp.proto import rfc1902
from snmpsim.grammar.snmprec import SnmprecGrammar

from netsim_common.enums import IfOperStatus, InterfaceType, PwOperStatus
from netsim_common.oids import PYSNMP_TYPE_BY_BASE
from netsim_common.paths import contract_dir


@pytest.mark.parametrize("base", sorted(SNMPREC_TAG_BY_BASE))
def test_snmprec_tags_match_snmpsim(base):
    cls = getattr(rfc1902, PYSNMP_TYPE_BY_BASE[base])
    tag = SnmprecGrammar().get_tag_by_type(cls())
    assert SNMPREC_TAG_BY_BASE[base].removesuffix("x") == tag


def test_zero_dot_zero_is_in_the_pinned_smi():
    text = (contract_dir() / "mibs" / "SNMPv2-SMI.txt").read_text(encoding="utf-8")
    assert re.search(r"zeroDotZero\s+OBJECT-IDENTITY.*?::=\s*\{\s*0\s+0\s*\}", text, re.S)
    assert ZERO_DOT_ZERO == "0.0"


def test_contract_section_8_objects_are_served(oids):
    wanted = {
        "sysDescr",
        "sysObjectID",
        "sysUpTime",
        "sysName",
        "ifName",
        "ifDescr",
        "ifAlias",
        "ifAdminStatus",
        "ifOperStatus",
        "ifMtu",
        "cpwVcType",
        "cpwVcPsnType",
        "cpwVcPeerAddrType",
        "cpwVcPeerAddr",
        "cpwVcID",
        "cpwVcLocalIfMtu",
        "cpwVcRemoteIfMtu",
        "cpwVcRemoteIfString",
        "cpwVcName",
        "cpwVcDescr",
        "cpwVcAdminStatus",
        "cpwVcOperStatus",
        "cpwVcInboundOperStatus",
        "cpwVcOutboundOperStatus",
        "cpwVcUpTime",
    }
    served = set(SYSTEM_OBJECTS) | set(IF_COLUMNS) | set(IFX_COLUMNS) | set(PW_COLUMNS)
    assert wanted <= served
    for name in served:
        oids.object(name)  # every served object is in oids.yaml


def sample(view: MibView):
    sys = SysInfo(
        hostname="spoke-1", descr="NetSim PE (Clixon 7.9.0) spoke-1", location="BR-01", contact="", boot_since=0.0
    )
    ifs = [
        IfRow(1, "GigabitEthernet0/0/0", InterfaceType.CORE_UPLINK, 9000, True, IfOperStatus.up, "CORE"),
        IfRow(
            3,
            "GigabitEthernet0/0/1.101",
            InterfaceType.SUBINTERFACE,
            1500,
            True,
            IfOperStatus.up,
            "Customer-A SVC-0001",
        ),
    ]
    pws = [
        PwRow(
            1,
            "SVC-0001:primary",
            "Customer-A",
            True,
            "10.255.0.1",
            1001,
            1500,
            1500,
            "GigabitEthernet0/0/2.101",
            True,
            PwOperStatus.up,
            10.0,
        ),
        PwRow(2, "SVC-0001:backup", "Customer-A", True, "10.255.0.2", 2001, 1500, 0, "", True, PwOperStatus.down, None),
    ]
    return view.rows(sys, ifs, pws)


def test_rows_are_sorted_and_complete(oids):
    view = MibView(oids)
    rows = sample(view)
    assert [r.oid for r in rows] == sorted(r.oid for r in rows)
    assert len(rows) == len(SYSTEM_OBJECTS) + 1 + 2 * (len(IF_COLUMNS) + len(IFX_COLUMNS)) + 2 * len(PW_COLUMNS)
    anchor = oid_tuple(view.anchor())
    assert view.anchor() == "1.3.6.1"
    assert all(r.oid[: len(anchor)] == anchor for r in rows)


def test_row_encodings(oids):
    rows = {r.oid: r for r in sample(MibView(oids))}

    def cell(name, *index):
        return rows[oid_tuple(oids.instance(name, *index))]

    assert cell("cpwVcPeerAddr", 1).value == "0aff0001"  # 4 octets, hex in snmprec "4x"
    assert bytes.fromhex(cell("cpwVcName", 2).value) == b"SVC-0001:backup"
    assert bytes.fromhex(cell("cpwVcDescr", 1).value) == b"Customer-A"
    assert cell("cpwVcType", 1).value == str(oids.value("cpwVcType", "ethernetVLAN"))
    assert cell("cpwVcPsnType", 1).value == str(oids.value("cpwVcPsnType", "mpls"))
    assert cell("cpwVcPeerAddrType", 1).value == str(oids.value("cpwVcPeerAddrType", "ipv4"))
    assert cell("cpwVcOperStatus", 2).value == cell("cpwVcInboundOperStatus", 2).value == str(PwOperStatus.down.value)
    assert (cell("cpwVcUpTime", 1).tag, cell("cpwVcUpTime", 1).since) == ("67", 10.0)
    assert (cell("cpwVcUpTime", 2).value, cell("cpwVcUpTime", 2).since) == ("0", None)
    assert cell("cpwVcRemoteIfMtu", 2).value == "0" and cell("cpwVcRemoteIfString", 2).value == ""
    assert cell("sysObjectID").value == "0.0"
    assert bytes.fromhex(cell("sysName").value) == b"spoke-1"
    assert cell("ifType", 1).value == str(oids.value("ifType", "ethernetCsmacd"))
    assert cell("ifType", 3).value == str(oids.value("ifType", "l2vlan"))
    assert cell("ifNumber").value == "2"


def test_sql_oid_sorts_like_snmp():
    oids_ = ["1.3.6.1.2.1.2.2.1.10.1", "1.3.6.1.2.1.2.2.1.2.1", "1.3.6.1.2.1.2.2.1.2", "1.3.6.1.2.1.2.2.1.2.10"]
    assert sorted(oids_, key=sql_oid) == sorted(oids_, key=oid_tuple)
    assert all(unsql_oid(sql_oid(o)) == o for o in oids_)
