# Demos: the Grok Bot app in the NetSim NOC

Every demo uses the live lab and the NOC room (<http://127.0.0.1:8095>; put it on the big screen). The Grok Bot app
works through the NOC tools (MCP connector `netsim-noc`, or `uv run botctl call` on your computer, see
[README.md](README.md)) with [SKILL.md](SKILL.md) as its skill. Each demo lists:
- **Setup:** what you run before;
- **Prompt:** what you paste into the app;
- **Show:** what the audience should notice.

Before each demo, start from a green lab with no open incidents:

```bash
uv run labctl reset
```

```bash
uv run botctl status
```

**Suggested 10-minute story:** demo 2, then 1, then 6, then 3, then 8.

---

## 1. On call: the bot gets paged

A fault happens and nobody asks anything: Zabbix sees it, the NOC pages the Grok Bot app, it investigates and posts
the diagnosis with a fix waiting for your approval.

**Setup (once):** the routine and the pager, [README.md §3](README.md#3-the-pager-let-faults-wake-the-app). Routine
prompt, trigger "when a webhook fires":

> You are on call for the NetSim NOC (skill: netsim-noc). The webhook body is a page from the NOC: read `summary`,
> `incident` and `services`. Investigate that incident with the NOC tools: get_incident, then the reads you need
> (get_open_problems, get_drift, get_device_state, get_impact). Decide the root cause from evidence. If a fix is
> needed, propose it with incident_id set (the crew may already have filed it: then it is the same ticket). Post to
> Slack #noc (or reply here if Slack is not connected): a one-line headline, root cause, impact by tier with
> customer names, the ticket id and "approve at http://127.0.0.1:8095". Then wait_for_proposal (300 s). When it is
> applied, check list_services and get_drift and post the outcome. Finally add_incident_note with your summary. Never
> approve anything yourself. If the event is incident.test, just answer "page received".

**Run:**

```bash
uv run labctl scenario apply hub-down
```

**Show:**
- POLL raises the alarm and says "paged GROK";
- the GROK visitor walks in and visits the rack and the whiteboard;
- a ticket goes onto the operator desk (power on hub-b), and you approve it;
- PATCH drives out, and the app posts "applied, 7 services OK again".

Try `double-failure` next, both hubs of a region at once: "down" is not "degraded".

## 2. "What breaks if…?" before a change

**Prompt:**

> I want to reboot hub-c tonight. Which customers lose service or redundancy, by SLA tier? Is any of them already
> running without a working backup? Draft a two-line customer notice for each affected customer.

**Show:**
- the app calls get_impact hub-c and list_services;
- answer: SVC-0007 (Customer-E, gold) and SVC-0011 (Customer-C, silver) degraded, and SVC-0009 (Customer-F, bronze,
  unprotected) **down**, with notices per customer.

**Twist:**

```bash
uv run labctl scenario apply stale-backup
```

Then ask the same about **hub-a**. SVC-0001 (Customer-A, gold) and SVC-0005 (Customer-D, silver) would now go
**down**, not degraded: their backups are already broken. The unprotected bronze SVC-0010 goes down by design. NetBox's plan says one thing, the live network another. That is the demo's punchline.

## 3. Morning redundancy audit (weekday routine)

Faults that raise no red alarm: services that look protected but are not.

**Routine (every weekday, 08:00):**

> Morning redundancy audit for the NetSim NOC (skill: netsim-noc). Using list_services, get_drift and get_topology:
> list every redundant-pair service whose backup PW is down, points to the wrong peer or VC ID, or terminates on the
> same hub as its primary. For each: customer, tier, what is wrong (host, config path, value vs NetBox), and what
> would happen if its primary hub failed today (get_impact on that hub). Propose the fixes (propose_apply_intent per
> device) but do not wait for them. Post a short report to Slack #noc-morning, worst tier first; if all is clean, say
> so in one line.

**Setup (to have something to find):**

```bash
uv run labctl scenario apply fake-redundancy
```

**Show:**
- the report names SVC-0002 (Customer-A, gold): "both PWs on hub-b: redundancy lost", with three tickets (hub-a,
  spoke-2, hub-b);
- approve them and watch the make-before-break order: hub-a, then spoke-2, then hub-b.

## 4. Drift detective

**Setup:**

```bash
uv run labctl scenario apply vcid-typo
```

Or `mtu-mismatch`.

**Prompt:**

> Something changed on the network in the last 15 minutes. Find what changed, when and on which device, and whether
> NetBox or the device is right. Fix whichever is wrong.

**Show:**
- get_events: a RESTCONF commit on spoke-1 changed SVC-0001's backup VC ID from 2001 to 2010;
- get_drift: config-drift;
- get_device_intent: the exact path;
- the app proposes NetBox intent on spoke-1; you approve and it verifies.

## 5. Deliver an ordered service

**Setup:**

```bash
uv run labctl scenario apply new-service-pending
```

SVC-0012 for Customer-C is ordered in NetBox but not built.

**Prompt:**

> Customer-C says their new service never came up. Find out why and get it delivered.

**Show:**
- the app sees intent-only / not-delivered drift and proposes NetBox intent on spoke-3, hub-a and hub-b;
- you approve all three, and SVC-0012 goes from Disaster to OK in Zabbix.

Afterwards:

```bash
uv run labctl reset
```

## 6. Game day: Grok Bot attacks, the crew defends

The app plays chaos engineer on your computer, and the in-house crew has to catch it. It works without an xAI key:
the crew runs its rules brain.

**Prompt (the app needs permission to run commands on your computer):**

> Game day in the NetSim lab (repository at ~/Work/Experiment/net-ops-grok-bot). Pick ONE fault the NOC crew must
> catch, without telling me which: run exactly one of `uv run labctl device stop <hub>`,
> `uv run labctl fault uplink <host>`, `uv run labctl cut <spoke> <hub>` or a `uv run labctl config set` that breaks a
> VC ID. Then watch the crew with list_incidents / get_incident every 15 s. Score them:
> - time until the incident opened;
> - whether the root cause in their report matches what you did;
> - whether their ticket would fix it.
>
> When I have approved and it is resolved, run get_drift to confirm nothing is left, and post the scorecard. Do not
> fix anything yourself.

**Show:**
- GROK walks in with an evil plan;
- POLL raises the alarm and PING investigates;
- the crew's report, then the scorecard.

To end the game day:

```bash
uv run labctl reset
```

## 7. Maintenance window assistant (calendar routine)

**Routine (on each event of a "Network maintenance" calendar series):**

> A maintenance window starts now for the host named in the event title. Before: get_impact for that host and post
> the affected customers by tier to Slack #noc. Ask me to put the NOC in maintenance mode (`uv run botctl pause
> --reason "<event title>"`). When I say the work is done: run get_drift and list_services. If all is clean, ask me
> to resume the NOC (`uv run botctl resume`) and post a closing note. If not, list what is still wrong and propose
> fixes.

**Show:**
- the crew sleeps on the couch during maintenance, and nothing is paged;
- afterwards the app proves the network is back to intent.

## 8. Shift handover

**Routine (every day, 18:00), or ask in chat:**

> Write the NOC shift handover for today: every incident from list_incidents (active_only false) with get_incident:
> what broke, who fixed it (crew or you), time to resolve, the tickets and who approved them. Then what is still open
> or pending approval. Keep it under 20 lines, worst first. Post it to Slack #noc-handover.

**Show:** a real handover built from the day's demos, with incident ids that match the NOC room's board.

---

### Cost and safety notes

- Each page and each routine run is a Grok Bot run on your plan. The pager is capped at `BOT_PAGER_MAX_PER_HOUR`
  (6), and `botctl pager mute` stops it.
- Commands the app runs on your computer are asked for one by one. For game day, allow only `labctl` and
  `botctl call`.
- Approval always stays with a human. That is the line to say out loud in the demo.
