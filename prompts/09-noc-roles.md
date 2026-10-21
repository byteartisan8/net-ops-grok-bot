# Session 09 — NOC roles: three Grok Bot agents, Zabbix paging, link traps

## Role
You are a senior engineer turning the NOC room (grok-bot, <http://127.0.0.1:8095>) into the workplace of three AI
colleagues: **NOC Engineer**, **Network Engineer** and **NOC Manager**. Each one is a routine of the user's Grok Bot
app. The app thinks on its own cloud PC; it works in this lab only through grok-bot's tools. Zabbix, not a bot,
decides when they are paged.

## Read first
`docs/CONTRACT.md`, then `docs/reports/05-integration.md`, `07-grok-bot.md` and `08-grokbot-app.md`, then
`bot/README.md` and `bot/grokbot-app/README.md`. Build on what exists; most of the tool surface is already there.

## Decisions already made (by the user, 2026-10-21): do not revisit without asking
- **Only three robots in the room:** NOC Engineer, Network Engineer and NOC Manager. Remove the in-house crew (POLL,
  PING, TRACE, DIG, PATCH), the visitor GROK, the in-house Grok and rules brains, and the old pager.
- **Zabbix alerts on BSM customer nodes** (Customer-A, ...) when a customer is **degraded (Warning) or down
  (Disaster)**.
- **Alerts are batched.** Zabbix sends them to grok-bot, which bundles the alerts arriving together into one
  incident and one page.
- **The NOC Engineer may acknowledge Zabbix problems.** That is the agents' only direct write.
- **"Ask the NOC"** questions go to the NOC Manager.
- **No field-technician robot.** Field repairs happen off-screen; the ticket shows the progress.
- **Every other change is a change ticket that a human approves** (operator desk or `botctl approve`). Agents never
  approve or apply. `BOT_AUTONOMY=auto` goes away; `observe` and `propose` stay.

## Start state
Sessions 07 and 08 committed. This session deletes much of 07's code, and that commit is the way back.

## You own
This session crosses components. List every edit outside `bot/` in the report and in the contract's change log.
- **All of `bot/`.**
- **Devices:** `devices/simd` (link traps).
- **Contract:** `tools/gen_oids.py` and the regenerated `contract/oids.yaml` (IF-MIB notifications).
- **Zabbix (`zabbix/`):**
  - template trap items;
  - bootstrap: role users, media type, macros;
  - provisioning: the service action;
  - `bsm_sync`: the customer-level tag;
  - `fixtures/send_trap.py`.
- **lab-api (`lab/`):**
  - direct reads and acknowledgments;
  - `openapi.json`;
  - e2e `conftest.py`.
- **Config and docs:** `.env.example` (bot and Zabbix sections), `docs/CONTRACT.md` (sections + change log),
  `docs/RUNBOOK.md`, the root `README.md` sections on the bot, `prompts/README.md` (rows 08 and 09),
  `docs/reports/09-noc-roles.md`.

## Target

```
AC or PW fault on a router
 └─ trap (linkDown / cpwVcDown) → Zabbix re-polls that router (≤ 5 s)
     └─ PW problems → BSM customer node: Warning (degraded) or Disaster (down)
         └─ Zabbix service action → POST grok-bot /hooks/zabbix (lab network, no internet)
             └─ grok-bot batches (~10 s) → one incident + one page → NOC Engineer routine (Grok Bot cloud)
                 └─ the NOC Engineer works through /mcp (tunnel): triage, ack, field ticket, escalate
                     └─ Network Engineer: root cause, config ticket   NOC Manager: priority, review, updates
                         └─ the human approves → grok-bot applies and verifies → Zabbix OK → "recovered" page
```

## Tasks

1. **Link traps (IF-MIB `linkDown` / `linkUp`).** PW traps and the trap → check-now action already exist.
   - **OID registry:** add `"IF-MIB": ["linkDown", "linkUp"]` to the notifications in `tools/gen_oids.py`, then
     regenerate `contract/oids.yaml`.
   - **simd** (`traps.py`):
     - Send `linkDown` when an interface's ifOperStatus leaves up(1), and `linkUp` when it enters up(1).
     - This covers every interface in ifTable: the core uplink, access ports and AC subinterfaces.
     - Varbinds are exactly the MIB's (ifIndex, ifAdminStatus, ifOperStatus), sent within ≤ 1 s.
     - No traps for the baseline at start, as for PWs.
     - A parent port going down sends one trap per interface that changed.
   - **Zabbix template:**
     - Add a trap item `trap=link`, matched by numeric OID, with a hint trigger tagged like the other trap triggers.
     - The existing action `NetSim: SNMP trap -> check now` (conditions: template + trap tag) should then re-poll
       the router with no change. Verify it does.
   - **Fixture:** `fixtures/send_trap.py` learns `linkDown` / `linkUp`.
   - **Contract:** update §8 (traps), §9.1 (trap flow), and add a §9.5 row: AC fault → interface problem in
     Zabbix ≤ 10 s.

2. **Zabbix pages the roles, through grok-bot.**
   - **`bsm_sync`:** tag customer nodes `bsm_level=customer`. Service nodes already carry `sla_tier` too, so the
     tag is what lets an action select customers only.
   - **Bootstrap, the media type** `NetSim NOC (grok-bot)`:
     - A webhook that POSTs JSON to `{$NETSIM.BOT.HOOK.URL}` (`http://172.30.10.60:8095/hooks/zabbix`).
     - Header `Authorization: Bearer {$NETSIM.BOT.HOOK.TOKEN}`: a secret macro holding `BOT_ZABBIX_HOOK_TOKEN`
       from `.env`.
     - Fields: role (the media's send-to), event id, problem / recovery / update, severity, service name, service
       tags, the root-cause problems, event time.
     - Use the service-event macros 8.0.0rc1 really has (check `{SERVICE.ROOTCAUSE}`, `{SERVICE.TAGSJSON}`, ...
       on the running server).
   - **Bootstrap, the users:**
     - User group `NetSim NOC`, with users `noc-engineer`, `network-engineer` and `noc-manager`.
     - Each user has that media with send-to = its role.
     - Each user has an API token, so acknowledgments show who made them. Keep the tokens like
       `ZABBIX_API_TOKEN`: kept while valid, written to `.env`.
   - **Provisioning, the service action** `NetSim: customer alert` (condition: service tag
     `bsm_level=customer`):
     - Step 1: immediately to `noc-engineer`.
     - Step 2: after `{$NETSIM.ESCALATE.AFTER}` (default 10m), if the customer is still not OK, to `noc-manager`.
     - Recovery: to `noc-engineer`.
     - Find out how 8.0 reports a customer going from degraded to down (update operation, or new event). grok-bot
       must see it either way.
   - **Rebuilds:** all of this is idempotent and survives `make clean && make up && make seed`.

3. **lab-api: direct reads and acknowledgments** (additive; lab-api keeps every credential).
   - **Zabbix reads:** `POST /zabbix/api` `{method, params}`.
     - Only read methods on an allow-list: `problem.get`, `event.get`, `trigger.get`, `item.get`, `history.get`,
       `trend.get`, `host.get`, `hostgroup.get`, `service.get`, `sla.get`, `sla.getsli`, `alert.get`,
       `maintenance.get`.
     - Any other method gets 403. Responses are size-capped.
   - **NetBox reads:** `GET /netbox/{path}`. GET-only passthrough to NetBox's `/api/`, with query parameters,
     size-capped.
   - **Router reads:** `GET /devices/{host}/restconf/{path}?content=config|nonconfig|all`. A Clixon RESTCONF GET.
   - **Acknowledgments:** `POST /zabbix/acknowledge` (lab token, `X-Lab-Actor`) `{eventids, message, role}`.
     - It acknowledges the **trigger problems** behind the alert, as the role's Zabbix user. Service events
       probably cannot be acknowledged; verify that.
     - It goes in the audit log.
   - Export `lab/openapi.json` again; add tests.

4. **grok-bot: the office of three roles.**
   1. **Remove:**
      - **Crew and code:** the crew and visitor; investigations and the responders' chat; `brains.py`, `grok.py`,
        `pricing.py`, `prompts.py` and `pager.py`.
      - **Settings:** `XAI_API_KEY`, `BOT_BRAIN`, `BOT_DAILY_BUDGET_USD`, `BOT_MAX_STEPS`, `BOT_SETTLE_S`,
        `BOT_MAX_SETTLE_S`, `BOT_AGENT_TOKEN`, `BOT_AGENT_NAME`, `BOT_PAGER_*`, and `BOT_AUTONOMY=auto`.
      - **Commands:** `botctl grok-check` and `make bot-eval`.

      Keep:
      - the data feed (lab-api every `BOT_POLL_S`: wall screen, rack LEDs, board);
      - the store, and approvals with `BOT_OPERATOR_TOKEN`;
      - the change executor, with make-before-break ordering and `plan_id` confirmation;
      - the actor convention `grok-bot/<ROLE> approved-by:<who>`;
      - `/mcp` and `BOT_MCP_HOSTS`.
   2. **Alert intake and batching:** `POST /hooks/zabbix` (Bearer `BOT_ZABBIX_HOOK_TOKEN`; never reachable through
      `BOT_MCP_HOSTS`).
      - **Batching:** store every alert. Batch per role: `BOT_BATCH_S` (10) from the first alert, extended while
        alerts keep coming, at most `BOT_MAX_BATCH_S` (30).
      - **New incident:** a NOC Engineer batch opens **one incident**. It lists every alerted customer (tier,
        degraded or down) and the problems behind them, and sends **one page**. A gold customer down in the batch
        also pages the NOC Manager.
      - **Same fault:** later alerts whose problems share hosts or services with an open incident join it without
        a page.
      - **Worse:** a customer going from degraded to down sends one batched update page.
      - **Recovery:** recoveries mark customers recovered. When every customer of an incident is OK, send one
        "recovered" page to the NOC Engineer, who verifies and closes the incident.
      - **Zabbix escalation step:** the `noc-manager` alert pages the NOC Manager with the incident.
      - **No page:**
        - Problems that touch no customer (a bare interface, an `unexpected` PW) never page; they stay on the
          wall screen.
        - In maintenance mode, or muted, alerts are recorded but open no incident and send no page.
   3. **Roles on `/mcp`:**
      - **Tokens:** `BOT_NOCENG_TOKEN`, `BOT_NETENG_TOKEN` and `BOT_NOCMGR_TOKEN`, generated by `make env`. The
        token decides the role.
      - **Names:** the robots are NOCENG, NETENG and NOCMGR; the lab-api actor is `grok-bot/<ROLE>`.
      - **Tool sets:**
        - **All roles:** today's reads and NOC tools, plus the direct reads `zabbix_get`, `netbox_get` and
          `device_restconf_get`.
        - **NOC Engineer:** `take_incident`, `ack_zabbix_problems`, `submit_triage`, `propose_field_repair`,
          `escalate` (to Network Engineer or NOC Manager, with a note), and `close_incident` (only when Zabbix
          shows every customer OK).
        - **Network Engineer:** `submit_root_cause` (today's `finish_investigation` fields),
          `propose_apply_intent`, `propose_config_patch`, `propose_field_repair`, and `escalate` (to NOC Manager).
        - **NOC Manager:** `set_priority` (P1–P4), `recommend_ticket` (approve or reject, with the reason; shown on
          the ticket; never approves), `write_customer_update`, `write_shift_report`, and `answer_question`.
      - **Wrong role:** a tool outside the caller's set fails with a message naming the role that has it.
   4. **Paging the routines:**
      - **Settings:** `BOT_ROUTINE_<ROLE>_URL` and `_KEY` for each role (`Authorization: Bearer <key>`).
      - **Content:** the incident, its customers and services with tier and state, the problems, links (NOC room,
        Zabbix, NetBox), how to respond, and, for escalations, the note and the triage.
      - **Who gets paged:**
        - **NOC Engineer:** a new incident, a customer getting worse, all recovered.
        - **Network Engineer:** an escalation.
        - **NOC Manager:** the Zabbix escalation step, a gold customer down, change tickets filed (batched), an
          escalation, and Ask-the-NOC questions.
      - **Delivery:** hourly cap `BOT_PAGE_MAX_PER_HOUR`; 3 tries; no retry on 4xx; a delivery log.
      - **CLI:** `botctl page status|test <role>`, `mute` / `unmute`.
   5. **The room:**
      - **NOCENG** sits at the Zabbix console desk, with a desk phone that rings when it is paged.
      - **NETENG** sits between the server rack and the NetBox cabinet.
      - **NOCMGR** sits at a desk by the incident board.
      - **Movement:** they stay at their desks between runs, get up on their first tool call, and walk to each
        tool's station (today's mapping).
      - **Removed:** POLL's seat, PATCH's bench, the couch, and the visitor's entrance.
   6. **Field repairs off-screen:** an approved field ticket moves through *dispatched → on site*
      (`BOT_FIELD_DELAY_S`) *→ repaired* on the ticket and the board. No robot.
   7. **Ask the NOC:**
      - A question pages the NOC Manager. The panel says this counts as one run.
      - The answer arrives through `answer_question` and shows in the panel.
      - Unanswered questions time out with a message.
   8. **CLI:**
      - `botctl call --role <role> <tool> '<json>'`;
      - `botctl mcp-config --role <role> --url https://<tunnel>/mcp` prints the connector JSON;
      - `botctl tunnel` (when `cloudflared` is installed) starts a quick tunnel and prints all three connector
        configs.

5. **Grok Bot app side** (`bot/grokbot-app/`)
   - **Skills:** replace `SKILL.md` with a shared reference (the lab, the tools, the rules) and three skills:
     `noc-engineer`, `network-engineer` and `noc-manager`. Each says what the role owns, when it escalates, and
     what it never does.
   - **README:**
     - **The tunnel:** a quick tunnel's URL changes on every start, so set `BOT_MCP_HOSTS=*.trycloudflare.com`. A
       named tunnel needs a domain.
     - **Connectors:** three MCP connectors, one per role token. Check in the app whether connectors are app-wide
       or per routine. If app-wide, name them per role and pin each routine's prompt to its own.
     - **Routines:** three routines "when a webhook fires", with their URL and key going into `.env`. An optional
       daily shift-report routine for the NOC Manager.
     - **Slack:** optional.
   - **`DEMOS.md`:** rewrite for the three roles:
     - an alert storm (hub-a power-off → one page for 6 customers);
     - an AC fault caught by `linkDown`;
     - fake redundancy → the Network Engineer;
     - gold customer down → the NOC Manager;
     - Ask the NOC.
   - **Verified or not:** say what was verified in the user's account and what was not.

6. **Tests**
   - **Unit tests:** for everything above (bot, lab-api, simd, Zabbix template and provisioning). Lint clean.
   - **Bot e2e without the cloud:**
     - A small **routine sink** container stands in for the three routines.
     - A **scripted role player** drives `/mcp` with the three tokens, exactly as the app would.
   - **Cases:**
     - **hub-a power-off:**
       1. Zabbix alerts for 6 customers.
       2. Exactly one incident and one NOC Engineer page listing all 6, plus a NOC Manager page for gold down.
       3. The NOC Engineer triages and acknowledges; Zabbix shows the acknowledgment by `noc-engineer`.
       4. A field ticket is approved through the operator API and moves dispatched → on site → repaired.
       5. One recovered page, then the NOC Engineer closes the incident.
     - **Drift (`vcid-typo` or `fake-redundancy`):**
       1. The customer is degraded and the NOC Engineer escalates.
       2. The Network Engineer proposes `apply_intent` and the NOC Manager recommends it.
       3. Approved → applied → customer OK → recovered page.
     - **AC fault:** `linkDown` → the customer is degraded → page.
     - **Escalation step:** with a short `{$NETSIM.ESCALATE.AFTER}`, the NOC Manager is paged.
     - **Ask the NOC:** the NOC Manager is paged and the answer shows.
     - **Refusals:** a wrong-role tool is refused; maintenance mode sends no pages.
   - **Timing:** measure, then pin in §9.5:
     - fault → NOC Engineer page sent (target ≤ 30 s);
     - AC fault → interface problem ≤ 10 s.
   - **Contract §12:** `make e2e` no longer needs the rules brain; it uses the sink and the scripted player.

7. **Docs.**
   - **Contract:** change-log entries, plus a new §9.6 "Alerting: customer nodes → grok-bot → roles". Also §8,
     §9.1, §9.5, §11 (new tokens) and §12.
   - **Other docs:** `bot/README.md` rewritten for the three roles; `docs/RUNBOOK.md` (pages, the tunnel,
     muting); `.env.example`.
   - **Report** `docs/reports/09-noc-roles.md`: what was built, the deviations, the cross-component edits, the
     timings, and what is still unverified in the Grok Bot app.

## Acceptance criteria
- **Clean build:** `make env net build up seed e2e` passes from a clean state, with the timing table in the report.
  No xAI key is needed anywhere.
- **The room:** shows exactly three robots.
- **Alert storm:** a hub-a power-off gives exactly one NOC Engineer page listing every affected customer, within
  budget. Zabbix shows the acknowledgment by `noc-engineer`.
- **AC fault:** gives a `linkDown` trap, a re-poll, and the interface problem in ≤ 10 s.
- **Writes:** no role can approve or apply. Acknowledgments are the only write outside change tickets. The direct
  reads reject anything that writes (tests prove it).
- **Removed settings:** they are gone from `.env.example`. `make env` never touches the user's existing values, so
  the old ones may stay harmlessly in `.env`; the report says so.

## Working rules
- The decisions above are fixed; ask the user before changing any of them.
- Verify Zabbix 8.0.0rc1 behaviour on the running server (service-event macros, acknowledging service events,
  severity updates) before relying on it, and record what you find in the report.
- Role tokens are the only secrets that leave this PC. lab-api keeps the Zabbix, NetBox and lab tokens.
- After any bot change, rebuild the container: `docker compose build grok-bot && docker compose up -d grok-bot`.
- Finish with a full `make e2e` and fix every failure before calling it done.
- Commit only when the user asks.
