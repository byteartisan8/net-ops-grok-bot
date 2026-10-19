"""Canonical JSON and structured diffs of ``netsim-device`` configs (contract section 6).

Canonical form (what golden files and intent renderers emit, byte for byte):

* the config is parsed with :class:`~netsim_common.device_config.DeviceConfig`, so YANG defaults are
  filled in (``"enabled": true``) and optional leaves that are absent stay absent;
* lists are sorted by key: ``interface`` by ``name`` and ``xconnect`` by ``service-id`` (plain string
  order), ``pseudowire`` by ``role`` in YANG enum order (``primary`` before ``backup``);
* empty lists and empty containers are omitted (as in RFC 7951);
* ``json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False)`` plus a trailing newline.

:func:`diff_configs` compares two configs after the same normalization and returns one
:class:`ConfigDiff` per differing leaf (or per missing/unexpected list entry), e.g.::

    l2vpn/xconnect[SVC-0002]/pseudowire[primary]/peer-ipv4: expected 10.255.0.1, got 10.255.0.2
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from netsim_common.device_config import MODULE, DeviceConfig
from netsim_common.enums import PwRole

ConfigInput = DeviceConfig | Mapping[str, Any] | str | bytes

#: YANG list name -> key leaf.
LIST_KEYS: dict[str, str] = {"interface": "name", "xconnect": "service-id", "pseudowire": "role"}
#: Top-level containers in YANG order: (module-qualified JSON member, path label).
TOP_LEVEL: tuple[tuple[str, str], ...] = (
    (f"{MODULE}:system", "system"),
    (f"{MODULE}:interfaces", "interfaces"),
    (f"{MODULE}:l2vpn", "l2vpn"),
)

_ROLE_ORDER = {role.value: role.sort_index for role in PwRole}


def _sort_key(list_name: str, entry: Mapping[str, Any]) -> Any:
    key = entry[LIST_KEYS[list_name]]
    return _ROLE_ORDER.get(key, len(_ROLE_ORDER)) if list_name == "pseudowire" else key


def _prune(value: Any) -> Any:
    """Sort keyed lists and drop empty lists/containers, recursively."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = _prune(v)
            if k in LIST_KEYS and isinstance(v, list):
                v = sorted(v, key=lambda e, name=k: _sort_key(name, e))
            if v in ({}, []):
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def to_config(config: ConfigInput, *, partial: bool = True) -> DeviceConfig:
    """Coerce a model, a RESTCONF JSON object or JSON text into a :class:`DeviceConfig`.

    ``partial=True`` (default) skips the cross-container leafref checks so single-container payloads
    can be canonicalized and diffed; every other constraint is still validated.
    """
    if isinstance(config, DeviceConfig):
        return config
    return DeviceConfig.from_restconf(config, partial=partial)


def canonical_dict(config: ConfigInput) -> dict[str, Any]:
    """Canonical RESTCONF JSON object of a config (see module docstring)."""
    data = to_config(config).model_dump(mode="json", by_alias=True, exclude_none=True)
    return _prune(data)


