"""A plan: the list of changes a provisioning or sync step makes (printed; nothing is written with ``--dry-run``)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Change:
    action: str  # create | update | delete
    kind: str  # host, macro, service, sla, map, template, action, dashboard
    name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.action:<6} {self.kind:<9} {self.name}" + (f": {self.detail}" if self.detail else "")


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, action: str, kind: str, name: str, detail: str = "") -> None:
        self.changes.append(Change(action, kind, name, detail))

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def extend(self, other: Plan) -> None:
        self.changes += other.changes
        self.warnings += other.warnings

    def __len__(self) -> int:
        return len(self.changes)

    def summary(self) -> str:
        if not self.changes:
            return "0 changes"
        counts: dict[str, int] = {}
        for c in self.changes:
            counts[c.action] = counts.get(c.action, 0) + 1
        parts = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
        return f"{len(self.changes)} change(s): {parts}"

    def lines(self) -> list[str]:
        return [str(c) for c in self.changes] + [f"WARNING {w}" for w in self.warnings]
