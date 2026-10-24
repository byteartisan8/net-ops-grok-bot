"""``make reset`` / ``labctl reset`` / lab-api ``POST /reset``: back to the seeded lab, then wait until green.

1. clear every fault and cut key in ``core``;
2. start every stopped PE container and wait until simd answers;
3. NetBox: if its intent differs from the topology (a scenario added/removed something), ``netbox-seed --prune``;
4. every device whose running config differs from golden gets golden back (one ``PUT /restconf/data`` each);
5. drift baselines back to golden;
6. the Clixon controller (10-clixon-controller) takes every router's config as its copy again (``pull``, or
   ``connection open`` for a router it is not connected to), through lab-api, which holds the controller's
   credentials; skipped with a note when lab-api or the controller is down;
7. ``zabbix-sync`` if NetBox changed or Zabbix's BSM/spec differs (through lab-api when it runs, so syncs never
   overlap; locally otherwise);
8. Zabbix polls every PE now; wait until every PW is up in simd and Zabbix shows every service OK with no
   problem of severity Warning or higher on the lab hosts.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from netsim_common.canonical import canonical_dict


class ResetError(RuntimeError):
    pass


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)


def simd_green(lab: Any) -> list[str]:
    """Why simd is not green (empty = every device answers and every PW is up/ok)."""
    out = []
    for host, st in lab.devices.states().items():
        if isinstance(st, Exception):
            out.append(f"{host}: {st}")
            continue
        out += [
            f"{host} {p['service_id']} {p['role']}: {p['oper']}/{p['reason']}"
            for p in st["pseudowires"]
            if p["oper"] != "up"
        ]
    return out


def zabbix_green(lab: Any) -> list[str]:
    """Why Zabbix is not green (empty = every managed service OK, no problem >= Warning on the lab hosts)."""
    z = lab.zabbix()
    services = z.services()
    out = [f"service {s}: {v.get('status')}" for s, v in services["services"].items() if v.get("status") != "OK"]
    if services["root"] not in (None, "OK"):
        out.append(f"root: {services['root']}")
    out += [f"[{p['severity']}] {p['name']}" for p in z.problems(min_severity=2)]
    return out


def wait_green(lab: Any, timeout: float = 150.0, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    start = time.monotonic()
    deadline = start + timeout
    why: list[str] = []
    while time.monotonic() < deadline:
        why = simd_green(lab)
        if not why:
            break
        time.sleep(0.5)
    simd_s = time.monotonic() - start
    if why:
        return {"green": False, "simd_s": None, "zabbix_s": None, "why": why}
    # Zabbix must have polled every host *after* the lab became green in simd, otherwise "green" could be the state
    # from before the reset (a fault that Zabbix has not seen yet, or seen but not yet cleared).
    t_poll, polled, fresh = time.time(), False, False
    while time.monotonic() < deadline:
        try:
            z = lab.zabbix()
            if not polled:
                z.poll_now()
                polled = True
            if not fresh:
                clocks = z.walk_clocks()
                stale = [h for h in lab.hosts if clocks.get(h, 0.0) < t_poll]
                fresh = not stale
                why = [f"Zabbix has not polled {', '.join(stale)} since the reset"] if stale else []
            if fresh:
                why = zabbix_green(lab)
        except Exception as exc:
            why = [f"Zabbix: {type(exc).__name__}: {exc}"]
        if fresh and not why:
            break
        time.sleep(0.5 if not fresh else 1.0)
    green = not why
    if not green:
        _log(log, f"reset: not green after {timeout:.0f} s: {'; '.join(why[:8])}")
    return {
        "green": green,
        "simd_s": round(simd_s, 2),
        "zabbix_s": round(time.monotonic() - start, 2) if green else None,
        "why": why,
    }


def wait_devices_ready(lab: Any, timeout: float = 120.0) -> None:
    """Every PE ready: container healthy (its healthcheck probes RESTCONF, SNMP and simd) and config readable.

    simd can answer before Clixon's RESTCONF does after a (re)start, so neither alone is enough.
    """
    deadline = time.monotonic() + timeout
    why: list[str] = []
    while time.monotonic() < deadline:
        why = []
        for host in lab.hosts:
            try:
                info = lab.docker.inspect(host)
            except Exception:  # no Docker access (e.g. a remote client): rely on the device checks below
                info = None
            if info is not None and (not info["running"] or info["health"] not in (None, "", "healthy")):
                why.append(f"{host}: container {info['status']}/{info['health'] or '-'}")
        if not why:
            for host, cfg in lab.devices.running().items():
                if isinstance(cfg, Exception):
                    why.append(f"{host}: {cfg}")
            for host, st in lab.devices.states().items():
                if isinstance(st, Exception):
                    why.append(f"{host}: {st}")
        if not why:
            return
        time.sleep(0.5)
    raise ResetError("devices not ready: " + "; ".join(why))


def request_sync(lab: Any, reason: str, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run zabbix-sync through lab-api when it is up (serialized with webhook syncs), else locally."""
    from netsim_lab.labapi_client import LabApiClient, LabApiError

    with LabApiClient(lab.settings.lab_api_url, lab.settings.lab_api_token, "reset") as api:
        if api.healthy() and lab.settings.lab_api_token:
            try:
                result = api.sync(wait=True, reason=reason)
                return {"via": "lab-api", **(result.get("last") or {})}
            except LabApiError as exc:
                _log(log, f"reset: lab-api sync failed ({exc}); running zabbix-sync locally")
    from netsim_zabbix.sync import sync

    result = sync()
    return {"via": "local", "summary": result.plan.summary(), "seconds": round(result.duration, 1)}


