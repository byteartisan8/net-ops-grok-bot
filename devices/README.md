# devices/ - simulated PE routers (owner: session 02-device-simulator)

One image, `netsim/netsim-pe:0.1.0`, runs a virtual PE router per container: Clixon config (RESTCONF + CLI),
`simd` (simulated targeted-LDP signaling, PW evaluation, SNMP data, traps, state API) and an SNMP agent
(snmpsim). `core` is the Valkey signaling bus. `devices/compose.yaml` is generated from `contract/topology.yaml`.

```bash
make devices-build          # image (first build compiles Clixon/CLIgen 7.9.0, ~1-2 min)
make devices-up             # core + 6 PEs, waits until every device is healthy and all 12 PWs are up
make devices-shell S=spoke-1    # clixon_cli on spoke-1 (= docker exec -it spoke-1 clixon_cli)
uv run netsim-show --url http://127.0.0.1:19021      # spoke-1 PW table with reasons
make devices-test           # unit tests (no containers)
make devices-test-integration   # container scenarios + timing report (isolated stack)
```

`make build` / `make up` at the root also build and start the devices (they are part of the root compose).

## Container contract

| Env | Example | Meaning |
|---|---|---|
| `HOSTNAME` | `spoke-1` | Device identity: bus keys, fault keys, golden config file (required) |
| `MGMT_IP` | `172.30.10.21` | Mgmt address on labnet, published in adverts; traps leave from it (required) |
| `CORE_URL` | `redis://172.30.10.5:6379/0` | Signaling bus (default: topology `core`) |
| `TRAP_TARGETS` | `172.30.10.42:1162` | `host[:port],...` (port 162 if omitted); set but empty = no traps |
| `SNMP_COMMUNITY` | `netsim` | v2c read community (trap community: `TRAP_COMMUNITY`, default `netsim`) |
| `SITE` | `BR-01` | `sysLocation` |

Optional tuning: `CONFIG_POLL_INTERVAL` (2 s), `TRAP_BATCH_WINDOW` (0.1 s), `LOG_LEVEL`; see
`devices/simd/src/netsim_simd/settings.py`.

| Port | Service | Host (127.0.0.1 only) |
|---|---|---|
| 8080/tcp | Clixon native RESTCONF, HTTP/1.1, no auth | `18000 + last octet` |
| 9000/tcp | simd API (OpenAPI: `/docs`, `devices/simd/openapi.json`) | `19000 + last octet` |
| 161/udp | SNMP v2c (snmpsim) | `16100 + last octet` |

Container names equal hostnames (`docker stop hub-a` is the "power off" of contract section 7). Each device
has a named volume (`netsim_<host>-data` at `/var/lib/netsim`) with the Clixon datastores and simd state.

## Inside the container

```
supervisord (PID 1)
├── clixon_backend   netsim-device YANG, datastores on the volume, startup mode "running"
│     └── netsim_backend.so: RFC 6470 netconf-config-change on the NETCONF stream after every commit
├── clixon_restconf  native, 0.0.0.0:8080 (also the SSE stream /streams/NETCONF)
├── snmpsim          0.0.0.0:161/udp, variation module "netsim" -> SQLite written by simd
└── simd             asyncio: config watch, adverts/faults on core, PW evaluation, SNMP rows, traps, API :9000
```

**Why supervisord (not s6-overlay).** The image already ships Python for simd and snmpsim, so supervisord is
one pinned wheel (locked in `uv.lock`) with an INI file every engineer can read, `supervisorctl status`
inside the container, per-program restart and log forwarding to `docker logs`. s6-overlay's strengths
(dependency graph, readiness notification, a C PID 1) are not needed: start order is handled by the programs
themselves (`clixon_restconf` waits for the backend socket, simd waits for RESTCONF and Clixon restarts are
transparent to both), and supervisord reaps orphans as PID 1. `docker stop` sends SIGTERM; supervisord stops
simd first, which exits without withdrawing its advert (a power-off for the peers).

**Config watch.** simd subscribes to the standard RESTCONF event stream (`GET /streams/NETCONF`, SSE). The
backend plugin emits `ietf-netconf-notifications:netconf-config-change` (user, edited paths) at the end of
every commit that changed running; simd then reads `GET /restconf/data?content=config` and compares the
canonical JSON hash. Typical commit-to-simd latency is 2-5 ms. A 2 s hash poll is the safety net (dropped
subscription, reconnect gap; every re-subscription also triggers a refresh). Only real changes count: one
`config-change` event (with the structured `diff_configs` diff and the commit user) and one
`ciscoConfigManEvent` per change. CLI commits (`clixon_cli`) and RESTCONF commits take the same path; they
are attributed to `root` and `restconf` respectively.

**First boot / persistence.** If the volume has no `simd/provisioned.json` marker and the datastore is empty,
simd loads `contract/golden/<HOSTNAME>.json` with `PUT /restconf/data` (one commit), then writes the marker.
Later boots never touch the config, even an empty one, so drift survives `docker restart` and
`make down && make up`. `make clean` (volumes deleted) returns to golden on the next start.

**Signaling (contract section 7).** Every second simd writes `netsim:adv:<host>` (`PX 3000`) and
`netsim:lo:<loopback>`, and publishes `{"type":"state","host","boot_id","seq","ts"}` on `netsim:events`
when its advert content changes (config or AC/uplink state). It recomputes on every `netsim:events` message,
on every fault/cut key change and advert expiry (`core` runs with `--notify-keyspace-events KEg$x`), on local
config changes and every 1 s. Faults are only read: `netsim:fault:uplink:<host>`, `netsim:fault:ac:<host>:<if>`
(any interface name; a fault on the physical access interface takes its subinterfaces and their PWs down)
and `netsim:cut:<a>|<b>`, each active only with the value `1`. labctl/lab-api may publish any message on
`netsim:events` after writing a key; with keyspace notifications it is not needed.

