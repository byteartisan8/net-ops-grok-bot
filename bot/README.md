# bot/ — grok-bot: the NOC bot and the NOC room (owner: session 07-grok-bot)

A crew of bots that runs the lab's network operations center: it notices Zabbix problems on its own, investigates
them with Grok (xAI API, function calling on lab-api), weighs the impact by SLA tier, proposes the fix, and, once a
human approves, applies it and verifies it through simd and Zabbix. The **NOC room** at <http://127.0.0.1:8095>
shows the crew at work as pixel-art robots, with a wall screen of the Zabbix services. Reports:
[`docs/reports/07-grok-bot.md`](../docs/reports/07-grok-bot.md),
[`docs/reports/08-grokbot-app.md`](../docs/reports/08-grokbot-app.md).

**Outside agents** (the Grok Bot app, any MCP client) work beside the crew through `/mcp` or `botctl call`: the same
reads and proposals, never an approval. They get paged per incident by webhook, and appear in the room as the visitor
**GROK**. Setup, skill and demo scripts: [`grokbot-app/`](grokbot-app/README.md).

```
bot/
  src/netsim_bot/       the netsim-bot package (uv workspace member; talks HTTP only: lab-api and api.x.ai)
    engine.py           watcher loop, crew, investigations, proposals, approvals, change executor
    incidents.py        Zabbix problem snapshots -> incidents (settle window, joining, resolution)
    brains.py           Grok function-calling loop, and the deterministic rules triage
    tools.py            the responders' tools (lab-api reads, proposals, the report), with their room stations
    agent.py            outside agents: their tools (crew reads + proposals + NOC tools) and the visitor robot
    pager.py            one webhook per incident event (e.g. a Grok Bot routine)
    prompts.py          system prompts and incident briefs
    grok.py             xAI Responses API client (httpx)
    labapi.py           lab-api client (writes carry X-Lab-Token and X-Lab-Actor)
    store.py, bus.py    SQLite (incidents, proposals, activity, chat), event bus (SSE)
    api/                the web server: NOC page, REST API, event stream, /mcp (MCP server for outside agents)
    web/                the NOC room: index.html, css/, js/ (canvas, no build step, no external files)
    cli.py              botctl
  tests/                unit tests (recorded lab-api data in tests/data/; part of `make test`)
  e2e/                  end-to-end on the running lab (`make bot-e2e`, part of `make e2e`); opt-in Grok eval
  grokbot-app/          the Grok Bot app in this NOC: setup (README), SKILL.md, DEMOS.md
  Dockerfile, compose.yaml, bot.mk
```

## Start it

```bash
make bot-up                     # part of `make up` too; then open http://127.0.0.1:8095
uv run labctl scenario apply hub-down   # break something and watch the crew
```

Without a key the responders run the **rules** brain (a fixed triage playbook: problems → drift → device state →
impact → proposals). For **Grok**, put an xAI API key in `.env` (create it at console.x.ai → API Keys; API usage is
billed by xAI, separately from SuperGrok/X/Grok Bot subscriptions):

```bash
XAI_API_KEY=xai-...             # in .env; read live, no restart
uv run botctl grok-check        # one function-calling round trip with the key and model
```

## The crew and the room

| Bot | Role | In the room |
|---|---|---|
| POLL | watcher: polls lab-api every `BOT_POLL_S` s (polling is truth; trap hints never open an incident), groups problems into incidents | sits at the Zabbix console, pins new incidents on the board |
| PING, TRACE (, DIG) | responders: take open incidents by severity, investigate with tools, propose fixes, file the report | walk to the station of each tool call: NetBox cabinet (intent), server rack (device state), whiteboard (blast radius), wall screen (services), desk (drift, report), printer → operator desk (change tickets) |
| PATCH | field technician: carries out approved field repairs | leaves through the door, comes back when the device is repaired |
| you | approve or reject change tickets | the operator desk: its lamp blinks while tickets wait |

The wall screen (click it to zoom) shows the Zabbix L2VPN business services per region with their SLA tier, the
topology with PW state, open problems, and the crew's status. The rack LEDs are the devices (power, uplink, PWs).
The incident board columns are NEW, WORK, YOU (needs a human), FIX, DONE. A red beacon turns when a High or
Disaster problem is open. The side panel has the incidents (the bot's report, its change tickets with the exact
diff, evidence, timeline), approvals, live activity, and **Ask the NOC** (questions answered with the same tools).

## How an incident runs

1. POLL sees new problems (scopes `l2vpn-pw`, `l2vpn-unexpected`, `interface`, `availability`), waits
   `BOT_SETTLE_S` (6 s, at most `BOT_MAX_SETTLE_S` 20 s) for the rest of the fault's problems, opens one incident.
   Problems of the same service (or host) join it later.
2. A responder investigates (Grok: up to `BOT_MAX_STEPS` model turns; tools in `tools.py`) and calls
   `finish_investigation`: title, summary, root cause, category (config-drift, intent-only, physical-fault, ...),
   affected services, impact per tier, evidence, confidence.
3. Fixes are **proposals**: `propose_apply_intent` / `propose_config_patch` make a lab-api dry run (diff,
   validation, affected services, predicted drift, `plan_id`); `propose_field_repair` creates a field ticket. The
   model has no tool that applies anything and never sees a write token.
4. A human approves (page, `botctl approve`), or `BOT_AUTONOMY=auto` does. The executor runs approved changes one at
   a time; several approved together run make-before-break (field repairs, then pure additions, then changes, then
   pure removals). lab-api confirms exactly the approved `plan_id` (a changed device or NetBox → HTTP 409 → the
   proposal returns to pending with the new plan) and verifies through simd and Zabbix. The audit actor is
   `grok-bot/<bot> approved-by:<who>`.
