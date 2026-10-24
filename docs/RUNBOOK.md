# NetSim L2VPN lab — Runbook

How to bring the lab up, drive it, reset it, run every scenario and fix the usual problems. Architecture and
contracts: [`README.md`](../README.md), [`docs/CONTRACT.md`](CONTRACT.md). Tool details: [`lab/README.md`](../lab/README.md).

| What | Where |
|---|---|
| NetBox (intent) | <http://127.0.0.1:8000> — user `admin`, password `NETBOX_SUPERUSER_PASSWORD` in `.env` |
| Zabbix (monitoring) | <http://127.0.0.1:8080> — user `Admin`, password `ZABBIX_ADMIN_PASSWORD` in `.env` |
| lab-api (bot tools) | <http://127.0.0.1:8090/docs> — writes need `X-Lab-Token: $LAB_API_TOKEN` |
| NOC room (grok-bot) | <http://127.0.0.1:8095> — the three NOC roles live, incidents, approvals, Ask the NOC; `uv run botctl status` |
| Clixon controller | `make controller-shell` — its CLI as `admin` (labnet only, nothing published); section 6 |
| Live view | `uv run labctl status --watch` |

## 1. Bring-up

On a Linux host with Docker Engine 24+, Compose v2.20+ and uv (`make doctor` checks them):

```bash
make env      # .env with random secrets (never overwrites existing values)
make net      # external Docker network labnet 172.30.10.0/24
make build    # images: netsim-pe (Clixon from source, first time ~5 min), lab-api, grok-bot, the Clixon controller
              # (~2 min), Zabbix 8.0.0rc1 (~7 min, once)
make up       # every container (detached)
make seed     # waits until every container is healthy, then NetBox seed -> Zabbix provision (waits for LLD, builds map)
make e2e      # optional: the NOC office, the controller, every scenario end to end, ~22 min, timing tables at the end
```

`make seed` waits first (`labctl wait-healthy`): a fresh NetBox migrates its database for 4-8 minutes, a fresh Zabbix
creates its schema. If a container never becomes healthy, the error names it with its last log lines. Order inside
`make seed`: `netbox-bootstrap` (tokens into `.env`) -> `netbox-seed` (objects + webhook to lab-api) ->
`zabbix-bootstrap` (API user/token into `.env`, webhook script) -> `zabbix-provision` (template, hosts, trap action,
spec macros, BSM, SLAs, wait for discovery, map, dashboard).

Check it:

```bash
uv run labctl status          # controller: 10/10 connected, 10 in sync; 11 services (2 unprotected), PWs up, Zabbix OK
uv run labctl drift           # "no findings", exit 0
make zabbix-status            # BSM tree all OK, 0 problems
```

`make down` stops everything and keeps the volumes: running configs (drift included), NetBox and Zabbix survive
`make down && make up`. `make clean CONFIRM=yes` deletes all volumes.

## 2. Reset

```bash
make reset                    # = uv run labctl reset
```

Clears every fault and cut, starts stopped PEs, re-seeds NetBox if a scenario changed it (`--prune`), puts the golden
config back on every PE that differs (one RESTCONF PUT each), resets the drift baselines, has the Clixon controller
take every router's config as its copy again (through lab-api; skipped with a note when lab-api or the controller is
down), runs `zabbix-sync` if needed, has Zabbix poll every PE, then waits until every PW is up and every Zabbix
service is OK with no problem of severity Warning or higher (typically 2-10 s; after a power-off up to ~30 s).
Through the API: `POST /reset`.

## 3. Driving the lab by hand

```bash
uv run labctl status --watch                       # leave this running in a second terminal
uv run labctl fault uplink hub-a                   # ... and --restore
uv run labctl cut spoke-3 hub-b                    # ... and --restore
uv run labctl device stop hub-b                    # ... and start
uv run labctl config set spoke-1 'l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id' 2010
uv run labctl drift                                # what differs from NetBox, per service
uv run labctl impact hub-a                         # what taking hub-a down would do now, per service and SLA tier
uv run labctl events --since 2m                    # what the PEs did (config changes, PW transitions, traps)
docker exec -it spoke-1 clixon_cli                 # the router CLI: show l2vpn, show compare, set ..., commit
make controller-shell                              # the controller CLI: show connections, show devices spoke-1 diff
```

