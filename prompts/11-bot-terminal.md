# Session 11 — noc-controller: the Bots run their controller reads from their own computer

## Role
You are a senior network-automation engineer adding a small command, `noc-controller`, that each NOC role's Grok Bot
runs in the terminal of its own cloud computer to read the Clixon controller. In a demo the audience sees the Bot
type a controller command on its own Linux computer and the answer come back, while the NOC room's CONTROLLER
terminal (<127.0.0.1>) shows the same command. The command is only a thin client of grok-bot's `/mcp`
and its `controller_cli` tool: no new way into the lab, no new rights, still read only.

## Read first
`docs/CONTRACT.md` (§9.7 the controller, the `/mcp` parts, §11); `docs/reports/08-grokbot-app.md`, `09-noc-roles.md`
and `10-clixon-controller.md`; `bot/README.md`, `bot/grokbot-app/README.md` and `bot/grokbot-app/DEMOS.md`;
`bot/src/netsim_bot/api/mcp.py`, `roles.py`, `tools.py` (`controller_cli`) and `cli.py` (`_mcp` and `mcp-config`:
the same door the Grok Bot app uses).

## Decisions already made (2026-10-24, with the user): do not revisit without asking
- **The command** `noc-controller <controller command>`: one file, Python 3 standard library only, so it runs on any
  Linux computer that has `python3`. It sends one MCP `tools/call` for `controller_cli` to grok-bot's `/mcp` (the
  tunnel URL the Bot's connector already uses) and prints the answer like a Clixon CLI session.
- **Same door, same walls.** Nothing changes behind `/mcp`: lab-api's allow-list, the controller's NACM, the audit,
  the room's terminal and its transcript. No SSH, no controller key and no lab token on the Bot's computer; no new
  endpoint, port or tunnel route.
- **Its own narrow token.** The Bot can read any file on its computer, so the command does not use the role token.
  New per-role terminal tokens `BOT_NOCENG_TERMINAL_TOKEN`, `BOT_NETENG_TERMINAL_TOKEN`, `BOT_NOCMGR_TERMINAL_TOKEN`:
  `/mcp` accepts one as its role, but only for `controller_cli`. An empty one switches that role's terminal off.
  They are rotated after a demo, with the role tokens.
- **The tool stays.** `controller_cli` is unchanged and is the fallback: the skills tell a Bot to use `noc-controller`
  when it is installed and reaches the lab, and the tool otherwise.
- **Installed by pasting.** The user pastes a block into the Bot's chat and the Bot runs it on its computer. Nothing
  installs itself, and the lab never reaches into the Bot's computer.

## Out of scope
- Any change through the controller (`set`, `commit`, an executor user): a later session.
- Changes to the allow-list, NACM, lab-api, the controller, approvals, or the tunnel's host rule (a tunnel host
  reaches only `/mcp`).
- The Grok Bot app itself: you cannot reach it; the user does the app-side steps.

## Start state
Session 10 (`feature/clixon-controller`) must be committed. Run `git status` first: if it shows changes you did not
make, ask the user to commit them before you start, so this session's diff stands alone. Then create the branch
`feature/bot-terminal` from `feature/clixon-controller` (or from `main` if it has been merged). The lab is running
(`make up`). Save this prompt as `prompts/11-bot-terminal.md`.

## You own
Mostly `bot/`. List every edit outside `bot/` in the report and in the contract's change log.
- **New `bot/grokbot-app/terminal/`:** the `noc-controller` command and a short README.
- **grok-bot:** the terminal tokens in the settings and `/mcp`, `botctl terminal-setup`, the transcript's source mark,
  tests.
