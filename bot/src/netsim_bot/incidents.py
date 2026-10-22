"""From Zabbix customer alerts to incidents and pages (pure logic: the engine feeds it alerts and persists the result).

* **Batching** (:class:`Batcher`): alerts are held per role. A batch closes ``batch_s`` after its latest alert and
  at most ``max_batch_s`` after its first, so the alerts of one fault (a hub power-off alerts six customers within a
  few seconds) become one batch.
* **A NOC Engineer batch** (:func:`apply_batch`), customer by customer (several alerts of one customer in a batch
  are folded in order, the last state wins):

  - a customer already in an open incident: its state changes there. Degraded -> down is *worse* (an update page),
    down -> degraded is *better* (recorded), bad -> OK is *recovered*, OK -> bad again is *back* (paged as worse);
  - a new bad customer whose problems share a host or a service with an open incident *joins* it, without a page
    (the same fault reaching another customer). A PW problem's hosts are both its ends;
  - every other new bad customer of the batch goes into **one new incident** with **one page**;
  - a customer that is OK again before its batch closed, or a recovery without an incident, is only recorded.

  When every customer of an incident is OK, the incident is *recovered* (one "recovered" page); the NOC Engineer
  verifies and closes it. A gold customer that is down (new, joined, worse or back) also pages the NOC Manager, once
  per customer and incident.
* **A NOC Manager batch** (the Zabbix escalation step: the customer is still not OK after
  ``{$NETSIM.ESCALATE.AFTER}``) pages the NOC Manager with the incidents of those customers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from netsim_bot.models import TIER_RANK, Alert, Incident, now, worst
from netsim_bot.settings import NOCENG, NOCMGR

STATE_RANK = {"ok": 0, "degraded": 1, "down": 2}


# ---------------------------------------------------------------------------------------------- batching
@dataclass
class Batch:
    id: str
    key: str
    first: float
    last: float
    items: list[Any] = field(default_factory=list)


class Batcher:
    """Collects items per key; a batch is due ``batch_s`` after its latest item, at most ``max_batch_s`` after its
    first (``batch_s = 0``: due at once)."""

    def __init__(self, batch_s: float, max_batch_s: float, new_id: Callable[[], str]) -> None:
        self.batch_s = batch_s
        self.max_batch_s = max(max_batch_s, batch_s)
        self.new_id = new_id
        self.open: dict[str, Batch] = {}

    def configure(self, batch_s: float, max_batch_s: float) -> None:
        self.batch_s, self.max_batch_s = batch_s, max(max_batch_s, batch_s)

    def add(self, key: str, item: Any, t: float | None = None) -> Batch:
        t = now() if t is None else t
        batch = self.open.get(key)
        if batch is None:
            batch = self.open[key] = Batch(self.new_id(), key, t, t)
        batch.items.append(item)
        batch.last = t
        return batch

    def deadline(self, batch: Batch) -> float:
        return min(batch.first + self.max_batch_s, batch.last + self.batch_s)

    def due(self, t: float | None = None) -> list[Batch]:
        t = now() if t is None else t
        ready = [b for b in self.open.values() if t >= self.deadline(b)]
        for b in ready:
            del self.open[b.key]
        return ready

    def next_deadline(self) -> float | None:
        return min((self.deadline(b) for b in self.open.values()), default=None)


# ---------------------------------------------------------------------------------------------- what a batch means
@dataclass
class PageRequest:
    role: str
    event: str
    incident_id: str
    customers: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    created: list[Incident] = field(default_factory=list)
    changed: dict[str, Incident] = field(default_factory=dict)
    pages: list[PageRequest] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)

    def touch(self, inc: Incident) -> None:
        self.changed[inc.id] = inc


def overlaps(alert: Alert, inc: Incident) -> bool:
    """The same fault: the alert's problems share a host (PW ends included) or a service with the incident."""
    return bool(set(alert.hosts) & set(inc.hosts) or set(alert.services) & set(inc.services))


def holding(customer: str, incidents: Iterable[Incident]) -> Incident | None:
    """The newest active incident that lists the customer."""
    found = [i for i in incidents if i.active and customer in i.customers]
    return max(found, key=lambda i: i.created) if found else None


