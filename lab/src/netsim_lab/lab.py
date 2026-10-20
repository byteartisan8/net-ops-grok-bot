"""One object for the whole lab: devices (RESTCONF + simd), ``core``, Docker, NetBox intent and Zabbix.

    with Lab() as lab:
        report = lab.drift()             # netsim_lab.drift.DriftReport
        lab.core.set_uplink("hub-a")     # physical fault
        lab.zabbix().service_statuses()  # {"SVC-0001": "Warning", ...}

Thread-safe for lab-api's request threads: the HTTP clients pool connections, intent loads are cached briefly
on request, and the Zabbix client is rebuilt when ``ZABBIX_API_TOKEN`` changes in ``.env``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

from netsim_common.canonical import canonical_dict
from netsim_common.device_config import DeviceConfig
from netsim_common.golden import render_device_config
from netsim_common.topology import Topology, load_topology
from netsim_lab.core import Core, CoreError
from netsim_lab.devices import Devices
from netsim_lab.docker_api import Docker, DockerError
from netsim_lab.drift import DriftInputs, DriftReport, analyze, baseline_updates, plans_from_intent
from netsim_lab.settings import LabSettings, load_settings
from netsim_lab.zabbix import LabZabbix


class Lab:
    def __init__(self, settings: LabSettings | None = None, topology: Topology | None = None) -> None:
        self.topology = topology or load_topology()
        self.settings = settings or load_settings(topology=self.topology)
        self.devices = Devices(self.settings.devices, self.settings.timeout)
        self.core = Core(self.settings.core_url)
        self.docker = Docker(self.settings.docker_socket, allowed=self.settings.hosts)
        self._lock = threading.Lock()
        self._zabbix: LabZabbix | None = None
        self._zabbix_token: str | None = None
        self._intent: tuple[float, Any] | None = None
        self._golden: dict[str, DeviceConfig] | None = None

    def __enter__(self) -> Lab:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.devices.close()
        self.core.close()
        self.docker.close()
        if self._zabbix is not None:
            self._zabbix.close()

    @property
    def hosts(self) -> list[str]:
        return self.settings.hosts

    # ------------------------------------------------------------------ NetBox
    def intent(self, max_age: float = 0.0) -> Any:
        """NetBox intent snapshot (``netsim_netbox.intent.LabIntent``), reused if younger than ``max_age`` s."""
        from netsim_netbox.intent import load_intent

        with self._lock:
            cached = self._intent
        if cached is not None and max_age > 0 and time.monotonic() - cached[0] < max_age:
            return cached[1]
        intent = load_intent()
        with self._lock:
            self._intent = (time.monotonic(), intent)
        return intent

    def forget_intent(self) -> None:
        with self._lock:
            self._intent = None

    def golden(self) -> dict[str, DeviceConfig]:
        if self._golden is None:
            self._golden = {h: render_device_config(self.topology, h) for h in self.hosts}
        return self._golden

    # ------------------------------------------------------------------ Zabbix
    def zabbix(self) -> LabZabbix:
        """Zabbix client with the current ``ZABBIX_API_TOKEN`` (re-read from ``.env``)."""
        from netsim_zabbix.settings import load_settings as zabbix_settings

        settings = zabbix_settings()
        with self._lock:
            if self._zabbix is None or settings.token != self._zabbix_token:
                if self._zabbix is not None:
                    self._zabbix.close()
                self._zabbix = LabZabbix(settings.api())
                self._zabbix_token = settings.token
            return self._zabbix

    # ------------------------------------------------------------------ Docker
    def stopped(self) -> list[str]:
        """PE containers that exist but are not running (``[]`` if Docker cannot be asked)."""
        try:
            return self.docker.stopped()
        except DockerError:
            return []

    # ------------------------------------------------------------------ baselines
    def baselines(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        golden = self.golden()
        out, source = {}, {}
        for host in self.hosts:
            stored = self.core.baseline(host)
            out[host] = stored if stored is not None else canonical_dict(golden[host])
            source[host] = "stored" if stored is not None else "golden"
        return out, source

    def record_agreement(self, host: str, config: DeviceConfig) -> None:
        """Device and NetBox agree on ``config``: it becomes the host's drift baseline."""
        if canonical_dict(config) == canonical_dict(self.golden()[host]):
            self.core.clear_baseline(host)
        else:
            self.core.set_baseline(host, config)

    # ------------------------------------------------------------------ drift
    def drift_inputs(self, intent: Any | None = None, running: dict[str, Any] | None = None) -> DriftInputs:
        from netsim_netbox.intent import IntentError, render_device

        intent = intent if intent is not None else self.intent()
        intended: dict[str, Any] = {}
        for host in self.hosts:
            try:
                intended[host] = render_device(host, intent)
            except IntentError as exc:
                intended[host] = exc
            except KeyError as exc:
                intended[host] = RuntimeError(f"{host} is not a device in NetBox ({exc})")
        loopbacks = {d.hostname: d.loopback for d in self.topology.devices}
        for name, dev in intent.devices.items():
            if dev.loopback:
                loopbacks[name] = dev.loopback
        if running is None:
            running = dict(self.devices.running())
        baselines, sources = self.baselines()
        return DriftInputs(
            hosts=self.hosts,
            intended=intended,
            running=running,
            baselines=baselines,
            baseline_source=sources,
            plans=plans_from_intent(intent),
            loopbacks=loopbacks,
            states=dict(self.devices.states()),
            faults=self.core.faults(),
            stopped=self.stopped(),
            intent_problems=[str(p) for p in intent.problems],
        )

    def drift(self, *, record: bool = True) -> DriftReport:
        """Check every device against NetBox (see :mod:`netsim_lab.drift`); records new agreements as baselines."""
        start = time.monotonic()
        inputs = self.drift_inputs()
        report = analyze(inputs)
        if record:
            for host, config in baseline_updates(inputs, report).items():
                with contextlib.suppress(CoreError):
                    self.record_agreement(host, config)
        report.duration_s = time.monotonic() - start
        return report
