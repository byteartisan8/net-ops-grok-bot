"""Desired-state building blocks of the seeder: object specs, references by natural key, change records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from netsim_netbox.objects import KINDS, LAB_TAG

Key = tuple[Any, ...]


@dataclass(frozen=True)
class Ref:
    """Reference to another object by kind + natural key; resolved to its NetBox ID when written."""

    kind: str
    key: Key

    def __str__(self) -> str:
        return "/".join(str(k) for k in self.key)


@dataclass
class Spec:
    """One object the seeder manages.

    ``fields`` are the managed fields in NetBox's write format (values may be :class:`Ref` or lists of them);
    fields not listed are never touched. ``custom_fields`` and ``tags`` are merged into what the object already has
    (other custom fields and tags are left alone). ``update_only`` specs only adjust an object created by an
    earlier phase (e.g. a device's ``primary_ip4`` once its IP exists). ``device`` attributes the object to a device
    for the seed journal entry.
    """

    kind: str
    key: Key
    fields: dict[str, Any]
    custom_fields: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = (LAB_TAG,)
    phase: str = ""
    device: str | None = None
    update_only: bool = False

    @property
    def label(self) -> str:
        return f"{KINDS[self.kind].label} {'/'.join(str(k) for k in self.key)}"


Action = Literal["create", "update", "delete"]


@dataclass
class Change:
    action: Action
    kind: str
    key: Key
    details: list[str] = field(default_factory=list)
    device: str | None = None
    object_id: int | None = None

    @property
    def label(self) -> str:
        return f"{KINDS[self.kind].label} {'/'.join(str(k) for k in self.key)}"

    def __str__(self) -> str:
        text = f"{self.action:<7} {self.label}"
        return f"{text}: {'; '.join(self.details)}" if self.details else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "kind": self.kind,
            "key": list(self.key),
            "details": self.details,
            "device": self.device,
            "id": self.object_id,
        }


@dataclass
class SeedReport:
    changes: list[Change] = field(default_factory=list)
    journal_entries: int = 0
    dry_run: bool = False
    netbox_version: str = ""

    def count(self, action: Action) -> int:
        return sum(1 for c in self.changes if c.action == action)

    @property
    def total(self) -> int:
        return len(self.changes)

    def summary(self) -> str:
        if not self.changes:
            return "0 changes (NetBox matches the topology)"
        verb = "would be " if self.dry_run else ""
        parts = [f"{self.count(a)} {verb}{a}d" for a in ("create", "update", "delete") if self.count(a)]
        return f"{self.total} change(s): " + ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "netbox_version": self.netbox_version,
            "total": self.total,
            "created": self.count("create"),
            "updated": self.count("update"),
            "deleted": self.count("delete"),
            "journal_entries": self.journal_entries,
            "changes": [c.to_dict() for c in self.changes],
        }
