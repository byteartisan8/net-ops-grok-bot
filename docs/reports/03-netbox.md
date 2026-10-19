# Session 03 — NetBox report

**Date:** 2026-10-19. **Owner paths:** `netbox/` (compose fragment, `netbox.mk`, the `netsim-netbox-tools` package in
`netbox/tools/`, integration tests), this report. Shared files touched (all logged in the contract): `.env.example`
(two NetBox variables), `tools/gen_env.py` (one line: pepper length), `docs/CONTRACT.md` (§2 pins, §4 row, §10 text,
six change-log entries), `uv.lock` (the new package's httpx dependency).

**Result:** every acceptance criterion passes. Fresh NetBox 4.7.3 healthy with both tokens in **232 s** (first start,
migrations); `make netbox-seed` twice → the second run reports **0 changes** (no write request at all); the intent
rendered from NetBox alone equals `contract/golden/*.json` **byte for byte** for all 6 devices and passes yanglint; the
BSM spec from NetBox equals `bsm_spec_from_topology()` after `normalized()` and validates against the schema;
**76 unit tests** + **15 integration tests** on a real NetBox pass.

---

## What was built

```
 contract/topology.yaml ──netbox-seed (RW)──▶  NetBox 4.7.3 (source of truth)  ──netbox-intent (RO)──▶ DeviceConfig per device
                                               │  netbox 172.30.10.30            │                      BSM spec (§9.3) → zabbix-sync (04)
 netbox-bootstrap ── tokens ──▶ .env           │  netbox-worker 172.30.10.31     │                      bot queries, journal (RW)
                                               ▼  PostgreSQL 16, Valkey ×2 (internal network)
                         event rule + webhook (HMAC-SHA512) ──▶ lab-api 172.30.10.50:8090/hooks/netbox (05) → zabbix-sync
```

| Piece | Where | Notes |
|---|---|---|
| Deployment | `netbox/compose.yaml` | Official image `netboxcommunity/netbox:v4.7.3-5.1.1` (netbox-docker 5.1.1 = reference for env/healthchecks, its `configuration.py` is baked into the image and reads only env), `netbox-worker` (RQ), `postgres:16.15-alpine3.24`, two Valkey (`--appendonly yes` queue, non-persistent cache) on the internal network `netbox-internal`. Healthchecks on all five; first start migrates and creates the superuser from `.env` with no manual step; Internet features off (`ISOLATED_DEPLOYMENT`, census, Copilot). |
| Token bootstrap | `netsim_netbox.bootstrap` (`netbox-bootstrap`) | Waits for NetBox, obtains/validates `NETBOX_TOKEN_RW` and `NETBOX_TOKEN_RO` over REST, writes them into `.env`, deletes stale ones it created earlier. |
| Seeder | `netsim_netbox.seed` (`netbox-seed`) | Declarative desired state from the topology (`desired.py`, pure) + reconcile engine (`engine.py`): natural keys, managed fields only, bulk writes, `--dry-run`, `--check`, `--prune`, `--no-webhooks`, journal entry per changed device. |
| Intent | `netsim_netbox.intent` (`netbox-intent`) | `snapshot` (NetBox reads) → `model.build_intent` (pure interpretation, problems per device) → `render` (DeviceConfig), `bsm` (BsmSpec), `queries` (bot), `journal` (RW). Never reads `topology.yaml` (a unit test makes the topology loader raise). |
| REST client | `netsim_netbox.client` | httpx, v2 `Bearer` / v1 `Token` auth, pagination (rebases NetBox's absolute `next` links), bulk POST/PATCH/DELETE in chunks of 100, errors that never print a token. |
| Make | `netbox/netbox.mk` | `netbox-up/down/bootstrap/seed/plan/prune/render/bsm-spec/ps/logs/shell/test/test-integration`; `make seed` → `netbox-seed` (which bootstraps first). |
| Tests | `netbox/tools/tests/`, `netbox/tests/integration/` | Unit tests replay a NetBox 4.7.3 recorded right after seeding (`data/netbox-seeded.json`, secrets scrubbed) through a fake client; integration tests run on a throwaway stack rendered from `netbox/compose.yaml`. |

### Decisions

**Pseudowire ends: hub termination + `spoke_ac` custom field (decided with the user).** The contract asked for one
L2VPN per PW with two terminations (spoke + hub subinterface). NetBox 4.7.3 allows **one L2VPN termination per
interface** (`L2VPNTermination.clean()`: "L2VPN Termination already assigned"), and the primary and backup PWs of a
service share the spoke's subinterface, so the second termination is refused (the first seed attempt failed exactly
there). Each hub subinterface carries exactly one PW, so the hub end stays a real termination and the spoke end is
the object custom field `spoke_ac` (→ `dcim.interface`) on the L2VPN. This keeps one L2VPN per PW, `identifier` =
vc-id and one `netbox_l2vpn_id` per BSM leaf (§9.3). Rejected: one L2VPN per service (`evpl`, three terminations:
breaks identifier = vc-id and the per-leaf ID) and both ends as custom fields (no terminations at all in the UI).

**VLAN storage on subinterfaces: an IPAM VLAN object as the subinterface's single tagged VLAN.** One VLAN per
service (`SVC-0001`, VID 101, tenant = customer) in VLAN group `l2vpn-acs` scoped to region `Lab` (so it may be
assigned on every lab device); each subinterface has 802.1Q mode `tagged`, that VLAN as its only tagged VLAN and no
untagged VLAN. A dot1q subinterface classifies frames *tagged* with that VID on its parent, which is NetBox's
`tagged` mode (`access` would claim the frames are untagged). A real VLAN object keeps referential integrity, appears
on the VLAN's page and in `?vlan_id=` filters, and the VLAN group enforces unique VIDs. A custom field holding a
number would have none of that. 4.7.3 has no dedicated "encapsulation VLAN" field (checked in the OpenAPI schema:
only `mode`, `untagged_vlan`, `tagged_vlans`, `qinq_svlan`). The renderer is strict: any other shape is a problem.

**YANG interface type: custom field `if_role`.** Physical interfaces carry `if_role` = `core-uplink` / `access`
(selection); a subinterface is an interface with a `parent`. That is also the rule that decides what is in the
device config: `Mgmt0`, `Loopback0` and any other interface without `if_role` and without a parent are not.

**Tokens: v2, obtained from NetBox over REST, written into `.env`.** See [Token handling](#token-handling).

**Plain REST (httpx), not pynetbox.** The seeder needs exact control of what it compares and sends (it normalizes
NetBox's read format to the write format field by field), every field was verified against the running 4.7.3's
OpenAPI schema, and httpx 0.28.1 is already pinned in the workspace.

**Seeder semantics.** Each object has a natural key (slug, name, (device, interface), host address, ...); only the
fields the seeder sets are compared, custom fields and tags are merged (foreign ones kept). Up to date = zero write
requests. `--prune` deletes only objects tagged `netsim-lab`, in reverse dependency order. A lab L2VPN's
terminations are reconciled as a set (a `vpws` L2VPN holds at most two). Journal entries are written only for devices
that changed, so a no-op run writes nothing: on a fresh NetBox each device gets one listing everything created for it.

**Intent is strict, per device.** Anything that cannot be interpreted (subinterface without exactly one tagged VLAN,
L2VPN without `spoke_ac` or with ≠ 1 hub termination, missing loopback, duplicate role, ...) becomes a problem naming
the devices it affects; rendering those devices raises `IntentError`, other devices still render. A broken PW also
blocks its service's spoke (its xconnect would be incomplete). The BSM spec and service views refuse to build while
any PW or customer has a problem. Rationale: partial intent would make the bot "fix" devices towards a wrong target.
`netbox-intent problems` lists them with NetBox URLs.

**Others.** `netbox-worker` gets a static labnet address (172.30.10.31): it sends the webhooks, and a dynamic address
could take a fixed one of a later container. L2VPN `status` is informational (intent always renders `enabled: true`,
as golden does). The read-only permission excludes `extras.webhook` (the RO token could otherwise read the HMAC secret).

## Data model

Contract §10, as stored (trimmed REST API output of NetBox 4.7.3, RO token):

```json
{
 "l2vpn": {"id": 3, "name": "SVC-0002-primary", "type": {"value": "vpws", "label": "VPWS"}, "identifier": 1002,
           "status": {"value": "active", "label": "Active"}, "tenant": {"id": 1, "name": "Customer-A", "slug": "customer-a"},
           "tags": [{"slug": "l2vpn-pw"}, {"slug": "netsim-lab"}],
           "custom_fields": {"pw_role": {"value": "primary", "label": "Primary"}, "service_id": "SVC-0002",
                             "spoke_ac": {"id": 38, "name": "GigabitEthernet0/0/1.102", "device": {"name": "spoke-2"}}}},
 "termination": {"id": 39, "l2vpn": {"id": 3, "name": "SVC-0002-primary"}, "assigned_object_type": "dcim.interface",
                 "assigned_object": {"id": 26, "name": "GigabitEthernet0/0/2.102", "device": {"name": "hub-a"}}},
 "spoke_subinterface": {"id": 38, "device": {"name": "spoke-2"}, "name": "GigabitEthernet0/0/1.102",
                        "type": {"value": "virtual"}, "enabled": true, "parent": {"id": 16, "name": "GigabitEthernet0/0/1"},
                        "mtu": null, "description": "Customer-A SVC-0002", "mode": {"value": "tagged", "label": "Tagged"},
                        "untagged_vlan": null, "tagged_vlans": [{"id": 2, "name": "SVC-0002", "vid": 102}],
                        "custom_fields": {"if_role": null}},
 "access_interface": {"id": 16, "name": "GigabitEthernet0/0/1", "type": {"value": "1000base-t"}, "mtu": 1500,
                      "description": "CUSTOMER-ACCESS", "custom_fields": {"if_role": {"value": "access", "label": "Customer access"}}}
}
```

| Lab concept | NetBox (all seeded objects tagged `netsim-lab`) |
|---|---|
| region / sites | `Lab`; `DC-A`, `DC-B`, `BR-01`..`BR-04` |
| PE | device (name = hostname), type `NetSim PE` (manufacturer `NetSim`), platform `netsim-clixon`, role `hub` / `spoke` |
| interfaces | `Mgmt0` (virtual, mgmt only, labnet `/24`, `primary_ip4`), `Loopback0` (virtual, `/32`, IP role `loopback`), `GigabitEthernet0/0/0` (1000base-t, MTU 9000, `if_role` core-uplink), access interface (MTU 1500, `if_role` access), subinterfaces (virtual, `parent`, tagged VLAN, description `<customer> <service>`) |
| customers | tenant group `L2VPN Customers`; tenants with `sla_tier` (A/B gold, C/D silver) |
| pseudowires | 12 L2VPNs `SVC-000n-primary/backup` (`vpws`, identifier = vc-id, tenant, `l2vpn-pw`, `service_id`, `pw_role`, `spoke_ac`), 12 hub terminations |
| schema | tags `netsim-lab`, `l2vpn-pw`; custom fields `sla_tier`, `service_id` (regex `^SVC-[0-9]{4}$`), `pw_role`, `spoke_ac`, `if_role` (group "NetSim L2VPN"); choice sets `netsim SLA tiers` / `PW roles` / `interface roles` |
| notifications | webhook `netsim lab-api: intent changed` + event rule `netsim: L2VPN intent changes -> lab-api` |

Rendering rule (contract §10): for each device, every L2VPN ending on it (spoke end or hub end) is a PW in xconnect
`service_id` with `role = pw_role`, `vc-id = identifier`, `peer-ipv4` = the other end's loopback, AC = this end's
subinterface; xconnect `customer` = tenant; every `enabled` spelled out. Output goes through `canonical_json`.

## Token handling

Verified on the running 4.7.3 and in its source (`users/models/tokens.py`, `utilities/security.py`):

- v2 tokens (default since 4.5) are `nbt_<12-char key>.<40-char secret>`, sent as `Authorization: Bearer <token>`.
  NetBox stores an HMAC-SHA256 digest made with pepper `API_TOKEN_PEPPERS[1]` and shows the plaintext once, in the
  creation response. The key is not secret (printed by the tools as `nbt_<key>.***`).
- The pepper must be ≥ 50 characters or NetBox refuses to start. `make env` generated 32-character secrets, so
  `tools/gen_env.py` now generates 64 characters for keys ending in `PEPPER` (`NETBOX_API_TOKEN_PEPPER`).
- 4.7 ignores client-supplied plaintexts in the REST API (#22409), so tokens cannot be pre-generated into `.env`:
  `netbox-bootstrap` asks NetBox for them and writes what it gets.

`netbox-bootstrap` (run by `make netbox-up`, `make netbox-seed`, `make netbox-bootstrap`):

1. waits until `/login/` answers (what the container healthcheck probes);
2. `NETBOX_TOKEN_RW`: kept if it authenticates as the superuser and is write-enabled, else a new one from
   `POST /api/users/tokens/provision/` (superuser name + password from `.env`);
3. `NETBOX_TOKEN_RO`: group `netsim-readonly` with object permission `netsim-readonly` (`view` on every public object
   type except the `users` app and `extras.webhook`, 133 types), user `netsim-bot` (random password, not stored),
   token `write_enabled=false` via `POST /api/users/tokens/`. Kept if it belongs to `netsim-bot`, is read-only and
   authenticates;
4. each new token goes into `.env` immediately; then it checks the RO token can read and gets 403 on a write; finally
   deletes tokens it created earlier that `.env` no longer holds.

A second run keeps both tokens; after a database reset (`make clean`) the stale values are replaced automatically.
RO token observed: `dcim/devices` 200, `core/object-changes` 200, `users/users` 403, `extras/webhooks` 403, any write 403.

## CLI usage

```bash
make netbox-up                                         # or: docker compose up -d netbox ... && uv run netbox-bootstrap
make netbox-seed                                       # SEED_ARGS="--prune" / "--no-webhooks"
uv run netbox-seed --dry-run --prune                   # plan only (= make netbox-plan); --check exits 1 if out of sync
uv run netbox-intent render spoke-2 [--json]
uv run netbox-intent render-all --out build/intent --check-golden --yanglint
uv run netbox-intent bsm-spec --out spec.json [--normalized]
uv run netbox-intent services spoke-2 | expected-pws hub-a | blast-radius hub-a | service SVC-0002   # --json
uv run netbox-intent journal device:spoke-2 "Restored SVC-0002 primary" --kind success
uv run netbox-intent problems                          # NetBox data that cannot become intent
```

```python
from netsim_netbox.intent import load_intent, render_device, bsm_spec, blast_radius, write_journal

intent = load_intent()  # one consistent snapshot (RO token)
render_device("spoke-2", intent)  # netsim_common.DeviceConfig
blast_radius("hub-a", intent)  # [ServiceImpact(service_id="SVC-0001", impact="degraded", ...)]
```

## Contract changes (docs/CONTRACT.md §13, all dated 2026-10-19, session 03-netbox)

| § | Change |
|---|---|
| 2 | NetBox / PostgreSQL / Valkey pins (below). |
| 4 | `netbox-worker` 172.30.10.31; NetBox inside labnet = `http://172.30.10.30:8080`; DB/Valkey on internal `netbox-internal`. |
| 10 | **PW ends: hub termination + `spoke_ac` custom field** (text of §10 updated, rendering rule reworded); `if_role`; VLAN as single tagged VLAN in group `l2vpn-acs`; NetBox interface types; platform selects PEs; `netsim-lab` tag; status informational; 4.7 `{value, label}` selections. |
| 9.3 | NetBox IDs/URLs in the spec (`netbox_tenant_id`, `netbox_l2vpn_id`, URLs, `NETBOX_PUBLIC_URL`). |
| 10/11 | Webhook + event rule, HMAC-SHA512 `X-Hook-Signature` with `NETBOX_WEBHOOK_SECRET`; lab-api should debounce. |
| 11 | v2 tokens, bootstrap, `netsim-bot` read-only user, new `.env.example` variables, `gen_env.py` pepper length. |

## Pinned versions

| Thing | Pin | Verified |
|---|---|---|
| NetBox image | `netboxcommunity/netbox:v4.7.3-5.1.1` (digest `sha256:1d6e0b5fbd48…`) | GitHub releases: v4.7.3 (2026-10-13) = latest 4.x; netbox-docker 5.1.1 (2026-09-10); Docker Hub tag pushed 2026-10-17; `/api/status/`: `4.7.3-Docker-5.1.1`, Python 3.14.4, Django 6.1.2 |
| PostgreSQL | `postgres:16.15-alpine3.24` | Docker Hub (2026-09-21); NetBox 4.7 needs ≥ 15 (+ `ltree`, installed by the migrations) |
| Valkey (queue + cache) | `valkey/valkey:8.1.10-alpine3.24` | same image as `core`; NetBox 4.7 needs Redis ≥ 6 |
| httpx | 0.28.1 | workspace pin (simd) |

API fields were taken from the running instance's OpenAPI schema (`/api/schema/`, 322 paths) and probed (e.g. tags
written as `[{"slug": ...}]`, `?address=10.255.1.1` matching any prefix length, HTTP 400 for unknown tag/platform slugs).

## How to verify

```bash
make netbox-up                 # healthy + tokens (prints both token keys, "created" / "kept")
make netbox-seed               # first run: creates everything; run it again: "0 changes (NetBox matches the topology)"
uv run netbox-intent render-all --out build/intent --check-golden --yanglint
uv run netbox-intent bsm-spec --normalized | diff - <(uv run netsim-contract bsm-spec) && echo "BSM spec equal"
uv run netbox-intent blast-radius hub-a; uv run netbox-intent blast-radius spoke-2
make netbox-test               # unit tests, < 1 s, no containers
make netbox-test-integration   # throwaway NetBox (~4 min first start; NETBOX_IT_KEEP=1 to reuse)
```

## Acceptance results (2026-10-19)

**NetBox up, healthy, both tokens, UI login** (`make netbox-up` on empty volumes; first start 232 s):

```
netbox-bootstrap: NetBox 4.7.3 at http://127.0.0.1:8000
  NETBOX_TOKEN_RW  nbt_AtFZnbiQbd4N.***   user admin       write enabled                      created -> .env
  NETBOX_TOKEN_RO  nbt_dzIPSZkQ42ya.***   user netsim-bot  read-only (133 object types)       created -> .env
# second run: both "kept"
POST /login/ -> 302 Location: / | session cookie: True                    (UI login with the .env superuser)
GET /vpn/l2vpns/?tag=l2vpn-pw -> 200 | 12 PWs listed                       anonymous -> 302 /login/
```

**`make netbox-seed` twice** (the first run on this NetBox happened in two steps around the termination-model
decision; the integration suite shows the single-run first seed on a fresh NetBox):

```
netbox-seed: NetBox 4.7.3 at http://127.0.0.1:8000, topology contract/topology.yaml (6 devices, 6 services)
netbox-seed: 27 change(s): 15 created, 12 updated
netbox-seed: journal entry written on 6 device(s)
netbox-seed: NetBox 4.7.3 at http://127.0.0.1:8000, topology contract/topology.yaml (6 devices, 6 services)
netbox-seed: 0 changes (NetBox matches the topology)
```

**Golden equality and YANG** (`netbox-intent render-all --out build/intent --check-golden --yanglint`):

```
netbox-intent: wrote 6 config(s) to build/intent: hub-a, hub-b, spoke-1, spoke-2, spoke-3, spoke-4
  hub-a: equals contract/golden/hub-a.json (byte for byte)
  hub-b: equals contract/golden/hub-b.json (byte for byte)
  spoke-1: equals contract/golden/spoke-1.json (byte for byte)
  spoke-2: equals contract/golden/spoke-2.json (byte for byte)
  spoke-3: equals contract/golden/spoke-3.json (byte for byte)
  spoke-4: equals contract/golden/spoke-4.json (byte for byte)
  yanglint: 6 file(s) valid against netsim-device (config)
```

**BSM spec**: `netbox-intent bsm-spec --normalized` equals `netsim-contract bsm-spec` (`bsm_spec_from_topology()`);
the full spec (4 customers, 6 services, 12 PW leaves) validates against `contract/bsm-spec.schema.json`.

**Blast radius** (`netbox-intent blast-radius ...`): `hub-a` → all six services `degraded` (primary lost);
`spoke-2` → `SVC-0002` and `SVC-0003` `down`, the other four `unaffected`.

**Tests**

```
make lint   -> ruff check: All checks passed! / ruff format: 111 files already formatted / pyang + yanglint clean
make test   -> 354 passed in 19.85s   (165 foundation + 113 devices + 76 netbox)
make netbox-test-integration -> 15 passed in 273.13s (fresh throwaway NetBox; 229 s of it is the first start)
```

| Integration test (real NetBox 4.7.3, throwaway stack) | Checks |
|---|---|
| version and tokens | `4.7.3`; RO = `netsim-bot`, reads OK, every write 403, `users/*` and webhooks 403 |
| bootstrap idempotent / stale token | second bootstrap keeps both; a stale RW value is replaced, written to the file, the old token deleted |
| seed twice | first run creates > 100 objects and journals all 6 devices; second run: 0 changes |
| journal | every device has a "Seeded by `netbox-seed`" entry |
| drift | a hand edit of an uplink MTU/description: dry run reports exactly that interface and writes nothing; seed repairs it |
| golden + yanglint | `render_all` from NetBox equals the 6 golden files; yanglint accepts them |
| intent change | `SVC-0002-primary` identifier 1002 → 1102: spoke-2 and hub-a differ exactly at `l2vpn/xconnect[SVC-0002]/pseudowire[primary]/vc-id`, others unchanged; reverted |
| blast radius, queries, journal | as above; `write_journal("l2vpn:SVC-0003-backup", ...)` stored with kind success |
| BSM spec | equals the topology spec after `normalized()`, validates, leaves carry NetBox IDs/URLs |
| added / deleted L2VPN | a NetBox-created `SVC-0007-primary` (new subinterfaces, VLAN, L2VPN, hub termination) appears as a new leaf, disappears when deleted; deleting `SVC-0001-backup` drops that leaf, re-seeding recreates it (L2VPN + hub-b termination) |
| prune | a stale `netsim-lab` tenant is deleted only with `--prune`; an untagged one is kept |
| webhook / event rule | payload URL, method, secret, object types, event rule → webhook |
| CLIs | `netbox-seed --check` (exit 0, 0 changes), `netbox-intent render spoke-1 --json` (= golden), `blast-radius --json`, `problems` |

## Measured timings

| Path | Measured |
|---|---|
| First start (empty volumes) → `netbox` healthy (migrations + superuser) | 232 s |
| `netbox-seed` on an empty NetBox (171 objects + 6 journal entries) | 5.9 s |
| `netbox-seed` with nothing to do | 2.1–2.3 s (GET requests only) |
| `load_intent()` (REST snapshot) / `render_all` | 0.67 s / 0.7 ms |
| `netbox-intent render-all --check-golden` / `bsm-spec` (CLI) | 1.0 s / 1.1 s |

## Known limitations

- The spoke end is not a NetBox termination: the spoke subinterface's page does not list its L2VPNs. Find them with
  `/vpn/l2vpns/?cf_spoke_ac=<interface id>`; `netbox-intent services <spoke>` lists them too.
- Intent ignores L2VPN `status` and has no field for "PW admin down": rendered xconnects/PWs are always enabled.
- One webhook per changed object (NetBox behaviour): a seed run can POST dozens; lab-api must debounce.
- Tokens are written after NetBox is up: a container that read `.env` earlier (lab-api) must be restarted to see them.
- VLAN IDs are unique per VLAN group, i.e. across the lab. True here (every service lands on both hubs).
- netbox-docker's `configuration.py` always sets `LOGIN_REQUIRED`, so NetBox logs a FutureWarning on each start.
- First start takes ~4 minutes (migrations of a fresh 4.7 database).

## Notes for sessions 04–05

- **Zabbix (04):** take the spec from `netsim_netbox.intent.bsm_spec(load_intent())` or `netbox-intent bsm-spec`
  (RO token; from a container `NETBOX_URL=http://172.30.10.30:8080`, keep `NETBOX_PUBLIC_URL=http://127.0.0.1:8000`
  for browser links). Hosts: NetBox devices with platform `netsim-clixon`, `primary_ip4` = mgmt address. Leaves carry
  `netbox_l2vpn_id` (field + tag), customers `netbox_tenant_id`.
- **lab-api (05):** `POST /hooks/netbox` from 172.30.10.31 with NetBox 4.7's standard payload (`event`, `timestamp`,
  `object_type`, `data`, `snapshots`, `request`); verify `X-Hook-Signature` = hex HMAC-SHA512(`NETBOX_WEBHOOK_SECRET`,
  raw body); debounce, then run zabbix-sync. Bot tools: the `netsim_netbox.intent` functions (RO) and `write_journal`
  (RW) for remediation records. Restore NetBox intent: `make netbox-seed SEED_ARGS=--prune`.

## Sessions 01/02 review

Session 01's artifacts were correct as consumed: the renderer reproduces every golden file from NetBox, and the BSM
builders/`normalized()` made source-independent comparison trivial. One foundation file changed: `tools/gen_env.py`
(pepper length; reason above). Session 02's device lab kept running throughout; nothing in `devices/` was touched.