**PW evaluation.** `netsim_simd.evaluate` implements the contract table literally (first match wins); `/state`
reports the reason and the rule number. Edge cases: a PW whose peer is the device's own loopback is
`peer-unreachable`; if several adverts share a loopback the first hostname wins; if the peer has several
matching PWs (it reports `duplicate-pw`), the first by (service, role) is used.

**SNMP data.** simd builds the whole MIB view (system group, IF-MIB `ifTable`/`ifXTable`, `cpwVcTable` with the
15 contract columns) from `contract/oids.yaml` and writes it to SQLite in one transaction whenever it changes.
The table is snmpsim's SQL-module layout (`oid` padded for lexicographic order, `tag`, `value`, `maxaccess`)
plus a `since` column; snmpsim's data file has a single subtree line `1.3.6.1|:netsim|` handing every
request to the `netsim` variation module (`devices/snmpsim/variation/netsim.py` -> `netsim_simd.snmp_variation`).
GET is a primary-key lookup and GETNEXT is `oid > ? ORDER BY oid LIMIT 1`; snmpsim builds GETBULK and walks
from GETNEXT. The custom module (rather than the stock `sql` one) adds live TimeTicks: `sysUpTime` and
`cpwVcUpTime` are computed from `since` at request time instead of being rewritten every tick. Rows appear and
disappear atomically; `devices/simd/tests/test_snmp_agent.py` proves add/delete/change + GET/GETNEXT/GETBULK
ordering against a real snmpsim. Encodings: `cpwVcName` `SVC-0001:primary`, `cpwVcDescr` customer,
`cpwVcPeerAddr` 4 octets, `cpwVcType` `ethernetVLAN` (subinterface AC), `cpwVcPsnType` `mpls`, inbound/outbound
oper = `cpwVcOperStatus`, remote MTU/interface from the peer advert (0/empty when unknown), `sysObjectID`
`zeroDotZero`, `ifType` `ethernetCsmacd` / `l2vlan`. Indexes (`cpwVcIndex` per (service, role), `ifIndex` per
name) are persisted on the volume; a deleted PW's index comes back when it is re-created.

**Traps** (pysnmp message API, v2c, to every `TRAP_TARGETS` entry, from the mgmt IP): `coldStart` once the agent
serves after each simd start; `ciscoConfigManEvent` per config change (CommandSource `commandLine`,
ConfigSource `commandSource`, ConfigDestination `running`, TerminalUser = commit user); `cpwVcUp` when a row
enters `up`, `cpwVcDown` for every other oper transition (`down` and `lowerLayerDown`, varbinds carry the new
value). Transitions are batched for 100 ms and coalesced into one notification per run of consecutive
`cpwVcIndex` values with the same transition (the MIB's index range), e.g. an uplink fault on a hub gives one
`cpwVcDown` for indexes 1..6. Nothing is ever suppressed: each transition is covered by exactly one
notification, in order. The initial state at boot sends no PW traps (coldStart covers it).

## simd API (`:9000`)

| Endpoint | |
|---|---|
| `GET /health` | `ok` / `degraded` (200) or `starting` (503), checks: restconf, core, snmp_db, config, config_stream |
| `GET /state` | hostname, loopback, boot_id, uplink {admin, oper, fault}, faults, interfaces, pseudowires `[{index, service_id, role, peer, peer_hostname, vc_id, admin, oper, reason, rule, since, remote{hostname, mtu, ac_name, service_id, role}\|null, ...}]` |
| `GET /neighbors` | fresh adverts with age, boot_id, uplink, reachability |
| `GET /events?since=<epoch or ISO 8601>&limit=` | ring buffer (1000): boot, config-change (with diff), oper-change, reason-change, pw-added/removed, if-oper-change, fault-change, neighbor-up/down/restart, trap-sent |
| `POST /resync` | re-read config, re-publish the advert, recompute now |

## CLI

```
$ docker exec -it spoke-1 clixon_cli
root@spoke-1 /# set l2vpn xconnect SVC-0001 pseudowire primary vc-id 1010
root@spoke-1 /# show compare
root@spoke-1 /# commit
root@spoke-1 /# show l2vpn                 # PWs with oper status and reason (from simd)
root@spoke-1 /# show l2vpn neighbors | show interfaces | show running-config [xml|json]
```

Also in the container: `netsim-show [pw|interfaces|neighbors|events]`, `snmpwalk`, `supervisorctl status`.

## Files

| Path | |
|---|---|
| `Dockerfile`, `Dockerfile.dockerignore` | image (build context: repo root) |
| `compose.yaml` | generated (`make devices-compose`), checked by `make devices-check` and the unit tests |
| `devices.mk` | `make devices-*` targets |
| `clixon/` | `clixon.xml` (/etc/clixon.xml), CLI spec, backend/CLI plugins (C), vendored RFC 6470 YANG |
| `snmpsim/variation/netsim.py` | snmpsim variation module entry point |
| `supervisord.conf`, `entrypoint.sh` | process supervision, runtime directories |
| `simd/` | Python package `netsim-simd` (+ `openapi.json`, unit tests) |
| `tests/integration/` | container scenarios (`make devices-test-integration`) |
