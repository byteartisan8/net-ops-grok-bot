# Session 01 — Foundation report

**Date:** 2026-10-19 (artifacts first written 2026-10-18; this report records the end-to-end verification).
**Owner paths:** `contract/`, `lib/netsim_common/`, root `compose.yaml`, `Makefile`, `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`, `tools/`, `tests/`, this report.
**Not built:** devices, NetBox, Zabbix, lab-api, the Grok bot.

Later sessions start from these artifacts. Do not invent OIDs, ports, JSON keys or YANG leaves — load them from `contract/` and `netsim_common`.

---

## What was built

### Repo scaffolding
- Git repository initialized on `main` (no commits yet; working tree is the foundation).
- uv workspace at the root (`requires-python = "==3.12.*"`, Python resolved to **3.12.15**). Members:
  - `lib/netsim_common` (`netsim-common`) — real package.
  - `devices/simd`, `netbox/tools`, `zabbix/tools`, `lab` — stub `pyproject.toml` so `uv sync` works today. Owners replace them freely (update the root workspace table if the distribution name changes).
- ruff 0.17.1 + pytest 9.1.1 configured at the root. `.python-version` is `3.12`.
- `.gitignore`: Python, uv (`.venv`), `.env`, local volumes, `.scratch/`, IDE files.
- Empty owner placeholders only: `devices/README.md`, `netbox/README.md`, `zabbix/README.md`, `lab/README.md` plus stub compose fragments and stub member projects.

### Compose and Makefile
- Root `compose.yaml` is only `name: netsim` + `include:` of the four fragments. Project name is fixed so volume names stay `netsim_<volume>` across `make down && make up`.
- Each fragment is `services: {}` plus the **external** network `labnet`. `docker compose config` therefore succeeds now.
- `Makefile` targets: `doctor`, `env`, `sync`, `net`, `up`, `down`, `ps`, `logs`, `build`, `seed`, `reset`, `test`, `e2e`, `lint`, `fmt`, `contract`, `contract-check`, `yang-clixon`, `clean`.
  - `make net` creates `labnet` `172.30.10.0/24` gw `172.30.10.1` idempotently; refuses to reuse a same-named network with different settings.
  - `make seed` → `netbox-seed` then `zabbix-provision`.
  - `make reset` → `lab-reset`; `make e2e` → `lab-e2e`.
  - Component targets live in `<component>/<component>.mk` (included last). Until then, `make devices-*` / `netbox-*` / `zabbix-*` / `lab-*` print `not implemented yet (owner: session NN)` and exit 1.
  - `make clean` warns that it deletes volumes; requires `CONFIRM=yes` when stdin is not a TTY.
  - `make env` (`tools/gen_env.py`) copies `.env.example` → `.env`, replaces every `__generate__` with a random secret, never overwrites existing values, only appends new variables. Tokens that the owning component writes stay empty.

`.env.example` has every contract §11 token plus compose secrets (NetBox secret key / superuser / DB / Redis, Zabbix DB / admin / API user) and the device-side `SNMP_COMMUNITY`, `CORE_URL`, `TRAP_TARGETS=172.30.10.42:1162`. Other sessions may append their own section and must log it in the contract.

### Topology
- `contract/topology.yaml` encodes contract §4–§5 and §10: six PEs (hostname, role, site, mgmt IP, loopback, physical interfaces with MTU), four customers with `sla_tier`, six services with ACs, VLANs, primary/backup VC IDs and hubs. Also the labnet, published-port bases, infrastructure hosts, and SNMP communities.
- `contract/topology.schema.json` is generated from `netsim_common.topology.Topology`. Tests validate the file against it.
- `tools/gen_topology.py --spokes N --services M` grows a topology with the same addressing (`172.30.10.<21+i>`, `10.255.1.<1+i>`) and VC-ID scheme (`1000+n` / `2000+n`). `--spokes 4 --services 6 --customers 4` reproduces the lab file.

### YANG `netsim-device`
- `contract/yang/netsim-device.yang`: namespace `urn:netsim:device`, prefix `nsd`, revision **2026-10-18**.
- Leafrefs, `unique "peer-ipv4 vc-id"` on each xconnect, enums, defaults, `must` rules (subinterface requires `parent` + `vlan-id`; only subinterfaces may have them; parent must not itself be a subinterface), lengths tied to the SNMP objects that carry the values.
- `service-id` pattern is `SVC-[0-9]{4}` (ASCII digits; Clixon translates patterns to POSIX).
- Valid / invalid instance files in `contract/yang/examples/` plus `expectations.yaml`.
- Tree dump: `contract/yang/netsim-device.tree`.
- Validated with `pyang --strict -Werror`, yanglint 6.4.13 (`-t config` on every golden file), and a real Clixon **7.9.0** (`make yang-clixon`: 22/22 cases).

