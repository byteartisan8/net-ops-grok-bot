"""lab-api: the NetSim lab's tool surface for the (future) Grok bot. FastAPI, OpenAPI in ``lab/openapi.json``.

Reads need no token. Writes need ``X-Lab-Token: $LAB_API_TOKEN`` and are recorded in the audit log (JSONL on
lab-api's volume) with ``X-Lab-Actor`` as "who". Device changes always go propose -> approve -> apply -> verify:
a request without ``confirm=true`` is a dry run that returns the diff, the validation and a ``plan_id``; the same
request with ``confirm=true`` and that ``plan_id`` applies it and returns the verification from simd and Zabbix.
NetBox's webhook (``POST /hooks/netbox``, HMAC-SHA512 signed) triggers a debounced ``zabbix-sync``.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from netsim_lab import __version__
from netsim_lab.api import models as M
from netsim_lab.audit import AuditLog
from netsim_lab.lab import Lab
from netsim_lab.settings import LabSettings, SettingsError, load_settings
from netsim_lab.syncer import SyncRunner

TAGS = [
    {"name": "intent", "description": "NetBox intent: topology, services, what each device should run."},
    {"name": "live", "description": "Live device state from simd (PW oper status and reason per end) and events."},
    {"name": "monitoring", "description": "Zabbix: business service status, problems and their correlation."},
    {"name": "analysis", "description": "Drift (NetBox vs devices, per service) and the impact of losing a host."},
    {"name": "faults", "description": "Physical faults and device power (write: token)."},
    {
        "name": "changes",
        "description": "Device config changes: dry run -> confirm with plan_id -> verified (write: token).",
    },
    {"name": "scenarios", "description": "Declarative lab scenarios and reset (write: token)."},
    {"name": "sync", "description": "NetBox -> Zabbix sync: the NetBox webhook and manual triggers."},
    {"name": "audit", "description": "Every write lab-api performed: who, when, what, diff, result."},
]

HOST = Path(description="Device name: hub-a, hub-b, spoke-1 .. spoke-4", examples=["spoke-1"])


class Actor:
    def __init__(self, name: str, client: str | None) -> None:
        self.name = name
        self.client = client


class AppState:
    """Shared by every request: the Lab, the audit log, the webhook journal and the sync runner."""

    def __init__(
        self,
        settings: LabSettings | None = None,
        lab: Lab | None = None,
        sync_fn: Callable[[], dict[str, Any]] | None = None,
        settings_loader: Callable[[], LabSettings] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self._settings_loader = settings_loader
        self.started = time.time()
        self._lab = lab
        self._lab_lock = threading.Lock()
        self.audit = AuditLog(self.settings.data_dir / "audit.jsonl")
        self.hook_log = self.settings.data_dir / "netbox-webhooks.jsonl"
        self.hooks: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        self.syncer = SyncRunner(
            sync_fn or self._run_sync,
            debounce_s=float(os.environ.get("LAB_SYNC_DEBOUNCE_S", "2.0")),
            on_done=self._sync_done,
        )

    @property
    def lab(self) -> Lab:
        with self._lab_lock:
            if self._lab is None:
                self._lab = Lab(self.settings)
            return self._lab

    def current(self) -> LabSettings:
        """Settings re-read from the environment and ``.env`` (tokens written after start are seen)."""
        if self._settings_loader is not None:
            return self._settings_loader()
        return load_settings(topology=self.lab.topology)

    def _run_sync(self) -> dict[str, Any]:
        from netsim_zabbix.sync import sync

        result = sync()
        self.lab.forget_intent()
        return {
            "summary": result.plan.summary(),
            "changes": [vars(c) for c in result.plan.changes],
            "warnings": result.plan.warnings,
            "changed_hosts": result.changed_hosts,
            "refreshed": result.refreshed,
            "source": result.source,
            "seconds": round(result.duration, 2),
        }

    def _sync_done(self, result: dict[str, Any]) -> None:
        self.audit.write(
            actor="zabbix-sync",
            client=None,
            action="sync.run",
            request={"reasons": result.get("reasons")},
            result={k: result.get(k) for k in ("ok", "summary", "warnings", "error", "seconds")},
            ok=bool(result.get("ok")),
        )


def get_state(request: Request) -> AppState:
    """The app's shared state (created on first use, so importing the app has no side effects)."""
    if getattr(request.app.state, "lab_state", None) is None:
        request.app.state.lab_state = AppState()
    return request.app.state.lab_state


