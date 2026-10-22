"""Customer alerts from Zabbix: the body the media type ``NetSim NOC (grok-bot)`` posts to ``/hooks/zabbix``.

The body (``zabbix/tools/src/netsim_zabbix/js/noc_webhook.js``) carries the role (the media's send-to), the kind
(``problem``, ``update``, ``recovery``, ``escalation``; from the message template, because Zabbix 8.0.0rc1 keeps
``{EVENT.UPDATE.STATUS}`` at 0), the service event, the effective severity (an update carries the new one), the
customer node's tags and ``{SERVICE.ROOTCAUSE}``. The root cause is text, one line per trigger problem::

    Host: "spoke-4" Problem name: "SVC-0005 primary on spoke-4: PW down (2), ..." Severity: "High" Age: 2s
    Problem tags: "check:oper, ..., host:spoke-4, peer:10.255.0.1, pw_role:primary, scope:l2vpn-pw, service:SVC-0005"

(on one line). :func:`parse_root_cause` turns it into problems with their tags; a PW problem's hosts are its own and
its peer's (the peer address mapped to a host by loopback), so a hub that is powered off still shows up in the hosts
of its spokes' problems.
"""

from __future__ import annotations

import re
from typing import Any

from netsim_bot.models import Alert, now, state_of
from netsim_bot.settings import ROLES

KINDS = ("problem", "update", "recovery", "escalation")
SEVERITY_NAMES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster", "OK")
ROOT_CAUSE_LINE = re.compile(
    r'^Host: "(?P<host>[^"]*)" Problem name: "(?P<name>.*?)" Severity: "(?P<severity>[^"]*)"'
    r'(?: Age: (?P<age>.*?))?(?: Problem tags: "(?P<tags>[^"]*)")?\s*$'
)


class AlertError(ValueError):
    """The body is not a customer alert grok-bot can use."""


def parse_tags(text: str | None) -> dict[str, str]:
    """``"check:oper, host:spoke-4"`` -> ``{"check": "oper", "host": "spoke-4"}``."""
    out: dict[str, str] = {}
    for part in (text or "").split(", "):
        key, sep, value = part.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def parse_root_cause(text: str | None, loopbacks: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The trigger problems of ``{SERVICE.ROOTCAUSE}``: host, name, severity, tags, and the hosts it involves."""
    loopbacks = loopbacks or {}
    out = []
    for line in (text or "").splitlines():
        m = ROOT_CAUSE_LINE.match(line.strip())
        if not m:
            continue
        tags = parse_tags(m["tags"])
        hosts = [h for h in (m["host"], tags.get("host"), loopbacks.get(tags.get("peer", ""))) if h]
        out.append(
            {
                "host": m["host"],
                "name": m["name"],
                "severity": m["severity"],
                "service": tags.get("service"),
                "pw_role": tags.get("pw_role"),
                "check": tags.get("check"),
                "scope": tags.get("scope"),
                "hosts": sorted(set(hosts)),
            }
        )
    return out


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_alert(body: Any, alert_id: str, loopbacks: dict[str, str] | None = None) -> Alert:
    """An :class:`Alert` from the webhook body (raises :class:`AlertError` for what is not a usable alert)."""
    if not isinstance(body, dict):
        raise AlertError("the body must be a JSON object")
    role = _text(body.get("role"))
    if role not in ROLES:
        raise AlertError(f"role must be one of {', '.join(ROLES)} (the media's send-to), not {role!r}")
    kind = (_text(body.get("kind")) or "problem").lower()
    if kind not in KINDS:
        raise AlertError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    event_id = _text(body.get("event_id"))
    if not event_id:
        raise AlertError("event_id is missing")
    service = body.get("service") if isinstance(body.get("service"), dict) else {}
    tags = service.get("tags") if isinstance(service.get("tags"), dict) else {}
    tags = {str(k): str(v) for k, v in tags.items()}
    customer = _text(service.get("name")) or tags.get("customer")
    if not customer:
        raise AlertError("service.name (the customer) is missing")
    severity = _text(body.get("severity")) or ("OK" if kind == "recovery" else "Not classified")
    if kind == "recovery":
        severity = "OK"
    if severity not in SEVERITY_NAMES:
        raise AlertError(f"severity {severity!r} is not a Zabbix severity")
    problems = parse_root_cause(_text(body.get("root_cause")), loopbacks)
    try:
        event_time = float(body["event_time"]) if body.get("event_time") is not None else None
    except (TypeError, ValueError):
        event_time = None
    return Alert(
        id=alert_id,
        role=role,
        kind=kind,
        event_id=event_id,
        customer=customer,
        severity=severity,
        state=state_of(severity),
        tier=tags.get("sla_tier"),
        service_id=_text(service.get("id")),
        recovery_event_id=_text(body.get("recovery_event_id")),
        event_time=event_time,
        tags=tags,
        problems=problems,
        hosts=sorted({h for p in problems for h in p["hosts"]}),
        services=sorted({p["service"] for p in problems if p.get("service")}),
        subject=_text(body.get("subject")),
        received=now(),
    )


def is_customer_alert(alert: Alert) -> bool:
    """The action selects ``bsm_level=customer``; a body without that tag (or with another level) is not one."""
    return alert.tags.get("bsm_level", "customer") == "customer"
