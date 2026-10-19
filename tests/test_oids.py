"""contract/oids.yaml: generated from the pinned MIBs, complete for contract section 8, consistent with pysnmp."""

from __future__ import annotations

import pytest
from netsim_testlib import CONTRACT, load_tool

from netsim_common.enums import AdminStatus, IfOperStatus, PwOperStatus
from netsim_common.oids import PYSNMP_TYPE_BY_BASE, load_oids

# Contract section 8, verbatim.
CONTRACT_OBJECTS = {
    "SNMPv2-MIB": ["sysDescr", "sysObjectID", "sysUpTime", "sysName"],
    "IF-MIB": ["ifName", "ifDescr", "ifAlias", "ifAdminStatus", "ifOperStatus", "ifMtu"],
    "CISCO-IETF-PW-MIB": [
        "cpwVcType", "cpwVcPsnType", "cpwVcPeerAddrType", "cpwVcPeerAddr", "cpwVcID", "cpwVcLocalIfMtu",
        "cpwVcRemoteIfMtu", "cpwVcRemoteIfString", "cpwVcName", "cpwVcDescr", "cpwVcAdminStatus", "cpwVcOperStatus",
        "cpwVcInboundOperStatus", "cpwVcOutboundOperStatus", "cpwVcUpTime",
    ],
}  # fmt: skip
CONTRACT_NOTIFICATIONS = {
    "SNMPv2-MIB": ["coldStart"],
    "CISCO-IETF-PW-MIB": ["cpwVcDown", "cpwVcUp"],
    "CISCO-CONFIG-MAN-MIB": ["ciscoConfigManEvent"],
}


@pytest.fixture(scope="module")
def oids():
    return load_oids()


def test_oids_yaml_is_resolved_from_pinned_mibs():
    gen = load_tool("gen_oids")
    fresh = gen.render(gen.build())
    assert (CONTRACT / "oids.yaml").read_text(encoding="utf-8") == fresh, "run `make contract`"


def test_oids_yaml_records_the_mib_checksums(oids):
    fetch = load_tool("fetch_mibs")
    rows = fetch.parse_sources_md((CONTRACT / "mibs" / "SOURCES.md").read_text(encoding="utf-8"))
    assert {m.file: m.sha256 for m in oids.mibs.values()} == {f: sha for f, (_, sha) in rows.items()}


@pytest.mark.parametrize(("module", "name"), [(m, n) for m, names in CONTRACT_OBJECTS.items() for n in names])
def test_contract_objects_are_resolved(oids, module, name):
    obj = oids.object(name)
    assert obj.module == module
    assert obj.oid.startswith("1.3.6.1.") and all(part.isdigit() for part in obj.oid.split("."))
    assert obj.syntax is not None and obj.syntax.base in PYSNMP_TYPE_BY_BASE
    if obj.kind == "column":
        row = oids.object(obj.row)
        assert row.index, f"{obj.row} has no index"
        assert oids.object(obj.table).kind == "table"
        assert all(oids.object(i).kind == "column" for i in row.index)
    else:
        assert obj.kind == "scalar" and obj.instance == f"{obj.oid}.0"


@pytest.mark.parametrize(("module", "name"), [(m, n) for m, names in CONTRACT_NOTIFICATIONS.items() for n in names])
def test_contract_notifications_are_resolved(oids, module, name):
    notif = oids.notification(name)
    assert notif.module == module
    assert notif.description
    for vb in notif.objects:
        obj = oids.object(vb.name)
        assert obj.oid == vb.oid
        assert vb.index == (oids.object(obj.row).index if obj.kind == "column" else [])


def test_notification_varbinds_follow_objects_clauses(oids):
    assert [v.name for v in oids.notification("cpwVcDown").objects] == ["cpwVcOperStatus", "cpwVcOperStatus"]
    assert [v.name for v in oids.notification("cpwVcUp").objects] == ["cpwVcOperStatus", "cpwVcOperStatus"]
    assert oids.notification("coldStart").objects == []
    # The 2019-03-11 revision of CISCO-CONFIG-MAN-MIB adds ccmHistoryEventTerminalUser.
    assert [v.name for v in oids.notification("ciscoConfigManEvent").objects] == [
        "ccmHistoryEventCommandSource",
        "ccmHistoryEventConfigSource",
        "ccmHistoryEventConfigDestination",
        "ccmHistoryEventTerminalUser",
    ]
    assert oids.notification("cpwVcDown").objects[0].instance(7) == f"{oids.oid('cpwVcOperStatus')}.7"


