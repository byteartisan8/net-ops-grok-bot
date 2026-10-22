"""simd main program: ties config watch, bus, evaluation, SNMP data, traps, events and the API together.

Recompute (coalesced: triggers that arrive while one runs cause exactly one more run):

1. read the bus (fresh adverts, our faults and cuts);
2. compute interface/uplink state from config + faults and our advert; write the advert when it changed or
   the hello is due (1 s), and publish on ``netsim:events`` when its content changed;
3. evaluate every PW (contract section 7) against the peers' adverts;
4. diff against the previous state: events, logs, trap transitions, SNMP rows (SQLite, atomic).

Triggers: local config change, any bus message (events channel, fault/cut keyspace changes, advert
expiry), the 1 s hello tick, and ``POST /resync``.

Boot: wait for Clixon, load the golden config on first boot (empty datastore, no marker on the volume),
first recompute (baseline: no PW traps), then ``coldStart``. ``SIGTERM`` stops simd without withdrawing the
advert: peers see the device disappear after the hold time, like a power-off.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import uvicorn
from redis.exceptions import RedisError

from netsim_common.canonical import diff_configs
from netsim_common.device_config import DeviceConfig
from netsim_common.enums import InterfaceType, PwOperStatus, PwRole
from netsim_common.oids import load_oids
from netsim_common.signaling import Advert
from netsim_simd import __version__
from netsim_simd.api import (
    DeviceState,
    EventsResponse,
    Faults,
    Health,
    InterfaceState,
    Neighbor,
    PseudowireState,
    Remote,
    ResyncResult,
    Uplink,
    create_app,
)
from netsim_simd.bus import Bus, BusSnapshot, state_message
from netsim_simd.clixon import Restconf
from netsim_simd.configwatch import ConfigSnapshot, ConfigUpdate, ConfigWatcher
from netsim_simd.evaluate import (
    IfState,
    LocalFaults,
    PeerView,
    PwEval,
    UplinkState,
    advert_pws,
    evaluate_pseudowires,
    interface_states,
    reachable,
    uplink_state,
)
from netsim_simd.events import EventLog
from netsim_simd.indexes import IndexStore, atomic_write_text
from netsim_simd.jsonlog import iso_ts, log_event, setup_logging
from netsim_simd.mibview import IfRow, MibView, PwRow, SysInfo
from netsim_simd.settings import Settings, SettingsError
from netsim_simd.snmpdb import SnmpDbWriter, boottime, timeticks_since
from netsim_simd.traps import (
    LinkTransition,
    Transition,
    TransitionBatcher,
    Trap,
    TrapBuilder,
    TrapSender,
    link_transition,
)

log = logging.getLogger("simd")

MARKER_FILE = "provisioned.json"
INDEX_FILE = "indexes.json"


@dataclass
class PwRecord:
    eval: PwEval
    index: int
    since_wall: float
    since_mono: float


def _updown(flag: bool) -> str:
    return "up" if flag else "down"


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers (simd owns SIGTERM/SIGINT)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Simd:
    def __init__(
        self,
        settings: Settings,
        *,
        bus: Bus | None = None,
        restconf: Restconf | None = None,
        sender: TrapSender | None = None,
    ) -> None:
        self.s = settings
        self.host = settings.hostname
        self.oids = load_oids()
        self.mib = MibView(self.oids)
        self.boot_id = uuid.uuid4().hex[:16]
        self.boot_wall = time.time()
        self.boot_mono = boottime()
        self.events = EventLog(self.host, self.boot_id, settings.event_buffer)
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.indexes = IndexStore(settings.state_dir / INDEX_FILE)
        self.restconf = restconf or Restconf(settings.restconf_url)
        self.watcher = ConfigWatcher(self.restconf, self._on_config_update, settings.config_poll_interval)
        self.bus = bus or Bus(settings.core_url, self.host, settings.hold_time)
        self.traps = TrapBuilder(self.oids)
        self.sender = sender or TrapSender(settings.trap_targets, settings.trap_community)
        self.batcher = TransitionBatcher()
        self.link_pending: list[LinkTransition] = []
        self.snmpdb: SnmpDbWriter | None = None

        self.config: DeviceConfig | None = None
        self.config_digest: str | None = None
        self.faults = LocalFaults()
        self.ifstates: dict[str, IfState] = {}
        self.uplink: UplinkState | None = None
        self.pws: dict[tuple[str, PwRole], PwRecord] = {}
        self.bus_view = BusSnapshot()
        self.peer_view = PeerView(adverts={}, own_fresh=False)
        self.core_ok = False
        self.ready = False
        self.baseline_done = False
        self.seq = 0
        self.last_advert_body: dict[str, Any] | None = None
        self.last_advert_write: float | None = None
        self.advert_ok_at: float | None = None
        self.ccm_index = 0
        self.recomputes = 0

        self._wake = asyncio.Event()
        self._trap_pending = asyncio.Event()
        self._stop = asyncio.Event()
        self._recompute_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[Any]] = []

    # ================================================================== lifecycle
    def stop(self) -> None:
        self._stop.set()

    def uptime_ticks(self) -> int:
        return timeticks_since(self.boot_mono)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)
        self.snmpdb = SnmpDbWriter(self.s.snmp_db)
        server = _Server(
            uvicorn.Config(
                create_app(self),
                host=self.s.api_host,
                port=self.s.api_port,
                log_config=None,
                access_log=False,
                lifespan="off",
            )
        )
        api_task = asyncio.create_task(server.serve(), name="api")
        self.events.add("boot", version=__version__, mgmt_ip=self.s.mgmt_ip)
        log_event(
            log,
            "boot",
            f"simd {__version__} starting, boot_id {self.boot_id}",
            boot_id=self.boot_id,
            mgmt_ip=self.s.mgmt_ip,
            core=self.s.core_url,
            trap_targets=[str(t) for t in self.s.trap_targets],
        )
        try:
            if not await self._wait_for_clixon():
                return
            snap = await self._bootstrap_config()
            await self._on_config_update(ConfigUpdate(None, snap, "initial", None, time.time()))
            self.watcher.current = snap
            self._tasks = [
                asyncio.create_task(self.watcher.run(self._stop), name="config-watch"),
                asyncio.create_task(self.bus.listen(self.wake, self._stop), name="bus-listen"),
                asyncio.create_task(self._hello_loop(), name="hello"),
                asyncio.create_task(self._recompute_loop(), name="recompute"),
                asyncio.create_task(self._trap_loop(), name="traps"),
            ]
            await self.recompute()
            await self._send_trap(self.traps.cold_start(self.uptime_ticks()))
            self.ready = True
            log_event(log, "ready", f"{self.host} ready: {len(self.pws)} pseudowire(s)", pws=len(self.pws))
            await self._stop.wait()
        finally:
            log_event(log, "shutdown", "simd stopping (advert left to expire, like a power-off)")
            self._stop.set()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(api_task, 3)
            self.sender.close()
            with contextlib.suppress(Exception):
                await self.bus.close()
            with contextlib.suppress(Exception):
                await self.restconf.aclose()
            if self.snmpdb is not None:
                self.snmpdb.close()

    async def _wait_for_clixon(self) -> bool:
        delay = 0.1
        while not self._stop.is_set():
            if await self.restconf.ready():
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)
        return False

    async def _bootstrap_config(self) -> ConfigSnapshot:
        """First boot: load contract/golden/<host>.json into an empty datastore. Later boots keep running."""
        marker = self.s.state_dir / MARKER_FILE
        while True:
            try:
                snap = await self.watcher.fetch()
                break
            except Exception as exc:
                log.warning("waiting for a readable running config: %s", exc, extra={"fields": {"event": "boot-wait"}})
                await asyncio.sleep(0.5)
        if marker.exists():
            return snap
        source = "existing"
        if snap.empty:
            golden = (self.s.golden_dir / f"{self.host}.json") if self.s.golden_dir else None
            if golden is not None and golden.is_file():
                await self.restconf.replace_running(json.loads(golden.read_text(encoding="utf-8")))
                snap = await self.watcher.fetch()
                source = "golden"
                log_event(log, "golden-load", f"first boot: loaded {golden}", file=str(golden), config_hash=snap.digest)
            else:
                source = "empty"
                log_event(
                    log, "golden-missing", f"first boot without a golden config for {self.host}", level=logging.WARNING
                )
        payload = {"source": source, "time": iso_ts(time.time()), "boot_id": self.boot_id, "config_hash": snap.digest}
        atomic_write_text(marker, json.dumps(payload, indent=2) + "\n")
        return snap

    # ================================================================== triggers
    def wake(self, reason: str = "") -> None:
        self._wake.set()

    async def _hello_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.s.hello_interval)
            self.wake("hello")

    async def _recompute_loop(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.recompute()
            except Exception:
                log.exception("recompute failed", extra={"fields": {"event": "recompute-error"}})
                await asyncio.sleep(0.2)

    # ================================================================== config
    async def _on_config_update(self, update: ConfigUpdate) -> None:
        old = self.config
        self.config = update.new.config
        self.config_digest = update.new.digest
        if update.source == "initial" or update.old is None or old is None:
            log_event(log, "config-loaded", f"running config {update.new.digest[:12]}", config_hash=update.new.digest)
        else:
            diffs = diff_configs(old, update.new.config)
            commit = update.commit
            user = commit.username if commit is not None and not commit.server else None
            self.events.add(
                "config-change",
                source=update.source,
                changed_by=user if user else ("server" if commit is not None and commit.server else None),
                config_hash=update.new.digest,
                commit=commit.to_dict() if commit is not None else None,
                diff=[d.to_dict() for d in diffs],
            )
            log_event(
                log,
                "config-change",
                f"running config changed ({len(diffs)} diff(s), via {update.source})",
                source=update.source,
                changed_by=user,
                config_hash=update.new.digest,
                diff=[d.message for d in diffs],
            )
            self.ccm_index += 1
            await self._send_trap(self.traps.config_man_event(self.ccm_index, user or "", self.uptime_ticks()))
        self.wake("config")

    # ================================================================== recompute
    async def recompute(self) -> None:
        if self.config is None:
            return
        async with self._recompute_lock:
            self.recomputes += 1
            config = self.config
            try:
                snap = await self.bus.snapshot()
                if not self.core_ok:
                    self.last_advert_body = None  # bus (re)connected: re-advertise and publish right away
                self.core_ok = True
            except (RedisError, OSError) as exc:
                if self.core_ok:
                    log.warning("core bus unreachable: %s", exc, extra={"fields": {"event": "core-down"}})
                self.core_ok = False
                snap = BusSnapshot(faults=self.faults)
            faults = snap.faults
            ifstates = interface_states(config, faults)
            uplink = uplink_state(config, ifstates, faults)
            loopback = config.system.loopback_ipv4
            if loopback is not None and self.core_ok:
                await self._refresh_advert(config, loopback, ifstates, uplink)
            now = boottime()
            own_fresh = (
                loopback is not None and self.advert_ok_at is not None and now - self.advert_ok_at < self.s.hold_time
            )
            view = PeerView(
                adverts={h: a for h, a in snap.adverts.items() if h != self.host},
                own_fresh=own_fresh,
                cut_peers=faults.cut_peers,
            )
            evals = evaluate_pseudowires(
                hostname=self.host, loopback=loopback, config=config, ifstates=ifstates, uplink=uplink, view=view
            )
            self._apply(config, evals, ifstates, uplink, faults, snap, view)

    async def _refresh_advert(
        self, config: DeviceConfig, loopback: str, ifstates: dict[str, IfState], uplink: UplinkState
    ) -> None:
        body = {
            "hostname": self.host,
            "loopback": loopback,
            "mgmt_ip": self.s.mgmt_ip,
            "boot_id": self.boot_id,
            "uplink_oper_up": uplink.oper_up,
            "pws": [p.model_dump(mode="json") for p in advert_pws(config, ifstates)],
        }
        now = boottime()
        changed = body != self.last_advert_body
        due = self.last_advert_write is None or now - self.last_advert_write >= 0.8 * self.s.hello_interval
        if not (changed or due):
            return
        self.seq += 1
        advert = Advert.model_validate({**body, "seq": self.seq, "ts": round(time.time(), 3)})
        try:
            await self.bus.write_advert(advert)
            self.advert_ok_at = now
            self.last_advert_write = now
            if changed:
                await self.bus.publish(state_message(self.host, self.boot_id, self.seq))
                self.last_advert_body = body
        except (RedisError, OSError) as exc:
            self.core_ok = False
            log.warning("cannot write advert: %s", exc, extra={"fields": {"event": "core-down"}})

    def _apply(
        self,
        config: DeviceConfig,
        evals: list[PwEval],
        ifstates: dict[str, IfState],
        uplink: UplinkState,
        faults: LocalFaults,
        snap: BusSnapshot,
        view: PeerView,
    ) -> None:
        now_wall, now_mono = time.time(), boottime()
        self._fault_events(faults)
        for name, st in ifstates.items():
            old = self.ifstates.get(name)
            if old is not None and old.oper != st.oper:
                self.events.add("if-oper-change", interface=name, old=old.oper.name, new=st.oper.name, fault=st.fault)
                log_event(
                    log,
                    "if-oper-change",
                    f"{name} {old.oper.name} -> {st.oper.name}",
                    interface=name,
                    old=old.oper.name,
                    new=st.oper.name,
                )
            if self.baseline_done:  # IF-MIB linkUp/linkDown: entering or leaving up(1); none for the boot baseline
                lt = link_transition(name, old.oper if old is not None else None, st.oper, st.admin_up)
                if lt is not None:
                    self.link_pending.append(lt)
        self._neighbor_events(snap)

        indexes = self.indexes.allocate_pws(e.pw.key for e in evals)
        records: dict[tuple[str, PwRole], PwRecord] = {}
        for e in evals:
            key = e.pw.key
            prev = self.pws.get(key)
            fields = {"service_id": e.pw.service_id, "role": e.pw.role.value, "index": indexes[key]}
            if prev is None:
                rec = PwRecord(e, indexes[key], now_wall, now_mono)
                self.events.add("pw-added", **fields, oper=e.oper.name, reason=e.reason.value)
                log_event(
                    log,
                    "pw-added",
                    f"{e.pw.name} added: {e.oper.name} ({e.reason.value})",
                    **fields,
                    new=e.oper.name,
                    reason=e.reason.value,
                )
                if self.baseline_done and e.oper is PwOperStatus.up:
                    self.batcher.add(Transition(rec.index, e.pw.name, None, e.oper))
            else:
                rec = prev
                if prev.eval.oper != e.oper:
                    rec.since_wall, rec.since_mono = now_wall, now_mono
                    self.events.add(
                        "oper-change",
                        **fields,
                        old=prev.eval.oper.name,
                        new=e.oper.name,
                        old_reason=prev.eval.reason.value,
                        reason=e.reason.value,
                    )
                    log_event(
                        log,
                        "oper-change",
                        f"{e.pw.name} {prev.eval.oper.name} -> {e.oper.name} ({e.reason.value})",
                        **fields,
                        old=prev.eval.oper.name,
                        new=e.oper.name,
                        reason=e.reason.value,
                    )
                    self.batcher.add(Transition(rec.index, e.pw.name, prev.eval.oper, e.oper))
                elif prev.eval.reason != e.reason:
                    self.events.add(
                        "reason-change",
                        **fields,
                        oper=e.oper.name,
                        old_reason=prev.eval.reason.value,
                        reason=e.reason.value,
                    )
                    log_event(
                        log,
                        "reason-change",
                        f"{e.pw.name} still {e.oper.name}: {prev.eval.reason.value} -> {e.reason.value}",
                        **fields,
                        old=prev.eval.reason.value,
                        new=e.reason.value,
                        reason=e.reason.value,
                    )
                rec.eval = e
            records[key] = rec
        for key in self.pws.keys() - records.keys():
            gone = self.pws[key]
            fields = {"service_id": key[0], "role": key[1].value, "index": gone.index}
            self.events.add("pw-removed", **fields, oper=gone.eval.oper.name)
            log_event(log, "pw-removed", f"{gone.eval.pw.name} removed (SNMP row {gone.index} withdrawn)", **fields)
        self.pws = records
        self.ifstates = ifstates
        self.uplink = uplink
        self.faults = faults
        self.bus_view = snap
        self.peer_view = view
        self._write_snmp(config)
        self.baseline_done = True
        if len(self.batcher) or self.link_pending:
            self._trap_pending.set()

    def _fault_events(self, faults: LocalFaults) -> None:
        old = self.faults
        changes: list[tuple[str, str, bool]] = []
        if faults.uplink != old.uplink:
            changes.append(("uplink", self.host, faults.uplink))
        changes += [("ac", i, True) for i in sorted(faults.interfaces - old.interfaces)]
        changes += [("ac", i, False) for i in sorted(old.interfaces - faults.interfaces)]
        changes += [("cut", p, True) for p in sorted(faults.cut_peers - old.cut_peers)]
        changes += [("cut", p, False) for p in sorted(old.cut_peers - faults.cut_peers)]
        for kind, target, active in changes:
            self.events.add("fault-change", kind=kind, target=target, active=active)
            log_event(
                log,
                "fault-change",
                f"{kind} fault {target} {'set' if active else 'cleared'}",
                kind=kind,
                target=target,
                active=active,
            )

    def _neighbor_events(self, snap: BusSnapshot) -> None:
        old = {h: a.boot_id for h, a in self.bus_view.adverts.items() if h != self.host}
        new = {h: a.boot_id for h, a in snap.adverts.items() if h != self.host}
        for h in sorted(new.keys() - old.keys()):
            self.events.add("neighbor-up", neighbor=h, loopback=snap.adverts[h].loopback, neighbor_boot_id=new[h])
            log_event(log, "neighbor-up", f"advert from {h} ({snap.adverts[h].loopback})", neighbor=h)
        for h in sorted(old.keys() - new.keys()):
            self.events.add("neighbor-down", neighbor=h)
            log_event(log, "neighbor-down", f"advert from {h} expired", neighbor=h)
        for h in sorted(new.keys() & old.keys()):
            if new[h] != old[h]:
                self.events.add("neighbor-restart", neighbor=h, neighbor_boot_id=new[h])
                log_event(log, "neighbor-restart", f"{h} restarted (new boot_id)", neighbor=h)

    def _write_snmp(self, config: DeviceConfig) -> None:
        if self.snmpdb is None:
            return
        if_indexes = self.indexes.allocate_ifs(
            (n, st.type is InterfaceType.SUBINTERFACE) for n, st in self.ifstates.items()
        )
        sysinfo = SysInfo(
            hostname=config.system.hostname or self.host,
            descr=f"NetSim PE (Clixon {self.s.clixon_version}) {config.system.hostname or self.host}",
            location=self.s.site or "",
            contact="",
            boot_since=self.boot_mono,
        )
        ifrows = [
            IfRow(
                index=if_indexes[n],
                name=n,
                type=st.type,
                mtu=st.mtu,
                admin_up=st.admin_up,
                oper=st.oper,
                alias=st.description or "",
            )
            for n, st in sorted(self.ifstates.items(), key=lambda kv: if_indexes[kv[0]])
        ]
        pwrows = []
        for rec in sorted(self.pws.values(), key=lambda r: r.index):
            e = rec.eval
            ac = self.ifstates.get(e.pw.ac)
            pwrows.append(
                PwRow(
                    index=rec.index,
                    name=e.pw.name,
                    descr=e.pw.customer or "",
                    subinterface_ac=ac is not None and ac.type is InterfaceType.SUBINTERFACE,
                    peer=e.pw.peer,
                    vc_id=e.pw.vc_id,
                    local_mtu=e.local_mtu,
                    remote_mtu=e.remote.mtu if e.remote else 0,
                    remote_if=e.remote.ac_name if e.remote else "",
                    admin_up=e.pw.admin_up,
                    oper=e.oper,
                    up_since=rec.since_mono if e.oper is PwOperStatus.up else None,
                )
            )
        meta = {"host": self.host, "boot_id": self.boot_id, "written_at": iso_ts(time.time())}
        self.snmpdb.write(self.mib.rows(sysinfo, ifrows, pwrows), meta)

    # ================================================================== traps
    async def _trap_loop(self) -> None:
        while not self._stop.is_set():
            await self._trap_pending.wait()
            await asyncio.sleep(self.s.trap_batch_window)
            self._trap_pending.clear()
            await self.flush_traps()

    async def flush_traps(self) -> None:
        """Send the batched transitions: one linkDown/linkUp per interface first (the cause), then the PW
        transitions (one notification per contiguous index range)."""
        links, self.link_pending = self.link_pending, []
        for lt in links:
            if_index = self.indexes.if_index(lt.name)  # allocated by _write_snmp in the same recompute
            if if_index is None:
                continue
            await self._send_trap(self.traps.link(lt.up, if_index, lt.admin_up, lt.new, self.uptime_ticks(), lt.name))
        for plan in self.batcher.drain():
            await self._send_trap(
                self.traps.pw_range(plan.up, plan.first, plan.last, plan.value, self.uptime_ticks(), plan.pws)
            )

    async def _send_trap(self, trap: Trap) -> None:
        try:
            sent = await self.sender.send(trap)
        except Exception as exc:
            log.warning("trap %s not sent: %s", trap.name, exc, extra={"fields": {"event": "trap-error"}})
            return
        self.events.add("trap-sent", targets=sent, **trap.summary())
        subjects = trap.pws or trap.interfaces
        log_event(
            log,
            "trap-sent",
            f"{trap.name} -> {', '.join(sent) if sent else 'no targets'}"
            + (f" ({', '.join(subjects)})" if subjects else ""),
            trap=trap.name,
            targets=sent,
            pws=list(trap.pws),
            interfaces=list(trap.interfaces),
        )

    # ================================================================== API views
    def health(self) -> Health:
        checks = {
            "restconf": self.watcher.last_fetch_ok,
            "core": self.core_ok,
            "snmp_db": self.snmpdb is not None and self.snmpdb.generation > 0,
            "config": self.config is not None,
            "config_stream": self.watcher.stream_connected,
        }
        if not self.ready:
            status = "starting"
        elif all(v for k, v in checks.items() if k != "config_stream"):
            status = "ok"
        else:
            status = "degraded"
        return Health(
            status=status, boot_id=self.boot_id, uptime_s=round(boottime() - self.boot_mono, 3), checks=checks
        )

    def state(self) -> DeviceState:
        if self.config is None or self.uplink is None:
            raise LookupError("simd is starting: no state yet")
        if_indexes = self.indexes.if_map
        pws = []
        for rec in sorted(self.pws.values(), key=lambda r: r.index):
            e = rec.eval
            pws.append(
                PseudowireState(
                    index=rec.index,
                    service_id=e.pw.service_id,
                    role=e.pw.role,
                    customer=e.pw.customer,
                    ac=e.pw.ac,
                    peer=e.pw.peer,
                    peer_hostname=e.peer_hostname,
                    vc_id=e.pw.vc_id,
                    local_mtu=e.local_mtu,
                    admin=_updown(e.pw.admin_up),
                    oper=e.oper.name,
                    reason=e.reason,
                    rule=e.reason.rule,
                    since=iso_ts(rec.since_wall),
                    remote=Remote(
                        hostname=e.remote.hostname,
                        mtu=e.remote.mtu,
                        ac_name=e.remote.ac_name,
                        service_id=e.remote.service_id,
                        role=e.remote.role,
                    )
                    if e.remote
                    else None,
                )
            )
        interfaces = [
            InterfaceState(
                index=if_indexes.get(n, 0),
                name=n,
                type=st.type.value,
                description=st.description,
                parent=st.parent,
                mtu=st.mtu,
                admin=_updown(st.admin_up),
                oper=st.oper.name,
                fault=st.fault,
            )
            for n, st in self.ifstates.items()
        ]
        return DeviceState(
            hostname=self.host,
            loopback=self.config.system.loopback_ipv4,
            mgmt_ip=self.s.mgmt_ip,
            boot_id=self.boot_id,
            booted_at=iso_ts(self.boot_wall),
            uptime_s=round(boottime() - self.boot_mono, 3),
            config_hash=self.config_digest,
            uplink=Uplink(
                admin=_updown(self.uplink.admin_up),
                oper=_updown(self.uplink.oper_up),
                fault=self.uplink.fault,
                interfaces=list(self.uplink.interfaces),
            ),
            faults=Faults(
                uplink=self.faults.uplink,
                interfaces=sorted(self.faults.interfaces),
                cuts=sorted(self.faults.cut_peers),
            ),
            pseudowires=pws,
            interfaces=interfaces,
        )

    def neighbors(self) -> list[Neighbor]:
        uplink_up = self.uplink.oper_up if self.uplink else False
        out = []
        for host, adv in sorted(self.bus_view.adverts.items()):
            if host == self.host:
                continue
            out.append(
                Neighbor(
                    hostname=host,
                    loopback=adv.loopback,
                    mgmt_ip=adv.mgmt_ip,
                    boot_id=adv.boot_id,
                    seq=adv.seq,
                    age_s=round(self.bus_view.ages.get(host, 0.0), 3),
                    uplink_oper_up=adv.uplink_oper_up,
                    pw_count=len(adv.pws),
                    reachable=reachable(adv, self.peer_view, uplink_up),
                )
            )
        return out

    def events_response(self, since: float | None, limit: int | None) -> EventsResponse:
        return EventsResponse(
            host=self.host,
            boot_id=self.boot_id,
            now=round(time.time(), 6),
            capacity=self.events.capacity,
            events=self.events.since(since, limit),
        )

    async def resync(self) -> ResyncResult:
        if self.config is None:
            raise LookupError("simd is starting: no config yet")
        changed = await self.watcher.refresh("resync")
        self.last_advert_body = None  # force advert write + publish
        await self.recompute()
        return ResyncResult(config_changed=changed, state=self.state())


# ====================================================================== entry point
def main() -> int:
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"simd: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.hostname, settings.log_level)
    asyncio.run(Simd(settings).run())
    return 0
