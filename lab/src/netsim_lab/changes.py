"""Propose -> approve -> apply -> verify for device configuration (lab-api's write path for the bot).

1. **Propose** (:func:`plan_patch` / :func:`plan_intent`): read the running config, build the candidate (a config
   patch, or NetBox's rendered intent), validate it, diff it, and predict the drift findings after the change.
   The plan carries a ``plan_id`` = hash of (host, running config, candidate).
2. **Approve**: the caller sends the same request again with ``confirm=true`` and that ``plan_id``. If the
   running config or the candidate changed in between, the ids differ and nothing is applied (HTTP 409).
3. **Apply** (:func:`apply_plan`): one RESTCONF commit (``PUT /restconf/data``) of the whole candidate.
4. **Verify**: wait until simd runs the new config (``config_hash``) and the PW states settle on every affected
   host, make Zabbix poll those hosts now, wait until the walk is fresh and the business services stop changing,
   then report simd state, Zabbix service status/problems and the drift findings, with a verdict.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from netsim_common.canonical import canonical_dict, diff_configs
from netsim_common.device_config import DeviceConfig
from netsim_lab.configpath import Validation, apply_patch, dumps, validate
from netsim_lab.devices import config_hash
from netsim_lab.drift import Finding, analyze

ChangeKind = Literal["patch", "apply-intent"]
_host_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def host_lock(host: str) -> threading.Lock:
    with _locks_guard:
        return _host_locks.setdefault(host, threading.Lock())


class PlanError(RuntimeError):
    """The change cannot be planned (bad patch, intent missing for the host, device unreachable)."""


@dataclass
class ChangePlan:
    host: str
    kind: ChangeKind
    plan_id: str
    running_hash: str
    candidate: dict[str, Any]
    diff: list[Any]
    validation: Validation
    affected_services: list[str]
    affected_hosts: list[str]
    drift_before: list[Finding] = field(default_factory=list)
    drift_after: list[Finding] = field(default_factory=list)
    patch: list[dict[str, Any]] | None = None

    @property
    def empty(self) -> bool:
        return not self.diff

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "kind": self.kind,
            "plan_id": self.plan_id,
            "running_hash": self.running_hash,
            "candidate_hash": config_hash(self.candidate) if self.validation.valid else None,
            "no_change": self.empty,
            "diff": [change_dict(d) for d in self.diff],
            "validation": self.validation.to_dict(),
            "affected_services": self.affected_services,
            "affected_hosts": self.affected_hosts,
            "drift_before": [f.to_dict() for f in self.drift_before],
            "drift_after_predicted": [f.to_dict() for f in self.drift_after],
            "patch": self.patch,
        }


def change_dict(d: Any) -> dict[str, Any]:
    """A running -> candidate difference, worded as a change (``from`` -> ``to``)."""
    from netsim_common.canonical import DiffKind, format_value

    kind = {DiffKind.CHANGED: "changed", DiffKind.MISSING: "removed", DiffKind.UNEXPECTED: "added"}[d.kind]
    return {
        "path": d.path,
        "change": kind,
        "from": d.expected,
        "to": d.actual,
        "message": f"{d.path}: {format_value(d.expected)} -> {format_value(d.actual)}",
        "service_id": d.service_id,
        "pw_role": d.pw_role,
        "interface": d.interface,
    }


def _plan_id(host: str, kind: str, running: dict[str, Any], candidate: dict[str, Any]) -> str:
    digest = hashlib.sha256(f"{host}|{kind}|{dumps(running)}|{dumps(candidate)}".encode()).hexdigest()
    return digest[:16]


def _affected(
    lab: Any, host: str, running: DeviceConfig, candidate: dict[str, Any], plans: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Services touched by the change (xconnects, their ACs or the ACs' parents) and every host they end on."""
    try:
        cand = DeviceConfig.from_restconf(candidate, partial=True)
    except Exception:
        cand = None
    services: set[str] = set()
    for d in diff_configs(running, candidate):
        if d.service_id:
            services.add(d.service_id)
        if d.interface:
            for cfg in (running, cand):
                if cfg is None:
                    continue
                for xc in cfg.l2vpn.xconnect:
                    ac = cfg.interface(xc.attachment_circuit)
                    if xc.attachment_circuit == d.interface or (ac is not None and ac.parent == d.interface):
                        services.add(xc.service_id)
        if d.path.startswith("system/") or (
            d.leaf == "enabled" and d.interface and running.core_uplink() and running.core_uplink().name == d.interface
        ):
            services.update(xc.service_id for xc in running.l2vpn.xconnect)
    hosts = {host}
    for s in services:
        plan = plans.get(s)
        if plan is not None:
            hosts.update(plan.hosts)
        for cfg in (running, cand):
            xc = cfg.xconnect(s) if cfg is not None else None
            for pw in xc.pseudowire if xc else []:
                peer = lab_host_by_loopback(lab, pw.peer_ipv4)
                if peer:
                    hosts.add(peer)
    return sorted(services), sorted(hosts)


