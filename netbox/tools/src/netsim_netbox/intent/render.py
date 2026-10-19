"""Render a device's intended config (``netsim-device`` YANG, RESTCONF JSON) from NetBox intent only.

Contract section 10 rule: every pseudowire terminating on the device becomes a PW of xconnect ``service_id`` with
``role = pw_role``, ``vc-id = identifier``, ``peer-ipv4`` = the other end's loopback, AC = the local end's
subinterface; the xconnect's ``customer`` is the tenant. Interfaces and the loopback are described in
:mod:`netsim_netbox.intent.model`. Every ``enabled`` is spelled out; output is compared with
:func:`netsim_common.canonical_json` / :func:`netsim_common.diff_configs`, so for a freshly seeded NetBox it equals
``contract/golden/<host>.json`` byte for byte.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from netsim_common.canonical import canonical_json
from netsim_common.device_config import DeviceConfig, Interface, Interfaces, L2vpn, Pseudowire, System, Xconnect
from netsim_netbox.intent.model import IntentError, IntentProblem, LabIntent


def render_device(intent: LabIntent, host: str) -> DeviceConfig:
    """Intended config of ``host``; :class:`IntentError` if NetBox's data for it cannot be interpreted."""
    device = intent.device(host)
    if problems := intent.problems_for(host):
        raise IntentError(problems, f"the intent of {host}")
    try:
        interfaces = [
            Interface(
                name=i.name,
                type=i.type,
                enabled=i.enabled,
                mtu=i.mtu,
                description=i.description,
                parent=i.parent,
                vlan_id=i.vlan_id,
            )
            for i in device.interfaces
        ]
        by_service: dict[str, list] = defaultdict(list)
        for pw in intent.pws_on(host):
            by_service[pw.service_id].append(pw)
        xconnects = []
        for service_id, pws in sorted(by_service.items()):
            local, _ = pws[0].local_remote(host)
            xconnects.append(
                Xconnect(
                    service_id=service_id,
                    customer=pws[0].customer,
                    attachment_circuit=local.ac,
                    enabled=True,
                    pseudowire=[
                        Pseudowire(
                            role=pw.role, peer_ipv4=pw.local_remote(host)[1].loopback, vc_id=pw.vc_id, enabled=True
                        )
                        for pw in pws
                    ],
                )
            )
        return DeviceConfig(
            system=System(hostname=host, loopback_ipv4=device.loopback),
            interfaces=Interfaces(interface=interfaces),
            l2vpn=L2vpn(xconnect=xconnects),
        )
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}" for e in exc.errors())
        problem = IntentProblem(
            f"{host}: the config NetBox describes is not valid netsim-device: {errors}",
            "device",
            frozenset({host}),
            device.url,
        )
        raise IntentError([problem], f"the intent of {host}") from exc


def render_all(intent: LabIntent) -> dict[str, DeviceConfig]:
    """Every netsim device (sorted by name). Raises :class:`IntentError` listing the problems of all devices."""
    out: dict[str, DeviceConfig] = {}
    problems: list[IntentProblem] = []
    for host in sorted(intent.devices):
        try:
            out[host] = render_device(intent, host)
        except IntentError as exc:
            problems += [p for p in exc.problems if p not in problems]
    if problems:
        raise IntentError(problems, "the device intents")
    return out


def write_configs(configs: dict[str, DeviceConfig], out_dir: Path) -> list[Path]:
    """``<out_dir>/<host>.json`` in canonical JSON (the golden-file format)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for host, config in configs.items():
        path = out_dir / f"{host}.json"
        path.write_text(canonical_json(config), encoding="utf-8")
        paths.append(path)
    return paths


def yanglint(paths: list[Path]) -> subprocess.CompletedProcess[str]:
    """Validate config files with the pinned yanglint (``tools/yanglint.sh``; files must be inside the repository).

    Set ``YANGLINT=/path/to/yanglint`` to use a native binary instead of the container image.
    """
    from netsim_common.paths import repo_root

    repo = repo_root()
    cmd = [
        str(repo / "tools" / "yanglint.sh"),
        "-D",
        "-p",
        str(repo / "contract" / "yang"),
        "-t",
        "config",
        str(repo / "contract" / "yang" / "netsim-device.yang"),
        *(str(p.resolve()) for p in paths),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=repo, timeout=600)
