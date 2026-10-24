# Demos: three Grok Bot roles in the NetSim NOC

Every demo runs on the live lab with the NOC room (<http://127.0.0.1:8095>) on the big screen, and the three Bots
set up as in [README.md](README.md): connectors, skills and webhook routines. Each demo lists:
- **Run:** what you type to break something (you play the network; the roles never break anything);
- **Show:** what the audience should notice, in the room, in Zabbix (<http://127.0.0.1:8080>) and in the app.

Before each demo, start from a green lab with no open incident:

```bash
uv run labctl reset
```

```bash
uv run botctl status
```

**No app at hand?** `make bot-sink-up` sends the pages to the routine sink (`uv run botctl sink show` prints them)
and you play the roles with `uv run botctl call --role <role> ...`. `make bot-e2e` plays every demo below that way.

**Cost:** every page is one routine run. Demo 1 costs about four runs (the page, the tickets page, the recovered
page, and the NOC Manager's review run), demo 5 one.

**Suggested 10-minute story:** demo 1, then 3, then 5.

---

## 1. Alert storm: one page for six customers

hub-a, the North region's primary hub, loses power. Zabbix sees dozens of PW problems and six customers turn
Warning or Disaster within seconds; the NOC Engineer gets **one** page.

**Run:**

```bash
uv run labctl device stop hub-a
```

**Show:**
- Zabbix → Services: Customer-A to E Warning (degraded: their backups via hub-b carry them), Customer-F Disaster
  (bronze, unprotected, down).
- About 20 s later NOCENG's desk phone rings once. The page lists all six customers, gold first.
- NOCENG gets up: incident board (takes it), Zabbix console (acknowledges), desk (triage), printer (field ticket
  "power on hub-a"), which it carries to NOCMGR's desk for the review.
- Zabbix → Problems: every hub-a PW problem shows **Acknowledged**, by `noc-engineer`, with its message.
- NOCMGR's phone rings for the ticket; it reviews it and carries it to the operator desk: proposed, with its
  reason. Only now can you approve it (Approvals tab, **Approve INC-xxxx**, or `uv run botctl approve CHG-xxxx`).
- The ticket goes **dispatched → on site → repaired** (no robot: the field technician works off-screen). hub-a boots,
  the customers go OK, NOCENG gets one "recovered" page, verifies in Zabbix and closes the incident.

## 2. An AC fault caught by linkDown

One attachment circuit fails: hub-a's port of SVC-0001 (Customer-A, gold). No hub or PW config changed.

**Run:**

```bash
uv run labctl fault ac hub-a GigabitEthernet0/0/2.101
```

**Show:**
- hub-a sends an IF-MIB **linkDown** trap within a second; Zabbix re-polls hub-a and shows "Interface
  GigabitEthernet0/0/2.101 (Customer-A SVC-0001) on hub-a: down, admin up" within about 5 s
  (`uv run labctl events --since 1m` shows the trap).
- The interface problem pages nobody by itself; Customer-A turning Warning (its backup PW carries it) does: one page.
- NOCENG reads `get_device_state` on hub-a (`ac-down`), files a field ticket of kind `ac`. NOCMGR proposes it; approve it;
  linkUp, then OK, then the recovered page.

## 3. Fake redundancy goes to the Network Engineer

Someone "moved" SVC-0002's primary PW from hub-a to hub-b. Traffic still flows on both PWs, but one hub failure
would take Customer-A down. Zabbix compares every PW with NetBox, so it is not green.

**Run:**

```bash
uv run labctl scenario apply fake-redundancy
```

**Show:**
- Customer-A Warning (degraded), with three problems on three hosts that correlate to one cause.
- NOCENG triages it as config drift and **escalates** to the Network Engineer: NETENG's phone rings, with the note and
  the triage in the page.
- NETENG works at its desk, the NetBox cabinet and the rack: `get_drift` says "both PWs terminate on hub-b —
  redundancy lost". It files the root cause (the incident's title changes) and three tickets at the printer, one per
  host.
- NOCMGR gets one page for the three tickets, reviews them as a set and proposes each. Approve them together
  (**Approve INC-xxxx** on the incident): the executor runs hub-a first (adds the PW), then spoke-2, then hub-b (removes the extra one): make
  before break. `uv run labctl events --since 5m` shows the three commits in that order.
- Customer-A OK, `get_drift` clean, recovered page, closed.

## 4. A gold customer down goes to the NOC Manager

spoke-2 loses its core uplink: both PWs of SVC-0002 (Customer-A) and SVC-0003 (Customer-B) go down. Two gold
customers are **down**, not degraded.

**Run:**

```bash
uv run labctl fault uplink spoke-2
```

**Show:**
- NOCENG and NOCMGR are paged at the same time: a gold customer down pages the NOC Manager too.
- NOCMGR sets **P1** and writes the customer update; NOCENG takes, acknowledges and files the field ticket (uplink).
- Leave the ticket unapproved: after `{$NETSIM.ESCALATE.AFTER}` (10 min; set it to `1m` in Zabbix → Administration
  → Macros for a short demo, and back afterwards) **Zabbix's escalation step** pages the NOC Manager again, "still
  not OK". Then approve the ticket (NOCMGR proposed it in its first run).

## 5. Ask the NOC

**Run:** in the room's panel, tab **Ask the NOC** (or `uv run botctl ask "..."`), ask for example:

> If we reboot hub-c tonight, which customers lose service or only redundancy, by SLA tier? Is any of them running
> without a working backup already?

**Show:**
- The panel says the question pages the NOC Manager: **one routine run**.
- NOCMGR's phone rings; it walks to the whiteboard and the wall screen (`get_impact`, `list_services`) and its
  answer appears in the panel. Unanswered questions time out after `BOT_ASK_TIMEOUT_S` (10 min) with a message.

---

## After a demo

```bash
uv run labctl reset
```

The roles close what recovered. To keep the routines quiet between demos: `uv run botctl mute` (alerts are only
recorded) and `uv run botctl unmute` before the next one.
