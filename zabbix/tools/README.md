# netsim-zabbix-tools (`netsim_zabbix`)

Zabbix 8.0 side of the NetSim L2VPN lab (owner: 04-zabbix). See [`zabbix/README.md`](../README.md) and
[`docs/reports/04-zabbix.md`](../../docs/reports/04-zabbix.md).

| Module | Entry point | What |
|---|---|---|
| `api` | | JSON-RPC client (httpx, `Authorization: Bearer`) |
| `settings` | | `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `NETSIM_BSM_SOURCE`, ... (environment, then `.env`) |
| `bootstrap` | `zabbix-bootstrap` | Admin password, host/template groups, API role/group/user/token, global macros, check-now script |
| `template` (+ `js/`) | `zabbix-template` | generates the template YAML from `contract/oids.yaml`; `canonical` = Zabbix's export order |
| `source` | | BSM spec + PE hosts from NetBox (default) or `contract/topology.yaml` |
| `provision` | `zabbix-provision` | template import, hosts, trap action, then `sync`, dashboard |
| `sync`, `bsm_sync`, `map_sync`, `spec_macros` | `zabbix-sync` | spec macros, BSM tree, SLAs, map (diff-based, idempotent) |
| `dashboard` | `zabbix-dashboard` | dashboard YAML import/export (Zabbix 8.0) |
| `status` | `zabbix-status` | BSM tree, current L2VPN problems, PW counts |
| `fixtures` | `zabbix-fixtures` | snmpsim fixtures and fixture traps |

Tests: `make zabbix-test` (unit, includes the JavaScript under Node.js when available), `make zabbix-test-integration`.
