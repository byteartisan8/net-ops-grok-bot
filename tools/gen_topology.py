#!/usr/bin/env python3
"""Generate a bigger topology for scale tests, extending contract/topology.yaml with the same schemes.

    uv run python tools/gen_topology.py --spokes 20 --services 120 --customers 8 > /tmp/topology-20x120.yaml

The contract's devices, customers and services are kept unchanged; additions follow the lab's rules:

* spoke k: hostname spoke-k, site BR-kk, loopback 10.255.1.k, same physical interfaces as spoke-1;
  mgmt IP 172.30.10.(20+k) while free (k <= 9), then the next free address from .60 upwards;
* customer i (0-based): Customer-<letters> (A..Z, AA..), SLA tier gold, gold, silver, silver, ... (as A-D);
* service n: SVC-nnnn, customer round-robin, spoke round-robin, VLAN 100*c + j (c = 1-based customer,
  j = that customer's j-th service, j <= 99), ACs <access>.<vlan>, VC IDs 1000+n (primary, hub-a) and
  2000+n (backup, hub-b); n <= 999 keeps the two VC ID ranges apart.

`--spokes 4 --services 6 --customers 4` reproduces contract/topology.yaml (as data). The result is
validated with netsim_common.topology.Topology before it is printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import string
import sys
from pathlib import Path
from typing import Any

import yaml

from netsim_common.enums import DeviceRole, PwRole, SlaTier
from netsim_common.topology import Topology, load_topology

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_SERVICES = 999
FIRST_EXTRA_OCTET = 60


class GenerationError(ValueError):
    pass


def customer_name(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = string.ascii_uppercase[rem] + letters
    return f"Customer-{letters}"


def sla_tier_for(index: int) -> SlaTier:
    return SlaTier.GOLD if (index // 2) % 2 == 0 else SlaTier.SILVER


def extend(base: Topology, spokes: int, services: int, customers: int) -> Topology:
    data: dict[str, Any] = base.model_dump(mode="json")
    base_spokes = base.spokes
    if spokes < len(base_spokes) or services < len(base.services) or customers < len(base.customers):
        raise GenerationError(
            f"the generator extends the contract topology: need --spokes >= {len(base_spokes)}, "
            f"--services >= {len(base.services)}, --customers >= {len(base.customers)}"
        )
    if services > MAX_SERVICES:
        raise GenerationError(f"--services must be <= {MAX_SERVICES} (VC IDs 1000+n and 2000+n must not overlap)")
    if spokes > 254:
        raise GenerationError("--spokes must be <= 254 (loopbacks 10.255.1.k)")

    net = base.network.ipv4_network
    used = {d.mgmt_ip for d in base.devices} | {h.mgmt_ip for h in base.infrastructure}
    used.add(base.network.gateway)
    prefix = str(net.network_address).rsplit(".", 1)[0]
    pool = [f"{prefix}.{o}" for o in range(FIRST_EXTRA_OCTET, 255) if f"{prefix}.{o}" not in used]
    template = base_spokes[0]
    for k in range(len(base_spokes) + 1, spokes + 1):
        preferred = f"{prefix}.{20 + k}"
        if k <= 9 and preferred not in used and ipaddress.IPv4Address(preferred) in net:
            mgmt = preferred
        elif pool:
            mgmt = pool.pop(0)
        else:
            raise GenerationError(f"no free address on {net} for spoke-{k}")
        used.add(mgmt)
        pool = [ip for ip in pool if ip != mgmt]
        data["devices"].append(
            {
                "hostname": f"spoke-{k}",
                "role": DeviceRole.SPOKE.value,
                "site": f"BR-{k:02d}",
                "mgmt_ip": mgmt,
                "loopback": f"10.255.1.{k}",
                "interfaces": [i.model_dump(mode="json") for i in template.interfaces],
            }
        )

    for i in range(len(base.customers), customers):
        data["customers"].append({"name": customer_name(i), "sla_tier": sla_tier_for(i).value})
    customer_names = [c["name"] for c in data["customers"]]
    if 100 * len(customer_names) + 1 > 4094:
        raise GenerationError("too many customers for the VLAN scheme 100*c + j (c <= 40)")

    per_customer = {name: sum(1 for s in base.services if s.customer == name) for name in customer_names}
    spoke_names = [d["hostname"] for d in data["devices"] if d["role"] == DeviceRole.SPOKE.value]
    access = {d["hostname"]: next(i["name"] for i in d["interfaces"] if i["type"] == "access") for d in data["devices"]}
    hub_for = {role: base.services[0].pw(role).hub for role in PwRole}
    for n in range(len(base.services) + 1, services + 1):
        customer = customer_names[(n - 1) % len(customer_names)]
        c = customer_names.index(customer) + 1
        per_customer[customer] += 1
        j = per_customer[customer]
        if j > 99:
            raise GenerationError(f"{customer} would get more than 99 services (VLAN 100*c + j); add --customers")
        vlan = 100 * c + j
        spoke = spoke_names[(n - 1) % len(spoke_names)]
        data["services"].append(
            {
                "id": f"SVC-{n:04d}",
                "customer": customer,
                "spoke": spoke,
                "vlan": vlan,
                "spoke_ac": f"{access[spoke]}.{vlan}",
                "hub_ac": f"{access[hub_for[PwRole.PRIMARY]]}.{vlan}",
                "pseudowires": {
                    "primary": {"hub": hub_for[PwRole.PRIMARY], "vc_id": 1000 + n},
                    "backup": {"hub": hub_for[PwRole.BACKUP], "vc_id": 2000 + n},
                },
            }
        )
    return Topology.model_validate(data)


def to_yaml(topology: Topology, argv: list[str]) -> str:
    header = (
        f"# GENERATED by tools/gen_topology.py {' '.join(argv)}\n"
        "# Scale-test topology extending contract/topology.yaml (same addressing and VC ID schemes).\n"
    )
    return header + yaml.safe_dump(topology.model_dump(mode="json"), sort_keys=False, width=120)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spokes", type=int, default=4)
    parser.add_argument("--services", type=int, default=6)
    parser.add_argument("--customers", type=int, default=4)
    parser.add_argument("--base", type=Path, default=REPO_ROOT / "contract" / "topology.yaml")
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    try:
        topology = extend(load_topology(args.base), args.spokes, args.services, args.customers)
    except (GenerationError, ValueError) as exc:
        print(f"gen_topology: error: {exc}", file=sys.stderr)
        return 2
    text = to_yaml(topology, argv)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {len(topology.spokes)} spokes, {len(topology.services)} services", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
