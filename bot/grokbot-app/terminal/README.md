# noc-controller: a role's terminal to the Clixon controller

`noc-controller` is the command a NOC role's Grok Bot runs in the terminal of its own cloud computer to read the
Clixon controller. It is one Python 3 file using only the standard library (Python 3.7 or newer), so any Linux
computer with `python3` runs it. It sends one MCP `tools/call` of `controller_cli` to grok-bot's `/mcp` with the role's
**terminal token**, and prints the answer like a Clixon CLI session. The NOC room's CONTROLLER terminal shows the
same command, and its transcript marks it "from its computer".

```text
$ noc-controller --role network-engineer show devices spoke-1 diff
lab-api@controller> show devices spoke-1 diff
spoke-1:
         pseudowire backup {
-           vc-id "2010";
+           vc-id "2001";
         }
read only · as lab-api · 0.4 s
```

It is only a client of the door the Bot's connector already uses: no SSH, no controller key, no lab token on the Bot's
computer, and no new endpoint, port or tunnel route. lab-api's allow-list, the controller's NACM, the audit (actor
`grok-bot/<ROLE>`) and the room's terminal are the same as for the tool.

| | |
|---|---|
| Install | On the NOC's computer: `uv run botctl terminal-setup --role <role> --url https://<tunnel-host>/mcp --show-token`. Paste the block it prints into that role's Bot chat. The Bot writes `~/.local/bin/noc-controller` (755) and `~/.config/noc-controller/<role>.conf` (600, from quoted heredocs: the shell expands nothing), then runs `noc-controller --role <role> show connections`. A new block (a new URL, a rotated token) replaces the config. |
| Config | `~/.config/noc-controller/<role>.conf` (under `$XDG_CONFIG_HOME` when it is set): a `url=` and a `token=` line, mode 600, or the command refuses it. One file per role: with one file it is used; with several (Bots sharing a computer) `--role` picks. |
| Commands | lab-api's allow-list (`noc-controller --help` lists it): `show connections [<router> [detail]]`, `show devices <router> check\|diff`, `show configuration devices device <router> config [<path>]`. The words are joined into one command, so quoting is optional. |
| Output | `<user>@controller> <command>` (the line the room's CRT types), the answer, then `read only · as <user> · <seconds>` (with the meaning of an empty answer, e.g. `in sync`). A refusal prints lab-api's reason and the allowed commands. Colour only on a terminal (`NO_COLOR` turns it off). |
| Exit codes | 0 answered; 1 refused; 2 controller unavailable (carry on without it); 3 cannot reach `/mcp`, bad token, bad config or bad usage, with a one-line `hint:`. |
| The token | The role's terminal token (`BOT_<ROLE>_TERMINAL_TOKEN`): `/mcp` takes it for `controller_cli` only, never the role token (the Bot can read every file on its computer). It never appears in the output: not in errors, not in `--help`, not in a traceback (there is none). Redirects are not followed, so it goes to that URL only. |

Tests: `bot/tests/test_noc_controller.py` (the command against a fake `/mcp` and against grok-bot's app),
`bot/tests/test_terminal_tokens.py` (the tokens, `terminal-setup`), `bot/e2e/test_80_terminal.py` (the live lab);
`make bot-lint-terminal` (part of `make lint`). Setup in the app: [`../README.md`](../README.md#5-the-bots-terminal-noc-controller).