def canonical_json(config: ConfigInput) -> str:
    """Canonical JSON text of a config; golden files are exactly this string."""
    return json.dumps(canonical_dict(config), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------------------- diffs
class DiffKind(StrEnum):
    CHANGED = "changed"  # leaf present on both sides with different values
    MISSING = "missing"  # in expected, absent from actual
    UNEXPECTED = "unexpected"  # absent from expected, present in actual


@dataclass(frozen=True, slots=True)
class PathPart:
    """One step of a diff path: a node name, plus the key for a list entry."""

    name: str
    key: str | None = None

    def __str__(self) -> str:
        return self.name if self.key is None else f"{self.name}[{self.key}]"


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    """One difference between an expected and an actual config.

    ``expected`` / ``actual`` hold JSON values (``None`` = absent). For a missing or unexpected list
    entry they hold the whole entry (a dict) and :attr:`path` ends at the entry.
    """

    parts: tuple[PathPart, ...]
    expected: Any
    actual: Any

    @property
    def path(self) -> str:
        """Slash-separated path without module prefix, e.g. ``l2vpn/xconnect[SVC-0002]/pseudowire[primary]``."""
        return "/".join(str(p) for p in self.parts)

    @property
    def kind(self) -> DiffKind:
        if self.actual is None:
            return DiffKind.MISSING
        if self.expected is None:
            return DiffKind.UNEXPECTED
        return DiffKind.CHANGED

    @property
    def leaf(self) -> str | None:
        """Leaf name when the diff is about a single leaf (``None`` for whole list entries)."""
        last = self.parts[-1]
        return last.name if last.key is None else None

    def key_of(self, list_name: str) -> str | None:
        """Key of the enclosing ``list_name`` entry, e.g. ``key_of("xconnect") -> "SVC-0002"``."""
        return next((p.key for p in self.parts if p.name == list_name and p.key is not None), None)

    @property
    def service_id(self) -> str | None:
        return self.key_of("xconnect")

    @property
    def pw_role(self) -> str | None:
        return self.key_of("pseudowire")

    @property
    def interface(self) -> str | None:
        return self.key_of("interface")

    @property
    def message(self) -> str:
        return f"{self.path}: expected {format_value(self.expected)}, got {format_value(self.actual)}"

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (for APIs and ``--json`` output)."""
        return {
            "path": self.path,
            "kind": self.kind.value,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "service_id": self.service_id,
            "pw_role": self.pw_role,
            "interface": self.interface,
        }


def format_value(value: Any) -> str:
    """Human-readable rendering used in diff messages."""
    if value is None:
        return "<absent>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value) if (not value or any(c.isspace() or c in ",{}=" for c in value)) else value
    if isinstance(value, list):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if k in LIST_KEYS and isinstance(v, list):
                v = [e.get(LIST_KEYS[k]) for e in v]
            parts.append(f"{k}={format_value(v)}")
        return "{" + " ".join(parts) + "}"
    return str(value)


def _without(entry: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k != key}


def _diff_container(
    parts: tuple[PathPart, ...], expected: Mapping[str, Any], actual: Mapping[str, Any], out: list[ConfigDiff]
) -> None:
    names = list(expected) + [k for k in actual if k not in expected]
    for name in names:
        e, a = expected.get(name), actual.get(name)
        if name in LIST_KEYS and isinstance(e or a, list):
            _diff_list(parts, name, e or [], a or [], out)
        elif isinstance(e, Mapping) or isinstance(a, Mapping):
            _diff_container((*parts, PathPart(name)), e or {}, a or {}, out)
        elif e != a:
            out.append(ConfigDiff((*parts, PathPart(name)), e, a))


def _diff_list(
    parts: tuple[PathPart, ...], name: str, expected: list[Any], actual: list[Any], out: list[ConfigDiff]
) -> None:
    key = LIST_KEYS[name]
    by_key_e = {str(entry[key]): entry for entry in expected}
    by_key_a = {str(entry[key]): entry for entry in actual}
    for k in list(by_key_e) + [k for k in by_key_a if k not in by_key_e]:
        e, a = by_key_e.get(k), by_key_a.get(k)
        entry_parts = (*parts, PathPart(name, k))
        if e is None or a is None:
            out.append(ConfigDiff(entry_parts, e and _without(e, key), a and _without(a, key)))
        else:
            _diff_container(entry_parts, _without(e, key), _without(a, key), out)


def diff_configs(expected: ConfigInput, actual: ConfigInput) -> list[ConfigDiff]:
    """Structured diff of two configs (``expected`` = intent/golden, ``actual`` = running).

    Both sides are normalized first (defaults filled, lists keyed), so ordering and omitted default
    leaves never show up as differences. Returns ``[]`` when the configs are equivalent.
    """
    e, a = canonical_dict(expected), canonical_dict(actual)
    out: list[ConfigDiff] = []
    for member, label in TOP_LEVEL:
        _diff_container((PathPart(label),), e.get(member, {}), a.get(member, {}), out)
    return out


def format_diffs(diffs: list[ConfigDiff]) -> str:
    """One line per diff, or ``"no differences"``."""
    return "\n".join(d.message for d in diffs) if diffs else "no differences"
