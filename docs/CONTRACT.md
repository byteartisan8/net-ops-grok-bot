# NetSim L2VPN Lab — Shared Contract

**This file is the single source of interface truth for every build session.**
If your work needs something that contradicts this file, do not silently diverge:
add an entry to the [Change log](#13-change-log) with the reason, make the smallest
compatible change, and mention it in your final report.

---

## 1. What we are building

A lab that simulates a hub-and-spoke L2VPN (VPWS pseudowire) network with redundancy,
monitored by Zabbix, with NetBox as source of truth. A Grok-powered bot will later sit
on top (out of scope for the foundation build) and must be able to:

- read **intent** from NetBox,
- read **running config** from each device (Clixon RESTCONF),
- read **observed state** from Zabbix (problems, items, business services),
- inject **faults** and **scenarios** through the lab API,
- push **remediation** to devices and verify it through Zabbix.

Core principle: **traps are hints, polling is truth.** **Config is intent/drift; faults are physical failures.**
These two must never be mixed up in any component.

```
                ┌──────────── NetBox (intent / SoT) ─────────────┐
                │                                                 │
   lab-api ─────┤                                                 ├── NetBox→Zabbix sync
   (bot tools)  │                                                 │   (hosts, services tree)
                ▼                                                 ▼
  ┌─────────────────────── device container (x6) ───────────┐   Zabbix server
  │ Clixon (YANG config, RESTCONF :8080)                     │◀── SNMP poll (walk) ──┐
  │      │ running config                                    │                       │
  │      ▼                                                   │                       │
  │ simd (brain) ──► snmpsim (SNMP agent :161/udp)           │── traps ──► zabbix-snmptraps
  │      ▲  │                                                │             │
  │      │  └──► simd API :9000 (state, reasons, events)     │             ▼
  └──────┼───────────────────────────────────────────────────┘   trap → action → "check now"
         │ adverts / faults (simulated LDP + physical faults)
         ▼
     core (Redis)  ◄── labctl / lab-api (faults, cuts)
```

---

## 2. Versions & tooling (pin exact versions; verify they exist before pinning)

| Thing | Choice |
|---|---|
| Container runtime | Docker Engine 24+, Docker Compose v2.20+ (uses `include:`) |
| Python | 3.12, managed with `uv` as a workspace at repo root |
| Clixon / CLIgen | Latest tagged release of `clicon/cligen` and `clicon/clixon`, built from source, tags pinned |
| SNMP agent | `snmpsim` (LeXtudio maintained), latest 1.x, pinned |
| SNMP library | `pysnmp` (LeXtudio maintained), pinned, used by simd for traps and by tests |
| Signaling bus ("core") | Redis 7.x or Valkey 8.x, pinned |
| NetBox | Official `netboxcommunity/netbox` image, latest stable 4.x, pinned |
| Zabbix | **8.0 LTS** (PostgreSQL backend). Pin `8.0.0` GA if it's released when you build; otherwise pin the newest `8.0.0rcN` and record the planned bump. Use the 8.0 docs only. |
| PostgreSQL | 16.x for NetBox; for Zabbix use the newest version that Zabbix 8.0's requirements page lists as supported |

Pinned by 01-foundation (2026-10-18; `uv.lock` holds the full Python closure): uv 0.13.1, Python 3.12 (uv resolved 3.12.15),
pydantic 2.14.1, PyYAML 6.0.3, pysmi 2.0.0, **pysnmp 7.1.30** (use the same in simd), pyang 2.7.1, libyang/yanglint 6.4.13,
jsonschema 4.26.0, pytest 9.1.1, ruff 0.17.1, hatchling 1.32.4. Latest Clixon/CLIgen tag at that date: **7.9.0**
(clixon `98178407d162`, cligen `9fd27a8e85ea`); the YANG is verified against it (`make yang-clixon`).

Pinned by 02-device-simulator (2026-10-19): device image `netsim/netsim-pe:0.1.0` on `python:3.12.15-slim-trixie`
with Clixon/CLIgen **7.9.0** built from the same commits (native RESTCONF, HTTP/1.1 only), snmpsim **1.2.2** (+ pysmi
2.0.0, which snmpsim imports at runtime), pysnmp 7.1.30, FastAPI 0.141.1, uvicorn 0.54.0, httpx 0.28.1, redis-py 8.1.0,
supervisor 4.3.0; `core` = **`valkey/valkey:8.1.10-alpine3.24`**.

Pinned by 03-netbox (2026-10-19): **`netboxcommunity/netbox:v4.7.3-5.1.1`** (NetBox 4.7.3 = latest stable 4.x, on
netbox-docker 5.1.1 / Granian), `postgres:16.15-alpine3.24`, Valkey for its task queue and cache = the same
`valkey/valkey:8.1.10-alpine3.24` as `core`; the NetBox tools use httpx 0.28.1 (plain REST, no pynetbox).

Pinned by 04-zabbix (2026-10-19): **Zabbix 8.0.0rc1** (revision dd0d9c2; newest 8.0 release, 8.0.0 GA not out yet;
API reports `8.0.0`). Zabbix publishes no image tags for pre-releases, so `zabbix/images/build-images.sh` builds
`netsim/zabbix-{server-pgsql,web-nginx-pgsql,snmptraps}:8.0.0rc1-alpine` with Zabbix's own Dockerfiles (zabbix-docker
`d60c37c45705`, 2026-09-28) from the source tag; **planned bump: `zabbix/zabbix-*:alpine-8.0.0` from Docker Hub at GA**.
PostgreSQL for Zabbix: `postgres:18.6-alpine3.24` (8.0 supports 15.0-18.x).

Use the official docs for the pinned version. **Never write OIDs, API fields, or config keys from memory**; verify them against the MIB file / docs / a running instance.

---

## 3. Repository layout & ownership

| Path | Owner session | Notes |
|---|---|---|
| `docs/CONTRACT.md` | all (via Change log) | this file |
| `docs/reports/` | each session writes its own report | `NN-<name>.md` |
| `contract/topology.yaml` | 01-foundation | lab topology, input to everything |
| `contract/yang/netsim-device.yang` | 01-foundation | device config model |
| `contract/mibs/` + `contract/oids.yaml` | 01-foundation | pinned MIBs + resolved OIDs |
| `contract/golden/<host>.json` | 01-foundation (generated) | golden config per device |
| `contract/bsm-spec.schema.json` | 01-foundation | JSON Schema of the BSM spec (§9.3) |
| `contract/topology.schema.json`, `contract/yang/examples/`, `contract/yang/netsim-device.tree` | 01-foundation | topology schema, YANG example instances, YANG tree (generated) |
| `lib/netsim_common/` | 01-foundation | shared pydantic models, topology loader, constants |
| `tools/`, `tests/` (repo root) | 01-foundation | contract generators/checkers and the contract-level tests |
| `compose.yaml`, `Makefile`, `.env.example`, `pyproject.toml` (workspace) | 01-foundation | others may only add targets/members they own |
| `<component>/<component>.mk` (`devices/devices.mk`, `netbox/netbox.mk`, `zabbix/zabbix.mk`, `lab/lab.mk`) | that component's owner | its make targets; included by the root `Makefile`, overrides the "not implemented yet" placeholders |
| `devices/` | 02-device-simulator | image, simd, snmpsim data, `devices/compose.yaml` |
| `netbox/` | 03-netbox | `netbox/compose.yaml`, seeder, intent renderer |
| `zabbix/` | 04-zabbix | `zabbix/compose.yaml`, template, provisioning, dashboards |
| `lab/` | 05-integration | labctl, lab-api, scenarios, e2e tests, `lab/compose.yaml` |

