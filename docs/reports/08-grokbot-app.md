# Session 08 — The Grok Bot app as an outside agent (MCP, pager, visitor, demos)

**Date:** 2026-10-20. **Scope (agreed in conversation, no prompt file):**
- the user wants to demo their own **Grok Bot app** on the existing stack;
- I proposed demo uses and two building blocks: a lab-api connector (MCP) and a page on Zabbix problems;
- the user said "go ahead".

Session 07 is still uncommitted on `session-07-grok-bot`; this work is on the same branch, uncommitted. Nothing is
committed until the user asks.

**Result:**
- **Reaching the NOC:** grok-bot now serves the NOC's tools to outside agents, at `POST /mcp` (MCP) and from the
  shell with `botctl call`.
- **Paging:** it pages a webhook per incident: the Grok Bot app's routine, or anything else.
- **In the room:** the outside agent shows up as the visitor robot **GROK**.
- **Docs:** setup, a skill and eight demo scripts are in [`bot/grokbot-app/`](../../bot/grokbot-app/README.md).
- **Bug found:** the new e2e found a real race in proposal deduplication (fixed, below).
- **Checks:** see [Checks](#checks).

---

## What the Grok Bot app can do (correction to session 07)

Session 07's report said the app has "no API, webhook or event trigger". **That was wrong.** Its UI strings in
`/opt/Grok Bot/resources/app.asar` (grok-bot-0.66.0) show:

| Capability | Evidence (UI text) | Used here |
|---|---|---|
| Routines on a webhook | "When a webhook fires", "Webhook URL", "Webhook key"; code: `CreateAutomationWebhookApiKey` (Cursor automations) | the pager |
| Other routine triggers | "Every hour / day / weekday", calendar series, "On a Microsoft Teams message" | demos 3, 7, 8 |
| Custom MCP servers | "Add a custom MCP", "MCP server JSON" ("an object keyed by the server name"), "HTTP headers", "Command" | `/mcp` |
| Where a command MCP server runs | "A command runs on the box each conversation uses … Use Remote HTTPS for a server that needs secrets" | why `/mcp` needs a tunnel |
| Commands on the user's computer | "Run a command on your local computer", "Allow … to run a command on your computer?" | `botctl call` |
| Egress | "Route traffic through this computer" | noted, untested |
| Slack | workspace install, per-channel replies | demos |

None of this was tried in the user's account: plan or team settings may disable parts of it. Cursor's automation
webhooks take `Authorization: Bearer crsr_...` and an arbitrary JSON body. Forum threads from 2026 report
service-side 401 `ERROR_NOT_LOGGED_IN` failures on those webhooks. Both points come from community threads, not
official docs, and are noted in the setup guide.

## What was built

```
Grok Bot app ──(MCP over a tunnel: POST /mcp, Bearer BOT_AGENT_TOKEN)──┐
     │  or: "run a command on your computer" ─▶ uv run botctl call ─────┤
     ▲                                                                   ▼
     └──── webhook (one per incident) ◀── pager ◀── grok-bot engine ──▶ lab-api (dry runs only)
                                                     │
                                         NOC room: visitor GROK walks the stations
```

| Piece | Where | Notes |
|---|---|---|
| Agent gateway | `bot/src/netsim_bot/agent.py` | **11 reads:** the crew's own `Tool` objects and handlers. **3 `propose_*`:** with `incident_id`. **6 NOC tools:** `get_noc_status`, `list_incidents`, `get_incident`, `get_proposal`, `wait_for_proposal` (≤300 s), `add_incident_note`. Every call is an activity of the visitor. |
| MCP endpoint | `bot/src/netsim_bot/api/mcp.py` | Streamable HTTP, stateless JSON: `initialize` (negotiates 2025-11-25 / 06-18 / 03-26), `ping`, `tools/list`, `tools/call` (text + `structuredContent`, `isError`), notifications → 202, batches, `GET` → 405. Bearer auth with constant-time compare. A foreign `Origin` gets 403. Off (404) without a token. |
| Tunnel guard | `api/app.py` `McpOnlyHosts` | Hosts in `BOT_MCP_HOSTS` reach `/mcp` and nothing else, not the page or the open read API |
| Pager | `bot/src/netsim_bot/pager.py`, engine hooks | Events `opened` / `diagnosed` / `resolved`. Severity floor, hourly cap. Skipped while muted or in maintenance mode. Three tries; a 4xx is not retried. History kept in SQLite. Activity: POLL "paged GROK". |
| Visitor | engine crew, `web/js/room.js`, `sprites.js`, `screen.js`, `index.html` | Role `visitor`, dark visor. Comes in through the door on its first call, walks to each tool's station, leaves after 45 s of quiet. Listed on the wall screen only while present. Name in the legend. |
| CLI | `cli.py` | `botctl tools`, `botctl call <tool> '<json>' [-a k=v]` (speaks MCP to `/mcp`), `botctl mcp-config`, `botctl pager status|test|mute|unmute` |
| Settings | `settings.py`, `.env.example` | `BOT_AGENT_TOKEN` (generated), `BOT_AGENT_NAME`, `BOT_MCP_HOSTS`, `BOT_PAGER_*`, `BOT_PUBLIC_URL`; validated (unknown events, severities, crew names refused; a pasted `Bearer ` prefix is stripped) |
| Docs | `bot/grokbot-app/README.md`, `SKILL.md`, `DEMOS.md` | setup (two ways to connect, pager routine, Slack, troubleshooting), the skill to give the app, eight demos with prompts |

### Safety model (unchanged, extended)

- **No approvals for the agent.** The agent token is not the operator token: it cannot approve, reject or change the
  bot. The tool list has no approve tool.
- **A human approves every agent proposal**, even under `BOT_AUTONOMY=auto`: `_propose(..., allow_auto=False)`.
  Policy approval stays a crew-only lab convenience.
- **The audit actor names both sides:** `grok-bot/GROK` for dry runs, `grok-bot/GROK approved-by:<human>` for the
  change.
- **No fault injection, reset or scenario tools.** A game day (demo 6) needs the user to let the app run `labctl` on
  their computer, command by command.
- **Pages carry no secrets:** incident data and `127.0.0.1` links only. `GET /api/pager` never shows the URL or key.

## Decisions

**The bot pages, not a Zabbix action.**
- One hub power-off raises 7-14 Zabbix problems, and each page is a paid agent run.
- The watcher already turns those problems into one incident (settle window, joining by service), so paging from
  there costs one page per fault.
- It also needs no change to session 04's Zabbix provisioning.
- I had offered "a Zabbix webhook action" in conversation; this is the better form of the same idea.

**MCP at `/mcp` and the shell path, one implementation.**
- **Why two paths:** the app's command-type MCP servers run on its cloud computer, and URL servers must be reachable
  from the cloud. Without a tunnel, the dependable path today is the app running `botctl call` on the user's computer.
- **One implementation:** `botctl call` speaks MCP to `/mcp`, so both paths run the same code, auth and audit.

**The visitor joins the crew list.** The room, the wall screen, the activity panel and the audit trail all key on
crew names, so a crew member with role `visitor` got all of them for free. Only the room's comings and goings are new.

**Proposals are serialized (bug found by the new e2e).**
- **What happened:** the first live run of `test_20_outside_agent` produced two tickets for the same change
  (CHG-0014 by PING, CHG-0015 by GROK).
- **Why:** `_propose` checked for a pending duplicate *before* awaiting the lab-api dry run, so two proposers 1-2 s
  apart both passed the check. After approve-all, the second ticket hit lab-api's 409 and went back to "pending" with
  an empty plan.
- **The fix:**
  - an engine-wide `asyncio.Lock` around `_propose`, so the check and the ticket are atomic;
  - a ticket whose fresh plan has nothing left to change becomes `obsolete` instead of pending;
  - the first proposer's reason is kept when someone else's call joins the ticket.
- **The race was there before:** two crew responders could hit it too. An outside agent made it likely.
- **Test:** `test_crew_and_agent_filing_the_same_change_at_once_make_one_ticket` gives the fake dry run 50 ms of
  latency. It fails without the lock (two tickets) and passes with it.

## Tests

- **Unit, `bot/tests/test_outside_agents.py` (18):**
  - MCP off without a token; auth, and the operator token is not accepted as the agent's;
  - version negotiation, notifications (202), protocol errors, batches, `GET` 405;
  - the Origin guard and the MCP-only tunnel hosts;
  - the tool list follows autonomy;
  - the visitor arrives and leaves; tool errors come back as results;
  - an agent proposal stays pending under auto, then a human approves, it is applied with `approved-by:alice`, and
    the wait, the note and get_incident all reflect it;
  - the wait timeout;
  - the pager: one page per incident with the Bearer key and payload, plus resolved; muted, maintenance, severity and
    rate limit; 5xx retried, 4xx not, connection errors; the test endpoint;
  - settings validation; the proposal race; a stale empty plan becoming obsolete.
- **e2e, `bot/e2e/test_20_outside_agent.py` (2, live lab):**
  - the MCP handshake;
  - vcid-typo: the agent diagnoses with `get_drift` and files `propose_apply_intent` on the incident; a human approves;
    `wait_for_proposal` returns applied with verdict ok; the note shows on the incident; drift is clean.
- **e2e fixtures:** both suites now mute the pager for their run and restore it.
- **Manual live check:** a throwaway receiver (`pager-sink` on labnet, from the grok-bot image) received a test page,
  then `opened` / `diagnosed` / `resolved` for a hub-down incident, all with `Authorization: Bearer`. The agent path
  went:
  1. `botctl call get_impact`, `get_device_state`;
  2. `propose_field_repair` with the incident id, which created CHG-0013 (by GROK);
  3. a human approval;
  4. `wait_for_proposal` returned applied, "hub-b powered on".

  `.env` was restored afterwards and the receiver removed.

## Checks

- **`make test`:** 592 passed (574 before, plus 18 new), 37 s.
- **`make lint`:** clean (ruff, format, pyang, yanglint).
- **`make e2e` passed in 21 min 59 s:**
  - **bot-e2e:** 8 passed (the 6 from session 07, plus 2 new); 6 skipped, the opt-in Grok eval, as there is no xAI key.
  - **lab-e2e:** 23 of 23 passed, every timing within budget. For example: trap to check-now, median 2.71 s and
    max 5.72 s (budgets 5 and 7 s); hub-down to Zabbix status, 3.94 s (budget 12 s).
- **After the run:** grok-bot is out of maintenance mode, the pager is unmuted, and `labctl drift` reports no findings.

## What the user does next

1. **Pick a way in** ([setup guide](../../bot/grokbot-app/README.md)):
   - **commands on your computer:** works now;
   - **MCP connector:** needs a tunnel you trust and `BOT_MCP_HOSTS`;
   - either way, give the app [`SKILL.md`](../../bot/grokbot-app/SKILL.md) as a skill.
2. **For demo 1:** create the webhook routine in the app, then put its URL and key in `.env` as `BOT_PAGER_URL` and
   `BOT_PAGER_TOKEN`. Then run:

   ```bash
   uv run botctl pager test
   ```

3. **Connect Slack** in the app if the demos should post there.
4. **Rehearse** [`DEMOS.md`](../../bot/grokbot-app/DEMOS.md). Suggested story: demo 2, then 1, then 6, then 3,
   then 8.
