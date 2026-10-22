# Session 09 — NOC roles: three Grok Bot agents, Zabbix paging, link traps

**Date:** 2026-10-21. **Prompt:** [`prompts/09-noc-roles.md`](../../prompts/09-noc-roles.md) (the user's decisions of
2026-10-21). **Branch:** `feature/noc-roles`, uncommitted until the user asks.

**Result:**
- **The room** has exactly three robots: NOCENG (NOC Engineer) at the Zabbix console desk with a desk phone, NETENG
  (Network Engineer) between the server rack and the NetBox cabinet, NOCMGR (NOC Manager) at the desk by the incident
  board. The crew (POLL, PING, TRACE, DIG, PATCH), the visitor GROK, the Grok and rules brains, the pager and
  `BOT_AUTONOMY=auto` are gone.
- **Zabbix decides who is paged:** customer nodes of the BSM (`bsm_level=customer`) turning Warning or Disaster go
  through the service action `NetSim: customer alert` and the media type `NetSim NOC (grok-bot)` to grok-bot's
  `/hooks/zabbix`. grok-bot batches what arrives together into one incident and one page per role.
- **Alert storm:** a hub-a power-off gives exactly one incident and one NOC Engineer page listing all six customers,
  18 s after the fault (budget 30 s). Zabbix shows the acknowledgment by `noc-engineer`.
- **AC fault:** a `linkDown` trap, a re-poll and the interface problem in Zabbix in 4.5 s (budget 10 s).
- **Writes:** no role can approve or apply. The NOC Engineer's Zabbix acknowledgment is the only write outside change
  tickets. lab-api's direct reads refuse anything that writes (unit tests and the live e2e prove it).
