"""Drift checker: NetBox intent vs the devices' running config, with semantic findings per service.

**Per device:** ``diff_configs(intent, running)``. Each difference is classified with a three-way comparison
against the device's *baseline* (the last config on which device and NetBox agreed, :mod:`netsim_lab.core`;
golden when none is stored):

* ``config-drift``: the device moved away from the baseline (someone changed the device);
* ``intent-only``: the device still equals the baseline and NetBox moved (a NetBox change not yet applied).

**Per service:** the differences are interpreted the way an engineer would read them, e.g.

* ``SVC-0002: both PWs terminate on hub-b (intent: hub-a + hub-b) — redundancy lost``
* ``SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001``
* ``SVC-0007: in NetBox but not configured on hub-a, hub-b, spoke-3 (ordered, not delivered)``

and services whose PWs are down without any config difference get ``fault`` findings that name the physical
cause (fault key, core cut, powered-off device), e.g. ``SVC-0001: primary PW down: hub-a core uplink down
(fault); service degraded``.

Finding classes (prompt 05): ``config-drift`` (device != NetBox), ``fault`` (an active fault, no drift),
``intent-only`` (NetBox changed, device not yet updated). Drift = any ``config-drift`` or ``intent-only`` finding.

:func:`analyze` is pure (unit-tested with hand-made inputs); :func:`check` gathers the inputs from NetBox, the
devices, ``core`` and Docker, then records new agreements as baselines.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from netsim_common.canonical import ConfigDiff, DiffKind, canonical_dict, diff_configs
from netsim_common.device_config import DeviceConfig, Pseudowire, Xconnect
from netsim_common.signaling import FaultKey
from netsim_lab.configpath import get_path

FindingClass = Literal["config-drift", "fault", "intent-only"]
DiffClass = Literal["config-drift", "intent-only"]
DRIFT_CLASSES = ("config-drift", "intent-only")
ROLE_ORDER = {"primary": 0, "backup": 1}


# ---------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True, slots=True)
class PwPlan:
    """Intent of one PW: spoke end (a-end) and hub end (z-end)."""

    service: str
    role: str
    vc_id: int
    spoke: str
    spoke_ac: str
    hub: str
    hub_ac: str


@dataclass(frozen=True, slots=True)
class ServicePlan:
    service: str
    customer: str | None
    spoke: str
    pws: dict[str, PwPlan]

    @property
    def hosts(self) -> list[str]:
        return sorted({self.spoke, *(p.hub for p in self.pws.values())})


def plans_from_intent(intent: Any) -> dict[str, ServicePlan]:
    """Service plans from a :class:`netsim_netbox.intent.LabIntent` (a-end = spoke, z-end = hub)."""
    out = {}
    for service_id, pws in sorted(intent.services().items()):
        plans = {}
        for pw in pws:
            plans[pw.role.value] = PwPlan(
                service=service_id,
                role=pw.role.value,
                vc_id=pw.vc_id,
                spoke=pw.a_end.host,
                spoke_ac=pw.a_end.ac,
                hub=pw.z_end.host,
                hub_ac=pw.z_end.ac,
            )
        out[service_id] = ServicePlan(service_id, pws[0].customer, pws[0].a_end.host, plans)
    return out


def plans_from_topology(topology: Any) -> dict[str, ServicePlan]:
    out = {}
    for svc in topology.services:
        pws = {}
        for role in ("primary", "backup"):
            spw = getattr(svc.pseudowires, role)
            pws[role] = PwPlan(svc.id, role, spw.vc_id, svc.spoke, svc.spoke_ac, spw.hub, svc.hub_ac)
        out[svc.id] = ServicePlan(svc.id, svc.customer, svc.spoke, pws)
    return out


@dataclass
class DriftInputs:
    """Everything :func:`analyze` needs. Per-host values are exceptions when they could not be obtained."""

    hosts: list[str]
    intended: dict[str, DeviceConfig | Exception]
    running: dict[str, DeviceConfig | Exception]
    baselines: dict[str, Mapping[str, Any]]
    baseline_source: dict[str, str]
    plans: dict[str, ServicePlan]
    loopbacks: dict[str, str]
    states: dict[str, Mapping[str, Any] | Exception] = field(default_factory=dict)
    faults: list[FaultKey] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    intent_problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------- outputs
@dataclass(frozen=True)
class ClassifiedDiff:
    host: str
    diff: ConfigDiff
    cls: DiffClass
    baseline: Any

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, **self.diff.to_dict(), "class": self.cls, "baseline": self.baseline}


@dataclass
class DeviceDrift:
    host: str
    status: Literal["in-sync", "drift", "unreachable", "no-intent"]
    diffs: list[ClassifiedDiff] = field(default_factory=list)
    baseline_source: str = "golden"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "status": self.status,
            "baseline": self.baseline_source,
            "error": self.error,
            "diffs": [d.to_dict() for d in self.diffs],
        }


@dataclass
class Finding:
    cls: FindingClass
    kind: str
    service: str | None
    message: str
    hosts: list[str]
    roles: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    live: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def is_drift(self) -> bool:
        return self.cls in DRIFT_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "kind": self.kind,
            "service": self.service,
            "message": self.message,
            "hosts": self.hosts,
            "roles": self.roles,
            "paths": self.paths,
            "details": self.details,
            "live": self.live,
        }


@dataclass
class DriftReport:
    devices: dict[str, DeviceDrift]
    findings: list[Finding]
    faults: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    intent_problems: list[str] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @property
    def drift(self) -> bool:
        return any(f.is_drift for f in self.findings)

    @property
    def incomplete(self) -> bool:
        return any(d.status in ("unreachable", "no-intent") for d in self.devices.values())

    def for_service(self, service: str) -> list[Finding]:
        return [f for f in self.findings if f.service == service]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.cls] = counts.get(f.cls, 0) + 1
        return {
            "drift": self.drift,
            "incomplete": self.incomplete,
            "summary": counts,
            "findings": [f.to_dict() for f in self.findings],
            "devices": {h: d.to_dict() for h, d in self.devices.items()},
            "faults": self.faults,
            "stopped": self.stopped,
            "intent_problems": self.intent_problems,
            "checked_at": self.checked_at,
            "duration_s": round(self.duration_s, 3),
        }


# ---------------------------------------------------------------------------------------------- classification
def _strip_key(value: Any, list_name: str | None) -> Any:
    if isinstance(value, dict) and list_name:
        from netsim_common.canonical import LIST_KEYS

        return {k: v for k, v in value.items() if k != LIST_KEYS.get(list_name)}
    return value


def classify(host: str, diff: ConfigDiff, baseline: Mapping[str, Any]) -> ClassifiedDiff:
    """``intent-only`` when the device still equals its baseline at this path, else ``config-drift``."""
    try:
        base = get_path(baseline, diff.path)
    except KeyError:
        base = None
    last = diff.parts[-1]
    if last.key is not None:
        base = _strip_key(base, last.name)
    cls: DiffClass = "intent-only" if base == diff.actual and diff.expected != base else "config-drift"
    return ClassifiedDiff(host, diff, cls, base)


# ---------------------------------------------------------------------------------------------- analysis
@dataclass
class _Obs:
    """One semantic observation about a service (becomes a finding, or a detail of one)."""

    kind: str
    service: str | None
    message: str
    hosts: set[str]
    roles: set[str]
    prefixes: list[tuple[str, str]]  # (host, path prefix) this observation explains
    diffs: list[ClassifiedDiff] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def matches(self, cd: ClassifiedDiff) -> bool:
        return any(cd.host == h and (cd.diff.path == p or cd.diff.path.startswith(p + "/")) for h, p in self.prefixes)


def _pw(xc: Xconnect | None, role: str) -> Pseudowire | None:
    return xc.pw(role) if xc is not None else None


def _mtu(cfg: DeviceConfig | None, ac: str | None) -> int | None:
    if cfg is None or ac is None:
        return None
    try:
        return cfg.effective_mtu(ac)
    except KeyError:
        return None


def _iface_prefixes(host: str, cfg: DeviceConfig | None, ac: str | None) -> list[tuple[str, str]]:
    if ac is None:
        return []
    out = [(host, f"interfaces/interface[{ac}]")]
    iface = cfg.interface(ac) if cfg is not None else None
    if iface is not None and iface.parent:
        out.append((host, f"interfaces/interface[{iface.parent}]"))
    return out


def _roles(roles: Iterable[str]) -> list[str]:
    return sorted(set(roles), key=lambda r: ROLE_ORDER.get(r, 9))


def _live(states: Mapping[str, Any], service: str, plan: ServicePlan | None) -> dict[str, dict[str, str]]:
    """``{role: {host: "oper/reason"}}`` from simd, for the service's intended ends."""
    out: dict[str, dict[str, str]] = {}
    if plan is None:
        return out
    for role, pw in plan.pws.items():
        ends = {}
        for host in (pw.spoke, pw.hub):
            st = states.get(host)
            if st is None:
                continue
            if isinstance(st, Exception):
                ends[host] = "unreachable"
                continue
            row = next(
                (p for p in st.get("pseudowires", []) if p.get("service_id") == service and p.get("role") == role),
                None,
            )
            ends[host] = f"{row['oper']}/{row['reason']}" if row else "absent"
        out[role] = ends
    return out


