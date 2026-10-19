"""Canonical order of Zabbix 8.0 export data, so generated YAML equals what Zabbix itself exports.

``configuration.importcompare`` compares lists in order, so a template is only "unchanged" when its source lists
objects in the exporter's order. The rules mirror ``CConfigurationExportBuilder`` (Zabbix 8.0.0rc1): most lists go
through ``CArrayHelper::sort()`` (PHP ``strnatcasecmp`` on given fields), macros through ``order_macros()`` (natural
order of the name without ``{$`` and ``}``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any


def _compare_right(a: str, i: int, b: str, j: int) -> tuple[int, int, int]:
    """Right-aligned digit runs: the longest wins, else the first difference."""
    bias = 0
    while True:
        a_digit = i < len(a) and a[i].isdigit()
        b_digit = j < len(b) and b[j].isdigit()
        if not a_digit and not b_digit:
            return bias, i, j
        if not a_digit:
            return -1, i, j
        if not b_digit:
            return 1, i, j
        if bias == 0:
            bias = (a[i] > b[j]) - (a[i] < b[j])
        i += 1
        j += 1


def _compare_left(a: str, i: int, b: str, j: int) -> tuple[int, int, int]:
    """Left-aligned (fractional) digit runs: the first difference wins."""
    while True:
        a_digit = i < len(a) and a[i].isdigit()
        b_digit = j < len(b) and b[j].isdigit()
        if not a_digit and not b_digit:
            return 0, i, j
        if not a_digit:
            return -1, i, j
        if not b_digit:
            return 1, i, j
        if a[i] != b[j]:
            return (1 if a[i] > b[j] else -1), i, j
        i += 1
        j += 1


def strnatcasecmp(a: str, b: str) -> int:
    """Port of PHP's ``strnatcmp_ex(..., is_case_insensitive=true)`` (ext/standard/strnatcmp.c)."""
    if not a or not b:
        return 0 if len(a) == len(b) else (1 if len(a) > len(b) else -1)
    i = j = 0
    leading = True
    while True:
        ca, cb = (a[i] if i < len(a) else "\0"), (b[j] if j < len(b) else "\0")
        while leading and ca == "0" and i + 1 < len(a) and a[i + 1].isdigit():
            i += 1
            ca = a[i]
        while leading and cb == "0" and j + 1 < len(b) and b[j + 1].isdigit():
            j += 1
            cb = b[j]
        leading = False
        while ca.isspace():
            i += 1
            ca = a[i] if i < len(a) else "\0"
        while cb.isspace():
            j += 1
            cb = b[j] if j < len(b) else "\0"
        if ca.isdigit() and cb.isdigit():
            fractional = ca == "0" or cb == "0"
            result, i, j = (_compare_left if fractional else _compare_right)(a, i, b, j)
            if result != 0:
                return result
            if i >= len(a) and j >= len(b):
                return 0
            if i >= len(a):
                return -1
            if j >= len(b):
                return 1
            ca, cb = a[i], b[j]
        ca, cb = ca.upper(), cb.upper()
        if ca < cb:
            return -1
        if ca > cb:
            return 1
        i += 1
        j += 1
        if i >= len(a) and j >= len(b):
            return 0
        if i >= len(a):
            return -1
        if j >= len(b):
            return 1


def _field_cmp(fields: Sequence[str]) -> Callable[[dict, dict], int]:
    def cmp(x: dict, y: dict) -> int:
        for f in fields:
            a, b = x.get(f), y.get(f)
            if a is None and b is None:
                continue
            if a is None:
                return -1
            if b is None:
                return 1
            c = strnatcasecmp(str(a), str(b))
            if c:
                return c
        return 0

    return cmp


def php_sort(rows: list[dict], *fields: str) -> list[dict]:
    """``CArrayHelper::sort($rows, [fields])`` (stable, like PHP 8's sort)."""
    return sorted(rows, key=functools.cmp_to_key(_field_cmp(fields)))


def sort_macros(rows: list[dict]) -> list[dict]:
    """``order_macros($macros, 'macro')``: natural order of ``{$NAME}`` by ``NAME``."""
    return sorted(rows, key=functools.cmp_to_key(lambda x, y: strnatcasecmp(x["macro"][2:-1], y["macro"][2:-1])))


#: export list name -> sort fields (``description`` of triggers is exported as ``name``).
_TRIGGER = ("name", "expression", "recovery_expression")
_RULES: dict[str, tuple[str, ...]] = {
    "template_groups": ("name",),
    "host_groups": ("name",),
    "templates": ("template",),
    "hosts": ("host",),
    "groups": ("name",),
    "items": ("key",),
    "item_prototypes": ("key",),
    "discovery_rules": ("key",),
    "triggers": _TRIGGER,
    "trigger_prototypes": _TRIGGER,
    "dependencies": _TRIGGER,
    "tags": ("tag", "value"),
    "lld_macro_paths": ("lld_macro",),
    "valuemaps": ("name",),
    "parameters": ("name",),
}


def canonicalize(data: Any, parent: str | None = None) -> Any:
    """Copy of export data with every list in the exporter's order."""
    if isinstance(data, dict):
        return {k: canonicalize(v, k) for k, v in data.items()}
    if isinstance(data, list):
        rows = [canonicalize(v, None) for v in data]
        if parent == "macros":
            return sort_macros(rows)
        if parent in _RULES and all(isinstance(r, dict) for r in rows):
            return php_sort(rows, *_RULES[parent])
        return rows
    return data
