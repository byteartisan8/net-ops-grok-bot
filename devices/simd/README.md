# netsim-simd

The brain of the `netsim-pe` device image (owner: 02-device-simulator). See [`devices/README.md`](../README.md)
for the image, the API and how to run the tests.

| Module | What |
|---|---|
| `evaluate` | Contract §7 as pure functions: interface/uplink state, reachability, the PW oper-evaluation table |
| `daemon` | asyncio orchestrator: config watch, adverts, recompute, SNMP data, traps, events, API |
| `clixon` | RESTCONF client, golden first-boot load, `netconf-config-change` subscription (SSE) |
| `bus` | `core` Redis: adverts, faults, cuts, `netsim:events`, keyspace notifications |
| `mibview`, `snmpdb`, `snmp_variation` | MIB view from contract/oids.yaml -> SQLite -> snmpsim variation module `netsim` |
| `traps` | SNMPv2c notifications (pysnmp) and range-coalescing batcher |
| `indexes` | Persistent `cpwVcIndex` / `ifIndex` maps |
| `api` | FastAPI app on `:9000` (`/health`, `/state`, `/neighbors`, `/events`, `/resync`) |
| `healthcheck`, `show`, `trapsink`, `snmpclient`, `devtool` | Container healthcheck, `netsim-show`, test trap receiver, SNMP client, compose renderer |