class _Analyzer:
    def __init__(self, inputs: DriftInputs) -> None:
        self.i = inputs
        self.by_loopback = {lo: h for h, lo in inputs.loopbacks.items()}
        self.ok_running = {h: c for h, c in inputs.running.items() if isinstance(c, DeviceConfig)}
        self.ok_intended = {h: c for h, c in inputs.intended.items() if isinstance(c, DeviceConfig)}

    # ---------------------------------------------------------------- helpers
    def peer_name(self, address: str) -> str:
        host = self.by_loopback.get(address)
        return f"{address} ({host})" if host else f"{address} (no PE has this loopback)"

    def running(self, host: str) -> DeviceConfig | None:
        return self.ok_running.get(host)

    def reachable(self, host: str) -> bool:
        return host in self.ok_running

    # ---------------------------------------------------------------- per device
    def devices(self) -> tuple[dict[str, DeviceDrift], list[ClassifiedDiff]]:
        out, all_diffs = {}, []
        for host in self.i.hosts:
            source = self.i.baseline_source.get(host, "golden")
            intended, running = self.i.intended.get(host), self.i.running.get(host)
            if not isinstance(running, DeviceConfig):
                out[host] = DeviceDrift(host, "unreachable", baseline_source=source, error=str(running))
                continue
            if not isinstance(intended, DeviceConfig):
                out[host] = DeviceDrift(host, "no-intent", baseline_source=source, error=str(intended))
                continue
            baseline = self.i.baselines.get(host, {})
            diffs = [classify(host, d, baseline) for d in diff_configs(intended, running)]
            all_diffs.extend(diffs)
            out[host] = DeviceDrift(host, "drift" if diffs else "in-sync", diffs, source)
        return out, all_diffs

    # ---------------------------------------------------------------- per service
    def service_observations(self, service: str) -> list[_Obs]:
        plan = self.i.plans.get(service)
        actual_hosts = sorted(h for h, c in self.ok_running.items() if c.xconnect(service) is not None)
        if plan is None:
            if not actual_hosts:
                return []
            prefixes = []
            for h in actual_hosts:
                xc = self.ok_running[h].xconnect(service)
                prefixes.append((h, f"l2vpn/xconnect[{service}]"))
                prefixes += [
                    p
                    for p in _iface_prefixes(h, self.ok_running[h], xc.attachment_circuit)[:1]
                    if self._subif(h, xc.attachment_circuit)
                ]
            roles = {pw.role.value for h in actual_hosts for pw in self.ok_running[h].xconnect(service).pseudowire}
            return [
                _Obs(
                    "not-removed",
                    service,
                    f"{service}: not in NetBox but still configured on {', '.join(actual_hosts)}",
                    set(actual_hosts),
                    roles,
                    prefixes,
                )
            ]
        intent_hosts = plan.hosts
        reachable_intent = [h for h in intent_hosts if self.reachable(h)]
        if reachable_intent and not (set(actual_hosts) & set(intent_hosts)):
            prefixes = [(h, f"l2vpn/xconnect[{service}]") for h in reachable_intent]
            for pw in plan.pws.values():
                prefixes += [
                    (pw.spoke, f"interfaces/interface[{pw.spoke_ac}]"),
                    (pw.hub, f"interfaces/interface[{pw.hub_ac}]"),
                ]
            return [
                _Obs(
                    "not-delivered",
                    service,
                    f"{service}: in NetBox but not configured on {', '.join(reachable_intent)} "
                    "(ordered, not delivered)",
                    set(reachable_intent),
                    set(plan.pws),
                    prefixes,
                )
            ]

        obs: list[_Obs] = []
        spoke_cfg = self.running(plan.spoke)
        spoke_xc = spoke_cfg.xconnect(service) if spoke_cfg else None
        intended_hubs = {role: pw.hub for role, pw in plan.pws.items()}

        # redundancy: where do the spoke's PWs actually go?
        if spoke_xc is not None and len(set(intended_hubs.values())) > 1:
            actual = {pw.role.value: self.by_loopback.get(pw.peer_ipv4) for pw in spoke_xc.pseudowire}
            targets = set(actual.values())
            if len(actual) >= 2 and None not in targets and len(targets) == 1:
                hub = next(iter(targets))
                wanted = " + ".join(sorted(set(intended_hubs.values())))
                prefixes = [(plan.spoke, f"l2vpn/xconnect[{service}]")]
                prefixes += [(h, f"l2vpn/xconnect[{service}]") for h in set(intended_hubs.values())]
                obs.append(
                    _Obs(
                        "redundancy-lost",
                        service,
                        f"{service}: both PWs terminate on {hub} (intent: {wanted}) — redundancy lost",
                        {plan.spoke, *intended_hubs.values()},
                        set(actual),
                        prefixes,
                    )
                )

        for _role, pw in sorted(plan.pws.items(), key=lambda kv: ROLE_ORDER.get(kv[0], 9)):
            obs.extend(self.pw_observations(service, plan, pw, spoke_cfg, spoke_xc))

        # PWs on hosts that the intent does not put there
        for host in actual_hosts:
            xc = self.ok_running[host].xconnect(service)
            for apw in xc.pseudowire:
                role = apw.role.value
                ipw = plan.pws.get(role)
                if ipw is not None and host in (ipw.spoke, ipw.hub):
                    continue
                where = f"intent: {role} on {ipw.hub}" if ipw else f"intent has no {role} PW"
                obs.append(
                    _Obs(
                        "pw-unexpected",
                        service,
                        f"{service}: unexpected {role} PW on {host} (peer {apw.peer_ipv4}, vc-id {apw.vc_id}); {where}",
                        {host},
                        {role},
                        [(host, f"l2vpn/xconnect[{service}]/pseudowire[{role}]"), (host, f"l2vpn/xconnect[{service}]")]
                        + (
                            [(host, f"interfaces/interface[{xc.attachment_circuit}]")]
                            if host not in intent_hosts and self._subif(host, xc.attachment_circuit)
                            else []
                        ),
                    )
                )
        return self._fold_redundancy(obs)

    def _subif(self, host: str, name: str | None) -> bool:
        """True if ``name`` is a subinterface on ``host`` (only its own service uses it)."""
        cfg = self.ok_running.get(host)
        iface = cfg.interface(name) if cfg and name else None
        return iface is not None and iface.is_subinterface

    def pw_observations(
        self, service: str, plan: ServicePlan, pw: PwPlan, spoke_cfg: DeviceConfig | None, spoke_xc: Xconnect | None
    ) -> list[_Obs]:
        role = pw.role
        out: list[_Obs] = []
        hub_cfg = self.running(pw.hub)
        hub_xc = hub_cfg.xconnect(service) if hub_cfg else None
        s_pw, h_pw = _pw(spoke_xc, role), _pw(hub_xc, role)
        pw_path = f"l2vpn/xconnect[{service}]/pseudowire[{role}]"
        xc_path = f"l2vpn/xconnect[{service}]"
        ends = ((pw.spoke, spoke_cfg, spoke_xc, s_pw, pw.hub), (pw.hub, hub_cfg, hub_xc, h_pw, pw.spoke))

        for host, cfg, xc, apw, peer_host in ends:
            if cfg is None:
                continue
            if apw is None:
                out.append(
                    _Obs(
                        "pw-missing",
                        service,
                        f"{service}: {role} PW missing on {host} (intent: {pw.spoke} <-> {pw.hub}, vc-id {pw.vc_id})",
                        {host},
                        {role},
                        [(host, pw_path), (host, xc_path)]
                        + (
                            [(host, f"interfaces/interface[{pw.spoke_ac if host == pw.spoke else pw.hub_ac}]")]
                            if xc is None
                            else []
                        ),
                    )
                )
                continue
            want_peer = self.i.loopbacks.get(peer_host)
            if want_peer and apw.peer_ipv4 != want_peer:
                out.append(
                    _Obs(
                        "peer-mismatch",
                        service,
                        f"{service}: {role} PW on {host} points to {self.peer_name(apw.peer_ipv4)}, "
                        f"intent {want_peer} ({peer_host})",
                        {host},
                        {role},
                        [(host, f"{pw_path}/peer-ipv4")],
                    )
                )
            if not apw.enabled or not xc.enabled:
                what = "PW" if not apw.enabled else "xconnect"
                out.append(
                    _Obs(
                        "admin-down",
                        service,
                        f"{service}: {role} {what} disabled on {host} (enabled=false), intent enabled",
                        {host},
                        {role},
                        [(host, f"{pw_path}/enabled"), (host, f"{xc_path}/enabled")],
                    )
                )
            ac = xc.attachment_circuit
            if ac and cfg.interface(ac) is not None and not cfg.admin_up(ac):
                out.append(
                    _Obs(
                        "ac-admin-down",
                        service,
                        f"{service}: attachment circuit {ac} disabled on {host} (enabled=false), intent enabled",
                        {host},
                        {role},
                        _iface_prefixes(host, cfg, ac),
                    )
                )

        if s_pw is not None and h_pw is not None:
            if s_pw.vc_id != h_pw.vc_id:
                spoke_off, hub_off = s_pw.vc_id != pw.vc_id, h_pw.vc_id != pw.vc_id
                first, second = (
                    ((pw.hub, h_pw.vc_id), (pw.spoke, s_pw.vc_id))
                    if hub_off and not spoke_off
                    else ((pw.spoke, s_pw.vc_id), (pw.hub, h_pw.vc_id))
                )
                out.append(
                    _Obs(
                        "vcid-mismatch",
                        service,
                        f"{service}: {role} vc-id mismatch {first[0]}={first[1]} vs {second[0]}={second[1]}",
                        {h for h, off in ((pw.spoke, spoke_off), (pw.hub, hub_off)) if off} or {pw.spoke, pw.hub},
                        {role},
                        [(pw.spoke, f"{pw_path}/vc-id"), (pw.hub, f"{pw_path}/vc-id")],
                    )
                )
            elif s_pw.vc_id != pw.vc_id:
                out.append(
                    _Obs(
                        "vcid-changed",
                        service,
                        f"{service}: {role} vc-id {s_pw.vc_id} on {pw.spoke} and {pw.hub}, intent {pw.vc_id}",
                        {pw.spoke, pw.hub},
                        {role},
                        [(pw.spoke, f"{pw_path}/vc-id"), (pw.hub, f"{pw_path}/vc-id")],
                    )
                )
        else:
            for host, apw in ((pw.spoke, s_pw), (pw.hub, h_pw)):
                if apw is not None and apw.vc_id != pw.vc_id:
                    out.append(
                        _Obs(
                            "vcid-changed",
                            service,
                            f"{service}: {role} vc-id {apw.vc_id} on {host}, intent {pw.vc_id}",
                            {host},
                            {role},
                            [(host, f"{pw_path}/vc-id")],
                        )
                    )

        # MTU: both ends' attachment circuits must agree (contract section 7, rule 9)
        if spoke_xc is not None and hub_xc is not None:
            m_s, m_h = _mtu(spoke_cfg, spoke_xc.attachment_circuit), _mtu(hub_cfg, hub_xc.attachment_circuit)
            if m_s is not None and m_h is not None and m_s != m_h:
                want = _mtu(self.ok_intended.get(pw.spoke), pw.spoke_ac) or _mtu(
                    self.ok_intended.get(pw.hub), pw.hub_ac
                )
                hub_off = want is not None and m_h != want
                first, second = ((pw.hub, m_h), (pw.spoke, m_s)) if hub_off else ((pw.spoke, m_s), (pw.hub, m_h))
                suffix = f" (intent {want})" if want is not None else ""
                out.append(
                    _Obs(
                        "mtu-mismatch",
                        service,
                        f"{service}: {role} MTU mismatch {first[0]}={first[1]} vs {second[0]}={second[1]}{suffix}",
                        {h for h, m in ((pw.spoke, m_s), (pw.hub, m_h)) if want is None or m != want},
                        {role},
                        _iface_prefixes(pw.spoke, spoke_cfg, spoke_xc.attachment_circuit)
                        + _iface_prefixes(pw.hub, hub_cfg, hub_xc.attachment_circuit),
                    )
                )
        return out

    @staticmethod
    def _fold_redundancy(obs: list[_Obs]) -> list[_Obs]:
        """A redundancy-lost observation explains the missing/unexpected/repointed PWs of the same service."""
        lost = next((o for o in obs if o.kind == "redundancy-lost"), None)
        if lost is None:
            return obs
        rest = []
        for o in obs:
            if o is lost:
                continue
            if o.kind in ("pw-missing", "pw-unexpected", "peer-mismatch"):
                lost.prefixes.extend(o.prefixes)
                lost.hosts |= o.hosts
                lost.roles |= o.roles
                lost.details.append(o.message)
            else:
                rest.append(o)
        return [lost, *rest]

    # ---------------------------------------------------------------- device-level drift
    def device_observations(self, unexplained: list[ClassifiedDiff]) -> list[_Obs]:
        out = []
        by_host: dict[str, list[ClassifiedDiff]] = {}
        for cd in unexplained:
            by_host.setdefault(cd.host, []).append(cd)
        for host, diffs in sorted(by_host.items()):
            cfg = self.running(host)
            for cd in diffs:
                path = cd.diff.path
                kind = "device-config"
                if path == "system/loopback-ipv4":
                    kind = "loopback-changed"
                elif cfg is not None and cd.diff.interface:
                    iface = cfg.interface(cd.diff.interface)
                    if iface is not None and iface.type.value == "core-uplink" and cd.diff.leaf == "enabled":
                        kind = "uplink-admin-down" if cd.diff.actual is False else "device-config"
                out.append(_Obs(kind, None, f"{host}: {cd.diff.message}", {host}, set(), [(host, path)], [cd]))
        return out

    # ---------------------------------------------------------------- faults
    def fault_findings(self, explained_roles: dict[str, set[str]], device_drift_hosts: set[str]) -> list[Finding]:
        stopped = set(self.i.stopped)
        uplinks = {f.hosts[0] for f in self.i.faults if f.kind == "uplink"}
        cuts = {frozenset(f.hosts) for f in self.i.faults if f.kind == "cut"}
        acs = {(f.hosts[0], f.ifname) for f in self.i.faults if f.kind == "ac"}
        findings: list[Finding] = []
        for service, plan in sorted(self.i.plans.items()):
            live = _live(self.i.states, service, plan)
            down_roles = {
                role for role, ends in live.items() if any(not v.startswith("up/") for v in ends.values())
            } - explained_roles.get(service, set())
            if not down_roles:
                continue
            causes: dict[tuple[str, str], tuple[str, set[str], set[str]]] = {}

            def add(
                kind: str, key: str, text: str, role: str, hosts: Iterable[str], causes: dict[Any, Any] = causes
            ) -> None:
                entry = causes.setdefault((kind, key), (text, set(), set()))
                entry[1].add(role)
                entry[2].update(hosts)

            for role in down_roles:
                pw = plan.pws[role]
                found = False
                for host in (pw.spoke, pw.hub):
                    if host in device_drift_hosts:
                        found = True  # explained by a device-level config change (reported as config-drift)
                    if host in stopped:
                        add("device-down", host, f"{host} powered off (container stopped)", role, [host])
                        found = True
                    elif isinstance(self.i.states.get(host), Exception):
                        add("device-unreachable", host, f"{host} not answering (simd unreachable)", role, [host])
                        found = True
                    if host in uplinks:
                        add("uplink-down", host, f"{host} core uplink down", role, [host])
                        found = True
                if frozenset((pw.spoke, pw.hub)) in cuts:
                    add(
                        "core-cut",
                        f"{pw.spoke}|{pw.hub}",
                        f"core path {pw.spoke} <-> {pw.hub} cut",
                        role,
                        [pw.spoke, pw.hub],
                    )
                    found = True
                for host, ac in ((pw.spoke, pw.spoke_ac), (pw.hub, pw.hub_ac)):
                    cfg = self.running(host)
                    names = {ac}
                    iface = cfg.interface(ac) if cfg else None
                    if iface is not None and iface.parent:
                        names.add(iface.parent)
                    for name in sorted(names):
                        if (host, name) in acs:
                            add("ac-down", f"{host}:{name}", f"attachment circuit {name} down on {host}", role, [host])
                            found = True
                if not found:
                    ends = ", ".join(f"{h} {v}" for h, v in live[role].items())
                    add("pw-down", role, f"no fault or drift found ({ends})", role, [pw.spoke, pw.hub])
            total = len(plan.pws)
            all_down = (
                len({role for role, ends in live.items() if any(not v.startswith("up/") for v in ends.values())})
                >= total
            )
            impact = "down" if all_down else "degraded"
            for (kind, _key), (text, roles, hosts) in sorted(causes.items()):
                role_text = " and ".join(_roles(roles))
                plural = "PWs" if len(roles) > 1 else "PW"
                label = "" if kind in ("pw-down", "device-unreachable") else " (fault)"
                findings.append(
                    Finding(
                        "fault",
                        kind,
                        service,
                        f"{service}: {role_text} {plural} down: {text}{label}; service {impact}",
                        sorted(hosts),
                        _roles(roles),
                        live=live,
                    )
                )
        return findings

    # ---------------------------------------------------------------- all
    def run(self) -> tuple[dict[str, DeviceDrift], list[Finding]]:
        devices, diffs = self.devices()
        services = sorted(
            set(self.i.plans) | {xc.service_id for c in self.ok_running.values() for xc in c.l2vpn.xconnect}
        )
        observations: list[_Obs] = []
        for service in services:
            observations.extend(self.service_observations(service))
        # attach diffs; whatever stays unexplained becomes an attribute (service) or device-level observation
        explained: set[int] = set()
        for o in observations:
            for idx, cd in enumerate(diffs):
                if o.matches(cd):
                    o.diffs.append(cd)
                    explained.add(idx)
        leftovers = [cd for idx, cd in enumerate(diffs) if idx not in explained]
        attributes, device_level = [], []
        for cd in leftovers:
            service = cd.diff.service_id or self._service_of_interface(cd.host, cd.diff.interface)
            if service:
                attributes.append(
                    _Obs(
                        "attribute", service, f"{service}: on {cd.host}: {cd.diff.message}", {cd.host}, set(), [], [cd]
                    )
                )
            else:
                device_level.append(cd)
        observations.extend(attributes)
        observations.extend(self.device_observations(device_level))

        findings: list[Finding] = []
        explained_roles: dict[str, set[str]] = {}
        for o in observations:
            classes = {cd.cls for cd in o.diffs}
            cls: FindingClass = "intent-only" if classes == {"intent-only"} else "config-drift"
            message = o.message
            if cls == "intent-only" and o.kind not in ("not-delivered", "not-removed"):
                message += " — NetBox changed, device not yet updated"
            if o.service:
                explained_roles.setdefault(o.service, set()).update(o.roles)
            plan = self.i.plans.get(o.service) if o.service else None
            findings.append(
                Finding(
                    cls,
                    o.kind,
                    o.service,
                    message,
                    sorted(o.hosts),
                    _roles(o.roles),
                    sorted({f"{cd.host}:{cd.diff.path}" for cd in o.diffs}),
                    list(o.details),
                    _live(self.i.states, o.service, plan) if o.service else {},
                )
            )
        device_drift_hosts = {
            h
            for o in observations
            if o.service is None and o.kind in ("loopback-changed", "uplink-admin-down")
            for h in o.hosts
        }
        findings.extend(self.fault_findings(explained_roles, device_drift_hosts))
        for problem in self.i.intent_problems:
            findings.append(Finding("intent-only", "intent-invalid", None, f"NetBox intent problem: {problem}", []))
        findings.sort(
            key=lambda f: (f.service or "~", {"config-drift": 0, "intent-only": 1, "fault": 2}[f.cls], f.kind)
        )
        return devices, findings

    def _service_of_interface(self, host: str, interface: str | None) -> str | None:
        if not interface:
            return None
        for cfg in (self.ok_running.get(host), self.ok_intended.get(host)):
            if cfg is None:
                continue
            for xc in cfg.l2vpn.xconnect:
                if xc.attachment_circuit == interface:
                    return xc.service_id
        return None