def customer_record(alert: Alert, t: float) -> dict[str, Any]:
    return {
        "name": alert.customer,
        "tier": alert.tier,
        "state": alert.state,
        "severity": alert.severity,
        "event_id": alert.event_id,
        "service_id": alert.service_id,
        "services": list(alert.services),
        "hosts": list(alert.hosts),
        "problems": list(alert.problems),
        "first_alert": t,
        "last_alert": t,
        "worst": alert.state,
        "recovered": None,
        "gold_down_paged": False,
        "history": [{"ts": t, "kind": alert.kind, "state": alert.state, "alert": alert.id}],
    }


def _update(rec: dict[str, Any], alert: Alert, t: float) -> None:
    rec["state"], rec["severity"], rec["last_alert"] = alert.state, alert.severity, t
    rec["history"].append({"ts": t, "kind": alert.kind, "state": alert.state, "alert": alert.id})
    if alert.state != "ok":
        rec["event_id"] = alert.event_id
        rec["recovered"] = None
        if alert.problems:  # the latest root cause (an update carries the current one)
            rec["problems"] = list(alert.problems)
            rec["services"] = sorted(set(rec["services"]) | set(alert.services))
            rec["hosts"] = sorted(set(rec["hosts"]) | set(alert.hosts))
        if STATE_RANK[alert.state] > STATE_RANK.get(rec.get("worst") or "ok", 0):
            rec["worst"] = alert.state
    else:
        rec["recovered"] = t
    if alert.tier and not rec.get("tier"):
        rec["tier"] = alert.tier


def _merge_problems(inc: Incident) -> None:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for c in inc.customers.values():
        for p in c.get("problems") or []:
            seen[(p.get("host") or "", p.get("name") or "")] = {**p, "customer": c["name"]}
    inc.problems = sorted(seen.values(), key=lambda p: (p.get("host") or "", p.get("name") or ""))


def auto_title(inc: Incident) -> str:
    """What Zabbix shows, before anyone diagnosed it: the host every customer has in common, and the customers."""
    bad = [c for c in inc.customers.values() if c.get("state") != "ok"] or list(inc.customers.values())
    counts = {"down": 0, "degraded": 0}
    for c in bad:
        if c.get("state") in counts:
            counts[c["state"]] += 1
    common: set[str] | None = None
    for c in bad:
        hosts = set(c.get("hosts") or [])
        common = hosts if common is None else common & hosts
    hosts = sorted(common or [])
    hubs = [h for h in hosts if h.startswith("hub")]
    where = (hubs or hosts)[:1]
    what = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "recovered"
    who = f"{len(bad)} customer{'s' if len(bad) != 1 else ''}" if len(bad) > 2 else " + ".join(c["name"] for c in bad)
    return f"{where[0] + ': ' if where else ''}{who} ({what})"


def refresh_status(inc: Incident, t: float) -> None:
    inc.refresh()
    _merge_problems(inc)
    if inc.status == "closed":
        return
    if not inc.bad_customers():
        if inc.status != "recovered":
            inc.status, inc.recovered = "recovered", t
    elif inc.status == "recovered":
        inc.status, inc.recovered = "working", None


def _gold_down(inc: Incident, names: Iterable[str], out: Outcome) -> None:
    newly = []
    for name in names:
        rec = inc.customers.get(name)
        if rec and rec.get("tier") == "gold" and rec.get("state") == "down" and not rec.get("gold_down_paged"):
            rec["gold_down_paged"] = True
            newly.append(name)
    if newly:
        out.pages.append(PageRequest(NOCMGR, "incident.gold-down", inc.id, newly))


