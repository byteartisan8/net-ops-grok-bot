# Session 07 — Grok bot report (grok-bot: the NOC bot and the NOC room)

**Date:** 2026-10-20. **Scope (agreed in conversation, no prompt file):** build the Grok bot on lab-api, the bot's tool
surface since session 05. The user asked for:
- a visualization of the bot like the "AI agents working in a room" animations going round social media (Pixel
  Agents, Star-Office-UI), with a big monitor in the room showing the Zabbix dashboard, plus "other things that are
  cool";
- a bot that reacts to Zabbix problems on its own;
- the bot to use their installed, subscribed Grok if possible (model and configuration: my choice).

Session 06 was committed first (`9d47943` on `session-06-topology-v2`, after `make test` 504 passed and
`make lint` clean). This session's work is on branch `session-07-grok-bot`, uncommitted.

**Result:** grok-bot runs as a lab container (`172.30.10.60`, NOC room at <http://127.0.0.1:8095>). It notices Zabbix
problems on its own, opens incidents, investigates, proposes fixes as lab-api dry runs and, after a human approves,
applies them make-before-break. Fixes are verified through simd and Zabbix. Field repairs go through a field
technician bot.
- **Grok brain:** built, covered by unit tests against the documented xAI Responses API, and the container reaches
  api.x.ai. It **has not run against the live API yet: there is no `XAI_API_KEY`**. Until a key is added, the
  deterministic rules brain runs; see [Grok access](#grok-access-what-was-found).
- **Checks:** `make test` 574 passed (504 before + 70), `make lint` clean, every generated file current.
  `make bot-e2e` 6/6 on the live lab (327 s). The full **`make e2e` passed**: bot 6/6, then lab 23/23 with every
  timing within budget, 1249 s.

---

## Grok access: what was found

| Thing on this machine | What it is | Usable by the lab bot? |
|---|---|---|
| `/opt/Grok Bot` (`grok-bot-0.66.0` RPM, vendor SpaceXAI, built with Cursor's agent stack), signed in | **Grok Bot**, SpaceXAI's desktop client for its cloud agent (beta since 2026-08-11). Bots run on a cloud machine; the app is chat, review and approvals | **No.** The docs list no API, webhook or event trigger for sending it work, only schedule-like routines, so Zabbix cannot wake it. Its plans (X/SuperGrok/Cursor tiers) meter Grok Bot usage and include no xAI API access |
| `~/.local/bin/cursor-agent` 2026.05.09 | Cursor's CLI agent (headless `-p` mode) | No: `cursor-agent models` answers "No models available for this account" |
| xAI API (console.x.ai) | pay-as-you-go API, billed separately from subscriptions | **Yes**: what grok-bot uses. **Needs a key from the user** (creating accounts and keys is the user's step) |

> **Correction (session 08):** the app does have webhook-triggered routines, custom MCP servers and commands on the
> user's computer; its UI text says so. That does not change the brain's choice, because a routine still cannot be
> the in-process function-calling loop. It does let the app join the NOC as an outside agent: see
> [08-grokbot-app.md](08-grokbot-app.md).

So the bot calls the **xAI Responses API** (`POST https://api.x.ai/v1/responses`, function calling, documented at
docs.x.ai on 2026-10-20) with plain httpx. The container reaches it (`GET /v1/models` → 401 without credentials).

**Model choice: `grok-4.7`.** It is xAI's flagship on 2026-10-20 ("the most capable model"; agentic tool calling,
500k context), priced $2.00 input / $0.50 cached / $6.00 output per million tokens. A diagnosis is the part that
must be right, and the lab produces a few incidents an hour, so the strongest model is cheap enough: an
investigation of 8-15 tool calls should cost roughly $0.10-0.40 (an estimate from token counts, not measured yet).
`GROK_MODEL` takes any listed model (e.g. `grok-4.3` at $1.25/$2.50). The reasoning effort is left at the model's
default (`GROK_REASONING_EFFORT` empty); a model that refuses the field is retried without it. Spend is capped per UTC
day (`BOT_DAILY_BUDGET_USD=5`); after that the rules brain takes over.

**To switch Grok on:** put the key in `.env` (`XAI_API_KEY=xai-...`), then run `uv run botctl grok-check`. The check
does one function-calling round trip and also sends the responders' real tool schemas, so a schema xAI rejects shows
up there and not in the first incident. Then run `make bot-eval` (opt-in, costs money, see [Tests](#tests)) to grade
Grok on six scenarios.

---

## What was built

```
lab-api (.50) ◀── polls every 2 s: problems, services, devices ── grok-bot (.60)  ──▶ api.x.ai (Grok, function calling)
      ▲                                                             │  POLL (watcher) → incident
      │ dry runs (plan_id), approved changes, field repairs         │  PING / TRACE (responders) → tools → report
      └─────────────────────────────────────────────────────────────┤  you (operator) → approve / reject
                                                                    │  PATCH (field technician) → fault restore
NOC room page (http://127.0.0.1:8095) ◀── SSE: lab, incidents, proposals, activity, crew, chat
```

| Piece | Where | Notes |
|---|---|---|
| Engine | `bot/src/netsim_bot/engine.py` | watcher loop, crew dispatch by severity, investigations as asyncio tasks, proposals, approvals, the one-at-a-time change executor, field repairs, chat, maintenance mode, budget, restart recovery |
| Incidents | `incidents.py` | snapshots → incidents: settle window (6 s, max 20 s), joining by service (or host), resolve after 8 s without problems and nothing running, trap hints ignored |
| Brains | `brains.py`, `prompts.py` | **Grok**: Responses API loop with `previous_response_id`, parallel tool calls run in order, one nudge, the last step offers only `finish_investigation` with `tool_choice: required`. **Rules**: a fixed triage (problems → drift → device state → impact → proposals → report) |
| Tools | `tools.py` | 11 lab-api reads (compacted for the model; every result ≤ 16k characters), 3 proposal tools, `finish_investigation`. Every tool has a *station* in the room |
| Grok client | `grok.py`, `pricing.py` | httpx; retries 429/5xx with `retry-after`; error kinds auth/billing/rate/server/bad-request; usage incl. cached and reasoning tokens; cost per model |
| Web | `api/app.py`, `web/` | the NOC page, REST, SSE (replay with `Last-Event-ID`), operator token, Host guard |
| CLI | `cli.py` | `botctl status/incidents/show/approve/reject/ask/watch/pause/resume/brain/autonomy/grok-check` |
| Container | `bot/Dockerfile`, `compose.yaml`, `bot.mk` | `netsim/grok-bot:0.1.0`, repository mounted read-only (live `.env`), volume for SQLite, no Docker socket |

### The NOC room

An original pixel-art office drawn on a 480×270 canvas: no image files, a 3×5 pixel font and robots drawn in code,
and no external requests. It follows the trend the user saw (Pixel Agents, Star-Office-UI) but is built for this
lab:
- **Crew:**
  - POLL (headset) watches at the Zabbix console.
  - PING and TRACE (optionally DIG) sit at their desks.
  - PATCH (hard hat) waits at the field bench.
- **Every real tool call is a walk to its station:**

  | Station | Tools |
  |---|---|
  | NetBox cabinet | intent, topology |
  | Server rack (the inspected PE's unit glows blue) | device state, config |
  | Whiteboard | blast radius |
  | Wall screen | services |
  | Desk | drift, report |
  | Printer → operator desk | change tickets |

  Speech bubbles show what the bot says (Grok's own one-line narration, or the rules brain's lines) and the result
  of each call; bubbles that would overlap stack up.
- **The big wall screen** draws Zabbix's L2VPN business services per region and SLA tier, the topology with PW state,
  the open problems and the crew, with a scrolling problem ticker. Clicking it zooms to a full HTML view (KPIs,
  service tiles with PW paths, an SVG topology, the problem table) with links to Zabbix, NetBox and lab-api.
- **Ambience driven by real state:**
  - rack LEDs per PE (power, uplink, PWs; a powered-off PE goes dark);
  - a red beacon and vignette while a High/Disaster problem is open;
  - the incident board's sticky notes moving NEW → WORK → YOU → FIX → DONE;
  - the operator desk's in-tray and blinking lamp while tickets wait, with an APPROVED/REJECTED stamp;
  - PATCH leaving through the door for field repairs;
  - responders asleep on the couch in maintenance mode, idle bots wandering to the coffee machine;
  - a window with the real time of day and a wall clock.
- **Side panel:**
  - incidents (report, evidence, impact by tier, change tickets with the exact diff and predicted drift, timeline,
    Grok usage);
  - approvals (approve, reject, approve all make-before-break);
  - live activity per bot;
  - **Ask the NOC**: chat answered with the same tools.
- **Header:** brain, autonomy and maintenance controls; cost today. Optional sounds.

### Safety model

- The model never sees a write token and has no tool that applies anything. Proposals are lab-api dry runs.
- Execution happens only after a human approves, or under `BOT_AUTONOMY=auto` (lab only).
- What is applied is exactly the approved `plan_id`. A changed device or NetBox makes lab-api answer 409, and the
  proposal returns to pending with the new plan.
- Tools exclude the lab's answer key and its levers: no scenario definitions, no fault injection, no reset. The bot
  diagnoses from what a NOC can see.
- Tool results are declared data, not instructions, in the system prompt.
- The audit actor names both: `grok-bot/PING approved-by:<who>`.
- Writes to the bot itself need `X-Operator-Token`. The page served by the bot carries it, and requests with a
  foreign `Host` header are refused (DNS rebinding).

## Decisions

**Polling, not a Zabbix action, wakes the bot.** POLL polls `GET /zabbix/problems` every 2 s; lab-api answers in
about 0.07 s. That is the lab's own rule (polling is truth) and needs no change to 04's Zabbix provisioning. The
settle window absorbs the few seconds over which one fault's problems appear. A Zabbix webhook action could push
instead; it would save under 2 s.

**A rules brain beside Grok.** It is the fallback for the cases below, and it is the e2e harness, so the Definition of
Done needs no paid key and stays deterministic:
- no key;
- budget spent;
- xAI refusing the key: auth and billing errors mark Grok unavailable until the key changes;
- a failed Grok run: the rules brain finishes that incident.

The room animates its tool calls exactly like Grok's. It is labeled everywhere as "rules", never as Grok.

**One incident per fault, not per problem.** A hub power-off raises 7-14 problems over several seconds. The settle
window plus joining by service gives one incident per fault, as lab-api's correlation does. Two faults applied
together (stale-backup) become one incident with two causes, and the report says so.

**Make before break.** Changes approved together run in this order:
1. field repairs;
2. intent pushes that only add;
3. changes;
4. pure removals.

For fake-redundancy that is hub-a (gets its PW back), then spoke-2 (repoints), then hub-b (drops the extra PW). Every
step stays verifiable; e2e checks the order in lab-api's audit log.

**Field repairs are tickets, not magic.** A physical fault cannot be fixed by config. The bot proposes a field ticket.
After approval, PATCH "drives to the site" (`BOT_FIELD_DELAY_S`) and lab-api restores the fault (`POST /faults`,
`action: restore`), which stands in for the technician's work.

**Live `.env`, like lab-api.** The repository is mounted read-only and settings are re-read every 2 s. Adding the key
needs no restart, and the page header shows the effective brain and why.

**No external assets.** The page loads nothing from the internet (canvas art, own font, system fonts), so the NOC room
works offline and nothing is copied from the projects that inspired it.

**Maintenance mode during lab-e2e.** The bot would react to every fault lab-e2e injects: incidents, dry runs in the
audit log a test reads, and API load during timing tests. `lab/e2e/conftest.py` pauses it for the run and restores
the previous mode.

## Cross-session changes

| File (owner) | Change | Why |
|---|---|---|
| `compose.yaml`, `Makefile`, `pyproject.toml`, `.env.example`, `uv.lock` (01) | include `bot/compose.yaml`; `FRAGMENTS`, `COMPONENT_MKS`, `bot-%` placeholder, `build` help; member `bot`, dependency `netsim-bot`, `testpaths` + `bot`; the bot's `.env` section | the new component, additive (contract §3 allows members/targets an owner adds) |
| `tests/test_scaffolding.py` (01) | `FRAGMENTS` + `bot`, workspace members + `bot` | the scaffolding tests pin the component list |
| `devices/Dockerfile` + `.dockerignore` (02), `lab/Dockerfile` + `.dockerignore` (05) | copy `bot/pyproject.toml` into the build stage | `uv sync --frozen` needs every member's pyproject; both images stopped building otherwise |
| `lab/e2e/conftest.py` (05) | session fixture: grok-bot maintenance mode during lab-e2e, restored after; fails loudly if the bot runs but refuses the token | see Decisions |
| `docs/CONTRACT.md` | §1, §3 (owner), §4 (address, port), §11 (tokens), §12 (`make e2e` incl. bot-e2e), five change-log entries | contract rule |
| `README.md`, `docs/RUNBOOK.md`, `prompts/README.md` | the bot in the intro, diagram, quickstart, run order; runbook section "The NOC bot" and troubleshooting | |

## Tests

| Suite | Count | What |
|---|---|---|
| `bot/tests` (unit, in `make test`) | 68 | Grok client (request shape, parsing, retries, reasoning fallback, error kinds); incident grouping (settle, storms, joining, resolution hold, busy, flapping, maintenance, restart); tools on **recorded lab-api responses** (healthy, fake-redundancy with its three real dry runs, hub-down) incl. JSON-Schema validity of every tool; both brains (scripted Grok: call ids, response chaining, nudge, forced report; rules on recorded states); the engine's whole loop on a fake lab-api (make-before-break, exact `plan_id`, approver in the actor, 409 re-plan, auto mode, field repair, maintenance, obsolete proposals, chat, Grok failure fallback, accounting, budget, recovery); the web API (token, Host guard, SSE hello); settings, store, pricing, page assets (`node --check`) |
| `bot/e2e` (live lab, in `make e2e`) | 6 | vcid-typo → intent on spoke-1, verdict ok, audited `approved-by`; fake-redundancy → 3 proposals, applied hub-a → spoke-2 → hub-b; hub-down → field ticket, PATCH, hub-b on; reject + maintenance mode + resume; chat (hub-c maintenance → SVC-0009 bronze down); the page and its stream |
| `bot/e2e/test_grok_eval.py` (`make bot-eval`, opt-in) | 6 cases | Grok on vcid-typo, mtu-mismatch, fake-redundancy, stale-backup, hub-down, region-down; hard: category and every expected fix proposed; scored: no extra fixes, root cause mentions, tier counts, confidence; nothing applied; scorecard + cost in `build/grok-bot-eval/results.json` |

## Verification

- `make test`: **574 passed**; `make lint` clean; `contract-check`, `lab-check` current.
- `make bot-e2e`: **6/6 in 327 s** on the live lab.
- In the browser pane: the room animating real incidents (mtu-mismatch end to end: alarm, investigation at rack
  and board, ticket, approval on the page, verdict ok, resolved in 56 s), maintenance mode, the chat, the zoomed
  wall screen, phone width.
- **Full `make e2e`: passed, 1249 s.** First `bot-e2e`: 6 passed (the six Grok-eval cases skipped: no key) in
  322 s. Then `lab-e2e` with the bot in maintenance mode: **23/23 in 921 s**, every timing within budget:
  - fault -> business service status 3.5-9.8 s (budgets 10/12 s);
  - trap -> check-now re-poll median 3.1 s, max 5.1 s;
  - polling only 55.4 s (budget 70 s).

  `make down && make up` recreated every container, grok-bot included, and all 22 came back healthy. The bot left
  maintenance mode afterwards.

## Notes for later

- **Grok live:** the first runs with a key should be `botctl grok-check` and `make bot-eval`. Expect prompt tuning
  from the scorecard (the system prompt is `prompts.py`). Every report's usage is on the incident (`botctl show`).
- **Possible next steps:**
  - acknowledge Zabbix problems with the bot's diagnosis (`event.acknowledge` is allowed for the API user; it needs a
    small lab-api endpoint);
  - a Zabbix webhook action instead of polling;
  - exposing the bot's read tools over MCP so the user's Grok Bot app could ask about the lab through "route traffic
    through your desktop";
  - a third responder (`BOT_RESPONDERS=3`, DIG) for incident storms.
- The rules brain acts only on what the drift checker classifies; Grok sees the same data and has to reason, and
  that difference is what `bot-eval` measures.