def analyze(inputs: DriftInputs) -> DriftReport:
    start = time.monotonic()
    devices, findings = _Analyzer(inputs).run()
    from netsim_lab.core import fault_dict

    return DriftReport(
        devices=devices,
        findings=findings,
        faults=[fault_dict(f) for f in inputs.faults],
        stopped=list(inputs.stopped),
        intent_problems=list(inputs.intent_problems),
        duration_s=time.monotonic() - start,
    )


def baseline_updates(inputs: DriftInputs, report: DriftReport) -> dict[str, DeviceConfig]:
    """Hosts where running == intent but the stored baseline says otherwise: record the new agreement."""
    out = {}
    for host, dev in report.devices.items():
        running = inputs.running.get(host)
        if dev.status != "in-sync" or not isinstance(running, DeviceConfig):
            continue
        if canonical_dict(running) != dict(inputs.baselines.get(host, {})):
            out[host] = running
    return out


__all__ = [
    "DRIFT_CLASSES",
    "ClassifiedDiff",
    "DeviceDrift",
    "DiffKind",
    "DriftInputs",
    "DriftReport",
    "Finding",
    "PwPlan",
    "ServicePlan",
    "analyze",
    "baseline_updates",
    "classify",
    "plans_from_intent",
    "plans_from_topology",
]
