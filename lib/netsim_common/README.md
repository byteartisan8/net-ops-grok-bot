# netsim-common

Shared library for the NetSim L2VPN lab (owner: 01-foundation). Pure Python (pydantic v2 + PyYAML), no network I/O.

| Module | Provides |
|---|---|
| `device_config` | Pydantic models of the `netsim-device` YANG in RESTCONF JSON (`DeviceConfig`, `Interface`, `Xconnect`, `Pseudowire`); `from_restconf()` accepts full datastores, `ietf-restconf:data` wrappers and (with `partial=True`) single containers; `effective_mtu()`, `admin_up()`, `duplicate_pseudowires()` |
| `canonical` | `canonical_json()` (golden-file format), `canonical_dict()`, `diff_configs()` -> `[ConfigDiff]` with `.path`, `.kind`, `.expected`, `.actual`, `.service_id`, `.pw_role`, `.message`, `.to_dict()` |
| `topology` | `load_topology()` -> `Topology` with lookups (`device()`, `service()`, `services_on()`, `pseudowires_on()`, `subinterfaces()`, `host_port()`) |
| `golden` | `render_device_config(topology, host)`, `render_golden(topology)` |
| `oids` | `load_oids()` -> `OidRegistry` (`oid()`, `instance()`, `enum()`, `value()`, `label()`, `notification()`, `name_of()`), `PYSNMP_TYPE_BY_BASE` |
| `bsm` | `BsmSpec` + builders, `bsm_spec_from_topology()`, `pw_spec_for_host()`, `pw_spec_macro_value()`, `PwCheck`, `StatusRules` |
| `signaling` | Redis key helpers (`advert_key`, `uplink_fault_key`, `ac_fault_key`, `cut_key`, `parse_fault_key`), `EVENTS_CHANNEL`, `HELLO_INTERVAL_S`, `HOLD_TIME_S`, `Advert` model |
| `enums` | `DeviceRole`, `PwRole`, `InterfaceType`, `SlaTier`, `PwOperStatus`, `IfOperStatus`, `AdminStatus`, `PwReason` (contract §7 table: `.rule`, `.oper_status`) |
| `constants` | `lab_constants()`: every address/port/URL derived from the topology |
| `paths` | `contract_dir()` (`$NETSIM_CONTRACT_DIR` or the repo's `contract/`) |
| `cli` | `netsim-contract` command (`canonical`, `diff`, `golden`, `pw-spec`, `bsm-spec`, `hosts`, `oid`) |

Example:

```python
from netsim_common import DeviceConfig, diff_configs, load_topology, render_device_config

intent = render_device_config(load_topology(), "spoke-2")
running = DeviceConfig.from_restconf(restconf_json)  # e.g. GET /restconf/data
for d in diff_configs(intent, running):
    print(d)  # l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4: expected 10.255.0.1, got 10.255.0.2
```

Tests: `uv run pytest lib/netsim_common` (plus the contract-level tests in the repo's `tests/`).