Root `compose.yaml` only contains `include:` of the four fragments. All fragments attach to the
**external** network `labnet` (created by `make net`), so fragments never fight over network definitions.
The root file also fixes the compose project name (`name: netsim`), so volume names are stable (`netsim_<volume>`).

Make targets: components define theirs in `<component>/<component>.mk` (prefix `devices-`, `netbox-`, `zabbix-`, `lab-`).
Root targets delegate: `make seed` → `netbox-seed` then `zabbix-provision`; `make reset` → `lab-reset`; `make e2e` → `lab-e2e`.
`.env.example` belongs to 01-foundation; other sessions may append variables for their component in its section
(and log it here).

---

## 4. Network & addressing

Docker network `labnet`: `172.30.10.0/24`, gateway `172.30.10.1`, created externally.

| Host | Role | Site | Mgmt IP (labnet) | Loopback0 (simulated) |
|---|---|---|---|---|
| `core` | Redis signaling bus | — | 172.30.10.5 | — |
| `hub-a` | hub | DC-A | 172.30.10.11 | 10.255.0.1 |
| `hub-b` | hub | DC-B | 172.30.10.12 | 10.255.0.2 |
| `spoke-1` | spoke | BR-01 | 172.30.10.21 | 10.255.1.1 |
| `spoke-2` | spoke | BR-02 | 172.30.10.22 | 10.255.1.2 |
| `spoke-3` | spoke | BR-03 | 172.30.10.23 | 10.255.1.3 |
| `spoke-4` | spoke | BR-04 | 172.30.10.24 | 10.255.1.4 |
| `netbox` | NetBox web/API | — | 172.30.10.30 | — |
| `netbox-worker` | NetBox background worker (sends the webhooks) | — | 172.30.10.31 | — |
| `zabbix-server` | Zabbix server | — | 172.30.10.40 | — |
| `zabbix-web` | Zabbix frontend/API | — | 172.30.10.41 | — |
| `zabbix-snmptraps` | snmptrapd | — | 172.30.10.42 | — |
| `lab-api` | lab/bot tool API | — | 172.30.10.50 | — |

Loopbacks are **not** real container IPs. They exist only in config and signaling and are the PW peer addresses.
Traps must leave the device container from its mgmt IP (same bridge, no NAT), because Zabbix matches trap source to host interface IP.

### Ports published to the host (127.0.0.1 only)

| Service | Host port |
|---|---|
| NetBox UI/API | 8000 |
| Zabbix UI/API | 8080 |
| lab-api | 8090 |
| Device RESTCONF (container :8080) | `180<last-octet>` → hub-a 18011, hub-b 18012, spoke-1 18021 … spoke-4 18024 |
| Device simd API (container :9000) | `190<last-octet>` → 19011 … 19024 |
| Device SNMP (container :161/udp) | `161<last-octet>`/udp → 16111 … 16124 |

Numerically: host port = 18000 / 19000 / 16100 + last octet of the mgmt IP (same values as above; also valid for
3-digit octets in generated scale topologies). Encoded in `contract/topology.yaml` (`device_ports`), computed by
`netsim_common.topology.Topology.host_port()` / `netsim_common.constants.lab_constants()`.

### Device container ports

| Port | Purpose |
|---|---|
| 8080/tcp | Clixon native RESTCONF (HTTP, no TLS, no auth — lab only) |
| 161/udp | SNMP v2c agent (snmpsim) |
| 9000/tcp | simd HTTP API |
| CLI | `docker exec -it <host> clixon_cli` (autocli generated from YANG) |

---

## 5. Services (customer L2VPNs)

Each service = one spoke attachment circuit, dual-homed: **primary PW to hub-a**, **backup PW to hub-b**.
VC ID scheme: primary `1000+n`, backup `2000+n`.

| Service | Customer | Spoke | VLAN | Spoke AC | Hub AC (both hubs) | Primary VC (↔hub-a) | Backup VC (↔hub-b) |
|---|---|---|---|---|---|---|---|
| SVC-0001 | Customer-A | spoke-1 | 101 | GigabitEthernet0/0/1.101 | GigabitEthernet0/0/2.101 | 1001 | 2001 |
| SVC-0002 | Customer-A | spoke-2 | 102 | GigabitEthernet0/0/1.102 | GigabitEthernet0/0/2.102 | 1002 | 2002 |
| SVC-0003 | Customer-B | spoke-2 | 201 | GigabitEthernet0/0/1.201 | GigabitEthernet0/0/2.201 | 1003 | 2003 |
| SVC-0004 | Customer-C | spoke-3 | 301 | GigabitEthernet0/0/1.301 | GigabitEthernet0/0/2.301 | 1004 | 2004 |
| SVC-0005 | Customer-D | spoke-4 | 401 | GigabitEthernet0/0/1.401 | GigabitEthernet0/0/2.401 | 1005 | 2005 |
| SVC-0006 | Customer-B | spoke-4 | 202 | GigabitEthernet0/0/1.202 | GigabitEthernet0/0/2.202 | 1006 | 2006 |

Physical interfaces on every device:
- `GigabitEthernet0/0/0`: `core-uplink`, MTU 9000
- spokes: `GigabitEthernet0/0/1` (`access`, MTU 1500); hubs: `GigabitEthernet0/0/2` (`access`, MTU 1500)
- subinterfaces: type `subinterface`, `parent` = access interface, `vlan-id`, MTU inherited from parent unless overridden

Totals: 12 PWs; each hub holds 6, spokes hold 2 or 4.

---

## 6. Device config model (YANG `netsim-device`, namespace `urn:netsim:device`, prefix `nsd`)

Shape (the real YANG lives in `contract/yang/netsim-device.yang`):

