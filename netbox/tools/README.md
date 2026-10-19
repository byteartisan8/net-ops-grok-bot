# netsim-netbox-tools

NetBox tools of the NetSim lab (owner: 03-netbox). Plain REST (httpx) against the pinned NetBox 4.7.3.

| Command | Module | Purpose |
|---|---|---|
| `netbox-bootstrap` | `netsim_netbox.bootstrap` | wait for NetBox, provision `NETBOX_TOKEN_RW` / `NETBOX_TOKEN_RO` (v2) into `.env` |
| `netbox-seed` | `netsim_netbox.seed` | make NetBox match `contract/topology.yaml` (idempotent, `--dry-run`, `--prune`, `--no-webhooks`) |
| `netbox-intent` | `netsim_netbox.intent` | intent from NetBox only: `render`, `render-all`, `bsm-spec`, bot queries, `journal` |

See `netbox/README.md` for the data model and `docs/reports/03-netbox.md` for the design decisions.
