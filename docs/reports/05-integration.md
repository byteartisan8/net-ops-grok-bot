# Session 05 — Integration report

**Date:** 2026-10-20. **Owner paths:** `lab/` (the `netsim-lab` package: labctl, lab-api, scenarios, drift checker,
reset, e2e tests; `lab/compose.yaml`, `lab/Dockerfile`, `lab/lab.mk`, `lab/openapi.json`), `docs/RUNBOOK.md`, the
quickstart section of the root `README.md`, this report. Cross-session fixes are listed [below](#cross-session-fixes).

**Result:** every acceptance criterion passes. **From a clean clone** (a copy of the tree without `.env`, `.venv`, `build/`; `make clean` first): `make env net build up seed e2e` passed completely: **21/21 e2e tests** in 608 s (bring-up `make up` 233 s incl. NetBox's first-start migrations, `make seed` 38 s). Every scenario reaches its expected Zabbix business service status within budget (fault keys, cuts and config changes: 5.0-5.6 s of 10 s; phases with a power-off: 3.5-9.6 s of 12 s), every expectation also holds in simd, SNMP, Zabbix items and the drift checker; trap -> check-now median 3.3 s / max 5.8 s; polling only 4.0 s of 70 s; `labctl status --watch` shows a fault after 0.5 s. `make test` **477 passed** (55 new), `make lint` clean. The e2e run found one integration bug in the root Makefile (`make up`/`make build` broke once no compose fragment was a stub; fixed, regression-tested).

---

## What was built

```
                      ┌─────────────── lab-api 172.30.10.50:8090 (FastAPI, OpenAPI = the bot's tool descriptions) ──────────────┐
 NetBox webhook ─────▶│ /hooks/netbox (HMAC-SHA512) -> debounced zabbix-sync      audit log (JSONL, every write: who/what/diff) │
 the bot ────────────▶│ reads: topology, services, devices, drift, impact, zabbix/problems (+correlation), events, scenarios   │
   X-Lab-Token ──────▶│ writes: faults, devices/{h}/config + apply-intent (dry run -> confirm plan_id -> verify), reset, sync  │
                      └──────────────────────────────────────────┬───────────────────────────────────────────────────────────────┘
 labctl (engineer) ────────────────────────▶ netsim_lab (one library) ─┬─ devices: RESTCONF (config), simd (state, events)
   status --watch, fault, cut, device,                                 ├─ core: fault/cut keys, drift baselines
   config, drift, impact, scenario, reset                              ├─ Docker: power off/on of the six PEs
 make e2e (pytest) ────────────────────────▶                           ├─ NetBox: intent (RO), seeding (RW, scenarios + reset)
                                                                       └─ Zabbix: services, problems, items, check-now, sync
```

| Piece | Where | Notes |
|---|---|---|
| Lab facade | `netsim_lab.lab.Lab` | Devices (pooled httpx, parallel), core (redis), Docker (unix socket, PEs only), NetBox intent (cached on request), Zabbix (rebuilt when the token in `.env` changes). Same code in labctl, lab-api and the tests. |
| Drift checker | `netsim_lab.drift` | `diff_configs(intent, running)` per PE; each difference classified against the PE's baseline; one semantic finding per service (redundancy-lost, vcid-mismatch, peer-mismatch, mtu-mismatch, pw-missing/-unexpected, not-delivered/-removed, ...), fault findings with the physical cause. Pure `analyze()` (unit-tested) + I/O in `Lab.drift()`. |
| Service status | `netsim_lab.status` | NetBox intent + simd state of both ends of each PW + Zabbix service and leaf status; each source may be down on its own. |
| Impact | `netsim_lab.impact` | `blast_radius()` from NetBox + live state of the surviving PWs: "degraded by design" becomes "down" when the backup is already broken. |
| Change loop | `netsim_lab.changes` | Plan (candidate, validation, diff, affected services/hosts, drift before/predicted after, `plan_id`) -> apply (one RESTCONF PUT) -> verify (simd runs the config hash, PW states settled, Zabbix re-polled now, services settled, drift after) -> verdict. |
| Scenarios | `lab/scenarios/*.yaml`, `netsim_lab.scenarios` | Declarative steps + expectations (simd/SNMP/Zabbix items per PW end, Zabbix services, problems, correlation, drift findings, impact) + follow-up phases; pydantic-validated; the same files drive labctl, lab-api and e2e. |
| Reset | `netsim_lab.reset` | Faults cleared, PEs started and ready, NetBox restored (`netbox-seed --prune`) when it differs, golden config where it differs, baselines cleared, zabbix-sync when needed (through lab-api), Zabbix polls every PE, wait green. |
| Correlation | `netsim_lab.correlate` | PW problems grouped by `service` tag and start time window. |
| labctl | `netsim_lab.cli` (Typer + rich) | status `--watch`, fault/cut/device, config get/set/delete/diff, drift, impact, scenario list/show/apply/verify/check/run, reset, events, sync, wait, wait-healthy. |
| lab-api | `netsim_lab.api` | 23 paths, typed request/response models with descriptions, `lab/openapi.json`; image `netsim/lab-api:0.1.0`. |
| Bring-up | `lab/lab.mk`, `netsim_lab.bringup` | `make seed/reset/e2e` wait until every container is healthy (clear error with the culprits' logs). |
| Tests | `lab/tests` (unit, in `make test`), `lab/e2e` (`make e2e`) | see [Tests](#tests). |

## Decisions

**Drift classification needs a baseline.** "config-drift (device != NetBox)" and "intent-only (NetBox changed, device
not yet updated)" are both "device != NetBox"; what differs is who moved. Each PE has a baseline = the canonical
config on which device and NetBox last agreed (key `netsim:lab:baseline:<host>` on `core`, golden when absent). Per
difference: device value == baseline value -> NetBox moved -> `intent-only`; else `config-drift`. Agreements are
recorded when lab-api applies intent and whenever a check finds device == intent; `reset` clears them. This handles
the whole `new-service-pending` lifecycle (added in NetBox -> intent-only; delivered -> agreement; removed from
NetBox -> intent-only "not removed") and keeps a hand-made device change `config-drift`. `core` keeps no data, so a
restart of `core` falls back to golden (documented). Rejected: timestamps (simd's event buffer is volatile) and NetBox
journal/config context (writes would fire the webhook).

**One semantic finding per root cause.** The checker reads the spoke's actual peers before anything else: if both
PWs resolve to the same hub while NetBox wants two, that is one `redundancy-lost` finding that folds in the
missing/unexpected/repointed PWs behind it as details. Messages use the prompt's wording exactly
(`SVC-0002: both PWs terminate on hub-b (intent: hub-a + hub-b) — redundancy lost`,
`SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001`). Down PWs with no config difference become `fault`
findings naming the cause (fault key, cut, stopped container); `labctl drift` exits 1 only on config-drift or
intent-only.

**Approve means the exact plan.** A dry run returns a `plan_id` = hash of (host, running config, candidate). Applying
needs `confirm=true` and that id; if the device or NetBox changed in between, the ids differ and lab-api answers 409
with the new plan. So what the bot (or its human) approved is byte for byte what gets committed. Every proposal,
refusal and apply is in the audit log. Paths in patches are the drift paths, so a finding's `paths` turn directly into
a fix; RFC 6902 JSON Patch is accepted too.

**Verification waits for Zabbix, not just the device.** After the commit, lab-api waits until simd reports the new
`config_hash` and the PW states of every affected host stop changing, asks Zabbix to execute the walk on those hosts
now, waits until the walks are fresh and the business services and PW problems stay unchanged for 6 s (more than one
service-manager cycle), then reports simd, Zabbix and drift together with a verdict (`ok`, `degraded`, `unverified`).
Measured 10-15 s per change.

**lab-api reads `.env` live.** The NetBox and Zabbix tokens are written by the bootstraps after `make up`; a container
started with `env_file` would keep empty or stale tokens (03's report notes this). lab-api mounts the repository
read-only and its settings re-read `.env` on each use. A single-file bind mount is not enough: the bootstraps replace
`.env` atomically (new inode), which a file mount never sees.

**Webhook -> sync is debounced and serialized.** One NetBox change fires several webhooks (03); a sync with refresh
takes 15-25 s (04). One thread runs `zabbix-sync` 2 s after the last request; a request during a run schedules exactly
one more run; `POST /sync {"wait": true}` and `reset` join the same queue, so two syncs never race in Zabbix.

**labctl is the engineer, lab-api is the bot.** labctl acts directly (RESTCONF, core keys, Docker), like hands on the
routers: its changes are not audited by lab-api and create real drift. lab-api is the audited, token-protected path.

**Scenarios are data.** Every expectation a test checks is in the YAML (including "every PW end not listed must be
up/ok" as an explicit `default:` key). Each expected PW end is checked in simd, by SNMP walk and in Zabbix's
`netsim.pw.row` item. Follow-up phases (`then:`) cover the prompt's "verify step" (fake-redundancy: stop hub-b) and
new-service-pending's deliver/remove stages.

## Cross-session fixes

| File (owner) | Change | Why |
|---|---|---|
| `zabbix/tools/src/netsim_zabbix/js/observed_lld.js`, `template.py` (04) | The observed view's discovery adds `{#SERVICE}`/`{#ROLE}` (split from `cpwVcName`); the `unexpected` trigger is tagged `service` and `pw_role`. Template regenerated (`zabbix/template/netsim_l2vpn_pe_snmp.yaml`) and imported with `zabbix-provision`. | fake-redundancy must correlate `missing`, `spec` and `unexpected` "from the API data alone (same service tag)"; `unexpected` had no service tag. Still `scope=l2vpn-unexpected`, so it stays out of the BSM. |
| `zabbix/tools/tests/test_js.py`, `test_template.py` (04) | Assert the new macros and tags. | Keep 04's tests covering the change. |
| `Makefile` (01), one line in `require_services` | `stubs="$$( { grep -l ... \|\| true; } \| xargs ... )"` | **`make up` and `make build` failed (exit 2) once every compose fragment was real.** The stub note runs under `bash -eu -o pipefail`; with no stub left, `grep -l` exits 1, the pipeline fails and `set -e` aborts the recipe. It only appeared when lab/compose.yaml, the last stub, was replaced; found by the e2e persistence test (`make down && make up`). Regression test: `lab/tests/test_bringup.py` (fails without the fix). |
| `README.md` (01) | Quickstart section. | Prompt task 7. |
| `docs/CONTRACT.md` | Five change-log entries (below). | Contract rule. |
| `uv.lock` | typer, rich (+ shellingham) for netsim-lab. | New dependencies. |

No other session's code needed changes: the device RESTCONF/simd APIs, the NetBox intent/seed library and
`netsim_zabbix.sync`/`status` were used as they are.

## Contract changes (docs/CONTRACT.md §13, 2026-10-20, session 05-integration)

| § | Change |
|---|---|
| 9.2 | The `unexpected` problem is tagged `service` and `pw_role` (observed LLD macros `{#SERVICE}`/`{#ROLE}` from `cpwVcName`); still `scope=l2vpn-unexpected`, outside the BSM. |
| 7 | Lab-owned `core` keys `netsim:lab:baseline:<host>`: the drift baseline (last agreed config; absent = golden; volatile with `core`). |
| 4/11 | lab-api: container port 8090 (172.30.10.50, host 127.0.0.1:8090); repository mounted read-only so `.env` is read live; Docker socket for PE power only; `X-Lab-Token` for writes, `X-Lab-Actor` for the audit log; device changes need the dry run's `plan_id` (409 if stale). |
| 3/12 | `make seed`, `make reset`, `make e2e` wait for healthy containers first (`lab-wait-healthy`); `make build` builds lab-api. |
| 9.5 | e2e timing definitions; budgets: 10 s for fault keys, cuts and config changes; **12 s for a device power-off** (3 s advert hold time + Zabbix 8.0's 5 s check-now grid; measured median 7.9 s, max 9.7 s over 11 samples); check-now median <= 5 s / max <= 7 s (04); polling only 70 s. |

## Pinned versions

| Thing | Pin | Verified |
|---|---|---|
| typer | 0.27.2 | PyPI: newest release older than two weeks on 2026-10-19 (0.27.3 is from 2026-10-06) |
| rich | 15.0.0 | PyPI (2026-04-12) |
| FastAPI / uvicorn / httpx / redis / PyYAML | 0.141.1 / 0.54.0 / 0.28.1 / 8.1.0 / 6.0.3 | workspace pins (02, 03, 04) |
| lab-api image | `netsim/lab-api:0.1.0` on `python:3.12.15-slim-trixie`, uv 0.13.1 | same base and uv as the device image |

## How to run

```bash
make env net build up seed     # bring-up (seed waits for healthy containers)
uv run labctl status --watch   # live view
uv run labctl scenario run fake-redundancy
make reset
make e2e                       # ~10 min
```

Everything else: [`docs/RUNBOOK.md`](../RUNBOOK.md) and [`lab/README.md`](../../lab/README.md).

## Tests

**Unit tests** (`make test`, no containers): **477 passed** = 165 foundation + 113 devices + 76 NetBox + 68 Zabbix +
**55 lab**:

| File | Tests | What |
|---|---|---|
| `test_drift.py` | 11 | every scenario's drift semantics on hand-made inputs: exact messages, classes, hosts, paths; baseline-based intent-only vs config-drift; faults (uplink, power-off, double failure); device-level drift; baseline updates |
| `test_configpath.py` | 12 | path parsing (keys containing `/`), RESTCONF paths/bodies, set/delete round trip from a drift path, RFC 6902, validation errors vs warnings (duplicate PW) |
| `test_scenario_files.py` | 11 | all 10 prompt scenarios exist; every host, service, config patch (applied to golden and validated), PW key and budget in them is real |
| `test_api.py` | 15 | every write endpoint rejects missing/wrong tokens; 503 without a configured token; writes (incl. failed ones) audited with the actor; webhook HMAC (bad/missing/good), debounced sync, journal; `POST /sync` waits for its run; `lab/openapi.json` current; every operation has a summary and every write declares `X-Lab-Token` |
| `test_correlate_and_syncer.py` | 4 | fake-redundancy correlation; time windows; 20 webhooks -> 1 sync, a request during a run -> exactly one more; sync errors recorded |
| `test_bringup.py` | 2 | the Makefile stub check works with no stub left (fails without the fix); every compose fragment is real |

**End-to-end** (`make e2e`, running lab): **21 tests**:

| File | Tests |
|---|---|
| `test_10_api.py` | served OpenAPI = `lab/openapi.json`; reads (topology, services, service detail, drift, impact, problems, events, device running/state/intent, scenarios, scenario check, 404s); every write rejects missing/wrong tokens and changes nothing; the change loop (dry run changes nothing -> wrong `plan_id` 409 -> confirm -> verification `degraded` with simd/Zabbix/drift evidence -> apply-intent -> `ok` -> no drift) with the exact audit trail; `POST /reset` |
| `test_20_scenarios.py` | the 10 scenarios: reset -> steps -> every expectation (simd, SNMP walk, Zabbix `netsim.pw.row` items, Zabbix services, problems/correlation, drift, impact) -> budget -> `labctl drift` exit code and messages / `labctl impact` as the file says -> follow-up phases -> reset -> green |
| `test_30_timing.py` | trap path proof (after a scheduled poll, the trap's check-now refreshes the walk ~55 s before the next poll); check-now latency (8 traps, median/max); polling only (all PEs recreated with `TRAP_TARGETS=""`, spoke-isolated within 70 s, no trap reached Zabbix) |
| `test_40_status_watch.py` | `labctl status --watch` shows a fault within 2.5 s, the restore within 2.5 s and the Zabbix status within 12 s |
| `test_50_persistence.py` | vcid-typo, `make down && make up`, the typo is still on spoke-1 and the drift check still reports it |
| `test_90_final.py` | the run leaves the lab green, drift-free, fault-free |

Results on the existing lab: 21/21 (19 in the first full run; the persistence test then found the Makefile bug,
see [Cross-session fixes](#cross-session-fixes), and passed with the fix together with the final check).

**Clean clone** (2026-10-20, 08:21-08:36): the lab was stopped and every volume plus `.env` backed up to `build/volume-backup-20261020-082134/` (restore steps in its `RESTORE.md`); the tree was copied without ignored files; in the copy `make clean CONFIRM=yes` (1 s), `make env` (0 s), `make net` (0 s), `make build` (14 s, Docker cache), `make up` (233 s), `make seed` (38 s: both NetBox tokens created, 126 NetBox changes, Zabbix bootstrap 12 changes + token, `zabbix-provision` 42 changes in 20.9 s) and `make e2e` (610 s: **21 passed**). Afterwards the running lab was re-pointed at the working repository (its `.env` now holds the clean run's secrets) and `make reset` was green.

## Measured timings

Timing table of the clean-clone `make e2e` (also `build/lab-e2e/timings-clean-clone.json`); "last step -> Zabbix service status" is contract 9.5's fault -> business service status.

| Path | Measured (s) | Budget (s) | OK |
|---|---|---|---|
| initial reset -> green | 2.34 |  |  |
| lab-api config apply -> verified (simd + Zabbix settled) | 10.85 |  |  |
| lab-api apply-intent -> verified OK | 14.05 |  |  |
| lab-api POST /reset (one fault) -> green | 5.21 |  |  |
| healthy: every expectation holds | 1.43 |  |  |
| primary-fail: last step -> Zabbix service status | 4.96 | 10.0 | yes |
| primary-fail: every expectation holds | 6.16 |  |  |
| hub-down: last step -> Zabbix service status | 9.61 | 12.0 | yes |
| hub-down: every expectation holds | 12.73 |  |  |
| spoke-isolated: last step -> Zabbix service status | 5.63 | 10.0 | yes |
| spoke-isolated: every expectation holds | 6.81 |  |  |
| double-failure: last step -> Zabbix service status | 3.54 | 12.0 | yes |
| double-failure: every expectation holds | 6.77 |  |  |
| vcid-typo: last step -> Zabbix service status | 5.16 | 10.0 | yes |
| vcid-typo: every expectation holds | 6.44 |  |  |
| mtu-mismatch: last step -> Zabbix service status | 5.15 | 10.0 | yes |
| mtu-mismatch: every expectation holds | 6.27 |  |  |
| fake-redundancy: last step -> Zabbix service status | 5.49 | 10.0 | yes |
| fake-redundancy: every expectation holds | 20.87 |  |  |
| fake-redundancy / hub-b-down: last step -> Zabbix service status | 6.57 | 12.0 | yes |
| fake-redundancy / hub-b-down: every expectation holds | 7.16 |  |  |
| stale-backup: last step -> Zabbix service status | 5.44 | 10.0 | yes |
| stale-backup: every expectation holds | 7.50 |  |  |
| new-service-pending: last step -> Zabbix service status | 22.68 | 60.0 | yes |
| new-service-pending: every expectation holds | 24.12 |  |  |
| new-service-pending / deliver: last step -> Zabbix service status | 0.68 | 60.0 | yes |
| new-service-pending / deliver: every expectation holds | 1.39 |  |  |
| new-service-pending / remove: last step -> Zabbix service status | 0.49 | 60.0 | yes |
| new-service-pending / remove: every expectation holds | 1.44 |  |  |
| trap path: fault -> trap item in Zabbix | 0.13 |  |  |
| trap path: fault -> walk lastclock (check-now) | 3.93 | 7.0 | yes |
| trap path: walk refreshed after the scheduled poll (next poll at +60 s) | 4.00 | 60.0 | yes |
| trap -> walk re-polled (check-now), 8 traps (median budget 5.0) | median 3.28, max 5.78 (n=8) | 7.0 | yes |
| polling only: fault -> Zabbix service status | 4.02 | 70.0 | yes |
| status --watch: fault -> live state shown | 0.51 | 2.5 | yes |
| status --watch: restore -> live state shown | 0.99 | 2.5 | yes |
| status --watch: fault -> Zabbix status shown | 4.51 | 12.0 | yes |
| make down && make up -> every container healthy | 29.44 |  |  |

Power-off decomposition (11 extra samples, `docker stop hub-b`): container off -> peers' PW down 2.4-2.5 s (hold time), -> trap item in Zabbix 2.6-3.7 s, trap -> walk re-polled 2.1-5.2 s (task-manager grid), off -> all services Warning 5.4-9.7 s, median 7.9 s.

Other measurements: drift check of the whole lab 0.5-1.3 s; `GET /services` 0.04 s (cached intent) to 1 s; verified config change 10-15 s per device; `make reset` 1.3-8 s (up to ~30 s after a power-off); `make down && make up` -> all containers healthy 29-34 s.

## Known limitations

- **Power-off -> business service status is ~10 s worst case** by construction: the peers notice after the 3 s
  advert hold time (contract 7), then the trap's check-now waits for Zabbix 8.0's fixed 5 s task-manager grid (04).
  Measured 5.4-9.7 s (median 7.9 s, 11 samples); the e2e budget for power-off phases is 12 s. Fault keys, cuts and
  config changes stay within 10 s (measured 3.0-6.2 s over all runs).
- An **added** PW on a device (fake-redundancy's `unexpected`) appears only after that device's next regular walk
  (<= 60 s; 04's limitation): fake-redundancy's service status is Warning in ~5 s, its third problem can take ~30 s.
- **Drift baselines live on `core`**, which keeps no data: after a restart of `core` (e.g. `make down && make up`) a
  recorded agreement is forgotten and the baseline is golden again. Only matters after intent was applied that
  differs from golden (e.g. a delivered new service): a later NetBox-side change to it is then classified
  `config-drift` instead of `intent-only` until device and NetBox agree again.
- lab-api runs as root with the host's Docker socket (it powers the PEs off/on; the code refuses any other container)
  and reads `.env` through a read-only mount of the repository: lab only. RESTCONF is plain HTTP without auth (02).
- lab-api's image has no SNMP client: `GET /scenarios/{name}/check` checks simd, Zabbix, drift and impact; the SNMP
  part of the expectations is checked by labctl and the e2e tests.
- labctl acts directly on the lab and is not audited by lab-api (by design: the engineer's hands, not the bot's).
- A verified change takes 10-15 s per device (Zabbix re-poll + service settle); a three-device apply-intent rollout
  verifies each device in turn, and the first ones can report `degraded` while their peers are not updated yet.
- If lab-api is down when NetBox changes, the webhooks are lost: `labctl sync` (or `make reset`, which syncs when
  Zabbix differs) catches up.
- `labctl status --watch` re-reads NetBox intent every 10 s (device and Zabbix state every second).
- The clean-clone run reused the host's Docker images and build cache (the Clixon and Zabbix image builds from
  scratch were verified by sessions 02 and 04; `make build` itself ran).

## Notes for the bot (next step)

- Start from `GET /openapi.json` (also `lab/openapi.json`): every endpoint and field is described for tool use.
- Investigation: `GET /services` (one call: intent + live + Zabbix), `GET /zabbix/problems?service=` (with
  correlation groups), `GET /drift` (semantic findings with `paths`), `GET /impact/{host}`, `GET /events?since=`.
- Remediation: `POST /devices/{host}/apply-intent` (NetBox wins) or `/config` with a patch built from a finding's
  `paths`; show the dry run to the human, then confirm with its `plan_id`; report the `verification.verdict`.
- Identify yourself with `X-Lab-Actor`; everything you write is in `GET /audit`.