def apply_batch(
    alerts: list[Alert],
    incidents: dict[str, Incident],
    new_incident: Callable[[], Incident],
    t: float | None = None,
) -> Outcome:
    """Apply one closed batch of alerts to the open ``incidents`` (mutated in place; new ones are added)."""
    t = now() if t is None else t
    out = Outcome(alerts=list(alerts))
    escalations = [a for a in alerts if a.kind == "escalation"]
    engineer = [a for a in alerts if a.kind != "escalation"]
    by_customer: dict[str, list[Alert]] = {}
    for a in sorted(engineer, key=lambda a: (a.received, a.event_time or 0)):
        by_customer.setdefault(a.customer, []).append(a)
    worse: dict[str, list[str]] = {}
    fresh: list[tuple[str, list[Alert]]] = []
    was_recovered = {i.id for i in incidents.values() if i.status == "recovered"}
    for customer, seq in by_customer.items():
        inc = holding(customer, incidents.values())
        if inc is not None:
            rec = inc.customers[customer]
            before = rec.get("state") or "ok"
            for a in seq:
                _update(rec, a, t)
                a.incident_id = inc.id
            after = rec["state"]
            if before == "degraded" and after == "down":
                outcome = "worse"
            elif before == "ok" and after != "ok":
                outcome = "back"
            elif before == "down" and after == "degraded":
                outcome = "better"
            elif before != "ok" and after == "ok":
                outcome = "recovered"
            else:
                outcome = "joined"
            for a in seq:
                a.outcome = outcome
            if outcome in ("worse", "back"):
                worse.setdefault(inc.id, []).append(customer)
            inc.alerts += [a.id for a in seq if a.id not in inc.alerts]
            out.touch(inc)
            continue
        final = seq[-1]
        if final.state == "ok":
            why = (
                "recorded: OK again before its batch closed"
                if len(seq) > 1
                else "recorded: a recovery without an open incident"
            )
            for a in seq:
                a.outcome = why
            continue
        fresh.append((customer, seq))
    # new bad customers: join an open incident of the same fault, else one new incident for the rest
    leftovers: list[tuple[str, list[Alert]]] = []
    for customer, seq in fresh:
        final = seq[-1]
        candidates = sorted(
            (i for i in incidents.values() if i.active and overlaps(final, i)), key=lambda i: i.created, reverse=True
        )
        if not candidates:
            leftovers.append((customer, seq))
            continue
        inc = candidates[0]
        inc.customers[customer] = customer_record(seq[0], t)
        for a in seq[1:]:
            _update(inc.customers[customer], a, t)
        for a in seq:
            a.outcome, a.incident_id = "joined", inc.id
        inc.alerts += [a.id for a in seq]
        _gold_down(inc, [customer], out)
        out.touch(inc)
    if leftovers:
        inc = new_incident()
        inc.created = inc.updated = t
        for customer, seq in leftovers:
            inc.customers[customer] = customer_record(seq[0], t)
            for a in seq[1:]:
                _update(inc.customers[customer], a, t)
            for a in seq:
                a.outcome, a.incident_id = "opened", inc.id
            inc.alerts += [a.id for a in seq]
        refresh_status(inc, t)
        inc.title = auto_title(inc)
        incidents[inc.id] = inc
        out.created.append(inc)
        out.pages.append(PageRequest(NOCENG, "incident.opened", inc.id, customer_order(inc)))
        _gold_down(inc, list(inc.customers), out)
    for inc_id, names in worse.items():
        inc = incidents[inc_id]
        out.pages.append(PageRequest(NOCENG, "incident.worse", inc_id, names))
        _gold_down(inc, names, out)
    for inc in out.changed.values():
        inc.updated = t
        refresh_status(inc, t)
        if not inc.root_cause:  # the Network Engineer's root cause names the incident from then on
            inc.title = auto_title(inc)
        if inc.status == "recovered" and inc.id not in was_recovered:
            out.pages.append(PageRequest(NOCENG, "incident.recovered", inc.id, sorted(inc.customers)))
    # the Zabbix escalation step: the NOC Manager hears about the incidents of those customers
    esc_by_incident: dict[str, list[str]] = {}
    for a in escalations:
        inc = holding(a.customer, incidents.values())
        if inc is None:
            inc = new_incident()
            inc.created = inc.updated = t
            inc.customers[a.customer] = customer_record(a, t)
            refresh_status(inc, t)
            inc.title = auto_title(inc)
            incidents[inc.id] = inc
            out.created.append(inc)
            out.pages.append(PageRequest(NOCENG, "incident.opened", inc.id, [a.customer]))
        a.outcome, a.incident_id = "escalated", inc.id
        inc.alerts.append(a.id)
        esc_by_incident.setdefault(inc.id, [])
        if a.customer not in esc_by_incident[inc.id]:
            esc_by_incident[inc.id].append(a.customer)
        out.touch(inc)
    for inc_id, names in esc_by_incident.items():
        out.pages.append(PageRequest(NOCMGR, "incident.zabbix-escalation", inc_id, names))
    return out


def _customer_order(inc: Incident) -> Callable[[str], tuple[int, int, str]]:
    def key(name: str) -> tuple[int, int, str]:
        c = inc.customers[name]
        return (TIER_RANK.get(c.get("tier") or "", 9), -STATE_RANK.get(c.get("state") or "ok", 0), name)

    return key


def customer_order(inc: Incident) -> list[str]:
    """Customers by SLA tier (gold first), then down before degraded."""
    return sorted(inc.customers, key=_customer_order(inc))


def incident_severity(inc: Incident) -> str:
    return worst([c.get("severity") or "OK" for c in inc.customers.values()])