def lab_host_by_loopback(lab: Any, address: str) -> str | None:
    return next((d.hostname for d in lab.settings.devices.values() if d.loopback == address), None)


def _relevant(findings: list[Finding], host: str, services: list[str]) -> list[Finding]:
    return [f for f in findings if (f.service in services) or (host in f.hosts and f.cls != "fault")]


def _plan(
    lab: Any, host: str, kind: ChangeKind, build: Callable[[dict[str, Any], Any], dict[str, Any]], patch: Any = None
) -> ChangePlan:
    lab.settings.device(host)
    device = lab.devices[host]
    running_cfg = device.running()
    running = canonical_dict(running_cfg)
    intent = lab.intent()
    candidate = build(running, intent)
    validation = validate(candidate)
    candidate_c = canonical_dict(validation.config) if validation.valid else candidate
    inputs = lab.drift_inputs(intent=intent)
    before = analyze(inputs)
    after: list[Finding] = []
    if validation.valid:
        # Predict drift only: fault findings come from the *current* live state, which the change will alter.
        inputs.running = {**inputs.running, host: validation.config}
        after = [f for f in analyze(inputs).findings if f.is_drift]
    from netsim_lab.drift import plans_from_intent

    plans = plans_from_intent(intent)
    services, hosts = _affected(lab, host, running_cfg, candidate_c, plans)
    return ChangePlan(
        host=host,
        kind=kind,
        plan_id=_plan_id(host, kind, running, candidate_c),
        running_hash=config_hash(running_cfg),
        candidate=candidate_c,
        diff=diff_configs(running_cfg, candidate_c) if validation.valid else [],
        validation=validation,
        affected_services=services,
        affected_hosts=hosts,
        drift_before=_relevant(before.findings, host, services),
        drift_after=_relevant(after, host, services),
        patch=patch,
    )


def plan_patch(lab: Any, host: str, patch: Any) -> ChangePlan:
    from netsim_lab.configpath import normalize_patch

    ops = normalize_patch(patch)
    return _plan(lab, host, "patch", lambda running, _intent: apply_patch(running, ops), ops)


def plan_intent(lab: Any, host: str) -> ChangePlan:
    from netsim_netbox.intent import IntentError, render_device

    def build(_running: dict[str, Any], intent: Any) -> dict[str, Any]:
        try:
            return canonical_dict(render_device(host, intent))
        except IntentError as exc:
            raise PlanError(f"NetBox intent of {host} cannot be rendered: {exc}") from exc

    return _plan(lab, host, "apply-intent", build)


