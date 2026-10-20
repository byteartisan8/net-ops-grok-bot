"""simd events of every device, merged into one time-ordered list (config changes, PW transitions, traps)."""

from __future__ import annotations

from typing import Any


def merged_events(
    lab: Any, since: str | float | None = None, limit: int | None = None, hosts: list[str] | None = None
) -> dict[str, Any]:
    """``{"events": [...], "errors": {host: message}}``; each event keeps its ``host``, sorted by ``ts``."""
    results = lab.devices.map(lambda d: d.events(since=since), hosts)
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for host, result in results.items():
        if isinstance(result, Exception):
            errors[host] = str(result)
            continue
        for event in result.get("events", []):
            events.append({"host": host, **event})
    events.sort(key=lambda e: (e.get("ts", 0.0), e["host"], e.get("id", 0)))
    if limit is not None and limit >= 0:
        events = events[-limit:] if limit else []
    return {"events": events, "errors": errors}
