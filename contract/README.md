# contract/ - shared artifacts (owner: 01-foundation)

Everything here is consumed by the other sessions. `docs/CONTRACT.md` is the prose contract; these are its
machine-readable parts. Files marked *generated* must not be edited by hand: change the input, run `make contract`,
and `make test` / `make contract-check` verify that every generated file is current.

| Path | What | Produced by |
|---|---|---|
| `topology.yaml` | Lab topology: network, ports, devices, physical interfaces, customers, services (contract §4, §5, §10) | hand-written |
| `topology.schema.json` | JSON Schema of `topology.yaml` | *generated* from `netsim_common.topology.Topology` (`tools/gen_schemas.py`) |
| `yang/netsim-device.yang` | Device config model (`urn:netsim:device`, prefix `nsd`), contract §6 | hand-written |
| `yang/netsim-device.tree` | `pyang -f tree` view of the model | *generated* (`make contract`) |
| `yang/examples/` | Valid and invalid instance files + `expectations.yaml` (expected yanglint/model errors) | hand-written |
| `golden/<host>.json` | Golden running config per device, canonical JSON | *generated* from the topology (`tools/render_golden.py`) |
| `mibs/` | Pinned MIB modules + `SOURCES.md` (URL, commit, sha256 per file) | `tools/fetch_mibs.py` |
| `oids.yaml` | Numeric OIDs, syntax, enums, notification varbinds for contract §8 | *generated* from `mibs/` with pysmi (`tools/gen_oids.py`) |
| `bsm-spec.schema.json` | JSON Schema of the BSM spec (contract §9.3) | *generated* from `netsim_common.bsm.BsmSpec` |

## Using it from Python

```python
from netsim_common import (
    load_topology,
    load_oids,
    lab_constants,  # topology.yaml, oids.yaml, derived addresses/ports
    DeviceConfig,
    canonical_json,
    diff_configs,  # YANG config models, canonical form, structured diff
    render_device_config,  # golden config of a host
    bsm_spec_from_topology,
    pw_spec_for_host,
    pw_spec_macro_value,
)

topo = load_topology()
oids = load_oids()
oids.instance("cpwVcOperStatus", 3)  # '1.3.6.1.4.1.9.10.106.1.2.1.26.3'
lab_constants().devices["spoke-1"].restconf_url  # 'http://127.0.0.1:18021'
```

Inside a container, set `NETSIM_CONTRACT_DIR` to wherever this directory is mounted (e.g. `/opt/netsim/contract`).

## From the shell

```bash
uv run netsim-contract hosts                          # addresses and published ports
uv run netsim-contract golden spoke-1                 # golden config (same as golden/spoke-1.json)
uv run netsim-contract diff golden/spoke-1.json running.json   # structured diff, exit 1 if different
uv run netsim-contract pw-spec hub-a --pretty         # {$NETSIM.PW.SPEC} for hub-a (topology-derived)
uv run netsim-contract oid cpwVcDown                  # numeric OID
tools/yanglint.sh -D -p contract/yang -t config contract/yang/netsim-device.yang running.json
```

## Bigger topologies

`uv run python tools/gen_topology.py --spokes 20 --services 120 --customers 8 > /tmp/topo.yaml` extends this topology
with the same addressing and VC ID schemes (`--spokes 4 --services 6 --customers 4` reproduces it). Load it with
`load_topology("/tmp/topo.yaml")` or render its golden configs with `tools/render_golden.py --topology ... --out ...`.
