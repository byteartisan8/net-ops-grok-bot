# bot/ — grok-bot: the NOC office of three roles and the NOC room (owner: session 07-grok-bot; three roles since 09-noc-roles)

The lab's network operations center is the workplace of three colleagues: the **NOC Engineer** (NOCENG), the
**Network Engineer** (NETENG) and the **NOC Manager** (NOCMGR). Each one is a routine of your **Grok Bot app**: it
thinks on the app's cloud PC and works in the lab only through grok-bot's tools (`/mcp`). **Zabbix, not a bot,
decides when they are paged:** its service action posts customer alerts to grok-bot, which batches them into
incidents and pages the roles' routines. Every change is a ticket an engineer files, the NOC Manager reviews and
proposes, and a human approves; grok-bot then applies and verifies it. The **NOC room** at <http://127.0.0.1:8095>
shows the three at work as pixel-art robots, with a wall
screen of the Zabbix services. Reports: [`docs/reports/09-noc-roles.md`](../docs/reports/09-noc-roles.md) (and
[07](../docs/reports/07-grok-bot.md), [08](../docs/reports/08-grokbot-app.md) for the history). Setting up the app's
side: [`grokbot-app/`](grokbot-app/README.md).

```
AC or PW fault -> trap (linkDown, cpwVcDown) -> Zabbix re-polls the router -> customer node Warning or Disaster
  -> Zabbix service action "NetSim: customer alert" -> POST grok-bot /hooks/zabbix
     -> batch (BOT_BATCH_S 10 s, at most BOT_MAX_BATCH_S 30 s) -> one incident, one page -> the NOC Engineer
        -> /mcp: take, ack in Zabbix, triage, field ticket or escalate -> Network Engineer / NOC Manager
           -> the NOC Manager reviews each ticket: proposes it to the operator, or returns it to its engineer
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
    api/                the web server: NOC page, REST API, event stream, /hooks/zabbix, /mcp; phone.py: the
                        approvals page for the operator's phone (its own listener, :8097)
    phone.py            signing a phone in (one-time links, the phones signed in)
    web/                the NOC room: index.html, css/, js/, img/ (canvas, no build step, no external files)
    cli.py              botctl
  tests/                unit tests (recorded lab-api data in tests/data/; part of `make test`)
  e2e/                  end-to-end on the running lab with the routine sink and a scripted role player (`make bot-e2e`)
  grokbot-app/          the Grok Bot app's side: setup, the shared skill, the three role skills, demos, and
                        terminal/ (noc-controller: a role's terminal to the controller on its Bot's own computer)
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
| NOCMGR | NOC Manager | the desk by the incident board | `set_priority` (P1-P4), `review_ticket` (every change ticket: `propose` it to the operator or `return` it to its engineer, with the reason; never an approval), `write_customer_update`, `write_shift_report`, `answer_question` |

Every role also gets the lab reads (`get_open_problems`, `list_services`, `get_service`, `get_topology`,
`list_devices`, `get_device_state`, `get_device_running_config`, `get_device_intent`, `get_drift`, `get_impact`,
`get_events`), the direct reads (`zabbix_get`, `netbox_get`, `device_restconf_get`, `controller_cli`: lab-api's
allow-listed read endpoints, nothing that writes) and the NOC tools (`get_noc_status`, `list_incidents`,
`get_incident`, `get_proposal`, `wait_for_proposal`, `add_incident_note`). A tool outside the caller's set is refused
with the name of the role that has it. **No role can approve or apply.** Under `BOT_AUTONOMY=observe` the `propose_*`
tools are gone.

**`controller_cli(command)`** (session 10) runs one read in the Clixon controller's CLI (`controller/README.md`) as
the role's own controller user: `show connections [<router> [detail]]`, `show devices <router> check|diff`, `show
configuration devices device <router> config [<path>]`. lab-api refuses everything else (HTTP 403, the tool's error
lists the allowed commands) and the controller's NACM refuses the role users any edit, commit, pull or push. A router
out of sync with the controller's copy was changed outside an approved change: drift. When the controller is stopped
the tool says it is unavailable and the roles carry on without it.

**`noc-controller`** (session 11, [`grokbot-app/terminal/`](grokbot-app/terminal/README.md)) is the same read typed
in the terminal of a Bot's own computer: `noc-controller --role network-engineer show devices spoke-1 diff` prints
`lab-api@controller> show devices spoke-1 diff`, the answer and `read only · as lab-api · 0.4 s`; exit codes 0
answered, 1 refused, 2 controller unavailable, 3 cannot reach `/mcp` (or a bad token or config). It is one MCP
`tools/call` of `controller_cli` with the role's **terminal token** (`BOT_<ROLE>_TERMINAL_TOKEN`), which `/mcp` takes
for that tool only: the same allow-list, NACM, actor (`grok-bot/<ROLE>`), robot walk and CRT; the transcript marks the
call `via: terminal` ("from its computer"). `botctl terminal-setup` prints the block that installs it, pasted into the
Bot's chat; the skills use it first and `controller_cli` as the fallback.

**In the room** the robots stay at their desks between runs, get up on their first tool call and walk to each tool's
station: Zabbix console (problems, events, `zabbix_get`, acks), wall screen (services), NetBox cabinet (intent),
server rack (device state, RESTCONF), whiteboard (impact), desk (drift, triage, root cause, reports, reading a
ticket), printer → the NOC Manager's desk (a new ticket, for the review), incident board (incidents, notes,
escalations), the CONTROLLER terminal (`controller_cli`). The terminal is a CRT on its own desk: once the robot stands
there it types `<user>@controller> <command>` and shows the first lines of the answer, a refusal in red and an
unavailable controller in amber; click it for the transcript (who ran what, when, the full output) in the side panel's
Terminal tab. The NOC Manager carries a reviewed ticket to the operator desk (proposed) or back to the engineer's desk
(returned). Above a robot, a speech bubble shows what its role says (triage, escalation, root cause, priority, review,
customer update, closing summary) for long enough to read, and a caption under it the tool being called, then its
result. A ringing phone means a page arrived. The NOC Manager's desk shows the tickets waiting for its review; the
operator desk's lamp blinks while proposed tickets wait for you. The board's columns are NEW, WORK, YOU (a ticket
waits for a human), FIX, OK (recovered: verify and close) and DONE. Field repairs happen off-screen: the ticket and
the board show dispatched → on site → repaired. The rug carries the Ductus logo and the Grok Bot app's bots.

**Sound** (🔊 in the header; on unless you turned it off, and browsers start it at your first click or key press in
the page): chiptune background music in the room's pixel-art style, a calm loop while no incident is open and a tense
one (same tempo, A minor) while one is. A simple two-tone alarm rings when an incident opens, gets worse or comes back
(about 6 s, as long as the room's red flash), then once every 8 bars (about 21 s) until the incident recovers; when
Zabbix shows each of its customers OK again (or it is closed straight away), a jingle plays and the calm loop starts
again. The phone, the review and the closing keep their short beeps. One click on 🔊 turns all of it off, music
included, in every tab of the room open in that browser; 🎵 turns the music alone off and on (with the sound off,
it turns the sound on with the music). Everything is made in
the page with Web Audio (`web/js/sound.js`, no audio files); in the browser's console `__noc.sound.test('alarm')` plays
the alarm (also `'recovered'`, and `'incident'` or `'calm'` for a loop) and `__noc.sound.debug()` shows its state.

## Alerts, incidents and pages

- **Intake:** `POST /hooks/zabbix` with `Authorization: Bearer <BOT_ZABBIX_HOOK_TOKEN>` (from Zabbix on labnet; never
  through a `BOT_MCP_HOSTS` host). Every alert is stored (`GET /api/alerts`).
- **Batching:** per role, a batch closes `BOT_BATCH_S` after its latest alert, at most `BOT_MAX_BATCH_S` after its
  first. Change tickets to review are batched the same way for the NOC Manager (`tickets.filed`).
- **New incident:** a NOC Engineer batch opens one incident listing every alerted customer (tier, degraded or down,
  Zabbix event, services, hosts, the trigger problems behind them) and sends one page. A gold customer down also
  pages the NOC Manager.
- **Same fault:** later alerts whose customers share hosts or services with an open incident join it, without a page.
- **Worse:** a customer going from degraded to down (Zabbix: an update of the open event) sends one update page.
- **Recovered:** when every customer of an incident is OK, one "recovered" page to the NOC Engineer, who verifies and
  closes the incident.
- **Zabbix's escalation step** (`{$NETSIM.ESCALATE.AFTER}`, 10 min, still not OK): the NOC Manager is paged.
- **Returned ticket:** the engineer who filed it is paged (`ticket.returned`) with the ticket and the NOC Manager's
  reason.
- **No page:** problems that touch no customer (an interface alone, an `unexpected` PW) stay on the wall screen; in
  maintenance mode or with pages muted, alerts are only recorded (no incident).
- **Delivery:** `BOT_ROUTINE_<ROLE>_URL` with `Authorization: Bearer <BOT_ROUTINE_<ROLE>_KEY>`; at most
  `BOT_PAGE_MAX_PER_HOUR` per role; 3 tries, none after a 4xx; every page in the delivery log (`GET /api/pages`).

## Change tickets

An engineer's `propose_apply_intent` / `propose_config_patch` makes a lab-api dry run (diff, validation, affected
services, predicted drift, `plan_id`); `propose_field_repair` creates a field ticket. Every ticket starts in
**review**: the NOC Manager is paged (`tickets.filed`, batched) and reviews it with `review_ticket`:

| Status | Who has it | Next |
|---|---|---|
| `review` | the NOC Manager | `propose` → `pending`; `return` → `returned` (its engineer is paged with the reason) |
| `pending` | the operator (you) | approve → `approved` → `applying` → `applied` or `failed`; reject → `rejected` |
| `returned`, `rejected`, `obsolete` | nobody | the engineer files a new ticket, if any |

Nobody can approve a ticket in review. A ticket goes to the operator **without a review** (marked so on the ticket)
when the NOC Manager is not paged for it (no routine, muted, maintenance, hourly cap, delivery failed) or has not
reviewed it within `BOT_REVIEW_TIMEOUT_S`; a late review still counts until a human decided (`return` takes it back
from the operator). A human approves in the room (a ticket, or **Approve INC-xxxx** for every proposed ticket of an
incident once none is in review), with `botctl approve`, or on their phone (below). The room's Approve-incident button
sends the tickets and plans on screen; if another ticket was proposed since, nothing is approved (HTTP 409). The
executor runs approved tickets one at a time; tickets approved together run make before break (field repairs, then
pure additions, then changes, then pure removals). lab-api applies exactly the approved `plan_id` (a changed device or NetBox → HTTP 409 →
the ticket goes back to the NOC Manager's review with the new plan) and verifies through simd and Zabbix. The audit
actor is `grok-bot/<ROLE> approved-by:<who>`.

## Approvals on the operator's phone

grok-bot serves a second, small web app for the operator's phone on its own listener: container port 8097, published
on `127.0.0.1:8097` only, and put on the user's tailnet by `tailscale serve --https=8097` (never Funnel; Funnel can
only publish 443, 8443 and 10000, and grok-bot refuses whatever came through it). `BOT_PHONE_URL` is its address on the
tailnet (`https://<tailnet-host>:8097`): the NOC Manager's `review_ticket` result then carries `phone` links (the
ticket, its incident) that its routine reports in the app, and the NOC room makes QR codes with it.