In Zabbix: **Dashboards -> L2VPN Services** (problems by severity, the topology map, a honeycomb of every expected
PW, current problems, SLA reports); **Services -> Services -> L2VPN** for the business service tree;
**Monitoring -> Problems** filtered by tag `service`.

The lab has two regions with their own hub pair: **North** (hub-a primary, hub-b backup; spoke-1..4) and **South**
(hub-c primary, hub-d backup; spoke-5..6). Protected services have a primary and a backup PW; the bronze customer's
SVC-0009 (spoke-5, hub-c) and SVC-0010 (spoke-4, hub-a) are **unprotected**: one PW, so losing that hub takes them
down (Disaster) where a protected service is only degraded (Warning). Service table: [`CONTRACT.md`](CONTRACT.md) §5.

## 4. Scenarios

Every scenario: `uv run labctl scenario run <name>` (reset -> apply -> verify incl. follow-ups -> reset), or step by
step `labctl reset`, `labctl scenario apply <name>`, look around, `labctl scenario verify <name>`, `labctl reset`.
`labctl scenario show <name>` prints the steps and every expectation. Times below are from the e2e run (fault ->
Zabbix service status; budget 10 s with traps).

### healthy
Nothing injected. Zabbix: every service OK, **Problems** empty (trap hints, Information, may show briefly after a
reset). `labctl drift`: no findings.

### primary-fail — `labctl fault uplink hub-a`
hub-a's PWs `lowerLayerDown/local-uplink-down`, the North spokes' primaries `down/peer-unreachable`, backups up.
Zabbix: the seven protected North services **Warning** (primary leaf High, backup leaf OK), the unprotected
SVC-0010 **Disaster** (its only PW is gone), the South region OK; problems `oper` on both ends of every hub-a PW and
`Interface GigabitEthernet0/0/0 ... down` on hub-a; the map's hub-a links red. Drift: one `fault` finding per
affected service ("hub-a core uplink down", "service degraded" or "service down"), exit 0: no config changed.

### hub-down — `labctl device stop hub-b`
After the 3 s hold time the backup of every protected North service is `down/peer-unreachable`. Zabbix: those seven
**Warning**; SVC-0010 (no PW on hub-b) and the South region stay OK; the problems come from the spokes' ends (hub-b's
own items keep their last values); after ~70 s also "hub-b: SNMP agent unreachable". Drift: `fault`/`device-down`
per affected service; hub-b is reported unreachable.

### spoke-isolated — `labctl fault uplink spoke-2`
Both PWs of SVC-0002 and SVC-0003 down: those two **Disaster**, the rest OK.

### double-failure — `labctl fault uplink hub-a` + `labctl device stop hub-b`
The North region has no hub left: all eight North services **Disaster**; two fault findings per protected service
(hub-a uplink, hub-b powered off), one for SVC-0010. The South region (own hub pair) stays OK.

### vcid-typo — `labctl config set spoke-1 'l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id' 2010`
Both ends of the backup `down/remote-not-configured`. Zabbix: SVC-0001 **Warning**; problems `oper` (High) on spoke-1
and hub-b, `spec` (Average) on spoke-1 named `SVC-0001 backup on spoke-1: VC ID 2010, NetBox expects 2001`. Drift
(config-drift): `SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001`. Controller: spoke-1 **out of sync**,
the other nine in sync; `show devices spoke-1 diff` shows `-  vc-id "2010";` (the router) and `+  vc-id "2001";`
(its copy). Fix it the NOC's way: `POST /devices/spoke-1/apply-intent` (dry run, then confirm with the `plan_id`);
lab-api then has the controller pull spoke-1, which is in sync again.

