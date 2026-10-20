# lab/ — integration: labctl, lab-api, scenarios, drift checker, e2e tests (owner: session 05-integration)

Everything that ties the four components (devices, NetBox, Zabbix, contract) into one lab you can drive and observe,
plus the tool surface the Grok bot will use later. Runbook: [`docs/RUNBOOK.md`](../docs/RUNBOOK.md); report:
[`docs/reports/05-integration.md`](../docs/reports/05-integration.md).

```
lab/
  src/netsim_lab/      the netsim-lab package (uv workspace member)
    lab.py             one object for the lab: devices (RESTCONF + simd), core, Docker, NetBox intent, Zabbix
    drift.py           NetBox intent vs running config -> classified diffs + one semantic finding per service
    status.py          intent + live state + Zabbix status per service (labctl status, GET /services)
    impact.py          blast radius (NetBox) + live state of the surviving PWs (labctl impact, GET /impact/{host})
    changes.py         propose -> approve (plan_id) -> apply -> verify (lab-api config / apply-intent)
    scenarios.py       declarative scenario engine (steps, expectations, follow-up phases)
    reset.py           make reset: golden configs, faults cleared, NetBox/Zabbix back to the topology, wait green
    correlate.py       group Zabbix problems by service tag + start time (one root cause)
    configpath.py      config paths (drift notation) and patches (set/delete, RFC 6902)
    api/               lab-api (FastAPI): app.py, models.py, server.py
    cli.py             labctl (Typer)
  scenarios/*.yaml     the scenarios (format: scenarios/README.md)
  tests/               unit tests (no containers; part of `make test`)
  e2e/                 end-to-end tests on the running lab (`make e2e`)
  openapi.json         lab-api's OpenAPI document (`make lab-openapi`; checked by a unit test)
  Dockerfile, compose.yaml, lab.mk
```

## labctl

```bash
uv run labctl status --watch            # the demo's live view: per service, intent | spoke end | hub end | Zabbix
uv run labctl fault uplink hub-a        # physical fault (core key); --restore to repair
uv run labctl fault ac spoke-1 GigabitEthernet0/0/1.101
uv run labctl cut spoke-3 hub-b         # core path between two PEs; --restore
uv run labctl device stop hub-b         # power off (docker stop); start
uv run labctl fault clear-all           # every fault key gone, every device powered on
uv run labctl config get spoke-1 'l2vpn/xconnect[SVC-0001]'
uv run labctl config set spoke-1 'l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id' 2010   # one RESTCONF commit
uv run labctl config diff spoke-1       # NetBox intent vs running (exit 1 if different)
uv run labctl drift [--json]            # every device + semantic findings; exit 1 on drift
uv run labctl impact hub-a              # what maintenance on hub-a would do now
uv run labctl scenario list | show NAME | apply NAME | verify NAME | check NAME | run NAME
uv run labctl reset                     # = make reset
uv run labctl events --since 5m [-f]    # simd events of every PE, merged
uv run labctl sync [--local]            # NetBox -> Zabbix sync (through lab-api by default)
uv run labctl wait-healthy | wait       # bring-up helpers (containers healthy / lab green)
```

labctl acts directly on the lab (RESTCONF, core keys, Docker), like an engineer with hands on the routers: its
changes are *not* in lab-api's audit log. lab-api is the audited path (the bot's).

Config paths are the paths the drift checker prints: `l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id`,
`interfaces/interface[GigabitEthernet0/0/2]/mtu`, `l2vpn/xconnect[SVC-0002]` (a whole entry).

## lab-api

FastAPI at `172.30.10.50:8090` (host `127.0.0.1:8090`), OpenAPI in [`openapi.json`](openapi.json), interactive docs
at <http://127.0.0.1:8090/docs>.

| | Endpoints |
|---|---|
| reads (no token) | `GET /health`, `/topology`, `/services`, `/services/{id}`, `/devices`, `/devices/{host}/running`, `/devices/{host}/state`, `/devices/{host}/intent`, `/drift`, `/impact/{host}`, `/zabbix/problems?service=`, `/events?since=`, `/faults`, `/scenarios`, `/scenarios/{name}`, `/scenarios/{name}/check`, `/sync`, `/hooks/netbox`, `/audit` |
| writes (`X-Lab-Token`) | `POST /faults`, `DELETE /faults`, `POST /devices/{host}/config`, `POST /devices/{host}/apply-intent`, `POST /scenarios/{name}/apply`, `POST /reset`, `POST /sync` |
| NetBox webhook | `POST /hooks/netbox` (HMAC-SHA512 `X-Hook-Signature`, `NETBOX_WEBHOOK_SECRET`) -> debounced `zabbix-sync` |

Every write is recorded in the audit log (`/var/lib/lab-api/audit.jsonl` on the `netsim_lab-api-data` volume,
readable via `GET /audit`): who (`X-Lab-Actor` + client address), when, what, the diff, the result.

**Device changes** always go propose -> approve -> apply -> verify:

```bash
TOKEN=$(grep ^LAB_API_TOKEN= .env | cut -d= -f2-)
# 1. dry run: diff, validation, affected services/hosts, drift findings before and predicted after, plan_id
curl -s -X POST localhost:8090/devices/spoke-1/apply-intent -H "X-Lab-Token: $TOKEN" -H 'X-Lab-Actor: me' \
  -H 'Content-Type: application/json' -d '{}'
# 2. apply exactly that plan; the response carries the verification (simd, Zabbix, drift) and a verdict
curl -s -X POST localhost:8090/devices/spoke-1/apply-intent -H "X-Lab-Token: $TOKEN" -H 'X-Lab-Actor: me' \
  -H 'Content-Type: application/json' -d '{"confirm": true, "plan_id": "<from step 1>"}'
```

`POST /devices/{host}/config` takes a `patch` (`[{"op": "set", "path": ..., "value": ...}, {"op": "delete",
"path": ...}]`, or RFC 6902 JSON Patch with JSON Pointers) and works the same way. A `plan_id` that no longer matches
(the device changed since the dry run) is refused with HTTP 409 and the new plan.

## Drift checker

`labctl drift` / `GET /drift` compares, for every PE, NetBox's rendered intent with the running config
(`diff_configs`), then explains each service:

| class | meaning | example |
|---|---|---|
| `config-drift` | the device differs from NetBox (someone changed the device) | `SVC-0001: backup vc-id mismatch spoke-1=2010 vs hub-b=2001` |
| `intent-only` | NetBox changed, the device was not updated yet | `SVC-0007: in NetBox but not configured on hub-a, hub-b, spoke-3 (ordered, not delivered)` |
| `fault` | PWs down by a physical cause, config as NetBox says | `SVC-0001: primary PW down: hub-a core uplink down (fault); service degraded` |

`config-drift` vs `intent-only` is decided per difference against the device's **baseline**: the last config on which
device and NetBox agreed (`netsim:lab:baseline:<host>` on `core`; golden when absent). If the device still equals its
baseline, NetBox moved (`intent-only`); otherwise the device moved (`config-drift`). Baselines are recorded when
lab-api applies intent and whenever a check sees device == intent, and cleared by `reset`.

## Tests

```bash
make lab-test     # unit tests (drift semantics, paths/patches, scenario files, lab-api auth/webhook/audit, ...)
make e2e          # on the running lab, ~10 min; timing table at the end + build/lab-e2e/timings.json
```