```
container system      { leaf hostname; leaf loopback-ipv4; }
container interfaces  { list interface (key name) {
                          leaf name; leaf description; leaf enabled (default true);
                          leaf type (enum core-uplink|access|subinterface);
                          leaf parent (leafref, only for subinterface); leaf vlan-id (1..4094);
                          leaf mtu (optional; subinterface inherits parent) } }
container l2vpn       { list xconnect (key service-id) {
                          leaf service-id (pattern SVC-\d{4}); leaf customer; leaf description;
                          leaf attachment-circuit (leafref interface name); leaf enabled (default true);
                          list pseudowire (key role) {
                            leaf role (enum primary|backup); leaf peer-ipv4; leaf vc-id (uint32 1..4294967295);
                            leaf enabled (default true);
                            unique "peer-ipv4 vc-id"; } } }
```

Rules:
- A hub's xconnect normally contains only the PW whose role matches that hub (hub-a → `primary`, hub-b → `backup`), but the model **must** allow both roles on a hub (needed for drift scenarios).
- `(peer-ipv4, vc-id)` must be unique **per device** across all xconnects. YANG can only enforce it within one xconnect; simd flags violations with reason `duplicate-pw`.
- Mgmt interface and Loopback0 are **not** in the device config. The loopback is `system/loopback-ipv4`.
- Effective MTU: an interface's own `mtu`; else (subinterface) the parent's effective MTU; a physical interface
  without `mtu` uses **1500**. A PW's MTU (advert `mtu`, rule 9 of §7) is the effective MTU of its AC.
  Helper: `DeviceConfig.effective_mtu()`.
- YANG details beyond the shape above (all verified with pyang, yanglint and Clixon 7.9.0): `service-id` pattern is
  written `SVC-[0-9]{4}` (ASCII digits); mandatory leaves are interface `type`, xconnect `attachment-circuit`, PW
  `peer-ipv4` and `vc-id` (none at top level, so an empty datastore is valid); `must`: a subinterface needs `parent`
  and `vlan-id`, only subinterfaces may have them, and `parent` must be a non-subinterface; lengths follow the SNMP
  objects carrying the value: interface `name` ≤ 80 (cpwVcRemoteIfString), `description` ≤ 64 (ifAlias),
  xconnect `customer` 1..255 (cpwVcDescr); `mtu` 64..65535; `hostname` is a DNS label. Example instances (valid and
  invalid, with expected errors) live in `contract/yang/examples/`.
- Golden rendering (`netsim_common.golden`): physical interfaces carry `description` `CORE` / `CUSTOMER-ACCESS`
  (hubs too); each service subinterface is `<access>.<vlan>` with `description` `"<customer> <service-id>"`, no
  `mtu`; xconnects carry `customer`, no `description`; every `enabled` is spelled out (`true`).

RESTCONF JSON example (spoke-1, golden):

```json
{
  "netsim-device:system": { "hostname": "spoke-1", "loopback-ipv4": "10.255.1.1" },
  "netsim-device:interfaces": { "interface": [
    { "name": "GigabitEthernet0/0/0", "type": "core-uplink", "enabled": true, "mtu": 9000, "description": "CORE" },
    { "name": "GigabitEthernet0/0/1", "type": "access", "enabled": true, "mtu": 1500, "description": "CUSTOMER-ACCESS" },
    { "name": "GigabitEthernet0/0/1.101", "type": "subinterface", "parent": "GigabitEthernet0/0/1",
      "vlan-id": 101, "enabled": true, "description": "Customer-A SVC-0001" }
  ]},
  "netsim-device:l2vpn": { "xconnect": [
    { "service-id": "SVC-0001", "customer": "Customer-A", "attachment-circuit": "GigabitEthernet0/0/1.101", "enabled": true,
      "pseudowire": [
        { "role": "primary", "peer-ipv4": "10.255.0.1", "vc-id": 1001, "enabled": true },
        { "role": "backup",  "peer-ipv4": "10.255.0.2", "vc-id": 2001, "enabled": true }
      ] }
  ]}
}
```

RESTCONF paths: `/restconf/data/netsim-device:l2vpn`, `/restconf/data/netsim-device:interfaces`, etc.
Interface names in URLs must be percent-encoded (`GigabitEthernet0%2F0%2F1.101`).
Lists are emitted **sorted by key** in golden and intent-rendered output, so two configs can be compared byte-for-byte after canonical JSON dump (`sort_keys=True`).

Canonical form (`netsim_common.canonical_json`, the only implementation everyone should use): parse with the
`DeviceConfig` model (YANG defaults filled in), sort `interface` by `name` and `xconnect` by `service-id` (plain string
order) and `pseudowire` by `role` in YANG enum order (`primary` before `backup`), drop empty lists/containers, then
`json.dumps(sort_keys=True, indent=2, ensure_ascii=False)` plus a trailing newline. Golden files are exactly this
string. `netsim_common.diff_configs(expected, actual)` compares after the same normalization and returns one entry per
leaf (or per missing/unexpected list entry), e.g.
`l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4: expected 10.255.0.1, got 10.255.0.2`.

---

## 7. Simulated control plane (simd ↔ core)

### Adverts (simulated targeted-LDP)
- Every simd writes `netsim:adv:<hostname>` every **1 s** (hello) with TTL **3 s** (hold time), and publishes on channel `netsim:events` whenever its local state changes.
- Advert JSON: `{hostname, loopback, mgmt_ip, boot_id, seq, uplink_oper_up, pws:[{service_id, role, peer, vc_id, enabled, ac_oper_up, mtu, ac_name}]}`
- Peer lookup is by loopback (small lab: scanning adverts is fine; optional index key `netsim:lo:<loopback>` → hostname).

### Physical faults (set by labctl/lab-api, never by config)
| Key | Meaning |
|---|---|
| `netsim:fault:uplink:<hostname>` = `1` | core uplink physically down (oper down, config untouched) |
| `netsim:fault:ac:<hostname>:<ifname>` = `1` | attachment circuit physically down |
| `netsim:cut:<hostA>\|<hostB>` = `1` (hostnames sorted) | core path between the pair broken |
| device power off | `docker stop <host>` (adverts expire after hold time, SNMP unreachable) |

### Reachability
`reachable(A,B) = fresh(adv A) ∧ fresh(adv B) ∧ uplink_oper_up(A) ∧ uplink_oper_up(B) ∧ ¬cut(A,B)`
where `uplink_oper_up = config enabled ∧ ¬fault`.

### PW oper evaluation (local PW `p` on device A, peer loopback P, vc-id N). First match wins.

| # | Condition | cpwVcOperStatus | reason |
|---|---|---|---|
| 1 | xconnect or PW `enabled=false` | down | `admin-down` |
| 2 | another local PW has same (peer, vc-id) | down | `duplicate-pw` |
| 3 | local AC disabled or AC fault | lowerLayerDown | `ac-down` |
| 4 | local uplink down | lowerLayerDown | `local-uplink-down` |
| 5 | no fresh advert with loopback P, or unreachable | down | `peer-unreachable` |
| 6 | peer has no PW with peer=A.loopback ∧ vc-id=N | down | `remote-not-configured` |
| 7 | remote PW admin down | down | `remote-admin-down` |
| 8 | remote AC down | down | `remote-ac-down` |
| 9 | effective MTU differs | down | `mtu-mismatch` |
| 10 | otherwise | up | `ok` |

