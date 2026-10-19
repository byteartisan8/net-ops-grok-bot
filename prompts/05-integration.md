# Session 05 — Integration: labctl, lab-api, scenarios, drift check, end-to-end tests

## Role
You are a senior engineer integrating the lab into one reliable, demo-ready system. You also build the tool surface
(`lab-api`) that a Grok bot will call later, so its API must be clean, typed, safe and documented.

## Read first
`docs/CONTRACT.md`, then **all** of `docs/reports/01-…04-*.md`. Note every deviation they logged and build on what
actually exists, not on what the prompts assumed.

## You own
`lab/` (`lab/compose.yaml`, labctl, lab-api, scenarios, e2e tests), root `README.md` updates (quickstart/runbook
sections only), `docs/RUNBOOK.md`, `docs/reports/05-integration.md`. You must implement `make reset` and `make e2e`.
Small fixes in other sessions' paths are allowed **only** to fix integration bugs; list each one in your report.

## Tasks

1. **Bring-up**: make `make env && make net && make build && make up && make seed` work end to end on a clean host,
   in the right order, with wait-for-healthy steps and clear errors. Fix integration issues (trap port, tokens,
   provisioning order: devices up → NetBox seed → Zabbix provision → wait for LLD → build the map).

2. **Drift checker** (library + CLI): for each device, compare NetBox intent (`netbox-intent`) with the Clixon running
   config (RESTCONF), using `diff_configs`.
   - Output: per-device structured diffs, plus a **semantic finding** per service, e.g.
     `SVC-0002: both PWs terminate on hub-b (intent: hub-a + hub-b) — redundancy lost`,
     `SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001`.
   - Classify each finding: `config-drift` (device ≠ NetBox), `fault` (an active fault key, no drift), or
     `intent-only` (NetBox changed, device not yet updated).
   - Exit code ≠ 0 if any drift is found; `--json` output.

3. **`labctl` CLI** (Typer, installed via the uv workspace)
   - `status`: one table per service combining intent (NetBox), live device state (simd `/state`: oper + reason per end)
     and Zabbix service status. Add a `--watch` mode that refreshes every 1 s; this is the demo's live view.
   - `fault uplink <host> [--restore]`, `fault ac <host> <if> [--restore]`, `cut <a> <b> [--restore]`,
     `device stop|start <host>`, `fault clear-all`.
   - `config get <host> [path]`, `config set <host> <path> <json>` (RESTCONF helpers that behave like an engineer
     making a change).
   - `drift [--json]`.
   - `scenario list | apply <name> | verify <name>`, and `reset` (every device back to golden via RESTCONF PUT, all
     faults cleared, stopped devices started, then wait until everything is green in simd **and** Zabbix).

