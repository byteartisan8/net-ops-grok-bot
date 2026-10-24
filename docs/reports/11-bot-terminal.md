# Session 11 — noc-controller: the Bots run their controller reads from their own computer

**Date:** 2026-10-24. **Prompt:** [`prompts/11-bot-terminal.md`](../../prompts/11-bot-terminal.md) (the user's
decisions of 2026-10-24). **Branch:** `feature/bot-terminal`, from `03c4373` (session 10, as merged into `main` on
GitHub); uncommitted until the user asks.

## First: what you do in the Grok Bot app

`.env` already has the three new terminal tokens (`make env` appended them; no existing value changed). For each of
the three Bots (NOC Engineer, Network Engineer, NOC Manager):

1. **Can its computer reach the tunnel?** Ask the Bot in its chat to run
   `curl -s -o /dev/null -w '%{http_code}\n' https://<tunnel-host>/mcp`. It should print `405` (grok-bot's `/mcp`
   answered a plain GET). Anything else: that computer cannot reach the tunnel, and the Bot keeps using
   `controller_cli`.
2. **Print the role's block,** with the URL its connector uses:

   ```bash
   uv run botctl terminal-setup --role network-engineer --url https://<tunnel-host>/mcp --show-token
   ```

   (`--role noc-engineer`, `--role noc-manager` for the other two.) Only the block goes to stdout; it holds the
   role's terminal token, so treat it like a password.
3. **Paste the whole block into that Bot's chat** and let the Bot run it. Check its smoke test: the first line is
   `network-engineer@controller> show connections`, then ten routers `OPEN`, then
   `read only · as network-engineer · 1.0 s`. At the same moment the role's robot walks to the CONTROLLER terminal
   in the NOC room and the CRT types the same command; the Terminal tab marks it "from its computer".
4. **Re-add the changed skills** in every Bot: the shared [`SKILL.md`](../../bot/grokbot-app/SKILL.md) and the
   Bot's role skill (`skills/noc-engineer/`, `skills/network-engineer/`, `skills/noc-manager/`). They now say: use
   `noc-controller` when it is installed, `controller_cli` otherwise, and never read the config file.

Nothing else changes in the app: the same connectors, role tokens and routines. **After a demo**, rotate the terminal
tokens with the role tokens (new values for `BOT_<ROLE>_TERMINAL_TOKEN` in `.env`: grok-bot reads them live) and
paste new blocks before the next demo. When a quick tunnel's URL changes, print and paste the block again: it
replaces the config. The demo beat is in [`DEMOS.md`](../../bot/grokbot-app/DEMOS.md) (demo 3).

**Result:**
- **`noc-controller`** ([`bot/grokbot-app/terminal/`](../../bot/grokbot-app/terminal/README.md)): one Python 3 file,
  standard library only. `noc-controller --role network-engineer show devices spoke-2 diff` sends one MCP
  `tools/call` of `controller_cli` to `/mcp` and prints the CLI session the room's CRT types: the prompt line, the
  answer, `read only · as lab-api · 0.2 s`. Exit codes: 0 answered, 1 refused, 2 controller unavailable, 3 cannot
  reach `/mcp`, bad token, bad config or bad usage (with a `hint:`).
- **On a bare Linux:** in a throwaway `python:3-slim` container (Python 3.15.1, Debian 13, `dash`, uid 1000, nothing
  installed) the pasted block installed the command and its smoke test answered. It reached grok-bot two ways: on
  labnet (`http://172.30.10.60:8095/mcp`, 0.1-0.2 s per command) and through the running Tailscale Funnel, from the
  container on Docker's default bridge to the Funnel's public address (0.9-1.2 s per command). Over the Funnel,
  `GET /mcp` gave `405` and `GET /` gave `404`.
- **The terminal tokens** `BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`, `BOT_NOCMGR_TERMINAL_TOKEN`: each
  one is its role on `/mcp`, for `controller_cli` only. `tools/list` shows that one tool. Any other tool is refused
  as "this is the Network Engineer's terminal token", before lab-api or a robot moves. No other route accepts these
  tokens. The role and operator tokens work as before.
