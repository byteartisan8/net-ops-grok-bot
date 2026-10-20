"""Declarative scenarios (``lab/scenarios/*.yaml``): steps, then the expected outcome in every system.

The same files drive ``labctl scenario apply|verify``, lab-api's ``POST /scenarios/{name}/apply`` and the e2e
tests; nothing a test checks is outside the file. Format (see ``lab/scenarios/README.md``)::

    name: vcid-typo
    title: ...
    description: ...
    budget_s: 10                      # e2e: last step -> Zabbix service status as expected within this time
    steps:                            # applied in order
      - config:
          host: spoke-1
          patch: [{op: set, path: "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id", value: 2010}]
    expect:
      pws:                            # every PW end: simd oper/reason; SNMP cpwVcOperStatus and Zabbix row agree
        default: up/ok                # ends not listed below
        SVC-0001 backup: {spoke-1: down/remote-not-configured, hub-b: down/remote-not-configured}
      zabbix:
        services: {default: OK, SVC-0001: Warning}
        customers: {default: OK, Customer-A: Warning}   # optional: the customer nodes of the BSM
        problems: [{host: spoke-1, service: SVC-0001, pw_role: backup, check: spec, severity: Average}]
      drift:
        findings: [{service: SVC-0001, class: config-drift, kind: vcid-mismatch, message: "..."}]
    then:                             # follow-up phases, run by `verify` once `expect` holds
      - name: ...
        steps: [...]
        expect: {...}

Steps: ``fault: {uplink: H}``, ``fault: {ac: {host: H, interface: I}}``, ``cut: [A, B]`` (each with optional
``restore: true``), ``device: {stop: H}`` / ``{start: H}``, ``config: {host, patch}`` (one RESTCONF commit, like an
engineer), ``netbox: {add_service: {...}}`` / ``{remove_service: SVC}``, ``lab_api: {apply_intent: [hosts]}``
(lab-api's propose -> confirm -> verify flow, as the bot would do it), ``sleep: seconds``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from netsim_common.signaling import FaultKey
from netsim_lab.configpath import apply_patch, normalize_patch, validate
from netsim_lab.correlate import correlate

SEVERITY_NAMES = ("OK", "Not classified", "Information", "Warning", "Average", "High", "Disaster")
PW_KEY = re.compile(r"^(?P<service>SVC-[0-9]{4}) (?P<role>primary|backup)$")
END_VALUE = re.compile(r"^(?P<oper>up|down|lowerLayerDown)(/(?P<reason>[a-z-]+))?$|^(absent|unreachable)$")


class ScenarioError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------- model
class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AddService(_M):
    id: str = Field(pattern=r"^SVC-[0-9]{4}$")
    customer: str
    spoke: str
    vlan: int = Field(ge=1, le=4094)
    primary_vc: int = Field(ge=1)
    backup_vc: int | None = Field(default=None, ge=1, description="omit for an unprotected service (primary only)")
    primary_hub: str = "hub-a"
    backup_hub: str = "hub-b"


class Step(_M):
    """Exactly one action per step."""

    fault: dict[str, Any] | None = None
    cut: list[str] | None = None
    restore: bool = False
    device: dict[str, str] | None = None
    config: dict[str, Any] | None = None
    netbox: dict[str, Any] | None = None
    lab_api: dict[str, Any] | None = None
    sleep: float | None = None

    @model_validator(mode="after")
    def _one_action(self) -> Step:
        actions = [
            k
            for k in ("fault", "cut", "device", "config", "netbox", "lab_api", "sleep")
            if getattr(self, k) is not None
        ]
        if len(actions) != 1:
            raise ValueError(f"a step needs exactly one action, got {actions or 'none'}")
        if self.cut is not None and len(self.cut) != 2:
            raise ValueError("cut needs two hosts")
        if self.fault is not None and set(self.fault) - {"uplink", "ac"}:
            raise ValueError("fault is {uplink: host} or {ac: {host, interface}}")
        if self.device is not None and (len(self.device) != 1 or next(iter(self.device)) not in ("stop", "start")):
            raise ValueError("device is {stop: host} or {start: host}")
        if self.config is not None:
            if set(self.config) - {"host", "patch"} or "host" not in self.config:
                raise ValueError("config is {host, patch}")
            normalize_patch(self.config.get("patch"))
        if self.netbox is not None:
            if set(self.netbox) - {"add_service", "remove_service"} or len(self.netbox) != 1:
                raise ValueError("netbox is {add_service: {...}} or {remove_service: SVC-xxxx}")
            if "add_service" in self.netbox:
                AddService.model_validate(self.netbox["add_service"])
        if self.lab_api is not None and set(self.lab_api) != {"apply_intent"}:
            raise ValueError("lab_api is {apply_intent: [hosts]}")
        return self

    def describe(self) -> str:
        if self.fault is not None:
            if "uplink" in self.fault:
                return f"{'restore' if self.restore else 'fault'} uplink {self.fault['uplink']}"
            ac = self.fault["ac"]
            return f"{'restore' if self.restore else 'fault'} ac {ac['host']} {ac['interface']}"
        if self.cut is not None:
            return f"{'restore' if self.restore else 'cut'} {self.cut[0]} <-> {self.cut[1]}"
        if self.device is not None:
            action, host = next(iter(self.device.items()))
            return f"device {action} {host}"
        if self.config is not None:
            ops = normalize_patch(self.config["patch"])
            return f"config {self.config['host']}: " + "; ".join(
                f"{o['op']} {o['path']}" + (f" = {o['value']}" if "value" in o else "") for o in ops
            )
        if self.netbox is not None:
            if "add_service" in self.netbox:
                return f"NetBox: add {self.netbox['add_service']['id']}"
            return f"NetBox: remove {self.netbox['remove_service']}"
        if self.lab_api is not None:
            return f"lab-api: apply intent on {', '.join(self.lab_api['apply_intent'])}"
        return f"sleep {self.sleep} s"


class ProblemExpect(_M):
    host: str | None = None
    service: str | None = None
    pw_role: str | None = None
    check: str | None = None
    severity: str | None = None
    scope: str | None = None
    name: str | None = Field(default=None, description="regular expression matched against the event name")


class CorrelationExpect(_M):
    service: str
    checks: list[str]
    hosts: list[str]
    window_s: float = 120.0


class ZabbixExpect(_M):
    services: dict[str, str] = Field(default_factory=dict)
    customers: dict[str, str] = Field(default_factory=dict, description="status of the BSM customer nodes")
    problems: list[ProblemExpect] = Field(default_factory=list)
    problems_exact: bool = Field(default=False, description="no other PW problems (scope l2vpn-pw/-unexpected) open")
    correlation: list[CorrelationExpect] = Field(default_factory=list)

    @field_validator("services", "customers")
    @classmethod
    def _statuses(cls, v: dict[str, str]) -> dict[str, str]:
        for key, status in v.items():
            if status not in (*SEVERITY_NAMES, "absent"):
                raise ValueError(f"{key}: {status!r} is not a Zabbix status (or 'absent')")
        return v


class FindingExpect(_M):
    service: str | None = None
    services: list[str] | None = Field(
        default=None, description="the same finding once per listed service (instead of `service`)"
    )
    cls: str = Field(alias="class")
    kind: str
    hosts: list[str] | None = None
    message: str | None = None
    match: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> FindingExpect:
        if self.service is not None and self.services is not None:
            raise ValueError("a finding names `service` or `services`, not both")
        if self.services is not None and self.message is not None:
            raise ValueError("`message` names one service: use `match` with `services`")
        return self


class DriftExpect(_M):
    findings: list[FindingExpect] = Field(default_factory=list)


class ImpactExpect(_M):
    """``labctl impact <host>``: per service, the intent blast radius and the live impact (down/degraded/unaffected)."""

    host: str
    intent: dict[str, str] = Field(default_factory=dict)
    live: dict[str, str] = Field(default_factory=dict)


class Expect(_M):
    pws: dict[str, Any] = Field(default_factory=dict)
    zabbix: ZabbixExpect | None = None
    drift: DriftExpect | None = None
    impact: list[ImpactExpect] = Field(default_factory=list)

    @field_validator("pws")
    @classmethod
    def _pws(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key, value in v.items():
            if key == "default":
                if not END_VALUE.match(str(value)):
                    raise ValueError(f"pws.default: bad end state {value!r}")
                continue
            if not PW_KEY.match(key):
                raise ValueError(f"pws: key {key!r} must be '<SVC-xxxx> <primary|backup>' or 'default'")
            ends = value if isinstance(value, dict) else {"*": value}
            for host, state in ends.items():
                if not END_VALUE.match(str(state)):
                    raise ValueError(f"pws.{key}.{host}: bad end state {state!r} (oper[/reason], absent, unreachable)")
        return v


class Phase(_M):
    name: str = "main"
    steps: list[Step] = Field(default_factory=list)
    expect: Expect = Field(default_factory=Expect)
    budget_s: float | None = None
    timeout_s: float = 120.0


class Scenario(_M):
    name: str
    title: str
    description: str
    budget_s: float | None = None
    timeout_s: float = 120.0
    steps: list[Step] = Field(default_factory=list)
    expect: Expect = Field(default_factory=Expect)
    then: list[Phase] = Field(default_factory=list)

    @property
    def main(self) -> Phase:
        return Phase(
            name="main", steps=self.steps, expect=self.expect, budget_s=self.budget_s, timeout_s=self.timeout_s
        )

    @property
    def phases(self) -> list[Phase]:
        return [self.main, *self.then]


def load_scenarios(directory: Path) -> dict[str, Scenario]:
    out = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            scenario = Scenario.model_validate(data)
        except Exception as exc:
            raise ScenarioError(f"{path.name}: {exc}") from exc
        if scenario.name != path.stem:
            raise ScenarioError(f"{path.name}: name {scenario.name!r} must equal the file name")
        out[scenario.name] = scenario
    return out


def load_scenario(directory: Path, name: str) -> Scenario:
    scenarios = load_scenarios(directory)
    if name not in scenarios:
        raise ScenarioError(f"no scenario {name!r} (have: {', '.join(scenarios)})")
    return scenarios[name]


# ---------------------------------------------------------------------------------------------- steps
def lab_api_apply_intent(lab: Any, host: str, actor: str) -> dict[str, Any]:
    """lab-api ``POST /devices/{host}/apply-intent``: dry run, then confirm with the returned ``plan_id``."""
    from netsim_lab.labapi_client import LabApiClient, LabApiError

    try:
        with LabApiClient(lab.settings.lab_api_url, lab.settings.lab_api_token, actor) as api:
            result = api.apply_intent(host)
    except LabApiError as exc:
        raise ScenarioError(str(exc)) from exc
    return {"host": host, "applied": result.get("applied"), "verdict": result.get("verification", {}).get("verdict")}


def run_step(lab: Any, step: Step, actor: str = "scenario") -> dict[str, Any]:
    """Execute one step; returns a short record (what, result)."""
    from netsim_lab import netbox_ops

    record: dict[str, Any] = {"step": step.describe(), "t": time.time()}
    if step.fault is not None:
        if "uplink" in step.fault:
            lab.settings.device(step.fault["uplink"])
            lab.core.set_uplink(step.fault["uplink"], not step.restore)
        else:
            ac = step.fault["ac"]
            lab.settings.device(ac["host"])
            lab.core.set_ac(ac["host"], ac["interface"], not step.restore)
    elif step.cut is not None:
        for h in step.cut:
            lab.settings.device(h)
        lab.core.set_cut(step.cut[0], step.cut[1], not step.restore)
    elif step.device is not None:
        action, host = next(iter(step.device.items()))
        record["changed"] = lab.docker.stop(host) if action == "stop" else lab.docker.start(host)
    elif step.config is not None:
        host = step.config["host"]
        device = lab.devices[host]
        from netsim_common.canonical import canonical_dict

        candidate = apply_patch(canonical_dict(device.running()), step.config["patch"])
        validation = validate(candidate)
        if not validation.valid:
            raise ScenarioError(f"config step on {host}: invalid result: {'; '.join(validation.errors)}")
        device.replace(validation.config)
    elif step.netbox is not None:
        if "add_service" in step.netbox:
            spec = AddService.model_validate(step.netbox["add_service"])
            record["netbox"] = netbox_ops.add_service(
                netbox_ops.NewService(
                    spec.id,
                    spec.customer,
                    spec.spoke,
                    spec.vlan,
                    spec.primary_vc,
                    spec.backup_vc,
                    spec.primary_hub,
                    spec.backup_hub,
                )
            )
        else:
            record["netbox"] = netbox_ops.remove_service(step.netbox["remove_service"])
        lab.forget_intent()
    elif step.lab_api is not None:
        record["results"] = [lab_api_apply_intent(lab, h, actor) for h in step.lab_api["apply_intent"]]
    elif step.sleep is not None:
        time.sleep(step.sleep)
    record["elapsed_s"] = round(time.time() - record["t"], 3)
    return record


def steps_end(records: list[dict[str, Any]]) -> float:
    """When the last step finished (the start of a phase's timing); now if there were no steps."""
    return records[-1]["t"] + records[-1]["elapsed_s"] if records else time.time()


def run_steps(
    lab: Any, steps: list[Step], actor: str = "scenario", log: Callable[[str], None] | None = None
) -> list[dict[str, Any]]:
    out = []
    for step in steps:
        if log:
            log(f"  step: {step.describe()}")
        out.append(run_step(lab, step, actor))
    return out


# ---------------------------------------------------------------------------------------------- expectations
def _services_in_intent(lab: Any) -> list[str]:
    from netsim_lab.drift import plans_from_intent

    return sorted(plans_from_intent(lab.intent(max_age=2.0)))


def expected_ends(lab: Any, expect: Expect) -> dict[tuple[str, str, str], str]:
    """``{(service, role, host): "oper/reason"}`` for every intended PW end (plus explicitly listed ones)."""
    from netsim_lab.drift import plans_from_intent

    plans = plans_from_intent(lab.intent(max_age=2.0))
    default = expect.pws.get("default")
    out: dict[tuple[str, str, str], str] = {}
    if default:
        for service, plan in plans.items():
            for role, pw in plan.pws.items():
                for host in (pw.spoke, pw.hub):
                    out[(service, role, host)] = str(default)
    for key, value in expect.pws.items():
        if key == "default":
            continue
        m = PW_KEY.match(key)
        service, role = m["service"], m["role"]
        if isinstance(value, dict):
            for host, state in value.items():
                out[(service, role, host)] = str(state)
        else:
            plan = plans.get(service)
            if plan is None or role not in plan.pws:
                raise ScenarioError(f"pws.{key}: not in NetBox intent; list the hosts explicitly")
            pw = plan.pws[role]
            for host in (pw.spoke, pw.hub):
                out[(service, role, host)] = str(value)
    return out


def _end_matches(want: str, oper: str, reason: str | None) -> bool:
    if want in ("absent", "unreachable"):
        return oper == want
    w_oper, _, w_reason = want.partition("/")
    if w_oper == "up" and not w_reason:
        w_reason = "ok"
    return oper == w_oper and (not w_reason or reason == w_reason)


def check_simd(states: dict[str, Any], ends: dict[tuple[str, str, str], str]) -> list[str]:
    out = []
    for (service, role, host), want in sorted(ends.items()):
        st = states.get(host)
        if st is None or isinstance(st, Exception):
            oper, reason = "unreachable", None
        else:
            row = next((p for p in st.get("pseudowires", []) if p["service_id"] == service and p["role"] == role), None)
            oper, reason = (row["oper"], row["reason"]) if row else ("absent", None)
        if not _end_matches(want, oper, reason):
            got = oper if reason is None else f"{oper}/{reason}"
            out.append(f"simd {host} {service} {role}: expected {want}, got {got}")
    return out


OPER_VALUES = {"up": 1, "down": 2, "lowerLayerDown": 7}


def snmp_pw_table(lab: Any, hosts: list[str]) -> dict[str, dict[str, int] | Exception]:
    """``{host: {"SVC-0001:primary": cpwVcOperStatus}}`` by SNMP walk (needs the workspace's netsim-simd)."""
    from netsim_simd.snmpclient import walk

    from netsim_common import load_oids

    oids = load_oids()
    name_col, oper_col = oids.oid("cpwVcName"), oids.oid("cpwVcOperStatus")

    async def one(host: str) -> dict[str, int] | Exception:
        addr, port = lab.settings.device(host).snmp
        try:
            names = await walk(addr, port, lab.settings.snmp_community, name_col, timeout=1.0, retries=1)
            opers = await walk(addr, port, lab.settings.snmp_community, oper_col, timeout=1.0, retries=1)
        except Exception as exc:
            return exc
        by_index = {vb.oid.rsplit(".", 1)[1]: str(vb.rendered) for vb in names}
        return {
            by_index[vb.oid.rsplit(".", 1)[1]]: int(vb.rendered) for vb in opers if vb.oid.rsplit(".", 1)[1] in by_index
        }

    async def all_hosts() -> list[dict[str, int] | Exception]:
        return await asyncio.gather(*(one(h) for h in hosts))

    return dict(zip(hosts, asyncio.run(all_hosts()), strict=True))


def check_snmp(lab: Any, ends: dict[tuple[str, str, str], str]) -> list[str]:
    try:
        tables = snmp_pw_table(lab, sorted({h for _, _, h in ends}))
    except ImportError:
        return []  # lab-api's image has no SNMP client; labctl and the e2e tests do
    out = []
    for (service, role, host), want in sorted(ends.items()):
        table = tables.get(host)
        if want == "unreachable":
            if not isinstance(table, Exception):
                out.append(f"SNMP {host}: expected no answer (device down), got a cpwVcTable")
            continue
        if isinstance(table, Exception) or table is None:
            out.append(f"SNMP {host}: walk failed: {table}")
            continue
        value = table.get(f"{service}:{role}")
        if want == "absent":
            if value is not None:
                out.append(f"SNMP {host} {service}:{role}: expected no row, got cpwVcOperStatus {value}")
            continue
        expected = OPER_VALUES[want.partition("/")[0]]
        if value != expected:
            out.append(f"SNMP {host} {service}:{role}: cpwVcOperStatus expected {expected}, got {value}")
    return out


def check_zabbix_items(rows: dict[str, Any], ends: dict[tuple[str, str, str], str]) -> list[str]:
    """Zabbix ``netsim.pw.row`` of every expected PW end (items exist for intended PWs only) must agree.

    Values are compared, not timestamps: a host the step did not touch keeps its last (still correct) value until
    its next regular poll; a host whose PWs changed sent traps and was re-polled by check-now.
    """
    out = []
    for (service, role, host), want in sorted(ends.items()):
        if want == "unreachable":
            continue  # a stopped device's items keep their last values (04 report)
        row = rows.get(host, {}).get((service, role))
        if row is None:
            continue  # not in this host's NetBox spec: Zabbix tracks it as an observed PW instead
        if want == "absent":
            if row.get("present") != 0:
                out.append(f"Zabbix {host} {service} {role}: expected present=0, got {row.get('present')}")
            continue
        expected = OPER_VALUES[want.partition("/")[0]]
        if row.get("present") != 1 or row.get("oper") != expected:
            got = f"present={row.get('present')} oper={row.get('oper')}"
            out.append(f"Zabbix {host} {service} {role}: expected present=1 oper={expected}, got {got}")
    return out


def check_services(
    statuses: dict[str, str], want: dict[str, str], all_services: list[str], what: str = "service"
) -> list[str]:
    out = []
    default = want.get("default")
    targets = dict.fromkeys(all_services, default) if default else {}
    targets.update({k: v for k, v in want.items() if k != "default"})
    for service, expected in sorted(targets.items()):
        got = statuses.get(service, "absent")
        if got != expected:
            out.append(f"Zabbix {what} {service}: expected {expected}, got {got}")
    return out


def _problem_matches(p: dict[str, Any], e: ProblemExpect) -> bool:
    for f in ("host", "service", "pw_role", "check", "severity", "scope"):
        want = getattr(e, f)
        if want is not None and p.get(f) != want:
            return False
    return e.name is None or re.search(e.name, p["name"]) is not None


def check_problems(problems: list[dict[str, Any]], z: ZabbixExpect) -> list[str]:
    out = []
    pw_problems = [p for p in problems if p.get("scope") in ("l2vpn-pw", "l2vpn-unexpected")]
    for e in z.problems:
        hit = next((p for p in problems if _problem_matches(p, e)), None)
        if hit is None:
            out.append(f"Zabbix problem missing: {e.model_dump(exclude_none=True)}")
    if z.problems_exact:
        extra = [p for p in pw_problems if not any(_problem_matches(p, e) for e in z.problems)]
        out += [f"Zabbix unexpected problem: [{p['severity']}] {p['name']}" for p in extra]
    for c in z.correlation:
        groups = [g for g in correlate(problems, c.window_s) if g["service"] == c.service]
        ok = any(g["checks"] == sorted(c.checks) and g["hosts"] == sorted(c.hosts) for g in groups)
        if not ok:
            seen = [(g["checks"], g["hosts"]) for g in groups]
            out.append(
                f"correlation {c.service}: expected one group {sorted(c.checks)} on {sorted(c.hosts)}, got {seen}"
            )
    return out


def check_drift(report: Any, d: DriftExpect, all_services: list[str]) -> list[str]:
    out = []
    expected = []
    for f in d.findings:
        services = f.services if f.services is not None else all_services if f.service == "*" else [f.service]
        expected += [(s, f) for s in services]
    remaining = list(report.findings)
    for service, f in expected:
        hit = next(
            (
                a
                for a in remaining
                if a.service == service
                and a.cls == f.cls
                and a.kind == f.kind
                and (f.hosts is None or sorted(f.hosts) == a.hosts)
                and (f.message is None or a.message == f.message)
                and (f.match is None or re.search(f.match, a.message))
            ),
            None,
        )
        if hit is None:
            out.append(f"drift finding missing: {service} {f.cls} {f.kind} {f.message or f.match or ''}".rstrip())
        else:
            remaining.remove(hit)
    out += [f"drift finding not expected: [{a.cls}/{a.kind}] {a.message}" for a in remaining]
    return out


def check_impact(lab: Any, expectations: list[ImpactExpect], all_services: list[str]) -> list[str]:
    from netsim_lab.impact import impact

    out = []
    for e in expectations:
        result = {s["service"]: s for s in impact(lab, e.host)["services"]}
        for field_name, want in (("intent_impact", e.intent), ("live_impact", e.live)):
            default = want.get("default")
            targets = dict.fromkeys(all_services, default) if default else {}
            targets.update({k: v for k, v in want.items() if k != "default"})
            for service, expected in sorted(targets.items()):
                got = result.get(service, {}).get(field_name)
                if got != expected:
                    out.append(f"impact {e.host} {service} {field_name}: expected {expected}, got {got}")
    return out


def check_phase(lab: Any, expect: Expect, since: float, *, deep: bool = True) -> dict[str, Any]:
    """Every mismatch between the lab and ``expect`` right now (``deep`` adds SNMP and drift, the slow parts)."""
    result: dict[str, list[str]] = {
        "simd": [],
        "snmp": [],
        "zabbix_items": [],
        "zabbix_services": [],
        "zabbix_problems": [],
        "drift": [],
        "impact": [],
    }
    services = _services_in_intent(lab)
    ends = expected_ends(lab, expect) if expect.pws else {}
    if ends:
        result["simd"] = check_simd(dict(lab.devices.states()), ends)
    if expect.zabbix is not None or ends:
        z = lab.zabbix()
        if expect.zabbix is not None:
            tree = z.services()
            statuses = {s: v.get("status", "?") for s, v in tree["services"].items() if "status" in v}
            all_services = sorted(set(services) | set(statuses))
            result["zabbix_services"] = check_services(statuses, expect.zabbix.services, all_services)
            if expect.zabbix.customers:
                result["zabbix_services"] += check_services(
                    tree["customers"], expect.zabbix.customers, sorted(tree["customers"]), "customer"
                )
            if expect.zabbix.problems or expect.zabbix.problems_exact or expect.zabbix.correlation:
                result["zabbix_problems"] = check_problems(z.problems(), expect.zabbix)
        if ends:
            result["zabbix_items"] = check_zabbix_items(z.pw_rows(), ends)
    if deep and not any(result.values()):
        if ends:
            result["snmp"] = check_snmp(lab, ends)
        if expect.drift is not None:
            result["drift"] = check_drift(lab.drift(), expect.drift, services)
        if expect.impact:
            result["impact"] = check_impact(lab, expect.impact, services)
    return {k: v for k, v in result.items() if v}


def verify_phase(
    lab: Any, phase: Phase, since: float, *, timeout: float | None = None, log: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Poll until every expectation holds (or the timeout).

    The Zabbix business service statuses are also polled on their own (one cheap ``service.get``) and the first
    moment they all match is recorded: that is the "fault -> business service status" time of contract 9.5.
    """
    deadline = time.monotonic() + (timeout if timeout is not None else phase.timeout_s)
    start = time.monotonic()
    zabbix_at = None
    want_services = phase.expect.zabbix.services if phase.expect.zabbix is not None else {}
    services = _services_in_intent(lab) if want_services else []
    mismatches: dict[str, list[str]] = {}
    while True:
        if zabbix_at is None and want_services:
            # Until the business services match, poll only them (one service.get every 0.2 s): precise timing.
            statuses = lab.zabbix().service_statuses()
            if not check_services(statuses, want_services, sorted(set(services) | set(statuses))):
                zabbix_at = time.time()
            elif time.monotonic() < deadline:
                time.sleep(0.2)
                continue
        mismatches = check_phase(lab, phase.expect, since)
        if not mismatches or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    done = time.time()
    result = {
        "phase": phase.name,
        "ok": not mismatches,
        "mismatches": mismatches,
        "elapsed_s": round(done - since, 2),
        "zabbix_services_s": round(zabbix_at - since, 2) if zabbix_at else None,
        "budget_s": phase.budget_s,
        "checked_for_s": round(time.monotonic() - start, 2),
    }
    if log:
        state = "OK" if result["ok"] else "FAILED"
        log(f"  {phase.name}: {state} after {result['elapsed_s']} s (Zabbix services: {result['zabbix_services_s']} s)")
        for part, items in mismatches.items():
            for item in items[:10]:
                log(f"    {part}: {item}")
    return result


def apply_scenario(
    lab: Any, scenario: Scenario, actor: str = "labctl", log: Callable[[str], None] | None = None
) -> dict[str, Any]:
    if log:
        log(f"scenario {scenario.name}: {scenario.title}")
    records = run_steps(lab, scenario.steps, f"{actor}:{scenario.name}", log)
    return {"scenario": scenario.name, "steps": records, "applied_at": time.time()}


def verify_scenario(
    lab: Any,
    scenario: Scenario,
    since: float | None = None,
    *,
    follow_ups: bool = True,
    actor: str = "labctl",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Check the main expectations, then run each ``then`` phase (its steps, then its expectations)."""
    phases = [verify_phase(lab, scenario.main, since or time.time() - scenario.timeout_s, log=log)]
    if follow_ups and phases[0]["ok"]:
        for phase in scenario.then:
            if log:
                log(f"phase {phase.name}:")
            records = run_steps(lab, phase.steps, f"{actor}:{scenario.name}:{phase.name}", log)
            result = verify_phase(lab, phase, steps_end(records), log=log)
            result["steps"] = records
            phases.append(result)
            if not result["ok"]:
                break
    return {
        "scenario": scenario.name,
        "ok": all(p["ok"] for p in phases) and len(phases) == len(scenario.phases if follow_ups else [0]),
        "phases": phases,
    }


def faults_of(lab: Any) -> list[FaultKey]:
    return lab.core.faults()


__all__ = [
    "Expect",
    "Phase",
    "Scenario",
    "ScenarioError",
    "Step",
    "apply_scenario",
    "check_phase",
    "load_scenario",
    "load_scenarios",
    "run_step",
    "verify_phase",
    "verify_scenario",
]