### mtu-mismatch — `labctl config set hub-b 'interfaces/interface[GigabitEthernet0/0/2]/mtu' 9000`
hub-b's subinterfaces inherit 9000; every backup on hub-b `down/mtu-mismatch` on both ends: the seven protected
North services **Warning**, SVC-0010 and the South region OK. Drift: per affected service
`backup MTU mismatch hub-b=9000 vs spoke-N=1500 (intent 1500)`.

### fake-redundancy — `labctl scenario apply fake-redundancy`
SVC-0002's primary is moved to hub-b (spoke-2 peer 10.255.0.2, a matching primary on hub-b, SVC-0002 removed from
hub-a). Both PWs are **up** — traffic flows — but both go through hub-b. Zabbix does not stay green: SVC-0002
**Warning** with exactly three problems sharing `service=SVC-0002`: `missing` (High) on hub-a, `spec` (Average) on
spoke-2 (`SVC-0002 primary on spoke-2: peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)`), `unexpected` (Warning)
on hub-b (appears after hub-b's next regular walk, up to 60 s). `GET /zabbix/problems?service=SVC-0002` groups
them as one root cause. Drift: `SVC-0002: both PWs terminate on hub-b (intent: hub-a + hub-b) — redundancy lost`.
Controller: spoke-2, hub-a and hub-b out of sync; spoke-2's diff is its primary's `peer-ipv4` (`10.255.0.2` on the
router, `10.255.0.1` in the copy).
Proof: `labctl device stop hub-b` -> SVC-0002 **Disaster** while the other protected North services are only
Warning.

### new-service-pending — `labctl scenario run new-service-pending`
SVC-0012 (Customer-C, spoke-3, VLAN 701, VC IDs 1012/2012 via hub-a/hub-b) is created in NetBox only. NetBox's
webhook reaches lab-api (`GET /hooks/netbox`, `GET /sync`), which runs `zabbix-sync` ~2 s after the last event: new
BSM nodes, spec macros, discovery -> `missing` on both ends of both PWs -> SVC-0012 **Disaster** ("ordered, not
delivered"; ~20-25 s). Drift: `intent-only`/`not-delivered`. Then lab-api pushes NetBox's intent to spoke-3, hub-a
and hub-b (`POST /devices/{host}/apply-intent`, dry run -> confirm -> verify) -> SVC-0012 **OK**. Finally SVC-0012
is deleted from NetBox and from the devices: its BSM nodes are pruned (`zabbix-status` no longer lists it).

### stale-backup — `labctl scenario apply stale-backup`
SVC-0001's backup points to 10.255.0.12 (no such PE) on spoke-1; SVC-0005's backup has VC ID 2055 on hub-b. Both
**Warning**. `labctl impact hub-a`: NetBox's blast radius says *degraded* for the protected North services (and
*down* for the unprotected SVC-0010, by design), but SVC-0001 and SVC-0005 would go **down** (Disaster) too during
hub-a maintenance, because their backups are already broken. The South region does not use hub-a.

### region-down — `labctl fault uplink hub-c` + `labctl fault uplink hub-d`
Both South hubs lose their core uplink: SVC-0007 (Customer-E, gold), SVC-0011 (Customer-C, silver) and the
unprotected SVC-0009 (Customer-F, bronze) **Disaster**; the North region is untouched. In the BSM, Customer-E and
Customer-C turn **Disaster** although their North services (SVC-0008, SVC-0004) are OK: read the customer node
together with its services. Drift: per South service one `fault` finding per lost hub ("service down"). Follow-up:
`labctl fault uplink hub-c --restore` -> SVC-0007 and SVC-0011 **Warning** (backup through hub-d still down),
SVC-0009 OK.

### maintenance-by-tier — `labctl impact hub-c`, then `labctl device stop hub-c`
Before the maintenance, `labctl impact hub-c` (or `GET /impact/hub-c`) says per service and SLA tier who gets hurt:
SVC-0007 (gold) and SVC-0011 (silver) *degraded* (backup through hub-d), the unprotected bronze SVC-0009 *down*;
`by_tier` counts it. Powering hub-c off proves it: SVC-0007/SVC-0011 **Warning**, SVC-0009 **Disaster**, North
untouched. While hub-c is off, `labctl impact hub-d` shows hub-d has become a single point of failure: by design it
would only degrade SVC-0007/SVC-0011, live it would take them **down**.

## 5. The NOC: three roles, paged by Zabbix (grok-bot)

The NOC room (<http://127.0.0.1:8095>) is the workplace of three Grok Bot app routines: the **NOC Engineer**
(NOCENG), the **Network Engineer** (NETENG) and the **NOC Manager** (NOCMGR). Setup on the app's side:
[`bot/grokbot-app/`](../bot/grokbot-app/README.md). How it works: [`bot/README.md`](../bot/README.md), contract §9.6.

**What happens on a fault:** a customer node turns Warning (degraded) or Disaster (down) in Zabbix; Zabbix's service
action `NetSim: customer alert` posts the alert to grok-bot, which batches what arrives together (about 10 s, at
most 30 s) into **one incident** and sends **one page** to the NOC Engineer's routine (about 20 s after the fault).
The NOC Engineer takes it, acknowledges the problems in Zabbix (shown as `noc-engineer`), triages, and files a field
ticket or escalates. Every change ticket goes to the NOC Manager's review first (its desk shows "N TO REVIEW"); it
proposes each one to you or returns it to the engineer who filed it. Proposed tickets wait for you at the operator
desk or in `uv run botctl status`; approve them in the room (a ticket, or **Approve INC-xxxx** for the whole
incident), with `uv run botctl approve CHG-xxxx` or `--incident INC-xxxx`. Config fixes are applied make before break
and verified by lab-api (~12 s each); field repairs go dispatched → on site (`BOT_FIELD_DELAY_S`, 10 s) → repaired.
When Zabbix shows every customer OK, the NOC Engineer gets one "recovered" page and closes the incident.

Per scenario: drift scenarios (vcid-typo, mtu-mismatch, fake-redundancy, stale-backup) → the Network Engineer pushes
NetBox's intent to each drifted device; new-service-pending → intent pushed to the devices of the ordered service;
fault scenarios (primary-fail, hub-down, spoke-isolated, double-failure, region-down, maintenance-by-tier's power-off)
→ a field ticket per physical cause. A gold customer down pages the NOC Manager too, and so does Zabbix's escalation
step when a customer is still not OK after `{$NETSIM.ESCALATE.AFTER}` (10 min).

**Pages** (each one is a paid routine run):
- `uv run botctl page status`: which routines are set, mute and sink state, the delivery log (HTTP status, tries).
- `uv run botctl page test noc-engineer`: one test page now (checks the routine's URL and key).
- **Mute** while you work on the lab: `uv run botctl mute` / `unmute`. **Maintenance mode** for planned work:
  `uv run botctl pause --reason "..."` / `resume`. In both, Zabbix's alerts are only recorded: no incident, no page.
  The lab part of `make e2e` uses maintenance mode; `make bot-e2e` sends its pages to the routine sink.
- **No app?** `make bot-sink-up` sends every page to the routine sink (`uv run botctl sink show`), and you play the
  roles with `uv run botctl call --role <role> <tool> '<json>'`. `make bot-sink-down` goes back to the routines.
- Hourly cap per role: `BOT_PAGE_MAX_PER_HOUR` (12); 3 tries per page, none after a 4xx.

**The tunnel** (the app's cloud PC reaches `/mcp` only through one):
- Put `BOT_MCP_HOSTS=*.trycloudflare.com` in `.env` and recreate grok-bot once (`docker compose up -d
  --force-recreate grok-bot`: the host list is read at start).
- `uv run botctl tunnel` (needs `cloudflared`) prints the URL and the three connector configs; it runs until Ctrl-C.
  A quick tunnel's URL changes on every start: update the three connectors each time.
- Through a tunnel host only `/mcp` is reachable, with a role token; rotate the tokens in `.env` after a demo.

**Autonomy:** `uv run botctl autonomy observe|propose|default`: under `observe` the roles diagnose and report only
(no `propose_*` tools). No role can approve or apply in any mode.

**The controller terminal:** when a role calls `controller_cli`, its robot walks to the CONTROLLER terminal, which
types `<user>@controller> <command>` and the first lines of the answer; red = refused by lab-api's allow-list,
amber = the controller is unavailable. Click the terminal (or the side panel's **Terminal** tab) for the transcript:
who ran what, when, and the full output. The Network Engineer checks a suspected router there and quotes its diff in
the root cause; the NOC Engineer looks at `show connections` while triaging. Section 6.

## 6. The Clixon controller

The controller (`controller`, 172.30.10.70) manages the ten PEs over NETCONF and keeps a copy of each router's
config: what the lab last put there on purpose. A router that differs from that copy is **out of sync**, a second
drift signal next to NetBox. The NOC roles read it through `controller_cli`, never change anything through it.
Details: [`controller/README.md`](../controller/README.md), contract §9.7.

```bash
uv run labctl status | head -2         # controller: 10/10 connected, 10 in sync (or: out of sync: spoke-1)
make controller-shell                  # its CLI as admin (admin may do anything: look, don't push)
#   show connections                   every router: OPEN, or CLOSED and why
#   show devices spoke-1 check         nothing = in sync; "Error: device out-of-sync"
#   show devices spoke-1 diff          '-' lines: the router now; '+' lines: the controller's copy
#   show configuration devices device spoke-1 config l2vpn xconnect SVC-0001     the copy, a subtree
#   quit
curl -s 127.0.0.1:8090/controller/status   # the same as JSON, per router (lab-api)
```

**Reading out of sync.** The copy is golden after a reset and follows every approved change lab-api applies (it has
the controller pull that router), so out of sync means something else changed the router: a scenario, or someone on
its CLI or RESTCONF. The diff says exactly what. Putting the router back the way the copy has it makes it in sync
again (no pull needed); `labctl reset` makes every router golden and in sync.

**What the controller does not see.** It takes a router's config as its copy whenever it connects to it, so after a
controller restart (or `make down && make up`) every router counts as in sync, even one with drift: `labctl drift`
and Zabbix still see it, and `labctl reset` brings both back to golden. A router powered on with `labctl device
start` or `labctl fault clear-all` (Docker directly) stays CLOSED in the controller until the next reset or `POST
/controller/resync`; powered on through lab-api (`POST /faults`, the NOC's field repair) it is reconnected.

**Optional:** `docker stop controller` and the lab works as before: `labctl status` says the controller is
unavailable, `controller_cli` tells the roles so (they carry on without it). `docker start controller` and it
connects every router again within seconds.

## 7. Troubleshooting

**Traps not arriving** (Zabbix reacts only at the 60 s poll; e2e trap timings fail):
- `docker inspect hub-a --format '{{range .Config.Env}}{{println .}}{{end}}' | grep TRAP_TARGETS` must show
  `172.30.10.42:1162` (empty = traps disabled; recreate with `docker compose up -d hub-a`).
- `docker exec zabbix-snmptraps tail -5 /var/lib/zabbix/snmptraps/snmptraps.log` should show `ZBXTRAP <device IP>`
  lines after a fault; `uv run labctl events --since 1m` shows `trap-sent` on the device.
- The source address must be the device's mgmt IP (Zabbix matches traps to the SNMP interface).
- In Zabbix, **Alerts -> Actions -> Trigger actions -> NetSim: SNMP trap -> check now** must be enabled; its
  executions are under **Reports -> Action log** (a failure there usually means a stale API token: `make zabbix-bootstrap`).

**LLD not discovered / items missing** (no `netsim.pw.row[...]` items, BSM leaves stay OK although PWs are down):
- The spec macro: `{$NETSIM.PW.SPEC}` on the host must list its PWs (`uv run zabbix-sync --dry-run` reports pending
  changes; `make zabbix-sync` writes them and runs the discovery now).
- **Data collection -> Hosts -> <host> -> Discovery**: "Expected PWs (NetBox spec)" must have no error; "PW table"
  and "Interfaces" depend on the walk, which must be supported (green "SNMP" availability).
- After a NetBox change: `GET /sync` on lab-api shows the last run and its errors; `uv run labctl sync` re-runs it.

**Tokens** (401/403 from NetBox or Zabbix, lab-api writes refused):
- `make netbox-bootstrap` / `make zabbix-bootstrap` verify the tokens in `.env` and replace stale ones (after
  `make clean` they always are). lab-api reads `.env` on every request: no restart needed.
- lab-api writes need `X-Lab-Token` = `LAB_API_TOKEN` from `.env` (`make env` creates it); 503 means lab-api has
  none.
- NetBox webhook rejected (`hook.netbox` with status 401 in `GET /audit`): `NETBOX_WEBHOOK_SECRET` in `.env` and the
  secret of the NetBox webhook differ; `make netbox-seed` sets the webhook from `.env` again.

**Not green after reset**: `labctl reset` prints why (e.g. a PW still down, a Zabbix problem). `labctl status` and
`labctl drift` show the rest; `docker compose ps` for container health; `make logs S=<service>`.

**Drift reported right after `make up`**: the drift checker needs NetBox; while NetBox starts, `labctl drift` fails
with a NetBox error. Wait for `labctl wait-healthy`.

**grok-bot** (the page shows "lab-api unreachable", no incidents, no pages, or changes refused):
- `docker compose ps grok-bot` and `make bot-logs`; `curl -s 127.0.0.1:8095/api/health` shows `lab_ok`.
- No incident for a fault: maintenance mode or mute on (`uv run botctl status`, the page header)? The alert is in
  `GET /api/alerts` with its outcome. Not there: Zabbix → **Reports → Action log** for `NetSim: customer alert`
  (a 401 from grok-bot means `BOT_ZABBIX_HOOK_TOKEN` and the secret macro differ: `make zabbix-bootstrap`). A problem
  that touches no customer (an interface alone, an unexpected PW) never opens an incident, by design.
- No page: `uv run botctl page status` (routine URL empty, cap reached, the HTTP error of the last try).
- Approvals answer 401: the page carries the token of the bot that served it; reload the page after changing
  `BOT_OPERATOR_TOKEN`. `botctl` reads it from `.env`.
- A ticket went back to *review* with a note: the device or NetBox changed after the dry run (lab-api 409); the NOC
  Manager reviews the new plan, then you approve again.
- A ticket stays in *review*: the NOC Manager's routine did not answer (paused, or waiting on an approval card in the
  app: set `review_ticket` to "Always allow"). After `BOT_REVIEW_TIMEOUT_S` (5 min) it comes to you marked "not
  reviewed".
- Approve INC-xxxx answers 409: another ticket of the incident was proposed since the page showed it; look again.
- A role's tool answers "belongs to the NOC Engineer ...": that Bot uses another role's connector or token.

**The controller** (`labctl status` says unavailable, routers CLOSED, `controller_cli` errors):
- `docker compose ps controller` and `make controller-logs`: the agent logs `<pe>: connected` for each router after
  every start; `docker exec controller supervisorctl status` shows the backend, sshd and the agent.
- Unavailable with "the controller has not started yet": it has not published its host key in the shared volume
  `netsim_controller-keys` (first start, or after `make clean`). lab-api publishes its own key and pins the
  controller's current host key on every call, so the next call after the controller is up works.
- A router stays CLOSED: it was powered on outside lab-api, or it was not up within 10 minutes of the controller's
  start. `uv run labctl reset`, or `curl -X POST 127.0.0.1:8090/controller/resync -H "X-Lab-Token: $LAB_API_TOKEN"`.
  Still CLOSED after a resync, and the router's volume was made again: it has a new SSH host key, which the
  controller's strict host key check refuses. Forget the old key (`docker exec controller ssh-keygen -R
  '[172.30.10.21]:830' -f /var/lib/controller/ssh/known_hosts`, with the router's mgmt IP); the agent learns the new
  one within 30 s (`make controller-logs`: "host key learned"), then resync as above.
- A role's command is refused: lab-api's allow-list (contract §9.7); the refusal lists what is allowed. A command
  that reaches the controller and answers "access denied" is the controller's NACM (wall 2).
