"""lab-api request and response models. Every field is described: the descriptions become the bot's tool docs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["OK", "Not classified", "Information", "Warning", "Average", "High", "Disaster"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------------------ health, topology
class Health(_Out):
    status: Literal["ok"] = Field(description="Always 'ok' when lab-api answers; dependencies are checked elsewhere")
    version: str = Field(description="lab-api version")
    uptime_s: float = Field(description="Seconds since lab-api started")


class DeviceInfo(_Out):
    hostname: str = Field(description="Device name (also the container name and the Zabbix host name)")
    role: str = Field(description="hub or spoke")
    site: str = Field(description="NetBox site")
    region: str = Field(description="NetBox region of the site (each region has its own hub pair)")
    mgmt_ip: str = Field(description="Management address on labnet (SNMP, RESTCONF, simd)")
    loopback: str = Field(description="Loopback0: the address PWs peer with")
    running: bool | None = Field(description="Container running (null if Docker could not be asked)")


class LinkInfo(_Out):
    spoke: str = Field(description="Spoke end")
    hub: str = Field(description="Hub end")
    pseudowires: list[str] = Field(description="PWs on this spoke-hub pair as '<service> <role> vc <id>'")


class Topology(_Out):
    source: Literal["netbox", "topology"] = Field(
        description="Where devices/services come from (NetBox unless it is down)"
    )
    regions: dict[str, list[str]] = Field(description="Region -> its sites (NetBox child regions of the lab)")
    devices: list[DeviceInfo] = Field(description="Every PE")
    services: list[dict[str, Any]] = Field(
        description="Every service: customer, SLA tier, status rules (redundant-pair: primary + backup PW; "
        "unprotected: primary only), spoke, PWs with hub and VC ID"
    )
    links: list[LinkInfo] = Field(description="Spoke-hub pairs that carry PWs (the L2VPN map)")
    customers: dict[str, str | None] = Field(
        description="Customer -> SLA tier (gold 99.95 %, silver 99.9 %, bronze 99.5 %)"
    )
    errors: dict[str, str] = Field(default_factory=dict, description="Sources that could not be read")


# ------------------------------------------------------------------------------------------------ services
class EndState(_Out):
    host: str = Field(description="Device of this PW end")
    ac: str = Field(description="Attachment circuit (subinterface) of this end")
    oper: str | None = Field(description="simd oper status: up, down, lowerLayerDown, absent (no such PW), unreachable")
    reason: str | None = Field(description="simd reason (contract section 7), e.g. ok, peer-unreachable, mtu-mismatch")
    peer: str | None = Field(description="Peer address configured on the device")
    vc_id: int | None = Field(description="VC ID configured on the device")


class PseudowireStatus(_Out):
    role: Literal["primary", "backup"] = Field(
        description="primary = via the service's primary hub, backup = via a second hub (by intent; unprotected "
        "services have no backup)"
    )
    vc_id: int = Field(description="VC ID NetBox intends")
    hub: str = Field(description="Hub NetBox intends for this PW")
    up: bool = Field(description="Both ends up in simd")
    spoke_end: EndState
    hub_end: EndState
    zabbix_leaf: str | None = Field(description="Zabbix status of the BSM leaf of this PW (null if Zabbix has none)")


class ServiceStatus(_Out):
    service: str = Field(description="Service ID, SVC-xxxx")
    customer: str | None = Field(description="Customer (NetBox tenant)")
    spoke: str = Field(description="Spoke the service is delivered on")
    sla_tier: str | None = Field(description="gold, silver or bronze")
    status_rules: str | None = Field(
        description="redundant-pair (primary + backup PW: one down = degraded) or unprotected (primary only: down "
        "= service down); null for a service NetBox no longer has"
    )
    in_netbox: bool = Field(description="False for a service Zabbix still has but NetBox no longer has")
    live: Literal["up", "degraded", "down"] = Field(description="From simd: every PW up / some / none")
    zabbix: str | None = Field(description="Zabbix business service status (OK, Warning = degraded, Disaster = down)")
    pseudowires: list[PseudowireStatus]


class Services(_Out):
    services: list[ServiceStatus]
    devices: dict[str, dict[str, Any]] = Field(description="Per device: reachable, uplink, faults, PW counts")
    zabbix_root: str | None = Field(description="Status of the BSM root 'L2VPN'")
    errors: dict[str, str] = Field(description="Sources that could not be read (netbox, zabbix)")
    generated_at: float = Field(description="Unix time of this snapshot")
    duration_s: float = Field(description="Time taken to collect it")


class ServiceDetail(_Out):
    service: str
    intent: dict[str, Any] | None = Field(
        description="NetBox: customer, SLA tier, status rules, spoke, its PWs with ends, VC IDs, NetBox URLs"
    )
    live: ServiceStatus | None = Field(description="simd state of its PWs (both ends) and the Zabbix status")
    zabbix: dict[str, Any] = Field(
        description="Business service status, leaf statuses, open problems and their correlation groups"
    )
    drift: list[dict[str, Any]] = Field(description="Drift/fault findings for this service (see GET /drift)")


# ------------------------------------------------------------------------------------------------ drift, impact
class Finding(_Out):
    class_: Literal["config-drift", "fault", "intent-only"] = Field(
        alias="class",
        description="config-drift: device differs from NetBox; intent-only: NetBox changed, device not updated yet; "
        "fault: a physical fault or powered-off device, config matches NetBox",
    )
    kind: str = Field(
        description="What it is: redundancy-lost, vcid-mismatch, vcid-changed, peer-mismatch, pw-missing, "
        "pw-unexpected, mtu-mismatch, admin-down, ac-admin-down, not-delivered, not-removed, attribute, device-config, "
        "loopback-changed, uplink-admin-down, intent-invalid; faults: uplink-down, ac-down, core-cut, device-down, "
        "device-unreachable, pw-down"
    )
    service: str | None = Field(description="Service it concerns (null for device-level findings)")
    message: str = Field(
        description="One-line explanation, e.g. 'SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001'"
    )
    hosts: list[str] = Field(description="Hosts where the cause is (the drifted device, the faulty device)")
    roles: list[str] = Field(description="PW roles affected")
    paths: list[str] = Field(
        description="'<host>:<config path>' of the differences behind it (usable in config patches)"
    )
    details: list[str] = Field(description="Underlying observations folded into this finding")
    live: dict[str, dict[str, str]] = Field(description="simd state per role per end: {role: {host: 'oper/reason'}}")


class DriftReport(_Out):
    drift: bool = Field(description="True if any config-drift or intent-only finding exists (labctl drift exits 1)")
    incomplete: bool = Field(description="True if a device could not be checked (unreachable, or NetBox intent broken)")
    summary: dict[str, int] = Field(description="Number of findings per class")
    findings: list[Finding]
    devices: dict[str, dict[str, Any]] = Field(
        description="Per device: status (in-sync, drift, unreachable, no-intent), baseline (golden or stored) and "
        "every difference with its class"
    )
    faults: list[dict[str, Any]] = Field(description="Active fault/cut keys")
    stopped: list[str] = Field(description="Powered-off devices")
    intent_problems: list[str] = Field(description="NetBox data that cannot be turned into intent")
    checked_at: float
    duration_s: float


class Impact(_Out):
    host: str = Field(description="Host whose loss/maintenance is assessed")
    services: list[dict[str, Any]] = Field(
        description="Per service: customer, sla_tier, status_rules, intent_impact (NetBox blast radius), "
        "live_impact (taking the surviving PWs' current state into account), lost/surviving PWs, current Zabbix "
        "status and an explanation"
    )
    summary: dict[str, int] = Field(description="Number of services per live impact")
    by_tier: dict[str, dict[str, int]] = Field(
        description="Per SLA tier: number of services per live impact (down, degraded); unaffected not counted"
    )
    worse_than_intent: list[str] = Field(
        description="Services whose live impact is worse than NetBox's blast radius says"
    )


# ------------------------------------------------------------------------------------------------ Zabbix
class Problem(_Out):
    eventid: str
    name: str = Field(description="Event name; PW checks show expected vs actual")
    severity: Status
    clock: float = Field(description="Start time (Unix)")
    host: str | None
    service: str | None
    pw_role: str | None
    check: str | None = Field(description="missing, oper, spec, unexpected (PW checks), or null")
    scope: str | None = Field(description="l2vpn-pw, l2vpn-unexpected, l2vpn-trap, interface, availability")
    tags: dict[str, str]


class CorrelationGroup(_Out):
    service: str
    problems: int
    eventids: list[str]
    checks: list[str]
    hosts: list[str]
    severity: str
    spread_s: float = Field(description="Seconds between the first and the last problem of the group")
    summary: str = Field(description="One line: service, hosts, checks, 'one root cause'")


class Problems(_Out):
    problems: list[Problem]
    groups: list[CorrelationGroup] = Field(
        description="PW problems grouped by service tag and start time (one root cause each)"
    )


class ZabbixRead(_In):
    method: str = Field(
        description="A Zabbix 8.0 read method: problem.get, event.get, trigger.get, item.get, history.get, "
        "trend.get, host.get, hostgroup.get, service.get, sla.get, sla.getsli, alert.get, maintenance.get. Anything "
        "else gets 403.",
        examples=["problem.get"],
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="The method's parameters as the Zabbix 8.0 API documents them. *.get calls get limit 1000 "
        "unless they set one (at most 10000). Service alerts: alert.get / event.get need eventsource/source 4.",
        examples=[{"output": ["eventid", "name", "severity"], "tags": [{"tag": "customer", "value": "Customer-A"}]}],
    )


class ZabbixReadResult(_Out):
    method: str
    result: Any = Field(description="Zabbix's result, unchanged")
    size: int = Field(description="Size of the response in bytes (at most LAB_READ_MAX_BYTES, default 512 KiB)")


class AckRequest(_In):
    eventids: list[str] = Field(
        min_length=1,
        max_length=200,
        description="Trigger problem event IDs (GET /zabbix/problems) and/or service event IDs (a customer alert's "
        "event_id): a service event stands for the open trigger problems behind that customer",
    )
    message: str = Field(min_length=1, max_length=2048, description="Shown with the acknowledgment in Zabbix")
    role: Literal["noc-engineer", "network-engineer", "noc-manager"] = Field(
        description="Whose Zabbix user acknowledges (with its own API token, so Zabbix shows who acknowledged)"
    )


class AckResult(_Out):
    role: str
    zabbix_user: dict[str, str | None] = Field(
        description="The Zabbix user that acknowledged (userid, username): Zabbix records this userid on each "
        "acknowledgment"
    )
    acknowledged: list[str] = Field(description="Trigger problems acknowledged now (with the message)")
    commented: list[str] = Field(description="Problems that were already acknowledged: the message was added")
    resolved: dict[str, list[str]] = Field(description="Service event ID -> the trigger problems it stood for")
    skipped: list[dict[str, str]] = Field(description="Event IDs that were not acknowledged, and why")
    problems: list[dict[str, Any]] = Field(description="The trigger problems behind the request: eventid, name")


# ------------------------------------------------------------------------------------------------ faults
class FaultRequest(_In):
    kind: Literal["uplink", "ac", "cut", "device"] = Field(
        description="uplink: core uplink physically down; ac: an attachment circuit down; cut: core path between two "
        "devices broken; device: power off/on (docker stop/start)"
    )
    host: str = Field(description="Device (for cut: one end)")
    interface: str | None = Field(default=None, description="ac only: interface name, e.g. GigabitEthernet0/0/1.101")
    peer: str | None = Field(default=None, description="cut only: the other end")
    action: Literal["inject", "restore"] = Field(
        default="inject", description="inject the fault, or restore (repair) it"
    )


class Faults(_Out):
    faults: list[dict[str, Any]] = Field(description="Active fault and cut keys")
    stopped: list[str] = Field(description="Powered-off devices")


class FaultResult(Faults):
    done: str = Field(description="What was done")


# ------------------------------------------------------------------------------------------------ changes
class ConfigChange(_In):
    patch: list[dict[str, Any]] = Field(
        description="Operations applied in order: {op: set, path, value} / {op: delete, path} with paths written like "
        "drift paths ('l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id'), or RFC 6902 JSON Patch ops (add, remove, "
        "replace, test) with JSON Pointer paths into the RESTCONF JSON",
        examples=[[{"op": "set", "path": "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id", "value": 2001}]],
    )
    confirm: bool = Field(default=False, description="false: dry run only (default). true: apply; needs plan_id")
    plan_id: str | None = Field(
        default=None, description="plan_id from the dry run you approve (required with confirm)"
    )
    verify: bool = Field(default=True, description="After applying, wait for and return the simd + Zabbix verification")
    timeout_s: float = Field(default=60.0, ge=5, le=300, description="Verification timeout")
    comment: str | None = Field(default=None, description="Why (stored in the audit log)")


class IntentChange(_In):
    confirm: bool = Field(default=False, description="false: dry run only (default). true: apply; needs plan_id")
    plan_id: str | None = Field(
        default=None, description="plan_id from the dry run you approve (required with confirm)"
    )
    verify: bool = Field(default=True, description="After applying, wait for and return the simd + Zabbix verification")
    timeout_s: float = Field(default=60.0, ge=5, le=300, description="Verification timeout")
    comment: str | None = Field(default=None, description="Why (stored in the audit log)")


class ChangeResult(_Out):
    host: str
    kind: Literal["patch", "apply-intent"]
    plan_id: str = Field(description="Send this back with confirm=true to apply exactly this plan")
    running_hash: str = Field(description="SHA-256 of the running config the plan was made from")
    candidate_hash: str | None = Field(description="SHA-256 of the config that would be committed")
    no_change: bool = Field(description="The candidate equals the running config")
    diff: list[dict[str, Any]] = Field(
        description="Running -> candidate, one entry per leaf / list entry: path, change (changed, added, removed), "
        "from, to, message ('<path>: <from> -> <to>')"
    )
    validation: dict[str, Any] = Field(
        description="valid, errors (device would refuse), warnings (accepted but broken)"
    )
    affected_services: list[str] = Field(description="Services the change touches")
    affected_hosts: list[str] = Field(description="Devices whose PWs can change state (the device and the PW peers)")
    drift_before: list[dict[str, Any]] = Field(description="Drift/fault findings for these services/host now")
    drift_after_predicted: list[dict[str, Any]] = Field(description="Findings expected after the change (computed)")
    applied: bool = Field(default=False, description="True once committed")
    verification: dict[str, Any] | None = Field(
        default=None,
        description="After apply: verdict (ok, degraded, unverified, not-verified), problems, simd PW states, Zabbix "
        "(walk refreshed, service statuses, problems) and the drift findings after the change",
    )
    elapsed_s: float | None = None


# ---------------------------------------------------------------------------------------- scenarios, reset, sync
class ScenarioSummary(_Out):
    name: str
    title: str
    description: str
    steps: list[str] = Field(description="Main steps, human readable")
    phases: list[str] = Field(description="main + follow-up phases (run by labctl scenario verify)")
    budget_s: float | None = Field(description="Time budget from the last step to the expected Zabbix status")


class ScenarioApply(_Out):
    scenario: str
    steps: list[dict[str, Any]] = Field(description="Each step with its result")
    applied_at: float


class ScenarioCheck(_Out):
    scenario: str
    ok: bool = Field(description="Every expectation of the main phase holds right now")
    mismatches: dict[str, list[str]] = Field(
        description="Per system (simd, snmp, zabbix_*, drift, impact): what differs"
    )


class ResetRequest(_In):
    wait: bool = Field(default=True, description="Wait until simd and Zabbix are green")
    timeout_s: float = Field(default=150.0, ge=10, le=600)


class SyncRequest(_In):
    wait: bool = Field(default=False, description="Wait until the sync that includes this request has finished")
    reason: str = Field(default="manual", description="Why (shown in the sync status)")


class SyncStatus(_Out):
    pending: bool = Field(description="A sync is scheduled (debounce running)")
    due_in_s: float | None
    running: bool
    runs: int = Field(description="Finished syncs since lab-api started")
    debounce_s: float
    last: dict[str, Any] | None = Field(description="Last run: ok, summary, changes, warnings, seconds, reasons, error")


class HookAccepted(_Out):
    accepted: bool
    event: str | None
    model: str | None
    sync: SyncStatus