- **Same door, same walls:** lab-api's allow-list, the controller's NACM, the audit actor (`grok-bot/NETENG`), the
  robot's walk and the CRT are those of the tool. lab-api, the controller, its NACM and the tunnel's host rule did
  not change. The room's transcript and its `terminal` event say `via: terminal`.
- **Tests:** `make test` 804 passed, `make lint` clean, `make e2e` passed (bot e2e 15 including the three new
  terminal tests, controller 7, lab e2e 23; 22 min 47 s). The run before it failed one lab timing test that this
  session did not touch: the trap → re-poll median, 5.21 s against 5.0 s. It is a flake of Zabbix's 5-second task
  grid ([Checks](#checks)) and is offered as a separate task.

---

## What was built

```
Bot's computer:  $ noc-controller --role network-engineer show devices spoke-2 diff
   └─ HTTPS, terminal token ─► tunnel ─► grok-bot /mcp ─► controller_cli ─► lab-api (allow-list) ─SSH─► controller (NACM)
                                            └─► NOC room: the robot walks to CONTROLLER, the CRT types the same command;
                                                the transcript says "from its computer" (via: terminal)
```

### 1. The command (`bot/grokbot-app/terminal/noc-controller`)

- `noc-controller [--role <role>] <words…>` joins the words into one command (quoting is optional; options may come
  before or after the words) and sends one JSON-RPC `tools/call` (`controller_cli`, `{"command": ...}`) the way
  `botctl`'s `_mcp` does: `Authorization: Bearer <terminal token>`, `Accept: application/json, text/event-stream`,
  timeout 60 s. It does not filter commands itself: lab-api decides, so a refusal is also audited and shown in red on
  the CRT.
- **Output:** the prompt line the CRT types (`<user>@controller> <command>`; `lab-api` for `check` and `diff`, which
  run as lab-api's controller user), the answer, then `read only · as <user> · 0.2 s`. When the answer is empty it
  adds what that means (`in sync`, `no difference: in sync`). In a diff, `-` lines are red and `+` lines green, but
  colour is only used on a TTY (not when piped, not with `NO_COLOR`). A refusal prints lab-api's reason, the allowed
  commands one per line, and `refused by lab-api's allow-list · nothing reached the controller`. When the controller
  is unavailable it prints the tool's message (carry on without it: `get_drift`, ...) and
  `controller unavailable · carry on without it`.
- **Exit codes:** 0 answered; 1 refused; 2 controller unavailable (also any other error of the tool, e.g. lab-api
  down: the controller cannot be read now); 3 cannot reach `/mcp` (DNS, refused, timeout, TLS, a tunnel's 5xx), bad
  token (401), wrong host (400), redirect, not grok-bot (404, not JSON-RPC), bad config or bad usage. Each exit 3
  prints one `noc-controller: <what>` line and one `hint: <what to do>` line on stderr, and nothing on stdout.
- **Config:** `~/.config/noc-controller/<role>.conf` (`$XDG_CONFIG_HOME` if set) holds `url=` and `token=`. A config
  that others can read (any group or other bit) is refused, as ssh refuses keys: `chmod 600 <file>`. With one config
  installed, it is used. With several (Bots sharing a computer), `--role` picks (`NETENG`-style names work too). An
  unknown role or a missing config is exit 3, listing what is set up. A placeholder token is caught.
- **The token never shows:** everything printed (stdout and stderr) is filtered: the token becomes `***`, even if a
  server echoes it. No traceback ever appears; an unexpected error is one line, exit 3. `--help` names the config
  file, never its content. Redirects are not followed: a 3xx is reported, so the token goes to the configured URL
  only. The printed text is also stripped of control characters (ESC, BEL, C1), so nothing in an answer can drive
  the Bot's terminal.
- **Pipes:** with `noc-controller ... | head -3`, when the reader goes away the rest of the output goes nowhere and
  the exit code stays the real one (found when testing `--help | head -1` in the container).
- Python **3.7 or newer**: only the standard library (`http.client`, `json`, `os`, `sys`, `time`, `urllib`,
  `pathlib`). Ruff found no syntax newer than 3.7 (`ruff check --target-version py37`). It ran on 3.12 (unit tests),
  3.14 (this host) and 3.15 (`python:3-slim`).

### 2. Terminal tokens in grok-bot

- **Settings** (`settings.py`): `BOT_<ROLE>_TERMINAL_TOKEN`, read live like the role tokens (`.env` on every load;
  rotating one locks the old one out within 2 s); empty = that role's terminal is off.
  `GET /api/state` `settings.roles.<role>.terminal` says which terminals are on (no secret), and `botctl status`
  has a `terminal` column. **Every `/mcp` token (role or terminal) must differ from every other token**, including
  `BOT_OPERATOR_TOKEN`, `BOT_ZABBIX_HOOK_TOKEN` and `LAB_API_TOKEN`. Otherwise the settings are refused, and the
  error names the two variables, never the value. Before this session only the three role tokens were compared.
- **`/mcp`** (`api/mcp.py`): the token decides a `Caller(role, via)`, compared in constant time against all six
  tokens. For a terminal token, `initialize` gives the terminal's short instructions (server
  `netsim-noc-<bot>-terminal`) and `tools/list` lists `controller_cli` alone. In `tools/call`, any other tool is
  refused by the role gateway (`roles.py`) with "`<tool>` is refused: this is the `<Role>`'s terminal token
  (noc-controller on the Bot's computer), which runs controller_cli only", logged, before the robot moves or
  anything reaches lab-api. The endpoint stays on as long as any role or terminal token is set.
- **Nothing outside `/mcp`** accepts a terminal token: the operator API wants `X-Operator-Token` (a different
  value, now enforced), `/hooks/zabbix` the hook token, and a tunnel host reaches `/mcp` only (`McpOnlyHosts`,
  unchanged).
- **The transcript and the event** (`engine.record_terminal`) carry `via`: `connector` (the role token) or
  `terminal`. The side panel's Terminal tab shows a "from its computer" pill on such sessions, and the CRT's tooltip
  says it too. The lab-api actor stays `grok-bot/<ROLE>`, and the robot walks to the CONTROLLER terminal as for the
  tool.

### 3. `botctl terminal-setup --role <role> --url <the /mcp URL> [--show-token]`

- Prints one block for the Bot's chat: a sentence telling the Bot what to run and never to read the config, then a
  POSIX `sh` block. Inside a subshell with `set -eu`, it writes the command to `~/.local/bin/noc-controller` (755)
  from a quoted heredoc (`<<'NOC_CONTROLLER_SCRIPT'`). It then removes and re-creates the role's config under
  `umask 077` from another quoted heredoc (`<<'NOC_CONTROLLER_CONFIG'`), `chmod 600`, with the config folder at 700.
  Outside the subshell it puts `~/.local/bin` on the PATH and ends with the smoke test,
  `noc-controller --role <role> show connections`. The shell expands nothing: a test token holding `$HOME`,
  backquotes and a backslash arrives as it was.
- Without `--show-token` the token is the placeholder `<BOT_<ROLE>_TERMINAL_TOKEN from .env>`, as `mcp-config` does
  it. With `--show-token` and the terminal off (empty), it refuses (exit 1). It refuses a delimiter line inside the
  script and a control character in the URL or token, which would break the quoting.
- **Checks the URL:** only http(s) URLs are accepted. It warns when the URL does not end in `/mcp`, when it is plain
  http to a non-local host, and when the host is in neither `BOT_MCP_HOSTS` nor `BOT_ALLOWED_HOSTS` (grok-bot would
  answer 400). The block goes to stdout, notes to stderr.
- Running it again (a new quick-tunnel URL, a rotated token) and pasting the new block replaces the config, and makes
  it 600 again even if someone opened it up.

### 4. Skills and docs

- [`SKILL.md`](../../bot/grokbot-app/SKILL.md) has a new section "Your terminal (`noc-controller`)". It covers the
  check (`command -v noc-controller`, or `~/.local/bin/noc-controller` when that folder is not on the PATH), one
  command per run with `--role <your role>`, and the exit codes (1 and 2 mean what they mean for the tool; 3 or not
  installed: use `controller_cli`). It also says never to read, print or copy the config, and never to reach the lab
  or the controller any other way.
- In the three role skills, the controller step names the command first and the tool as the fallback. The Network
  Engineer still quotes the diff, and for the NOC Engineer `show connections` still belongs to every triage.
- [`bot/grokbot-app/README.md`](../../bot/grokbot-app/README.md) gains section 5, "The Bot's terminal:
  `noc-controller`": per Bot, the reachability check (`405`), `terminal-setup`, the paste and the smoke test, re-adding
  the skills, rotation, and what is not verified in the app. It also gains five troubleshooting rows; Slack and
  "No tunnel" are now sections 6 and 7.
  [`DEMOS.md`](../../bot/grokbot-app/DEMOS.md): the terminal beat in demo 3 (one command on two screens, then
  `noc-controller commit push` refused on the Bot's own computer). Also updated: `bot/README.md`, the terminal's own
  README, `.env.example`, the contract and `prompts/README.md`.

### 5. Tests

- **Unit, grok-bot side** (`bot/tests/test_terminal_tokens.py`, 27):
  - each terminal token is its role, with one tool listed and the same lab-api actor;
  - five other tools refused (`get_drift`, `get_noc_status`, `take_incident`, `review_ticket`, an unknown one), with
    no lab-api request and no event;
  - nothing outside `/mcp` takes the token (operator API, approve, test page, Ask, `/hooks/zabbix`, the page and the
    reads through a tunnel host);
  - role and operator tokens unchanged;
  - `via` in the event, the transcript and `GET /api/state`, and the page's "from its computer";
  - tokens read live and rotated; a terminal token equal to six kinds of other token is refused without showing it;
  - `terminal-setup`: placeholder; real token once, quoted; the script verbatim; the smoke test last; the block run
    by `sh` in a throwaway home (modes 755/600/700, the odd token intact, a second URL replacing the config and
    making it 600 again); URL checks; terminal off; delimiter and newline guards;
  - the skills, `DEMOS.md` and the README.
- **Unit, the command** (`bot/tests/test_noc_controller.py`, 27): run as a subprocess with its own home, against a
  fake `/mcp` (a stdlib HTTP server):
  - an answer (prompt line, diff lines, footer, the exact request), an empty answer with its meaning;
  - a refusal (reason, list, routers); unavailable and any other tool error (2);
  - 401, 404, 400 host, 502 and 530 with HTML bodies (not printed), a captive portal, a JSON-RPC error (each 3 with
    a hint); a redirect not followed (the other server gets nothing); nothing listening;
  - the token echoed by a server, then redacted; an answer that makes the command fail (one line, no traceback);
    escape sequences dropped;
  - which config: none, one, several, `--role` forms, an option after the words, unknown, not set up, `XDG_CONFIG_HOME`;
  - a bad config (mode 644, placeholder, no url, no token); usage errors and `--help`;
  - a closed pipe keeping the exit code; `--help` matching lab-api's allow-list; stdlib-only imports;
  - against grok-bot's real app on a real port (uvicorn in a thread, lab-api faked): a read, a refusal, the events
    `via: terminal`, and a disabled terminal's token (401).
  - Every run also asserts that no token appears in the output and that there is no traceback.
- **Bot e2e** (`bot/e2e/test_80_terminal.py`, 3, on the running lab with the routine sink):
  - the block from `botctl terminal-setup` run by `sh` in a throwaway home with the real Network Engineer's terminal
    token: modes, smoke test;
  - `show connections` exit 0 with the prompt line and ten `OPEN`; `show devices spoke-1 check` answers, in sync;
  - `pull spoke-1` and `commit push` exit 1 with the allowed list;
  - the room's event stream carrying the four sessions `via: terminal`; the transcript; the audit (actor
    `grok-bot/NETENG`, 200, 200, 403, 403);
  - over raw MCP the terminal token lists one tool, gets `get_drift` refused, and gets 401 from the operator API and
    `/hooks/zabbix`.
  - No token is printed: assertions compare booleans, never the token.
- `make lint` now also runs ruff on the command (`bot-lint-terminal`, added from `bot/bot.mk` like `e2e: bot-e2e`):
  `ruff check .` skips files without `.py` unless they are named.

## Deviations and choices within the plan

- **Start state.** Session 10 was committed on `feature/noc-roles` and merged into `main` on GitHub (`03c4373`);
  there was no `feature/clixon-controller` branch. Following "from `main` if it has been merged", `feature/bot-terminal`
  starts at `03c4373`. The working tree was clean. **The running lab was still the session 9 build:** no `controller`
  container, and lab-api, grok-bot and the PEs on images from before session 10. Session 10 had been built and
  tested on another computer. With pages muted (`botctl mute`), `make build up` brought it to session 10 (the build
  took 1.5 min). `labctl status`: `controller: 10/10 connected, 10 in sync`, and `controller_cli` answered through
  grok-bot. Volumes were kept; nothing was cleaned.
- **Exit 3 also means bad usage** (no command, an unknown option): the prompt's four codes leave no room for it, and
  argparse's usual 2 would read as "controller unavailable".
- **Other tool errors are exit 2.** If lab-api is down, the tool fails the same way, so falling back to it is
  pointless and the Bot should carry on without the controller.
- **Also chosen:** the command refuses a config that others can read; it does not follow redirects; it strips
  control characters; it accepts options after the words; it adds the meaning of an empty answer to the footer. None
  of these changes what reaches grok-bot.
- **Token uniqueness is wider than before:** all `/mcp` tokens are compared with the operator, hook and lab tokens,
  not only the role tokens among themselves. It is the only way to guarantee that "the token opens nothing else".
  An existing `.env` passes (its values are random); a hand-set duplicate now stops grok-bot from loading the
  settings, as a duplicated role token already did.
- `host_matches` moved from `api/app.py` to `settings.py`: `botctl terminal-setup` uses it to warn about the
  tunnel's host.
- **The setup block exports the PATH for the smoke test,** and the skill also looks in `~/.local/bin`: a Bot's
  non-login shell may not have `~/.local/bin` on its PATH.

## Edits outside `bot/`

| Path | Owner | Change |
|---|---|---|
| `.env.example` | 01-foundation | `BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`, `BOT_NOCMGR_TERMINAL_TOKEN` (`__generate__`) and their comment; the role tokens' comment says the terminal tokens also leave this computer |
| `docs/CONTRACT.md` | all | §3 (the `bot/` row, the `make lint` delegate), §9.6 (two token kinds on `/mcp`), §9.7 (`via` in the `terminal` event; the roles' terminals), §11 (the terminal tokens; every `/mcp` token distinct), §12, change log (three rows) |
| `prompts/README.md` | all | step 9 with the report link; one sentence on session 11 |
| `prompts/11-bot-terminal.md` (new) | 11 | the prompt, as given |
| `docs/reports/11-bot-terminal.md` (new) | 11 | this report |

Nothing in `lab/`, `controller/`, `devices/`, `zabbix/`, `netbox/`, `contract/`, `tools/` or the root `Makefile`,
`compose.yaml` and `pyproject.toml`. In `bot/`: `src/netsim_bot/` (`settings.py`, `api/mcp.py`, `api/app.py`,
`roles.py`, `engine.py`, `cli.py`, `web/js/panel.js`, `web/js/main.js`, `web/css/noc.css`), `grokbot-app/`
(`terminal/` new, `SKILL.md`, the three role skills, `README.md`, `DEMOS.md`), `README.md`, `bot.mk`, `tests/`
(`conftest.py`, `test_api.py`, two new files), `e2e/test_80_terminal.py` (new).

## Timings

| Path | Measured |
|---|---|
| `noc-controller show connections` on this host → `/mcp` → controller (bot e2e, two runs) | 0.19 s, 0.18 s |
| the same from a `python:3-slim` container on labnet | 0.1-0.2 s per command |
| the same through the Tailscale Funnel's public address | 0.9-1.2 s per command |
| `labctl reset` after `fake-redundancy` (the controller resync included) | 5.7 s |

## Checks

All on 2026-10-24, on the running lab (session 10's images built with `make build up`; grok-bot rebuilt after each
bot change):

| Check | Result |
|---|---|
| `make test` | 804 passed (35 s) |
| `make lint` | ruff, ruff format, `pyang --strict`, yanglint, ruff on `noc-controller`: clean |
| `make e2e`, final run | **passed** in 22 min 47 s: bot e2e **15 passed** (8 min 49 s; `test_80_terminal` 3), `controller-test` **7 passed** (28 s), lab e2e **23 passed** (13 min 10 s, including `make down && make up`). Timings in budget, e.g. hub-a power-off → NOC Engineer page 23.2 s (30), AC fault → interface problem 5.0 s (10), trap → re-poll median 3.46 s, max 5.46 s (5 / 7) |
| `make e2e`, the run before | bot e2e 15 passed, `controller-test` 7 passed, lab e2e **22 passed, 1 failed**: `test_trap_to_check_now_latency`, median 5.21 s (budget 5.0), samples `[5.75, 2.72, 1.72, 5.72, 4.72, 2.71, 5.71, 5.70]`, max within its 7 s. Each sample is about 0.7 s plus the wait to the next tick of Zabbix 8.0's fixed 5 s task-manager grid (contract §9.5, 04-zabbix), and the test does not spread its traps evenly over that grid. That test alone, run twice right after: medians 2.96 s and 2.93 s, passed; earlier runs here had medians 2.5-4.6 s. Nothing in this session touches traps, Zabbix or lab-api. The fix (place the traps evenly across the grid) belongs to `lab/` (05) and is offered as a separate task |
| bare Linux, labnet | `docker run --network labnet --user 1000:1000 python:3-slim`, the block piped into `sh`: smoke test exit 0, `show devices spoke-1 diff` exit 0 (in sync), `pull spoke-1` and `commit push` exit 1, `--help \| head -1` and `show configuration ... \| head -3` clean |
| bare Linux, through the tunnel | the Funnel was already up (not started by this session): `GET /mcp` → `405`, `GET /` → `404` from its public address. The container, on Docker's default bridge with the tunnel host mapped to the public address, ran the block: smoke test exit 0, `show devices spoke-1 check` exit 0 (in sync), `commit push` exit 1 |
| live demo beat | `labctl scenario apply fake-redundancy` (pages muted), then from the throwaway home: `show devices spoke-2 diff` → `-  peer-ipv4 "10.255.0.2";` / `+  peer-ipv4 "10.255.0.1";`, `hub-c check` in sync, `hub-a check` `Error: device out-of-sync`; `labctl reset` green in 5.7 s, ten in sync |
| the room | the Terminal tab shows "from its computer" on the sessions from the terminal (browser pane, 1600×1000); the pill wrapped in the narrow panel, so it got `white-space: nowrap` |
| pages | muted before the rebuild and during the work (`botctl mute`); both e2e suites used the routine sink or maintenance mode; set back to on, as found, at the end |

Not done: a run from empty volumes (`make clean`, then `make env net build up seed e2e`). It waits for your go-ahead.
This session changed nothing in bring-up beyond three generated `.env` variables (`make env` appends them).

## How to verify

```bash
uv run botctl status                                   # the terminal column: on for the three roles
# a bare Linux as the Bot's computer: the block's shell part, piped (no file holds the token)
uv run botctl terminal-setup --role network-engineer --url http://172.30.10.60:8095/mcp --show-token \
  | sed -n '/^```sh$/,/^```$/p' | sed '1d;$d' \
  | docker run --rm -i --network labnet --user 1000:1000 -e HOME=/tmp/bot python:3-slim \
      sh -c 'mkdir -p "$HOME" && cd "$HOME" && sh -s'  # the smoke test: network-engineer@controller> show connections
make bot-test                                          # the tokens, terminal-setup, the command (fake /mcp, grok-bot)
make lint                                              # includes ruff on noc-controller
make bot-e2e                                           # includes test_80_terminal (~10 min)
```

Open the NOC room (<http://127.0.0.1:8095>) while the container runs: NETENG walks to the CONTROLLER terminal and the
CRT types `network-engineer@controller> show connections`; the Terminal tab marks it "from its computer".

**Not verified in the app** (you do these steps): that a Bot's computer reaches the tunnel, keeps
`~/.local/bin/noc-controller` and its config between runs and chats, runs commands without asking each time, and
runs a 14 KB block as it is.