- **Skills and docs:** `bot/grokbot-app/SKILL.md` and the three role skills, `bot/grokbot-app/README.md`, `DEMOS.md`,
  `bot/README.md`; `.env.example` (the three tokens); `docs/CONTRACT.md` (`/mcp`, §11, change log);
  `prompts/README.md` (this session's row); the report `docs/reports/11-bot-terminal.md`.

## Target

```
Bot's computer:  $ noc-controller --role network-engineer show devices spoke-1 diff
   └─ HTTPS, terminal token ─► tunnel ─► grok-bot /mcp ─► controller_cli ─► lab-api (allow-list) ─SSH─► controller (NACM)
                                            └─► NOC room: the robot walks to CONTROLLER, the CRT types the same command
```

## Tasks

1. **The command (`bot/grokbot-app/terminal/noc-controller`).**
   - `noc-controller [--role <role>] <words…>` joins the words into one command and sends `tools/call`
     (`controller_cli`, `{"command": ...}`) the way `botctl`'s `_mcp` does; timeout 60 s.
   - It prints the prompt line the room's CRT types (`<user>@controller> <command>`), then the output, then a short
     footer (`read only · as <user> · 0.3 s`). A refusal prints lab-api's reason and the allowed commands; an
     unavailable controller says so and to carry on without it. The output is readable without colour (the app may
     not render ANSI); use colour only on a TTY.
   - Exit codes: 0 answered, 1 refused, 2 controller unavailable, 3 cannot reach `/mcp`, bad token or bad config
     (each with a one-line hint).
   - Config: `~/.config/noc-controller/<role>.conf` (mode 600: URL and token). If one is installed it is used; with
     several (Bots that share one computer) `--role` picks; an unknown or missing one is an error. `--help` shows
     usage, the allowed commands and the config path.
   - **It never prints the token**: not in errors, not in `--help`, not in a traceback.

2. **Terminal tokens in grok-bot.**
   - The three variables go in `.env.example` as `__generate__` (`make env` appends them) and are read live like the
     role tokens; each must differ from every other token.
   - `/mcp`: a terminal token is its role, for `controller_cli` only. `initialize` gives short instructions for the
     terminal, `tools/list` lists that one tool, and `tools/call` refuses any other tool, naming the token kind. The
     token opens nothing else: not the operator API, not `/hooks/zabbix`, not the page.
   - The lab-api actor stays `grok-bot/<ROLE>`. grok-bot's terminal transcript and its event mark calls made with a
     terminal token (`via: terminal`), and the transcript panel shows "from its computer".

3. **`botctl terminal-setup --role <role> --url https://<tunnel-host>/mcp [--show-token]`.**
   - It prints one block to paste into the Bot's chat. The block writes the command to `~/.local/bin/noc-controller`
     (mode 755) and the role's config (mode 600), using quoted heredocs so the shell never expands the script or the
     token, and ends with `noc-controller --role <role> show connections` as a smoke test. Without `--show-token` the
     token is a placeholder, as `mcp-config` does it.
   - Running it again with a new URL (a quick tunnel's URL changes at every start) replaces the config.

4. **Skills (`bot/grokbot-app/`).**
   - `SKILL.md`: when `command -v noc-controller` finds it, run controller reads in your terminal with
     `noc-controller --role <your role> <command>`: one command per run, the same commands and answers as
     `controller_cli`. Otherwise, or when it says it cannot reach the lab, use `controller_cli`. A refusal or
     "unavailable" means what it means for the tool. **Never read, print or copy its config file**: the token would
     show on screen. Never try to reach the lab or the controller any other way.
   - Each role skill: its controller step names the command first and the tool as the fallback.

5. **Tests.**
   - **Unit:** the token kinds (each terminal token is its role, `tools/list` has one tool, any other tool is refused,
     nothing outside `/mcp` accepts it; the role and operator tokens are unchanged); `terminal-setup` (placeholder
     without `--show-token`, safe quoting, file modes); the command against a fake `/mcp` (answered, refused,
     unavailable, 401, unreachable, missing or several configs, `--role`, exit codes, the token never in its output).
   - **On a bare Linux:** run the command in a throwaway `python:3-slim` container (only `python3`, nothing installed)
     against the running grok-bot's `/mcp`; record in the report how it reached grok-bot.
   - **Bot e2e** (routine sink, as today): run the command as a subprocess with the Network Engineer's terminal token.
     `show connections` exits 0 with the prompt line; `show devices spoke-1 check` answers; `pull spoke-1` and
     `commit push` are refused (exit 1, the allowed list); the room's event stream carries them marked
     `via: terminal`; the terminal token calling `get_drift` over raw MCP is refused.
   - If a tunnel is already up, also run the command through it (first expect `405` from a plain `GET` on `/mcp`).
     Do not start a tunnel without asking: it publishes `/mcp`.

6. **Docs.**
   - `bot/grokbot-app/README.md`: a section "The Bot's terminal: `noc-controller`": per Bot, `terminal-setup`, paste,
     smoke test; the reachability check (`curl -s -o /dev/null -w '%{http_code}\n' https://<tunnel-host>/mcp` should
     print `405`); rotating the tokens after a demo; and what is **not verified in the app**: that the Bot's computer
     reaches the tunnel, keeps files between runs, and runs commands without asking each time.
   - `DEMOS.md`: a beat in the drift demo. One command on two screens (the Bot's terminal and the room's CRT); then
     ask the Bot to run `noc-controller commit push`: refused, even on its own computer.
   - The contract (`/mcp` token kinds, §11, change log), `bot/README.md`, and `prompts/README.md` (step 9, with the
     report link).
   - **Report** `docs/reports/11-bot-terminal.md`: what was built, deviations, edits outside `bot/`, how to verify.
     **At the top:** what the user does in the Grok Bot app: per Bot, run `terminal-setup --show-token`, paste the
     block into the Bot's chat, check the smoke test; then re-add the changed skills.

## Acceptance criteria
- On a bare Linux with only `python3`, `noc-controller show connections` with a terminal token prints the
  controller's answer through `/mcp`, and the NOC room shows it on the CONTROLLER terminal.
- A terminal token can call only `controller_cli`; the role and operator tokens work as before.
- The command never prints the token; `terminal-setup` prints it only with `--show-token`.
- `/mcp` is still the Bots' only way in; lab-api, the controller and its NACM are unchanged.
- `make test`, `make lint` and `make bot-e2e` pass.

## Working rules
- The decisions above are fixed; ask the user before changing any of them.
- **Never page the user's real routines** (each page is a paid run): `uv run botctl mute`, or the routine sink
  (`make bot-sink-up`), before any e2e. Put back what was there when you finish.
- **Do not change existing `.env` values.** Never print a token in the chat, the report or a log; tests use fake
  tokens, or read real ones from `.env` without printing them.
- **Ask before `make clean`:** it deletes every lab volume, the NetBox and Zabbix databases included.
- Stay inside this repository. The meetup deck, runbook and videos in the parent folder are not yours.
- After any bot change, rebuild the container: `docker compose build grok-bot && docker compose up -d grok-bot`.
- Commit only when the user asks. Commit messages follow the repo's style (`git log`), with no co-author trailers
  and no tool attribution lines.