- `GET /m/`: what waits for the operator, by incident (P1 first); `/m/t/<CHG>`: a ticket with **Approve** and
  **Reject**; `/m/i/<INC>`: an incident and its tickets with **Approve INC-xxxx** (make before break). A decision
  names the plan shown (a ticket with a newer plan → 409), an incident's approval the tickets shown (as above).
- **Signing in:** the operator makes a one-time link (NOC room: Approvals → **Sign in a phone**, a QR code;
  `botctl phone pair`), valid 10 minutes; the phone opens it and taps **Sign in** and gets a cookie (HttpOnly,
  SameSite=Lax, Secure over https) for `BOT_PHONE_SESSION_DAYS` (30). grok-bot keeps the token's SHA-256 only. A phone
  that signs out (on the phone, in the room, `botctl phone sign-out`) or runs out is forgotten.
- **Guards:** `Host` must be the phone URL's host or a loopback name; a POST must come from the page itself (`Origin`)
  and carry the phone's form value; `Tailscale-Funnel-Request` → 403; no role, terminal or operator token opens it;
  pages are `no-store`, cannot be framed and load nothing from elsewhere. The office's routes are not served there, and
  the office's port does not serve `/m/`.
- A decision on the phone is `phone:<name>` (the name given at sign-in, else `android`, `iphone`, ...): the ticket's
  `decided_by`, the room's activity, lab-api's audit (`approved-by:phone:<name>`).