- **Tests:** `make bot-e2e` plays every flow without the cloud: a routine sink stands in for the three routines and
  a scripted role player drives `/mcp` with the three role tokens. `make env net build up seed e2e` from a clean
  state passes (bot e2e 10, lab e2e 23), after fixing a latent lab-api bug the first clean run found
  ([below](#bugs-found-in-the-final-checks)).

---

## What was built

```
AC or PW fault -> linkDown / cpwVcDown trap -> Zabbix re-polls the router (<= 5 s)
  -> PW problems -> BSM customer node Warning (degraded) or Disaster (down)
     -> service action "NetSim: customer alert" -> media type "NetSim NOC (grok-bot)"
        -> POST grok-bot /hooks/zabbix (labnet) -> batch (10 s, max 30 s) -> one incident, one page
           -> NOC Engineer routine (Grok Bot app) -> /mcp (tunnel): take, ack, triage, field ticket, escalate
              -> Network Engineer: root cause, config tickets; NOC Manager: priority, recommendations, updates
                 -> a human approves -> grok-bot applies + verifies (lab-api) -> Zabbix OK -> "recovered" page
```

### 1. Link traps (IF-MIB `linkDown` / `linkUp`)

| Piece | Where | What |
|---|---|---|
| OID registry | `tools/gen_oids.py`, `contract/oids.yaml` | `"IF-MIB": ["linkDown", "linkUp"]`; regenerated (OBJECTS ifIndex, ifAdminStatus, ifOperStatus) |
| simd | `devices/simd/src/netsim_simd/traps.py`, `daemon.py` | `link_transition()` per interface on every recompute after the baseline: `linkDown` when ifOperStatus leaves up(1), `linkUp` when it enters up(1); every ifTable interface (uplink, access ports, AC subinterfaces); a parent port going down sends one trap per interface that changed; sent with the PW traps of the same recompute, link traps first (within the 100 ms batching, so ≤ 1 s) |
| Template | `zabbix/tools/src/netsim_zabbix/template.py`, regenerated YAML | two trap items `trap=link` matched by numeric OID, hint triggers tagged like the other trap triggers; the action `NetSim: SNMP trap -> check now` re-polls on them unchanged (verified live: trap event "SNMP trap linkDown (hint, re-polling now)" 2.5 s, interface problem 4.7-6.4 s after the fault) |
| Fixture | `zabbix/fixtures/send_trap.py` | `linkDown` / `linkUp` |

### 2. Zabbix pages the roles, through grok-bot

| Piece | Where | What |
|---|---|---|
| Customer tag | `bsm_sync.py` | every BSM node gets `bsm_level` = `root`, `customer`, `service` or `pw` |
| Macros | `noc.py` (zabbix-bootstrap) | `{$NETSIM.BOT.HOOK.URL}` = `http://172.30.10.60:8095/hooks/zabbix`; `{$NETSIM.BOT.HOOK.TOKEN}` (secret) = `BOT_ZABBIX_HOOK_TOKEN`, fingerprint in its description; `{$NETSIM.ESCALATE.AFTER}` created as `10m`, never reset afterwards |
| Media type | `noc.py`, `js/noc_webhook.js` | webhook `NetSim NOC (grok-bot)`, 3 attempts 10 s apart, message templates for service events whose message is the alert kind (`problem`, `recovery`, `update`); the JSON body of contract §9.6; throws on non-2xx so Zabbix retries and logs it |
| Users | `noc.py` | role `NetSim NOC` (User type, every service readable, API allow-list `event.acknowledge`, `event.get`, `problem.get`), group `NetSim NOC` (read on the hub and spoke host groups), users `noc-engineer`, `network-engineer`, `noc-manager` with the media (send-to = role) and API tokens `ZABBIX_NOCENG_TOKEN`, ... (kept while they authenticate as their user, else replaced and written into `.env`) |
| Service action | `noc.py` `sync_customer_action` | `NetSim: customer alert`, condition service tag `bsm_level` = `customer`; step 1 at once to `noc-engineer`; step 2 after `{$NETSIM.ESCALATE.AFTER}` to `noc-manager` with its own message `escalation`; recovery and update operations to `noc-engineer`. Idempotent: compared operation by operation, updated in place |

### 3. lab-api: direct reads and acknowledgments (additive)

| Endpoint | What |
|---|---|
| `POST /zabbix/api` `{method, params}` | the 13 allow-listed read methods with lab-api's token; `*.get` limited to 1000 (at most 10000); any other method 403; response size-capped |
| `GET /netbox/{path}` | GET-only passthrough to NetBox's `/api/` with the read-only token and the query parameters (repeated keys kept); size-capped |
| `GET /devices/{host}/restconf[/{path}]` | Clixon RESTCONF GET, `content=config\|nonconfig\|all`, percent-encoded keys passed through (`raw_path`) |
| `POST /zabbix/acknowledge` | lab token + `X-Lab-Actor`; `{eventids, message, role}`: trigger problem events as they are, service events stand for the open trigger problems of the customer's subtree; acknowledged with the role user's own token (`user.checkAuthentication` names the user in the result); already acknowledged problems only get the message; audited as `zabbix.acknowledge` |

`lab/openapi.json` regenerated; 25 new unit tests (`lab/tests/test_direct_reads.py`), including every write method,
`POST`/`PUT` on the read paths, size caps and the host-map fix below.

### 4. grok-bot 0.2.0: the office of three roles

| Piece | Where | What |
|---|---|---|
| Removed | `brains.py`, `grok.py`, `pricing.py`, `prompts.py`, `pager.py`, `agent.py`, their tests, `bot/e2e/test_grok_eval.py` | the crew and visitor, investigations and the responders' chat, `botctl grok-check`, `make bot-eval` |
| Kept | `engine.py`, `store.py`, `labapi.py`, `tools.py` | the lab feed (wall screen, rack LEDs, board), the store, approvals with `BOT_OPERATOR_TOKEN`, the executor (make before break, `plan_id` confirmation), the actor convention, `/mcp` and `BOT_MCP_HOSTS` |
| Alert intake | `api/app.py`, `alerts.py` | `POST /hooks/zabbix` (Bearer, constant-time compare; never through a `BOT_MCP_HOSTS` host); parses `{SERVICE.ROOTCAUSE}` into problems with tags and hosts (a PW problem's peer loopback mapped to its host) |
| Batching and incidents | `incidents.py`, `engine.py` | per-role batches (`BOT_BATCH_S`, `BOT_MAX_BATCH_S`); new incident, same fault (shared hosts or services), worse, back, better, recovered, Zabbix escalation; gold-down pages; ticket batches for the NOC Manager; alerts in maintenance or mute only recorded; batches survive a restart |
| Paging | `paging.py` | `BOT_ROUTINE_<ROLE>_URL` / `_KEY`; body with incident, customers, services, problems, links, `how_to_respond`, escalation note and triage; hourly cap, 3 tries, no retry on 4xx, delivery log; test sink with `Bearer test-sink`, never a real key |
| Roles on `/mcp` | `roles.py`, `api/mcp.py` | the token decides the role; tool sets of the plan; a wrong-role tool names the role that has it; per-role `initialize` instructions; `propose_*` gone under `observe` |
| Room | `web/` | three robots at their desks; they get up on the first tool call and walk to each tool's station; desk phone rings on a page; ticket progress on the board; the crew's seat, PATCH's bench, the couch and the door are gone |
| Field repairs | `engine.py` | dispatched → on site (`BOT_FIELD_DELAY_S`) → repaired, on the ticket and the board; the executor waits for them, so make-before-break order holds |
| Ask the NOC | `engine.py`, `web/js/panel.js` | pages the NOC Manager ("one routine run"); the answer arrives through `answer_question`; timeout after `BOT_ASK_TIMEOUT_S` |
| CLI | `cli.py` | `botctl call --role`, `tools --role`, `mcp-config --role --url`, `tunnel`, `page status\|test`, `mute`/`unmute`, `sink on\|off\|show` |
| Routine sink | `sink.py`, `bot/compose.yaml` profile `sink` | `grok-bot-sink` at 172.30.10.61:8096 (same image): records pages per role; `make bot-sink-up` / `bot-sink-down` |

### 5. The Grok Bot app's side (`bot/grokbot-app/`)

- [`SKILL.md`](../../bot/grokbot-app/SKILL.md): the shared reference `netsim-noc` (the lab, the page, every tool, the
  rules); three role skills in [`skills/`](../../bot/grokbot-app/skills/) (`noc-engineer`, `network-engineer`,
  `noc-manager`): what the role owns, when it escalates, what it never does.
- [`README.md`](../../bot/grokbot-app/README.md): the tunnel (`BOT_MCP_HOSTS=*.trycloudflare.com`, `botctl tunnel`;
  a named tunnel needs a domain), **three Bots** with one connector each, the skills, three webhook routines with
  their prompts and `.env` keys, the optional daily shift report, Slack, commands on your computer, what was verified.
- [`DEMOS.md`](../../bot/grokbot-app/DEMOS.md): alert storm, AC fault caught by linkDown, fake redundancy → the
  Network Engineer, gold customer down → the NOC Manager (with Zabbix's escalation step), Ask the NOC.

**Connectors per routine or app-wide?** The app's UI text answers it: "Add a custom MCP server for everyone using
this Bot", "Routines are recurring tasks this Bot runs", "Skills teach your Bot how to work". MCP servers, skills and
routines belong to a **Bot**, and an account can have several ("Create new Bot"). So the setup is one Bot per role,
each holding only its own connector; a single Bot with three connectors named per role is the fallback (its routine
prompts then pin their connector). Also found: routines whose results nobody reads are paused by the app ("Routines
paused while you were away"), which the troubleshooting table mentions.

### 6. Tests

- **Unit:** bot 73 (alerts and batching, incidents, paging, the office, API, `/mcp`, settings, tools, sink, the page),
  lab-api 25 new, zabbix 9 new (`test_noc.py`) plus updated template and sync tests, simd link-trap tests,
  `tests/test_oids.py`. `make test`: 630 passed.
- **Bot e2e** (`bot/e2e`, 6 files, no cloud): the room has exactly three robots; each role's tools, none approves;
  wrong-role refusals; direct reads cannot write (live 403/405); Ask the NOC; hub-a power-off end to end; fake
  redundancy end to end (three tickets proposed in reverse order, applied hub-a → spoke-2 → hub-b); AC fault via
  linkDown; spoke-2 uplink (two gold customers down) with `{$NETSIM.ESCALATE.AFTER}` set to `1m` for the test and
  restored; maintenance mode and mute. Every test starts and ends green; timings go to
  `build/bot-e2e/timings.json`.

---

## Zabbix 8.0.0rc1, verified on the running server

Probed with a webhook media type that echoed about 44 macros to a sink, a service action on Customer-A, and hub
uplink faults and restores; then confirmed in the live e2e.

| Question | Finding | Consequence |
|---|---|---|
| Which nodes raise service events? | only services an action matches (only Customer-A did in the probe) | the action selects `bsm_level=customer` |
| Macros in service alerts | resolved: `{EVENT.ID}`, `{EVENT.SEVERITY}`, `{EVENT.NSEVERITY}`, `{EVENT.STATUS}`, `{EVENT.VALUE}`, `{EVENT.TIMESTAMP}`, `{EVENT.TAGSJSON}`, `{SERVICE.NAME}`, `{SERVICE.ID}`, `{SERVICE.DESCRIPTION}`, `{SERVICE.TAGS}`, `{SERVICE.TAGSJSON}`, `{SERVICE.TAGS.<name>}`, `{SERVICE.ROOTCAUSE}` (one line per trigger problem: host, name, severity, age, tags; **no event ids**), `{ESC.HISTORY}`, `{ACTION.*}`, `{ALERT.*}`; `{EVENT.RECOVERY.*}` in recoveries. Left literal: `{EVENT.UPDATE.ACTION}`, `{EVENT.UPDATE.MESSAGE}`, `{EVENT.UPDATE.HISTORY}`, `{EVENT.CAUSE.ID}`, `{USER.USERNAME}` | the webhook sends unresolved macros as `null`; problems are parsed from `{SERVICE.ROOTCAUSE}` |
| Degraded → down | **not** a new event: an update operation of the open event; `{EVENT.UPDATE.SEVERITY}` = Disaster, but `{EVENT.SEVERITY}` stays Warning, `{EVENT.NAME}` stays "changed to Warning" and `{EVENT.UPDATE.STATUS}` = 0 | the alert kind comes from the message template (recovery 2 = `update`); the effective severity is the update's |
| Acknowledging service events | `event.acknowledge` on a service event, even as Super admin: "No permissions to referred object or it does not exist!" | lab-api acknowledges the trigger problems behind the customer |
| Finding those problems | `service.get` `selectProblemEvents` lists problem events on the **PW leaves only**; the customer node's list is empty (also for Super admin) | lab-api collects them from the customer's subtree (`parentids` + `deep_parentids`) |
| Who acknowledged | the event's `acknowledges` carry `userid` 4; `username` `noc-engineer` (with name and surname) is returned to Super admin only | the e2e checks it as Super admin, as the frontend shows it |
| Service alerts in the API | `alert.get` needs `eventsource` 4 / `eventobject` 6, and then returns service alerts to Super admin only (the Admin-type `netsim-api` gets none) | the skill shows `event.get` (source 4, object 6) instead; `zabbix_get`'s description says so |
| Media type creation | `mediatype.create` leaves it **disabled** unless `status` is given ("Media type disabled." on every alert) | the bootstrap sets `status: 0` |
| Action API | `pause_symptoms` and `notify_if_canceled` are rejected for service actions; conditions 25/26 (service tag name/value) work, 29/30 do not; action and operation `esc_period` accept `{$MACRO}` | the body and the comparison leave them out |
| Escalation timing | with `esc_period` 60 s, step 2 ran exactly 60 s after the problem | the e2e measures it end to end |
| Who may create the action | the provisioning user (Admin type) cannot see the role users (`user.get` returns only its own groups' users), and an operation needs their ids | the action moved into zabbix-bootstrap (deviation 1) |

---

## Deviations from the plan

1. **The service action is created by zabbix-bootstrap, not by provisioning.** Its operations name the role users,
   which the provisioning API user cannot see (Zabbix shows Admin-type users only the users of their own groups).
   Making `netsim-api` Super admin or a member of `NetSim NOC` would have widened an API token that lab-api uses for
   everything. It is still idempotent and survives `make clean && make up && make seed`.
2. **hub-a power-off sends no "gold down" page.** The plan's case lists "plus a NOC Manager page for gold down", but
   with hub-a off every gold service rides its backup through hub-b: Customer-A, B and E are degraded, only
   Customer-F (bronze) is down. The test asserts that the NOC Manager is *not* paged for it (it is paged for the
   field ticket), and the gold-down page is proven by a spoke-2 uplink fault (Customer-A and B down) in its own
   test, which the plan's case list also asks for ("gold customer down → NOC Manager").
3. **Acknowledgments go to the trigger problems of the customer's subtree**, not via `selectProblemEvents` on the
   customer node (empty in 8.0.0rc1, see above).
4. **`{$NETSIM.ESCALATE.AFTER}` is the operator's after creation:** the bootstrap creates it as `10m` and never
   resets it, so a value tuned in the frontend survives re-runs. Zabbix's shortest step is 60 s.
5. **The routine sink is a compose profile of the grok-bot image** (`grok-bot-sink` entry point), not a separate
   image; `make bot-e2e` starts it.

---

## Bugs found in the final checks

1. **Clean run: a fresh lab's `reset` through lab-api never got green** (240 s, then `green: false`), so every bot
   e2e test failed in its first `green()`. lab-api's Zabbix client cached the managed hosts' ids for its whole life.
   On a clean bring-up, grok-bot's 2 s feed makes lab-api ask Zabbix right after zabbix-bootstrap has written the new
   API token but before `make seed` has created the hosts, so an empty map was cached; every reset then waited for
   walk polls of hosts it could not see. The bug was latent: the lab e2e resets with its own client on the host, so
   only resets through lab-api soon after a clean bring-up hit it. Fix (`lab/src/netsim_lab/zabbix.py`): an empty map
   is never kept and a map expires after 60 s; unit test `test_the_host_map_is_not_kept_before_zabbix_is_provisioned`.
2. **Browser check: the room was clipped into an ellipse.** The new field-ticket progress chips used the class
   `.stage`, which is also the room's `<section class="stage">`; the second rule gave the room a 999 px border radius
   (it looked like a vignette in screenshots, while the canvas pixels were fine). Renamed to `.fstage`; a unit test
   now fails when a top-level CSS class rule is defined twice.

## Edits outside `bot/`

| Component | Files | Change |
|---|---|---|
| contract | `tools/gen_oids.py`, `contract/oids.yaml`, `tests/test_oids.py` | IF-MIB `linkDown` / `linkUp` |
| devices | `devices/simd/src/netsim_simd/traps.py`, `daemon.py`, `devices/simd/tests/test_traps.py`, `test_simd_pair.py` | link traps |
| zabbix | `zabbix/tools/src/netsim_zabbix/names.py`, `template.py`, `bsm_sync.py`, `settings.py`, `bootstrap.py`, new `noc.py` and `js/noc_webhook.js`; `zabbix/template/netsim_l2vpn_pe_snmp.yaml` (regenerated); `zabbix/fixtures/send_trap.py`; tests `test_noc.py` (new), `test_component.py`, `test_sync_units.py`, `test_template.py`; `zabbix/README.md`; `zabbix/zabbix.mk` (help text of `zabbix-bootstrap`) | trap items, `bsm_level`, NOC users, media type, macros, customer action |
| lab | `lab/src/netsim_lab/zabbix.py`, `devices.py`, `api/app.py`, `api/models.py`, `lab/openapi.json`, `lab/tests/test_api.py`, new `lab/tests/test_direct_reads.py`, `lab/e2e/conftest.py` (`pages_muted`), `lab/README.md` | direct reads, acknowledgments; the host-map fix (bug 1 above) |
| root | `.env.example` (grok-bot and Zabbix sections), `uv.lock` (bot 0.2.0, new script), `README.md` (bot sections, addresses, layout), `docs/CONTRACT.md` (§1, §3, §4, §8, §9.1, §9.5, new §9.6, §11, §12, change log), `docs/RUNBOOK.md`, `prompts/README.md` (rows 08 and 09) | config and docs |

---

## Timings

Measured by the clean acceptance run (`make clean`, then `make env net build up seed e2e`, 2026-10-21 16:51-17:21;
`build/bot-e2e/timings.json`, `build/lab-e2e/timings.json`). The bot e2e fails a test over its budget.

| Path | Measured (s) | Budget (s) | Where |
|---|---|---|---|
| hub-a power-off → NOC Engineer page sent (one page, six customers) | 18.3 | 30 | contract §9.5 |
| fake-redundancy → NOC Engineer page sent | 14.1 | 30 | §9.5 |
| AC fault → `linkDown` → re-poll → interface problem in Zabbix | 4.5 | 10 | §9.5 |
| AC fault → NOC Engineer page sent | 17.9 | 30 | §9.5 |
| spoke-2 uplink (two gold customers down) → NOC Engineer page sent | 18.6 | 30 | §9.5 |
| gold customer down → NOC Manager page sent | 18.6 | 30 | bot e2e |
| Zabbix escalation step (`{$NETSIM.ESCALATE.AFTER}` = 1m) → NOC Manager page | 78.7 | 100 | bot e2e |
| hub-a field ticket approved → recovered page (10 s field delay, hub-a boots) | 30.1 | — | |
| fake-redundancy: three tickets approved → applied → recovered page | 33.8 | — | |
| AC field ticket approved → interface problem resolved (10 s field delay) | 14.1 | — | |
| trap → walk re-polled (check-now), 8 traps (lab e2e) | median 3.9, max 5.4 | 7 (median 5) | §9.5 |
| fault → Zabbix business service status, 12 scenario phases (lab e2e) | 3.9-8.0 | 10 (12 power-off) | §9.5 |
| `make env net build up seed e2e`, images already built | 29 min 12 s | — | of which bot e2e 7 min 58 s, lab e2e 14 min 08 s |

Where a page's time goes: Zabbix's service event about 6-10 s after the fault (trap, re-poll, problems, the service
manager's 5 s cycle), the alert within a second, then grok-bot's batch (10 s after the latest alert). The earlier runs
on the long-lived lab measured 17-22 s for the same pages and 4.7-6.4 s for the interface problem.

---

## The Grok Bot app: verified and not verified

- **Verified here, without the cloud** (`make bot-e2e`): everything up to the routines' webhooks (Zabbix → grok-bot
  → pages, as the routines would receive them) and everything a routine does through `/mcp` (the scripted role player
  uses the three role tokens with `initialize`, `tools/list`, `tools/call`, exactly as an MCP client does).
  `botctl mcp-config` prints the connector JSON the app's "Add a custom MCP" takes (`botctl tunnel` prints the same
  three for the tunnel's URL).
- **Read from the app's UI text, not tried in the user's account:** several Bots; MCP servers, skills and routines
  per Bot; the webhook trigger with URL and key; routines pausing themselves; commands on the user's computer; Slack;
  Egress.
- **Not verified at all:** that a webhook routine's run can call its Bot's MCP connector; how the app hands the
  webhook's JSON body to the routine; that the webhook key is accepted as `Authorization: Bearer <key>` (Cursor's
  automations take `Bearer crsr_...`, from community threads); the app's own rate limits; `cloudflared` is not
  installed on this computer, so `botctl tunnel` was tested only for its missing-binary message and its config
  output.

## Old values in `.env`

`make env` never removes or changes values. The settings this session removed (`XAI_API_KEY`, `GROK_MODEL`,
`GROK_REASONING_EFFORT`, `BOT_BRAIN`, `BOT_DAILY_BUDGET_USD`, `BOT_MAX_STEPS`, `BOT_SETTLE_S`, `BOT_MAX_SETTLE_S`,
`BOT_AGENT_TOKEN`, `BOT_AGENT_NAME`, `BOT_PAGER_*`) may therefore stay in an existing `.env`. Nothing reads them;
`BOT_AUTONOMY=auto` is read as `propose`, with a warning in `/api/state`. `make env` appended the new variables to
this computer's `.env` (backup: `build/.env.before-noc-roles-20261021-114408`) and kept `ZABBIX_ADMIN_PASSWORD`.

---

## Checks

| Check | Result |
|---|---|
| `make test` | 630 passed |
| `make lint` (ruff, format, `pyang --strict`, yanglint) | clean |
| `make contract-check`, `make lab-check`, `make zabbix-check` | OIDs, schemas, YANG tree, `lab/openapi.json` and the template are current |
| Clean acceptance, run 1 (16:13) | **failed**: every bot e2e test after the office ones stopped in `green()` (bug 1 above); `make` stopped before lab-e2e |
| Clean acceptance, run 2 (16:51, after the fix): `make clean CONFIRM=yes`, `make env net build up seed e2e` | **passed**, exit 0: bot e2e 10 passed, lab e2e 23 passed (log `build/clean-run-noc-roles-2.log`) |
| `make zabbix-test-integration` (grok-bot paused) | 11 passed, 2 failed, 1 skipped; both failures predate this session: `test_service_added_in_netbox_is_ordered_not_delivered_then_pruned` creates the NetBox L2VPN slug `svc-0007-...`, taken by the real SVC-0007 since session 06; `test_pw_repointed_but_up_is_spec_with_expected_vs_actual` waits 30 s for an LLD-discovered `unexpected` PW (it passed 1 of 2 re-runs; the lab e2e's fake-redundancy phase, the same situation, measures 21-42 s across sessions). Left for the zabbix component (`zabbix/tests/integration/` is not this session's), suggested as a separate task |
| Live, by hand with `botctl call` on the long-lived lab | hub-a power-off → one page (22.5 s) listing six customers → take, ack (Zabbix: acknowledged by `noc-engineer`), triage, field ticket → NOC Manager recommends → approved → dispatched, on site, repaired → one recovered page → closed; `close_incident` as the Network Engineer refused |
| The room in the browser | three robots at their desks, board, wall screen, desk phones; the Approvals badge hidden when empty |

Before each `make clean` the volumes and `.env` were backed up: `build/volume-backup-20261021-161256` (the lab as it
was before this session's clean runs) and `build/volume-backup-20261021-165109` (the lab of clean run 1).
`ZABBIX_ADMIN_PASSWORD` stayed `zabbix`.

## How to verify

```bash
make env net build up seed
```

```bash
make bot-e2e
```

```bash
make bot-sink-up
```

```bash
uv run labctl device stop hub-a
```

```bash
uv run botctl sink show
```

One page to `noc-engineer` lists six customers. Then play the role (`uv run botctl call --role noc-engineer
take_incident -a incident_id=INC-0001`, `ack_zabbix_problems`, `propose_field_repair`), approve in the room, and
watch the ticket go dispatched → on site → repaired and the recovered page arrive.
