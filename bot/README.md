# bot/ — grok-bot: the NOC office of three roles and the NOC room (owner: session 07-grok-bot; three roles since 09-noc-roles)

The lab's network operations center is the workplace of three colleagues: the **NOC Engineer** (NOCENG), the
**Network Engineer** (NETENG) and the **NOC Manager** (NOCMGR). Each one is a routine of your **Grok Bot app**: it
thinks on the app's cloud PC and works in the lab only through grok-bot's tools (`/mcp`). **Zabbix, not a bot,
decides when they are paged:** its service action posts customer alerts to grok-bot, which batches them into
incidents and pages the roles' routines. Every change is a ticket that a human approves; grok-bot then applies and
verifies it. The **NOC room** at <http://127.0.0.1:8095> shows the three at work as pixel-art robots, with a wall
screen of the Zabbix services. Reports: [`docs/reports/09-noc-roles.md`](../docs/reports/09-noc-roles.md) (and
[07](../docs/reports/07-grok-bot.md), [08](../docs/reports/08-grokbot-app.md) for the history). Setting up the app's
side: [`grokbot-app/`](grokbot-app/README.md).

```
AC or PW fault -> trap (linkDown, cpwVcDown) -> Zabbix re-polls the router -> customer node Warning or Disaster
  -> Zabbix service action "NetSim: customer alert" -> POST grok-bot /hooks/zabbix
     -> batch (BOT_BATCH_S 10 s, at most BOT_MAX_BATCH_S 30 s) -> one incident, one page -> the NOC Engineer
        -> /mcp: take, ack in Zabbix, triage, field ticket or escalate -> Network Engineer / NOC Manager
           -> a human approves -> grok-bot applies + verifies through lab-api -> Zabbix OK -> "recovered" page
```

```
bot/
  src/netsim_bot/       the netsim-bot package (uv workspace member; talks HTTP only: lab-api and the routines)
    engine.py           the office: alert intake, batches, incidents, pages, the roles' work, tickets, executor,
                        field repairs, Ask the NOC, the lab feed for the room
    alerts.py           Zabbix's customer alerts (the media type's JSON) -> Alert
    incidents.py        batches of alerts -> incidents (new, same fault, worse, recovered, Zabbix escalation)
    paging.py           pages to the roles' routines: hourly cap, 3 tries, delivery log, the test sink
    roles.py            the three roles on /mcp: tool sets, refusals, instructions, the robots' presence
    tools.py            lab reads, direct reads (Zabbix, NetBox, RESTCONF via lab-api), change proposals
    labapi.py           lab-api client (writes carry X-Lab-Token and X-Lab-Actor grok-bot/<ROLE>)
    store.py, bus.py    SQLite (alerts, incidents, tickets, pages, activity, chat), event bus (SSE)
    sink.py             grok-bot-sink: the routine sink (tests and dry runs)
    api/                the web server: NOC page, REST API, event stream, /hooks/zabbix, /mcp
    web/                the NOC room: index.html, css/, js/, img/ (canvas, no build step, no external files)
    cli.py              botctl
  tests/                unit tests (recorded lab-api data in tests/data/; part of `make test`)
  e2e/                  end-to-end on the running lab with the routine sink and a scripted role player (`make bot-e2e`)
  grokbot-app/          the Grok Bot app's side: setup, the shared skill, the three role skills, demos
  Dockerfile, compose.yaml, bot.mk
```

## Start it

```bash
make bot-up
```

`make up` starts it too; then open <http://127.0.0.1:8095>. Without routines nobody is paged (each page is logged as
skipped). To watch the office without the app, send the pages to the routine sink and play the roles yourself:

```bash
make bot-sink-up
```

```bash
uv run labctl device stop hub-a
```

```bash
uv run botctl sink show
```

```bash
uv run botctl call --role noc-engineer take_incident -a incident_id=INC-0001
```

## The three roles

| Robot | Role | Sits | Owns |
|---|---|---|---|
| NOCENG | NOC Engineer (first responder) | the Zabbix console desk, with a desk phone | `take_incident`, `ack_zabbix_problems` (the only direct write: Zabbix shows the user `noc-engineer`), `submit_triage`, `propose_field_repair`, `escalate` (to Network Engineer or NOC Manager), `close_incident` (only when Zabbix shows every customer OK) |
| NETENG | Network Engineer | between the server rack and the NetBox cabinet | `submit_root_cause`, `propose_apply_intent`, `propose_config_patch`, `propose_field_repair`, `escalate` (to NOC Manager) |
| NOCMGR | NOC Manager | the desk by the incident board | `set_priority` (P1-P4), `recommend_ticket` (approve or reject with a reason, shown on the ticket; never an approval), `write_customer_update`, `write_shift_report`, `answer_question` |

