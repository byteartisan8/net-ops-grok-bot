# Session 10 — Clixon controller: a controller in front of the routers, and a terminal the NOC roles type into

**Date:** 2026-10-24. **Prompt:** [`prompts/10-clixon-controller.md`](../../prompts/10-clixon-controller.md) (the user's
decisions of 2026-10-24). **Branch:** `feature/clixon-controller`, uncommitted until the user asks.

## First: what you do in the Grok Bot app

Every role has a new tool, `controller_cli`, and the four skills changed. In each of the three Bots:

1. **Re-add the skills:** the shared [`bot/grokbot-app/SKILL.md`](../../bot/grokbot-app/SKILL.md) and the Bot's role
   skill (`skills/noc-engineer/`, `skills/network-engineer/` or `skills/noc-manager/`). They teach the tool, the
   allowed commands, what out of sync means, and to carry on without the controller when it is unavailable.
2. **Check that `controller_cli` runs without an approval card.** It is marked read only (`readOnlyHint`), like
   `zabbix_get`; if the app still asks, set it to **Always allow** in the Bot's connector settings (it cannot change
   anything: lab-api's allow-list and the controller's NACM both stand in the way). Seen once already: during the
   final e2e your real Network Engineer, paged for INC-0012, ran `show devices hub-b check` and `show devices spoke-1
   check` on its own (13:53), from the tool's description alone, before any skill was re-added. Whether the app asked
   you first cannot be seen from here.

Nothing else changes for the app: same `/mcp`, same role tokens, same routines.

**Result:**
- **The controller** (`controller`, 172.30.10.70) runs the Clixon controller **1.9.0** on the PEs' Clixon/CLIgen
  **7.9.0**, manages the ten PEs over NETCONF over SSH and keeps a copy of each router's config. `make build up`
  brings it up with ten routers connected and in sync (`labctl status`: `controller: 10/10 connected, 10 in sync`).
- **A second drift signal:** after `vcid-typo`, `show devices spoke-1 check` says `Error: device out-of-sync` and the
  diff shows `vc-id` 2010 on the router where the controller has 2001; every other router is in sync. After the
  approved fix, lab-api has the controller pull spoke-1 and it is in sync again.
- **Read only, two walls:** lab-api's allow-list refuses anything but seven read commands before they reach the
  controller; the controller's NACM refuses the role users any edit, commit, pull, push or connection change even
  without lab-api (`make controller-test` proves both).
- **The NOC room** has a CONTROLLER terminal: the robot walks there, the CRT types `<user>@controller> <command>` and
  the first lines of the answer (refusals in red, an unavailable controller in amber); a click opens the transcript.
- **Optional:** with `controller` stopped, the drift case still goes from page to closed; started again, the
  controller connects the ten routers by itself in about 5 s.
- **No new door, no new secret outside:** the controller and the PEs' SSH listen on labnet only, nothing is
  published; lab-api holds the controller's credentials (SSH keys on volumes, no `.env` variable); `/mcp` and the
  role tokens are unchanged.