def request_controller_resync(lab: Any, hosts: list[str], log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The controller's resync through lab-api (``POST /controller/resync``); a note when that is not possible."""
    from netsim_lab.labapi_client import LabApiClient, LabApiError

    with LabApiClient(lab.settings.lab_api_url, lab.settings.lab_api_token, "reset") as api:
        if not (lab.settings.lab_api_token and api.healthy()):
            return {"skipped": "lab-api is not running: the controller keeps its copy until the next resync"}
        try:
            return api.request("POST", "/controller/resync", write=True, json={"hosts": hosts})
        except LabApiError as exc:
            _log(log, f"reset: controller resync failed ({exc})")
            return {"error": str(exc)}


def reset(
    lab: Any,
    *,
    wait: bool = True,
    timeout: float = 150.0,
    log: Callable[[str], None] | None = print,
    sync: Callable[..., dict[str, Any]] | None = None,
    resync: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """See the module docstring. ``sync(lab, reason, log)`` runs zabbix-sync (default: :func:`request_sync`);
    ``resync(lab, hosts, log)`` resyncs the controller (default: :func:`request_controller_resync`)."""
    from netsim_lab import netbox_ops

    start = time.monotonic()
    summary: dict[str, Any] = {}
    cleared = lab.core.clear_faults()
    summary["faults_cleared"] = cleared
    _log(log, f"reset: cleared {len(cleared)} fault/cut key(s)")

    started = []
    for host in lab.stopped():
        lab.docker.start(host)
        started.append(host)
    if started:
        _log(log, f"reset: started {', '.join(started)}")
    summary["devices_started"] = started
    wait_devices_ready(lab, timeout=120)

    netbox_changed = False
    try:
        lab.forget_intent()
        intent = lab.intent()
        from netsim_lab.drift import plans_from_intent, plans_from_topology

        differs = netbox_ops.intent_equals_golden(intent, lab.golden())
        extra = sorted(set(plans_from_intent(intent)) ^ set(plans_from_topology(lab.topology)))
        if differs or extra or intent.problems:
            what = ", ".join(differs + extra) or "intent problems"
            _log(log, f"reset: NetBox differs from the topology ({what}); re-seeding with --prune")
            summary["netbox"] = netbox_ops.restore()
            lab.forget_intent()
            netbox_changed = True
        else:
            summary["netbox"] = {"changes": [], "summary": "matches the topology"}
    except Exception as exc:
        summary["netbox"] = {"error": f"{type(exc).__name__}: {exc}"}
        _log(log, f"reset: NetBox check failed: {exc}")

    restored = []
    golden = lab.golden()
    running = lab.devices.running()
    for host in lab.hosts:
        cfg = running.get(host)
        if isinstance(cfg, Exception):
            raise ResetError(f"cannot read {host}'s config: {cfg}")
        if canonical_dict(cfg) != canonical_dict(golden[host]):
            lab.devices[host].replace(golden[host])
            restored.append(host)
    summary["devices_restored"] = restored
    if restored:
        _log(log, f"reset: golden config restored on {', '.join(restored)}")
    summary["baselines_cleared"] = lab.core.clear_baselines()
    summary["controller"] = (resync or request_controller_resync)(lab, list(lab.hosts), log)
    states = (summary["controller"].get("hosts") or {}).values()
    if states:
        _log(log, f"reset: controller resynced, {sum(h['state'] == 'OPEN' for h in states)}/{len(states)} connected")

    try:
        from netsim_zabbix.sync import sync as zabbix_sync

        pending_sync = netbox_changed or bool(zabbix_sync(dry_run=True).plan.changes)
        if pending_sync:
            _log(log, "reset: running zabbix-sync")
            summary["zabbix_sync"] = (sync or request_sync)(lab, "reset", log)
    except Exception as exc:
        summary["zabbix_sync"] = {"error": f"{type(exc).__name__}: {exc}"}
        _log(log, f"reset: zabbix-sync failed: {exc}")

    if wait:
        summary["wait"] = wait_green(lab, timeout=timeout, log=log)
        summary["green"] = summary["wait"]["green"]
    summary["elapsed_s"] = round(time.monotonic() - start, 2)
    _log(log, f"reset: done in {summary['elapsed_s']} s" + (" (green)" if summary.get("green") else ""))
    return summary