4. **Scenarios** (declarative YAML in `lab/scenarios/`: steps + expected outcome; the same files drive the e2e tests)
   Each scenario declares its steps (faults and/or RESTCONF config changes), then the expected per-PW oper/reason
   from simd, the expected Zabbix status per affected SVC, and the expected drift findings.
   - `healthy`: everything OK, no drift.
   - `primary-fail`: uplink fault on hub-a → all 6 SVC **Warning**, no drift (it's a fault).
   - `hub-down`: `docker stop hub-b` → all 6 SVC **Warning**.
   - `spoke-isolated`: uplink fault on spoke-2 → SVC-0002, SVC-0003 **Disaster**.
   - `double-failure`: hub-a uplink fault + hub-b stopped → all SVC **Disaster**.
   - `vcid-typo`: spoke-1 SVC-0001 backup vc-id 2001→2010 → SVC-0001 **Warning**, drift finding on spoke-1.
   - `mtu-mismatch`: hub-b access MTU change → affected SVCs **Warning**, reason `mtu-mismatch`, drift finding.
   - `fake-redundancy`: on spoke-2, repoint SVC-0002 primary to hub-b (10.255.0.2, vc-id 1002); add a matching primary
     PW to hub-b's SVC-0002 xconnect; remove SVC-0002 from hub-a. Traffic still flows on both PWs, but both go through
     hub-b. Expected Zabbix (contract §9.2): `missing` (High) on hub-a for SVC-0002 primary, `spec` (Average) on spoke-2
     for SVC-0002 primary (peer 10.255.0.2, expected 10.255.0.1), `unexpected` (Warning) on hub-b → SVC-0002
     **Warning**. Expected drift finding: "redundancy lost: both PWs on hub-b". Verify step: stop hub-b → SVC-0002
     **Disaster**. This scenario must also assert that the three Zabbix problems can be linked to one root cause from
     the API data alone (same service tag, overlapping time window); that's the correlation the bot will do.
   - `new-service-pending`: add SVC-0007 (new customer or existing, spoke-3, VLAN 701, vc-ids 1007/2007) **in NetBox only**
     → webhook → `zabbix-sync` → new BSM nodes appear with `missing` on both ends → SVC-0007 **Disaster**
     ("ordered, not delivered"). Then push the NetBox intent to the devices through `lab-api` → SVC-0007 **OK**.
     Clean up by deleting SVC-0007 from NetBox and the devices → BSM nodes pruned.
   - `stale-backup`: break the backup PW of SVC-0001 and SVC-0005 through drift (different causes: one peer-IP drift,
     one vc-id mismatch) → both **Warning**; `blast_radius(hub-a)` from NetBox says degraded, but the *live* impact of
     hub-a maintenance would be **Disaster** for those two. Expose this as `labctl impact <host>`, which combines intent
     blast radius with live backup-path state.

5. **`lab-api`** (FastAPI at 172.30.10.50, host 127.0.0.1:8090, in `lab/compose.yaml`). This is the future bot's tool
   surface.
   - Read endpoints (no token): `GET /topology`, `/services`, `/services/{id}` (intent + live + Zabbix status in one
     response), `/devices/{host}/running`, `/devices/{host}/state`, `/devices/{host}/intent`, `/drift`, `/impact/{host}`,
     `/zabbix/problems?service=`, `/events?since=` (merged simd events across devices).
   - Write endpoints (header `X-Lab-Token: $LAB_API_TOKEN`): `POST /faults`, `DELETE /faults`, `POST /scenarios/{name}/apply`,
     `POST /reset`, and `POST /devices/{host}/config`. The config endpoint takes a JSON patch, **always returns the diff
     and a dry-run result first**, and applies only with `confirm=true`; it then waits for and returns the verification
     result from simd **and** Zabbix (that's the propose → approve → apply → verify loop the bot will use).
   - `POST /hooks/netbox` receiver: log + store the event, then run `zabbix-sync` (debounced, ~2 s) so the BSM and the
     `{$NETSIM.PW.SPEC}` macros always follow NetBox. Also `POST /sync` (token) to trigger it manually.
   - `POST /devices/{host}/apply-intent` (token, same dry-run → confirm flow): pushes the NetBox-rendered intent for that
     host, i.e. remediation in the "NetBox wins" direction.
   - Full OpenAPI with clear descriptions on every endpoint and field (they become the bot's tool descriptions).
     Export `lab/openapi.json`.
   - Audit log (JSONL on a volume) for every write: who, when, what, diff, result.

6. **E2E tests** (`make e2e`, pytest)
   - For every scenario: `reset` → `apply` → assert simd state, SNMP walk, Zabbix item values, Zabbix service status,
     drift findings → `reset` → assert green.
   - Timing assertions per contract §9 (fault → Zabbix service status ≤10 s, using traps). Also a polling-only variant
     (trap targets disabled) asserting ≤70 s.
   - Trap path proof: the master item `lastclock` updated before the next scheduled poll.
   - Persistence: apply `vcid-typo`, run `make down && make up`, and confirm the drift is still present.
   - Print a timing summary table at the end.

7. **Docs**: `docs/RUNBOOK.md` (bring-up, reset, every scenario with the exact commands and what to look at in the
   Zabbix UI, troubleshooting: traps not arriving, LLD not discovered, tokens). Update root `README.md` with a 5-minute
   quickstart.

## Acceptance criteria
- From a clean clone: `make env net build up seed e2e` passes completely; paste the timing table into your report.
- `labctl status --watch` shows intent, live state and Zabbix status side by side and updates within ~1–2 s of a change.
- `fake-redundancy` shows SVC-0002 as Warning with `missing`/`spec`/`unexpected` evidence whose event names state
  expected vs actual, `labctl drift` reports lost redundancy, and stopping hub-b turns it into Disaster.
- `new-service-pending` proves the BSM follows NetBox (create → Disaster → deliver → OK → delete → pruned).
- `lab/openapi.json` exported; write endpoints reject requests without the token; every write appears in the audit log.
- `docs/reports/05-integration.md` lists every cross-session fix you made, measured timings, and known limitations.

## Working rules
- Build on what the reports say actually exists; record contract deviations in the change log.
- Keep scenarios purely declarative. No hidden logic in tests that the scenario file doesn't describe.
- Out of scope: the Grok bot itself (only its tool surface).