StateDep = Annotated[AppState, Depends(get_state)]


def writer(
    request: Request,
    st: StateDep,
    x_lab_token: Annotated[str | None, Header(description="LAB_API_TOKEN (required for every write)")] = None,
    x_lab_actor: Annotated[str | None, Header(description="Who is acting, e.g. grok-bot (audit log)")] = None,
) -> Actor:
    """Every write needs ``X-Lab-Token``; ``X-Lab-Actor`` names the caller in the audit log."""
    token = st.current().lab_api_token
    if not token:
        raise HTTPException(503, "LAB_API_TOKEN is not configured on lab-api (run `make env`)")
    if not x_lab_token or not hmac.compare_digest(x_lab_token.encode(), token.encode()):
        raise HTTPException(401, "missing or wrong X-Lab-Token")
    return Actor(x_lab_actor or "unknown", request.client.host if request.client else None)


WriterDep = Annotated[Actor, Depends(writer)]


# ---------------------------------------------------------------------------------------------- app factory
def create_app(state: AppState | None = None) -> FastAPI:
    app = FastAPI(
        title="NetSim lab-api",
        version=__version__,
        description=__doc__,
        openapi_tags=TAGS,
    )
    app.state.lab_state = state

    def audited(
        st: AppState,
        actor: Actor,
        action: str,
        target: str | None,
        request: Any,
        fn: Callable[[], Any],
        diff: Callable[[Any], Any] | None = None,
        summary: Callable[[Any], Any] | None = None,
    ) -> Any:
        try:
            result = fn()
        except HTTPException as exc:
            st.audit.write(
                actor=actor.name,
                client=actor.client,
                action=action,
                target=target,
                request=request,
                result=exc.detail,
                status=exc.status_code,
                ok=False,
            )
            raise
        except Exception as exc:
            st.audit.write(
                actor=actor.name,
                client=actor.client,
                action=action,
                target=target,
                request=request,
                result=f"{type(exc).__name__}: {exc}",
                status=500,
                ok=False,
            )
            raise
        st.audit.write(
            actor=actor.name,
            client=actor.client,
            action=action,
            target=target,
            request=request,
            diff=diff(result) if diff else None,
            result=summary(result) if summary else result,
            status=200,
        )
        return result

    # ------------------------------------------------------------------ errors
    from netsim_lab.changes import PlanError
    from netsim_lab.configpath import PathError
    from netsim_lab.core import CoreError
    from netsim_lab.devices import DeviceError
    from netsim_lab.docker_api import DockerError
    from netsim_lab.scenarios import ScenarioError

    def handler(status: int) -> Callable[[Request, Exception], JSONResponse]:
        def h(_req: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

        return h

    app.add_exception_handler(SettingsError, handler(404))
    app.add_exception_handler(PathError, handler(422))
    app.add_exception_handler(PlanError, handler(409))
    app.add_exception_handler(ScenarioError, handler(404))
    app.add_exception_handler(DeviceError, handler(502))
    app.add_exception_handler(CoreError, handler(503))
    app.add_exception_handler(DockerError, handler(502))
    try:
        from netsim_netbox.client import NetBoxError
        from netsim_netbox.intent import IntentError
        from netsim_netbox.settings import SettingsError as NetBoxSettingsError
        from netsim_zabbix.api import ZabbixApiError
        from netsim_zabbix.settings import SettingsError as ZabbixSettingsError

        app.add_exception_handler(IntentError, handler(409))
        app.add_exception_handler(NetBoxError, handler(502))
        app.add_exception_handler(NetBoxSettingsError, handler(503))
        app.add_exception_handler(ZabbixApiError, handler(502))
        app.add_exception_handler(ZabbixSettingsError, handler(503))
    except ImportError:  # pragma: no cover - always installed in the workspace and the image
        pass

    # ------------------------------------------------------------------ health
    @app.get("/health", response_model=M.Health, tags=["live"], summary="lab-api is up")
    def health(st: StateDep) -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "uptime_s": round(time.time() - st.started, 1)}

    # ------------------------------------------------------------------ intent
    @app.get("/topology", response_model=M.Topology, tags=["intent"], summary="Devices, services, spoke-hub links")
    def topology(st: StateDep) -> dict[str, Any]:
        """The lab as NetBox describes it (falls back to contract/topology.yaml if NetBox is down), with each
        device's container state."""
        from netsim_lab.drift import plans_from_intent, plans_from_topology

        lab = st.lab
        errors: dict[str, str] = {}
        customers: dict[str, str | None] = {}
        loopbacks = {d.hostname: d.loopback for d in lab.topology.devices}
        try:
            intent = lab.intent(max_age=2.0)
            plans, source = plans_from_intent(intent), "netbox"
            customers = {c.name: (c.sla_tier.value if c.sla_tier else None) for c in intent.customers.values()}
            loopbacks.update({n: d.loopback for n, d in intent.devices.items() if d.loopback})
        except Exception as exc:
            errors["netbox"] = f"{type(exc).__name__}: {exc}"
            plans, source = plans_from_topology(lab.topology), "topology"
            customers = {c.name: c.sla_tier.value for c in lab.topology.customers}
        devices = []
        for host, ep in lab.settings.devices.items():
            try:
                running: bool | None = lab.docker.running(host)
            except Exception:
                running = None
            devices.append(
                {
                    "hostname": host,
                    "role": ep.role,
                    "site": ep.site,
                    "mgmt_ip": ep.mgmt_ip,
                    "loopback": loopbacks.get(host, ep.loopback),
                    "running": running,
                }
            )
        links: dict[tuple[str, str], list[str]] = {}
        services = []
        for service, plan in plans.items():
            services.append(
                {
                    "service": service,
                    "customer": plan.customer,
                    "sla_tier": customers.get(plan.customer or ""),
                    "spoke": plan.spoke,
                    "pseudowires": [
                        {
                            "role": r,
                            "vc_id": p.vc_id,
                            "spoke": p.spoke,
                            "spoke_ac": p.spoke_ac,
                            "hub": p.hub,
                            "hub_ac": p.hub_ac,
                        }
                        for r, p in plan.pws.items()
                    ],
                }
            )
            for role, pw in plan.pws.items():
                links.setdefault((pw.spoke, pw.hub), []).append(f"{service} {role} vc {pw.vc_id}")
        return {
            "source": source,
            "devices": devices,
            "services": services,
            "links": [{"spoke": s, "hub": h, "pseudowires": pws} for (s, h), pws in sorted(links.items())],
            "customers": customers,
            "errors": errors,
        }

    @app.get(
        "/services",
        response_model=M.Services,
        tags=["intent", "live", "monitoring"],
        summary="Every service: intent, live state, Zabbix status",
    )
    def services(st: StateDep) -> dict[str, Any]:
        """One row per service with both PWs: NetBox intent (hubs, VC IDs), simd oper/reason on both ends of each PW,
        and the Zabbix business service + leaf status."""
        from netsim_lab.status import collect

        return collect(st.lab, intent_max_age=2.0).to_dict()

    @app.get(
        "/services/{service_id}",
        response_model=M.ServiceDetail,
        tags=["intent", "live", "monitoring"],
        summary="One service in every system",
    )
    def service(
        st: StateDep, service_id: Annotated[str, Path(description="Service ID", examples=["SVC-0002"])]
    ) -> dict[str, Any]:
        """Intent (NetBox, with URLs), live state of both PWs, the Zabbix service with its open problems and their
        correlation groups, and the drift/fault findings for the service."""
        from netsim_netbox.intent import IntentError, service_detail

        from netsim_lab.correlate import correlate
        from netsim_lab.status import collect

        lab = st.lab
        try:
            intent = service_detail(service_id, lab.intent())
        except IntentError:
            intent = None
        snapshot = collect(lab, intent_max_age=0.0)
        view = snapshot.service(service_id)
        zabbix: dict[str, Any] = {}
        try:
            z = lab.zabbix()
            zsvc = z.services()["services"].get(service_id)
            problems = z.problems(service=service_id)
            zabbix = {"service": zsvc, "problems": problems, "groups": correlate(problems)}
        except Exception as exc:
            zabbix = {"error": f"{type(exc).__name__}: {exc}"}
        if intent is None and view is None and not zabbix.get("service"):
            raise HTTPException(404, f"no service {service_id} in NetBox, on the devices or in Zabbix")
        drift = [f.to_dict() for f in lab.drift().for_service(service_id)]
        return {
            "service": service_id,
            "intent": intent,
            "live": view.to_dict() if view else None,
            "zabbix": zabbix,
            "drift": drift,
        }

    @app.get("/devices", tags=["live"], summary="Every device: reachability, uplink, faults, PW counts")
    def devices(st: StateDep) -> dict[str, Any]:
        from netsim_lab.status import device_summary

        lab = st.lab
        return device_summary(dict(lab.devices.states()), lab.stopped())

    @app.get("/devices/{host}/running", tags=["live"], summary="Running config (RESTCONF JSON, canonical)")
    def device_running(st: StateDep, host: Annotated[str, HOST]) -> dict[str, Any]:
        from netsim_common.canonical import canonical_dict
        from netsim_lab.devices import config_hash

        lab = st.lab
        lab.settings.device(host)
        config = lab.devices[host].running()
        return {"host": host, "config_hash": config_hash(config), "config": canonical_dict(config)}

    @app.get("/devices/{host}/state", tags=["live"], summary="simd state: PWs with oper, reason, rule, remote end")
    def device_state(st: StateDep, host: Annotated[str, HOST]) -> dict[str, Any]:
        """simd's ``/state`` (schema in devices/simd/openapi.json): uplink, faults, per PW: index, peer, VC ID,
        local MTU, admin, oper, reason, rule (contract section 7) and what the remote end advertises."""
        st.lab.settings.device(host)
        return st.lab.devices[host].state()

    @app.get(
        "/devices/{host}/intent",
        tags=["intent", "analysis"],
        summary="What NetBox says the device should run, and the diff",
    )
    def device_intent(st: StateDep, host: Annotated[str, HOST]) -> dict[str, Any]:
        """NetBox-rendered config of the device, the differences from its running config (each classified
        config-drift or intent-only against the device's baseline) and the findings that involve the device."""
        from netsim_common.canonical import canonical_dict
        from netsim_lab.drift import analyze

        lab = st.lab
        lab.settings.device(host)
        inputs = lab.drift_inputs()
        report = analyze(inputs)
        intended = inputs.intended.get(host)
        return {
            "host": host,
            "intent": canonical_dict(intended) if not isinstance(intended, Exception) else None,
            "intent_error": str(intended) if isinstance(intended, Exception) else None,
            "device": report.devices[host].to_dict(),
            "findings": [f.to_dict() for f in report.findings if host in f.hosts],
        }

    # ------------------------------------------------------------------ analysis
    @app.get(
        "/drift", response_model=M.DriftReport, tags=["analysis"], summary="NetBox vs devices, with semantic findings"
    )
    def drift(st: StateDep) -> dict[str, Any]:
        """Per device the structured diff (intent vs running config), and per service a finding that says what
        it means, classified config-drift / intent-only / fault. ``drift`` is true when any config-drift or
        intent-only finding exists."""
        return st.lab.drift().to_dict()

    @app.get(
        "/impact/{host}", response_model=M.Impact, tags=["analysis"], summary="What losing this host would do, now"
    )
    def impact_(st: StateDep, host: Annotated[str, HOST]) -> dict[str, Any]:
        """NetBox's blast radius (by design) combined with the live state of the PWs that would survive: a
        service whose backup is already broken goes down, not degraded."""
        from netsim_lab.impact import impact

        return impact(st.lab, host)

    # ------------------------------------------------------------------ monitoring
    @app.get(
        "/zabbix/problems",
        response_model=M.Problems,
        tags=["monitoring"],
        summary="Open problems on the lab hosts, correlated",
    )
    def zabbix_problems(
        st: StateDep,
        service: Annotated[str | None, Query(description="Only problems tagged with this service")] = None,
        scope: Annotated[
            str | None,
            Query(description="Only this scope: l2vpn-pw, l2vpn-unexpected, l2vpn-trap, interface, availability"),
        ] = None,
        window_s: Annotated[float, Query(ge=1, le=3600, description="Correlation window for start times")] = 120.0,
    ) -> dict[str, Any]:
        from netsim_lab.correlate import correlate

        problems = st.lab.zabbix().problems(service=service, scopes=(scope,) if scope else None)
        return {"problems": problems, "groups": correlate(problems, window_s)}

    @app.get("/events", tags=["live"], summary="simd events of every device, merged and time-ordered")
    def events(
        st: StateDep,
        since: Annotated[
            str | None, Query(description="Unix seconds or ISO 8601; omit for each device's whole buffer")
        ] = None,
        limit: Annotated[int | None, Query(ge=0, le=5000, description="Return at most this many (newest)")] = 500,
        host: Annotated[list[str] | None, Query(description="Only these devices")] = None,
    ) -> dict[str, Any]:
        """Config changes (who, RFC 6470 edits, diff), PW state transitions with reasons, traps sent, boots."""
        from netsim_lab.events import merged_events

        lab = st.lab
        for h in host or []:
            lab.settings.device(h)
        return merged_events(lab, since=since, limit=limit, hosts=host)

    # ------------------------------------------------------------------ faults
    @app.get("/faults", response_model=M.Faults, tags=["faults"], summary="Active faults and powered-off devices")
    def faults(st: StateDep) -> dict[str, Any]:
        from netsim_lab.core import fault_dict

        lab = st.lab
        return {"faults": [fault_dict(f) for f in lab.core.faults()], "stopped": lab.stopped()}

    @app.post("/faults", response_model=M.FaultResult, tags=["faults"], summary="Inject or restore a physical fault")
    def inject_fault(st: StateDep, actor: WriterDep, body: M.FaultRequest) -> dict[str, Any]:
        """Physical faults never touch config (contract section 7): uplink/ac/cut set keys on the core bus (simd
        reacts in ~0.1 s); device stops or starts the container (power off/on)."""
        from netsim_lab.core import fault_dict

        lab = st.lab

        def run() -> dict[str, Any]:
            lab.settings.device(body.host)
            on = body.action == "inject"
            if body.kind == "uplink":
                lab.core.set_uplink(body.host, on)
                done = f"uplink {'down' if on else 'restored'} on {body.host}"
            elif body.kind == "ac":
                if not body.interface:
                    raise HTTPException(422, "kind=ac needs 'interface'")
                lab.core.set_ac(body.host, body.interface, on)
                done = f"attachment circuit {body.interface} {'down' if on else 'restored'} on {body.host}"
            elif body.kind == "cut":
                if not body.peer:
                    raise HTTPException(422, "kind=cut needs 'peer'")
                lab.settings.device(body.peer)
                lab.core.set_cut(body.host, body.peer, on)
                done = f"core path {body.host} <-> {body.peer} {'cut' if on else 'restored'}"
            else:
                changed = lab.docker.stop(body.host) if on else lab.docker.start(body.host)
                done = f"{body.host} {'powered off' if on else 'powered on'}" + ("" if changed else " (already)")
            return {"done": done, "faults": [fault_dict(f) for f in lab.core.faults()], "stopped": lab.stopped()}

        return audited(
            st, actor, f"fault.{body.action}", body.host, body.model_dump(), run, summary=lambda r: r["done"]
        )

    @app.delete(
        "/faults", response_model=M.FaultResult, tags=["faults"], summary="Clear every fault and power every device on"
    )
    def clear_faults(st: StateDep, actor: WriterDep) -> dict[str, Any]:
        lab = st.lab

        def run() -> dict[str, Any]:
            keys = lab.core.clear_faults()
            started = [h for h in lab.stopped() if lab.docker.start(h)]
            return {
                "done": f"cleared {len(keys)} key(s), started {', '.join(started) or 'no device'}",
                "faults": [],
                "stopped": lab.stopped(),
            }

        return audited(st, actor, "fault.clear-all", None, None, run, summary=lambda r: r["done"])

    # ------------------------------------------------------------------ changes
    def change(
        st: AppState,
        actor: Actor,
        host: str,
        action: str,
        request: dict[str, Any],
        plan_fn: Callable[[], Any],
        body: Any,
    ) -> dict[str, Any]:
        from netsim_lab.changes import apply_plan, host_lock

        lab = st.lab
        lab.settings.device(host)

        def run() -> dict[str, Any]:
            with host_lock(f"plan:{host}"):
                plan = plan_fn()
                if not body.confirm:
                    return plan.to_dict()
                if not body.plan_id:
                    raise HTTPException(422, "confirm=true needs the plan_id of the dry run you approve")
                if body.plan_id != plan.plan_id:
                    raise HTTPException(
                        409,
                        {
                            "message": "the plan changed since the dry run (running config or candidate differs); "
                            "review the new plan",
                            "plan": plan.to_dict(),
                        },
                    )
                if not plan.validation.valid:
                    raise HTTPException(
                        422, {"message": "the candidate config is invalid", "errors": plan.validation.errors}
                    )
                return apply_plan(lab, plan, verify=body.verify, timeout=body.timeout_s).to_dict()

        return audited(
            st,
            actor,
            f"{action}.{'apply' if body.confirm else 'plan'}",
            host,
            request,
            run,
            diff=lambda r: r.get("diff"),
            summary=lambda r: {
                "plan_id": r.get("plan_id"),
                "applied": r.get("applied", False),
                "verdict": (r.get("verification") or {}).get("verdict"),
                "valid": r.get("validation", {}).get("valid"),
            },
        )

    @app.post(
        "/devices/{host}/config",
        response_model=M.ChangeResult,
        tags=["changes"],
        summary="Change a device's config (dry run first)",
    )
    def device_config(
        st: StateDep, actor: WriterDep, host: Annotated[str, HOST], body: M.ConfigChange
    ) -> dict[str, Any]:
        """Apply a config patch to the device's running config. Without ``confirm`` it is a dry run: the diff, the
        validation, the services/hosts it affects, the drift findings before and predicted after, and a
        ``plan_id``. With ``confirm=true`` and that ``plan_id`` the candidate is committed in one RESTCONF
        transaction and verified: simd runs it, PW states settled, Zabbix re-polled and its services settled."""
        from netsim_lab.changes import plan_patch

        return change(st, actor, host, "config", body.model_dump(), lambda: plan_patch(st.lab, host, body.patch), body)

    @app.post(
        "/devices/{host}/apply-intent",
        response_model=M.ChangeResult,
        tags=["changes"],
        summary="Push NetBox's intent to a device (dry run first)",
    )
    def apply_intent(
        st: StateDep, actor: WriterDep, host: Annotated[str, HOST], body: M.IntentChange | None = None
    ) -> dict[str, Any]:
        """Remediation in the "NetBox wins" direction: the candidate is the config NetBox renders for the device.
        Same dry run -> confirm(plan_id) -> verify flow as ``/devices/{host}/config``; applying it records the
        agreement as the device's drift baseline."""
        from netsim_lab.changes import plan_intent

        body = body or M.IntentChange()
        return change(st, actor, host, "apply-intent", body.model_dump(), lambda: plan_intent(st.lab, host), body)

    # ------------------------------------------------------------------ scenarios
    @app.get("/scenarios", response_model=list[M.ScenarioSummary], tags=["scenarios"], summary="Available scenarios")
    def scenarios(st: StateDep) -> list[dict[str, Any]]:
        from netsim_lab.scenarios import load_scenarios

        return [
            {
                "name": s.name,
                "title": s.title,
                "description": s.description.strip(),
                "steps": [step.describe() for step in s.steps],
                "phases": [p.name for p in s.phases],
                "budget_s": s.budget_s,
            }
            for s in load_scenarios(st.settings.scenarios_dir).values()
        ]

    @app.get("/scenarios/{name}", tags=["scenarios"], summary="A scenario's definition (steps and expected outcome)")
    def scenario(st: StateDep, name: str) -> dict[str, Any]:
        from netsim_lab.scenarios import load_scenario

        return load_scenario(st.settings.scenarios_dir, name).model_dump(mode="json", by_alias=True)

    @app.get(
        "/scenarios/{name}/check",
        response_model=M.ScenarioCheck,
        tags=["scenarios"],
        summary="Does the lab match the scenario's expected outcome now?",
    )
    def scenario_check(st: StateDep, name: str) -> dict[str, Any]:
        """Checks the main phase's expectations once (simd, Zabbix items/services/problems, drift, impact; SNMP is
        checked by labctl and the e2e tests). Read-only: runs no steps."""
        from netsim_lab.scenarios import check_phase, load_scenario

        sc = load_scenario(st.settings.scenarios_dir, name)
        mismatches = check_phase(st.lab, sc.expect, since=0.0)
        return {"scenario": name, "ok": not mismatches, "mismatches": mismatches}

    @app.post(
        "/scenarios/{name}/apply", response_model=M.ScenarioApply, tags=["scenarios"], summary="Run a scenario's steps"
    )
    def scenario_apply(st: StateDep, actor: WriterDep, name: str) -> dict[str, Any]:
        """Runs the main steps only (follow-up phases are run by ``labctl scenario verify``). Start from a clean lab:
        ``POST /reset`` first."""
        from netsim_lab.scenarios import apply_scenario, load_scenario

        sc = load_scenario(st.settings.scenarios_dir, name)
        return audited(
            st,
            actor,
            "scenario.apply",
            name,
            None,
            lambda: apply_scenario(st.lab, sc, actor.name, log=None),
            summary=lambda r: [s["step"] for s in r["steps"]],
        )

    @app.post("/reset", tags=["scenarios"], summary="Back to the seeded lab and wait until green")
    def reset_(st: StateDep, actor: WriterDep, body: Annotated[M.ResetRequest | None, Body()] = None) -> dict[str, Any]:
        """Faults cleared, stopped devices started, NetBox re-seeded if it differs from the topology, golden config
        on every device, baselines cleared, zabbix-sync if needed; then waits until simd and Zabbix are green."""
        from netsim_lab.reset import reset

        body = body or M.ResetRequest()

        def sync_now(_lab: Any, reason: str, _log: Any = None) -> dict[str, Any]:
            return {"via": "lab-api", **(st.syncer.run_now(reason, wait=True).get("last") or {})}

        return audited(
            st,
            actor,
            "reset",
            None,
            body.model_dump(),
            lambda: reset(st.lab, wait=body.wait, timeout=body.timeout_s, log=None, sync=sync_now),
            summary=lambda r: {
                "green": r.get("green"),
                "elapsed_s": r.get("elapsed_s"),
                "restored": r.get("devices_restored"),
            },
        )

    # ------------------------------------------------------------------ sync, webhooks
    @app.get("/sync", response_model=M.SyncStatus, tags=["sync"], summary="NetBox -> Zabbix sync status")
    def sync_status(st: StateDep) -> dict[str, Any]:
        return st.syncer.status()

    @app.post("/sync", response_model=M.SyncStatus, tags=["sync"], summary="Run zabbix-sync now")
    def sync_now(
        st: StateDep, actor: WriterDep, body: Annotated[M.SyncRequest | None, Body()] = None
    ) -> dict[str, Any]:
        """Spec macros, BSM tree, SLAs and map from NetBox. Runs after any sync in progress; ``wait`` returns when
        the run that includes this request has finished (15-25 s when a spec changed)."""
        body = body or M.SyncRequest()
        return audited(
            st,
            actor,
            "sync.request",
            None,
            body.model_dump(),
            lambda: st.syncer.run_now(f"{actor.name}: {body.reason}", wait=body.wait),
            summary=lambda r: r.get("last"),
        )

    @app.post(
        "/hooks/netbox",
        status_code=202,
        response_model=M.HookAccepted,
        tags=["sync"],
        summary="NetBox webhook receiver",
    )
    async def netbox_hook(
        request: Request,
        st: StateDep,
        x_hook_signature: Annotated[
            str | None, Header(description="hex HMAC-SHA512 of the body with NETBOX_WEBHOOK_SECRET")
        ] = None,
    ) -> dict[str, Any]:
        """NetBox calls this on every change of a device, interface, IP address, tenant, L2VPN or L2VPN termination.
        The signature is verified, the event stored, and ``zabbix-sync`` runs ~2 s after the last event (debounced)."""
        body = await request.body()
        secret = st.current().webhook_secret
        if not secret:
            raise HTTPException(503, "NETBOX_WEBHOOK_SECRET is not configured on lab-api")
        expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
        if not x_hook_signature or not hmac.compare_digest(x_hook_signature.strip().lower(), expected):
            st.audit.write(
                actor="netbox-webhook",
                client=request.client.host if request.client else None,
                action="hook.netbox",
                status=401,
                ok=False,
                result="bad signature",
            )
            raise HTTPException(401, "bad or missing X-Hook-Signature")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc
        data = payload.get("data") or {}
        event = {
            "received": time.time(),
            "event": payload.get("event"),
            "model": payload.get("object_type") or payload.get("model"),
            "object": data.get("display") or data.get("name") or data.get("id"),
            "username": payload.get("username"),
            "request_id": payload.get("request_id"),
        }
        st.hooks.append(event)
        with st.hook_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**event, "payload": payload}, default=str) + "\n")
        st.lab.forget_intent()
        st.syncer.request(f"netbox {event['event']} {event['model']} {event['object']}")
        st.audit.write(
            actor="netbox-webhook",
            client=request.client.host if request.client else None,
            action="hook.netbox",
            target=str(event["object"]),
            request=event,
            result="sync scheduled",
            status=202,
        )
        return {"accepted": True, "event": event["event"], "model": event["model"], "sync": st.syncer.status()}

    @app.get("/hooks/netbox", tags=["sync"], summary="Recent NetBox webhook events")
    def netbox_hooks(st: StateDep, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
        return list(st.hooks)[-limit:]

    # ------------------------------------------------------------------ audit
    @app.get("/audit", tags=["audit"], summary="Audit log of writes")
    def audit(
        st: StateDep,
        since: Annotated[float | None, Query(description="Only entries after this Unix time")] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 100,
        action: Annotated[
            str | None, Query(description="e.g. config.apply, apply-intent.plan, fault.inject, reset")
        ] = None,
    ) -> list[dict[str, Any]]:
        return st.audit.read(since=since, limit=limit, action=action)

    return app
