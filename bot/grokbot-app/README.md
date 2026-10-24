# The Grok Bot app in the NetSim NOC: three roles

The NOC room (<http://127.0.0.1:8095>) is the workplace of three colleagues. Each one is a **Bot of your Grok Bot app**
(`/opt/Grok Bot`, SpaceXAI's cloud agent) with one routine that Zabbix wakes up through grok-bot. The app thinks on
its own cloud PC and works in the lab only through grok-bot's tools (`/mcp`).

| Role | Robot | Paged for | Its connector | Its skills | `.env` |
|---|---|---|---|---|---|
| NOC Engineer | NOCENG | every new incident, a customer getting worse, all recovered, its ticket returned | `netsim-noc-noceng` | [`SKILL.md`](SKILL.md) + [`skills/noc-engineer`](skills/noc-engineer/SKILL.md) | `BOT_NOCENG_TOKEN`, `BOT_ROUTINE_NOCENG_URL`, `_KEY` |
| Network Engineer | NETENG | an escalation to it, its ticket returned | `netsim-noc-neteng` | [`SKILL.md`](SKILL.md) + [`skills/network-engineer`](skills/network-engineer/SKILL.md) | `BOT_NETENG_TOKEN`, `BOT_ROUTINE_NETENG_URL`, `_KEY` |
| NOC Manager | NOCMGR | a gold customer down, Zabbix's escalation step, an escalation, change tickets to review (batched), Ask the NOC | `netsim-noc-nocmgr` | [`SKILL.md`](SKILL.md) + [`skills/noc-manager`](skills/noc-manager/SKILL.md) | `BOT_NOCMGR_TOKEN`, `BOT_ROUTINE_NOCMGR_URL`, `_KEY` |

```
fault on a router -> trap -> Zabbix re-polls (<= 5 s) -> customer node Warning (degraded) or Disaster (down)
  -> Zabbix's service action -> POST grok-bot /hooks/zabbix (labnet, no internet)
     -> grok-bot batches (~10 s) -> one incident, one page -> the NOC Engineer's routine (HTTPS, the app's cloud)
        -> the Bot works through /mcp (tunnel, its role token): triage, ack, field ticket, escalate
           -> the NOC Manager's Bot reviews each ticket: proposes it to you, or returns it to its engineer's Bot
              -> the human approves in the NOC room or on their phone -> grok-bot applies and verifies -> Zabbix OK
                 -> "recovered" page
```

**The rules that do not bend:**
- **No role can approve or apply.** Every change is a lab-api dry run (or a field ticket) that an engineer files and
  the NOC Manager reviews; only a ticket the NOC Manager proposed can be approved, by a human in the NOC room
  (operator desk), with `uv run botctl approve CHG-xxxx`, or on their phone ([section 8](#8-approve-from-your-phone)).
  A role token is not the operator token, and no role token opens the phone's page.
- **The only direct write** is the NOC Engineer's Zabbix acknowledgment, made as the Zabbix user `noc-engineer`.
- **The role tokens and the roles' terminal tokens are the only secrets that leave this computer.** lab-api keeps
  the Zabbix, NetBox and lab tokens and the Clixon controller's SSH key; the roles' Zabbix, NetBox, router and
  controller reads go through lab-api's allow-listed read endpoints. A terminal token opens `controller_cli` and
  nothing else ([section 5](#5-the-bots-terminal-noc-controller)).
- **The controller is read only for the roles** (`controller_cli`): lab-api's allow-list decides what reaches it,
  and the controller's own NACM refuses the role users any edit, commit, pull or push.

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
`/hooks/zabbix`. Rotate the role tokens and the terminal tokens (new values in `.env`, read live, then update the
connectors and paste new terminal blocks) when a demo is over.

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
  > escalate; for `incident.recovered` verify and close it; for `ticket.returned` read the NOC Manager's reason and
  > file a corrected ticket or escalate. Never approve anything. End with a three-line summary.
- **Network Engineer:**
  > You are the NetSim Network Engineer (skills: netsim-noc, network-engineer). The webhook body is a page from the
  > NOC. If `event` is `test`, answer "page received" and stop. For `incident.escalated`, read the note and the
  > triage, find the root cause from evidence, submit_root_cause, then propose the fix, one ticket per host, with the
  > incident id; for `ticket.returned` read the NOC Manager's reason and file a corrected ticket or escalate. Never
  > approve anything. End with the root cause and the ticket ids.
- **NOC Manager:**
  > You are the NetSim NOC Manager (skills: netsim-noc, noc-manager). The webhook body is a page from the NOC. If
  > `event` is `test`, answer "page received" and stop. Follow `how_to_respond` and your skill: priorities and
  > customer updates for incidents, review_ticket on each ticket of `tickets.filed` (propose it to the operator or
  > return it to its engineer, with your reason), answer_question for `question`. A proposal is not an approval:
  > never approve anything. End with what you decided, then the phone links review_ticket gave you, if any.
- **Optional, NOC Manager, daily at the end of the shift** (trigger "every day"):
  > You are the NetSim NOC Manager (skills: netsim-noc, noc-manager). Write today's shift report with
  > write_shift_report: incidents opened and closed, customers hit by tier, what fixed them, what is still open.

**Let the routines' tools run without asking.** The app asks before every MCP tool that is not marked read-only,
and a routine run waits on that approval card. A NOC Manager review stuck on such a card keeps the ticket away from
you until `BOT_REVIEW_TIMEOUT_S` (5 min); then the ticket comes to the operator desk marked "not reviewed". In each
Bot's connector settings, set its role's tools to **Always allow** (for example `review_ticket`, `set_priority`,
`write_customer_update`). No role tool can approve or apply anything: approval stays with you, in the NOC room or on
your phone.

**After an update of grok-bot that renames a tool** (`recommend_ticket` became `review_ticket` on 2026-10-23): the
connector lists the new name on its next `tools/list`, but "Always allow" is per tool name, so set it again for the
new one, and update the skills and routine prompts that name the old one (grok-bot answers the old name with the new).

**After the controller update (session 10, 2026-10-24):** every role has a new tool, `controller_cli` (one read in
the Clixon controller's CLI: `show connections`, `show devices <router> check`, `show devices <router> diff`, `show
configuration devices device <router> config [<path>]`), and the four skills changed. In each Bot, re-add (or update)
[`SKILL.md`](SKILL.md) and its role skill, then check that `controller_cli` runs without an approval card: it is marked
read only (`readOnlyHint`), like the other reads, but if the app still asks, set it to **Always allow** (it cannot
change anything). Each call shows on the NOC room's CONTROLLER terminal; click it for the transcript.

**What a page carries:** see [`SKILL.md`](SKILL.md#how-you-are-paged). **Each page starts a paid routine run**, so:
- a fault is one page: the alerts that arrive together (about 10 s, at most 30 s) become one incident;
- later alerts of the same fault join the incident without a page; a customer going from degraded to down sends one
  update page, and the end of the fault one "recovered" page;
- at most `BOT_PAGE_MAX_PER_HOUR` (12) pages per role and hour; 3 tries per page, none after a 4xx;
- **no page** in maintenance mode (`uv run botctl pause` / `resume`) or while muted (`uv run botctl mute` /
  `unmute`): the alerts are only recorded. Both e2e suites use one or the other.

Zabbix's escalation step (`{$NETSIM.ESCALATE.AFTER}`, 10 min) pages the NOC Manager when a customer is still not OK.
Tune it in Zabbix (Administration → Macros); the bootstrap does not overwrite your value.

## 5. The Bot's terminal: `noc-controller`

Each Bot also has a computer of its own in the app's cloud, with a terminal. `noc-controller` reads the Clixon
controller from there: the Bot types a controller command on its own Linux computer and the answer comes back, while
the NOC room's CONTROLLER terminal types the same command. It is a thin client of `/mcp` and its `controller_cli` tool
([`terminal/`](terminal/README.md)): the same allow-list, NACM, audit and room, no new way into the lab. It carries the
role's **terminal token** (`BOT_<ROLE>_TERMINAL_TOKEN`), which `/mcp` takes for `controller_cli` only: the Bot can
read every file on its computer, so its role token never goes there.

For each Bot:
1. **Can its computer reach the tunnel?** Ask the Bot in its chat to run:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://<tunnel-host>/mcp
   ```

   `405` means grok-bot's `/mcp` answered (a plain GET is not allowed there). Anything else: the tunnel is down, or
   that computer has no way out (without `curl`, the smoke test of step 3 tells the same).
2. **Print the role's block,** with the URL its connector uses:

   ```bash
   uv run botctl terminal-setup --role network-engineer --url https://<tunnel-host>/mcp --show-token
   ```

   Only the block goes to stdout (notes and warnings to stderr). Treat it like a password: it holds the terminal
   token. Without `--show-token` the token is a placeholder.
3. **Paste the whole block into that Bot's chat.** The Bot runs it on its computer: it writes
   `~/.local/bin/noc-controller` and `~/.config/noc-controller/<role>.conf` (mode 600), then the smoke test
   `noc-controller --role <role> show connections`. Check the answer: `network-engineer@controller> show connections`,
   ten routers `OPEN`, then `read only · as network-engineer`. The room's CONTROLLER terminal types the same command,
   and the Terminal tab marks it "from its computer".
4. **Re-add the skills** ([`SKILL.md`](SKILL.md) and the role's): they tell the Bot to use `noc-controller` first and
   `controller_cli` when it is not installed or cannot reach the lab, and never to read its config file.

**When the URL changes** (a quick tunnel's changes at every start), print and paste the block again: it replaces the
config. **After a demo, rotate the terminal tokens** with the role tokens: new values for `BOT_NOCENG_TERMINAL_TOKEN`,
`BOT_NETENG_TERMINAL_TOKEN` and `BOT_NOCMGR_TERMINAL_TOKEN` in `.env` (grok-bot reads them live: the old ones stop
working within seconds), and new blocks before the next demo. An empty value switches that role's terminal off.

What its exit codes mean (the skills teach them): 0 answered; 1 refused by lab-api's allow-list; 2 the controller is
unavailable (carry on without it); 3 it cannot reach `/mcp`, the token is wrong or the config is (a `hint:` line says
which): then the Bot uses `controller_cli`.

**Not verified in the app:** that the Bot's computer reaches the tunnel (step 1 checks it), that it keeps files
between runs and chats (if not, the command is gone and the Bot falls back to `controller_cli`), and that it runs
commands without asking you each time. **Verified on this computer:** the block and the command in a bare
`python:3-slim` container (only `python3`) against grok-bot, on labnet and through the Tailscale Funnel's public
address ([report](../../docs/reports/11-bot-terminal.md)).

## 6. Slack (optional)

Connect a Bot to a Slack workspace in its settings (an admin may need to approve it), then add "post the summary to
#noc" to its routine prompt. The links in a page point at `127.0.0.1`: they open only on the machine running the lab.

## 7. No tunnel: commands on your computer

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

## 8. Approve from your phone

The NOC Manager's report in the app ends with links such as `Approve on your phone: [INC-0012](...) · [CHG-0031](...)`.
Tap one on your phone: the ticket opens with what changes, the services it touches, the predicted drift and the NOC
Manager's reason, and two buttons, **Approve** and **Reject**. An incident's page approves all of its tickets at once,
make before break.

It is a link, not a button inside the app, on purpose. Whatever comes back through a Bot's connector carries the NOC
Manager's token, and the Bot holds that token: grok-bot could not tell your tap from the Bot approving its own
proposal. So the approval happens on a page that needs what only you have:

- **a phone signed in to the office:** a one-time QR code from the NOC room, then a cookie on the phone (30 days);
- **your tailnet:** the page has a listener of its own (127.0.0.1:8097) that `tailscale serve` puts on your tailnet
  only. The Bot's cloud computer is not on it, and the public Funnel keeps serving `/mcp` alone.

A link approves nothing by itself, so it does not matter that the Bot sees it.

**Set it up once:**

1. Print the two lines for this computer:

   ```bash
   uv run botctl phone setup
   ```

   Run the `sudo tailscale serve --bg --https=8097 http://127.0.0.1:8097` line it prints (tailnet only: never
   `tailscale funnel` that port), and put its `BOT_PHONE_URL=https://<tailnet-host>:8097` line in `.env` (read
   live, no restart).
2. On the phone: install the Tailscale app and sign in to the same tailnet.
3. Sign the phone in: NOC room, tab **Approvals**, **Sign in a phone**; scan the QR code with the phone's camera and
   press **Sign in**. In a terminal, `uv run botctl phone pair` prints the same QR code.
4. Check the whole path (the listener, `tailscale serve`, through the tailnet, the phones signed in):

   ```bash
   uv run botctl phone status
   ```

5. In the NOC Manager's Bot, re-add [`SKILL.md`](SKILL.md) and [`skills/noc-manager`](skills/noc-manager/SKILL.md),
   and update its routine prompt ([section 4](#4-three-routines-let-zabbix-wake-them)): they tell it to end its
   report with the links.

**Then:** Zabbix pages, the engineers file tickets, the NOC Manager reviews them and its routine run ends with the
links; open its result in the app on your phone, tap a link, Approve. grok-bot applies the ticket and verifies it, and
the ticket's page follows it until it is applied. The ticket names you `phone:<name>` (lab-api's audit log:
`approved-by:phone:<name>`); a phone signed in without a name is `phone:android` or `phone:iphone`.

**The guards:** a decision names the plan the page showed (a newer plan is refused, and shown to you again); an
incident's Approve takes exactly the tickets on the page; forms from any other page are refused; anything that came
through Funnel is refused; the office's tokens do not open these pages. Sign a phone out in the NOC room (Approvals,
**Phones**), with `uv run botctl phone sign-out <id>`, or at the bottom of any page on the phone.

**Not verified in the app:** how the Grok Bot app on Android opens a link. If it opens it in a browser of its own,
without Chrome's cookies, the page says the phone is not signed in and offers **Open it in Chrome**.

## Verified, and not verified

**Verified on this computer** (`make bot-e2e`, no cloud): Zabbix's service action → `/hooks/zabbix` → batching →
pages to the three routine URLs (the routine sink stands in for them), and a scripted role player that calls `/mcp`
with the three tokens exactly as the app does (`initialize`, `tools/list`, `tools/call`): the alert storm, drift, an
AC fault, a gold customer down with Zabbix's escalation step, Ask the NOC, maintenance and mute, the refusals; and
`noc-controller` installed from the `terminal-setup` block in a throwaway home, reading the controller with the
Network Engineer's terminal token (refusals included, the room's terminal marking each command `via: terminal`); and
a phone signed in with a one-time link, approving a field ticket on the phone listener with the Host and Origin
`tailscale serve` passes on (the NOC Manager's links, the refusals, the audit log naming the phone).

**Read from the app's UI text, not tried in your account:** several Bots; MCP servers, skills and routines per Bot;
the webhook trigger with URL and key; routines pausing themselves; commands on your computer; Slack; Egress.

**Not verified at all:** that the app runs `controller_cli` without an approval card (it is marked read only, as
`zabbix_get` is); that a routine run can call its Bot's MCP connector; how the app hands the webhook's JSON
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
| `noc-controller: grok-bot refused the token (HTTP 401)` | the terminal token was rotated, or `BOT_<ROLE>_TERMINAL_TOKEN` is empty (that terminal is off): print and paste a new block |
| `noc-controller: cannot reach https://.../mcp` | the tunnel is down or its URL changed: `curl` check (405), then a new block with the new URL |
| `noc-controller: grok-bot refused the host name ... (HTTP 400)` | the tunnel's host is not in `BOT_MCP_HOSTS` (and grok-bot needs a restart after adding it) |
| `noc-controller: command not found` | `~/.local/bin` is not on the Bot's PATH: `export PATH="$HOME/.local/bin:$PATH"`, or run `~/.local/bin/noc-controller` |
| `noc-controller: several roles are set up here` | Bots share one computer: say which with `--role <role>` |
| the phone's link keeps loading | the phone looks the name up in public DNS, which (with Funnel on) gives the Funnel's address, where port 8097 does not exist: turn Android's Private DNS off (or to Automatic), turn on Use Tailscale DNS in the Tailscale app, and set Chrome's Use secure DNS to your current provider. `https://<tailnet-host>:8097/m/ping` must show `ok` |
