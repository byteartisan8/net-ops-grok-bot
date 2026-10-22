# The Grok Bot app in the NetSim NOC: three roles

The NOC room (<http://127.0.0.1:8095>) is the workplace of three colleagues. Each one is a **Bot of your Grok Bot app**
(`/opt/Grok Bot`, SpaceXAI's cloud agent) with one routine that Zabbix wakes up through grok-bot. The app thinks on
its own cloud PC and works in the lab only through grok-bot's tools (`/mcp`).

| Role | Robot | Paged for | Its connector | Its skills | `.env` |
|---|---|---|---|---|---|
| NOC Engineer | NOCENG | every new incident, a customer getting worse, all recovered | `netsim-noc-noceng` | [`SKILL.md`](SKILL.md) + [`skills/noc-engineer`](skills/noc-engineer/SKILL.md) | `BOT_NOCENG_TOKEN`, `BOT_ROUTINE_NOCENG_URL`, `_KEY` |
| Network Engineer | NETENG | an escalation to it | `netsim-noc-neteng` | [`SKILL.md`](SKILL.md) + [`skills/network-engineer`](skills/network-engineer/SKILL.md) | `BOT_NETENG_TOKEN`, `BOT_ROUTINE_NETENG_URL`, `_KEY` |
| NOC Manager | NOCMGR | a gold customer down, Zabbix's escalation step, an escalation, change tickets (batched), Ask the NOC | `netsim-noc-nocmgr` | [`SKILL.md`](SKILL.md) + [`skills/noc-manager`](skills/noc-manager/SKILL.md) | `BOT_NOCMGR_TOKEN`, `BOT_ROUTINE_NOCMGR_URL`, `_KEY` |

```
fault on a router -> trap -> Zabbix re-polls (<= 5 s) -> customer node Warning (degraded) or Disaster (down)
  -> Zabbix's service action -> POST grok-bot /hooks/zabbix (labnet, no internet)
     -> grok-bot batches (~10 s) -> one incident, one page -> the NOC Engineer's routine (HTTPS, the app's cloud)
        -> the Bot works through /mcp (tunnel, its role token): triage, ack, field ticket, escalate
           -> the human approves in the NOC room -> grok-bot applies and verifies -> Zabbix OK -> "recovered" page
```

**The rules that do not bend:**
- **No role can approve or apply.** Every change is a lab-api dry run that a human approves in the NOC room
  (operator desk) or with `uv run botctl approve CHG-xxxx`. A role token is not the operator token.
- **The only direct write** is the NOC Engineer's Zabbix acknowledgment, made as the Zabbix user `noc-engineer`.
- **The role tokens are the only secrets that leave this computer.** lab-api keeps the Zabbix, NetBox and lab
  tokens; the roles' Zabbix, NetBox and router reads go through lab-api's allow-listed read endpoints.

[`DEMOS.md`](DEMOS.md) has five demos with what to run and what to show.

## What the app offers (from its UI text, not tried in your account)

Read from the UI strings of `/opt/Grok Bot/resources/app.asar` (grok-bot-0.66.0) on 2026-10-20 and 2026-10-21:

| Feature | UI text | Used for |
|---|---|---|
| Several Bots | "Create new Bot", "Type a name to create a Bot" | one Bot per role |
| Custom MCP servers, **per Bot** | "Add a custom MCP server for everyone using this Bot", "MCP server JSON", "HTTP headers" | the role's connector |
| Skills, per Bot | "Skills teach your Bot how to work so it can follow your team's workflows and standards", "Add new skill" | the shared reference + the role's skill |
| Routines, per Bot | "Routines are recurring tasks this Bot runs on a schedule. Ask it in chat to set one up." | the role's routine |
| Webhook trigger | "When a webhook fires"; "Webhook URL": "The address an outside service sends its request to. Each request runs this routine once."; "Webhook key": "A secret the sender includes so Grok Bot knows the request really came from them." | the pages |
| Routines pause themselves | "Routines paused while you were away": routines whose results nobody read are paused "to avoid wasted spend" | check before a demo |
| Commands on your computer | "Run a command on your local computer", "Allow … to run a command on your computer?" | `botctl call` without a tunnel |
| Slack, Egress | workspace install; "Route traffic through this computer" | optional; Egress untested |

**Connectors are per Bot**, not app-wide and not per routine. So each role is its own Bot, holding only its own
connector: the NOC Engineer's routine cannot reach the NOC Manager's tools. If you must use one Bot, it holds all
three connectors (named per role, as `botctl mcp-config` names them) and each routine's prompt must say "use only the
`netsim-noc-noceng` tools" (or its own): the tokens still enforce each role's tools, but nothing then stops a routine
from picking another role's connector. Your plan or team settings may switch any of this off.

## 1. The NOC side

```bash
make env
```

`make env` adds the new settings to an existing `.env` (the three role tokens, `BOT_ZABBIX_HOOK_TOKEN`, the routines'
empty URLs and keys) and never changes values already there. Old settings of the in-house crew (`XAI_API_KEY`,
`BOT_BRAIN`, `BOT_PAGER_*`, `BOT_AGENT_TOKEN`, ...) may stay in `.env`: nothing reads them any more.

```bash
make up && make seed
```

The Zabbix bootstrap creates the role users `noc-engineer`, `network-engineer` and `noc-manager`, the media type
`NetSim NOC (grok-bot)` and the service action `NetSim: customer alert` (contract §9.6).

```bash
uv run botctl status
```

```bash
uv run botctl tools --role noc-engineer
```

```bash
uv run botctl call --role noc-engineer get_noc_status
```

**Try the office without the app:** the routine sink records the pages the routines would get.

```bash
make bot-sink-up
```

```bash
uv run labctl device stop hub-a
```

```bash
uv run botctl sink show
```

One page to `noc-engineer` lists the six customers. `make bot-sink-down` sends pages to the routines again, and
`uv run labctl reset` brings hub-a back.

## 2. The tunnel: /mcp for the app's cloud PC

grok-bot listens on `127.0.0.1:8095`; the app's computer is in the cloud. `/mcp` needs a public HTTPS URL.

**A quick tunnel** (needs [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)):
1. A quick tunnel's URL changes on every start, so allow every quick-tunnel host: put
   `BOT_MCP_HOSTS=*.trycloudflare.com` in `.env`, then restart grok-bot (the host list is read at start):

   ```bash
   docker compose up -d --force-recreate grok-bot
   ```

2. Start the tunnel. It prints the URL and the three connector configs, and runs until Ctrl-C:

   ```bash
   uv run botctl tunnel
   ```

3. After every restart of the tunnel, update the URL in the three Bots' connectors.

**A named tunnel** keeps one host name, but needs a domain of yours on Cloudflare (`cloudflared tunnel create`,
`cloudflared tunnel route dns`); then `BOT_MCP_HOSTS=<that host>`.

Requests for a `BOT_MCP_HOSTS` host reach **only** `/mcp`: not the NOC page, not the operator API, not
`/hooks/zabbix`. Rotate the role tokens (new values in `.env`, read live, then update the connectors) when a demo is
over.

## 3. Three Bots, three connectors, four skills

For each role, in the app:
1. **Create a new Bot:** "NetSim NOC Engineer", "NetSim Network Engineer", "NetSim NOC Manager".
2. **Add a custom MCP** with the role's "MCP server JSON". Print it with the tunnel's URL:

   ```bash
   uv run botctl mcp-config --role noc-engineer --url https://<tunnel-host>/mcp --show-token
   ```

   It prints `{"netsim-noc-noceng": {"url": ..., "headers": {"Authorization": "Bearer ..."}}}`. Treat the token
   like a password. Use `--role network-engineer` and `--role noc-manager` for the other two Bots.
3. **Add two skills:** the shared reference [`SKILL.md`](SKILL.md) (`netsim-noc`) and the role's own skill from
   [`skills/`](skills/).
4. Check it in the Bot's chat: "Call get_noc_status and tell me which role you are."

## 4. Three routines: let Zabbix wake them

In each Bot, ask in chat to set up a routine that runs **when a webhook fires**, with the prompt below. Saving it
gives a **webhook URL** and a **webhook key**. Put them in `.env` (read live, no restart):

```
BOT_ROUTINE_NOCENG_URL=https://...the NOC Engineer routine's webhook URL...
BOT_ROUTINE_NOCENG_KEY=...its webhook key...      # with or without "Bearer ": grok-bot sends "Authorization: Bearer <key>"
BOT_ROUTINE_NETENG_URL=...
BOT_ROUTINE_NETENG_KEY=...
BOT_ROUTINE_NOCMGR_URL=...
BOT_ROUTINE_NOCMGR_KEY=...
```

Then send each one a test page and read the delivery log:

```bash
uv run botctl page test noc-engineer
```

```bash
uv run botctl page status
```

**Routine prompts** (one per Bot):

- **NOC Engineer:**
  > You are the NetSim NOC Engineer (skills: netsim-noc, noc-engineer). The webhook body is a page from the NOC. If
  > `event` is `test`, answer "page received" and stop. Otherwise follow `how_to_respond` and your skill: for
  > `incident.opened` take the incident, look, ack it in Zabbix, file your triage, then propose a field repair or
  > escalate; for `incident.recovered` verify and close it. Never approve anything. End with a three-line summary.
- **Network Engineer:**
  > You are the NetSim Network Engineer (skills: netsim-noc, network-engineer). The webhook body is a page from the
  > NOC. If `event` is `test`, answer "page received" and stop. For `incident.escalated`, read the note and the
  > triage, find the root cause from evidence, submit_root_cause, then propose the fix, one ticket per host, with the
  > incident id. Never approve anything. End with the root cause and the ticket ids.
- **NOC Manager:**
  > You are the NetSim NOC Manager (skills: netsim-noc, noc-manager). The webhook body is a page from the NOC. If
  > `event` is `test`, answer "page received" and stop. Follow `how_to_respond` and your skill: priorities and
  > customer updates for incidents, recommend_ticket on each ticket of `tickets.filed`, answer_question for
  > `question`. A recommendation is not an approval: never approve anything. End with what you decided.
- **Optional, NOC Manager, daily at the end of the shift** (trigger "every day"):
  > You are the NetSim NOC Manager (skills: netsim-noc, noc-manager). Write today's shift report with
  > write_shift_report: incidents opened and closed, customers hit by tier, what fixed them, what is still open.

**Let the routines' tools run without asking.** The app asks before every MCP tool that is not marked read-only,
and a routine run waits on that approval card. A waiting NOC Manager recommendation reaches the operator desk after
you approved the ticket (it then shows "arrived after the ticket was applied"). In each Bot's connector settings, set
its role's tools to **Always allow** (for example `recommend_ticket`, `set_priority`, `write_customer_update`). No role
tool can approve or apply anything: approval stays with you in the NOC room.

**What a page carries:** see [`SKILL.md`](SKILL.md#how-you-are-paged). **Each page starts a paid routine run**, so:
- a fault is one page: the alerts that arrive together (about 10 s, at most 30 s) become one incident;
- later alerts of the same fault join the incident without a page; a customer going from degraded to down sends one
  update page, and the end of the fault one "recovered" page;
- at most `BOT_PAGE_MAX_PER_HOUR` (12) pages per role and hour; 3 tries per page, none after a 4xx;
- **no page** in maintenance mode (`uv run botctl pause` / `resume`) or while muted (`uv run botctl mute` /
  `unmute`): the alerts are only recorded. Both e2e suites use one or the other.

Zabbix's escalation step (`{$NETSIM.ESCALATE.AFTER}`, 10 min) pages the NOC Manager when a customer is still not OK.
Tune it in Zabbix (Administration → Macros); the bootstrap does not overwrite your value.

## 5. Slack (optional)

Connect a Bot to a Slack workspace in its settings (an admin may need to approve it), then add "post the summary to
#noc" to its routine prompt. The links in a page point at `127.0.0.1`: they open only on the machine running the lab.

## 6. No tunnel: commands on your computer

A Bot can run commands on your computer, with your permission per command. Tell it where the repository is; it then
calls the same tools as its role:

```bash
uv run botctl call --role noc-engineer get_open_problems
```

```bash
uv run botctl call --role network-engineer get_drift
```

`-a key=value` works too: `uv run botctl call --role noc-engineer get_device_state -a host=spoke-1`. Each call prints
JSON and exits 1 when the tool reports an error. Routines still need their webhook to be woken up.

## Verified, and not verified

**Verified on this computer** (`make bot-e2e`, no cloud): Zabbix's service action → `/hooks/zabbix` → batching →
pages to the three routine URLs (the routine sink stands in for them), and a scripted role player that calls `/mcp`
with the three tokens exactly as the app does (`initialize`, `tools/list`, `tools/call`): the alert storm, drift, an
AC fault, a gold customer down with Zabbix's escalation step, Ask the NOC, maintenance and mute, the refusals.

**Read from the app's UI text, not tried in your account:** several Bots; MCP servers, skills and routines per Bot;
the webhook trigger with URL and key; routines pausing themselves; commands on your computer; Slack; Egress.

**Not verified at all:** that a routine run can call its Bot's MCP connector; how the app hands the webhook's JSON
body to the routine; that the webhook key is accepted as `Authorization: Bearer <key>` (Cursor's automations take
`Bearer crsr_...`); the app's own rate limits; a quick tunnel staying up for a long demo. Forum threads from 2026
report service-side 401 `ERROR_NOT_LOGGED_IN` answers from Cursor's automation webhooks even with a fresh key: if
`botctl page test` shows that, it is not this lab.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `botctl call` says `BOT_NOCENG_TOKEN is not set` | run `make env` |
| `/mcp` answers 404 `the MCP endpoint is off` | no role token in `.env` |
| 401 `missing or wrong Authorization` | the connector's header must be `Authorization: Bearer <the role's token>`; the operator token is not accepted |
| 400 `Invalid host header` through the tunnel | add the tunnel's host (or `*.trycloudflare.com`) to `BOT_MCP_HOSTS` and recreate grok-bot |
| a tool answers "belongs to the NOC Engineer ..." | the Bot uses another role's connector: each Bot gets its own |
| `propose_*` missing from the tool list | autonomy is `observe` (`uv run botctl autonomy propose`) |
| no page arrives | `uv run botctl page status`: routine URL empty, maintenance or mute on, hourly cap, or the delivery failed (HTTP status shown) |
| pages arrive but nothing happens | the routine is paused in the app (routines pause when nobody reads their results) |
| Zabbix shows no alert to grok-bot | Zabbix → Reports → Action log; `BOT_ZABBIX_HOOK_TOKEN` must match the secret macro (re-run `make zabbix-bootstrap`) |
