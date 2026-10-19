# Session 02 — Device simulator report

**Date:** 2026-10-19. **Owner paths:** `devices/` (image, simd, snmpsim module, Clixon config/plugins, compose,
`devices.mk`, tests), this report. Contract: §2 pins + five change-log entries (below). `lib/netsim_common` and
`contract/` artifacts were used as-is (no edits); `uv.lock` gained the simd dependencies.

**Result:** every acceptance criterion passes. Full topology from `docker compose up -d` to 7/7 containers healthy
and 12/12 PWs up on both ends in **9.5 s** (first boot, golden load included); 113 unit tests, 7 container
scenarios, every contract §9 timing within budget.

---

## What was built

```
                 ┌──────────────────────── netsim-pe container (one per device) ────────────────────────┐
 RESTCONF :8080 ─┤ clixon_restconf ──┐                                                                    │
 docker exec ────┤ clixon_cli ───────┼─▶ clixon_backend (netsim-device YANG, datastores on the volume)    │
   clixon_cli    │                   │      └ netsim_backend.so: netconf-config-change on stream NETCONF  │
                 │                   │                     │ SSE /streams/NETCONF + 2 s hash poll         │
 simd API :9000 ─┤ simd (asyncio) ◀──┘─────────────────────┘                                              │
                 │   evaluate (contract §7) ─▶ SQLite MIB view ─▶ snmpsim + variation "netsim" ─┤ SNMP :161/udp
                 │   traps (pysnmp) ───────────────────────────────────────────────────────────┤ ─▶ TRAP_TARGETS
                 └── supervisord (PID 1) ─────────────────────────────────────────────────────────────────┘
                          │ adverts (1 s / 3 s TTL), netsim:events, faults, cuts, keyspace notifications
                          ▼
                    core (Valkey 8.1.10, 172.30.10.5)
```