5. The incident resolves when every problem has been gone for `BOT_RESOLVE_HOLD_S` (8 s). Pending proposals of a
   resolved incident become obsolete. Problems that persist 120 s after a fix trigger a follow-up investigation.

## Configuration (`.env`, read live)

| Variable | Default | |
|---|---|---|
| `XAI_API_KEY` | empty | empty: rules brain |
| `GROK_MODEL` | `grok-4.7` | any model of docs.x.ai/developers/models; priced from `pricing.py` |
| `GROK_REASONING_EFFORT` | empty | `low`, `medium`, `high`, `xhigh`; empty = the model's default |
| `BOT_BRAIN` | `auto` | `auto` (Grok if a key is set), `grok`, `rules` |
| `BOT_AUTONOMY` | `propose` | `observe` (diagnose only), `propose`, `auto` (lab only) |
| `BOT_DAILY_BUDGET_USD` | `5` | Grok spend per UTC day; then rules |
| `BOT_OPERATOR_TOKEN` | generated | approvals and control (`X-Operator-Token`) |
| `BOT_AGENT_TOKEN` | generated | `Authorization: Bearer` for `/mcp` (outside agents); empty = `/mcp` off |
| `BOT_AGENT_NAME` | `GROK` | the visitor's name in the room (1-8 letters/digits, not a crew name) |
| `BOT_MCP_HOSTS` | empty | tunnel host names that may reach `/mcp` and nothing else (`*.example.com` allowed; read at start) |
| `BOT_PAGER_URL`, `BOT_PAGER_TOKEN` | empty | the pager's webhook and its key (sent as `Authorization: Bearer`) |
| `BOT_PAGER_EVENTS`, `BOT_PAGER_MIN_SEVERITY`, `BOT_PAGER_MAX_PER_HOUR` | `opened`, `Average`, 6 | what gets paged |
| `BOT_PUBLIC_URL` | `http://127.0.0.1:8095` | the NOC room's address in pages |
| `BOT_RESPONDERS`, `BOT_POLL_S`, `BOT_SETTLE_S`, `BOT_MAX_SETTLE_S`, `BOT_RESOLVE_HOLD_S`, `BOT_MAX_STEPS`, `BOT_FIELD_DELAY_S` | 2, 2, 6, 20, 8, 24, 6 | |

Brain and autonomy can also be switched at runtime (page header, `botctl brain|autonomy`), and maintenance mode
(`botctl pause`/`resume`) makes the crew watch without opening incidents (`make e2e`'s lab part uses it).

## botctl

```bash
uv run botctl status                    # brain, autonomy, crew, open incidents, pending approvals
uv run botctl incidents [--all]
uv run botctl show INC-0003             # report, change tickets, timeline, Grok usage
uv run botctl approve CHG-0007 | --incident INC-0003
uv run botctl reject CHG-0007 --comment "not in business hours"
uv run botctl ask "what happens if hub-c goes down for maintenance?"
uv run botctl watch                     # live activity
uv run botctl pause --reason "planned work" | resume
uv run botctl brain auto|grok|rules|default ; botctl autonomy observe|propose|auto|default
uv run botctl grok-check
uv run botctl tools                     # what an outside agent gets at /mcp
uv run botctl call get_impact '{"host": "hub-c"}'   # one tool, as the outside agent; JSON out
uv run botctl mcp-config [--url https://.../mcp] [--show-token]   # JSON for the Grok Bot app's custom MCP
uv run botctl pager status|test|mute|unmute
```

## API

`GET /api/state`, `/api/stream` (SSE: hello, lab, incident, proposal, activity, crew, control, usage, chat),
`/api/incidents[/{id}]`, `/api/proposals[/{id}]`, `/api/activities`, `/api/chat`, `/api/health`; writes with
`X-Operator-Token`: `POST /api/proposals/{id}/approve|reject`, `/api/incidents/{id}/approve-all|reinvestigate`,
`/api/chat`, `/api/control` (also `pager_muted`), `/api/grok/check`, `/api/pager/test`; `GET /api/pager` (open read:
settings without URL or key, last deliveries). OpenAPI at <http://127.0.0.1:8095/api/docs>. Requests whose `Host` is
not in `BOT_ALLOWED_HOSTS` are refused (DNS rebinding).

`POST /mcp`: MCP Streamable HTTP, stateless JSON (protocol 2025-11-25, 2025-06-18, 2025-03-26; `initialize`, `ping`,
`tools/list`, `tools/call`), `Authorization: Bearer <BOT_AGENT_TOKEN>`. A foreign `Origin` is refused. 20 tools: the
11 reads, the 3 `propose_*` (with `incident_id`; hidden under `observe`) and `get_noc_status`, `list_incidents`,
`get_incident`, `get_proposal`, `wait_for_proposal`, `add_incident_note`. An outside agent's proposal always waits
for a human, even under `BOT_AUTONOMY=auto`; proposals are serialized, so the crew and an agent filing the same change
get one ticket.

## Tests

```bash
make bot-test     # unit: Grok client, incident grouping, tools on recorded lab data, both brains, engine loop, API,
                  # /mcp, outside-agent tools, the pager
make bot-e2e      # live lab, rules brain: vcid-typo, fake-redundancy (make before break), hub-down (field repair),
                  # rejection + maintenance mode, chat, the page, an outside agent over /mcp fixing vcid-typo with
                  # a human approval (~6 min); also part of make e2e. The pager is muted while it runs.
make bot-eval     # opt-in, costs money: Grok diagnoses six scenarios; scorecard and cost; nothing is applied
```
