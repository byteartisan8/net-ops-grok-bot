# Session 04 — Zabbix 8.0 (trap + poll monitoring, NetBox-driven BSM, dashboard)

## Role
You are a senior monitoring engineer building fast, trustworthy, **intent-aware** L2VPN monitoring in Zabbix 8.0.
The business service tree (BSM) is generated from NetBox, and every node carries its NetBox spec. Zabbix checks each
pseudowire against that spec, so a PW that is missing or repointed is a visible problem, not silence. A trap tells
Zabbix *something happened*; an immediate SNMP poll confirms *what is true*.

## Read first
`docs/CONTRACT.md` (especially §4, §8, **§9 entirely**, §10, §11) and `docs/reports/01-foundation.md`.
Use `contract/oids.yaml`, `contract/topology.yaml`, `contract/bsm-spec.schema.json` and the `BsmSpec` model in
`lib/netsim_common`.

## You own
`zabbix/` (`zabbix/compose.yaml`, template YAML, `zabbix/tools/` provisioning package, dashboards, test fixtures),
`docs/reports/04-zabbix.md`. You may add Makefile targets prefixed `zabbix-` and must implement `zabbix-provision`
(used by `make seed`) and `zabbix-sync` (BSM + spec macros only; fast; called again whenever NetBox changes).

## Version
Zabbix **8.0 LTS**. Check zabbix.com release notes and Docker Hub: pin `8.0.0` GA images if they're published; otherwise
pin the newest `8.0.0rcN` and record the planned bump to GA in your report. Use the **8.0** documentation only. Item
types, preprocessing, the services API, script/webhook settings, dashboard widgets and import/export may differ from
7.x, so verify each one. Where 8.0 adds something that fits (for example native dashboard import/export for keeping the
dashboard in git, or inherited tags), use it and say so in your report.

## Tasks

1. **Deployment** (`zabbix/compose.yaml`)
   - PostgreSQL (newest version supported by 8.0), `zabbix-server-pgsql` (172.30.10.40), `zabbix-web-nginx-pgsql`
     (172.30.10.41, host 127.0.0.1:8080), `zabbix-snmptraps` (172.30.10.42), and a shared traps volume read by the
     server (SNMP trapper enabled).
   - Confirm the UDP port snmptrapd actually listens on inside the official image and **record it in the contract
     change log** (simd's `TRAP_TARGETS` default depends on it).
   - Mount `contract/mibs` into snmptraps so traps are readable, but match traps in items by **numeric OID**.
   - Tune what's needed for speed (enough trappers/pollers, sane timeouts). Enable whatever global-script setting 8.0
     requires for the webhook script below.
   - First start: change the default Admin password to the `.env` value and create an API token automatically
     (`ZABBIX_API_TOKEN`, written into `.env` if unset), for a dedicated API user with the minimum role needed.

2. **Template `NetSim L2VPN PE SNMP`** (YAML in git, imported via the API)
   - **Master item**: SNMP `walk[...]` over the cpwVcTable columns (+ IF-MIB), every 60 s. Everything else is dependent.
   - **Intent LLD "Expected PWs"** (contract §9.2): discovers rows from the host macro `{$NETSIM.PW.SPEC}`
     (a JSON list of `{service, role, vc_id, expected_peer, expected_peer_host, customer, ac}`). Choose the cleanest
     8.0 mechanism for "LLD whose data is a macro" (e.g. a Script-type discovery rule returning the macro). Check the
     macro value length limit in 8.0. If it's too small for a host with many PWs, use the next-cleanest option and
     document it.
     - Item prototypes are dependent on the walk master. JS preprocessing finds the row where
       `cpwVcName == "{#SERVICE}:{#ROLE}"` and returns `present`, oper status, admin status, peer (dotted quad from the
       hex InetAddress), VC ID and remote MTU. If the row is absent, return a defined "absent" value (not an error), so
       the `missing` check is reliable.
     - Trigger prototypes, exactly per contract §9.2: `missing` (High), `oper` (High), `spec` (Average: peer or VC ID ≠
       expected), all with the full tag set. Event names show expected vs actual and the expected peer hostname, e.g.
       `SVC-0002 primary on spoke-2: peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)`.
       Add dependencies so `oper` is suppressed when `missing` fires, and `spec` doesn't double-alarm with `missing`.
   - **Observed LLD "PW table"**: SNMP-walk-to-JSON over cpwVcTable with `{#SNMPINDEX}`, `{#VCNAME}`, `{#VCID}`,
     `{#PEER}` and `{#CUSTOMER}`; dependent items for visibility; one trigger prototype `unexpected` (Warning,
     `scope=l2vpn-unexpected`) when `{#VCNAME}` isn't in `{$NETSIM.PW.SPEC}` (or a macro with context derived from it).
     Not mapped into the BSM.
   - IF-MIB LLD for interfaces (oper/admin status, MTU).
   - **Trap items**: `snmptrap[...]` for `cpwVcDown`/`cpwVcUp`, `ciscoConfigManEvent`, `coldStart`, and `snmptrap.fallback`.
     Trap triggers create one event per trap (multiple problem events), Information severity, tag `trap=pw|config|boot`,
     and recover automatically shortly after.
   - A host-level SNMP availability trigger.

3. **Trap → poll ("check now") automation**
   - Webhook-type global script that calls the Zabbix API "check now" task for the host's walk master item on
     `trap=pw`, and additionally for both LLD rules on `trap=config|boot`. Store the API token as a **secret** global macro.
   - An action that runs it on trap-trigger events.
   - Prove it: after a trap, the master item's `lastclock` updates within ≤5 s, well before the 60 s poll.

4. **NetBox-driven provisioning and BSM sync** (Python, idempotent)
   - Input: the **BSM spec** (contract §9.3), produced by `netbox-intent bsm-spec` (session 03, NetBox RO token).
     For parallel development, also implement `--source topology`, which builds the same `BsmSpec` from
     `contract/topology.yaml`. NetBox is the default. Validate the input against `contract/bsm-spec.schema.json`.
   - `zabbix-provision`: host groups `L2VPN/Hubs` and `L2VPN/Spokes`; hosts (name = hostname, SNMPv2 interface =
     mgmt IP:161, `{$SNMP_COMMUNITY}=netsim`), host tags (role, site), inventory from NetBox, template link; then runs
     `zabbix-sync`.
   - `zabbix-sync` (fast, safe to run often):
     - Writes `{$NETSIM.PW.SPEC}` per host from the spec (both ends of every PW leaf).
     - Builds the BSM tree exactly as contract §9.4: root `L2VPN` → customers → `SVC-xxxx (spoke)` → PW leaves. Each
       node gets the spec's tags and a description containing its NetBox URL and the expected values (for leaves:
       a-end/z-end host, AC, VC ID, expected peers), so anyone reading the BSM, including the bot, sees the spec.
     - Leaf problem-tag mapping: `scope=l2vpn-pw`, `service`, `pw_role`. Service `redundant-pair` status rules:
       ≥2 children ≥ High → Disaster; ≥1 child ≥ Average → Warning. Customer and root use most-critical-child.
     - SLA objects per `sla_tier` (gold 99.95 %, silver 99.9 %) selecting services by tag.
     - **Diff-based and idempotent**: creates new nodes, updates changed ones, **prunes** nodes and macro entries that
       are gone from NetBox (only objects tagged as lab-managed). Prints a plan; `--dry-run` supported. A second run
       makes 0 changes.
   - Expose `sync()` as an importable function, so lab-api (session 05) can call it from a NetBox webhook.

