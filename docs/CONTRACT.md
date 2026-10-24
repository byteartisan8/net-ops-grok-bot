# NetSim L2VPN Lab — Shared Contract

**This file is the single source of interface truth for every build session.**
If your work needs something that contradicts this file, do not silently diverge:
add an entry to the [Change log](#13-change-log) with the reason, make the smallest
compatible change, and mention it in your final report.

---

## 1. What we are building

A lab that simulates a hub-and-spoke L2VPN (VPWS pseudowire) network with redundancy,
monitored by Zabbix, with NetBox as source of truth. The NOC sits on top (`bot/`, grok-bot: sessions 07-09; out of
scope for the foundation build): three NOC roles, each a routine of the user's Grok Bot app, paged by Zabbix through
grok-bot (§9.6) and working through its tools. Through lab-api they must be able to:

- read **intent** from NetBox,
- read **running config** from each device (Clixon RESTCONF),
- read the **Clixon controller**'s view of the routers (since 10-clixon-controller, §9.7): which ones differ from
  the config the lab last put there on purpose (out of sync), read only,
- read **observed state** from Zabbix (problems, items, business services),
- push **remediation** to devices (as change tickets a human approves) and verify it through Zabbix.

The lab itself (labctl, lab-api) injects **faults** and **scenarios**; the NOC roles never do.

Core principle: **traps are hints, polling is truth.** **Config is intent/drift; faults are physical failures.**
These two must never be mixed up in any component.

```
                ┌──────────── NetBox (intent / SoT) ─────────────┐
                │                                                 │
   lab-api ─────┤                                                 ├── NetBox→Zabbix sync
   (bot tools)  │                                                 │   (hosts, services tree)
                ▼                                                 ▼
  ┌─────────────────────── device container (x10) ──────────┐   Zabbix server
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

Pinned by 10-clixon-controller (2026-10-24): **clixon-controller 1.9.0** (commit `53631aae4260`, released 2026-10-15,
the newest release), built from source on the **same Clixon/CLIgen 7.9.0 commits as the PEs**: it compiles with
`-Werror` and runs on them, so the lab runs one Clixon release and no other version had to be pinned. Image
`netsim/clixon-controller:1.9.0` on `python:3.12.15-slim-trixie` with supervisor 4.3.0 (hash from `uv.lock`). OpenSSH
from Debian trixie (`1:10.0p1-7+deb13u4` at build time) in the controller, the PE image (sshd for NETCONF) and lab-api
(client).

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
| `<component>/<component>.mk` (`devices/devices.mk`, `netbox/netbox.mk`, `zabbix/zabbix.mk`, `lab/lab.mk`, `bot/bot.mk`, `controller/controller.mk`) | that component's owner | its make targets; included by the root `Makefile`, overrides the "not implemented yet" placeholders |
| `devices/` | 02-device-simulator | image, simd, snmpsim data, `devices/compose.yaml` |
| `netbox/` | 03-netbox | `netbox/compose.yaml`, seeder, intent renderer |
| `zabbix/` | 04-zabbix | `zabbix/compose.yaml`, template, provisioning, dashboards |
| `lab/` | 05-integration | labctl, lab-api, scenarios, e2e tests, `lab/compose.yaml` |
| `bot/` | 07-grok-bot (three NOC roles since 09-noc-roles) | grok-bot (the NOC office of three roles, the NOC room page, the routine sink, botctl), its tests and e2e, `bot/grokbot-app/` (the Grok Bot app's side; `terminal/`: `noc-controller`, the roles' terminal on their Bots' own computers, since 11-bot-terminal), `bot/compose.yaml`, `bot/bot.mk` |
| `controller/` | 10-clixon-controller | the Clixon controller: image, Clixon settings, the startup datastore generated from the topology (PEs, NACM), sshd, the connect agent, `controller/compose.yaml`, `controller/controller.mk`, tests (`make controller-test`) |

Root `compose.yaml` only contains `include:` of the component fragments (four, plus `bot/compose.yaml` since 07 and `controller/compose.yaml` since 10). All fragments attach to the
**external** network `labnet` (created by `make net`), so fragments never fight over network definitions.
The root file also fixes the compose project name (`name: netsim`), so volume names are stable (`netsim_<volume>`).

Make targets: components define theirs in `<component>/<component>.mk` (prefix `devices-`, `netbox-`, `zabbix-`, `lab-`, `bot-`, `controller-`).
Root targets delegate: `make seed` → `netbox-seed` then `zabbix-provision`; `make reset` → `lab-reset`; `make e2e` → `bot-e2e`, `controller-test`, then `lab-e2e`; `make lint` also runs `bot-lint-terminal` (11-bot-terminal).
`.env.example` belongs to 01-foundation; other sessions may append variables for their component in its section
(and log it here).

---

## 4. Network & addressing

Docker network `labnet`: `172.30.10.0/24`, gateway `172.30.10.1`, created externally.

| Host | Role | Site | Mgmt IP (labnet) | Loopback0 (simulated) |
|---|---|---|---|---|
| `core` | Redis signaling bus | — | 172.30.10.5 | — |
| `hub-a` | hub (North primary) | DC-A | 172.30.10.11 | 10.255.0.1 |
| `hub-b` | hub (North backup) | DC-B | 172.30.10.12 | 10.255.0.2 |
| `hub-c` | hub (South primary) | DC-C | 172.30.10.13 | 10.255.0.3 |
| `hub-d` | hub (South backup) | DC-D | 172.30.10.14 | 10.255.0.4 |
| `spoke-1` | spoke | BR-01 | 172.30.10.21 | 10.255.1.1 |
| `spoke-2` | spoke | BR-02 | 172.30.10.22 | 10.255.1.2 |
| `spoke-3` | spoke | BR-03 | 172.30.10.23 | 10.255.1.3 |
| `spoke-4` | spoke | BR-04 | 172.30.10.24 | 10.255.1.4 |
| `spoke-5` | spoke | BR-05 | 172.30.10.25 | 10.255.1.5 |
| `spoke-6` | spoke | BR-06 | 172.30.10.26 | 10.255.1.6 |
| `netbox` | NetBox web/API | — | 172.30.10.30 | — |
| `netbox-worker` | NetBox background worker (sends the webhooks) | — | 172.30.10.31 | — |
| `zabbix-server` | Zabbix server | — | 172.30.10.40 | — |
| `zabbix-web` | Zabbix frontend/API | — | 172.30.10.41 | — |
| `zabbix-snmptraps` | snmptrapd | — | 172.30.10.42 | — |
| `lab-api` | lab/bot tool API | — | 172.30.10.50 | — |
| `grok-bot` | the NOC office + NOC room page (07, 09) | — | 172.30.10.60 | — |
| `routine-sink` | stand-in for the roles' Grok Bot routines (09; compose profile `sink`, tests) | — | 172.30.10.61 | — |
| `controller` | the Clixon controller (10, §9.7): NETCONF over SSH to the PEs; SSH :22 for lab-api only | — | 172.30.10.70 | — |

Loopbacks are **not** real container IPs. They exist only in config and signaling and are the PW peer addresses.
Traps must leave the device container from its mgmt IP (same bridge, no NAT), because Zabbix matches trap source to host interface IP.

### Ports published to the host (127.0.0.1 only)

| Service | Host port |
|---|---|
| NetBox UI/API | 8000 |
| Zabbix UI/API | 8080 |
| lab-api | 8090 |
| grok-bot (NOC room, bot API, `/mcp`) | 8095 |
| routine-sink (profile `sink`) | 8096 |
| controller | none: its SSH (22) and the PEs' NETCONF over SSH (830) are reachable on labnet only |
| Device RESTCONF (container :8080) | `180<last-octet>` → hub-a 18011 … hub-d 18014, spoke-1 18021 … spoke-6 18026 |
| Device simd API (container :9000) | `190<last-octet>` → 19011 … 19014, 19021 … 19026 |
| Device SNMP (container :161/udp) | `161<last-octet>`/udp → 16111 … 16114, 16121 … 16126 |

Numerically: host port = 18000 / 19000 / 16100 + last octet of the mgmt IP (same values as above; also valid for
3-digit octets in generated scale topologies). Encoded in `contract/topology.yaml` (`device_ports`), computed by
`netsim_common.topology.Topology.host_port()` / `netsim_common.constants.lab_constants()`.

### Device container ports

| Port | Purpose |
|---|---|
| 8080/tcp | Clixon native RESTCONF (HTTP, no TLS, no auth — lab only) |
| 161/udp | SNMP v2c agent (snmpsim) |
| 9000/tcp | simd HTTP API |
| 830/tcp | NETCONF over SSH (RFC 6242; 10-clixon-controller): user `controller`, key only (the controller's public key from the shared volume `netsim_controller-keys`), from 172.30.10.70 only; every session is `clixon_netconf`; not published |
| CLI | `docker exec -it <host> clixon_cli` (autocli generated from YANG) |

---

## 5. Services (customer L2VPNs)

Two regions, each with its own hub pair (NetBox child regions of `Lab`, section 10):

| Region | Hubs (primary, backup) | Spokes | Sites |
|---|---|---|---|
| North | hub-a, hub-b | spoke-1 … spoke-4 | DC-A, DC-B, BR-01 … BR-04 |
| South | hub-c, hub-d | spoke-5, spoke-6 | DC-C, DC-D, BR-05, BR-06 |

Each service = one spoke attachment circuit with a **primary PW** to its region's primary hub (hub-a, hub-c) and, if
the service is **protected**, a **backup PW** to the region's backup hub (hub-b, hub-d): dual-homed. An
**unprotected** service (the bronze customer's) has the primary PW only: single-homed, so one hub is a single point of
failure. VC ID scheme: primary `1000+n`, backup `2000+n`. VLAN scheme: `100*c + j` (c = customer number A=1, B=2,
…; j = that customer's j-th service).

| Service | Customer (tier) | Spoke | VLAN | Spoke AC | Hub AC (every hub of the service) | Primary VC (hub) | Backup VC (hub) |
|---|---|---|---|---|---|---|---|
| SVC-0001 | Customer-A (gold) | spoke-1 | 101 | GigabitEthernet0/0/1.101 | GigabitEthernet0/0/2.101 | 1001 (hub-a) | 2001 (hub-b) |
| SVC-0002 | Customer-A (gold) | spoke-2 | 102 | GigabitEthernet0/0/1.102 | GigabitEthernet0/0/2.102 | 1002 (hub-a) | 2002 (hub-b) |
| SVC-0003 | Customer-B (gold) | spoke-2 | 201 | GigabitEthernet0/0/1.201 | GigabitEthernet0/0/2.201 | 1003 (hub-a) | 2003 (hub-b) |
| SVC-0004 | Customer-C (silver) | spoke-3 | 301 | GigabitEthernet0/0/1.301 | GigabitEthernet0/0/2.301 | 1004 (hub-a) | 2004 (hub-b) |
| SVC-0005 | Customer-D (silver) | spoke-4 | 401 | GigabitEthernet0/0/1.401 | GigabitEthernet0/0/2.401 | 1005 (hub-a) | 2005 (hub-b) |
| SVC-0006 | Customer-B (gold) | spoke-4 | 202 | GigabitEthernet0/0/1.202 | GigabitEthernet0/0/2.202 | 1006 (hub-a) | 2006 (hub-b) |
| SVC-0007 | Customer-E (gold) | spoke-5 | 501 | GigabitEthernet0/0/1.501 | GigabitEthernet0/0/2.501 | 1007 (hub-c) | 2007 (hub-d) |
| SVC-0008 | Customer-E (gold) | spoke-1 | 502 | GigabitEthernet0/0/1.502 | GigabitEthernet0/0/2.502 | 1008 (hub-a) | 2008 (hub-b) |
| SVC-0009 | Customer-F (bronze) | spoke-5 | 601 | GigabitEthernet0/0/1.601 | GigabitEthernet0/0/2.601 | 1009 (hub-c) | — (unprotected) |
| SVC-0010 | Customer-F (bronze) | spoke-4 | 602 | GigabitEthernet0/0/1.602 | GigabitEthernet0/0/2.602 | 1010 (hub-a) | — (unprotected) |
| SVC-0011 | Customer-C (silver) | spoke-6 | 302 | GigabitEthernet0/0/1.302 | GigabitEthernet0/0/2.302 | 1011 (hub-c) | 2011 (hub-d) |

Customer-E and Customer-C have services in both regions; spoke-4 carries all three tiers (gold, silver, bronze).

Physical interfaces on every device:
- `GigabitEthernet0/0/0`: `core-uplink`, MTU 9000
- spokes: `GigabitEthernet0/0/1` (`access`, MTU 1500); hubs: `GigabitEthernet0/0/2` (`access`, MTU 1500)
- subinterfaces: type `subinterface`, `parent` = access interface, `vlan-id`, MTU inherited from parent unless overridden

Totals: 20 PWs (9 protected services × 2 + 2 unprotected × 1); hub-a holds 8, hub-b 7, hub-c 3, hub-d 2; spokes
hold 2 to 5.

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
- A hub's xconnect normally contains only the PW whose role matches that hub (hub-a/hub-c → `primary`, hub-b/hub-d → `backup`), but the model **must** allow both roles on a hub (needed for drift scenarios). An unprotected service's xconnect has the `primary` PW only, on the spoke too.
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
      "vlan-id": 101, "enabled": true, "description": "Customer-A SVC-0001" },
    { "name": "GigabitEthernet0/0/1.502", "type": "subinterface", "parent": "GigabitEthernet0/0/1",
      "vlan-id": 502, "enabled": true, "description": "Customer-E SVC-0008" }
  ]},
  "netsim-device:l2vpn": { "xconnect": [
    { "service-id": "SVC-0001", "customer": "Customer-A", "attachment-circuit": "GigabitEthernet0/0/1.101", "enabled": true,
      "pseudowire": [
        { "role": "primary", "peer-ipv4": "10.255.0.1", "vc-id": 1001, "enabled": true },
        { "role": "backup",  "peer-ipv4": "10.255.0.2", "vc-id": 2001, "enabled": true }
      ] },
    { "service-id": "SVC-0008", "customer": "Customer-E", "attachment-circuit": "GigabitEthernet0/0/1.502", "enabled": true,
      "pseudowire": [
        { "role": "primary", "peer-ipv4": "10.255.0.1", "vc-id": 1008, "enabled": true },
        { "role": "backup",  "peer-ipv4": "10.255.0.2", "vc-id": 2008, "enabled": true }
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
  - `IF-MIB` ifTable/ifXTable for all config interfaces (ifName, ifDescr, ifAlias=description, ifAdminStatus, ifOperStatus, ifMtu); notifications `linkDown`, `linkUp`
  - `CISCO-IETF-PW-MIB` `cpwVcTable` columns at minimum: cpwVcType, cpwVcPsnType, cpwVcPeerAddrType, cpwVcPeerAddr, cpwVcID, cpwVcLocalIfMtu, cpwVcRemoteIfMtu, cpwVcRemoteIfString, cpwVcName, cpwVcDescr, cpwVcAdminStatus, cpwVcOperStatus, cpwVcInboundOperStatus, cpwVcOutboundOperStatus, cpwVcUpTime; notifications `cpwVcDown`, `cpwVcUp`
  - `CISCO-CONFIG-MAN-MIB` notification `ciscoConfigManEvent` (sent on every commit that changes running config)
- Encoding conventions:
  - `cpwVcName` = `<service-id>:<role>` e.g. `SVC-0001:primary`. Zabbix derives service tags from this.
  - `cpwVcDescr` = customer name.
  - `cpwVcPeerAddr` = 4-octet InetAddress (Zabbix must convert hex → dotted quad).
- Traps go to `TRAP_TARGETS` (env, default `172.30.10.42:<port confirmed by 04-zabbix>`), within ≤1 s of the transition.
  `.env.example` ships `172.30.10.42:1162` (zabbix-docker trunk, `MAJOR_VERSION=8.0`, snmptraps Dockerfile: `EXPOSE 1162/udp`); **confirmed by 04-zabbix**: snmptrapd in the 8.0.0rc1 snmptraps image listens on `udp:1162` and `udp6:1162`.
  - `cpwVcDown` / `cpwVcUp` on every oper transition (varbinds exactly as the MIB defines)
  - `linkDown` when an interface's ifOperStatus leaves `up(1)`, `linkUp` when it enters `up(1)`: every ifTable
    interface (core uplink, access ports, AC subinterfaces), one trap per interface that changed (a parent port going
    down sends one for itself and one per subinterface), varbinds exactly the MIB's (`ifIndex`, `ifAdminStatus`,
    `ifOperStatus` of that ifIndex); none for the baseline at start, as for PWs
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
- Trap items (matched by numeric `snmpTrapOID.0`, tag `trap`): `pw` (cpwVcDown, cpwVcUp), `link` (linkDown, linkUp;
  09), `config` (ciscoConfigManEvent), `boot` (coldStart). Each has a hint trigger tagged like the others, so the action
  `NetSim: SNMP trap -> check now` (template + trap tag) re-polls the router on a link trap too, unchanged. An AC or
  uplink fault therefore shows as `Interface <ifName> (<ifAlias>) on <host>: down, admin up` (scope `interface`)
  within seconds, not at the next 60 s walk.

### 9.2 Two discovery views per host
| View | Source | Purpose |
|---|---|---|
| **Expected PWs** (intent) | Host macro `{$NETSIM.PW.SPEC}`: a JSON list of this host's expected PWs, written by the NetBox→Zabbix sync from the BSM spec | Drives the leaf checks below. Items are dependent on the walk master; each one finds its row by `cpwVcName == "<service>:<role>"` |
| **Observed PWs** (state) | SNMP walk LLD over cpwVcTable | Raw visibility, and flags PWs on the device that NetBox doesn't know about |

`{$NETSIM.PW.SPEC}` value: compact JSON list (no spaces) of
`{"service", "role", "vc_id", "expected_peer", "expected_peer_host", "customer", "ac"}` objects, one per PW leaf end on
this host (`expected_peer` = peer address this host must use, `ac` = this host's AC), sorted by service then role
(primary first). Built from the BSM spec by `netsim_common.pw_spec_macro_value(spec, host)`
(`pw_spec_for_host()` returns the typed entries). hub-a, the busiest host, carries 8 entries (about 1.4 KB).

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
`status_rules` follows the number of PW leaves (`netsim_common.bsm.status_rules_for()`, enforced by the model):
two leaves → `redundant-pair`, one leaf → `unprotected` (an unprotected service, or a service of which NetBox currently
has only one L2VPN).

### 9.4 BSM tree and status rules
```
L2VPN                                    root: most critical child
└── Customer-A                           customer: most critical child; SLA from sla_tier
    └── SVC-0001 (spoke-1)               service: status_rules below
        ├── SVC-0001 primary (hub-a)     leaf: problems with scope=l2vpn-pw, service=SVC-0001, pw_role=primary
        └── SVC-0001 backup (hub-b)      leaf: problems with scope=l2vpn-pw, service=SVC-0001, pw_role=backup
```
- A leaf matches problems from **both ends** (same tags), so a dead device is still detected by its peers.
- `redundant-pair` service rules (two PWs): **≥2 children ≥ High → Disaster** (down); **≥1 child ≥ Average → Warning** (degraded or at risk); else OK.
- `unprotected` service rules (one PW): **≥1 child ≥ High → Disaster** (down: no second path); **≥1 child ≥ Average → Warning**; else OK.
- SLA objects per `sla_tier` (`gold` 99.95 %, `silver` 99.9 %, `bronze` 99.5 %) select services by tag.
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
| AC fault → `linkDown` → re-poll → interface problem in Zabbix (09; measured 4.5-4.8 s) | ≤ 10 s |
| Fault → NOC Engineer page sent (Zabbix alert + grok-bot's batch; 09; measured 14-22 s) | ≤ 30 s |
| Without traps (polling only) | ≤ 70 s |

### 9.6 Alerting: customer nodes → grok-bot → roles (09-noc-roles)

Zabbix decides who is paged; grok-bot turns its alerts into incidents and pages; the roles' Grok Bot routines work
them through `/mcp`.

- **Customer nodes** carry the service tag `bsm_level=customer` (`bsm_sync`; the other levels are `root`, `service`,
  `pw`), so an action can select customers only (service nodes carry `sla_tier` too).
- **Users** (zabbix-bootstrap): group `NetSim NOC` (read on `L2VPN/Hubs`, `L2VPN/Spokes`), role `NetSim NOC` (User
  type, every service readable, API allow-list `event.acknowledge`, `event.get`, `problem.get`), users
  `noc-engineer`, `network-engineer`, `noc-manager`, each with an API token (`ZABBIX_NOCENG_TOKEN`, ...) and the media
  `NetSim NOC (grok-bot)` with send-to = its role.
- **Media type** `NetSim NOC (grok-bot)` (webhook, `zabbix/tools/src/netsim_zabbix/js/noc_webhook.js`; 3 attempts 10 s
  apart): `POST {$NETSIM.BOT.HOOK.URL}` (`http://172.30.10.60:8095/hooks/zabbix`) with
  `Authorization: Bearer {$NETSIM.BOT.HOOK.TOKEN}` (secret macro = `BOT_ZABBIX_HOOK_TOKEN`), JSON
  `{source, role, kind, event_id, recovery_event_id, severity, event_severity, update_severity, status, event_time,
  update_time, recovery_time, event_name, service: {id, name, tags}, root_cause, action, subject}`. `kind` is
  `problem`, `update`, `recovery` or `escalation`, taken from the message templates (service events: problem,
  recovery, update) and the escalation step's own message. `root_cause` is `{SERVICE.ROOTCAUSE}` (one line per
  trigger problem with host, name, severity, age and tags). `severity` is the effective one (an update carries the new
  severity). Unresolved macros are sent as `null`.
