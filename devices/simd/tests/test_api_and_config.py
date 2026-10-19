"""API (FastAPI app over an in-process device), settings parsing and the config watcher."""

from __future__ import annotations

import asyncio
import json
import warnings

import pytest
from netsim_simd.api import create_app
from netsim_simd.clixon import ConfigCommit, parse_config_change
from netsim_simd.configwatch import ConfigSnapshot, ConfigWatcher
from netsim_simd.devtool import openapi_document
from netsim_simd.events import EventLog, parse_since
from netsim_simd.settings import CONTRACT_TRAP_PORT, Settings, SettingsError, TrapTarget, parse_trap_targets

from netsim_common.paths import repo_root

NOTIFICATION = """<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">
<eventTime>2026-10-19T10:00:00.123456Z</eventTime>
<netconf-config-change xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-notifications">
<changed-by><username>restconf</username><session-id>0</session-id></changed-by>
<datastore>running</datastore>
<edit><target xmlns:nsd="urn:netsim:device">
/nsd:l2vpn/nsd:xconnect[nsd:service-id='SVC-0001']/nsd:pseudowire[nsd:role='primary']/nsd:vc-id</target>
<operation>replace</operation></edit>
</netconf-config-change></notification>"""


def test_api_endpoints(make_device, settle):
    async def build():
        hub, spoke = await make_device("hub-a"), await make_device("spoke-1")
        await settle(hub, spoke)
        return spoke

    spoke = asyncio.run(build())
    with warnings.catch_warnings():  # starlette 1.x prefers httpx2 for its TestClient; httpx still works
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    client = TestClient(create_app(spoke))
    assert client.get("/health").status_code == 503  # not ready until run() finished booting
    spoke.ready = True
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["checks"]["core"] is True
    state = client.get("/state").json()
    assert state["hostname"] == "spoke-1" and state["loopback"] == "10.255.1.1"
    primary = state["pseudowires"][0]
    assert {k: primary[k] for k in ("index", "service_id", "role", "peer", "vc_id", "admin", "oper", "reason")} == {
        "index": 1,
        "service_id": "SVC-0001",
        "role": "primary",
        "peer": "10.255.0.1",
        "vc_id": 1001,
        "admin": "up",
        "oper": "up",
        "reason": "ok",
    }
    assert primary["remote"]["hostname"] == "hub-a" and primary["since"].endswith("Z")
    assert state["pseudowires"][1]["remote"] is None
    assert state["uplink"] == {"admin": "up", "oper": "up", "fault": False, "interfaces": ["GigabitEthernet0/0/0"]}
    neighbors = client.get("/neighbors").json()
    assert [(n["hostname"], n["reachable"]) for n in neighbors] == [("hub-a", True)]
    events = client.get("/events").json()
    assert events["capacity"] >= 500
    assert {"neighbor-up", "pw-added"} <= {e["type"] for e in events["events"]}
    later = client.get("/events", params={"since": events["now"]}).json()
    assert later["events"] == []
    assert client.get("/events", params={"since": "not-a-time"}).status_code == 422


def test_openapi_export_is_current():
    path = repo_root() / "devices" / "simd" / "openapi.json"
    assert json.loads(path.read_text(encoding="utf-8")) == openapi_document(), "run `make devices-openapi`"


def test_event_log_ring_and_since():
    log = EventLog("h", "b", maxlen=3)
    for i in range(5):
        log.add("x", n=i)
    assert [e["n"] for e in log.since()] == [2, 3, 4]
    assert [e["n"] for e in log.since(limit=1)] == [4]
    ts = log.since()[0]["ts"]
    assert [e["n"] for e in log.since(ts)] == [3, 4] or [e["n"] for e in log.since(ts)] == [4]
    assert parse_since("2026-10-19T10:00:00Z") == parse_since("2026-10-19T10:00:00+00:00")
    assert parse_since("12.5") == 12.5 and parse_since(None) is None


def test_parse_config_change():
    commit = parse_config_change(NOTIFICATION)
    assert isinstance(commit, ConfigCommit)
    assert (commit.username, commit.server, commit.datastore) == ("restconf", False, "running")
    assert commit.edits[0][1] == "replace" and commit.edits[0][0].endswith("nsd:vc-id")
    server = parse_config_change(
        NOTIFICATION.replace("<username>restconf</username><session-id>0</session-id>", "<server/>")
    )
    assert server is not None and server.server and server.username is None
    assert parse_config_change("<notification xmlns='urn:ietf:params:xml:ns:netconf:notification:1.0'/>") is None
    assert parse_config_change("not xml") is None


class _Restconf:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    async def get_running(self):
        return self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]


def test_config_watcher_reports_only_real_changes(golden):
    one = golden("spoke-1").to_restconf()
    two = json.loads(json.dumps(one))
    two["netsim-device:l2vpn"]["xconnect"][0]["pseudowire"][0]["vc-id"] = 1010
    reordered = {k: one[k] for k in reversed(list(one))}  # same config, different member order
    updates = []

    async def on_update(update):
        updates.append(update)

    async def scenario():
        watcher = ConfigWatcher(
            _Restconf([one, reordered, two, {"netsim-device:l2vpn": {"xconnect": "bad"}}]), on_update
        )
        commit = parse_config_change(NOTIFICATION)
        assert await watcher.refresh("initial") is True
        assert await watcher.refresh("poll") is False
        assert await watcher.refresh("notification", commit) is True
        assert await watcher.refresh("poll") is False  # invalid payload is reported and ignored
        assert watcher.invalid_configs == 1

    asyncio.run(scenario())
    assert [u.source for u in updates] == ["initial", "notification"]
    assert updates[1].old.digest == ConfigSnapshot.from_payload(one).digest
    assert updates[1].commit is not None and updates[1].commit.username == "restconf"


def test_settings_from_env(tmp_path):
    env = {
        "HOSTNAME": "spoke-1",
        "MGMT_IP": "172.30.10.21",
        "TRAP_TARGETS": "172.30.10.42:1162, 10.0.0.9",
        "NETSIM_STATE_DIR": str(tmp_path),
    }
    s = Settings.from_env(env)
    assert s.trap_targets == (TrapTarget("172.30.10.42", 1162), TrapTarget("10.0.0.9", 162))
    assert s.core_url == "redis://172.30.10.5:6379/0" and s.snmp_community == "netsim"
    assert s.state_dir == tmp_path
    default = Settings.from_env({"HOSTNAME": "a", "MGMT_IP": "172.30.10.21"})
    assert default.trap_targets == (TrapTarget("172.30.10.42", CONTRACT_TRAP_PORT),)
    assert Settings.from_env({**env, "TRAP_TARGETS": ""}).trap_targets == ()
    with pytest.raises(SettingsError):
        Settings.from_env({"MGMT_IP": "172.30.10.21"})
    with pytest.raises(SettingsError):
        Settings.from_env({"HOSTNAME": "a", "MGMT_IP": "nope"})
    with pytest.raises(SettingsError):
        parse_trap_targets("host:notaport")