## Configuration (`.env`, read live)

| Variable | Default | |
|---|---|---|
| `BOT_AUTONOMY` | `propose` | `observe` (the roles diagnose and report only) or `propose`; `auto` is gone (read as `propose`, with a warning) |
| `BOT_OPERATOR_TOKEN` | generated | approvals and control (`X-Operator-Token`) |
| `BOT_ZABBIX_HOOK_TOKEN` | generated | `/hooks/zabbix` (the Zabbix bootstrap copies it into a secret macro) |
| `BOT_NOCENG_TOKEN`, `BOT_NETENG_TOKEN`, `BOT_NOCMGR_TOKEN` | generated | the roles on `/mcp` (`Authorization: Bearer`): their connectors; with the terminal tokens, the only secrets that leave this computer |
| `BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`, `BOT_NOCMGR_TERMINAL_TOKEN` | generated | the roles' terminals (`noc-controller` on a Bot's computer): on `/mcp` the role, for `controller_cli` only; empty: that terminal is off. Every token must differ from every other one |
| `BOT_ROUTINE_NOCENG_URL`, `_KEY` (and `NETENG`, `NOCMGR`) | empty | the routines' webhooks; empty URL: that role is not paged |
| `BOT_MCP_HOSTS` | empty | tunnel host names that may reach `/mcp` and nothing else (`*.trycloudflare.com` allowed; read at start) |
| `BOT_BATCH_S`, `BOT_MAX_BATCH_S` | 10, 30 | batching |
| `BOT_PAGE_MAX_PER_HOUR` | 12 | per role |
| `BOT_FIELD_DELAY_S` | 10 | field repair: dispatched → on site |
| `BOT_ASK_TIMEOUT_S` | 600 | an unanswered Ask-the-NOC question times out |
| `BOT_REVIEW_TIMEOUT_S` | 300 | a ticket the NOC Manager has not reviewed by then goes to the operator without a review |
| `BOT_POLL_S` | 2 | the lab feed for the room (wall screen, rack LEDs) |
| `BOT_PUBLIC_URL` | `http://127.0.0.1:8095` | the NOC room's address in pages |
| `BOT_PHONE_URL` | empty | the phone listener's address on the tailnet (`https://<tailnet-host>:8097`, from `botctl phone setup`); empty: no phone links, the listener answers on this machine only. Not https, a path, or a Funnel port (443, 8443, 10000): a warning |
| `BOT_PHONE_SESSION_DAYS` | 30 | how long a phone stays signed in |
| `BOT_PHONE_PORT` | 8097 | the phone listener in the container (process environment, read at start; 0: off) |

