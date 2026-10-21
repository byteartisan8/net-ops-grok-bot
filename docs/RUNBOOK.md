# NetSim L2VPN lab — Runbook

How to bring the lab up, drive it, reset it, run every scenario and fix the usual problems. Architecture and
contracts: [`README.md`](../README.md), [`docs/CONTRACT.md`](CONTRACT.md). Tool details: [`lab/README.md`](../lab/README.md).

| What | Where |
|---|---|
| NetBox (intent) | <http://127.0.0.1:8000> — user `admin`, password `NETBOX_SUPERUSER_PASSWORD` in `.env` |
| Zabbix (monitoring) | <http://127.0.0.1:8080> — user `Admin`, password `ZABBIX_ADMIN_PASSWORD` in `.env` |
| lab-api (bot tools) | <http://127.0.0.1:8090/docs> — writes need `X-Lab-Token: $LAB_API_TOKEN` |
| NOC room (grok-bot) | <http://127.0.0.1:8095> — the crew live, incidents, approvals, chat; `uv run botctl status` |
| Live view | `uv run labctl status --watch` |

## 1. Bring-up

On a Linux host with Docker Engine 24+, Compose v2.20+ and uv (`make doctor` checks them):

```bash
make env      # .env with random secrets (never overwrites existing values)
make net      # external Docker network labnet 172.30.10.0/24
make build    # images: netsim-pe (Clixon from source, first time ~5 min), lab-api, Zabbix 8.0.0rc1 (~7 min, once)
make up       # every container (detached)
make seed     # waits until every container is healthy, then NetBox seed -> Zabbix provision (waits for LLD, builds map)
make e2e      # optional: every scenario end to end, ~14 min, timing table at the end
```

`make seed` waits first (`labctl wait-healthy`): a fresh NetBox migrates its database for 4-8 minutes, a fresh Zabbix
creates its schema. If a container never becomes healthy, the error names it with its last log lines. Order inside
`make seed`: `netbox-bootstrap` (tokens into `.env`) -> `netbox-seed` (objects + webhook to lab-api) ->
`zabbix-bootstrap` (API user/token into `.env`, webhook script) -> `zabbix-provision` (template, hosts, trap action,
spec macros, BSM, SLAs, wait for discovery, map, dashboard).

Check it:

```bash
uv run labctl status          # 11 services (2 unprotected), every PW up on both ends, Zabbix OK
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
config back on every PE that differs (one RESTCONF PUT each), resets the drift baselines, runs `zabbix-sync` if
needed, has Zabbix poll every PE, then waits until every PW is up and every Zabbix service is OK with no problem of
severity Warning or higher (typically 2-10 s; after a power-off up to ~30 s). Through the API: `POST /reset`.

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
(config-drift): `SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001`. Fix it the bot's way:
`POST /devices/spoke-1/apply-intent` (dry run, then confirm with the `plan_id`).

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

## 5. The NOC bot (grok-bot)

grok-bot watches Zabbix through lab-api and reacts to every scenario above on its own: POLL opens an incident about
6-10 s after the fault (the problems settle first), a responder investigates and files a report with change tickets,
and the tickets wait for you at the operator desk of the NOC room (<http://127.0.0.1:8095>) or in
`uv run botctl status`. Approve them (page, `botctl approve CHG-xxxx` or `--incident INC-xxxx`): config fixes are
applied make-before-break and verified by lab-api (~12 s each); field repairs send PATCH on site
(`BOT_FIELD_DELAY_S`, 6 s) and then restore the fault through lab-api. The incident resolves when Zabbix is clear.

What the bot does per scenario: drift scenarios (vcid-typo, mtu-mismatch, fake-redundancy, stale-backup) → NetBox
intent pushed to each drifted device; new-service-pending → intent pushed to the devices of the ordered service
(delivery); fault scenarios (primary-fail, hub-down, spoke-isolated, double-failure, region-down, maintenance-by-tier's
power-off) → a field ticket per physical cause. `labctl reset` instead of approving also resolves the incident; its
open tickets become obsolete.

- **Grok**: `XAI_API_KEY` in `.env` (console.x.ai; billed by xAI, separately from subscriptions), then
  `uv run botctl grok-check`. Without a key, or after `BOT_DAILY_BUDGET_USD` is spent, or when xAI refuses the key,
  the responders use the rules brain; the page header says which and why.
- **Autonomy**: `botctl autonomy observe|propose|auto` (or the page header); `auto` applies fixes without a human
  (lab only). **Maintenance mode**: `botctl pause --reason ...` / `resume`; the lab part of `make e2e` uses it.
- **Grading Grok**: `make bot-eval` (opt-in, costs money) runs six scenarios with Grok, grades the reports and
  proposals, rejects every proposal and resets the lab; results in `build/grok-bot-eval/results.json`.

### The Grok Bot app (outside agent)

The Grok Bot app works beside the crew through `/mcp` (MCP connector) or `uv run botctl call <tool>`. It reads,
proposes (a human approves) and leaves notes; in the room it is the visitor GROK. The pager sends it one webhook per
incident (`BOT_PAGER_URL`, `uv run botctl pager test|status|mute`). Setup and demo scripts:
[`bot/grokbot-app/`](../bot/grokbot-app/README.md).

## 6. Troubleshooting

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

**grok-bot** (the page shows "lab-api unreachable", no incidents, or changes refused):
- `docker compose ps grok-bot` and `make bot-logs`; `curl -s 127.0.0.1:8095/api/health` shows `lab_ok`, the
  effective brain and why.
- No incident for a fault: maintenance mode on (`botctl status`, the page header)? Problems still settling (the
  incident list says so)? Trap-only events never open incidents (by design).
- Approvals answer 401: the page carries the token of the bot that served it; reload the page after changing
  `BOT_OPERATOR_TOKEN`. `botctl` reads it from `.env`.
- A proposal went back to *pending* with a note: the device or NetBox changed after the dry run (lab-api 409); review
  the new diff and approve again.
- Grok errors: `botctl grok-check` names the cause (401 bad key, 403/402 no credits, 429 rate limit). The bot keeps
  working on the rules brain meanwhile.
