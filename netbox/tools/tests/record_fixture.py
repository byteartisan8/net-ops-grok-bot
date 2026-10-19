"""Record a seeded NetBox into the unit-test fixture (``data/netbox-seeded.json``).

    uv run netbox-seed && uv run python netbox/tools/tests/record_fixture.py

Every object of the endpoints the seeder and the intent renderer read, exactly as NetBox's REST API returns them,
minus volatile or secret fields (timestamps, URLs, the webhook secret). :class:`fakenetbox.FakeNetBox` replays it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from netsim_netbox.objects import KINDS
from netsim_netbox.settings import load_settings

FIXTURE = Path(__file__).parent / "data" / "netbox-seeded.json"
#: Replaces the webhook secret of the recorded NetBox (tests seed with the same value).
TEST_WEBHOOK_SECRET = "test-webhook-secret"
DROP = {"url", "display_url", "created", "last_updated", "last_used"}
ENDPOINTS = sorted({k.endpoint for k in KINDS.values() if k.name != "extras.journalentry"})


def _scrub(value: object) -> object:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in sorted(value.items()) if k not in DROP}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def main() -> int:
    with load_settings().client(write=True) as client:
        version = client.status()["netbox-version"]
        data = {ep: [_scrub(o) for o in client.list(ep)] for ep in ENDPOINTS}
    for hook in data["extras/webhooks"]:
        hook["secret"] = TEST_WEBHOOK_SECRET
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({"netbox_version": version, "endpoints": data}, indent=1) + "\n", encoding="utf-8")
    counts = ", ".join(f"{ep}={len(objs)}" for ep, objs in data.items())
    print(f"recorded NetBox {version} -> {FIXTURE}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