The settings of the in-house crew (`XAI_API_KEY`, `BOT_BRAIN`, `BOT_DAILY_BUDGET_USD`, `BOT_MAX_STEPS`,
`BOT_SETTLE_S`, `BOT_MAX_SETTLE_S`, `BOT_AGENT_TOKEN`, `BOT_AGENT_NAME`, `BOT_PAGER_*`) are gone. `make env` never
removes values from an existing `.env`, so they may stay there: nothing reads them.

## botctl

```bash
uv run botctl status                    # roles, open incidents, tickets in review and proposed to you, batches, pages
uv run botctl incidents [--all]
uv run botctl show INC-0003             # customers, triage, root cause, tickets, pages, timeline
uv run botctl approve CHG-0007          # or --incident INC-0003 for every ticket of it the NOC Manager proposed
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
uv run botctl terminal-setup --role network-engineer --url https://<tunnel>/mcp [--show-token]   # noc-controller block
uv run botctl tunnel                    # Cloudflare quick tunnel (cloudflared) + the three connector configs
uv run botctl phone setup               # the tailscale serve line and BOT_PHONE_URL for this computer
uv run botctl phone pair [--label pixel]   # a one-time sign-in link for a phone, as a QR code in the terminal
uv run botctl phone status              # the phone listener, tailscale serve (not Funnel), through the tailnet, phones
uv run botctl phone list ; uv run botctl phone sign-out PHONE-01|--all
```