- **Tests:** `make test`, `make lint` and a full `make e2e` (bot e2e, the controller's live tests, lab e2e) pass on
  the rebuilt lab ([Checks](#checks)).

---

## What was built

```
Bot (Grok Bot cloud) ── /mcp, role token ──► grok-bot ──► NOC room: the CONTROLLER terminal shows command and output
                                                │
                                                └─► lab-api  POST /controller/cli
                                                      │  allow-list (wall 1), audit, 16 KiB cap, 15 s
                                                      └─ SSH :22 as the role's controller user ──► controller (NACM: wall 2)
                                                                                                    │
                                                                      NETCONF over SSH :830 ────────┴──► 10 PEs (Clixon)
lab-api ── RESTCONF, unchanged: dry runs, approved changes, scenarios, reset ─────────────────────────► 10 PEs
```

### 1. The PEs speak NETCONF over SSH

| Piece | Where | What |
|---|---|---|
| sshd | `devices/Dockerfile`, `devices/supervisord.conf`, `devices/sshd_config` | OpenSSH server under supervisord on :830; `Subsystem netconf /opt/clixon/bin/clixon_netconf -f /etc/clixon.xml -l e` (the flags of the image's binary, checked in a running PE) |
| user `controller` | `devices/Dockerfile`, `devices/netconf-session` | key login only (`AuthenticationMethods publickey`, password `*`: none, but not locked, which sshd would refuse even for keys), group `clicon` (the backend socket), login shell and `ForceCommand` a NETCONF session; `AllowUsers controller@172.30.10.70` |
| keys | `devices/entrypoint.sh`, generated `devices/compose.yaml` | each PE's host key is made once on its volume; the controller's public key comes from the shared volume `netsim_controller-keys` (read only), never baked into the image |
| health | `devices/simd/.../healthcheck.py` | also checks that something listens on :830 (from `/proc/net/tcp`: a probe connection would log a line in sshd every 5 s) |

RESTCONF, simd and the scenarios are unchanged; NACM on the PEs stays off. Commits over NETCONF are attributed to the
SSH user: simd's `config-change` event says `changed_by: controller` and the `ciscoConfigManEvent` trap's
`ccmHistoryEventTerminalUser` is `controller` (checked with a NETCONF edit and commit from the controller; a live test
keeps checking it). The controller fetches the routers' YANG from the routers (RFC 6022, `CLICON_NETCONF_MONITORING`
on by default): `show devices spoke-1 yang` lists `netsim-device 2026-10-18`.

### 2. The controller (`controller/`)

- **Image** `netsim/clixon-controller:1.9.0` (163 MB): CLIgen, Clixon and the controller built from release tags with
  their commits checked, as `devices/Dockerfile` pins Clixon. Controller **1.9.0** (2026-10-15, the newest release)
  compiles with `-Werror` against the PEs' Clixon/CLIgen **7.9.0** commits and runs on them, so no other Clixon
  version was needed. Its `make install` needs a `clicon` user at build time (added to the build stage).
- **Devices from the topology:** `controller/startup.py` (`make controller-config`; `controller-check` verifies)
  generates `controller/startup_db` from `contract/topology.yaml`: one device per PE at its mgmt IP, through the
  profile `netsim-pe` (NETCONF_SSH, port 830, user `controller`, strict host key, YANG bound from the device), device
  groups `north` and `south`, and the NACM rules.
- **Settings** (`controller/clixon/netsim.xml`, read after the controller's own `controller.xml`):
  `CLICON_AUTOLOCK=true`, NACM internal and always on, `CLICON_NACM_CREDENTIALS=exact`, recovery user `root`, no
  RESTCONF, no services daemon (`CONTROLLER_ACTION_COMMAND` empty: clixon-pyapi is not installed and services are a
  later step), CLI output as curly-brace text.
- **Users** (unix and NACM; login shell = the controller CLI): `noc-engineer`, `network-engineer`, `noc-manager`
  (read only), `lab-api` (also pulls and (re)connects, never pushes), `admin` (people, `make controller-shell`),
  `root` (the container itself: the recovery user). Table and rules: contract §9.7, `controller/README.md`.
- **Processes** (supervisord): `clixon_backend` (a fresh datastore from `startup_db` at every start), `sshd` (:22,
  only the four lab-api users from 172.30.10.50 with lab-api's key, `ForceCommand` the CLI, no forwarding, no TTY),
  `controller-agent` (learns each PE's host key on first contact, trust on first use, and connects every PE once the
  backend is up, within 10 minutes of the start; it never pulls).
- **Keys:** made where they are used, never moved; only public halves travel through `netsim_controller-keys`
  (`controller.pub` for the PEs, `controller-host.pub` that lab-api pins, `lab-api.pub` that the controller's sshd
  authorizes). They survive `make down && make up`.
- **Make targets:** `controller-build`, `controller-up`, `controller-down`, `controller-logs`, `controller-shell`,
  `controller-config`, `controller-check`, `controller-test`; `make build` and `make up` include the controller (it
  is in the root `compose.yaml`), `make e2e` runs `controller-test`.

### 3. lab-api: `POST /controller/cli`, the status, the resync

- **`POST /controller/cli`** (lab token, `X-Lab-Actor`, `{role, command}`): `allow()` in
  `lab/src/netsim_lab/controller.py` (wall 1) refuses with 403, listing the allowed commands, anything but:

  | Command | Runs as |
  |---|---|
  | `show connections` | the role |
  | `show connections <router>` [`detail`] | the role |
  | `show devices <router> check` | `lab-api` |
  | `show devices <router> diff` | `lab-api` |
  | `show configuration devices device <router> config` [`<path>`] | the role |

  It also refuses a second command (`;`, a newline), pipes and redirects, other shell and glob characters, control
  characters, non-ASCII, more than 200 characters, a path word that is not a config name or key, more than 12 path
  words and any router that is not one of the ten. An allowed command runs in one SSH session as the role's own
  controller user (`ssh -T`, the command on stdin, never through a shell; the users' login shell and sshd's
  `ForceCommand` are the CLI), 15 s timeout, output capped at 16 KiB, the CLI's echo of the command, ANSI codes and
  Clixon's log prefixes removed. Answer `{role, user, command, allowed, output, truncated, duration_ms}`; 503 when the
  controller is down. Every call is audited (`controller.cli`), refusals included.
- **`GET /controller/status`** (cached 5 s, `?max_age=0` for a fresh one): routers connected, in sync, out of sync,
  closed, and per router its state and diff, from `show connections` and `show devices * diff` as `lab-api`.
- **Resync** (`POST /controller/resync`, and lab-api on its own): `pull` a connected router, `connection open` a
  closed one, as `lab-api`, audited as `controller.resync`: after `labctl reset` / `POST /reset` (every router; reset
  step 6, through lab-api when labctl runs it), after an approved change is applied and verified (that router; the
  change's result carries `controller`), and when a router lab-api powered on is healthy again (field repair). The
  controller being down never fails what lab-api did: the resync is skipped, audited with `ok: false`.
- **`labctl status`** has a controller line under the header (`controller: 10/10 connected, 10 in sync`, or `out of
  sync: spoke-1`, or `unavailable (...)`), from lab-api (which holds the credentials); with `--watch` a thread
  refreshes it every 5 s so a frame never waits on the controller. `--json` adds `controller`.
- **Container:** the lab-api image gains `openssh-client`; its compose file mounts `netsim_controller-keys`. lab-api
  makes its key once on its own volume. `lab/openapi.json` re-exported.

### 4. grok-bot: the tool and the terminal

- **Tool** `controller_cli(command)` in all three roles' tool sets: a direct read (`readOnlyHint` true), station
  `terminal`. It returns lab-api's answer plus a one-line `summary` for the room's caption (`in sync`, `out of sync`,
  `differs from the controller's copy (4 lines)`, `10 OPEN`, ...). A refusal comes back as an error carrying
  lab-api's answer (the allowed commands); an unavailable controller as an error that names the reads to use instead
  and says to carry on without it.
- **The room:** a CRT on its own small desk labelled CONTROLLER (`web/js/terminal.js`, wired into `room.js`), in the
  room's 3x5 pixel font and palette, no build step, no external file. The robot walks there; the CRT types
  `<user>@controller> <command>` (32 characters a second), then scrolls in the first lines of the output; refused in
  red with the reason, unavailable in amber. Clicking it (or the side panel's new **Terminal** tab) shows the
  transcript: who ran what, when, and the full output.
- **Events:** each call is a `terminal` event on the existing `/api/stream` with the output's first 14 lines (160
  characters each) and the line count; the full output goes to the transcript (SQLite table `terminal`, the newest
  500 kept; `GET /api/terminal`), and `GET /api/state` carries the last 20 so a reload shows the screen again.
- **Skills** (`bot/grokbot-app/`): the shared skill has the controller in "The lab", the tool in the tools table, a
  section on reading its answers (what out of sync means, `-` and `+` in a diff) and the unavailable rule; the
  Network Engineer checks a suspected router, then diffs it, and quotes the diff in the root cause; the NOC Engineer
  looks at `show connections` while triaging; every role carries on without an unavailable controller.
  `bot/README.md` and `bot/grokbot-app/README.md` describe the tool, the terminal and the app-side steps above.

### 5. Tests

- **Unit** (`make test`): the allow-list (every refusal rule and injection attempts such as `show devices; pull`,
  `show connections && pull spoke-1`, `$(...)`, backquotes, a newline or carriage return, `|`, quotes, ESC and NUL,
  `../etc` as a path word, unknown routers), output cleaning, the parsers, lab-api's endpoints with a fake SSH session
  (runs-as, 403 and 503, the audit, a timeout, the output cap, lab-api's key made once and the host key pinned), the
  resync (pull or connect, never failing what lab-api did); the reset and apply hooks are covered end to end (the
  controller's tests assert each reset's resync, the drift e2e the resyncs after the applies); the controller's files (startup datastore current and correct, NACM rules, sshd settings, the image's pins, the
  healthcheck); the PE image and compose rendering; grok-bot's tool in each role's set with `readOnlyHint`, its
  errors, the `terminal` event and transcript, the page's assets, and that the skills teach the tool.
- **Controller, on the running lab** (`make controller-test`, part of `make e2e`): after a reset ten routers
  connected and in sync; `vcid-typo` puts exactly spoke-1 out of sync with `-  vc-id "2010";` / `+  vc-id "2001";`,
  and putting 2001 back by hand makes it in sync again without a pull (the sync time does not move); **NACM without
  lab-api:** an SSH session as `network-engineer` that tries an edit and `commit`, `commit local`, `pull`, `push`,
  `commit push`, `connection close` or a check (a transient pull) is denied, and `lab-api`'s user cannot push; a
  session is the CLI, never a shell; only lab-api's key gets in, and only the controller's key reaches a PE's
  NETCONF; `clixon_cli -U admin` as `noc-engineer` is refused; the YANG came from the routers; a NETCONF commit is
  attributed to `controller` in simd and in the trap.
- **Bot e2e** (routine sink and scripted role player, as before): the office test checks the tool in every role's
  set (read only), the three reads as each role, the five refusals (`pull spoke-1`, `commit push`, a `set` command,
  `show devices; pull`, an unknown router), the audit (actors, 403s) and the room's event stream carrying all eight
  sessions in order; the drift case (fake redundancy) has the Network Engineer check spoke-2, hub-a and hub-b (out
  of sync) and hub-c (in sync), diff spoke-2, quote the diff in the root cause, and after the approved fixes the
  controller shows all ten in sync (pulled after each apply, audited); `test_70_controller_down` stops the
  controller, runs `vcid-typo` from page to closed with the tool answering "unavailable" (the resync after the fix
  is tried, audited as failed, and the change stands), then starts it and waits for ten routers in sync.

---

## The controller, verified on the running controller

What the prompt asked to verify before relying on it (controller 1.9.0, Clixon 7.9.0, the lab's PEs):

| Question | Found |
|---|---|
| `show devices` / `show devices <host>` | **Incomplete commands** in 1.9.0 (`CLI syntax error: "show devices": Incomplete command`). The per-router connection view is `show connections` (table: Name, State, Time, Logmsg) and `show connections <router> detail` (`conn-state`, `conn-state-timestamp`, `sync-timestamp`, `netconf-framing-type`). |
| `show devices <host> check` | Prints nothing when the router equals the controller's copy, `Error: device out-of-sync` when not; the CLI exits 0 either way. `show devices * check` checks every router. |
| `show devices <host> diff` | `<host>:` then the changed subtree: `-` lines what the router runs now, `+` lines the controller's copy, leaf values quoted (`-           vc-id "2010";`). Nothing when in sync. `show devices * diff`: every router that differs. |
| The controller's copy of one router | `show configuration devices device <host> config` (whole) and `... config l2vpn xconnect SVC-0001` (a subtree; any path of the router's YANG). |
| RPCs behind `check` and `diff` | Both send **`config-pull` with `transient=true`** (the controller fetches the router's config without storing it; `send_pull_transient()` in `controller_cli_callbacks.c`), then `datastore-diff`; around the transaction the CLI calls `device-rpc-result` and, on failure, `transaction-error`. A real `pull` sends the same `config-pull` without `transient`. NACM matches RPC names only, so the roles cannot have `check`/`diff` without being able to pull: lab-api runs them as `lab-api`. |
| In sync again without a pull? | **Yes.** Putting `vc-id` 2001 back on spoke-1 makes `check` print nothing and `diff` empty at once; the `sync-timestamp` stays the same. A pull is only needed to make the controller accept a new config as its copy. |
| When does the copy change? | At every **connect** (`controller_device_state.c`: "Unconditionally sync" after the schemas) and every **pull**. So a controller restart, or a router reconnecting, takes the router's config as it is then. |
| What a read-only CLI user needs | Beyond `get` and `get-config`, the CLI itself calls `clixon-cache` (its autocli cache) and `nacm-autocli-filter-get` at start, `create-subscription` (it follows transaction notifications), and `config-path-info` (resolving the path) and `translate-format` (printing the subtree) for `show configuration devices device <host> config <path>`. Found one at a time: without each, the role's CLI failed to start or to answer that read with `access-denied default deny` (`exec-default deny`). |
| NACM refuses the role users | `edit-config` ("Editing configuration: application access-denied"), `commit`, `commit push`, `pull`, `push`, `connection close`: all "access denied"; nothing changed on the controller or the router. |
| Impersonation | `clixon_cli -U admin` as `noc-engineer`: `User noc-engineer credential not matching NACM user admin` (`CLICON_NACM_CREDENTIALS=exact`). |
| The PEs' YANG | Fetched from the routers (RFC 6022): `show devices spoke-1 yang` lists `netsim-device 2026-10-18`. |

## Deviations from the plan

- **The allow-list's first two commands.** The prompt's `show devices` and `show devices <host>` do not exist as
  commands in controller 1.9.0 (incomplete commands). Their content, each router's connection, is `show connections`
  and `show connections <router> [detail]`, which are on the allow-list instead. The other three are as asked.
- **`check` and `diff` run as `lab-api`** (the prompt's own rule, since they send `config-pull`): the controller
  user in the answer is `lab-api`, the audit's actor is still the role's (`grok-bot/NETENG`).
- **Two more lab-api endpoints:** `GET /controller/status` (for `labctl status`; labctl has no controller
  credentials) and `POST /controller/resync` (for `labctl reset` when it runs on the host), both behind lab-api like
  `POST /controller/cli`.
- **A reconnect after a field repair.** The prompt resyncs after a reset and after an applied change, "nothing else
  pulls". A router that was powered off is CLOSED in the controller when it comes back, and the agent only connects
  routers in the first 10 minutes after the controller starts, so lab-api also reconnects a router it powered on
  (`POST /faults`), once it is healthy. A connect is the controller's sync, so this takes the router's config as the
  copy; power does not change config, so the copy is the same unless the router had drifted before it was powered
  off.
- **Limits of the copy** (from the controller, not chosen): a controller restart (also `make down && make up`) takes
  every router's config as it is at that moment, so earlier drift no longer shows as out of sync until the next
  reset; NetBox's drift check and Zabbix still see it. A router powered on outside lab-api (`labctl device start`,
  `labctl fault clear-all`: Docker directly) stays CLOSED until the next reset or `POST /controller/resync`.
  Recorded in contract §9.7 and the runbook.
- **The controller's e2e lives in `controller/tests/integration/`** (`make controller-test`, part of `make e2e`)
  rather than in `lab/e2e/`; lab e2e is unchanged.
- **A test, not the tool name:** `bot/e2e/test_10_office.py` refused any role tool whose name contains `control`
  (meant for grok-bot's operator controls), which `controller_cli` does. The name is the prompt's, so the test now
  exempts that one tool by name (its read-only marking is checked in the same file).

## Edits outside `controller/`

| Path | Owner | Change |
|---|---|---|
| `compose.yaml`, `Makefile`, `pyproject.toml` | 01-foundation | include `controller/compose.yaml`; `FRAGMENTS`, `controller-%` placeholder, include `controller/controller.mk`; `controller` in `testpaths` |
| `contract/topology.yaml`, `tests/test_topology.py`, `tests/test_scaffolding.py` | 01-foundation | the controller under `infrastructure:` (172.30.10.70, container port 22); the tests know it |
| `devices/Dockerfile`, `devices/Dockerfile.dockerignore`, `devices/supervisord.conf`, `devices/entrypoint.sh`, `devices/sshd_config` (new), `devices/netconf-session` (new) | 02-device-simulator | NETCONF over SSH on :830 (section 1) |
| `devices/simd/src/netsim_simd/devtool.py`, `devices/compose.yaml` (regenerated), `devices/simd/src/netsim_simd/healthcheck.py`, `devices/simd/tests/test_compose_and_image.py` | 02-device-simulator | the shared key volume on every PE, read only; the :830 health check; their tests |
| `lab/src/netsim_lab/controller.py` (new), `lab/src/netsim_lab/api/app.py`, `api/models.py`, `reset.py`, `cli.py`, `labapi_client.py`, `lab/tests/test_controller.py` (new), `lab/openapi.json` | 05-integration | the controller client and allow-list, the three endpoints, the resync hooks, `labctl status`'s line, tests |
| `lab/Dockerfile`, `lab/compose.yaml` | 05-integration | `openssh-client`; the shared key volume, `CONTROLLER_KEYS_DIR` |
| `bot/src/netsim_bot/tools.py`, `roles.py`, `labapi.py`, `engine.py`, `store.py`, `api/app.py` | 07-grok-bot | the tool, its lab-api call, the `terminal` event and transcript, `GET /api/terminal` |
| `bot/src/netsim_bot/web/js/terminal.js` (new), `room.js`, `main.js`, `panel.js`, `index.html`, `css/noc.css` | 07-grok-bot | the CONTROLLER terminal, the Terminal tab |
| `bot/tests/test_controller_cli.py` (new), `bot/tests/test_settings_tools_web.py` | 07-grok-bot | unit tests; the new station in the stations list |
| `bot/e2e/conftest.py`, `test_10_office.py`, `test_30_drift.py`, `test_70_controller_down.py` (new) | 07-grok-bot | the event-stream reader; the controller in the office, drift and controller-down cases |
| `bot/grokbot-app/SKILL.md`, `skills/*/SKILL.md`, `bot/grokbot-app/README.md`, `bot/README.md` | 07-grok-bot | the skills and docs |
| `docs/CONTRACT.md` | all | §1, §2, §3, §4, §9.7 (new), §11, §12, change log |
| `docs/RUNBOOK.md`, `README.md`, `prompts/README.md` | all | the controller, `make controller-shell`, reading out of sync; diagram and tables; this report's link |

`.env.example` is unchanged: the controller needs no variable and no password.

## Timings

From the final `make e2e` and the live checks after it (see [Checks](#checks)):

| Path | Measured |
|---|---|
| controller started → ten routers connected, all in sync (bot e2e, `test_70_controller_down`) | 4.69 s (4.76 s in an earlier run) |
| a role's controller read through lab-api (`show connections`, `check`, `diff`; one SSH session each) | 0.12-0.13 s |
| `GET /controller/status` (connection table + `show devices * diff`, as `lab-api`) | 0.25-0.26 s |
| the resync of one router after an applied change (`pull`) | 0.14 s |
| `labctl reset` with one fault, the controller resync included → green | 5.0 s |

Every timed path of the bot and lab e2e stays within its budget with the controller in the lab, e.g. fake redundancy
→ NOC Engineer page 16.1 s (budget 30 s), AC fault → interface problem 4.2 s (10 s), trap → walk re-polled median
3.7 s, max 5.7 s (5 s median, 7 s max).

## Checks

All on 2026-10-24, on the running lab (`make build up` first, so every container ran an image built from this tree):

| Check | Result |
|---|---|
| `make test` | 750 passed (29 s) |
| `make lint` | ruff, ruff format, `pyang --strict`, yanglint: clean |
| `make build up`, then `labctl status` | every image rebuilt and every changed container recreated; `controller: 10/10 connected, 10 in sync`, Zabbix OK, `labctl drift`: no findings |
| `make e2e` (about 22 min) | bot e2e **12 passed** (8 min 41 s), `controller-test` **7 passed** (25 s), lab e2e **23 passed** (12 min 42 s), including `make down && make up` (drift survives, the controller comes back) and the final green-and-clean check |
| live: `vcid-typo`, then the fix | `controller_cli` as the Network Engineer: `show devices spoke-1 check` → out of sync, `diff` → `-  vc-id "2010";` / `+  vc-id "2001";`, hub-b in sync; `apply-intent` dry run and confirm → verdict ok, the change's `controller` field shows the pull of spoke-1, `labctl status` 10 in sync, audited as `controller.resync` ("apply-intent applied by ...") |

The first bot e2e run failed one test: the office test's list of words no role tool may contain caught `controller_cli`
(`control`). Fixed in the test ([Deviations](#deviations-from-the-plan)); the next full run passed.

During that run's lab e2e, maintenance mode and the mute were switched off from the NOC room (13:50, `noc-page`), so
the lab e2e's later faults paged the real routines: 13 pages for INC-0012 to INC-0014, which the Bots worked (two
closed, INC-0014 left open with its field ticket CHG-0018 approved from the room at 14:02). The e2e passed regardless.
Pages are on again, as they were before this session; the routine sink is removed.

Not done: a run from empty volumes (`make clean`, then `make env net build up seed e2e`). `make clean` deletes the
NetBox and Zabbix databases, so it waits for your go-ahead. What a clean run adds for this session, the first start
with no keys anywhere, did happen on this lab during the build: the controller made its keys, the PEs took its public
key from the shared volume and all ten routers connected; lab-api made its own key on its first call.

## How to verify

```bash
make build up                                   # the controller builds and starts with the rest
uv run labctl status | head -2                  # controller: 10/10 connected, 10 in sync
uv run labctl scenario apply vcid-typo
uv run labctl status | sed -n 2p                # ..., 9 in sync, out of sync: spoke-1
make controller-shell                           # show devices spoke-1 diff -> -  vc-id "2010"; / +  vc-id "2001";
uv run botctl call --role network-engineer controller_cli '{"command": "show devices spoke-1 check"}'
uv run botctl call --role network-engineer controller_cli '{"command": "pull spoke-1"}'      # refused, with the list
uv run labctl reset                             # golden again, the controller resynced: 10 in sync
docker stop controller && uv run labctl status | sed -n 2p     # controller: unavailable (...); the rest works
docker start controller                         # ten routers connected again within seconds
make controller-test                            # the controller's own checks, NACM without lab-api
make test lint e2e                              # everything (~22 min for e2e)
```

Open the NOC room (<http://127.0.0.1:8095>) while running the `botctl call` lines: the Network Engineer's robot
walks to the CONTROLLER terminal and the CRT shows the command and its answer; click it for the transcript.