5. **Dashboard `L2VPN Services`** (definition kept in git; use 8.0 native dashboard import/export if available)
   - Problems by severity for `scope=l2vpn-pw`, current problems with tags, the SLA report, a honeycomb (or equivalent)
     of PW status, and a **topology map** `L2VPN Topology` (hubs and spokes; spoke↔hub links colored by that pair's PW
     triggers, built from the spec after LLD has run).
   - Use only widget types that exist in 8.0.

6. **Fixtures for parallel development**
   - Until the real devices exist, run snmpsim with static `.snmprec` fixtures generated from `contract/oids.yaml` and
     `topology.yaml` (`zabbix/fixtures/`) on the device IPs, and send traps with a tiny pysnmp script. Include fixture
     variants for: healthy, one PW down, a PW missing, a PW with the wrong peer. Switch to the real devices once
     `devices/` is available.

## Tests
- Template import and `zabbix-sync` are idempotent (second run makes 0 changes).
- Intent LLD: discovers exactly the spec's PWs per host. Observed LLD: correct `{#VCNAME}` and dotted-quad `{#PEER}`.
- Leaf checks (fixtures or real devices):
  - PW down → `oper` High → SVC **Warning**; both PWs down → SVC **Disaster**; recovery → OK.
  - PW deleted on one end → `missing` High on that end → SVC **Warning**.
  - PW repointed (wrong peer, still up) → `spec` Average → SVC **Warning**, and the event name shows expected vs actual.
  - Extra PW not in NetBox → `unexpected` Warning, BSM unchanged.
- NetBox-driven changes: add a service to the spec → new BSM nodes with `missing` on both ends (Disaster); remove it →
  nodes and macro entries pruned.
- Trap test: `cpwVcDown` → trap item → action → check-now → master `lastclock` within ≤5 s.

## Acceptance criteria
- `make zabbix-up` → healthy; UI login works with the `.env` password; API token works.
- `make zabbix-provision` (NetBox source) creates 6 hosts, the full BSM with the spec in tags and descriptions, SLAs and
  the dashboard; re-running it makes 0 changes.
- With the real devices up: all 12 PWs present on both ends, all services OK, trap → check-now ≤5 s, fault → BSM status
  change ≤10 s (numbers in your report).
- `docs/reports/04-zabbix.md` covers: the pinned 8.0 version, the trap/poll flow, the intent-vs-observed design (with the
  macro mechanism chosen and its limits), BSM sync behavior, measured timings, and API output or screenshots of the
  BSM tree.

## Working rules
- Verify every API method, field, item type, preprocessing step and widget against the 8.0 docs; never guess.
- Everything goes through the API and is versioned in git; no manual clicking.
- Don't modify `contract/` (except the change-log entry for the trap port) or `lib/netsim_common/`.
- Out of scope: devices, NetBox internals, the bot.
