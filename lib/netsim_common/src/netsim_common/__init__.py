"""Shared models and contract helpers for the NetSim L2VPN lab (owner: 01-foundation).

Most-used entry points::

    from netsim_common import (
        load_topology, load_oids,                      # contract/topology.yaml, contract/oids.yaml
        DeviceConfig, canonical_json, diff_configs,    # netsim-device YANG config (RESTCONF JSON)
        render_device_config,                          # golden config of a host
        BsmSpec, bsm_spec_from_topology, pw_spec_for_host,
    )
"""

from netsim_common.bsm import (
    BsmSpec,
    PwCheck,
    PwSpecEntry,
    bsm_spec_from_topology,
    pw_spec_for_host,
    pw_spec_macro_value,
)
from netsim_common.canonical import ConfigDiff, DiffKind, canonical_dict, canonical_json, diff_configs, format_diffs
from netsim_common.constants import LabConstants, lab_constants
from netsim_common.device_config import DeviceConfig, Interface, Pseudowire, Xconnect
from netsim_common.enums import (
    AdminStatus,
    DeviceRole,
    IfOperStatus,
    InterfaceType,
    PwOperStatus,
    PwReason,
    PwRole,
    SlaTier,
)
from netsim_common.golden import render_device_config, render_golden
from netsim_common.oids import OidRegistry, load_oids
from netsim_common.paths import contract_dir, repo_root
from netsim_common.topology import Topology, load_topology

__version__ = "0.1.0"

__all__ = [
    "AdminStatus",
    "BsmSpec",
    "ConfigDiff",
    "DeviceConfig",
    "DeviceRole",
    "DiffKind",
    "IfOperStatus",
    "Interface",
    "InterfaceType",
    "LabConstants",
    "OidRegistry",
    "Pseudowire",
    "PwCheck",
    "PwOperStatus",
    "PwReason",
    "PwRole",
    "PwSpecEntry",
    "SlaTier",
    "Topology",
    "Xconnect",
    "bsm_spec_from_topology",
    "canonical_dict",
    "canonical_json",
    "contract_dir",
    "diff_configs",
    "format_diffs",
    "lab_constants",
    "load_oids",
    "load_topology",
    "pw_spec_for_host",
    "pw_spec_macro_value",
    "render_device_config",
    "render_golden",
    "repo_root",
]
