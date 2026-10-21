# The Grok Bot app in the NetSim NOC

Your **Grok Bot app** (`/opt/Grok Bot`, SpaceXAI's cloud agent) works the lab through the same door as the in-house
crew: the NOC's tools, served by grok-bot. In the NOC room it is a visitor robot, **GROK**, with a dark visor. It walks
in through the door when it starts calling tools and leaves when it goes quiet.

| Piece | What it is |
|---|---|
| `/mcp` on grok-bot | the NOC's tools as an MCP server: 11 lab reads, 3 proposal tools, 6 NOC tools (status, incidents, tickets, wait, notes). Auth: `Authorization: Bearer <BOT_AGENT_TOKEN>` |
| `uv run botctl call <tool> '<json>'` | the same tools from a shell, for an agent that runs commands on your computer |
| the pager | grok-bot POSTs one webhook per incident to a Grok Bot routine ("when a webhook fires"), so the app hears about faults on its own |
| [`SKILL.md`](SKILL.md) | how to work in this NOC, to give the Grok Bot app as a skill |
| [`DEMOS.md`](DEMOS.md) | eight demos with the prompts to paste and what to show |

**The rule that does not bend:** the app reads, proposes, waits and takes notes. **It cannot approve or apply.** Every
change it proposes is a lab-api dry run that a human approves in the NOC room (operator desk) or with
`uv run botctl approve CHG-xxxx`. That holds even under `BOT_AUTONOMY=auto`, which auto-approves only the crew's own
tickets. The agent token is not the operator token.

What the app can do was read from its own UI text (`app.asar`, grok-bot-0.66.0) on 2026-10-20, not tried in your
account:
- custom MCP servers: a remote URL with HTTP headers, or a command;
- running commands on your computer, with your permission per command;
- routines triggered by a webhook, a schedule (hourly, daily, weekdays), a calendar series or a Teams message;
- Slack;
- routing its traffic through your computer ("Egress").

Your plan or team settings may switch some of them off.

## 1. Check the NOC side

```bash
make env
```

`make env` adds `BOT_AGENT_TOKEN` and the pager settings to an existing `.env` and never changes values already there.

```bash
docker compose build grok-bot && docker compose up -d grok-bot
```

```bash
uv run botctl tools
```

```bash
uv run botctl call get_noc_status
```

## 2. Connect the app: pick one way

### A. Commands on your computer (works today, no tunnel)

The app's computer is in the cloud, but it can run commands on yours. Your permission is asked per command, and
auto-review checks it. Tell it where the repository is, and give it [`SKILL.md`](SKILL.md). It then works with:

```bash
uv run botctl call get_open_problems
```

```bash
uv run botctl call get_impact '{"host": "hub-c"}'
```

```bash
uv run botctl call propose_field_repair '{"kind": "device", "host": "hub-b", "reason": "...", "incident_id": "INC-0012"}'
```

Each call prints JSON and exits 1 when the tool reports an error. `-a key=value` works too:
`botctl call get_device_state -a host=spoke-1`.

### B. MCP connector (needs a public HTTPS URL)

The app adds a custom MCP server from JSON keyed by the server name. Print it with:

```bash
uv run botctl mcp-config --url https://<your-tunnel-host>/mcp --show-token
```

That prints `{"netsim-noc": {"url": ..., "headers": {"Authorization": "Bearer ..."}}}`. Treat the token like a
password.

The catch is reachability. A "command" MCP server would run on the app's cloud computer, and a URL server must be
reachable from the cloud, while grok-bot listens on `127.0.0.1:8095`. So `/mcp` needs a tunnel:
1. Use one you trust, for example a Cloudflare tunnel to `http://127.0.0.1:8095`.
2. Put its host name in `.env` as `BOT_MCP_HOSTS=<host>` (or `*.trycloudflare.com`). Requests for that host reach
   **only** `/mcp`: not the NOC page and not the open read API.
3. Keep `BOT_AGENT_TOKEN` long and secret. Rotate it (new value in `.env`, read live) when a demo is over.

The app's "Route traffic through this computer" (Egress) setting might let its computer reach your network without a
tunnel. It is untested here.

Any local MCP client (Cursor, MCP Inspector, ...) can use `http://127.0.0.1:8095/mcp` directly with the same
header.

## 3. The pager: let faults wake the app

1. In the Grok Bot app, ask it in chat to set up a routine that runs **when a webhook fires**, with the prompt from
   [`DEMOS.md` → demo 1](DEMOS.md#1-on-call-the-bot-gets-paged). Saving it gives a **webhook URL** and a **key**: the
   app calls it "Webhook key"; Cursor's automations show it as `Bearer crsr_...`.
2. Put both in `.env` (read live, no restart):

   ```
   BOT_PAGER_URL=https://...the routine's webhook URL...
   BOT_PAGER_TOKEN=crsr_...        # with or without "Bearer ", the bot sends "Authorization: Bearer <key>"
   BOT_PAGER_EVENTS=opened         # add diagnosed,resolved for follow-up pages (each one is an agent run)
   BOT_PAGER_MIN_SEVERITY=Average
   BOT_PAGER_MAX_PER_HOUR=6
   ```

3. Send a test page:

   ```bash
   uv run botctl pager test
   ```

   Then check its log:

   ```bash
   uv run botctl pager status
   ```

**Why the bot pages and not a Zabbix action:** one hub power-off raises 7-14 Zabbix problems in a few seconds, and
each page is a paid agent run. The watcher already joins them into one incident (settle window, service joining), so
a fault costs one page.

**What a page carries:**
- `event`: `incident.opened|diagnosed|resolved|test`;
- `summary` and `incident`: id, title, severity, services, hosts, problems;
- `services`: customer, tier, region, Zabbix state;
- `report` and `proposals` on follow-ups;
- `links`: the NOC room, Zabbix, NetBox;
- `how_to_respond`: a short instruction.

**When no page is sent:**
- maintenance mode is on, or the pager is muted (`botctl pager mute` / `unmute`); both e2e suites mute it;
- the severity is below the minimum;
- the hourly cap is reached.

**Retries:** three tries, then the delivery shows as failed. A 4xx is not retried.

**Known issue on Cursor's side (forum reports, 2026):** automation webhooks sometimes answered **401
`ERROR_NOT_LOGGED_IN`** even with a fresh key, a service-side fault Cursor acknowledged. If `botctl pager test` shows
401 with that body, it is not this lab: regenerate the key, and if it persists, run the routine by hand.

## 4. Slack (optional)

Connect the app to a Slack workspace in its settings: an admin may need to approve it. Then ask the routines in
[`DEMOS.md`](DEMOS.md) to post to a channel. The links in a page point at `127.0.0.1`, so they only open on the
machine running the lab.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `botctl call` says `BOT_AGENT_TOKEN is not set` | run `make env` |
| `/mcp` answers 404 `the MCP endpoint is off` | no `BOT_AGENT_TOKEN` in `.env` |
| 401 `missing or wrong Authorization` | the header must be `Authorization: Bearer <BOT_AGENT_TOKEN>`; the operator token is not accepted |
| 400 `Invalid host header` through a tunnel | add the tunnel's host to `BOT_MCP_HOSTS` and restart grok-bot (the Host list is read at start) |
| the visitor never shows in the room | the page needs a reload after rebuilding the image; GROK appears on its first tool call |
| `propose_*` missing from the tool list | autonomy is `observe` (`botctl autonomy propose`) |
| a proposal comes back with someone else's id | the crew had filed the same change for that incident: there is one ticket per change |