## API

`GET /api/state`, `/api/stream` (SSE: hello, lab, incident, proposal, activity, roles, control, chat, alert, batches,
page, shift_report, terminal), `/api/incidents[/{id}]`, `/api/proposals[/{id}]`, `/api/alerts`, `/api/pages[/{id}]`,
`/api/activities`, `/api/chat`, `/api/shift-reports`, `/api/terminal` (the controller terminal's transcript with the
full output; a `terminal` event carries the first 14 lines), `/api/health`; writes with `X-Operator-Token`:
`POST /api/proposals/{id}/approve|reject` (only a proposed ticket, `pending`; one in review → 409),
`/api/incidents/{id}/approve-all` (`{"tickets": {"CHG-0007": "<plan_id>", ...}}` optional: only if exactly those wait,
else 409), `/api/chat`, `/api/control` (`paused`, `pages_muted`, `autonomy`, `sink_url`), `/api/pages/test/{role}`,
`GET /api/phone` (the phone URL, the phones signed in), `POST /api/phone/pair` (`{"label": "pixel"}` optional: a
one-time sign-in link, its QR code as an SVG `data:` URI), `DELETE /api/phone/{id}` (sign a phone out).
OpenAPI at <http://127.0.0.1:8095/api/docs>. The phone's pages (`/m/...`) are on the phone listener only (above).
Requests whose `Host` is not in `BOT_ALLOWED_HOSTS` are refused (DNS rebinding).

`POST /hooks/zabbix`: Zabbix's customer alerts (contract §9.6), `Authorization: Bearer <BOT_ZABBIX_HOOK_TOKEN>`.

`POST /mcp`: MCP Streamable HTTP, stateless JSON (protocol 2025-11-25, 2025-06-18, 2025-03-26; `initialize`, `ping`,
`tools/list`, `tools/call`), `Authorization: Bearer <token>`: the token decides the role and its tools. A role token
(the Bot's connector) gets every tool of its role; a role's terminal token (`noc-controller`) gets `controller_cli`
alone (`tools/list` lists that one, any other tool is refused naming the token kind). Neither opens anything outside
`/mcp`. A foreign `Origin` is refused.

## Tests

```bash
make bot-test     # unit: alerts and batching, incidents, paging, the roles' tools and refusals, the NOC Manager's
                  # review (propose, return, not paged, timeout, a new plan), settings, API, /mcp, /hooks/zabbix,
                  # the routine sink, controller_cli and the terminal's transcript, the page's assets, the terminal
                  # tokens, terminal-setup's block, noc-controller against a fake /mcp and against grok-bot, the
                  # phone (BOT_PHONE_URL, one-time links, the guards, approve and reject with the plan shown, an
                  # incident's tickets, signing out, the NOC Manager's links, the two listeners)
make bot-e2e      # live lab, no cloud (~10 min, part of make e2e): the routine sink stands in for the three
                  # routines, a scripted role player drives /mcp with the three tokens. hub-a power-off (one
                  # incident, one page for six customers, ack by noc-engineer, field ticket reviewed and proposed,
                  # recovered, closed), fake redundancy (escalation, root cause, one ticket returned and filed again,
                  # three tickets make before break, the Network Engineer's controller check and diff), an AC fault
                  # caught by linkDown, gold down + Zabbix's escalation step, Ask the NOC, maintenance and mute,
                  # refusals (controller_cli's too), a drift case with the controller stopped, noc-controller
                  # installed from its setup block and run with the Network Engineer's terminal token, a field
                  # ticket approved on the phone listener by a phone signed in with a one-time link
```
