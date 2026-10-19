"""simd HTTP API on port 9000 (OpenAPI at ``/openapi.json``, exported to ``devices/simd/openapi.json``).

Read-only views of the device's simulated control plane, plus ``POST /resync``. The *reason* behind every PW
state is the contract section 7 rule that matched; see ``GET /state``.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from netsim_common.enums import PwReason, PwRole
from netsim_simd import __version__

Updown = Literal["up", "down"]
PwOper = Literal["up", "down", "testing", "unknown", "dormant", "notPresent", "lowerLayerDown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Remote(_Model):
    hostname: str = Field(description="Peer device that holds the matching PW")
    mtu: int = Field(description="Effective MTU of the peer's attachment circuit (cpwVcRemoteIfMtu)")
    ac_name: str = Field(description="Peer's attachment-circuit interface (cpwVcRemoteIfString)")
    service_id: str = Field(description="service-id of the matching PW on the peer (may differ from ours: drift)")
    role: PwRole = Field(description="role of the matching PW on the peer (may differ from ours: drift)")


class PseudowireState(_Model):
    index: int = Field(description="cpwVcIndex (stable per (service_id, role), persisted on the device volume)")
    service_id: str
    role: PwRole
    customer: str | None = Field(description="xconnect customer (cpwVcDescr)")
    ac: str = Field(description="Local attachment circuit")
    peer: str = Field(description="Configured peer address (peer-ipv4)")
    peer_hostname: str | None = Field(description="Device owning the peer address, if its advert is fresh")
    vc_id: int
    local_mtu: int = Field(description="Effective MTU of the local attachment circuit (cpwVcLocalIfMtu)")
    admin: Updown = Field(description="cpwVcAdminStatus: xconnect enabled and PW enabled")
    oper: PwOper = Field(description="cpwVcOperStatus label (MIB)")
    reason: PwReason = Field(description="First matching rule of the contract section 7 evaluation table")
    rule: int = Field(description="Row number of that rule in the contract table (1-10)")
    since: str = Field(description="When oper last changed (ISO 8601 UTC; simd boot for the initial state)")
    remote: Remote | None = Field(description="The peer's matching PW, when the peer is reachable and configured")


class InterfaceState(_Model):
    index: int = Field(description="ifIndex (stable per name, persisted)")
    name: str
    type: str
    description: str | None
    parent: str | None
    mtu: int = Field(description="Effective MTU (ifMtu)")
    admin: Updown = Field(description="ifAdminStatus (config enabled)")
    oper: str = Field(description="ifOperStatus label")
    fault: bool = Field(description="A physical fault key is set on this interface")


class Uplink(_Model):
    admin: Updown = Field(description="A core-uplink interface is enabled in config")
    oper: Updown = Field(description="uplink_oper_up = config enabled and not fault")
    fault: bool = Field(description="netsim:fault:uplink:<host> is set")
    interfaces: list[str]


class Faults(_Model):
    uplink: bool
    interfaces: list[str] = Field(description="Interfaces with netsim:fault:ac:<host>:<ifname> set")
    cuts: list[str] = Field(description="Peers with netsim:cut:<a>|<b> set towards this device")


class DeviceState(_Model):
    hostname: str
    loopback: str | None = Field(description="system/loopback-ipv4 (router ID, PW peer address)")
    mgmt_ip: str
    boot_id: str = Field(description="Changes on every simd start")
    booted_at: str
    uptime_s: float
    config_hash: str | None = Field(description="SHA-256 of the canonical running config")
    uplink: Uplink
    faults: Faults
    pseudowires: list[PseudowireState]
    interfaces: list[InterfaceState]


class Neighbor(_Model):
    hostname: str
    loopback: str
    mgmt_ip: str
    boot_id: str
    seq: int
    age_s: float = Field(description="Seconds since the advert was last refreshed (hold time 3 s)")
    uplink_oper_up: bool
    pw_count: int
    reachable: bool = Field(description="reachable(this device, neighbor) per contract section 7")


class Health(_Model):
    status: Literal["ok", "degraded", "starting"] = Field(
        description="ok: serving; degraded: serving but a dependency check fails; starting: not ready (HTTP 503)"
    )
    boot_id: str
    uptime_s: float
    checks: dict[str, bool] = Field(description="restconf, core, snmp_db, config, config_stream")


class EventsResponse(_Model):
    host: str
    boot_id: str
    now: float = Field(description="Server time (epoch seconds); pass it as `since` to poll for newer events")
    capacity: int = Field(description="Ring buffer size")
    events: list[dict[str, Any]] = Field(description="Oldest first; see netsim_simd.events for the types")


class ResyncResult(_Model):
    config_changed: bool
    state: DeviceState


class SimdApi(Protocol):
    def health(self) -> Health: ...
    def state(self) -> DeviceState: ...
    def neighbors(self) -> list[Neighbor]: ...
    def events_response(self, since: float | None, limit: int | None) -> EventsResponse: ...
    async def resync(self) -> ResyncResult: ...


DESCRIPTION = """
State API of one simulated PE router (netsim-pe). Values are what the device itself believes:

* `/state`: every pseudowire with its oper status and the **reason** (the contract section 7 rule that
  matched), the peer's matching PW, interfaces, uplink and active physical faults;
* `/neighbors`: fresh adverts on the core bus (simulated targeted-LDP hellos);
* `/events`: ring buffer (1000) of oper transitions, config changes (with structured diffs), faults,
  neighbor changes and traps sent;
* `/resync`: re-read the running config, re-publish the advert and recompute now.
"""


def create_app(simd: SimdApi) -> FastAPI:
    app = FastAPI(title="netsim-pe simd API", version=__version__, description=DESCRIPTION)

    @app.get("/health", response_model=Health, summary="Liveness and dependency checks")
    def health(response: Response) -> Health:
        result = simd.health()
        if result.status == "starting":
            response.status_code = 503
        return result

    @app.get("/state", response_model=DeviceState, summary="PW and interface state with reasons")
    def state() -> DeviceState:
        try:
            return simd.state()
        except LookupError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/neighbors", response_model=list[Neighbor], summary="Fresh adverts seen on the core bus")
    def neighbors() -> list[Neighbor]:
        return simd.neighbors()

    @app.get("/events", response_model=EventsResponse, summary="Events newer than `since`")
    def events(
        since: str | None = Query(default=None, description="Epoch seconds or ISO 8601; omit for the whole buffer"),
        limit: int | None = Query(default=None, ge=0, le=1000, description="Return at most this many (newest)"),
    ) -> EventsResponse:
        from netsim_simd.events import parse_since

        try:
            ts = parse_since(since)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return simd.events_response(ts, limit)

    @app.post("/resync", response_model=ResyncResult, summary="Re-read config, re-publish, recompute now")
    async def resync() -> ResyncResult:
        try:
            return await simd.resync()
        except LookupError as exc:
            raise HTTPException(503, str(exc)) from exc

    return app
