# Session 02 — Device simulator (Clixon + simd + snmpsim) and simulated core

## Role
You are a senior network-software engineer building a realistic virtual PE router as a container. It must behave like
a real device from the point of view of an NMS (SNMP polling and traps) and an automation system (YANG/RESTCONF).

## Read first
`docs/CONTRACT.md` (authoritative, especially §4, §6, §7, §8, §9 timing) and `docs/reports/01-foundation.md`.
Use `contract/yang/netsim-device.yang`, `contract/oids.yaml`, `contract/golden/*.json` and `lib/netsim_common` as-is.

## You own
`devices/` (Dockerfile, simd source under `devices/simd/`, snmpsim data/variation code, Clixon config, `devices/compose.yaml`),
`docs/reports/02-device-simulator.md`. You may add Makefile targets prefixed `devices-`.

## Goal
One image `netsim-pe` that, configured only by env (`HOSTNAME`, `MGMT_IP`, `CORE_URL`, `TRAP_TARGETS`, `SNMP_COMMUNITY`):
- serves its config via Clixon (RESTCONF + CLI),
- simulates pseudowire signaling with its peers through the `core` Redis bus,
- exposes accurate CISCO-IETF-PW-MIB / IF-MIB / SNMPv2-MIB data through snmpsim,
- sends traps on state and config changes,
- exposes a simd HTTP API with the *reason* behind every PW state.

## Architecture inside the container
Process supervisor (s6-overlay or supervisord, your choice; justify it) running:
1. `clixon_backend` with the netsim-device YANG; datastores on volume `/var/lib/netsim/clixon`.
2. `clixon_restconf` native mode on `0.0.0.0:8080`, HTTP, auth none (lab only).
3. `clixon_cli` available via `docker exec` (autocli from YANG, so engineers can type config like on a router).
4. `snmpsim` responder on `0.0.0.0:161/udp`, v2c community `netsim`.
5. `simd` (Python 3.12, asyncio): the brain.

Build Clixon/CLIgen from source in a multi-stage Dockerfile at pinned release tags; keep the runtime image lean.
Add a Docker `HEALTHCHECK` (RESTCONF responds, SNMP sysName responds, simd `/health` ok).

## simd requirements
1. **Config watch**: detect running-config changes in Clixon within ≤500 ms. Polling RESTCONF with a content hash is
   acceptable; a Clixon notification stream is better if it works reliably. Document which you chose and why.
   Parse config with the `lib/netsim_common` models.
2. **First boot**: if the datastore is empty, load `contract/golden/<HOSTNAME>.json` (bake golden files into the image or
   mount `contract/` read-only). On later boots, **keep** the existing running config (drift must survive restarts).
3. **Signaling**: implement contract §7 exactly: adverts every 1 s with 3 s TTL, `netsim:events` pub/sub, fault keys, cuts,
   reachability, and the oper-evaluation table (first match wins). Recompute on local change, on any event, and every 1 s.
4. **cpwVcIndex**: stable mapping keyed by (service-id, role), persisted on the volume.
5. **SNMP data**: rows must be dynamic (PWs added/removed at runtime) and GET/GETNEXT/GETBULK/walk must all be correct
   and lexicographically ordered. Preferred: snmpsim's SQL variation module backed by SQLite that simd writes atomically.
   Alternatives: a custom snmpsim variation module reading simd state, or atomic `.snmprec` regeneration with verified
   re-indexing. Whatever you choose, **prove it with tests** (add a row, delete a row, change a value, then walk).
   - Populate every object listed in contract §8 using the encodings there (`cpwVcName = SVC-xxxx:role`,
     `cpwVcDescr = customer`, 4-octet `cpwVcPeerAddr`, `cpwVcRemoteIfMtu`/`cpwVcRemoteIfString` from the peer advert,
     `cpwVcUpTime` reset on each up transition).
   - IF-MIB for all config interfaces: ifOperStatus follows config enabled ∧ ¬fault.
   - `sysUpTime` counts from simd boot (resets when the container restarts); sysName = hostname;
     sysDescr like `NetSim PE (Clixon <ver>) <hostname>`.