Only peer/vc-id/MTU/admin/reachability determine status, as on real routers. `service-id` and `role` mismatches between the two ends do **not** affect status (this is what makes drift realistic).

### cpwVcIndex
Stable per device, keyed by `(service-id, role)`, persisted on the device volume; new rows get `max+1`; deleted PW → row disappears from SNMP.

---

## 8. SNMP contract

- SNMP v2c, read community `netsim`, trap community `netsim`.
- MIBs (pinned in `contract/mibs/`, numeric OIDs resolved into `contract/oids.yaml` by 01-foundation):
  - `SNMPv2-MIB` system group (sysDescr, sysObjectID, sysUpTime, sysName = hostname), `coldStart`
  - `IF-MIB` ifTable/ifXTable for all config interfaces (ifName, ifDescr, ifAlias=description, ifAdminStatus, ifOperStatus, ifMtu)
  - `CISCO-IETF-PW-MIB` `cpwVcTable` columns at minimum: cpwVcType, cpwVcPsnType, cpwVcPeerAddrType, cpwVcPeerAddr, cpwVcID, cpwVcLocalIfMtu, cpwVcRemoteIfMtu, cpwVcRemoteIfString, cpwVcName, cpwVcDescr, cpwVcAdminStatus, cpwVcOperStatus, cpwVcInboundOperStatus, cpwVcOutboundOperStatus, cpwVcUpTime; notifications `cpwVcDown`, `cpwVcUp`
  - `CISCO-CONFIG-MAN-MIB` notification `ciscoConfigManEvent` (sent on every commit that changes running config)
- Encoding conventions:
  - `cpwVcName` = `<service-id>:<role>` e.g. `SVC-0001:primary`. Zabbix derives service tags from this.
  - `cpwVcDescr` = customer name.
  - `cpwVcPeerAddr` = 4-octet InetAddress (Zabbix must convert hex → dotted quad).
- Traps go to `TRAP_TARGETS` (env, default `172.30.10.42:<port confirmed by 04-zabbix>`), within ≤1 s of the transition.
  `.env.example` ships `172.30.10.42:1162` (zabbix-docker trunk, `MAJOR_VERSION=8.0`, snmptraps Dockerfile: `EXPOSE 1162/udp`); **confirmed by 04-zabbix**: snmptrapd in the 8.0.0rc1 snmptraps image listens on `udp:1162` and `udp6:1162`.
  - `cpwVcDown` / `cpwVcUp` on every oper transition (varbinds exactly as the MIB defines)
  - `ciscoConfigManEvent` on every config change
  - `coldStart` on simd start with a new boot_id
- `contract/oids.yaml` (generated from the pinned MIBs; typed access via `netsim_common.load_oids()`) holds, per object:
  numeric OID, kind (scalar/table/row/column), access, table/row/index, and syntax (`type` as written, SMI `base`, `enum`,
  `range`/`size`, display hint); per notification: OID, its OBJECTS in order with their index objects, and the MIB
  description. A v2c trap carries `sysUpTime.0`, `snmpTrapOID.0`, then the OBJECTS (RFC 3416 §4.2.6). Facts worth knowing:
  `cpwVcDown`/`cpwVcUp` carry `cpwVcOperStatus` twice (lowest and highest affected cpwVcIndex; identical for one PW);
  in the pinned CISCO-CONFIG-MAN-MIB (revision 2019-03-11) `ciscoConfigManEvent` has **four** varbinds:
  ccmHistoryEventCommandSource, ccmHistoryEventConfigSource, ccmHistoryEventConfigDestination, ccmHistoryEventTerminalUser
  (all indexed by ccmHistoryEventIndex).

---

## 9. Monitoring semantics (Zabbix 8.0)

Principle: **the BSM is built from NetBox, and every BSM node carries its NetBox spec.** Zabbix checks devices
*against that spec*, not just "whatever the device happens to have". A PW that NetBox expects but the device
doesn't have is a problem, not silence.

### 9.1 Hosts, polling, traps
- Host name = device hostname; SNMP interface = mgmt IP:161; template **`NetSim L2VPN PE SNMP`**.
- Polling: one SNMP `walk[...]` master item over cpwVcTable (+ IF-MIB) every **60 s**; everything else is dependent on it.
- Trap flow: trap item → trigger (event per trap) → action → webhook global script → Zabbix API "check now" on the walk master item (and the LLD rules for `ciscoConfigManEvent` / `coldStart`).

### 9.2 Two discovery views per host
| View | Source | Purpose |
|---|---|---|
| **Expected PWs** (intent) | Host macro `{$NETSIM.PW.SPEC}`: a JSON list of this host's expected PWs, written by the NetBox→Zabbix sync from the BSM spec | Drives the leaf checks below. Items are dependent on the walk master; each one finds its row by `cpwVcName == "<service>:<role>"` |
| **Observed PWs** (state) | SNMP walk LLD over cpwVcTable | Raw visibility, and flags PWs on the device that NetBox doesn't know about |

