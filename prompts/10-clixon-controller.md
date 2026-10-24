# Session 10 — Clixon controller: a controller in front of the routers, and a terminal the NOC roles type into

## Role
You are a senior network-automation engineer adding a **Clixon controller** to the lab. It manages the ten PE
routers over NETCONF, notices when someone changes a router behind its back, and gives the three NOC roles (the
user's Grok Bots) a real network CLI to type into. Their commands show live on a new terminal in the NOC room
(<http://127.0.0.1:8095>). The Bots still reach the lab only through grok-bot's `/mcp`: the controller adds no new
way in, and with it they can only read.

## Read first
`docs/CONTRACT.md`; `docs/reports/02-device-simulator.md`, `05-integration.md` and `09-noc-roles.md`;
`devices/README.md`, `lab/README.md`, `bot/README.md` and `bot/grokbot-app/README.md`. Then the controller's
documentation, <https://clixon-controller-docs.readthedocs.io/> (overview, CLI tutorial, transactions).

## Decisions already made (2026-10-24, with the user): do not revisit without asking
- **One controller,** the Clixon controller, in its own container `controller` at `172.30.10.70` on `labnet` (free
  in contract §4 today; check again). It manages all ten PEs.
- **The PEs also speak NETCONF over SSH.** The controller's southbound is NETCONF over SSH only, and today the PEs
  serve only RESTCONF. RESTCONF stays exactly as it is: lab-api, simd and the scenarios keep using it.
- **NetBox stays the source of truth.** The controller's copy of a router is the config the lab last put there on
  purpose: golden after `labctl reset`, or an approved change once lab-api has applied and verified it. Anything
  else that changes a router (a scenario such as `vcid-typo`, someone on the router's CLI) makes it **out of sync**.
  That is the point: a second drift signal, next to NetBox.
- **The Bots read; they never change anything through the controller.** One new MCP tool, `controller_cli`, for all
  three roles. lab-api runs each command in a real controller CLI session, after checking it against an allow-list
  in code. Changes stay change tickets, applied as today (lab-api over RESTCONF, after a human approves).
- **Two walls.**
  1. lab-api's allow-list decides what reaches the controller. Anything not on it is refused there.
  2. NACM on the controller: each role has its own controller user, which can read but not edit, commit, pull or
     push. `CLICON_NACM_CREDENTIALS=exact`, so no user can pose as another. lab-api's own controller user does
     the pulls.
- **No new door and no new secret outside.** The controller and the PEs' SSH listen on `labnet` only. The Bots still
  hold only their role tokens; lab-api holds the controller credentials, like every other credential.
- **The terminal is part of the NOC room,** in the room's pixel-art style, with no build step and no external files.
- **The controller is optional at run time.** With `controller` stopped, everything else works as before and the
  tool says the controller is unavailable.

## Out of scope (a later session)
- Changes through the controller: `commit push`, an executor user, services, templates.
- Any change to approvals, the executor, the `plan_id` check, `/mcp` auth or the tunnel.
- Reaching the controller from outside `labnet`: the Bots, the tunnel, published ports.

## Start state
Session 09 and the NOC room work since then are committed. Run `git status` first: if it shows changes you did not
make, ask the user to commit them before you start, so this session's diff stands alone. Work on a new branch,
`feature/clixon-controller`. The lab is running (`make up`).

## You own
This session crosses components. List every edit outside `controller/` in the report and in the contract's change
log.
- **New `controller/`:** Dockerfile, its config and NACM rules, `controller/compose.yaml`, `controller/controller.mk`
  (`controller-*` targets), tests. The root `compose.yaml` gets one more `include:`; the root `Makefile` includes
  `controller.mk`.
- **Devices:** the netsim-pe image (`devices/Dockerfile`, `devices/supervisord.conf`, `devices/entrypoint.sh`, the
  healthcheck), for NETCONF over SSH.
- **lab-api (`lab/`):** the controller client, `POST /controller/cli`, the controller resync, `labctl status`,
  `lab/openapi.json`, e2e.
- **grok-bot (`bot/`):** the `controller_cli` tool, the room's terminal, the skills in `bot/grokbot-app/`, e2e.
- **Config and docs:** `.env.example`, `contract/topology.yaml` (the controller under `infrastructure:`),
  `docs/CONTRACT.md` (a controller section, §3, §4, §11, §12 and the change log), `docs/RUNBOOK.md`, the root
  `README.md`, `prompts/README.md` (this session's row), `docs/reports/10-clixon-controller.md`.

## Target

```
Bot (Grok Bot cloud) ── /mcp, role token ──► grok-bot ──► NOC room: the terminal shows each command and its output
                                                │
                                                └─► lab-api  POST /controller/cli
                                                      │  allow-list (wall 1), audit, size cap
                                                      └─ SSH as the role's controller user ──► controller (NACM: wall 2)
                                                                                                │
                                                                      NETCONF over SSH :830 ────┴──► 10 PEs (Clixon)
lab-api ── RESTCONF, unchanged: dry runs, approved changes, scenarios, reset ─────────────────────► 10 PEs
```

## Tasks

1. **The PEs speak NETCONF over SSH.**
   - Add OpenSSH's server to the netsim-pe image, run by supervisord, with the NETCONF subsystem on port 830:
     `Subsystem netconf /opt/clixon/bin/clixon_netconf -f /etc/clixon.xml` (the binary is in the image; check the
     flags).
   - One user, `controller`: key login only, no password, no shell, allowed to reach the Clixon backend socket
     (group `clicon`). NACM on the PEs stays off.
   - The controller's public key reaches the PEs without being baked into the image, and its private key never
     leaves the controller. Generate the keys locally, never commit them, and keep them across
     `make down && make up`.
   - Commits over NETCONF are attributed to the SSH user. Check that simd's config-change events and the
     `ciscoConfigManEvent` trap (TerminalUser) handle the new user.
   - `CLICON_NETCONF_MONITORING` is on by default (RFC 6022), so the controller should be able to fetch the
     `netsim-device` YANG from the PEs. Verify it.

2. **The controller (`controller/`).**
   - Build a **released** Clixon controller, pinned by tag and commit as `devices/Dockerfile` pins Clixon. It may
     need other Clixon and CLIgen versions than the PEs' 7.9.0: pin the ones it needs and record why.
   - Configure the ten PEs from `contract/topology.yaml` (not by hand), connect them at start, and pull their config
     once they are healthy.
   - `CLICON_AUTOLOCK=true` (no shared candidate). NACM internal, `CLICON_NACM_CREDENTIALS=exact`.
   - Users, both unix and NACM: `noc-engineer`, `network-engineer`, `noc-manager` (read only), `lab-api` (pulls),
     and an admin for humans. A role user's SSH login (labnet only) goes straight into the controller CLI, never a
     shell.
   - Make targets `controller-build`, `controller-up`, `controller-shell` (the CLI as admin) and
     `controller-test`, wired into `make build` and `make up` like the other components.
   - **Verify on the running controller, and record in the report:** the exact CLI syntax of the commands in task 3;
     which RPCs `show devices <host> check` and `show devices <host> diff` use; and whether `check` says "in sync"
     again when a router's config goes back to the controller's copy, or only after a pull.

3. **lab-api: `POST /controller/cli`** (lab token, `X-Lab-Actor`, body `{role, command}`).
   - **Allow-list (wall 1),** the same for all three roles to start:
     - `show devices`
     - `show devices <host>`, `show devices <host> check`, `show devices <host> diff`
     - the controller's copy of one router's config, whole or a subtree (find the syntax)

     `<host>` must be one of the ten lab routers. Refuse everything else before it reaches the controller: other
     commands, a second command (`;` or a newline), pipes and redirects, control characters, more than 200
     characters. A refusal lists the allowed commands.
   - **NACM and RPC arguments:** NACM matches an RPC by its name and data by its path, never by an RPC's arguments.
     If `check` or `diff` use the same RPC as a real `pull`, do not grant that RPC to the role users: run those two
     commands as lab-api's controller user (the actor is still the role) and say so in the contract.
   - **Run it** in the role user's controller CLI session over SSH. Pass the command on stdin, never through a
     shell. Timeout 15 s, output capped at 16 KiB, ANSI codes stripped.
   - **Answer** `{role, user, command, allowed, output, truncated, duration_ms}`: 403 for a refusal, 503 when the
     controller is down. Audit every call, refusals included.
   - **Resync:** after `labctl reset`, and after lab-api has applied and verified an approved change, the controller
     pulls the routers concerned (as `lab-api`), so it shows them in sync again. Nothing else pulls.
   - **`labctl status`** gets a controller line: routers connected, in sync, out of sync.
   - Export `lab/openapi.json` again. Unit tests for the allow-list: every refusal rule and injection attempts.

4. **grok-bot: the tool and the terminal.**
   - **Tool** `controller_cli(command)` in all three roles' tool sets: a read (`readOnlyHint` true, like the other
     direct reads in `roles.py`) at a new station, `terminal`. Its result is lab-api's answer.
   - **The room:** a terminal, a monitor on its own small desk labelled CONTROLLER, at the new station in
     `web/js/room.js`, where the room has space. When a robot calls the tool it walks there; the monitor types
     `<user>@controller> <command>`, then shows the first lines of the output; a refusal shows in red. Clicking the
     terminal opens the session's transcript in the side panel: who ran what, when, and the full output. Use the
     room's own font and colours.
   - **Events:** the room gets what it needs from the existing event stream (`/api/stream`); keep the output in the
     event capped.
   - **Skills** (`bot/grokbot-app/`):
     - `SKILL.md`: the tool, the allowed commands, and what out of sync means.
     - Network Engineer: when drift is suspected, `show devices <host> check`, then `show devices <host> diff`, and
       quote the diff in the root cause.
     - NOC Engineer: `show devices` as part of triage.
     - Every skill: if the tool says the controller is unavailable, carry on without it.
   - `bot/README.md` and `bot/grokbot-app/README.md`: the tool and the terminal.

5. **Tests.**
   - **Unit:** the allow-list, lab-api's endpoint (with a fake SSH session), the tool in each role's set with
     `readOnlyHint`, the room's assets.
   - **Controller (`make controller-test`, on the running lab):**
     - after `labctl reset`: ten routers connected, all in sync;
     - after `labctl scenario apply vcid-typo`: spoke-1 is out of sync and its diff shows `vc-id` 2010 where the
       controller has 2001; every other router is in sync;
     - **NACM without lab-api:** an SSH session as `network-engineer` that tries an edit and commit, a `pull` or a
       `push` is denied by the controller.
   - **Bot e2e** (the routine sink and the scripted role player, as today):
     - the drift case calls `controller_cli` in the Network Engineer's steps, the room's event stream carries it,
       the fix still closes the incident, and spoke-1 is in sync afterwards;
     - refusals: `pull spoke-1`, `commit push`, a `set` command, `show devices; pull`, an unknown host;
     - `docker stop controller`: the tool answers "unavailable" and the drift case still closes; after
       `docker start controller` it reconnects.

6. **Docs.**
   - **Contract:** a controller section (address, ports, users and their rights, the allow-list, what "in sync"
     means), plus §3, §4, §11, §12 and the change log.
   - **Other docs:** `docs/RUNBOOK.md` (the controller, `make controller-shell`, reading out of sync); the root
     `README.md` (architecture diagram and tables); `prompts/README.md` (add the report link to this session's row).
   - **Report** `docs/reports/10-clixon-controller.md`: what was built, the verified controller behaviour (task 2),
     the deviations, the cross-component edits, and how to verify. **At the top:** what the user must do in the Grok
     Bot app: re-add the changed skills to each Bot, and check that `controller_cli` runs without an approval card.

## Acceptance criteria
- `make build up` brings the controller up with ten routers connected and in sync (`labctl status`).
- After `vcid-typo`, `controller_cli("show devices spoke-1 check")` reports spoke-1 out of sync and the diff shows
  the typo. After the approved fix, spoke-1 is in sync again.
- The roles can run only the allow-listed reads: lab-api refuses everything else, and the controller's NACM denies
  edits, commits, pulls and pushes to the role users even without lab-api. Tests prove both.
- The NOC room shows each command and its output on the terminal as it happens.
- `/mcp` is still the Bots' only way in, and the role tokens are still the only secrets that leave the computer.
- With the controller stopped, the rest of the lab works as before.
- `make test`, `make lint` and `make e2e` pass.

## Working rules
- The decisions above are fixed; ask the user before changing any of them.
- **Never page the user's real routines** (each page is a paid run): `uv run botctl mute`, or the routine sink
  (`make bot-sink-up`), before any scenario or e2e. Put back what was there when you finish.
- **Do not change existing `.env` values** (live role tokens, the tunnel host). New variables go in `.env.example`,
  and `make env` appends them.
- **Ask before `make clean`:** it deletes every lab volume, the NetBox and Zabbix databases included.
- Stay inside this repository. The meetup deck, runbook and videos in the parent folder are not yours.
- Verify the controller's behaviour on the running controller before relying on it, and record what you found.
- After any bot change, rebuild the container: `docker compose build grok-bot && docker compose up -d grok-bot`.
- Finish with a full `make e2e` and fix every failure before calling it done.
- Commit only when the user asks. Commit messages follow the repo's style (`git log`), with no co-author trailers
  and no tool attribution lines.
