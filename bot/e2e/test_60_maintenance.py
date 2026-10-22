"""Maintenance mode and muted pages: Zabbix's alerts are recorded, no incident opens, nobody is paged."""

from __future__ import annotations

import time


def _alert_for(bot, wait, customer: str, since: float, kind: str) -> dict:
    """Zabbix's first ``kind`` alert for ``customer`` since ``since``: the kind matters, because a degraded alert's
    Disaster update can land after a restore and must not be taken for the recovery."""
    return wait(
        lambda: next((a for a in bot.alerts_since(since) if a["customer"] == customer and a["kind"] == kind), None),
        60,
        f"Zabbix's {kind} alert for {customer}",
    )


def test_maintenance_mode_and_muted_pages_page_nobody(bot, sink, green, fault, wait) -> None:
    green()
    bot.control(paused=True, reason="bot-e2e: maintenance")
    try:
        t0 = fault("uplink", "spoke-3")  # SVC-0004: Customer-C (silver) down
        assert _alert_for(bot, wait, "Customer-C", t0, "problem")["outcome"] == "recorded: maintenance mode"
        t1 = fault("uplink", "spoke-3", action="restore")
        _alert_for(bot, wait, "Customer-C", t1, "recovery")  # recorded too
        bot.control(paused=False, pages_muted=True)
        t2 = fault("uplink", "spoke-3")
        assert _alert_for(bot, wait, "Customer-C", t2, "problem")["outcome"] == "recorded: pages muted"
        time.sleep(12)  # longer than a batch window
        assert sink.pages(since=t0) == []
        assert not [i for i in bot.incidents() if i["created"] >= t0]
    finally:
        bot.control(paused=False, pages_muted=False)
    green()
