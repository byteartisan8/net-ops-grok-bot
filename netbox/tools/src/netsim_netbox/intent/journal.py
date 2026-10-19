"""Journal entries (remediation records) on NetBox objects. Needs ``NETBOX_TOKEN_RW``.

Objects can be named the way the bot thinks of them::

    write_journal(client, "device:spoke-2", "Restored SVC-0002 primary vc-id 1002", kind="success")
    write_journal(client, "l2vpn:SVC-0002-primary", "...")
    write_journal(client, "interface:spoke-2:GigabitEthernet0/0/1.102", "...")
    write_journal(client, "tenant:Customer-A", "...")
    write_journal(client, ("dcim.device", 12), "...")      # or "dcim.device:12"

Entries are Markdown, tagged ``netsim-lab``; kinds are NetBox's ``info``, ``success``, ``warning``, ``danger``.
"""

from __future__ import annotations

from typing import Any

from netsim_netbox.client import NetBoxClient
from netsim_netbox.objects import KINDS, LAB_TAG

JOURNAL_KINDS = ("info", "success", "warning", "danger")
ObjectRef = str | tuple[str, int]

#: Short names accepted in "kind:name" references -> (object type, lookup filter builder).
_LOOKUPS: dict[str, tuple[str, Any]] = {
    "device": ("dcim.device", lambda n: {"name": n}),
    "l2vpn": ("vpn.l2vpn", lambda n: {"name": n}),
    "tenant": ("tenancy.tenant", lambda n: {"name": n}),
    "site": ("dcim.site", lambda n: {"name": n}),
    "vlan": ("ipam.vlan", lambda n: {"name": n}),
}


class JournalError(LookupError):
    pass


def resolve_object(client: NetBoxClient, ref: ObjectRef) -> tuple[str, int]:
    """``(object type, id)`` of a reference (see module docstring)."""
    if isinstance(ref, tuple):
        object_type, ident = ref
        if object_type not in KINDS:
            raise JournalError(f"unknown object type {object_type!r}")
        return object_type, int(ident)
    prefix, sep, rest = ref.partition(":")
    if not sep or not rest:
        raise JournalError(f"bad object reference {ref!r} (expected e.g. device:spoke-1 or dcim.device:12)")
    if "." in prefix:
        if prefix not in KINDS or not rest.isdigit():
            raise JournalError(f"bad object reference {ref!r}")
        return prefix, int(rest)
    if prefix == "interface":
        host, _, name = rest.partition(":")
        object_type, query = "dcim.interface", {"device": host, "name": name}
    elif prefix in _LOOKUPS:
        object_type, build = _LOOKUPS[prefix]
        query = build(rest)
    else:
        raise JournalError(f"unknown object kind {prefix!r} (use {', '.join([*_LOOKUPS, 'interface'])})")
    rows = client.list(KINDS[object_type].endpoint, **query)
    if len(rows) != 1:
        raise JournalError(f"{ref!r} matches {len(rows)} objects")
    return object_type, rows[0]["id"]


def write_journal(client: NetBoxClient, obj: ObjectRef, message: str, kind: str = "info") -> dict[str, Any]:
    """Add a journal entry to ``obj``; returns the created entry (``id``, ``url``, ...)."""
    if kind not in JOURNAL_KINDS:
        raise JournalError(f"kind must be one of {', '.join(JOURNAL_KINDS)}")
    if not message.strip():
        raise JournalError("empty journal message")
    object_type, ident = resolve_object(client, obj)
    return client.create(
        KINDS["extras.journalentry"].endpoint,
        {
            "assigned_object_type": object_type,
            "assigned_object_id": ident,
            "kind": kind,
            "comments": message,
            "tags": [{"slug": LAB_TAG}],
        },
    )
