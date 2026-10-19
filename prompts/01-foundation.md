# Session 01 — Foundation (repo, contract artifacts, shared library)

## Role
You are a senior network-automation engineer setting up a monorepo that later sessions will build on in parallel.
They will start from your output without asking you anything, so correctness and clarity matter more than speed.

## Read first
`docs/CONTRACT.md` is authoritative. Read all of it before doing anything.

## You own
`contract/`, `lib/netsim_common/`, root `compose.yaml`, `Makefile`, `pyproject.toml` (uv workspace), `.env.example`,
`.gitignore`, `README.md`, `docs/reports/01-foundation.md`. Do **not** create anything inside `devices/`, `netbox/`,
`zabbix/` or `lab/` except an empty placeholder `README.md` in each that states the owner session.

## Goal
Produce every shared artifact the other sessions consume: topology data, YANG model, pinned MIBs with resolved OIDs,
golden device configs, shared Python models, and the repo/compose/Makefile scaffolding.

## Tasks

1. **Repo scaffolding**
   - `git init`, `.gitignore` (Python, uv, `.env`, volumes, IDE files).
   - uv workspace `pyproject.toml` at root with members pre-declared for `lib/netsim_common`, `devices/simd`,
     `netbox/tools`, `zabbix/tools`, `lab`. Create minimal stub `pyproject.toml` files for the members you don't own
     so `uv sync` works today; owners will fill them in.
   - Tooling: ruff + pytest configured at root; Python 3.12.

2. **Compose & Makefile**
   - Root `compose.yaml` containing only `include:` of `devices/compose.yaml`, `netbox/compose.yaml`,
     `zabbix/compose.yaml`, `lab/compose.yaml`. Create each fragment as a valid empty stub
     (`services: {}` + external `labnet` network) so `docker compose config` passes now.
   - `Makefile` targets: `net` (create external `labnet` 172.30.10.0/24 idempotently), `up`, `down`, `ps`, `logs`,
     `build`, `seed` (delegates to `netbox-seed` and `zabbix-provision`, which owners implement), `reset`, `test`
     (unit), `e2e`, `lint`, `clean` (with an explicit warning that it deletes volumes). Targets for components not built yet
     must print a clear "not implemented yet (owner: session NN)" message and exit non-zero.
   - `.env.example` with every variable from contract §11 plus anything compose needs (DB passwords, NetBox secret key,
     superuser, Zabbix DB). Include a `make env` target that generates `.env` with random secrets if it's missing.

3. **`contract/topology.yaml`**
   - Encode contract §4–§5 exactly: devices (hostname, role, site, mgmt IP, loopback, interfaces with MTU),
     customers (name, `sla_tier` per contract §10) and services (id, customer, spoke, vlan, ACs, primary/backup vc-ids, hubs).
   - Write a JSON Schema for it (`contract/topology.schema.json`) and validate it in tests.
   - Optional but valuable: `tools/gen_topology.py --spokes N --services M` to generate bigger topologies for scale tests,
     using the same addressing and VC-ID scheme.

4. **YANG model `contract/yang/netsim-device.yang`**
   - Implement contract §6 exactly (namespace `urn:netsim:device`, prefix `nsd`, revision date today), including leafrefs,
     the `unique` statement, enums, defaults, patterns and `must` constraints where they help (e.g. `vlan-id` and `parent`
     required when `type = subinterface`). Keep it compatible with Clixon's YANG support (avoid exotic features).
   - Validate with `pyang --strict` and `yanglint`. Commit example instance files and validate them with `yanglint`.

5. **MIBs and OIDs**
   - Download and pin: `SNMPv2-MIB`, `IF-MIB`, `INET-ADDRESS-MIB`, `CISCO-IETF-PW-MIB`, `CISCO-IETF-PW-TC-MIB`,
     `CISCO-CONFIG-MAN-MIB`, plus their dependencies (e.g. `CISCO-SMI`, `CISCO-TC`, IANA TCs) into `contract/mibs/`.
     Use reputable sources (e.g. `github.com/cisco/cisco-mibs`) and record URL, commit/tag and sha256 per file in
     `contract/mibs/SOURCES.md`.
   - Using `pysmi`/`pysnmp` (not memory), resolve and write `contract/oids.yaml`: numeric OID, syntax, and enum values
     for every object and notification listed in contract §8, plus each notification's varbind list (OBJECTS clause).
   - Add a test that re-resolves from the MIB files and asserts `oids.yaml` matches.

6. **Shared library `lib/netsim_common`**
   - Pydantic v2 models for the device config (mirroring the YANG and RESTCONF JSON encoding with module-qualified
     top-level keys), topology model, enums (roles, oper status, reason codes from contract §7).
   - `canonical_json(config) -> str` (sorted lists by key, `sort_keys=True`) and `diff_configs(a, b)`, which returns a
     structured, human-readable diff (path, expected, actual). The bot and drift checker will rely on this, so make the diff
     output clear, e.g. `l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4: expected 10.255.0.1, got 10.255.0.2`.
   - `load_topology()`, `load_oids()`, constants for ports/IPs derived from topology (no duplicated literals).
   - **BSM spec**: a `BsmSpec` pydantic model matching contract §9.3, plus `contract/bsm-spec.schema.json` generated from it.
     Add `bsm_spec_from_topology(topology) -> BsmSpec` (Zabbix uses it for parallel development and NetBox's version must
     match it) and `pw_spec_for_host(spec, host)`, which returns the `{$NETSIM.PW.SPEC}` list for one host (contract §9.2).
     Test: the spec from `topology.yaml` has 4 customers, 6 services and 12 PW leaves, and validates against the schema.

7. **Golden configs**
   - `tools/render_golden.py` renders `contract/golden/<host>.json` from `topology.yaml` per contract §6 rules.
   - Validate every golden file against the YANG with `yanglint` and against the pydantic models.
   - Test: spoke-1 golden equals the contract's example byte-for-byte after canonicalization.

8. **Docs**
   - Root `README.md`: purpose, architecture diagram (mermaid), run order, make targets, port table.
   - `docs/reports/01-foundation.md`: what you built, pinned versions, any contract changes, how to verify.

## Acceptance criteria (run them all and paste the results into your report)
- `uv sync && make lint && make test` pass.
- `docker compose config` passes with the stub fragments.
- `make net` is idempotent.
- `pyang`/`yanglint` validate the YANG and all golden configs.
- `contract/oids.yaml` is generated from the pinned MIB files and its test passes.
- `contract/bsm-spec.schema.json` exists and the topology-derived BSM spec validates against it.

## Working rules
- Verify every external fact (OIDs, tool flags, image tags) against the source; never guess. If a pinned version doesn't
  exist, pick the closest stable one and record it.
- Write idempotent, deterministic scripts; no manual UI steps.
- If the contract is ambiguous, choose the option that keeps other sessions unblocked, log it in the contract change log,
  and call it out in your report.
- Don't build devices, NetBox, Zabbix or the bot.
