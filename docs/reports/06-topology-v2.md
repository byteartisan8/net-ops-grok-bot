# Session 06 — Topology v2 report (second region, new customers, unprotected services)

**Date:** 2026-10-20. **Scope (agreed in conversation, no prompt file):** add topology variety *before* the Grok bot is
built on the lab, so the bot has to reason about blast radius and SLA tiers instead of pattern-matching one shape:
a second region with its own hub pair, new customers (one spanning both regions, a mixed-tier spoke), a bronze tier
with unprotected (single-homed) services, and scenarios built on them. Session 05 was committed first (`70ee43d`) on
branch `session-06-topology-v2`; this session's work is on top of it, uncommitted.

**Result:** every check passes. `make test` **504 passed**, `make lint` clean, every generated file current
(`contract-check`, `devices-check`, `zabbix-check`, `lab-check`, `zabbix-dashboard check`). The live v1 lab was upgraded
in place (`make build up seed reset`) and **`make e2e` passed 23/23** (all 12 scenarios) in 803 s. Then the
**fresh-volume acceptance run**: back up all volumes, `make clean`, `make net build up seed e2e`. Its first attempt found
**two bugs** outside the topology code: NetBox's first start outlasted its healthcheck grace window (`make up`
failed), and the Zabbix trap triggers produced a second, phantom trap event and re-poll per trap (it made one e2e
latency sample 12.7 s). Both are fixed (see [Cross-session fixes](#cross-session-fixes-and-changes)). The final run
from `make clean` passed completely: `make up` 505 s (NetBox's first-start migrations), `make seed` 67 s, **`make e2e`
23/23 in 766 s**, every fault -> business service time within budget (4.5-9.5 s of 10/12 s), trap -> check-now
median 4.6 s / max 5.6 s. `.env` was kept for these runs (no `make env` from a fresh clone), so the Zabbix Admin
password you chose (`zabbix`) stays. Volume backups: `build/volume-backup-20261020-140250/` (the upgraded lab before
the first clean run) and `build/volume-backup-20261020-144111/` (RESTORE.md inside).

---

## What was built

### The lab, v2

```
             North region                                        South region
   hub-a (DC-A, primary)   hub-b (DC-B, backup)        hub-c (DC-C, primary)   hub-d (DC-D, backup)
        │  │  │  │             │  │  │  │                     │  │                     │  │
   spoke-1  spoke-2  spoke-3  spoke-4                      spoke-5            spoke-6
   (BR-01)  (BR-02)  (BR-03)  (BR-04)                      (BR-05)            (BR-06)
```

| | v1 (sessions 01-05) | v2 |
|---|---|---|
| PEs | 6 (hub-a/b, spoke-1..4) | 10 (+ hub-c .13, hub-d .14, spoke-5 .25, spoke-6 .26) |
| NetBox regions | `Lab` | `Lab` with child regions `North`, `South` |
| Customers (tier) | A, B gold; C, D silver | + E gold (services in both regions), F bronze |
| Services / PWs | 6 / 12 | 11 / 20 (SVC-0009, SVC-0010 unprotected: primary PW only) |
| SLA tiers | gold 99.95, silver 99.9 | + bronze 99.5 |
| Status rules | `redundant-pair` | + `unprotected` (one PW: down = Disaster) |
| Scenarios | 10 | 12 (+ `region-down`, `maintenance-by-tier`) |

New services (full table in `docs/CONTRACT.md` §5): SVC-0007 E/spoke-5 (hub-c/hub-d), SVC-0008 E/spoke-1
(hub-a/hub-b), SVC-0009 F/spoke-5 (hub-c only), SVC-0010 F/spoke-4 (hub-a only), SVC-0011 C/spoke-6 (hub-c/hub-d).
spoke-4 now carries gold (B), silver (D) and bronze (F); Customer-E and Customer-C have services in both regions.

What a hub failure means now (the reason for the change):

| Event | v1 | v2 |
|---|---|---|
| hub-a uplink down | all 6 Warning | 7 North protected Warning, SVC-0010 **Disaster**, South OK |
| hub-b powered off | all 6 Warning | 7 North protected Warning, SVC-0010 and South OK |
| both North hubs gone | all 6 Disaster | 8 North Disaster, **South OK** |
| both South hubs gone | (n/a) | 3 South Disaster (customers C, E, F Disaster in the BSM), North OK |
| hub-c maintenance | (n/a) | gold/silver degraded, bronze **down**; `impact` predicts it, `by_tier` counts it |

### Per component

| Component | Change |
|---|---|
| contract / `lib/netsim_common` | `topology.yaml` v2; model: optional `pseudowires.backup` (`Service.roles`, `.protected`; `pw(role)` raises `KeyError` for an absent backup), `regions` (+ `region_of()`), `SlaTier.BRONZE`; BSM: `StatusRules.UNPROTECTED`, `ServiceRule` (+ `describe()`), `status_rules_for()` (derived from the number of PW leaves, enforced by `BsmService`); golden configs (10), schemas regenerated |
| devices | `devices/compose.yaml` regenerated (4 PEs more); simd unchanged (it never assumed two hubs) |
| NetBox | seeder: child regions in their own phase `child-regions` (a parent must exist before a child references it: bulk creates resolve references first), sites in their child region, `Bronze` choice, L2VPNs per present role; `service_detail()` reports the real status rules; test fixture re-recorded from a v2-seeded NetBox |
| Zabbix | `bsm_sync`: rules per status rule (`service_status_rules()`), so an unprotected service goes Disaster when its PW is down; SLA `L2VPN bronze` (from `sla_tiers`); dashboard: three SLA reports; fixtures re-rendered (10 hosts); **fix**: the dashboard comparison is order-independent (see below) |
| lab | impact: `sla_tier`, `status_rules` per service, `by_tier`; status/`/services`/`/topology`: `status_rules`, `region`, `regions`; `labctl status`/`impact` show protection and tier; scenario format: `zabbix.customers`, drift `services: [...]`, `add_service` hubs + optional backup; scenarios re-derived; lab-api descriptions no longer say "primary = hub-a"; `lab/openapi.json` regenerated |
| tools | `gen_topology.py`: defaults reproduce v2; new spokes join spoke-1's region; generated services use their spoke's hub pair; bronze customers get unprotected services |

## Decisions

- **Variety inside one topology, not several switchable topologies.** Several profiles would each need their own
  golden configs, scenario expectations and e2e run. Scale testing stays with `tools/gen_topology.py`.
- **Regions are NetBox child regions** (`North`, `South` under `Lab`), listed in `topology.yaml` `regions`. The VLAN
  group stays scoped to `Lab` (VLAN IDs are unique lab-wide by the `100*c + j` scheme). lab-api's `/topology` returns
  them, so the bot can say "the South region" instead of listing hubs.
- **Unprotected = no backup PW**, independent of the tier in the model; in the lab (and in the generator) the bronze
  customer's services are the unprotected ones.
- **The status rule follows the number of PW leaves** (`redundant-pair` for two, `unprotected` for one), derived by
  one function and enforced by the model. Under `redundant-pair` (≥2 children ≥ High → Disaster) a one-PW service
  could never reach Disaster: a dead bronze service would have looked merely degraded. Consequence: a protected
  service of which NetBox currently holds one L2VPN (e.g. while an operator deletes and re-adds the backup) is
  `unprotected` for that time, which is what NetBox then says.
- **Hub roles stay positional per region**: primary to hub-a/hub-c, backup to hub-b/hub-d; VC IDs `1000+n`/`2000+n`
  lab-wide. The topology model only requires the two PWs of a service to end on different hubs.
- **`new-service-pending` orders SVC-0012** (VC IDs 1012/2012, VLAN 701, hub-a/hub-b given explicitly), SVC-0007
  being a topology service now.
- **Scenario expectations stay explicit** (no "every North service" selectors): a reader, and the bot learning from
  the files, sees exactly which service ends in which state. Only `services: [...]` was added for drift findings so a
  shared message is not repeated eight times.

## Cross-session fixes and changes

| Owner | File | Change | Why |
|---|---|---|---|
| 01 | `lib/netsim_common/src/netsim_common/{topology,bsm,enums}.py`, `contract/*` | model and contract v2 (above) | the change itself |
| 01 | `tools/gen_topology.py`, `tests/test_{topology,bsm,golden,yang}.py`, `lib/netsim_common/tests/test_cli_and_paths.py` | region/tier-aware generator; tests on v2 | topology v2 |
| 02 | `devices/compose.yaml` (generated), `devices/devices.mk` (help text), `devices/simd/tests/test_simd_pair.py`, `devices/tests/integration/test_devices.py` | 10 PEs; tests derive PW counts from the topology (spoke-1 has 4 PWs, hub-a 8) | topology v2 |
| 03 | `netbox/tools/src/netsim_netbox/seed/desired.py`, `intent/{queries,__init__}.py`, tests + fixture, `netbox/tests/integration/test_netbox.py` | child regions, Bronze, single-PW services, real status rules; tests on v2 (the integration test orders SVC-0012) | topology v2 |
| 04 | `zabbix/tools/src/netsim_zabbix/bsm_sync.py`, `zabbix/dashboards/l2vpn_services.yaml`, fixtures, tests, `zabbix/tests/integration/*` | `unprotected` rules, bronze SLA widget; tests on v2 | topology v2 |
| 03 | `netbox/compose.yaml`, `netbox/netbox.mk` | **bug fix**: NetBox's healthcheck `start_period` 300 s → 900 s, `make netbox-up` waits 1200 s (was 420 s). The fresh-volume run's first `make up` failed: NetBox's first start (every migration) took 337 s with the 10 PEs and Zabbix booting alongside, past the grace window, so compose saw `netbox` unhealthy and never started `netbox-worker`. The next clean run needed 494 s (migrations alone 7 min 48 s), hence 900 s rather than 600 s. The window only delays an "unhealthy" verdict (`start_interval` 2 s reports a healthy NetBox at once). | found by the fresh-volume acceptance run |
| 04 | `zabbix/tools/src/netsim_zabbix/dashboard.py` | **bug fix**: `_norm` sorted widgets by position only when every widget had `x` and `y`, but Zabbix's export leaves them out when 0, so the comparison depended on list order; after importing a dashboard with a new widget into an existing one, `zabbix-dashboard check` said "differs" forever (the new widget is exported last). Widgets are now recognized by `type`/`width`/`height` and missing coordinates count as 0. Regression test in `test_component.py`. | found by this session's checks |
| 04 | `zabbix/tools/src/netsim_zabbix/{template.py,js/check_now.js,bootstrap.py,status.py}`, template YAML, tests | **bug fix**: the trap triggers were `length(last(trap))>0 and nodata(trap,{$NETSIM.TRAP.HOLD})=0` in *multiple* event mode. `nodata()` makes a trigger time-based, so Zabbix's timer re-evaluated it every 30 s, and in *multiple* mode every PROBLEM evaluation is another event: each trap produced a second "SNMP trap … (hint, re-polling now)" event at the next timer tick (whole-second timestamps, :08/:38) and a second re-poll. Seen in every trace of the fresh run; it made the e2e latency test pair a walk with the wrong trap once (12.7 s against the 7 s ceiling). Now: `length(last(trap))>0`, OK event generation *none*, manual close allowed, and the check-now webhook closes its event (`event.acknowledge`, close + message) after scheduling the re-poll; `{$NETSIM.TRAP.HOLD}` removed. Verified live: 4 traps → 4 events, each closed after ~1 s, no further events over two timer ticks, 0 open trap problems. | found by the fresh-volume acceptance run |
| 05, 04 | `lab/e2e/test_30_timing.py`, `zabbix/tests/integration/test_zabbix.py` | the trap → check-now latency tests wait for the trial's own trap first, then for the first walk after it (they took the first walk after the fault, which a stray re-poll could satisfy) | robust pairing |
| 05 | `lab/` | impact/status/API/scenario changes above, scenarios, e2e tests (counts derived from the topology), `lab/compose.yaml` comment | topology v2 |

## Contract changes (docs/CONTRACT.md §13, 2026-10-20, session 06-topology-v2)

| § | Change |
|---|---|
| 4, 5, 10 | second region: hub-c, hub-d, spoke-5, spoke-6; child regions North/South; Customer-E (gold), Customer-F (bronze); SVC-0007 … SVC-0011; VLAN scheme `100*c + j` written down; §6 example (spoke-1 now also carries SVC-0008) |
| model | `pseudowires.backup` optional (unprotected services), `regions`, `bronze`; additive, `schema_version` stays 1 |
| 9.3, 9.4 | `status_rules` `unprotected` (derived from the leaf count, enforced); SLA `L2VPN bronze` 99.5 %; dashboard with three SLA reports |
| 10 | seeder phase `child-regions`; Bronze choice; one L2VPN per present PW role |
| lab-api | additive fields: `/topology` `regions` + device `region` + service `status_rules`; `/services` `status_rules`; `/impact` `sla_tier`, `status_rules`, `by_tier` |
| tools | `gen_topology.py` defaults reproduce v2; region and tier aware |
| 12 | NetBox first-start grace window 900 s (bring-up on a clean host) |
| 9.1 | trap triggers: one event per trap, closed by the check-now webhook (no `nodata()`, `{$NETSIM.TRAP.HOLD}` removed) |

## How to run

On an existing (v1) lab, the upgrade is: `make build up seed reset` (new images, the 4 new PEs boot from their golden
config, NetBox and Zabbix get the new objects, the existing PEs get the v2 golden config). From scratch: as before,
`make env net build up seed`.

```bash
uv run labctl status                       # 11 services; SVC-0009/SVC-0010 marked "unprotected"
uv run labctl impact hub-c                 # per service and SLA tier; "by SLA tier: bronze: 1 down, ..."
uv run labctl scenario run region-down     # South region down: only its 3 services (and customers) Disaster
uv run labctl scenario run maintenance-by-tier
make e2e                                   # every scenario end to end
```

## Tests

| Suite | Result |
|---|---|
| `make test` (unit, no lab) | **504 passed** (session 05: 477). New: region/topology checks, `unprotected` status rules and their validation, generator scale checks (South spokes keep hub-c/hub-d, bronze services unprotected), drift for a region outage and unprotected services, dashboard order-independence, check-now closing its event; many existing tests now derive counts from the topology instead of hard-coding 6/12 |
| `make lint` | ruff check + format, pyang --strict, yanglint (10 golden configs): clean |
| `make e2e`, upgraded lab (v1 -> v2 in place) | 23/23 in 803 s |
| `make e2e`, fresh volumes, attempt 1 | 22/23: `test_trap_to_check_now_latency` max 12.7 s (phantom trap events, fixed) |
| `make e2e`, fresh volumes, final | **23/23 in 766 s** |
| NetBox fixture | re-recorded from a fresh v2 seed; a second fresh seed recorded byte-identical (seeding is deterministic) |
| integration suites (`devices-`, `netbox-`, `zabbix-test-integration`) | updated for v2, **not run** in this session |

## Measured timings

Fault -> business service status is timed from the end of the last step to the first `service.get` showing every
expected status (contract 9.5). Left: the upgraded lab (old trap triggers); right: the final fresh-volume run.

| Path | upgraded lab | fresh lab (final) | budget |
|---|---|---|---|
| primary-fail: last step -> Zabbix service status | 4.9 | 5.9 | 10 |
| hub-down (power-off) | 8.5 | 8.2 | 12 |
| spoke-isolated | 5.2 | 5.4 | 10 |
| double-failure | 4.4 | 4.5 | 12 |
| vcid-typo | 5.5 | 5.2 | 10 |
| mtu-mismatch | 5.0 | 5.0 | 10 |
| fake-redundancy | 5.7 | 1.5 | 10 |
| fake-redundancy / hub-b-down (power-off) | 4.9 | 5.9 | 12 |
| stale-backup | 5.7 | 5.3 | 10 |
| new-service-pending (NetBox -> webhook -> sync -> Disaster) | 29.7 | 18.9 | 60 |
| new-service-pending / deliver, / remove | 1.2, 1.6 | 0.6, 0.7 | 60 |
| **region-down** | 4.9 | 5.3 | 10 |
| **region-down / hub-c-restored** | 4.7 | 7.2 | 10 |
| **maintenance-by-tier / hub-c-off** (power-off) | 8.7 | 9.5 | 12 |
| trap path: fault -> walk lastclock (check-now) | 3.9 | 3.9 | 7 |
| trap -> walk re-polled, 8 traps | med 2.5 / max 5.5 | med 4.6 / max 5.6 | med 5, max 7 |
| polling only (traps disabled): fault -> service status | 11.4 | 1.4 (a scheduled walk right after the fault) | 70 |
| status --watch: fault -> live / Zabbix shown | 1.0 / 3.0 | 0.2 / 3.2 | 2.5 / 12 |
| lab-api config apply -> verified; apply-intent -> verified | 15.2; 13.7 | 15.5; 18.4 | |
| make down && make up -> every container healthy | 42.3 | 34.9 | |

Raw files: `build/lab-e2e/timings-v2-upgraded-lab.json`, `build/lab-e2e/timings-v2-fresh-lab.json`; logs
`build/lab-e2e/e2e-v2-*.log`.

## Known limitations

- Regions exist in NetBox and in lab-api's `/topology`, not in Zabbix (hosts carry `site`, not `region`): filter
  Zabbix problems by host, or map hosts to regions through `/topology`.
- The BSM has no region level (root → customer → service → PW, as the contract defines); a regional outage shows up
  as several customers going Disaster at once, and `GET /zabbix/problems` groups problems per service.
- The integration suites (`make devices-test-integration`, `netbox-test-integration`, `zabbix-test-integration`)
  were updated for v2 (counts, SVC-0012 instead of SVC-0007, trap pairing) but not run in this session; `make test`
  and `make e2e` were.
- NetBox's first start (database migrations) takes 4-8 minutes on this host while everything else boots; `make up`
  on empty volumes takes that long. The healthcheck grace window is 900 s.
- `tools/gen_topology.py` still adds spokes only to the region of spoke-1 and never adds hubs.

## Notes for the bot

- Do not assume "primary = hub-a": read the hubs of a service from `GET /topology` or `GET /services/{id}`.
- `status_rules` tells you what a PW failure means: `redundant-pair` (one PW down = degraded) or `unprotected` (the
  only PW down = service down). `GET /impact/{host}` gives `live_impact` per service with its `sla_tier`, and
  `by_tier`; rank maintenance or incident impact by tier, not by count.
- A customer node in Zabbix is "most critical of its services": Customer-E Disaster can mean one region only
  (`region-down`: SVC-0007 down, SVC-0008 fine).
