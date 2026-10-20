# Scenarios

One YAML file per scenario. The same file drives `labctl scenario apply|verify|run`, lab-api
(`POST /scenarios/{name}/apply`, `GET /scenarios/{name}/check`) and the e2e test of that scenario: a test checks
exactly what the file says, nothing else.

| Scenario | Steps | Expected |
|---|---|---|
| `healthy` | none | everything up, every service OK, no PW problem, no drift |
| `primary-fail` | uplink fault on hub-a | all 6 services Warning; drift: `fault` per service, no drift |
| `hub-down` | `docker stop hub-b` | all 6 services Warning (seen from the spokes) |
| `spoke-isolated` | uplink fault on spoke-2 | SVC-0002, SVC-0003 Disaster |
| `double-failure` | hub-a uplink fault + hub-b stopped | all services Disaster, two fault findings each |
| `vcid-typo` | spoke-1 SVC-0001 backup vc-id 2001 -> 2010 | SVC-0001 Warning; `spec` + `oper` problems; drift on spoke-1 |
| `mtu-mismatch` | hub-b access MTU 1500 -> 9000 | every backup down `mtu-mismatch`; all Warning; drift per service |
| `fake-redundancy` | SVC-0002 primary moved to hub-b | Warning with `missing` (hub-a), `spec` (spoke-2), `unexpected` (hub-b) correlated; drift "redundancy lost"; then hub-b down -> Disaster |
| `new-service-pending` | SVC-0007 added in NetBox only | Disaster (ordered, not delivered); lab-api pushes intent -> OK; removed -> pruned |
| `stale-backup` | SVC-0001 backup peer drift + SVC-0005 backup vc-id drift | both Warning; `impact hub-a` live = down for those two |

## Format

```yaml
name: vcid-typo                 # = file name
title: VC ID typo on a spoke
description: >
  What happens and why (shown by labctl and lab-api).
budget_s: 10                    # e2e: last step -> expected Zabbix service status within this time (contract 9.5;
                                # 12 for power-off steps: the peers only notice after the 3 s advert hold time)
timeout_s: 120                  # how long verify waits for every expectation (default 120)
steps:                          # run in order
  - config:
      host: spoke-1
      patch:
        - {op: set, path: "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id", value: 2010}
expect:
  pws:                          # PW ends: "<oper>/<reason>", "up" (= up/ok), "absent" (no such PW), "unreachable"
    default: up/ok              # every intended PW end not listed below
    SVC-0001 backup: {spoke-1: down/remote-not-configured, hub-b: down/remote-not-configured}
  zabbix:
    services: {default: OK, SVC-0001: Warning}   # Zabbix status per service; "absent" = no such BSM node
    problems:                   # must be open; fields match the problem's tags; name = regex on the event name
      - {host: spoke-1, service: SVC-0001, pw_role: backup, check: spec, severity: Average}
    problems_exact: true        # and no other PW problem (scope l2vpn-pw / l2vpn-unexpected) is open
    correlation:                # correlate(problems) must form exactly this group
      - {service: SVC-0002, checks: [missing, spec, unexpected], hosts: [hub-a, hub-b, spoke-2], window_s: 60}
  drift:
    findings:                   # exactly these findings; "*" = every service; message exact, match = regex
      - {service: SVC-0001, class: config-drift, kind: vcid-mismatch, hosts: [spoke-1], message: "..."}
  impact:                       # labctl impact <host>: intent and live impact per service
    - {host: hub-a, intent: {default: degraded}, live: {default: degraded, SVC-0001: down}}
then:                           # follow-up phases: run by `verify` once `expect` holds (steps, then expect)
  - name: hub-b-down
    budget_s: 10
    steps: [{device: {stop: hub-b}}]
    expect: {zabbix: {services: {default: Warning, SVC-0002: Disaster}}}
```

Every PW end in `pws` is checked in three places: simd `/state` (oper and reason), SNMP `cpwVcOperStatus` of the row
named `<service>:<role>` (or no row for `absent`), and the Zabbix item `netsim.pw.row[<service>,<role>]` on that host
(present/oper; Zabbix only has items for the PWs NetBox expects on a host, and a stopped device keeps its last values).

Steps:

| step | effect |
|---|---|
| `fault: {uplink: H}` / `fault: {ac: {host: H, interface: I}}` (+ `restore: true`) | physical fault key on `core` |
| `cut: [A, B]` (+ `restore: true`) | core path between two PEs |
| `device: {stop: H}` / `{start: H}` | power off/on (`docker stop/start`) |
| `config: {host, patch}` | one RESTCONF commit of the patched running config (an engineer's change) |
| `netbox: {add_service: {id, customer, spoke, vlan, primary_vc, backup_vc}}` | the service's NetBox objects, as `netbox-seed` would create them |
| `netbox: {remove_service: SVC-xxxx}` | delete them again |
| `lab_api: {apply_intent: [hosts]}` | lab-api's dry run -> confirm(plan_id) -> verify, as the bot would |
| `sleep: s` | wait |