def test_encodings_needed_by_contract(oids):
    assert oids.enum("cpwVcPeerAddrType")["ipv4"] == 1
    peer = oids.object("cpwVcPeerAddr").syntax
    assert (peer.type, peer.base) == ("InetAddress", "OCTET STRING")
    for name in ("cpwVcName", "cpwVcDescr", "cpwVcRemoteIfString"):
        assert oids.object(name).syntax.type == "SnmpAdminString"
    assert oids.object("cpwVcRemoteIfString").syntax.size == "0..80"
    assert oids.object("ifAlias").syntax.size == "0..64"
    assert oids.object("cpwVcUpTime").syntax.base == "TimeTicks"
    assert oids.object("ifXEntry").augments == "ifEntry"
    assert oids.object("ifXEntry").index == ["ifIndex"]


def test_library_enums_match_the_mibs(oids):
    pw = {m.name: m.value for m in PwOperStatus}
    for name in ("cpwVcOperStatus", "cpwVcInboundOperStatus", "cpwVcOutboundOperStatus"):
        assert oids.enum(name) == pw
    assert oids.enum("ifOperStatus") == {m.name: m.value for m in IfOperStatus}
    admin = {m.name: m.value for m in AdminStatus}
    assert oids.enum("ifAdminStatus") == admin
    assert oids.enum("cpwVcAdminStatus") == admin


def test_registry_helpers(oids):
    assert oids.instance("sysName") == "1.3.6.1.2.1.1.5.0"
    assert oids.instance("cpwVcName", 3) == f"{oids.oid('cpwVcName')}.3"
    with pytest.raises(ValueError, match="index"):
        oids.instance("cpwVcName")
    assert oids.value("cpwVcOperStatus", "lowerLayerDown") == 7
    assert oids.label("cpwVcOperStatus", 1) == "up"
    assert oids.name_of(oids.instance("cpwVcOperStatus", 12)) == "cpwVcOperStatus"
    assert oids.name_of(oids.oid("cpwVcDown")) == "cpwVcDown"
    cols = [c.name for c in oids.columns("cpwVcTable")]
    assert cols[0] == "cpwVcIndex" and len(cols) == 16


def test_pysnmp_type_mapping_exists():
    from pysnmp.proto import rfc1902

    for cls in PYSNMP_TYPE_BY_BASE.values():
        assert hasattr(rfc1902, cls), cls


# pysmi 2.0.0 generates pysnmp modules that still call the camelCase builder API.
@pytest.mark.filterwarnings("ignore:.*is deprecated. Please use:DeprecationWarning")
def test_oids_match_pysnmp_mib_loader(tmp_path, oids):
    """Independent check: compile the pinned MIBs for pysnmp and compare OIDs, enums and OBJECTS."""
    from pysmi.codegen import PySnmpCodeGen
    from pysmi.compiler import MibCompiler
    from pysmi.parser import SmiV1CompatParser
    from pysmi.reader import FileReader
    from pysmi.searcher import PyFileSearcher, PyPackageSearcher, StubSearcher
    from pysmi.writer import PyFileWriter
    from pysnmp.smi import builder

    compiler = MibCompiler(SmiV1CompatParser(tempdir=""), PySnmpCodeGen(), PyFileWriter(str(tmp_path)))
    compiler.add_sources(FileReader(str(CONTRACT / "mibs"), recursive=False))
    compiler.add_searchers(
        PyFileSearcher(str(tmp_path)), PyPackageSearcher("pysnmp.smi.mibs"), StubSearcher(*PySnmpCodeGen.baseMibs)
    )
    modules = sorted({o.module for o in oids.objects.values()} | {n.module for n in oids.notifications.values()})
    status = compiler.compile(*modules)
    assert all(str(s) in ("compiled", "untouched") for s in status.values()), status

    mib_builder = builder.MibBuilder()
    mib_builder.add_mib_sources(builder.DirMibSource(str(tmp_path)))
    mib_builder.load_modules(*modules)
    for name, obj in oids.objects.items():
        (sym,) = mib_builder.import_symbols(obj.module, name)
        assert ".".join(map(str, sym.getName())) == obj.oid, name
        if obj.syntax and obj.syntax.enum:
            assert dict(sym.getSyntax().namedValues) == obj.syntax.enum, name
    for name, notif in oids.notifications.items():
        (sym,) = mib_builder.import_symbols(notif.module, name)
        assert ".".join(map(str, sym.getName())) == notif.oid, name
        assert [o for _, o in sym.getObjects()] == [v.name for v in notif.objects], name