- **Service action** `NetSim: customer alert` (eventsource 4; condition service tag `bsm_level` = `customer`; owned by
  zabbix-bootstrap because its operations name the role users): step 1 at once to `noc-engineer`; step 2 after
  `{$NETSIM.ESCALATE.AFTER}` (created as `10m`, the operator's to tune; at least 60 s), while the customer is not OK,
  to `noc-manager` (kind `escalation`); recovery and update operations to `noc-engineer`.
- **Zabbix 8.0.0rc1 behaviour this relies on** (verified on the running server, docs/reports/09-noc-roles.md):
  service events exist only for services an action matches; a customer going from Warning to Disaster (degraded →
  down) is an **update** of the open event (`{EVENT.UPDATE.SEVERITY}` = the new severity; `{EVENT.SEVERITY}` and
  `{EVENT.NAME}` keep the first; `{EVENT.UPDATE.STATUS}` stays 0), not a new event; service events cannot be
  acknowledged; `service.get` `selectProblemEvents` lists problem events on the PW leaves only, not on the customer
  node; `alert.get` shows service alerts to Super admins only; `mediatype.create` leaves a media type disabled unless
  `status` is given.
- **grok-bot** (`POST /hooks/zabbix`, Bearer `BOT_ZABBIX_HOOK_TOKEN`, never through a `BOT_MCP_HOSTS` host) stores
  every alert and batches per role: a batch closes `BOT_BATCH_S` (10 s) after its latest alert, at most
  `BOT_MAX_BATCH_S` (30 s) after its first. A NOC Engineer batch opens **one incident** with every alerted customer
  (tier, degraded or down, the trigger problems behind it) and sends **one page**; later alerts whose customers share
  hosts or services with an open incident join it without a page; degraded → down sends one update page; when every
  customer is OK, one `recovered` page to the NOC Engineer, who closes the incident. The NOC Manager is paged for a
  gold customer down, Zabbix's escalation step, change tickets to review (batched like alerts) and Ask-the-NOC
  questions; an escalation pages the role it is addressed to (Network Engineer or NOC Manager); a ticket the NOC
  Manager returns pages the engineer who filed it. Problems that touch no customer never page. In maintenance mode or
  with pages muted, alerts are only recorded.
- **Pages:** `POST BOT_ROUTINE_<ROLE>_URL` with `Authorization: Bearer BOT_ROUTINE_<ROLE>_KEY`, JSON `{source,
  version, page_id, event, role, sent_at, summary, incident, customers, how_to_respond, links, ...}` with `event` one
  of `incident.opened`, `incident.worse`, `incident.recovered`, `incident.escalated`, `incident.gold-down`,
  `incident.zabbix-escalation`, `tickets.filed`, `ticket.returned`, `question`, `test`. At most
  `BOT_PAGE_MAX_PER_HOUR` per role and hour; 3 tries, none after a 4xx.
- **Roles on `/mcp`:** the token (`BOT_NOCENG_TOKEN`, `BOT_NETENG_TOKEN`, `BOT_NOCMGR_TOKEN`) decides the role and its
  tools (bot/README.md); a tool of another role is refused naming the role that has it. **Two token kinds** (since
  11-bot-terminal): a role token is the role through its Bot's connector, every tool of the role; a role's **terminal
  token** (`BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`, `BOT_NOCMGR_TERMINAL_TOKEN`; empty: that terminal
  is off) is the same role for `controller_cli` only, the token of `noc-controller` on the Bot's own computer
  (§9.7): `initialize` gives the terminal's instructions (server `netsim-noc-<bot>-terminal`), `tools/list` lists
  `controller_cli` alone, `tools/call` of any other tool is refused naming the token kind (nothing reaches lab-api,
  no robot moves). Neither kind is accepted outside `/mcp` (operator API, `/hooks/zabbix`, the page), and every token
  must differ from every other one (grok-bot refuses the settings otherwise). The robots are NOCENG, NETENG,
  NOCMGR; the lab-api actor is `grok-bot/<ROLE>` (`grok-bot/<ROLE> approved-by:<who>` for an approved change). The
  only direct write is the NOC Engineer's acknowledgment through lab-api (`POST /zabbix/acknowledge`: the open trigger
  problems behind the incident's customers, as the Zabbix user `noc-engineer`, audited). Every other change is a
  ticket: an engineer files it (`review`), the NOC Manager proposes it to the operator (`pending`) or returns it to
  that engineer (`returned`), and only a human approves a proposed ticket; no role can approve or apply. A ticket the
  NOC Manager is not paged for, or has not reviewed within `BOT_REVIEW_TIMEOUT_S` (300 s), goes to the operator
  marked as not reviewed.
- **lab-api direct reads** for the roles (additive; lab-api keeps every credential): `POST /zabbix/api`
  `{method, params}` (allow-list `problem.get`, `event.get`, `trigger.get`, `item.get`, `history.get`, `trend.get`,
  `host.get`, `hostgroup.get`, `service.get`, `sla.get`, `sla.getsli`, `alert.get`, `maintenance.get`; anything else
  403), `GET /netbox/{path}` (GET-only passthrough to NetBox's `/api/`), `GET /devices/{host}/restconf[/{path}]`
  (`?content=config|nonconfig|all`); responses size-capped.

### 9.7 The Clixon controller: a second drift signal, read only (10-clixon-controller)

The Clixon controller manages the ten PEs over NETCONF and keeps a copy of each router's config; a router whose
running config differs from that copy is **out of sync**. The NOC roles read it through grok-bot's `controller_cli`
and never change anything through it: changes stay change tickets, applied by lab-api over RESTCONF after a human
approves. The controller is optional at run time.

- **Container** `controller` at 172.30.10.70 (§4), Clixon controller 1.9.0 (§2), all of it in `controller/`.
  Southbound: NETCONF over SSH (RFC 6242) to each PE's :830 as the PEs' user `controller`, with the controller's own
  key and strict host key checking (each PE's host key is learned on first contact and kept on the volume
  `netsim_controller-data`); the routers' YANG comes from the routers (RFC 6022 `get-schema`: `netsim-device`
  2026-10-18). Northbound: only sshd on :22, for lab-api (172.30.10.50) with lab-api's key, into the controller CLI
  (no shell, no forwarding). No RESTCONF; nothing published on the host. NACM on the PEs stays off.
- **Devices** are generated from `contract/topology.yaml` into the startup datastore (`make controller-config`,
  `controller/startup_db`): one per PE, by hostname, at its mgmt IP, profile `netsim-pe`; device groups `north` and
  `south`. The datastore is fresh at every start (`clixon_backend -s startup`); the container's agent then connects
  each PE once, as soon as it answers, within 10 minutes of the start.
- **The copy, in sync and out of sync.** The controller takes a router's running config as its copy whenever it
  connects to it (controller 1.9.0 syncs on every connect) or pulls it. lab-api resyncs as its controller user
  (`pull` a connected router, `connection open` a closed one) after a reset (`labctl reset`, `POST /reset`: every
  router), after it applied and verified a change (`confirm` of `apply-intent` or `config`: that router) and once a
  router it powered on is healthy again (`POST /faults`: device restore, clear-all). **Nothing else pulls.** So the
  copy is what the lab last put on the router on purpose (golden after a reset, an approved change once applied),
  and **out of sync** means something else changed the router since: a scenario (`vcid-typo`, `fake-redundancy`,
  ...), someone on its CLI or RESTCONF. A router is in sync again as soon as its config equals the copy (no pull
  needed). A closed router is neither. Limits: a controller restart (and `make down && make up`) takes every router's
  config as it is at that moment, so earlier drift no longer shows as out of sync (NetBox's drift check still sees
  it; a reset makes both golden); a router powered on outside lab-api (`labctl device start`, `labctl fault
  clear-all`: Docker directly) stays closed in the controller until the next reset or `POST /controller/resync`.
- **Users and NACM.** `CLICON_NACM_MODE=internal`, `CLICON_NACM_CREDENTIALS=exact` (the NACM user is the unix user of
  the session: nobody poses as another user), `read-default permit`, `write-default deny`, `exec-default deny` (an RPC
  runs only when a rule grants it), `CLICON_AUTOLOCK=true` (no shared candidate). Every user's login shell, and sshd's
  `ForceCommand`, is the controller CLI.

  | User | Who | May |
  |---|---|---|
  | `noc-engineer`, `network-engineer`, `noc-manager` | the NOC roles, through lab-api | read: `get`, `get-config` and the CLI's own RPCs (`clixon-cache`, `nacm-autocli-filter-get`, `create-subscription`, `config-path-info`, `translate-format`). Denied by name: `edit-config`, `copy-config`, `delete-config`, `commit`, `discard-changes`, `validate`, `lock`, `unlock`, `kill-session`, `config-pull`, `controller-commit`, `connection-change`, `device-rpc`, `device-template-apply`; `/nacm` hidden |
  | `lab-api` | lab-api | read, plus `config-pull`, `connection-change`, `datastore-diff`, `device-rpc-result`, `transaction-error`; denied `controller-commit` (no push) |
  | `admin` | people (`make controller-shell`) | everything |
  | `root` | the container (its connect agent, `docker exec`) | the NACM recovery user |

  NACM matches an RPC by its name, never by its arguments. `show devices <router> check` and `diff` send `config-pull`
  with `transient=true` (the RPC a real `pull` sends), then `datastore-diff`, so the roles do not get `config-pull`
  and lab-api runs those two commands as `lab-api`; the audit's actor is still the role.
- **lab-api** (additive; lab-api holds the controller credentials):
  - `POST /controller/cli` (lab token, `X-Lab-Actor`; body `{role, command}`): one allow-listed command (wall 1,
    `lab/src/netsim_lab/controller.py`; table below) in a controller CLI session over SSH as the role's own controller
    user (wall 2: the controller's NACM). The command goes on stdin, never through a shell; timeout 15 s, output capped
    at 16 KiB, its echo and ANSI codes removed. Answer `{role, user, command, allowed, output, truncated,
    duration_ms}`. 403 (same shape, `output` lists the allowed commands) for anything not on the list, a second
    command (`;` or a newline), pipes, redirects and other shell or glob characters
    (``| < > & $ ` \ ' " ( ) { } * ? ! # ~``), control characters, non-ASCII, more than 200 characters or an unknown
    router: nothing reaches the controller. 503 (same shape) when the controller is unavailable. Every call is
    audited as `controller.cli`, refusals included.
  - `GET /controller/status` (`?max_age=` seconds, default 5): `{available, connected, in_sync, out_of_sync, closed,
    routers: {<router>: {state, time, logmsg, in_sync, diff}}, checked_at, duration_ms}` from `show connections` and
    `show devices * diff` as `lab-api`; `{available: false, error}` when the controller is down. `labctl status`
    shows it as its controller line.
  - `POST /controller/resync` (lab token; `{hosts}`, default every router): the resync above; `labctl reset` calls it.
    Audited as `controller.resync` (actor `lab-api` when lab-api resyncs on its own, `request.why` says why); an
    applied change's result carries `controller` (the resync, or its error: the change stands either way).

  | Command (`<router>`: one of the ten PEs) | Runs as | Answers |
  |---|---|---|
  | `show connections` | the role | every router: `OPEN`, or `CLOSED` with the reason |
  | `show connections <router>` [`detail`] | the role | one router; `detail` adds the connection and last sync times |
  | `show devices <router> check` | `lab-api` | nothing when in sync; `Error: device out-of-sync` when not |
  | `show devices <router> diff` | `lab-api` | `<router>:` then the differences: `-` lines what the router runs now, `+` lines the controller's copy (leaf values quoted) |
  | `show configuration devices device <router> config` [`<path>`] | the role | the copy, whole or a subtree (`<path>`: up to 12 words such as `l2vpn xconnect SVC-0001`) |

- **grok-bot**: the tool `controller_cli(command)` for all three roles (a read: `readOnlyHint` true; station
  `terminal`) returns lab-api's answer plus a one-line `summary` (`in sync`, `out of sync`, `10 OPEN`, ...); a refusal
  is an error listing the allowed commands, an unavailable controller an error that says to carry on without it.
  Each call is a `terminal` event on `/api/stream` (`{seq, ts, bot, role, user, command, allowed, available, ok,
  truncated, duration_ms, incident_id, via, head, lines}`; `head` = the output's first 14 lines, each cut at 160
  characters; `via` = `connector` or `terminal`, below) and an entry of the transcript (`GET /api/terminal?after=&limit=`,
  the full output; the newest 500 are kept); `GET /api/state` carries the last 20 as `terminal`. The NOC room shows
  them on its CONTROLLER terminal.
- **The roles' terminals** (11-bot-terminal): `noc-controller` (`bot/grokbot-app/terminal/`, one Python 3 file, standard
  library only) runs in the terminal of a Bot's own computer: `noc-controller [--role <role>] <command>` sends one MCP
  `tools/call` of `controller_cli` with the role's terminal token (§9.6) to the URL of the Bot's connector and prints
  `<user>@controller> <command>`, the answer and `read only · as <user> · <seconds>`; exit codes 0 answered, 1 refused,
  2 controller unavailable, 3 cannot reach `/mcp`, bad token, bad config or bad usage. Config
  `~/.config/noc-controller/<role>.conf` (`url=`, `token=`; mode 600), installed by the block that
  `botctl terminal-setup --role <role> --url <the /mcp URL> [--show-token]` prints and the user pastes into the Bot's
  chat (quoted heredocs; the token only with `--show-token`). The token is never printed and redirects are not
  followed. Same door, same walls:
  the allow-list, the NACM, the audit actor `grok-bot/<ROLE>`, the robot's walk and the CRT are those of the tool; only
  the transcript and the event say `via: terminal` (the panel: "from its computer"). No new endpoint, port, tunnel route
  or credential on the Bot's computer besides its terminal token.
- **Optional at run time:** with `controller` stopped, `POST /controller/cli` and `POST /controller/resync` answer
  503, `GET /controller/status` says `available: false`, the tool answers that the controller is unavailable, lab-api's
  own resyncs are skipped (audited with `ok: false`) and everything else works as before. Started again, it connects
  every router by itself (measured 4.7 s to ten routers connected and in sync).
- **Verified on the running controller** (docs/reports/10-clixon-controller.md): `show devices` and `show devices
  <router>` alone are incomplete commands in 1.9.0 (their content is `show connections`); commits over NETCONF are
  attributed to the SSH user, so simd's `config-change` event (`changed_by`) and the `ciscoConfigManEvent` trap
  (`ccmHistoryEventTerminalUser`) say `controller`. Nothing commits on the PEs over NETCONF today: nobody pushes.

---

## 10. NetBox data model (intent)

- Region `Lab` with child regions `North` (sites `DC-A`, `DC-B`, `BR-01`…`BR-04`) and `South` (`DC-C`, `DC-D`,
  `BR-05`, `BR-06`); each site is in its child region (`contract/topology.yaml` `regions`).
- Manufacturer `NetSim`; device type `NetSim PE`; platform `netsim-clixon`; roles `hub`, `spoke`.
- Devices named exactly as hostnames. Interfaces:
  - `Mgmt0` (virtual, mgmt_only, IP `/24`, device `primary_ip4`)
  - `Loopback0` (virtual, IP `/32`, IP role `loopback`)
  - `GigabitEthernet0/0/0` (MTU 9000), access interface (MTU 1500), subinterfaces (virtual, `parent` set, VLAN assigned; 03-netbox documents exactly how VLAN is stored)
- Tenants = customers (tenant group `L2VPN Customers`), with tenant custom field `sla_tier` (selection
  gold|silver|bronze). Default lab tiers: Customer-A gold, Customer-B gold, Customer-C silver, Customer-D silver,
  Customer-E gold, Customer-F bronze.
- **One NetBox L2VPN object per pseudowire** (two per protected service, one per unprotected service): name `SVC-0001-primary`, type `vpws`, `identifier` = vc-id, tenant = customer,
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
- Tokens: `NETBOX_TOKEN_RW` (seeding/remediation), `NETBOX_TOKEN_RO` (read-only for bot), `ZABBIX_API_TOKEN`, `LAB_API_TOKEN` (write endpoints),
  `BOT_OPERATOR_TOKEN` (grok-bot approvals and control, generated).
- NOC roles (09): `BOT_ZABBIX_HOOK_TOKEN` (generated; Zabbix → `/hooks/zabbix`, copied into the secret macro
  `{$NETSIM.BOT.HOOK.TOKEN}`), `BOT_NOCENG_TOKEN`, `BOT_NETENG_TOKEN`, `BOT_NOCMGR_TOKEN` (generated; the roles on
  `/mcp`, into the Grok Bot app's connectors), `BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`,
  `BOT_NOCMGR_TERMINAL_TOKEN` (generated, 11-bot-terminal; `controller_cli` only, into `noc-controller`'s config on
  each Bot's own computer, through the setup block pasted into its chat; empty: that terminal is off). **The role and
  terminal tokens are the only secrets that leave this computer**; rotate them after a demo (new values in `.env`,
  read live). Every `/mcp` token must differ from every other token (`BOT_OPERATOR_TOKEN`, `BOT_ZABBIX_HOOK_TOKEN`,
  `LAB_API_TOKEN` included).
  `ZABBIX_NOCENG_TOKEN`, `ZABBIX_NETENG_TOKEN`, `ZABBIX_NOCMGR_TOKEN` (written by zabbix-bootstrap; lab-api
  acknowledges with them), `BOT_ROUTINE_<ROLE>_URL` / `_KEY` (supplied by the user from the app's routines). The xAI
  key of 07 is gone.
- Clixon controller (10, §9.7): no `.env` variable and no password. SSH keys only, each made where it is used and
  kept on that container's volume: the controller's key for the PEs and its sshd host key (`netsim_controller-data`),
  lab-api's key (`netsim_lab-api-data`), each PE's host key (its own volume). Only public halves travel, through the
  shared volume `netsim_controller-keys` (`controller.pub`, `controller-host.pub`, `lab-api.pub`; the PEs mount it
  read-only). The controller's users have no password; the roles reach theirs only through lab-api, so the role
  (and, since 11, terminal) tokens stay the only secrets that leave this computer. Keys survive
  `make down && make up`; `make clean` deletes
  them and the next start makes new ones.
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

all succeed (`make e2e` runs the bot's end-to-end tests, with the routine sink standing in for the three Grok Bot
routines and a scripted role player driving `/mcp` with the three role tokens, and `noc-controller` installed from its
setup block and run with a terminal token, then the controller's tests on the running lab (`controller-test`), then
lab-e2e: no cloud, no xAI key);
`make up` brings the controller up with ten routers connected and in sync (`labctl status`), and with `controller`
stopped the rest of the lab works as before;
`make down && make up` preserves device running config (drift survives reboot);
`make reset` returns every device to golden config, clears all faults and resyncs the controller.

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
| 2026-10-20 | 06-topology-v2 | §4/§5/§10: second region. Devices `hub-c` (DC-C, 172.30.10.13, 10.255.0.3), `hub-d` (DC-D, .14, 10.255.0.4), `spoke-5` (BR-05, .25, 10.255.1.5), `spoke-6` (BR-06, .26, 10.255.1.6); NetBox child regions `North` (hub-a/hub-b, spoke-1..4) and `South` (hub-c/hub-d, spoke-5/6) under `Lab`; customers `Customer-E` (gold, services in both regions) and `Customer-F` (bronze); services SVC-0007 … SVC-0011 (table in §5; VLAN scheme `100*c + j` written down). 10 PEs, 11 services, 20 PWs. | With one hub pair every hub fault hit every service the same way, so the lab could not tell a bot that reasons about blast radius from one that pattern-matches "hub-a = primary, all six services". Now the impact depends on the region and on the customer's tier. |
| 2026-10-20 | 06-topology-v2 | `contract/topology.yaml` model (additive, `schema_version` stays 1: every earlier file is still valid): `pseudowires.backup` is optional (a service without it is **unprotected**: primary PW only; `Service.roles` / `.protected`); optional `regions` (child regions with their sites; every site in exactly one; `Topology.region_of()`); SLA tier `bronze` (99.5 %). Consumers iterate `Service.roles` (never both roles blindly); `Service.pw(role)` raises `KeyError` for an absent backup. | A bronze service has no second hub; regions make the second hub pair a first-class fact (NetBox, lab-api `/topology`). |
| 2026-10-20 | 06-topology-v2 | §9.3/§9.4: `status_rules` is `redundant-pair` (two PW leaves) or new `unprotected` (one leaf: ≥1 child ≥ High → Disaster, ≥1 child ≥ Average → Warning), derived from the leaf count by `status_rules_for()` and enforced by `BsmService`; zabbix-sync maps both onto Zabbix 8.0 additional rules. SLA `L2VPN bronze` (99.5 %) from `sla_tiers`; dashboard `L2VPN Services` shows the three SLA reports side by side. Root description: "VPWS hub-and-spoke: dual-homed or unprotected". | Under `redundant-pair` a one-PW service could never reach Disaster: an unprotected service whose PW is down would have looked merely degraded. |
| 2026-10-20 | 06-topology-v2 | §10 seeding: child regions are their own seeder phase (`child-regions`, after `regions`: a bulk create resolves references before it runs, so a parent must exist first); sites point at their child region; the VLAN group stays scoped to `Lab`; `sla_tier` choice set gains `Bronze`; L2VPNs only for the roles a service has. `netbox/tools/tests/data/netbox-seeded.json` re-recorded from a v2-seeded NetBox. | Additive. |
| 2026-10-20 | 06-topology-v2 | lab-api / labctl (bot tool surface, additive fields): `GET /topology` adds `regions` and each device's `region`, each service's `status_rules`; `GET /services` adds `status_rules`; `GET /impact/{host}` adds per service `sla_tier` and `status_rules`, and `by_tier` (live impact counted per SLA tier); `labctl status` marks unprotected services, `labctl impact` shows the tier. Scenario format: `zabbix.customers` (status of BSM customer nodes), drift `services: [...]` (one finding per listed service), `add_service` takes `primary_hub` / `backup_hub` and an optional `backup_vc`. New scenarios `region-down` and `maintenance-by-tier`; every scenario's expectations re-derived for the 11 services; `new-service-pending` orders SVC-0012 (VC IDs 1012/2012), SVC-0007 being taken. | The bot must weigh impact by region and tier; scenarios now assert exactly that. |
| 2026-10-20 | 06-topology-v2 | `tools/gen_topology.py`: `--spokes 6 --services 11 --customers 6` reproduces the contract topology (new defaults); generated spokes join spoke-1's region; a generated service uses the hub pair its spoke already uses (South spokes: hub-c/hub-d); bronze customers get unprotected services. | Scale topologies keep the regional and tier structure. |
| 2026-10-20 | 06-topology-v2 | §12 bring-up: NetBox's healthcheck `start_period` 300 s → 900 s (`netbox/compose.yaml`) and `make netbox-up` waits up to 1200 s (was 420 s). | A clean `make up` failed: NetBox's first start (all migrations) took 337 s while the 10 PEs and Zabbix booted alongside, past the 300 s grace window, so compose saw `netbox` unhealthy and never started `netbox-worker`; a later clean run needed 494 s. The window only delays an "unhealthy" verdict; a NetBox that is up is reported healthy at once (`start_interval` 2 s). |
| 2026-10-20 | 06-topology-v2 | §9.1 trap flow: the trap triggers (cpwVcDown, cpwVcUp, ciscoConfigManEvent, coldStart) are `length(last(<trap item>))>0` with problem event generation *multiple*, OK event generation *none* and manual close allowed; the check-now webhook closes the event (`event.acknowledge`, action close + message) once the re-poll is scheduled. `{$NETSIM.TRAP.HOLD}` is gone. Exactly one short-lived event per trap. | The former `... and nodata(<trap item>,{$NETSIM.TRAP.HOLD})=0` made the trigger time-based: Zabbix's timer re-evaluated it every 30 s and, in *multiple* mode, every PROBLEM evaluation is a new event, so each trap gave a second "SNMP trap ... (hint)" event and a second re-poll at the next timer tick (observed in every trace; it made the e2e trap -> check-now latency test pair a walk with the wrong trap once: 12.7 s). A bot counting trap events would have counted every trap twice. |
| 2026-10-20 | 07-grok-bot | §1/§3/§4: new component `bot/` (owner 07): container `grok-bot` at 172.30.10.60, host 127.0.0.1:8095 (NOC room page, bot API, SSE), image `netsim/grok-bot:0.1.0`, volume `netsim_grok-bot-data` (SQLite), workspace member `netsim-bot` (talks HTTP only: lab-api and api.x.ai; imports no other member). Root files, additive: `compose.yaml` includes `bot/compose.yaml`; `Makefile` lists it in `FRAGMENTS`, includes `bot/bot.mk` and has a `bot-%` placeholder; `pyproject.toml` adds the member/dependency and `bot` to `testpaths`. | The Grok bot the contract was written for; its own component so it never edits lab-api's code. |
| 2026-10-20 | 07-grok-bot | Cross-session, build only: `devices/Dockerfile` and `lab/Dockerfile` (+ their `.dockerignore`) copy `bot/pyproject.toml` into the build stage. | `uv sync --frozen` needs every workspace member's `pyproject.toml` to resolve the lock; without it the device and lab-api images stop building once `bot` is a member (same reason 05 added `lab/pyproject.toml` to the device image). |
| 2026-10-20 | 07-grok-bot | lab-api usage convention (no API change): the bot reads only lab-api (never scenarios, faults injection or reset) and writes only dry runs (`X-Lab-Actor: grok-bot/<BOT>`), approved changes (`grok-bot/<BOT> approved-by:<who>`, confirming the exact dry-run `plan_id`) and field repairs (`POST /faults` with `action: restore`, same actor form). The audit log therefore names the bot and the human who approved each change. | The propose -> approve -> apply -> verify loop of 05, made visible per person. |
| 2026-10-20 | 07-grok-bot | §11 `.env.example` section `grok-bot (07-grok-bot)`: `XAI_API_KEY` (empty; user-supplied, billed by xAI separately from SuperGrok/X/Grok Bot plans), `GROK_MODEL=grok-4.7` (docs.x.ai/developers/models, 2026-10-20), `GROK_REASONING_EFFORT` (empty), `BOT_BRAIN=auto`, `BOT_AUTONOMY=propose`, `BOT_DAILY_BUDGET_USD=5`, `BOT_OPERATOR_TOKEN=__generate__`. grok-bot reads `.env` live (repository mounted read-only, like lab-api), so a key added later is used without a restart. | One place for the bot's settings; `make env` appends them to an existing `.env`. |
| 2026-10-20 | 07-grok-bot | §12: `make e2e` = `bot-e2e` (prerequisite from `bot/bot.mk`: the bot's loop on the live lab with its deterministic rules brain) then `lab-e2e`; `lab/e2e/conftest.py` (owner 05) puts grok-bot into maintenance mode for the lab run and restores it (no-op without a bot). `make bot-eval` (opt-in, costs money) grades Grok on six scenarios. | The bot reacts to every fault lab-e2e injects: its incidents and dry runs would add load and audit entries during timing and audit assertions. The Definition of Done must not need an xAI key. |
| 2026-10-20 | 08-grokbot-app | §1/§3/§4, additive, in `bot/` (owner 07): grok-bot serves `POST /mcp` on its existing port 8095 (MCP Streamable HTTP, stateless JSON; `Authorization: Bearer BOT_AGENT_TOKEN`; Host names in `BOT_MCP_HOSTS` reach only `/mcp`) and may send outbound HTTPS webhooks (the pager, `BOT_PAGER_URL`). Outside agents use lab-api only through grok-bot: dry runs as `X-Lab-Actor: grok-bot/<BOT_AGENT_NAME>`, applied changes as `grok-bot/<BOT_AGENT_NAME> approved-by:<who>`; they never approve. New endpoints `GET /api/pager`, `POST /api/pager/test`; `/api/control` takes `pager_muted`. | The user's Grok Bot app (a cloud agent with MCP connectors, local commands and webhook-triggered routines) joins the NOC without a second tool surface or a second safety model. |
| 2026-10-20 | 08-grokbot-app | §11 `.env.example`, grok-bot section: `BOT_AGENT_TOKEN=__generate__`, `BOT_AGENT_NAME=GROK`, `BOT_MCP_HOSTS=`, `BOT_PAGER_URL=`, `BOT_PAGER_TOKEN=`, `BOT_PAGER_EVENTS=opened`, `BOT_PAGER_MIN_SEVERITY=Average`, `BOT_PAGER_MAX_PER_HOUR=6`. | `make env` appends them to an existing `.env`; the pager stays off until the user pastes a routine's webhook URL and key. |
| 2026-10-20 | 08-grokbot-app | §12: `bot/e2e` (part of `make e2e`) and `lab/e2e/conftest.py` (owner 05) mute the pager for their run and restore it; `bot/e2e/test_20_outside_agent.py` drives `/mcp` on the live lab. | Test incidents must not start paid agent runs. |
| 2026-10-21 | 09-noc-roles | §1/§3/§4, in `bot/` (owner 07): grok-bot 0.2.0 is the office of three NOC roles (NOC Engineer, Network Engineer, NOC Manager), each a Grok Bot app routine; the in-house crew (POLL, PING, TRACE, DIG, PATCH), the visitor GROK, the Grok and rules brains and the pager are gone, and `BOT_AUTONOMY=auto` with them (an old `auto` in `.env` is read as `propose`, with a warning). New inbound `POST /hooks/zabbix` (from Zabbix on labnet) and outbound HTTPS pages to the routines. New container `routine-sink` at 172.30.10.61, host 127.0.0.1:8096 (compose profile `sink`, same image; `make bot-sink-up`, started by `make bot-e2e`), a stand-in for the routines. | The user's decisions of 2026-10-21 (prompts/09-noc-roles.md): Zabbix, not a bot, decides who is paged; the roles think in the Grok Bot app's cloud. |
| 2026-10-21 | 09-noc-roles | §8, cross-component (`tools/gen_oids.py`, regenerated `contract/oids.yaml`, `devices/simd`): IF-MIB `linkDown` / `linkUp` (ifIndex, ifAdminStatus, ifOperStatus) on every ifTable interface's ifOperStatus leaving or entering `up(1)`, one trap per changed interface, none for the boot baseline; sent with the PW traps of the same recompute (link traps first). | An AC or uplink fault must re-poll the router at once (§9.5: interface problem ≤ 10 s). |
| 2026-10-21 | 09-noc-roles | §9.1, cross-component (`zabbix/` template, `fixtures/send_trap.py`): trap items `trap=link` for linkDown and linkUp (numeric OID match, hint trigger tagged like the other trap triggers); the action `NetSim: SNMP trap -> check now` re-polls the router on them unchanged (verified: interface problem 4.7 s after an AC fault). `send_trap.py` sends linkDown/linkUp. | Task 1. |
| 2026-10-21 | 09-noc-roles | §9.3/§9.4, cross-component (`zabbix/tools` `bsm_sync`): every BSM node carries the service tag `bsm_level` (`root`, `customer`, `service`, `pw`). | The service action selects customer nodes only (`sla_tier` is on service nodes too). |
| 2026-10-21 | 09-noc-roles | §9.6 new, cross-component (`zabbix/tools` bootstrap): user group and role `NetSim NOC`, users `noc-engineer`, `network-engineer`, `noc-manager` with API tokens (`ZABBIX_NOCENG_TOKEN`, ...; kept while valid, written into `.env`), media type `NetSim NOC (grok-bot)`, global macros `{$NETSIM.BOT.HOOK.URL}`, `{$NETSIM.BOT.HOOK.TOKEN}` (secret), `{$NETSIM.ESCALATE.AFTER}` (`10m`, never reset), service action `NetSim: customer alert`. **Deviation:** the action is created by zabbix-bootstrap, not zabbix-provision: its operations name the role users, and the provisioning API user (Admin type) cannot see users outside its own groups (`user.get` returns none). Idempotent; survives `make clean && make up && make seed`. | The plan put the action in provisioning; Zabbix's user visibility rules decided otherwise. |
| 2026-10-21 | 09-noc-roles | lab-api, additive, cross-component (`lab/`): `POST /zabbix/api` (allow-listed reads, anything else 403), `GET /netbox/{path}` (GET-only passthrough), `GET /devices/{host}/restconf[/{path}]` (`content=config\|nonconfig\|all`), responses size-capped; `POST /zabbix/acknowledge` (lab token, `X-Lab-Actor`; `{eventids, message, role}`: the open trigger problems behind trigger or service events, as the role's Zabbix user; audited as `zabbix.acknowledge`). `lab/openapi.json` regenerated; `lab/e2e/conftest.py` pauses grok-bot with `pages_muted` (was `pager_muted`). | Task 3: the roles read Zabbix, NetBox and the routers directly while lab-api keeps every credential. |
| 2026-10-21 | 09-noc-roles | lab-api fix, cross-component (`lab/src/netsim_lab/zabbix.py`, no API change): the Zabbix client's map of managed hosts was cached for lab-api's whole life; an empty map (asked between zabbix-bootstrap's new token and `make seed` creating the hosts, as grok-bot's feed does on a clean bring-up) is no longer kept, and a map expires after 60 s. | On a clean lab every `POST /reset` waited 240 s for polls of hosts it could not see and returned `green: false`. |
| 2026-10-21 | 09-noc-roles | §9.5: AC fault → interface problem ≤ 10 s (measured 4.5-4.8 s); fault → NOC Engineer page sent ≤ 30 s (measured 14-22 s: Zabbix's service event ~6-10 s, its alert, then grok-bot's 10 s batch). | Measured by `make bot-e2e`. |
| 2026-10-21 | 09-noc-roles | §11 `.env.example`, grok-bot section rewritten: new `BOT_ZABBIX_HOOK_TOKEN`, `BOT_NOCENG_TOKEN`, `BOT_NETENG_TOKEN`, `BOT_NOCMGR_TOKEN` (generated), `BOT_BATCH_S=10`, `BOT_MAX_BATCH_S=30`, `BOT_ROUTINE_<ROLE>_URL` / `_KEY` (empty), `BOT_PAGE_MAX_PER_HOUR=12`, `BOT_FIELD_DELAY_S=10`, `BOT_ASK_TIMEOUT_S=600`; removed `XAI_API_KEY`, `GROK_MODEL`, `GROK_REASONING_EFFORT`, `BOT_BRAIN`, `BOT_DAILY_BUDGET_USD`, `BOT_MAX_STEPS`, `BOT_SETTLE_S`, `BOT_MAX_SETTLE_S`, `BOT_AGENT_TOKEN`, `BOT_AGENT_NAME`, `BOT_PAGER_*`, `BOT_AUTONOMY=auto`. Zabbix section: `ZABBIX_NOCENG_TOKEN`, `ZABBIX_NETENG_TOKEN`, `ZABBIX_NOCMGR_TOKEN` (empty, written by zabbix-bootstrap). `make env` never removes values: old ones may stay in `.env`, unread. | The role tokens are the only secrets that leave this computer. |
| 2026-10-21 | 09-noc-roles | §12: `make e2e` = `bot-e2e` (starts `routine-sink`; the sink stands in for the three routines and a scripted role player drives `/mcp` with the three tokens: alert storm, drift, AC fault, gold down with Zabbix's escalation step, Ask the NOC, maintenance and mute, refusals) then `lab-e2e`. `botctl grok-check` and `make bot-eval` are gone. | No cloud, no xAI key in the Definition of Done. |
| 2026-10-23 | noc-manager-review | §9.6 grok-bot (`bot/`; one tool renamed): change tickets start in `review`; the NOC Manager's `review_ticket` (`propose` → `pending`, `return` → `returned` and a `ticket.returned` page to the engineer who filed it) replaces `recommend_ticket` (the old name is refused with a pointer to the new one); only a `pending` ticket can be approved (one in review: HTTP 409); a ticket the NOC Manager is not paged for, or has not reviewed within `BOT_REVIEW_TIMEOUT_S` (new in §11, default 300), goes on to the operator marked not reviewed; a stale plan at apply (409) sends the new plan back to review; `POST /api/incidents/{id}/approve-all` takes an optional `tickets` map (ticket id → plan id) and approves nothing (409) when the waiting tickets differ; an unknown incident is 404. | The user's decision: the NOC Manager, not the engineer, puts a change in front of the operator, after its review. |
| 2026-10-24 | 10-clixon-controller | §1/§2/§3/§4: new component `controller/` (owner 10): container `controller` at 172.30.10.70 (Clixon controller 1.9.0 on the PEs' Clixon/CLIgen 7.9.0, image `netsim/clixon-controller:1.9.0`, volumes `netsim_controller-data` and `netsim_controller-keys`), sshd :22 on labnet for lab-api only, nothing published. Root files, additive: `compose.yaml` includes `controller/compose.yaml`; `Makefile` lists it in `FRAGMENTS`, has a `controller-%` placeholder and includes `controller/controller.mk` (`controller-build`, `-up`, `-down`, `-logs`, `-shell`, `-config`, `-check`, `-test`); `pyproject.toml` adds `controller` to `testpaths`; `contract/topology.yaml` lists it under `infrastructure:` (`tests/test_topology.py`, `tests/test_scaffolding.py` updated). | The user's decisions of 2026-10-24 (prompts/10-clixon-controller.md): one controller in front of the routers, a second drift signal next to NetBox, read only for the roles. |
| 2026-10-24 | 10-clixon-controller | §4, cross-component (`devices/`: `Dockerfile`, `Dockerfile.dockerignore`, `supervisord.conf`, `entrypoint.sh`, new `sshd_config` and `netconf-session`, simd's healthcheck and compose rendering (`devtool.py`) with their tests, regenerated `devices/compose.yaml`): the PEs also speak NETCONF over SSH on :830 (sshd under supervisord; subsystem `netconf` = `clixon_netconf -f /etc/clixon.xml`; one user `controller`: key only, no password, group `clicon`, login shell and `ForceCommand` a NETCONF session, from 172.30.10.70 only; a host key per PE on its volume). Each PE mounts `netsim_controller-keys` read-only for the controller's public key; the healthcheck also checks that sshd listens on :830. RESTCONF is unchanged and NACM on the PEs stays off. Commits over NETCONF are attributed to `controller` (simd's `config-change` `changed_by`, the `ciscoConfigManEvent` TerminalUser; verified). | The controller's southbound is NETCONF over SSH only; the PEs served RESTCONF only. |
| 2026-10-24 | 10-clixon-controller | §9.7 new: the controller's devices (from the topology), its copy of each router and what in sync / out of sync mean (including the limits: a controller restart or a reconnect takes the router's config as it is then), its users and NACM (`CLICON_NACM_CREDENTIALS=exact`, `exec-default deny`), `CLICON_AUTOLOCK=true`, the allow-list, optional at run time. **Deviation:** the prompt's `show devices` and `show devices <host>` are incomplete commands in controller 1.9.0; their content (each router's connection) is `show connections [<router> [detail]]`, which is on the allow-list instead. `check` and `diff` run as `lab-api`: they send `config-pull` (transient), the RPC a real pull sends. | Verified on the running controller; NACM matches RPC names, never their arguments. |
| 2026-10-24 | 10-clixon-controller | lab-api, additive, cross-component (`lab/`): `POST /controller/cli`, `GET /controller/status`, `POST /controller/resync` (§9.7); the controller resync after a reset (new reset step 6), after an applied and verified change (`ChangeResult.controller`) and after lab-api powers a router on; audit actions `controller.cli` and `controller.resync`; `labctl status` has a controller line (`--json`: `controller`); the lab-api image gains `openssh-client`, its compose file the volume `netsim_controller-keys` and `CONTROLLER_KEYS_DIR`; `lab/openapi.json` regenerated. | lab-api is the controller's only client and keeps its credentials, like every other credential. |
| 2026-10-24 | 10-clixon-controller | grok-bot, additive, cross-component (`bot/`): the tool `controller_cli` for the three roles (a read, station `terminal`), the `terminal` event on `/api/stream`, `GET /api/terminal`, `terminal` in `GET /api/state`, the CONTROLLER terminal in the NOC room (`web/js/terminal.js`) and the side panel's Terminal tab; the skills in `bot/grokbot-app/` teach the tool (the Network Engineer quotes the diff in the root cause, the NOC Engineer checks `show connections` in triage, every role carries on without an unavailable controller). | The roles type into a real network CLI and the room shows each command as it happens; `/mcp` stays their only way in. |
| 2026-10-24 | 10-clixon-controller | §11: no new `.env` variable; SSH keys on the volumes, only public halves in `netsim_controller-keys`. §12: `make e2e` = `bot-e2e`, `controller-test` (prerequisite from `controller/controller.mk`: on the running lab, ten routers in sync after a reset, spoke-1 out of sync after `vcid-typo`, NACM refusing a role user's edit, commit, pull, push and connection change without lab-api), then `lab-e2e`; bot e2e covers `controller_cli` (reads, five refusals, the drift case, the controller stopped and started). | No new secret leaves this computer; the prompt's acceptance criteria, as tests. |
| 2026-10-24 | 11-bot-terminal | §9.6/§11, in `bot/` (owner 07): two kinds of `/mcp` token. New role **terminal tokens** `BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`, `BOT_NOCMGR_TERMINAL_TOKEN` (`.env.example`, generated; read live like the role tokens; empty = that role's terminal is off): `/mcp` takes one as its role for `controller_cli` only (`initialize`: the terminal's instructions, server `netsim-noc-<bot>-terminal`; `tools/list`: `controller_cli` alone; `tools/call` of any other tool: refused naming the token kind, before anything reaches lab-api or a robot moves). Nothing outside `/mcp` accepts it. grok-bot refuses settings where an `/mcp` token (a role's or a terminal's) equals any other token (`BOT_OPERATOR_TOKEN`, `BOT_ZABBIX_HOOK_TOKEN`, `LAB_API_TOKEN` included; before, only the three role tokens were compared). `GET /api/state` `settings.roles.<role>.terminal` says whether a terminal is on (`botctl status` shows it). | The user's decisions of 2026-10-24 (prompts/11-bot-terminal.md): the Bot can read every file on its own computer, so the file there holds a token that can read the controller and nothing else; a token equal to another one would open what that one opens. |
| 2026-10-24 | 11-bot-terminal | §9.7, in `bot/`: `noc-controller` (`bot/grokbot-app/terminal/`, Python 3.7+ standard library only), the roles' terminal on their Bots' own computers: one MCP `tools/call` of `controller_cli` with the role's terminal token, printed like a Clixon CLI session (`<user>@controller> <command>`, the answer, `read only · as <user> · <seconds>`), exit codes 0 answered, 1 refused, 2 controller unavailable, 3 cannot reach `/mcp`, bad token, bad config or bad usage; config `~/.config/noc-controller/<role>.conf` (mode 600, `--role` picks among several); never prints the token, never follows a redirect. `botctl terminal-setup --role <role> --url <the /mcp URL> [--show-token]` prints the block a user pastes into the Bot's chat (quoted heredocs; a placeholder without `--show-token`; a new block replaces the config). The `terminal` event and the transcript gain `via` (`connector` or `terminal`; the panel shows "from its computer"). `make lint` also runs ruff on the command (`bot-lint-terminal`, a prerequisite added from `bot/bot.mk`: ruff skips files without `.py` unless named). | A Bot types a controller read on its own Linux computer and the room's CRT types the same command; same door and same walls (allow-list, NACM, audit actor `grok-bot/<ROLE>`): no new endpoint, port, tunnel route or lab credential on the Bot's computer. |
| 2026-10-24 | 11-bot-terminal | §12: bot e2e (`bot/e2e/test_80_terminal.py`) installs `noc-controller` from the `terminal-setup` block in a throwaway home and runs it with the Network Engineer's terminal token (reads, `pull spoke-1` and `commit push` refused, the event stream marking them `via: terminal`, the terminal token refused `get_drift` and the operator API). Edits outside `bot/`: `.env.example` (the three tokens), this file, `prompts/README.md` (step 9), `prompts/11-bot-terminal.md`, `docs/reports/11-bot-terminal.md`; lab-api, the controller, its NACM and the tunnel's host rule are unchanged. | The prompt's acceptance criteria, as tests. |