6. **Traps** (pysnmp, v2c, community `netsim`, to every `TRAP_TARGETS` entry), using OIDs and varbinds from
   `contract/oids.yaml`:
   - `cpwVcDown` / `cpwVcUp` on every oper transition, ≤1 s after it happens;
   - `ciscoConfigManEvent` on every running-config change;
   - `coldStart` at simd start.
   Add hold-down/dampening so a single change does not produce a storm (at most one trap per PW per transition),
   but never suppress a real transition.
7. **simd HTTP API** (`:9000`, FastAPI or aiohttp, OpenAPI spec published):
   - `GET /health`
   - `GET /state`: hostname, loopback, boot_id, uplink {admin, oper, fault}, and pseudowires
     `[{index, service_id, role, peer, vc_id, admin, oper, reason, since, remote:{hostname, mtu, ac_name}|null}]`
   - `GET /neighbors`: adverts currently seen and their age
   - `GET /events?since=<ts>`: ring buffer (≥500) of oper transitions, config changes and traps sent, with timestamps
   - `POST /resync`: force recompute and re-publish
8. **Logging**: structured JSON logs including hostname, event type, service_id, role, old→new oper and reason.

## `core` service
Redis/Valkey container `core` at 172.30.10.5 (pinned image, no persistence needed), defined in `devices/compose.yaml`.

## `devices/compose.yaml`
`core` + `hub-a`, `hub-b`, `spoke-1`…`spoke-4` from one image, static IPs on external `labnet`, a named volume per
device, host ports per contract §4 bound to 127.0.0.1, `TRAP_TARGETS` from `.env`, healthchecks, and `depends_on core`.
Generate the device service blocks from `topology.yaml` if you can keep that simple (e.g. a small render script that
writes the compose file). Otherwise write them out and add a test that they match the topology.

## Tests (pytest under `devices/simd/tests`, plus `devices/tests/integration`)
- **Unit**: table-driven tests for every row of the oper-evaluation table, reachability, index persistence, and trap
  dampening.
- **Integration** (docker compose with `core` + `hub-a` + `spoke-1` + a test trap receiver):
  1. golden boot → SVC-0001 primary is `up` on both ends (SNMP walk + `/state`);
  2. change spoke-1 SVC-0001 primary vc-id to 1010 via RESTCONF → both ends `down` within 3 s, reasons
     `remote-not-configured` on both, `cpwVcDown` and `ciscoConfigManEvent` traps received;
  3. revert → `up` within 3 s, `cpwVcUp` received;
  4. set the uplink fault key on hub-a → spoke-1 primary goes `down` with `peer-unreachable`, no config changed;
  5. `docker stop hub-a` → spoke-1 primary `down` within 4 s; start it again → `up`, config preserved;
  6. delete a PW → its SNMP row disappears; re-add it → the same cpwVcIndex is reused;
  7. Clixon CLI: a config change made with `clixon_cli` is picked up exactly like a RESTCONF one.
- Measure and print the timings against the contract §9 budget.

## Acceptance criteria
- `make build && docker compose up -d` (full topology): all 12 PWs are up and every device is healthy within 30 s.
- `snmpwalk -v2c -c netsim 127.0.0.1:16121 <cpwVcTable OID>` shows 2 rows with correct values.
- All unit and integration tests pass; the timing report is within budget.
- `docs/reports/02-device-simulator.md` includes: architecture, the SNMP data approach chosen and why, how to run
  tests, measured timings, known limitations.

## Working rules
- Use `contract/oids.yaml` for every OID; never hardcode OIDs from memory.
- Don't modify `contract/` or `lib/netsim_common/`. If you need a change there, log it in the contract change log
  and describe it in your report.
- Pin every version. Make every startup step idempotent.
- Out of scope: NetBox, Zabbix, labctl/scenarios, the bot.