### MIBs and OIDs
Pinned under `contract/mibs/` (URL, commit/tag, sha256 in `SOURCES.md`):

| Source | Ref | Modules |
|---|---|---|
| [net-snmp/net-snmp](https://github.com/net-snmp/net-snmp) tag `v5.9.5.2` (`319bbd0bb365`) | IETF/IANA | `SNMPv2-SMI`, `SNMPv2-TC`, `SNMPv2-CONF`, `SNMPv2-MIB`, `SNMP-FRAMEWORK-MIB`, `IANAifType-MIB`, `IF-MIB`, `INET-ADDRESS-MIB` |
| [cisco/cisco-mibs](https://github.com/cisco/cisco-mibs) `main` @ `c9ab98c03ee0` | Cisco | `CISCO-SMI`, `CISCO-TC`, `CISCO-IETF-PW-TC-MIB`, `CISCO-IETF-PW-MIB`, `CISCO-CONFIG-MAN-MIB` |

`tools/gen_oids.py` compiles those files with **pysmi 2.0.0** (no network, no bundled MIBs) into `contract/oids.yaml`: numeric OID, kind, access, syntax (`type`, SMI `base`, `enum`, range/size), and each notification's OBJECTS in order plus the index objects. `tests/test_oids.py` re-resolves and asserts a byte-for-byte match.

Facts later sessions must not re-guess:
- `cpwVcDown` / `cpwVcUp` carry `cpwVcOperStatus` **twice** (lowest and highest affected `cpwVcIndex`).
- In this MIB revision (2019-03-11) `ciscoConfigManEvent` has **four** varbinds: `ccmHistoryEventCommandSource`, `ccmHistoryEventConfigSource`, `ccmHistoryEventConfigDestination`, `ccmHistoryEventTerminalUser` (all indexed by `ccmHistoryEventIndex`).
- A v2c trap is `sysUpTime.0`, `snmpTrapOID.0`, then those OBJECTS (RFC 3416 §4.2.6).
- Access via `load_oids()` / `OidRegistry.instance("cpwVcOperStatus", 3)` → `1.3.6.1.4.1.9.10.106.1.2.1.26.3`.

### Shared library `lib/netsim_common`
Pydantic v2 models mirroring the YANG RESTCONF JSON (module-qualified top-level keys `netsim-device:system` / `:interfaces` / `:l2vpn`). Public surface:

| Entry point | Use |
|---|---|
| `load_topology()`, `Topology.host_port()`, `lab_constants()` | Addresses and ports from the topology — no duplicated literals |
| `DeviceConfig`, `canonical_json()`, `diff_configs()` | Config I/O and drift. Diff example: `l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4: expected 10.255.0.1, got 10.255.0.2` |
| `render_device_config(topo, host)` | Golden / intent of one host |
| `BsmSpec`, `bsm_spec_from_topology()`, `pw_spec_for_host()`, `pw_spec_macro_value()` | BSM spec and `{$NETSIM.PW.SPEC}` |
| `load_oids()` | Typed OID registry |
| `PwReason`, `PwOperStatus`, `IfOperStatus`, … | Contract §7 / MIB enums |
| `netsim-contract` CLI | `hosts`, `golden`, `diff`, `pw-spec`, `bsm-spec`, `oid`, `canonical` |

Canonical form (the only implementation everyone should use): parse with `DeviceConfig` (defaults filled), sort `interface` by `name`, `xconnect` by `service-id`, `pseudowire` by YANG enum order (`primary` before `backup`), drop empty lists/containers, `json.dumps(sort_keys=True, indent=2, ensure_ascii=False)` + trailing newline.

`BsmSpec` matches contract §9.3 (builders, `normalized()` comparison, optional `netbox_url`). Schema `contract/bsm-spec.schema.json` is generated from the model. Topology-derived spec: **4 customers, 6 services, 12 PW leaves**; validates against the schema. A hub's `{$NETSIM.PW.SPEC}` has 6 compact-JSON entries.

### Golden configs
`tools/render_golden.py` writes `contract/golden/<host>.json` from the topology. Every file is canonical JSON, yanglint-valid, pydantic-valid. spoke-1 equals the contract's RESTCONF example after canonicalization (byte-for-byte).

### Docs
- Root `README.md`: purpose, mermaid architecture, run order, make targets, port table.
- `contract/README.md` and `lib/netsim_common/README.md` for consumers.

---

## Pinned versions

Verified they exist before pinning. `uv.lock` holds the full Python closure.

| Thing | Pin | How verified |
|---|---|---|
| uv | 0.13.1 | `uv --version` |
| Python | 3.12 (`==3.12.*`; resolved **3.12.15**) | uv |
| pydantic | 2.14.1 | uv.lock / import |
| PyYAML | 6.0.3 | uv.lock / import |
| pysmi | 2.0.0 | uv.lock / import |
| **pysnmp** | **7.1.30** | uv.lock — **use the same in simd** |
| pyang | 2.7.1 | uv.lock |
| jsonschema | 4.26.0 | uv.lock |
| pytest | 9.1.1 | uv.lock |
| ruff | 0.17.1 | uv.lock |
| hatchling | 1.32.4 | member `pyproject.toml` build-system |
| libyang / yanglint | **6.4.13** (`f7e0cc1bc99e`) | `tools/docker/yanglint.Dockerfile`; `yanglint 6.4.13` |
| Alpine (check images) | 3.24.2 | Dockerfiles |
| **Clixon / CLIgen** | **7.9.0** (clixon `98178407d162`, cligen `9fd27a8e85ea`) | latest tags at pin time; `make yang-clixon` |
| Docker Engine / Compose on this host | 29.8.1 / 5.5.1 | `make doctor` (contract requires 24+ / 2.20+) |
| NetBox / Zabbix / Redis / snmpsim images | not pinned here | owners 02–04 pin and log |

---

## Contract changes

Already recorded in `docs/CONTRACT.md` §13 (2026-10-18, session 01-foundation). Summary of the ambiguities this session closed so later sessions stay unblocked:

| Topic | Choice |
|---|---|
| §2 versions | Recorded the pins above, including pysnmp 7.1.30 and Clixon 7.9.0. |
| §3 ownership | `tools/` and `tests/` are 01-foundation; component make targets live in `<component>/<component>.mk`; `make seed/reset/e2e` delegate; root compose sets `name: netsim`; others may append `.env.example` variables. |
| §4 ports | Host port = `18000` / `19000` / `16100` + last octet of mgmt IP (same numbers as the table; also valid for 3-digit octets in generated topologies). Encoded as `device_ports` in `topology.yaml`. |
| §6 canonical form | Defaults filled; lists sorted as above; empty containers dropped; indent 2 + newline. Physical-without-`mtu` → 1500; PW MTU = AC effective MTU (`DeviceConfig.effective_mtu()`). `SVC-[0-9]{4}`. Golden descriptions: `CORE` / `CUSTOMER-ACCESS` / `"<customer> <service-id>"`; every `enabled` spelled out. |
| §8 traps | `TRAP_TARGETS` default `172.30.10.42:1162` from zabbix-docker 8.0 snmptraps `EXPOSE 1162/udp` (04-zabbix must confirm on a running container). `oids.yaml` format and the four `ciscoConfigManEvent` varbinds. |
| §9.2 / §9.3 | Exact `{$NETSIM.PW.SPEC}` keys and sort order; optional `netbox_url`; builders; `normalized()`; 1–2 PW leaves per service. Additive — the contract example stays valid. |

No new contract edits were required on 2026-10-19. The YANG revision date stays **2026-10-18** (date the model was written).

---

## How to verify

```bash
uv sync
make lint          # ruff + pyang --strict + yanglint on the YANG and all golden configs
make test          # 165 unit tests; no running lab
docker compose config
make net           # twice: second run is a no-op
make contract-check
make yang-clixon   # optional, slower first image build; Clixon 7.9.0
```

Until owners land: `make seed`, `make e2e`, `make reset`, `make up`, `make build` fail with a clear owner message.

Regenerate generated files after changing the topology, YANG, models or MIBs:

```bash
make contract      # golden, oids.yaml, schemas, YANG tree
```

`make test` / `make contract-check` fail if a generated file is stale.

---

## Acceptance criteria (run 2026-10-19)

### `uv sync && make lint && make test`

```
===== uv sync =====
Resolved 37 packages in 2ms
Checked 35 packages in 0.56ms
===== make lint =====
uv run ruff check .
All checks passed!
uv run ruff format --check .
48 files already formatted
uv run pyang --strict -Werror -p contract/yang contract/yang/netsim-device.yang
tools/yanglint.sh -D -p contract/yang contract/yang/netsim-device.yang
tools/yanglint.sh -D -p contract/yang -t config contract/yang/netsim-device.yang contract/golden/*.json
===== make test =====
uv run pytest -m "not integration and not e2e"
============================= test session starts ==============================
platform linux -- Python 3.12.15, pytest-9.1.1, pluggy-1.7.0
rootdir: /home/luminaire/Work/Experiment/net-ops-grok-bot
configfile: pyproject.toml
testpaths: tests, lib, devices, netbox, zabbix, lab
collected 165 items

tests/test_bsm.py .......                                                [  4%]
tests/test_golden.py ...........                                         [ 10%]
tests/test_mibs.py ...                                                   [ 12%]
tests/test_oids.py .....................................                 [ 35%]
tests/test_scaffolding.py ...............                                [ 44%]
tests/test_topology.py ......................                            [ 57%]
tests/test_yang.py .....................................                 [ 80%]
lib/netsim_common/tests/test_canonical.py .......                        [ 84%]
lib/netsim_common/tests/test_cli_and_paths.py ......                     [ 87%]
lib/netsim_common/tests/test_device_config.py ............               [ 95%]
lib/netsim_common/tests/test_signaling_and_enums.py ........             [100%]

============================= 165 passed in 8.52s ==============================
```

`make lint` also ran `pyang --strict` and yanglint against the YANG and every `contract/golden/*.json` (no extra output = success).

### `docker compose config`

```
name: netsim
services: {}
```

Exit 0. The four fragments are empty stubs attached to external `labnet`.

### `make net` is idempotent

```
=== make net (1) ===
labnet: already exists (172.30.10.0/24, gateway 172.30.10.1) - nothing to do
=== make net (2) ===
labnet: already exists (172.30.10.0/24, gateway 172.30.10.1) - nothing to do
=== docker network ===
name=labnet subnet=172.30.10.0/24 gw=172.30.10.1
```

### `contract/oids.yaml` generated from pinned MIBs

```
uv run python tools/fetch_mibs.py --check
OK: 13 MIB files match contract/mibs/SOURCES.md
uv run python tools/gen_oids.py --check
OK: contract/oids.yaml matches the pinned MIBs
```

`tests/test_oids.py` (37 cases) re-resolves the same files and asserts equality.

### `contract/bsm-spec.schema.json` + topology-derived spec

File exists (`urn:netsim:schema:bsm-spec`, generated from `BsmSpec`). `tests/test_bsm.py` asserts 4 customers / 6 services / 12 PW leaves and `jsonschema.validate(...)` against that schema. `make contract-check` also reported `OK: 2 schemas are up to date`.

### Extra (not required by the prompt, recorded because the contract cites them)

```
make yang-clixon
Clixon version: 7.9.0-1: netsim-device loaded
  ok    contract/golden/{hub-a,hub-b,spoke-1,spoke-2,spoke-3,spoke-4}.json: valid
  … valid examples accepted, invalid examples rejected as expected …
22/22 cases behave as expected in Clixon
```

Placeholder targets (expected fail):

```
make seed   → make netbox-seed: not implemented yet (owner: session 03-netbox; define it in netbox/netbox.mk)
make e2e    → make lab-e2e: not implemented yet (owner: session 05-integration; define it in lab/lab.mk)
make reset  → make lab-reset: not implemented yet (owner: session 05-integration; define it in lab/lab.mk)
```

---

## Notes for sessions 02–05

- Import `netsim_common`; do not copy models, OIDs, or port numbers.
- Device image (02) should compile Clixon **7.9.0** (same commits as `tools/docker/clixon-check.Dockerfile`) and use **pysnmp 7.1.30**.
- NetBox intent renderer (03) must emit canonical JSON equal to `contract/golden/<host>.json`. `netbox-intent bsm-spec` must satisfy `produced.normalized() == bsm_spec_from_topology(load_topology())`.
- Zabbix (04) consumes `BsmSpec` and `pw_spec_macro_value(spec, host)`. Confirm trap port 1162 on the running snmptraps container and log it.
- Put component Make targets in `<dir>/<dir>.mk` with prefixes `devices-`, `netbox-`, `zabbix-`, `lab-`. Do not edit the root Makefile placeholders; `-include` lets a real rule win.
- Attach every published service to the external network `labnet`. Never redefine that subnet in a fragment.
- `NETSIM_CONTRACT_DIR` overrides the contract path inside a container (default: the repo's `contract/`).