# ---------------------------------------------------------------------------------------------- apply + verify
@dataclass
class ApplyResult:
    plan: ChangePlan
    applied: bool
    verification: dict[str, Any]
    elapsed_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.to_dict(),
            "applied": self.applied,
            "verification": self.verification,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def _pw_states(states: dict[str, Any], services: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    out: dict[str, dict[str, dict[str, str]]] = {}
    for host, st in sorted(states.items()):
        if isinstance(st, Exception):
            continue
        for p in st.get("pseudowires", []):
            if p["service_id"] in services:
                out.setdefault(p["service_id"], {}).setdefault(p["role"], {})[host] = f"{p['oper']}/{p['reason']}"
    return out


def wait_simd(
    lab: Any, host: str, want_hash: str, hosts: list[str], timeout: float = 15.0, stable_s: float = 1.5
) -> dict[str, Any]:
    """Wait until ``host`` runs ``want_hash`` and the PW states of ``hosts`` stop changing."""
    deadline = time.monotonic() + timeout
    applied = False
    while time.monotonic() < deadline:
        st = lab.devices[host].state()
        if st.get("config_hash") == want_hash:
            applied = True
            break
        time.sleep(0.1)
    last, since = None, time.monotonic()
    states: dict[str, Any] = {}
    while time.monotonic() < deadline:
        states = dict(lab.devices.states(hosts))
        snap = {
            h: (
                None
                if isinstance(s, Exception)
                else sorted((p["service_id"], p["role"], p["oper"], p["reason"]) for p in s.get("pseudowires", []))
            )
            for h, s in states.items()
        }
        if snap != last:
            last, since = snap, time.monotonic()
        elif time.monotonic() - since >= stable_s:
            break
        time.sleep(0.25)
    return {"config_applied": applied, "states": states}


def wait_zabbix(
    lab: Any, hosts: list[str], services: list[str], t_apply: float, timeout: float = 40.0, stable_s: float = 6.0
) -> dict[str, Any]:
    """Poll the hosts now; wait for fresh walks, then for the business services to stay unchanged ``stable_s``."""
    z = lab.zabbix()
    z.poll_now(hosts)
    deadline = time.monotonic() + timeout
    fresh: dict[str, bool] = {}
    while time.monotonic() < deadline:
        clocks = z.walk_clocks(hosts)
        fresh = {h: clocks.get(h, 0.0) >= t_apply for h in hosts}
        if all(fresh.values()):
            break
        time.sleep(0.5)
    last, since, statuses = None, time.monotonic(), {}
    while time.monotonic() < deadline:
        statuses = {s: v for s, v in z.service_statuses().items() if s in services}
        problems = [p for p in z.problems(scopes=("l2vpn-pw", "l2vpn-unexpected")) if p["service"] in services]
        snap = (statuses, sorted(p["eventid"] for p in problems))
        if snap != last:
            last, since = snap, time.monotonic()
        elif time.monotonic() - since >= stable_s:
            break
        time.sleep(0.5)
    problems = [p for p in z.problems(scopes=("l2vpn-pw", "l2vpn-unexpected")) if p["service"] in services]
    return {
        "walk_refreshed": fresh,
        "services": statuses,
        "problems": [{k: p[k] for k in ("severity", "name", "host", "service", "pw_role", "check")} for p in problems],
        "settled": time.monotonic() < deadline,
    }


def apply_plan(lab: Any, plan: ChangePlan, *, verify: bool = True, timeout: float = 60.0) -> ApplyResult:
    """Commit the plan's candidate (one RESTCONF PUT) and verify it (simd, Zabbix, drift)."""
    start = time.monotonic()
    if not plan.validation.valid:
        raise PlanError("the candidate config is invalid: " + "; ".join(plan.validation.errors))
    if plan.empty:
        return ApplyResult(plan, False, {"verdict": "no-change"}, 0.0)
    t_apply = time.time()
    with host_lock(plan.host):
        lab.devices[plan.host].replace(plan.candidate)
    if plan.kind == "apply-intent":
        lab.record_agreement(plan.host, DeviceConfig.from_restconf(plan.candidate))
    if not verify:
        return ApplyResult(plan, True, {"verdict": "not-verified"}, time.monotonic() - start)
    simd = wait_simd(lab, plan.host, config_hash(plan.candidate), plan.affected_hosts, timeout=min(15.0, timeout))
    pw_live = _pw_states(simd["states"], plan.affected_services)
    try:
        zabbix = wait_zabbix(
            lab,
            plan.affected_hosts,
            plan.affected_services,
            t_apply,
            timeout=max(10.0, timeout - (time.monotonic() - start)),
        )
    except Exception as exc:
        zabbix = {"error": f"{type(exc).__name__}: {exc}"}
    report = lab.drift()
    drift_after = _relevant(report.findings, plan.host, plan.affected_services)
    problems = []
    if not simd["config_applied"]:
        problems.append("simd did not report the new config hash")
    for service in plan.affected_services:
        for role, ends in pw_live.get(service, {}).items():
            for host, state in ends.items():
                if not state.startswith("up/"):
                    problems.append(f"{service} {role} on {host}: {state}")
    if "error" in zabbix:
        problems.append(f"Zabbix: {zabbix['error']}")
    else:
        if not all(zabbix["walk_refreshed"].values()):
            problems.append("Zabbix did not re-poll every affected host in time")
        for service, status in zabbix["services"].items():
            if status != "OK":
                problems.append(f"Zabbix {service}: {status}")
    for f in drift_after:
        if f.is_drift:
            problems.append(f"drift: {f.message}")
    verdict = (
        "ok" if not problems else "unverified" if ("error" in zabbix or not simd["config_applied"]) else "degraded"
    )
    verification = {
        "verdict": verdict,
        "problems": problems,
        "simd": {"config_applied": simd["config_applied"], "pseudowires": pw_live},
        "zabbix": zabbix,
        "drift_after": [f.to_dict() for f in drift_after],
    }
    return ApplyResult(plan, True, verification, time.monotonic() - start)
