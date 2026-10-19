# Session 03 — NetBox (source of truth, seeder, intent renderer)

## Role
You are a senior network-automation engineer making NetBox the authoritative intent for an L2VPN lab. A bot will later
compare this intent with live device config to detect and fix drift, so the data model must be precise and the intent
rendering must be exact.

## Read first
`docs/CONTRACT.md` (especially §4, §5, §6, §10, §11) and `docs/reports/01-foundation.md`.
Use `contract/topology.yaml`, `contract/golden/*.json` and `lib/netsim_common` as-is.

## You own
`netbox/` (`netbox/compose.yaml`, config, `netbox/tools/` Python package), `docs/reports/03-netbox.md`.
You may add Makefile targets prefixed `netbox-` and must implement `netbox-seed` (used by `make seed`).

## Tasks

1. **Deployment** (`netbox/compose.yaml`)
   - Official `netboxcommunity/netbox` image (latest stable 4.x, pinned) with netbox, netbox-worker, PostgreSQL 16,
     and Redis/Valkey (queue + cache). Use netbox-docker as the reference for env/config, but keep it self-contained
     in this fragment.
   - `netbox` on external `labnet` at 172.30.10.30; DB/redis on an internal network; UI/API on host 127.0.0.1:8000.
   - Healthchecks; first start must run migrations and create the superuser from `.env` with no manual steps.
   - API tokens: automatically create `NETBOX_TOKEN_RW` (superuser or write-scoped user) and `NETBOX_TOKEN_RO`
     (a dedicated read-only user and token, for the bot). Verify how tokens work in the pinned NetBox version
     (token format and provisioning changed across 4.x releases) and write the resulting values into `.env`
     if they aren't set yet.

2. **Seeder** (`netbox-seed`, pynetbox or plain REST)
   - Reads `contract/topology.yaml` and creates everything in contract §10: region, sites, manufacturer, device type,
     platform, roles, devices, interfaces (including subinterfaces with parents), VLANs, IPs (Mgmt0 as primary_ip4,
     Loopback0 with role loopback), tenant group + tenants with custom field `sla_tier`, tag `l2vpn-pw`, custom
     fields `service_id` and `pw_role` on L2VPN, one L2VPN per PW (type vpws, identifier = vc-id, tenant), and its
     two terminations.
   - Decide and **document** how VLAN ID is stored on subinterfaces (e.g. VLAN object assignment vs custom field),
     choosing what's most idiomatic in the pinned version.
   - **Idempotent**: a second run reports 0 changes. Add `--dry-run` (prints a plan) and `--prune` (removes lab-tagged
     objects that aren't in the topology). Tag every object it creates with `netsim-lab`.
   - Write a NetBox journal entry on each device saying it was seeded (and when), as an example of the journaling
     the bot will use later.

3. **Intent renderer** (`netbox/tools/intent`, library + CLI `netbox-intent`)
   - `render_device(hostname) -> DeviceConfig` (`lib/netsim_common` model), using **only** NetBox data and the rendering
     rule in contract §10. CLI: `netbox-intent render <host> [--json]` and `netbox-intent render-all --out <dir>`.
   - Must not read `topology.yaml`. NetBox is the only input. The point is that if someone edits NetBox, the intent
     changes.
   - Validate the output against the YANG (yanglint) and the pydantic model.
   - **Key acceptance**: on a freshly seeded NetBox, `render-all` output equals `contract/golden/*.json`
     byte-for-byte after `canonical_json`.

4. **BSM spec export** (`netbox-intent bsm-spec [--out file.json]`, plus a library function)
   - Builds the contract §9.3 document **from NetBox only**: tenants (with `sla_tier`) → customers, L2VPNs grouped by
     `service_id` → services, each L2VPN → a PW leaf with both ends (host, AC, expected peer = the other end's Loopback0
     IP), `vc_id` = identifier, NetBox object IDs and URLs in tags and descriptions.
   - Output is a `BsmSpec` (`lib/netsim_common`) and validates against `contract/bsm-spec.schema.json`.
   - **Key acceptance**: on a freshly seeded NetBox it equals `bsm_spec_from_topology()` (ignoring NetBox IDs and URLs).
   - This is what makes the Zabbix BSM NetBox-driven: Zabbix's sync consumes exactly this output.

5. **Query helpers for the future bot** (library functions with CLI wrappers, read-only, using the RO token)
   - `services_on_device(host)` → services and PW roles terminating there.
   - `expected_pws(host)` → list of (service_id, role, peer hostname, peer loopback, vc-id, AC).
   - `blast_radius(host)` → per service: `down` if every PW of the service terminates on `host` (or on the spoke
     itself when `host` is that spoke), `degraded` if some do, `unaffected` otherwise. Based on **intent only**.
   - `service_detail(service_id)` → customer, spoke, both PWs with ends and vc-ids.
   - `write_journal(object, message, kind)` (RW token) for remediation records.

6. **NetBox change notifications**: a NetBox event rule + webhook that POSTs L2VPN/tenant/interface/IP changes to
   `http://172.30.10.50:8090/hooks/netbox` (lab-api, built in session 05, which re-runs `zabbix-sync` so the BSM
   follows NetBox). Create it from the seeder (flag `--webhooks`, on by default); it's harmless if lab-api isn't up yet.

## Tests
- Seeder idempotency (run twice → the second run reports 0 changes).
- Golden-equality test for all 6 devices.
- An intent-change test: change one L2VPN identifier in NetBox → the rendered spoke and hub configs change exactly
  at that PW's vc-id (assert with `diff_configs`), then revert.
- `blast_radius("hub-a")` → all 6 services `degraded`; `blast_radius("spoke-2")` → SVC-0002 and SVC-0003 `down`.
- BSM spec equality with `bsm_spec_from_topology()`; an added L2VPN in NetBox appears as a new leaf, and a deleted one
  disappears.

## Acceptance criteria
- `make netbox-up` (or `docker compose up -d` for the fragment) → NetBox healthy, UI login works, both tokens exist.
- `make netbox-seed` twice → the second run reports 0 changes.
- All tests pass, including golden equality and BSM spec equality.
- `docs/reports/03-netbox.md` covers: data model with screenshots or an API example, the VLAN storage decision,
  token handling, CLI usage, and how to verify.

## Working rules
- Verify NetBox API fields against the pinned version's docs/OpenAPI schema (L2VPN and termination fields changed in 4.x).
- No manual UI steps; everything is scripted.
- Don't modify `contract/` or `lib/netsim_common/`. If you need a change there, log it in the contract change log.
- Out of scope: devices, Zabbix, the bot.
