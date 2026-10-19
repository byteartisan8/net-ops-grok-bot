"""The real snmpsim responder + our variation module + SQLite: dynamic rows, GET/GETNEXT/GETBULK/walk ordering.

Starts ``snmpsim-command-responder`` on 127.0.0.1 (random port) exactly as the image does (same data file,
variation module and options), writes MIB views with :class:`SnmpDbWriter`, and checks what an NMS sees.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from netsim_simd import snmpclient
from netsim_simd.mibview import IfRow, MibView, PwRow, SysInfo
from netsim_simd.snmpdb import SnmpDbWriter, boottime, oid_tuple, write_datafile

from netsim_common.enums import IfOperStatus, InterfaceType, PwOperStatus
from netsim_common.oids import OidRegistry
from netsim_common.paths import repo_root

VARIATION_DIR = repo_root() / "devices" / "snmpsim" / "variation"
COMMUNITY = "netsim"


@dataclass
class Agent:
    port: int
    writer: SnmpDbWriter
    view: MibView
    oids: OidRegistry
    boot: float

    def publish(self, pws: list[PwRow], ifs: list[IfRow] | None = None) -> None:
        sys_info = SysInfo("spoke-1", "NetSim PE (Clixon test) spoke-1", "BR-01", "", self.boot)
        ifs = (
            ifs
            if ifs is not None
            else [IfRow(1, "Gi0/0/0", InterfaceType.CORE_UPLINK, 9000, True, IfOperStatus.up, "CORE")]
        )
        self.writer.write(self.view.rows(sys_info, ifs, pws))

    def walk(self, root: str, *, bulk: bool = True, max_repetitions: int = 25) -> list[snmpclient.VarBind]:
        return asyncio.run(
            snmpclient.walk("127.0.0.1", self.port, COMMUNITY, root, bulk=bulk, max_repetitions=max_repetitions)
        )

    def get(self, *oids: str) -> list[snmpclient.VarBind]:
        return asyncio.run(snmpclient.get("127.0.0.1", self.port, COMMUNITY, *oids))

    def get_next(self, *oids: str) -> list[snmpclient.VarBind]:
        return asyncio.run(snmpclient.get_next("127.0.0.1", self.port, COMMUNITY, *oids))

    def get_bulk(self, *oids: str, non_repeaters: int = 0, max_repetitions: int = 10) -> list[snmpclient.VarBind]:
        return asyncio.run(
            snmpclient.get_bulk(
                "127.0.0.1", self.port, COMMUNITY, *oids, non_repeaters=non_repeaters, max_repetitions=max_repetitions
            )
        )

    def pw_indexes(self) -> list[int]:
        col = self.oids.oid("cpwVcName")
        return [int(vb.oid.rsplit(".", 1)[1]) for vb in self.walk(col)]


def pw(index: int, oper: PwOperStatus = PwOperStatus.up, *, name: str | None = None) -> PwRow:
    return PwRow(
        index=index,
        name=name or f"SVC-000{index}:primary",
        descr="Customer-A",
        subinterface_ac=True,
        peer="10.255.0.1",
        vc_id=1000 + index,
        local_mtu=1500,
        remote_mtu=1500 if oper is PwOperStatus.up else 0,
        remote_if="GigabitEthernet0/0/2.10" + str(index) if oper is PwOperStatus.up else "",
        admin_up=True,
        oper=oper,
        up_since=boottime() - 5 if oper is PwOperStatus.up else None,
    )


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def agent(tmp_path: Path, oids: OidRegistry) -> Iterator[Agent]:
    view = MibView(oids)
    write_datafile(tmp_path / "data", COMMUNITY, view.anchor())
    db = tmp_path / "agent.db"
    writer = SnmpDbWriter(db)
    port = _free_udp_port()
    responder = Path(sys.executable).parent / "snmpsim-command-responder"
    log = (tmp_path / "snmpsim.log").open("w")
    proc = subprocess.Popen(
        [
            str(responder),
            f"--data-dir={tmp_path / 'data'}",
            f"--cache-dir={tmp_path / 'cache'}",
            f"--variation-modules-dir={VARIATION_DIR}",
            f"--variation-module-options=netsim:db:{db}",
            f"--agent-udpv4-endpoint=127.0.0.1:{port}",
            "--log-level=error",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    a = Agent(port, writer, view, oids, boottime())
    a.publish([pw(1), pw(2, PwOperStatus.down)])
    deadline = time.monotonic() + 20
    try:
        while True:
            try:
                if not a.get(oids.instance("sysName"))[0].end:
                    break
            except snmpclient.SnmpError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("snmpsim did not start:\n" + (tmp_path / "snmpsim.log").read_text())
            time.sleep(0.2)
        yield a
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        writer.close()


def test_get_scalar_and_cell(agent: Agent, oids):
    sys_name, oper = agent.get(oids.instance("sysName"), oids.instance("cpwVcOperStatus", 2))
    assert sys_name.rendered == "spoke-1"
    assert oper.rendered == oids.value("cpwVcOperStatus", "down")


def test_get_missing_instance(agent: Agent, oids):
    [vb] = agent.get(oids.instance("cpwVcOperStatus", 99))
    assert vb.end and vb.rendered == "NoSuchInstance"


def test_walk_bulk_equals_walk_next(agent: Agent, oids):
    table = oids.oid("cpwVcTable")
    bulk = agent.walk(table, bulk=True, max_repetitions=7)
    nxt = agent.walk(table, bulk=False)
    live = oids.oid("cpwVcUpTime")  # TimeTicks keep counting between the two walks

    def static(vbs):
        return [(v.oid, None if v.oid.startswith(live + ".") else v.rendered) for v in vbs]

    assert static(bulk) == static(nxt)
    assert len(bulk) == 2 * 15  # 2 rows x 15 columns (contract section 8)


def test_full_walk_is_lexicographic_and_covers_every_row(agent: Agent, oids):
    everything = agent.walk("1.3")  # starts before the anchor
    assert everything[0].oid == oids.instance("sysDescr")
    assert len(everything) == 7 + 1 + 8 + 2 * 15
    assert everything[-1].oid == oids.instance("cpwVcOutboundOperStatus", 2)
    # column-major: all cpwVcType cells come before any cpwVcPsnType cell
    names = [oids.name_of(v.oid) for v in everything]
    assert names.index("cpwVcPsnType") == names.index("cpwVcType") + 2


def test_end_of_mib(agent: Agent, oids):
    [vb] = agent.get_next(oids.instance("cpwVcOutboundOperStatus", 2))
    assert vb.end and vb.rendered == "EndOfMibView"
    out = agent.get_bulk(oids.instance("cpwVcOperStatus", 1), max_repetitions=10)
    assert out[-1].end
    assert [oids.name_of(v.oid) for v in out if not v.end][:3] == [
        "cpwVcOperStatus",
        "cpwVcInboundOperStatus",
        "cpwVcInboundOperStatus",
    ]


def test_add_delete_change_rows_then_walk(agent: Agent, oids):
    assert agent.pw_indexes() == [1, 2]
    # add a row
    agent.publish([pw(1), pw(2, PwOperStatus.down), pw(3)])
    assert agent.pw_indexes() == [1, 2, 3]
    # delete a row: the walk skips it and GETNEXT from inside it lands on the next one
    agent.publish([pw(1), pw(3)])
    assert agent.pw_indexes() == [1, 3]
    [vb] = agent.get_next(oids.instance("cpwVcName", 2))
    assert vb.oid == oids.instance("cpwVcName", 3)
    [missing] = agent.get(oids.instance("cpwVcName", 2))
    assert missing.end
    # change a value
    assert agent.get(oids.instance("cpwVcOperStatus", 1))[0].rendered == 1
    agent.publish([pw(1, PwOperStatus.lowerLayerDown), pw(3)])
    assert agent.get(oids.instance("cpwVcOperStatus", 1))[0].rendered == oids.value("cpwVcOperStatus", "lowerLayerDown")
    # bulk walks agree with GETNEXT walks after every change
    table = oids.oid("cpwVcTable")
    assert [v.oid for v in agent.walk(table, max_repetitions=4)] == [v.oid for v in agent.walk(table, bulk=False)]
    # an empty table: the walk of cpwVcTable is empty and the MIB still ends cleanly
    agent.publish([])
    assert agent.walk(table) == []
    assert agent.get_next(oids.instance("ifAlias", 1))[0].end


def test_bulk_with_non_repeaters(agent: Agent, oids):
    out = agent.get_bulk(oids.oid("sysUpTime"), oids.oid("cpwVcName"), non_repeaters=1, max_repetitions=2)
    assert out[0].oid == oids.instance("sysUpTime")
    assert [v.oid for v in out[1:]] == [oids.instance("cpwVcName", 1), oids.instance("cpwVcName", 2)]


def test_uptimes_are_live(agent: Agent, oids):
    first = agent.get(oids.instance("sysUpTime"), oids.instance("cpwVcUpTime", 1), oids.instance("cpwVcUpTime", 2))
    time.sleep(0.35)
    second = agent.get(oids.instance("sysUpTime"), oids.instance("cpwVcUpTime", 1), oids.instance("cpwVcUpTime", 2))
    assert second[0].rendered > first[0].rendered
    assert second[1].rendered > first[1].rendered >= 500  # up for ~5 s
    assert first[2].rendered == second[2].rendered == 0  # not up


def test_peer_address_is_four_octets(agent: Agent, oids):
    [vb] = agent.get(oids.instance("cpwVcPeerAddr", 1))
    assert bytes(vb.value) == bytes([10, 255, 0, 1])


def test_wrong_community_gets_no_answer(agent: Agent, oids):
    with pytest.raises(snmpclient.SnmpError, match="no response"):
        asyncio.run(snmpclient.get("127.0.0.1", agent.port, "public", oids.instance("sysName"), timeout=0.3, retries=0))


def test_oid_order_helper_detects_disorder():
    assert oid_tuple("1.3.6.1.2") < oid_tuple("1.3.6.1.10")