| Piece | Where | Notes |
|---|---|---|
| Image `netsim/netsim-pe:0.1.0` | `devices/Dockerfile` | 3 stages: Clixon/CLIgen 7.9.0 from source (commits verified, same as 01's check image) + two C plugins built with `-Werror`; venv from `uv.lock` (`uv sync --frozen --package netsim-simd`); runtime on `python:3.12.15-slim-trixie`. 306 MB (61 MB are the net-snmp client tools). Configured only by env. HEALTHCHECK = RESTCONF + SNMP `sysName.0` + simd `/health`. |
| Process supervision | `devices/supervisord.conf`, `entrypoint.sh` | supervisord as PID 1 (rationale below); `supervisorctl status` works in the container. |
| Clixon | `devices/clixon/` | `/etc/clixon.xml`: startup mode `running` (drift survives), native RESTCONF HTTP/1.1 on 8080, auth none, NACM off, autocli for `netsim-device` only. `netsim_backend.so`: RFC 6470 `netconf-config-change`. `netsim_cli.so` + `netsim.cli`: `set/delete/commit/show compare/show running-config` (autocli) and `show l2vpn [neighbors\|events]`, `show interfaces` (from simd). |
| simd | `devices/simd/` (`netsim-simd`) | Config watch, golden first boot, adverts/faults/cuts, contract §7 evaluation, stable indexes, SNMP rows, traps with range coalescing, events ring buffer, JSON logs, FastAPI on 9000 (`devices/simd/openapi.json`). |
| SNMP agent | `devices/snmpsim/variation/netsim.py` | snmpsim 1.2.2, data file `<community>.snmprec` = `1.3.6.1\|:netsim\|`, our variation module reads simd's SQLite. |
| `core` + compose | `devices/compose.yaml` (generated) | Valkey with keyspace notifications; one service per device with static labnet IP, `127.0.0.1` ports, named volume, `depends_on: core (healthy)`, container name = hostname. `make devices-compose` regenerates; a unit test fails if it drifts from the topology. |
| Tools | `netsim-show`, `netsim-trapsink`, `netsim-healthcheck`, `netsim-devices` | router-style views, a trap receiver with an HTTP API (integration tests, debugging), compose/OpenAPI/data-file generation, `wait` for readiness. |
| Make | `devices/devices.mk` | `devices-build/up/down/wait/ps/logs/shell/compose/openapi/check/test/test-integration`. |

### Decisions

**supervisord rather than s6-overlay.** Python is already in the image (simd, snmpsim), so supervisord costs one
locked wheel and gives a readable INI file, `supervisorctl`, per-program restarts and log forwarding to
`docker logs`. s6's dependency graph and readiness protocol are not needed: `clixon_restconf` waits for the
backend socket, simd waits for RESTCONF, and both survive Clixon restarts (tested). supervisord reaps orphans as
PID 1 and stops simd first on SIGTERM; simd then exits without withdrawing its advert, so peers see a power-off.

**Config watch: Clixon notification stream, with polling as the safety net.** A ~150-line backend plugin emits the
*standard* RFC 6470 `netconf-config-change` (user, edited instance-identifiers) on the RFC 5277 `NETCONF` stream at
transaction end, after running was written. simd subscribes through native RESTCONF SSE (`GET /streams/NETCONF`) —
the same interface any NMS could use — and re-reads `GET /restconf/data?content=config` on each notification. Measured
commit → simd: 2–5 ms. A 2 s SHA-256 poll of the canonical config covers a lost subscription, and every
(re)subscription triggers a refresh. It held up when the Clixon backend was restarted inside a running
device: the stream re-subscribed and the next commit arrived by notification. CLI and RESTCONF commits take the same path. The
config-change event carries the commit user (`root` / `restconf`), the RFC 6470 edits and the
`diff_configs` structured diff.

**SNMP data: SQLite + a custom snmpsim variation module.** simd writes the whole MIB view (built only from
`contract/oids.yaml`) to SQLite in one transaction whenever it changes; the table uses snmpsim's SQL-module layout
(sortable padded OIDs) plus a `since` column. Why not the stock `sql` module: its values are static, so
`sysUpTime`/`cpwVcUpTime` would need a rewrite every tick; our module computes TimeTicks at request time from
`since` (system-wide CLOCK_BOOTTIME) and is ~30 lines. Why not `.snmprec` regeneration: snmpsim indexes data files
on (re)load, which is racy under polling; SQLite gives atomic replace and ordered `oid > ?` lookups. GETBULK and
walks are snmpsim/pysnmp built from GETNEXT. `test_snmp_agent.py` runs the real responder with the image's data
file and module, adds/deletes/changes rows and checks GET, GETNEXT, GETBULK (incl. non-repeaters and end of MIB),
walk order across tables, live uptimes, 4-octet peer address and community rejection.

**Traps and dampening.** Transitions are batched for 100 ms and coalesced into one notification per run of
consecutive cpwVcIndex values with the same transition (the MIB's index range). A batch never merges different
transitions and never holds a PW twice, so every transition is covered by exactly one notification, in order (a
fast up → down → up sends cpwVcDown then cpwVcUp). Repeated recomputes never re-send (unit-tested). An uplink fault on
a hub yields one `cpwVcDown` for indexes 1..6 instead of six.

## Contract changes (docs/CONTRACT.md §2, §13 — all additive or clarifying)

| § | Change |
|---|---|
| 2 | Device image / core pins (table below). |
| 4 | Container names = hostnames (`docker stop hub-a`); volume `netsim_<host>-data`; use `?content=config` (Clixon adds state modules otherwise); `PUT /restconf/data` replaces the whole config in one commit; RESTCONF commits as user `restconf`, CLI as the unix user. |
| 6/8 | RFC 6470 `netconf-config-change` on stream `NETCONF` (SSE `/streams/NETCONF`); first-boot rule (empty datastore and no marker on the volume). |
| 7 | `core` keyspace notifications (`KEg$x`): plain `SET`/`DEL` of fault keys act in ~0.1 s; `netsim:events` message format; fault value must be `1`; AC fault key applies to any interface (physical access fault → subinterfaces' PWs `ac-down`); three evaluation edge cases. |
| 8 | Trap semantics: `cpwVcDown` for every non-up transition with the actual value (incl. `lowerLayerDown(7)`, which the MIB has no notification for), range coalescing, no PW traps for the boot state, none for deleted rows; ccm varbind values; `TRAP_TARGETS=""` disables traps; values of sysObjectID, ifType, cpwVcType, cpwVcPsnType, inbound/outbound, remote MTU/ifString, cpwVcUpTime. |

## Pinned versions

| Thing | Pin | Verified |
|---|---|---|
| Base image | `python:3.12.15-slim-trixie` | Docker Hub tag (2026-10-09), matches uv's 3.12.15 |
| Clixon / CLIgen | 7.9.0 @ `98178407d162` / `9fd27a8e85ea` | `git rev-parse` in the build; same ARGs as `tools/docker/clixon-check.Dockerfile` (unit test) |
| core | `valkey/valkey:8.1.10-alpine3.24` | Docker Hub tag (2026-09-21) |
| snmpsim | 1.2.2 (sdist sha256 `392ff37e…d2d9` checked) | PyPI; requires pysnmp `>=7.1.26,<8` |
| pysmi | 2.0.0 | snmpsim imports it at runtime but declares it only as a dev extra — found in the first container run |
| pysnmp | 7.1.30 | workspace pin |
| FastAPI / uvicorn / httpx | 0.141.1 / 0.54.0 / 0.28.1 | PyPI (FastAPI: newest release older than 2 weeks; 0.142+ came out days ago) |
| redis-py | 8.1.0 | PyPI |
| supervisor | 4.3.0 | PyPI (extra `image` of netsim-simd, locked) |
| uv (build) | 0.13.1 (`ghcr.io/astral-sh/uv:0.13.1`) | GHCR manifest |
| RFC 6470 YANG | `ietf-netconf-notifications@2012-02-06` sha256 `b2d798ca…c2f2` | copied from the pinned pyang 2.7.1 wheel; `devices/clixon/yang/SOURCES.md`, hash unit-tested |

## How to run

```bash
make devices-build                 # or: make build
make devices-up                    # core + 6 PEs, waits until healthy and all PWs up (or: make up)
uv run netsim-show --url http://127.0.0.1:19021          # spoke-1 PWs with reasons
docker exec -it spoke-1 clixon_cli                         # show l2vpn / set ... / commit
make devices-test                  # 113 unit tests, ~12 s (also part of `make test`)
make devices-test-integration      # 7 container scenarios + timing report, ~22 s (isolated stack 172.30.99.0/24)
make devices-check                 # generated compose + OpenAPI are current
```

## Acceptance results (2026-10-19)

**`make build && docker compose up -d` (full topology, fresh volumes)**

```
spoke-2: ready, 4 PW(s) up (2.1s)
spoke-3: ready, 2 PW(s) up (2.1s)
hub-a: ready, 6 PW(s) up (2.6s)
hub-b: ready, 6 PW(s) up (2.6s)
spoke-4: ready, 4 PW(s) up (2.6s)
spoke-1: ready, 2 PW(s) up (3.1s)
docker compose up -d -> 7/7 containers healthy and 12/12 PWs up on both ends: 9.5s
NAME      STATUS
core      Up 9 seconds (healthy)
hub-a     Up 6 seconds (healthy)
...
spoke-4   Up 5 seconds (healthy)
```

Every device's RESTCONF running config equals `contract/golden/<host>.json` (`netsim-contract diff`: no differences).

**`snmpwalk -v2c -c netsim 127.0.0.1:16121 1.3.6.1.4.1.9.10.106.1.2`** (net-snmp from the image, `--network host`;
the OID is `netsim-contract oid cpwVcTable`):

```
iso.3.6.1.4.1.9.10.106.1.2.1.2.1 = INTEGER: 4                       cpwVcType ethernetVLAN
iso.3.6.1.4.1.9.10.106.1.2.1.4.1 = INTEGER: 1                       cpwVcPsnType mpls
iso.3.6.1.4.1.9.10.106.1.2.1.8.1 = INTEGER: 1                       cpwVcPeerAddrType ipv4
iso.3.6.1.4.1.9.10.106.1.2.1.9.1 = Hex-STRING: 0A FF 00 01          cpwVcPeerAddr (row 2: 0A FF 00 02)
iso.3.6.1.4.1.9.10.106.1.2.1.10.1 = Gauge32: 1001                   cpwVcID (row 2: 2001)
iso.3.6.1.4.1.9.10.106.1.2.1.13.1 = Gauge32: 1500                   cpwVcLocalIfMtu
iso.3.6.1.4.1.9.10.106.1.2.1.17.1 = Gauge32: 1500                   cpwVcRemoteIfMtu
iso.3.6.1.4.1.9.10.106.1.2.1.18.1 = STRING: "GigabitEthernet0/0/2.101"   cpwVcRemoteIfString
iso.3.6.1.4.1.9.10.106.1.2.1.21.1 = STRING: "SVC-0001:primary"     cpwVcName (row 2: "SVC-0001:backup")
iso.3.6.1.4.1.9.10.106.1.2.1.22.1 = STRING: "Customer-A"           cpwVcDescr
iso.3.6.1.4.1.9.10.106.1.2.1.24.1 = Timeticks: (1396) 0:00:13.96    cpwVcUpTime
iso.3.6.1.4.1.9.10.106.1.2.1.25.1 = INTEGER: 1                      cpwVcAdminStatus up
iso.3.6.1.4.1.9.10.106.1.2.1.26.1 = INTEGER: 1                      cpwVcOperStatus up (27, 28: inbound/outbound up)
... 30 values in total (2 rows x 15 columns), then end of MIB
```

**Lint and unit tests**

```
make lint   -> ruff check: All checks passed! / ruff format: 83 files already formatted / pyang + yanglint clean
make test   -> 278 passed in 20.27s   (165 foundation + 113 devices)
```

**Integration (`make devices-test-integration`)** — 7 passed in ~21 s; 5 consecutive runs, no flake:

| # | Scenario | Checks |
|---|---|---|
| 1 | golden boot | SVC-0001 primary up on both ends (SNMP walk + `/state`); every column value; coldStart from both mgmt IPs |
| 2 | spoke-1 primary vc-id → 1010 (RESTCONF) | both ends `down`/`remote-not-configured` in SNMP ≤ 3 s; `ciscoConfigManEvent` (user `restconf`) and `cpwVcDown` from both; no ccm from hub-a |
| 3 | revert | both `up` ≤ 3 s, `cpwVcUp` from both |
| 4 | `SET netsim:fault:uplink:hub-a 1` | spoke-1 `down`/`peer-unreachable`, hub-a `lowerLayerDown`/`local-uplink-down`, config hashes unchanged, no ccm, one `cpwVcDown` for hub-a indexes 1..6 (value 7) |
| 5 | `docker stop hub-a` / `docker start` | spoke-1 `down` ≤ 4 s; back `up`; new boot_id, same config hash, drift (`SVC-0003` description) and indexes preserved |
| 6 | delete spoke-1 SVC-0001 backup, re-add | SNMP row withdrawn (GET → noSuchInstance), re-created with the same cpwVcIndex |
| 7 | `clixon_cli` `set ... enabled false` + `commit` | handled like RESTCONF: `admin-down` / `remote-admin-down`, config-change via notification attributed to `root`, ccm with user `root`, `show l2vpn` shows the reason |

Also verified by hand on the full lab: drift survives `make down && make up`; restarting `core` → PWs down
(`peer-unreachable`) and all back up 1.9 s after the restart; `supervisorctl restart simd` → new boot_id, coldStart, PWs up;
`supervisorctl restart clixon_backend` → stream re-subscribed, next commit seen by notification.

## Measured timings (contract §9)

Worst case over the integration runs (container scenarios; host: Docker 29.8.1, Fedora 44):

| Path | Budget | Measured |
|---|---|---|
| Config commit → both ends' SNMP reflect new oper | ≤ 3 s | **0.079 s** |
| Device power off (`docker stop` issued) → peers' PWs down | ≤ 4 s | **3.03 s** |
|   … measured from the container's exit instead | — | 1.64 s |
| Oper transition → trap received | ≤ 1 s | **0.14 s** (includes the 100 ms batching window) |
| Config commit → `ciscoConfigManEvent` received | — | 0.021 s |
| Fault key set → peer PW down in SNMP | — | 0.14 s |
| Device power on → peers' PWs up | — | 2.65 s |
| Stack up → devices healthy (2 devices + core + sink) | — | 9.3 s |
| Full topology `up -d` → 7/7 healthy, 12/12 PWs up | ≤ 30 s | 9.5 s |

The power-off figure is bound by the contract's 3 s hold time. Docker takes ~0.4–0.8 s to deliver SIGTERM after
`docker stop` is issued, and simd keeps refreshing its advert until then. simd's own detection adds ~0.1 s after the
key expires (keyspace `expired` event or the 1 s recompute, whichever comes first).

## Known limitations

- Power-off detection is TTL-bound (≈ 3 s after the last hello, contract §7); there is no faster liveness signal by design.
- A config change that nets to nothing between two reads (A→B→A within a few ms) produces no event or ccm trap; every
  detected change produces exactly one.
- `netconf-config-change` reports `session-id 0` (the plugin API does not expose the committing session's id).
- Clixon's candidate is shared: uncommitted CLI edits are committed by the next RESTCONF commit (Clixon semantics).
- RESTCONF is HTTP/1.1 only (nghttp2 not built), plain HTTP, no auth — lab only, published on 127.0.0.1.
- snmpsim also answers its built-in community `index` (a data-file index under its own enterprise OID); SET requests are
  rejected (read-only agent). Only the contract §8 objects are served (no ifSpeed, counters, ifLastChange…).
- Inbound/outbound oper status mirror `cpwVcOperStatus`; no per-direction modelling.
- Traps are fire-and-forget v2c (no informs). `sysUpTime` restarts when simd restarts (as specified).

## Notes for sessions 03–05

- **Reset a device** (05): `PUT /restconf/data` with `{"ietf-restconf:data": <golden JSON>}` — one commit, one
  `ciscoConfigManEvent`. Read config with `GET /restconf/data?content=config`; `DeviceConfig.from_restconf()` ignores
  the extra modules either way.
- **Faults** (05): plain `SET netsim:fault:...` / `DEL` act within ~0.1 s (keyspace notifications on `core`); publishing
  on `netsim:events` is optional. `docker stop|start <host>` uses the container names.
- **Events** (05/bot): `GET :190xx/events?since=<epoch|ISO>`; `config-change` events carry `changed_by`, RFC 6470 edits
  and the `diff_configs` diff; `/state` gives reason + rule per PW. Each device also offers RESTCONF SSE
  `/streams/NETCONF`.
- **Traps** (04): sources are the mgmt IPs; `cpwVcDown` may cover an index range and may carry `lowerLayerDown(7)`.
  `netsim-trapsink` (in the image: `docker run --rm --network labnet netsim/netsim-pe:0.1.0 netsim-trapsink`) is handy
  to see what devices send. If you run snmpsim fixtures in a container, install `pysmi` next to it (snmpsim imports it
  but does not declare it).
- **Tooling**: `make devices-up` waits for readiness; `uv run netsim-devices wait` can be reused in bring-up scripts;
  `docker exec <host> snmpwalk/netsim-show/supervisorctl` for debugging.

## Session 01 review

I checked session 01's output before building on it: `uv sync && make lint && make test` passed (165 tests),
`docker compose config` passed, and every artifact I consumed (topology, YANG, golden configs, `oids.yaml`,
`netsim_common`) was correct as-is. The device configs loaded into a real Clixon 7.9.0 with RESTCONF canonicalize
byte-for-byte to the golden files. No changes to session 01's files were needed. The contract additions above are
change-log entries, and the only shared-file edits are the §2 pin paragraph and `uv.lock` (simd's dependencies).