Every role also gets the lab reads (`get_open_problems`, `list_services`, `get_service`, `get_topology`,
`list_devices`, `get_device_state`, `get_device_running_config`, `get_device_intent`, `get_drift`, `get_impact`,
`get_events`), the direct reads (`zabbix_get`, `netbox_get`, `device_restconf_get`: lab-api's allow-listed read
endpoints, nothing that writes) and the NOC tools (`get_noc_status`, `list_incidents`, `get_incident`,
`get_proposal`, `wait_for_proposal`, `add_incident_note`). A tool outside the caller's set is refused with the name of
the role that has it. **No role can approve or apply.** Under `BOT_AUTONOMY=observe` the `propose_*` tools are gone.

**In the room** the robots stay at their desks between runs, get up on their first tool call and walk to each tool's
station: Zabbix console (problems, events, `zabbix_get`, acks), wall screen (services), NetBox cabinet (intent),
server rack (device state, RESTCONF), whiteboard (impact), desk (drift, triage, root cause, reports), printer →
operator desk (tickets), incident board (incidents, notes, escalations). Above a robot, a speech bubble shows what
its role says (triage, escalation, root cause, priority, customer update, closing summary) for long enough to read,
and a caption under it the tool being called, then its result. A ringing phone means a page arrived. The
operator desk's lamp blinks while tickets wait for you. The board's columns are NEW, WORK, YOU (a ticket waits for a
human), FIX, OK (recovered: verify and close) and DONE. Field repairs happen off-screen: the ticket and the board
show dispatched → on site → repaired.

## Alerts, incidents and pages

- **Intake:** `POST /hooks/zabbix` with `Authorization: Bearer <BOT_ZABBIX_HOOK_TOKEN>` (from Zabbix on labnet; never
  through a `BOT_MCP_HOSTS` host). Every alert is stored (`GET /api/alerts`).
- **Batching:** per role, a batch closes `BOT_BATCH_S` after its latest alert, at most `BOT_MAX_BATCH_S` after its
  first. Change tickets are batched the same way for the NOC Manager.
- **New incident:** a NOC Engineer batch opens one incident listing every alerted customer (tier, degraded or down,
  Zabbix event, services, hosts, the trigger problems behind them) and sends one page. A gold customer down also
  pages the NOC Manager.
- **Same fault:** later alerts whose customers share hosts or services with an open incident join it, without a page.
- **Worse:** a customer going from degraded to down (Zabbix: an update of the open event) sends one update page.
- **Recovered:** when every customer of an incident is OK, one "recovered" page to the NOC Engineer, who verifies and
  closes the incident.
- **Zabbix's escalation step** (`{$NETSIM.ESCALATE.AFTER}`, 10 min, still not OK): the NOC Manager is paged.
- **No page:** problems that touch no customer (an interface alone, an `unexpected` PW) stay on the wall screen; in
  maintenance mode or with pages muted, alerts are only recorded (no incident).
- **Delivery:** `BOT_ROUTINE_<ROLE>_URL` with `Authorization: Bearer <BOT_ROUTINE_<ROLE>_KEY>`; at most
  `BOT_PAGE_MAX_PER_HOUR` per role; 3 tries, none after a 4xx; every page in the delivery log (`GET /api/pages`).

## Change tickets

A role's `propose_apply_intent` / `propose_config_patch` makes a lab-api dry run (diff, validation, affected
services, predicted drift, `plan_id`); `propose_field_repair` creates a field ticket. A human approves in the room or
with `botctl approve`. The executor runs approved tickets one at a time; tickets approved together run make before
break (field repairs, then pure additions, then changes, then pure removals). lab-api applies exactly the approved
`plan_id` (a changed device or NetBox → HTTP 409 → the ticket returns to pending with the new plan) and verifies
through simd and Zabbix. The audit actor is `grok-bot/<ROLE> approved-by:<who>`.

## Configuration (`.env`, read live)

