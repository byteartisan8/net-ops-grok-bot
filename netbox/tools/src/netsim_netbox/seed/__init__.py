"""``netbox-seed``: make NetBox match ``contract/topology.yaml`` (contract section 10).

    netbox-seed                    # create/update what differs; a second run reports 0 changes
    netbox-seed --dry-run          # print the plan, write nothing
    netbox-seed --prune            # also delete netsim-lab tagged objects that are not in the topology
    netbox-seed --no-webhooks      # do not manage the NetBox -> lab-api webhook / event rule
    netbox-seed --check            # dry run; exit 1 if NetBox differs from the topology

Uses ``NETBOX_TOKEN_RW``. Library use::

    from netsim_netbox.seed import seed
    report = seed(client, load_topology(), dry_run=True)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netsim_common.topology import Topology, load_topology
from netsim_netbox.client import NetBoxClient, NetBoxError
from netsim_netbox.seed.desired import PHASES, WebhookConfig, default_webhook_url, desired_state
from netsim_netbox.seed.engine import Seeder, SeedError
from netsim_netbox.seed.model import Change, Ref, SeedReport, Spec
from netsim_netbox.settings import SettingsError, get_setting, load_settings

__all__ = [
    "PHASES",
    "Change",
    "Ref",
    "SeedError",
    "SeedReport",
    "Seeder",
    "Spec",
    "WebhookConfig",
    "desired_state",
    "main",
    "seed",
]

WEBHOOK_SECRET = "NETBOX_WEBHOOK_SECRET"


def seed(
    client: NetBoxClient,
    topology: Topology,
    *,
    dry_run: bool = False,
    prune: bool = False,
    webhook: WebhookConfig | None = None,
    source: str = "contract/topology.yaml",
) -> SeedReport:
    """Reconcile NetBox with the topology (``webhook=None``: leave webhooks alone)."""
    seeder = Seeder(client, dry_run=dry_run, prune=prune, manage_webhooks=webhook is not None, source=source)
    return seeder.run(desired_state(topology, webhook=webhook))


def _display_path(path: Path) -> str:
    try:
        from netsim_common.paths import repo_root

        return str(path.resolve().relative_to(repo_root()))
    except (ValueError, FileNotFoundError):
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="netbox-seed", description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--topology", type=Path, help="topology file (default: contract/topology.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--check", action="store_true", help="dry run; exit 1 if anything would change")
    parser.add_argument("--prune", action="store_true", help="delete netsim-lab tagged objects not in the topology")
    parser.add_argument(
        "--webhooks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="manage the NetBox -> lab-api webhook and event rule (default: on)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="summary only")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or args.check

    try:
        settings = load_settings()
        topology = load_topology(args.topology)
        source = _display_path(args.topology) if args.topology else "contract/topology.yaml"
        webhook = None
        if args.webhooks:
            secret = get_setting(WEBHOOK_SECRET, env_file=settings.env_file)
            if not secret:
                raise SettingsError(f"{WEBHOOK_SECRET} is not set (run `make env`), or use --no-webhooks")
            webhook = WebhookConfig(url=get_setting("NETBOX_WEBHOOK_URL") or default_webhook_url(), secret=secret)
        with settings.client(write=True) as client:
            report = seed(client, topology, dry_run=dry_run, prune=args.prune, webhook=webhook, source=source)
    except (SeedError, NetBoxError, SettingsError, FileNotFoundError, ValueError) as exc:
        print(f"netbox-seed: error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        mode = " (dry run)" if dry_run else ""
        print(
            f"netbox-seed{mode}: NetBox {report.netbox_version} at {settings.url}, topology {source} "
            f"({len(topology.devices)} devices, {len(topology.services)} services)"
        )
        if not args.quiet:
            for change in report.changes:
                print(f"  {change}")
        print(f"netbox-seed: {report.summary()}")
        if report.journal_entries:
            print(f"netbox-seed: journal entry written on {report.journal_entries} device(s)")
    return 1 if args.check and report.changes else 0


if __name__ == "__main__":
    sys.exit(main())