`{$NETSIM.PW.SPEC}` value: compact JSON list (no spaces) of
`{"service", "role", "vc_id", "expected_peer", "expected_peer_host", "customer", "ac"}` objects, one per PW leaf end on
this host (`expected_peer` = peer address this host must use, `ac` = this host's AC), sorted by service then role
(primary first). Built from the BSM spec by `netsim_common.pw_spec_macro_value(spec, host)`
(`pw_spec_for_host()` returns the typed entries). A hub carries 6 entries (about 1 KB).

Checks on each expected PW (trigger prototypes of the intent LLD), all tagged
`scope=l2vpn-pw`, `service`, `pw_role`, `customer`, `host`, `check`, plus the expected `vcid` and `peer`:

| check | Condition | Severity | Meaning |
|---|---|---|---|
| `missing` | no row named `<service>:<role>` on this host | High | NetBox expects the PW; the device doesn't have it |
| `oper` | row present, `cpwVcOperStatus ≠ up(1)` | High | PW is down |
| `spec` | row present but peer ≠ expected peer **or** VC ID ≠ expected VC ID | Average | PW runs, but not on the path NetBox says (drift: redundancy may be fake) |

Observed-view trigger: `unexpected` (a row whose `service:role` isn't in this host's spec), severity Warning, tag `scope=l2vpn-unexpected`.
It is **not** mapped into the BSM.

Event names must show expected vs actual, e.g.
`SVC-0002 primary on spoke-2: peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)`.

### 9.3 BSM spec (generated from NetBox; schema `contract/bsm-spec.schema.json`)
Produced by `netbox-intent bsm-spec` (session 03) and consumed by `zabbix-provision` (session 04). Every node has
`name`, `tags`, `description` (with a NetBox URL), and its own spec:

```json
{
  "root": { "name": "L2VPN", "tags": {"scope": "l2vpn"} },
  "customers": [{
    "name": "Customer-A",
    "tags": {"customer": "Customer-A", "sla_tier": "gold", "netbox_tenant_id": "3"},
    "sla_tier": "gold",
    "services": [{
      "name": "SVC-0001 (spoke-1)",
      "service_id": "SVC-0001", "spoke": "spoke-1",
      "tags": {"service": "SVC-0001", "customer": "Customer-A", "spoke": "spoke-1"},
      "status_rules": "redundant-pair",
      "pseudowires": [{
        "name": "SVC-0001 primary (hub-a)",
        "role": "primary", "vc_id": 1001, "netbox_l2vpn_id": 11,
        "ends": [
          {"host": "spoke-1", "ac": "GigabitEthernet0/0/1.101", "expected_peer": "10.255.0.1"},
          {"host": "hub-a",   "ac": "GigabitEthernet0/0/2.101", "expected_peer": "10.255.1.1"}
        ],
        "tags": {"service": "SVC-0001", "pw_role": "primary", "vcid": "1001", "a_end": "spoke-1", "z_end": "hub-a"}
      }]
    }]
  }]
}
```

Model: `netsim_common.bsm.BsmSpec` (the schema is generated from it). Additions to the example above: every node has
an optional `netbox_url` (`null` when the spec does not come from NetBox) and a required `description`; build nodes with
`netsim_common.bsm.build_root/build_customer/build_service/build_pseudowire` so names, spec tags and descriptions are
identical whatever the source (the model rejects names/tags that disagree with the node's fields; extra tags such as
`netbox_tenant_id` are allowed). `ends` is `[a-end = spoke, z-end = hub]`; a service has 1 or 2 PW leaves with distinct
roles. Compare specs from different sources with `a.normalized() == b.normalized()` (drops NetBox IDs/URLs and
`netbox_*` tags, sorts, re-renders descriptions); `bsm_spec_from_topology()` is already normalized.

### 9.4 BSM tree and status rules
```
L2VPN                                    root: most critical child
└── Customer-A                           customer: most critical child; SLA from sla_tier
    └── SVC-0001 (spoke-1)               service: status_rules below
        ├── SVC-0001 primary (hub-a)     leaf: problems with scope=l2vpn-pw, service=SVC-0001, pw_role=primary
        └── SVC-0001 backup (hub-b)      leaf: problems with scope=l2vpn-pw, service=SVC-0001, pw_role=backup
```
- A leaf matches problems from **both ends** (same tags), so a dead device is still detected by its peers.
- `redundant-pair` service rules: **≥2 children ≥ High → Disaster** (down); **≥1 child ≥ Average → Warning** (degraded or at risk); else OK.
- SLA objects per `sla_tier` (`gold` 99.95 %, `silver` 99.9 %) select services by tag.
- The sync is idempotent and **NetBox-driven both ways**: a new L2VPN in NetBox creates nodes and spec macros; one removed from NetBox is pruned from the BSM.
  A service added in NetBox but not yet configured on devices shows `missing` on both ends (Disaster), which correctly means "ordered but not delivered".

### 9.5 Timing budget (measured by e2e tests)

| Path | Budget |
|---|---|
| Config commit → both ends' SNMP reflect new oper | ≤ 3 s |
| Device power off → peers' PWs down | ≤ 4 s |
| Oper transition → trap sent | ≤ 1 s |
| Trap → Zabbix item refreshed via check-now | ≤ 5 s |
| Fault injected → Zabbix business service status changed | ≤ 10 s |
| Without traps (polling only) | ≤ 70 s |

---

## 10. NetBox data model (intent)

- Region `Lab`; sites `DC-A`, `DC-B`, `BR-01`…`BR-04`.
- Manufacturer `NetSim`; device type `NetSim PE`; platform `netsim-clixon`; roles `hub`, `spoke`.
- Devices named exactly as hostnames. Interfaces:
  - `Mgmt0` (virtual, mgmt_only, IP `/24`, device `primary_ip4`)
  - `Loopback0` (virtual, IP `/32`, IP role `loopback`)
  - `GigabitEthernet0/0/0` (MTU 9000), access interface (MTU 1500), subinterfaces (virtual, `parent` set, VLAN assigned; 03-netbox documents exactly how VLAN is stored)
- Tenants = customers (tenant group `L2VPN Customers`), with tenant custom field `sla_tier` (selection gold|silver).
  Default lab tiers: Customer-A gold, Customer-B gold, Customer-C silver, Customer-D silver.
- **One NetBox L2VPN object per pseudowire**: name `SVC-0001-primary`, type `vpws`, `identifier` = vc-id, tenant = customer,
  tag `l2vpn-pw`, custom fields `service_id` (text) and `pw_role` (selection primary|backup).
  Its two ends (changed 2026-10-19, see the change log): **hub end = the L2VPN's termination** (the hub subinterface);
  **spoke end = custom field `spoke_ac`** (object → `dcim.interface`, the spoke subinterface), because NetBox allows one
  L2VPN termination per interface and both PWs of a service share the spoke subinterface.
- Interface details (03-netbox): physical interfaces carry custom field `if_role` (selection `core-uplink` | `access` =
  the YANG `type`); a subinterface is an interface with a `parent`, and its VLAN is an IPAM VLAN (group `l2vpn-acs`, one per
  service) assigned as its single tagged VLAN (802.1Q mode `tagged`). PE devices are those with platform `netsim-clixon`.
- **Intent rendering rule**: for each device, every L2VPN ending on it (spoke end or hub end) becomes a PW in xconnect
  `service_id` with `role = pw_role`, `vc-id = identifier`, `peer-ipv4` = the other end's Loopback0 IP, AC = this end's
  subinterface. Rendered intent for a freshly seeded NetBox **must equal** `contract/golden/<host>.json` (canonical JSON).
- **BSM spec rendering**: `netbox-intent bsm-spec` builds the §9.3 document from NetBox only (tenants → customers,
  `service_id` groups → services, L2VPN objects → PW leaves, Loopback0 IPs → expected peers).

---

## 11. Credentials & config

- All secrets in `.env` (gitignored); `.env.example` committed with placeholders.
- Tokens: `NETBOX_TOKEN_RW` (seeding/remediation), `NETBOX_TOKEN_RO` (read-only for bot), `ZABBIX_API_TOKEN`, `LAB_API_TOKEN` (write endpoints).
- RESTCONF has no auth (lab only); never publish device ports beyond 127.0.0.1.
- `make env` (also run by `make up`) creates `.env` from `.env.example`, replacing each `__generate__` value with a random
  secret; it never changes existing values and only appends variables that are new in the template. Empty values (the
  NetBox/Zabbix tokens) are written by the owning component's bootstrap.

---

## 12. Global Definition of Done

On a clean Linux host with Docker:

```
make net && make up && make seed && make e2e
```

all succeed; `make down && make up` preserves device running config (drift survives reboot);
`make reset` returns every device to golden config and clears all faults.

---

## 13. Change log

| Date | Session | Change | Reason |
|---|---|---|---|
| 2026-10-18 | initial | Contract created | — |
| 2026-10-18 | design | Zabbix 7.0 → 8.0 LTS. The BSM is built from a NetBox-generated spec; Zabbix checks each PW leaf against it (`missing` / `oper` / `spec`); removed `{$PW.ALERT.ON.REMOVED}`; added tenant `sla_tier`. | Monitoring should know intent: a PW deleted or repointed on a device must not stay silently green. |
| 2026-10-18 | 01-foundation | §2: recorded the versions pinned by the foundation (pysnmp 7.1.30, pysmi 2.0.0, libyang 6.4.13, Clixon/CLIgen 7.9.0 as latest tag, …). | One place for later sessions to pin the same versions. |
| 2026-10-18 | 01-foundation | §3: added `tools/`, `tests/`, topology schema, YANG examples/tree (01-foundation); component make targets live in `<component>/<component>.mk`; `make seed/reset/e2e` delegate to `netbox-seed`+`zabbix-provision`, `lab-reset`, `lab-e2e`; root compose sets `name: netsim`; other sessions may append their variables to `.env.example`. | Parallel sessions never edit the same Makefile/compose lines; stable volume names keep drift across `make down && make up`. |
| 2026-10-18 | 01-foundation | §4: host ports written as `base + last octet` (18000/19000/16100). Same values as the table. | Unambiguous rule that also covers generated scale topologies with 3-digit octets. |
| 2026-10-18 | 01-foundation | §6: canonical form defined exactly (`pseudowire` sorted primary-first = YANG enum order; defaults filled; empty containers dropped; indent 2 + newline); effective MTU of a physical interface without `mtu` is 1500, PW MTU = AC effective MTU; YANG details (mandatory leaves, `must` rules, lengths tied to ifAlias/cpwVcRemoteIfString/cpwVcDescr, `SVC-[0-9]{4}` instead of `SVC-\d{4}`); golden description rules. | The contract left these open; simd (MTU rule 9), the intent renderer and the drift checker must agree byte-for-byte. `[0-9]` avoids Unicode-digit and regex-engine differences (Clixon translates patterns to POSIX). |
| 2026-10-18 | 01-foundation | §8: `TRAP_TARGETS` default `172.30.10.42:1162` (from the zabbix-docker 8.0 snmptraps Dockerfile, still to be confirmed by 04-zabbix); `oids.yaml` format; `ciscoConfigManEvent` has four varbinds in the pinned MIB revision (adds `ccmHistoryEventTerminalUser`). | Resolved from the pinned files, not memory; simd must send exactly these varbinds. |
| 2026-10-18 | 01-foundation | §9.2: exact `{$NETSIM.PW.SPEC}` entry keys and ordering. §9.3: optional `netbox_url` on every node, builders, `normalized()` comparison, 1–2 PW leaves per service. | Sessions 03 and 04 must produce/consume the same macro and spec; additive only, the contract example stays valid. |
| 2026-10-19 | 02-device-simulator | §2: device image and `core` pins (see §2). | One place for later sessions to find the versions. |
| 2026-10-19 | 02-device-simulator | §4: device containers are named after their hostnames (`core`, `hub-a`, ...), so `docker stop hub-a` works; volume `netsim_<host>-data` at `/var/lib/netsim` (Clixon datastores + simd state). RESTCONF: use `GET /restconf/data?content=config` for config (without `content`, Clixon adds state modules such as `ietf-yang-library`, `ietf-netconf-monitoring`, `ietf-restconf-monitoring`); `PUT /restconf/data` with `{"ietf-restconf:data": {...}}` replaces the whole config in one commit (simd uses it for the golden config; a reset can too). RESTCONF commits are made as user `restconf` (`CLICON_ANONYMOUS_USER`), CLI commits as the unix user (`root` via `docker exec`). | Additive. Facts other sessions need to drive and reset devices. |
| 2026-10-19 | 02-device-simulator | §6/§8: every device emits the standard RFC 6470 `ietf-netconf-notifications:netconf-config-change` (changed-by user, edit target + operation) on the RFC 5277 `NETCONF` stream after each commit that changed running; RESTCONF clients subscribe with `GET /streams/NETCONF` (SSE). First boot loads `contract/golden/<host>.json` only if the datastore is empty and the volume has no `simd/provisioned.json` marker; later boots never touch the config. | Additive. simd's config watch (commit -> simd in ~5 ms, 2 s hash poll as safety net); lab-api/the bot can use the same stream. |
| 2026-10-19 | 02-device-simulator | §7: `core` runs with `notify-keyspace-events KEg$x`; simd recomputes on every `netsim:events` message, fault/cut key change and advert expiry, so a plain `SET`/`DEL` of a fault key acts in ~0.1 s (publishing is optional). simd publishes `{"type":"state","host","boot_id","seq","ts"}` when its advert content changes; adverts carry an extra `ts`; `netsim:lo:<loopback>` is written. A fault key is active only with value `1`; `netsim:fault:ac:<host>:<ifname>` applies to any interface name (a fault on the physical access interface takes its subinterfaces down, so their PWs get `ac-down`). Edge cases: peer = own loopback -> `peer-unreachable`; several adverts with the same loopback -> first hostname; several matching remote PWs -> first by (service, role). | The contract left the message format, wake-up mechanism and these cases open; additive. |
| 2026-10-19 | 02-device-simulator | §8: `cpwVcUp` when a row enters `up`; `cpwVcDown` for every other oper transition (into `down` or `lowerLayerDown`, also between them) with the row's actual new value in both varbinds (`lowerLayerDown(7)` although the MIB text names `down(2)`). Transitions are batched for 100 ms and coalesced per run of consecutive cpwVcIndex values with the same transition (the MIB's index range; e.g. a hub uplink fault -> one `cpwVcDown` for 1..6); none is suppressed. No PW traps for the initial state at boot (`coldStart` is sent once the agent serves); a new row that comes up sends `cpwVcUp`; a deleted row sends nothing (row withdrawn). `ciscoConfigManEvent`: commandLine / commandSource / running / TerminalUser = commit user, once per detected running-config change. `TRAP_TARGETS` set but empty disables traps. Values: `sysObjectID` = `zeroDotZero`, `sysLocation` = site, `ifType` `ethernetCsmacd`/`l2vlan`, `cpwVcType` `ethernetVLAN` (subinterface AC) / `ethernet`, `cpwVcPsnType` `mpls`, inbound/outbound oper = `cpwVcOperStatus`, `cpwVcRemoteIfMtu`/`RemoteIfString` from the peer advert (0 / empty when unknown), `cpwVcUpTime` 0 while not up. | "On every oper transition" includes `lowerLayerDown`, which the MIB has no notification for; the range form is what the MIB asks for against storms. Zabbix (04) matches traps by OID and only needs them as hints. |
| 2026-10-19 | 03-netbox | §10: a PW L2VPN has **one** termination, the hub subinterface; its spoke end is the object custom field `spoke_ac` (→ the spoke subinterface). The rendering rule reads "every L2VPN ending on the device (spoke end or hub end)". Decided with the user; rejected: one L2VPN per service (breaks identifier = vc-id and the per-leaf `netbox_l2vpn_id` of §9.3) and both ends as custom fields (NetBox would show no terminations). | NetBox 4.7.3 allows one L2VPN termination per interface (`L2VPNTermination.clean()`), and a service's primary and backup PWs share the spoke subinterface, so two terminations per PW cannot be stored. Each hub subinterface carries exactly one PW. |
| 2026-10-19 | 03-netbox | §10 additions: custom field `if_role` (dcim.interface, selection `core-uplink`/`access`) carries the YANG `type` of physical interfaces; a subinterface = an interface with `parent`, VLAN stored as an IPAM VLAN (group `l2vpn-acs`, scope region `Lab`, one per service named after it) assigned as the subinterface's single tagged VLAN (802.1Q mode `tagged`, no untagged VLAN); physical interfaces are NetBox type `1000base-t`, Mgmt0/Loopback0/subinterfaces `virtual`; PE devices = platform `netsim-clixon`; loopback = the device's IP with role `loopback`; every seeded object is tagged `netsim-lab` (`--prune` only deletes those); L2VPN `status` is informational (intent always renders `enabled: true`). NetBox 4.7 returns selection custom fields as `{"value", "label"}`. | The contract left the YANG interface type and VLAN storage open; `tagged` is what a dot1q subinterface is (frames tagged with that VID on the parent), and a VLAN object keeps referential integrity and shows on the VLAN's page. |
| 2026-10-19 | 03-netbox | §4: `netbox-worker` 172.30.10.31 (RQ worker; webhooks leave from it); NetBox listens on 8080 inside labnet (`http://172.30.10.30:8080`; host `127.0.0.1:8000`); PostgreSQL/Valkey only on the internal network `netbox-internal`. | Webhooks are sent by the worker, which needs labnet; a static address keeps Docker's dynamic allocation off the fixed addresses of later containers. |
| 2026-10-19 | 03-netbox | §9.3: NetBox references in the BSM spec: customers tag `netbox_tenant_id` + tenant URL; PW leaves `netbox_l2vpn_id` (field and tag) + L2VPN URL; services the URL of their L2VPN list (`?cf_service_id=`); root the list of `l2vpn-pw` L2VPNs. URL base `NETBOX_PUBLIC_URL` (default `http://127.0.0.1:8000`, what a browser opens). | Additive; `normalized()` drops them. |
| 2026-10-19 | 03-netbox | §10/§11 NetBox change notifications: webhook `netsim lab-api: intent changed` → `POST http://172.30.10.50:8090/hooks/netbox`, NetBox's standard JSON payload, signed with HMAC-SHA512 of the body in header `X-Hook-Signature` (secret `NETBOX_WEBHOOK_SECRET`); event rule on device, interface, IP address, tenant, L2VPN and L2VPN termination create/update/delete. NetBox sends one webhook per changed object (a seed run can send dozens), so lab-api should verify the signature and debounce before re-running zabbix-sync. Created by `netbox-seed` (`--no-webhooks` skips it). | The prompt's notification path, made verifiable; debouncing is lab-api's job (05). |
| 2026-10-19 | 03-netbox | §11 tokens: NetBox 4.7 uses v2 tokens `nbt_<12-char key>.<40-char secret>`, sent as `Authorization: Bearer <token>`; NetBox stores only an HMAC digest (pepper `API_TOKEN_PEPPERS[1]` = `NETBOX_API_TOKEN_PEPPER`, ≥ 50 characters) and generates every plaintext itself (4.7 ignores client-supplied values). `netbox-bootstrap` (run by `make netbox-up` and `make netbox-seed`) writes `NETBOX_TOKEN_RW` (superuser, via `/api/users/tokens/provision/`) and `NETBOX_TOKEN_RO` (user `netsim-bot`: group `netsim-readonly` with `view` on every public object type except the `users` app and webhooks, token `write_enabled=false`) into `.env`, keeps valid ones, replaces invalid ones (e.g. after the database was recreated). New `.env.example` variables `NETBOX_API_TOKEN_PEPPER`, `NETBOX_WEBHOOK_SECRET`; `tools/gen_env.py` generates 64 characters for keys ending in `PEPPER`. A container that read `.env` before the bootstrap ran must be restarted to see the tokens. | Token format and provisioning changed across 4.x; the pepper must be ≥ 50 characters or NetBox refuses to start, and `make env` generated 32. |
| 2026-10-19 | 04-zabbix | §3: `zabbix/zabbix.mk` adds `zabbix-images` as a prerequisite of the root `make up` and `make build` (rule `up build: zabbix-images`; the root recipes are unchanged). The Zabbix 8.0.0rc1 images exist in no registry and need Zabbix's multi-phase bake, which a compose `build:` section cannot express; once built the step is a no-op (0.2 s). | `make net && make up` must work on a clean host (§12). |
| 2026-10-19 | 04-zabbix | §8: **trap port confirmed**: the Zabbix 8.0.0rc1 snmptraps container runs `snmptrapd ... udp:1162 udp6:1162` (entrypoint code and `netstat -lnu` on the running container), so `TRAP_TARGETS=172.30.10.42:1162` stays. zabbix-snmptraps runs snmptrapd with `-OnTt`: OIDs numeric (the trap items match the `snmpTrapOID.0` varbind by numeric OID), values decoded through the mounted `contract/mibs` (e.g. `lowerLayerDown`, `restconf`). Sources are matched to the hosts' SNMP interfaces by the UDP source address (no `snmpTrapAddress.0` needed). | The prompt's open item; numeric matching keeps trap items independent of MIB loading. |
| 2026-10-19 | 04-zabbix | §2: Zabbix 8.0.0rc1 + PostgreSQL 18.6 pins (see §2), images built from Zabbix's Dockerfiles because no pre-release tags exist on Docker Hub; planned bump to 8.0.0 GA images. | No GA yet; `trunk` images are an unreleased 8.0.0rc2 nightly. |
| 2026-10-19 | 04-zabbix | §9.2 additions: a Zabbix 8.0 macro value holds at most **2048 characters** (`hostmacro.value varchar(2048)`), about 12 spec entries. `{$NETSIM.PW.SPEC}` keeps the contract format and is exactly `pw_spec_macro_value(spec, host)` whenever it fits (every lab host: a hub is ~1 KB); a longer spec continues in `{$NETSIM.PW.SPEC.2}` .. `{$NETSIM.PW.SPEC.8}` (same compact JSON-list format, entries in order, template default `[]`), ~96 PWs per host. Zabbix names consumers may rely on: items `netsim.pw.<present\|oper\|admin\|peer\|vcid\|remote_mtu\|spec_diff\|row>[<service>,<role>]` (expected PWs; `row` is JSON), `netsim.pw.observed.<oper\|peer\|vcid\|expected>[<cpwVcIndex>]`, `netsim.if.<oper\|admin\|mtu>[<ifIndex>]`, master `netsim.snmp.walk`; problem tags as §9.2 plus `scope=l2vpn-trap` (`trap=pw\|config\|boot`, Information, one event per trap), `scope=interface`, `scope=availability`. | Additive; the macro limit is a hard Zabbix constraint the format must survive. |
| 2026-10-19 | 04-zabbix | §9.4 as implemented in Zabbix 8.0 services: `redundant-pair` = main rule "set status to OK" + additional rules "≥ 2 children ≥ High → Disaster", "≥ 1 child ≥ Average → Warning" (8.0 additional rules can only raise the main rule's status; the highest match wins); customer/root "most critical of child services"; leaf problem tags `scope`, `service`, `pw_role` (equals). Every node also carries `managed_by=netsim-zabbix` (only those are updated/pruned) and service nodes the derived tag `sla_tier` (their customer's), so the SLAs `L2VPN gold` (99.95 %) / `L2VPN silver` (99.9 %), monthly, UTC, select customers and their services. | The contract's rule maps onto 8.0 only this way; the derived tag puts each customer service into its SLA report. |
| 2026-10-19 | 04-zabbix | §9.5 **trap → check-now bound**: Zabbix 8.0 processes "execute now" tasks (`task.create` type 6, used by the check-now webhook) on a fixed 5 s grid (`ZBX_TM_PROCESS_PERIOD 5` in `taskmanager_server.c`, not configurable, nothing wakes it), so trap → walk refreshed is ≤ 5 s at the median (measured 2.9 s; 30 of 36 traps ≤ 5 s) but up to ~6 s in the worst case (measured max 5.9 s). e2e should assert the median ≤ 5 s and every sample ≤ 7 s. Fault → BSM status change stays within 10 s (measured ≤ 5.2 s; device power-off 4.5 s). | Measured; the 5 s budget cannot hold for every sample with Zabbix's own check-now. |
| 2026-10-19 | 04-zabbix | §11: `ZABBIX_API_TOKEN` is generated by `zabbix-bootstrap` (`make zabbix-up`) for user `ZABBIX_API_USER` (`netsim-api`): user type Admin (needed to "execute now" discovery rules), role `NetSim API` with an API **allow-list** (`*.get`, configuration import/export, host/usermacro/service/sla/map/dashboard/action writes, `task.create`, `event.acknowledge`), read-write only on `L2VPN/Hubs`, `L2VPN/Spokes`, `Templates/NetSim`. Zabbix 8.0 disables API access for every role of a new installation except Super admin, so the role enables it. The webhook reads the token from the secret global macro `{$NETSIM.ZABBIX.API.TOKEN}` and the API at `{$NETSIM.ZABBIX.API.URL}` (`http://172.30.10.41:8080/api_jsonrpc.php`). `EnableGlobalScripts` stays 0: it only gates "Script"-type commands, not webhooks (`scripts.c`). Kept tokens are verified; a stale one is replaced and written into `.env`. | Minimum rights for provisioning, sync, check-now and lab-api; 8.0 defaults made the API opt-in. |
| 2026-10-20 | 05-integration | §9.2: the observed view's `unexpected` trigger is also tagged `service` and `pw_role` (LLD macros `{#SERVICE}`/`{#ROLE}` split from `cpwVcName` by `observed_lld.js`; template regenerated and imported). It still has `scope=l2vpn-unexpected`, so it stays out of the BSM. | The prompt's fake-redundancy check correlates the three problems of one moved PW "from the API data alone (same service tag)"; the `unexpected` problem had no `service` tag. |
| 2026-10-20 | 05-integration | §7: lab-owned keys on `core`: `netsim:lab:baseline:<host>` = canonical config on which the device and NetBox last agreed (drift baseline; absent = golden). Written by lab-api/labctl when intent is applied or a check sees device == intent, deleted by reset; not persisted (core keeps no data), so a restart of `core` means golden again. simd never reads them. | The drift checker must tell `config-drift` (the device moved) from `intent-only` (NetBox moved, device not updated yet); that needs the last agreed state. |
| 2026-10-20 | 05-integration | §4/§11 lab-api: container port 8090 at 172.30.10.50 (host 127.0.0.1:8090; NetBox's webhook URL unchanged). It mounts the repository read-only and reads `.env` on every request (tokens written later by the bootstraps are used without a restart; a single-file mount would keep the old inode after the bootstraps' atomic rewrite) and the Docker socket (power off/on of the six PEs only). Writes need `X-Lab-Token`; `X-Lab-Actor` names the caller in the audit log (JSONL on volume `netsim_lab-api-data`). Device changes need the `plan_id` of a dry run (`confirm=true`); a stale `plan_id` gets HTTP 409. | The prompt's propose -> approve -> apply -> verify loop, made enforceable: the approved diff is exactly what is applied. |
| 2026-10-20 | 05-integration | §3/§12: `make seed`, `make reset` and `make e2e` first wait until every container is running and healthy (`lab-wait-healthy`, prerequisite added from `lab/lab.mk`, root recipes unchanged); `make build` also builds `netsim/lab-api:0.1.0`. | `make up && make seed` on a clean host: NetBox's first start takes ~4 min and the seeders must not race it. |
| 2026-10-20 | 05-integration | §9.5 as measured by `make e2e`: fault -> business service status is timed from the end of the scenario's last step to the first `service.get` showing every expected status; budget **10 s** for fault keys, cuts and config changes (measured 3.0-6.2 s) and **12 s for a device power-off** (`docker stop`); trap -> walk re-polled: median <= 5 s and every sample <= 7 s (04's bound); polling only = every PE recreated with `TRAP_TARGETS=""`, budget 70 s. | A power-off is only noticed when the advert expires (3 s hold time, §7; measured 2.4-2.5 s), then the trap's check-now waits for Zabbix 8.0's fixed 5 s task-manager grid (04): power-off -> service Warning measured 5.4-9.7 s over 11 samples, median 7.9 s, worst case by construction ~10.3 s. |