| Variable | Default | |
|---|---|---|
| `BOT_AUTONOMY` | `propose` | `observe` (the roles diagnose and report only) or `propose`; `auto` is gone (read as `propose`, with a warning) |
| `BOT_OPERATOR_TOKEN` | generated | approvals and control (`X-Operator-Token`) |
| `BOT_ZABBIX_HOOK_TOKEN` | generated | `/hooks/zabbix` (the Zabbix bootstrap copies it into a secret macro) |
| `BOT_NOCENG_TOKEN`, `BOT_NETENG_TOKEN`, `BOT_NOCMGR_TOKEN` | generated | the roles on `/mcp` (`Authorization: Bearer`); the only secrets that leave this computer |
| `BOT_ROUTINE_NOCENG_URL`, `_KEY` (and `NETENG`, `NOCMGR`) | empty | the routines' webhooks; empty URL: that role is not paged |
| `BOT_MCP_HOSTS` | empty | tunnel host names that may reach `/mcp` and nothing else (`*.trycloudflare.com` allowed; read at start) |
| `BOT_BATCH_S`, `BOT_MAX_BATCH_S` | 10, 30 | batching |
| `BOT_PAGE_MAX_PER_HOUR` | 12 | per role |
| `BOT_FIELD_DELAY_S` | 10 | field repair: dispatched → on site |
| `BOT_ASK_TIMEOUT_S` | 600 | an unanswered Ask-the-NOC question times out |
| `BOT_POLL_S` | 2 | the lab feed for the room (wall screen, rack LEDs) |
| `BOT_PUBLIC_URL` | `http://127.0.0.1:8095` | the NOC room's address in pages |

The settings of the in-house crew (`XAI_API_KEY`, `BOT_BRAIN`, `BOT_DAILY_BUDGET_USD`, `BOT_MAX_STEPS`,
`BOT_SETTLE_S`, `BOT_MAX_SETTLE_S`, `BOT_AGENT_TOKEN`, `BOT_AGENT_NAME`, `BOT_PAGER_*`) are gone. `make env` never
removes values from an existing `.env`, so they may stay there: nothing reads them.

## botctl

```bash
uv run botctl status                    # roles, open incidents, tickets waiting for a human, batches, pages
uv run botctl incidents [--all]
uv run botctl show INC-0003             # customers, triage, root cause, tickets, pages, timeline
uv run botctl approve CHG-0007          # or --incident INC-0003 for every pending ticket of it
uv run botctl reject CHG-0007 --comment "not in business hours"
uv run botctl ask "what happens if hub-c goes down for maintenance?"   # pages the NOC Manager (one run)
uv run botctl watch                     # live activity
uv run botctl pause --reason "planned work" ; uv run botctl resume     # maintenance mode
uv run botctl mute ; uv run botctl unmute
uv run botctl autonomy observe|propose|default
uv run botctl page status ; uv run botctl page test noc-engineer
uv run botctl sink on|off|show          # the routine sink (make bot-sink-up / bot-sink-down)
uv run botctl tools --role network-engineer
uv run botctl call --role noc-engineer get_impact '{"host": "hub-c"}'
uv run botctl mcp-config --role noc-manager --url https://<tunnel>/mcp [--show-token]
uv run botctl tunnel                    # Cloudflare quick tunnel (cloudflared) + the three connector configs
```

## API

`GET /api/state`, `/api/stream` (SSE: hello, lab, incident, proposal, activity, roles, control, chat, alert, batches,
page, shift_report), `/api/incidents[/{id}]`, `/api/proposals[/{id}]`, `/api/alerts`, `/api/pages[/{id}]`,
`/api/activities`, `/api/chat`, `/api/shift-reports`, `/api/health`; writes with `X-Operator-Token`:
`POST /api/proposals/{id}/approve|reject`, `/api/incidents/{id}/approve-all`, `/api/chat`, `/api/control` (`paused`,
`pages_muted`, `autonomy`, `sink_url`), `/api/pages/test/{role}`. OpenAPI at <http://127.0.0.1:8095/api/docs>.
Requests whose `Host` is not in `BOT_ALLOWED_HOSTS` are refused (DNS rebinding).

`POST /hooks/zabbix`: Zabbix's customer alerts (contract §9.6), `Authorization: Bearer <BOT_ZABBIX_HOOK_TOKEN>`.

`POST /mcp`: MCP Streamable HTTP, stateless JSON (protocol 2025-11-25, 2025-06-18, 2025-03-26; `initialize`, `ping`,
`tools/list`, `tools/call`), `Authorization: Bearer <a role token>`: the token decides the role and its tools. A
foreign `Origin` is refused.

## Tests

```bash
make bot-test     # unit: alerts and batching, incidents, paging, the roles' tools and refusals, settings, API,
                  # /mcp, /hooks/zabbix, the routine sink, the page's assets
make bot-e2e      # live lab, no cloud (~10 min, part of make e2e): the routine sink stands in for the three
                  # routines, a scripted role player drives /mcp with the three tokens. hub-a power-off (one
                  # incident, one page for six customers, ack by noc-engineer, field ticket, recovered, closed),
                  # fake redundancy (escalation, root cause, three tickets make before break), an AC fault caught
                  # by linkDown, gold down + Zabbix's escalation step, Ask the NOC, maintenance and mute, refusals
```
