"""Config paths and patches, written exactly like the paths of :func:`netsim_common.diff_configs`.

A **path** is ``/``-separated segments, each ``name`` or ``name[key]`` for a list entry (a key may itself contain
``/``, as interface names do)::

    system/hostname
    interfaces/interface[GigabitEthernet0/0/2]/mtu
    l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id
    l2vpn/xconnect[SVC-0002]                          (a whole list entry)

So a drift entry such as ``l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id: expected 2001, got 2010`` turns
into the fix ``{"op": "set", "path": "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id", "value": 2001}``.

A **patch** is a list of operations applied in order to a RESTCONF JSON config:

* ``{"op": "set", "path": P, "value": V}``: create or replace the leaf, list entry or container at ``P``
  (missing containers and list entries on the way are created; a list entry's key leaf is filled in);
* ``{"op": "delete", "path": P}``: remove the leaf, list entry or container at ``P`` (error if absent).

Standard RFC 6902 JSON Patch is accepted as well: an operation whose ``path`` starts with ``/`` is a JSON Pointer
into the RESTCONF JSON document (module-qualified top-level members, list entries by position) with the ops
``add``, ``remove``, ``replace`` and ``test``.

The result is only a candidate: :func:`validate` checks it against the YANG rules mirrored by
:class:`~netsim_common.device_config.DeviceConfig` (types, ranges, leafrefs, ``must`` rules, unique PWs).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from pydantic import ValidationError

from netsim_common.canonical import LIST_KEYS, TOP_LEVEL
from netsim_common.device_config import MODULE, DeviceConfig

TOP_MEMBERS = {label: member for member, label in TOP_LEVEL}


class PathError(ValueError):
    """A path or patch operation that cannot be parsed or applied."""


@dataclass(frozen=True, slots=True)
class Segment:
    name: str
    key: str | None = None

    def __str__(self) -> str:
        return self.name if self.key is None else f"{self.name}[{self.key}]"


def parse_path(path: str) -> list[Segment]:
    """``l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id`` -> segments (``/`` inside ``[...]`` is part of the key)."""
    text = path.strip().strip("/")
    if not text:
        raise PathError("empty path")
    segments: list[Segment] = []
    i = 0
    while i < len(text):
        j = i
        while j < len(text) and text[j] not in "/[":
            j += 1
        name = text[i:j]
        if not name:
            raise PathError(f"{path!r}: empty segment at position {i}")
        key = None
        if j < len(text) and text[j] == "[":
            end = text.find("]", j)
            if end < 0:
                raise PathError(f"{path!r}: unterminated '[' after {name!r}")
            key = text[j + 1 : end]
            if not key:
                raise PathError(f"{path!r}: empty key for {name!r}")
            j = end + 1
        if key is not None and name not in LIST_KEYS:
            raise PathError(f"{path!r}: {name!r} is not a list (lists: {', '.join(LIST_KEYS)})")
        segments.append(Segment(name, key))
        if j < len(text):
            if text[j] != "/":
                raise PathError(f"{path!r}: expected '/' after {segments[-1]}")
            j += 1
        i = j
    if segments[0].name not in TOP_MEMBERS or segments[0].key is not None:
        raise PathError(f"{path!r}: must start with one of {', '.join(TOP_MEMBERS)}")
    for seg in segments[1:]:
        if seg.name in LIST_KEYS and seg.key is None and seg is not segments[-1]:
            raise PathError(f"{path!r}: list {seg.name!r} needs a key, e.g. {seg.name}[...]")
    return segments


def format_path(segments: Sequence[Segment]) -> str:
    return "/".join(str(s) for s in segments)


def restconf_path(path: str) -> str:
    """RESTCONF resource path (RFC 8040 section 3.5.3) of a config path, keys percent-encoded::

    interfaces/interface[GigabitEthernet0/0/1.101]/mtu
    -> netsim-device:interfaces/interface=GigabitEthernet0%2F0%2F1.101/mtu
    """
    segments = parse_path(path)
    parts = [f"{MODULE}:{segments[0].name}"]
    for seg in segments[1:]:
        parts.append(seg.name if seg.key is None else f"{seg.name}={quote(seg.key, safe='')}")
    return "/".join(parts)


def restconf_body(path: str, value: Any) -> dict[str, Any]:
    """RESTCONF JSON body that PUTs ``value`` at ``path`` (the target node's module-qualified name)."""
    segments = parse_path(path)
    last = segments[-1]
    name = f"{MODULE}:{last.name}"
    if last.key is not None:
        entry = dict(value) if isinstance(value, Mapping) else {}
        entry[LIST_KEYS[last.name]] = last.key
        return {name: [entry]}
    return {name: value}


# ---------------------------------------------------------------------------------------------- navigation
def _list_key(name: str) -> str:
    return LIST_KEYS[name]


def _find(entries: list[dict[str, Any]], name: str, key: str) -> int | None:
    leaf = _list_key(name)
    return next((i for i, e in enumerate(entries) if str(e.get(leaf)) == key), None)


def get_path(config: Mapping[str, Any], path: str) -> Any:
    """Value at ``path`` in a RESTCONF JSON config (``KeyError`` if absent). A bare list name returns the list."""
    segments = parse_path(path)
    node: Any = config.get(TOP_MEMBERS[segments[0].name])
    if node is None:
        raise KeyError(path)
    for seg in segments[1:]:
        if not isinstance(node, Mapping) or seg.name not in node:
            raise KeyError(path)
        node = node[seg.name]
        if seg.key is not None:
            index = _find(node, seg.name, seg.key) if isinstance(node, list) else None
            if index is None:
                raise KeyError(path)
            node = node[index]
    return node


def _container_for(doc: dict[str, Any], segments: list[Segment], create: bool, path: str) -> dict[str, Any]:
    """The dict that holds the last segment (creating containers/entries on the way when ``create``)."""
    member = TOP_MEMBERS[segments[0].name]
    if len(segments) == 1:
        return doc
    if member not in doc:
        if not create:
            raise PathError(f"{path}: {segments[0]} does not exist")
        doc[member] = {}
    node = doc[member]
    for seg in segments[1:-1]:
        if seg.name not in node:
            if not create:
                raise PathError(f"{path}: {seg} does not exist")
            node[seg.name] = [] if seg.key is not None else {}
        child = node[seg.name]
        if seg.key is not None:
            index = _find(child, seg.name, seg.key)
            if index is None:
                if not create:
                    raise PathError(f"{path}: {seg} does not exist")
                child.append({_list_key(seg.name): seg.key})
                index = len(child) - 1
            node = child[index]
        else:
            if not isinstance(child, dict):
                raise PathError(f"{path}: {seg} is not a container")
            node = child
    return node


def _set(doc: dict[str, Any], path: str, value: Any) -> None:
    segments = parse_path(path)
    holder = _container_for(doc, segments, True, path)
    last = segments[-1]
    if len(segments) == 1:
        if not isinstance(value, Mapping):
            raise PathError(f"{path}: a top-level container needs an object value")
        doc[TOP_MEMBERS[last.name]] = copy.deepcopy(dict(value))
        return
    if last.key is not None:
        if not isinstance(value, Mapping):
            raise PathError(f"{path}: a list entry needs an object value")
        entry = copy.deepcopy(dict(value))
        key_leaf = _list_key(last.name)
        if key_leaf in entry and str(entry[key_leaf]) != last.key:
            raise PathError(f"{path}: value has {key_leaf}={entry[key_leaf]!r}, path says {last.key!r}")
        entry[key_leaf] = last.key
        entries = holder.setdefault(last.name, [])
        index = _find(entries, last.name, last.key)
        if index is None:
            entries.append(entry)
        else:
            entries[index] = entry
        return
    holder[last.name] = copy.deepcopy(value)


def _delete(doc: dict[str, Any], path: str) -> None:
    segments = parse_path(path)
    holder = _container_for(doc, segments, False, path)
    last = segments[-1]
    if len(segments) == 1:
        if doc.pop(TOP_MEMBERS[last.name], None) is None:
            raise PathError(f"{path}: does not exist")
        return
    if last.key is not None:
        entries = holder.get(last.name) or []
        index = _find(entries, last.name, last.key)
        if index is None:
            raise PathError(f"{path}: does not exist")
        del entries[index]
        return
    if last.name not in holder:
        raise PathError(f"{path}: does not exist")
    del holder[last.name]


# ---------------------------------------------------------------------------------------------- RFC 6902
def _pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PathError(f"JSON Pointer {pointer!r} must start with '/'")
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]


def _walk_pointer(doc: Any, tokens: list[str], pointer: str) -> Any:
    node = doc
    for token in tokens:
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                raise PathError(f"{pointer}: no list index {token!r}") from None
        elif isinstance(node, dict):
            if token not in node:
                raise PathError(f"{pointer}: no member {token!r}")
            node = node[token]
        else:
            raise PathError(f"{pointer}: {token!r} below a leaf")
    return node


def _json_patch_op(doc: dict[str, Any], op: Mapping[str, Any]) -> None:
    name, pointer = op.get("op"), op.get("path", "")
    tokens = _pointer(pointer)
    if not tokens:
        raise PathError("replacing the whole document is not supported; use a top-level member")
    parent = _walk_pointer(doc, tokens[:-1], pointer)
    last = tokens[-1]
    if name == "test":
        if _walk_pointer(doc, tokens, pointer) != op.get("value"):
            raise PathError(f"test failed at {pointer}")
        return
    if name in ("add", "replace"):
        value = copy.deepcopy(op.get("value"))
        if isinstance(parent, list):
            if last == "-" and name == "add":
                parent.append(value)
                return
            try:
                index = int(last)
            except ValueError:
                raise PathError(f"{pointer}: bad list index {last!r}") from None
            if name == "add":
                if not 0 <= index <= len(parent):
                    raise PathError(f"{pointer}: list index out of range")
                parent.insert(index, value)
            else:
                if not 0 <= index < len(parent):
                    raise PathError(f"{pointer}: list index out of range")
                parent[index] = value
            return
        if not isinstance(parent, dict):
            raise PathError(f"{pointer}: parent is a leaf")
        if name == "replace" and last not in parent:
            raise PathError(f"{pointer}: nothing to replace")
        parent[last] = value
        return
    if name == "remove":
        if isinstance(parent, list):
            try:
                del parent[int(last)]
            except (ValueError, IndexError):
                raise PathError(f"{pointer}: no list index {last!r}") from None
            return
        if not isinstance(parent, dict) or last not in parent:
            raise PathError(f"{pointer}: nothing to remove")
        del parent[last]
        return
    raise PathError(f"unsupported JSON Patch op {name!r} (add, remove, replace, test)")


# ---------------------------------------------------------------------------------------------- patches
OpName = Literal["set", "delete", "add", "remove", "replace", "test"]


def normalize_patch(patch: Any) -> list[dict[str, Any]]:
    """Accept a list of operations (or one operation) and check their shape."""
    if isinstance(patch, Mapping):
        patch = [patch]
    if not isinstance(patch, list) or not patch:
        raise PathError("a patch is a non-empty list of operations")
    out = []
    for i, op in enumerate(patch):
        if not isinstance(op, Mapping) or "op" not in op or "path" not in op:
            raise PathError(f"operation {i}: needs 'op' and 'path'")
        name, path = op["op"], op["path"]
        if not isinstance(path, str):
            raise PathError(f"operation {i}: 'path' must be a string")
        if path.startswith("/"):
            if name not in ("add", "remove", "replace", "test"):
                raise PathError(f"operation {i}: JSON Patch op must be add, remove, replace or test, not {name!r}")
        else:
            if name not in ("set", "delete"):
                raise PathError(f"operation {i}: op must be 'set' or 'delete' (or a JSON Patch with '/' paths)")
            parse_path(path)
        if name in ("set", "add", "replace", "test") and "value" not in op:
            raise PathError(f"operation {i}: {name!r} needs a 'value'")
        out.append(dict(op))
    return out


def apply_patch(config: Mapping[str, Any], patch: Any) -> dict[str, Any]:
    """New RESTCONF JSON config = ``config`` with ``patch`` applied (``config`` is not modified)."""
    doc = copy.deepcopy(dict(config))
    for i, op in enumerate(normalize_patch(patch)):
        try:
            if op["path"].startswith("/"):
                _json_patch_op(doc, op)
            elif op["op"] == "set":
                _set(doc, op["path"], op["value"])
            else:
                _delete(doc, op["path"])
        except PathError as exc:
            raise PathError(f"operation {i} ({op['op']} {op['path']}): {exc}") from None
    return doc


@dataclass(frozen=True)
class Validation:
    """``errors``: the device would reject the config; ``warnings``: it accepts it, but something will not work."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    config: DeviceConfig | None

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def validate(candidate: Mapping[str, Any]) -> Validation:
    """Validate a candidate config the way the device's YANG would (types, ranges, leafrefs, ``must``).

    A ``(peer-ipv4, vc-id)`` pair used by two xconnects passes YANG (it can only check one xconnect) but brings
    both PWs down with reason ``duplicate-pw`` (contract section 6): reported as a warning.
    """
    try:
        config = DeviceConfig.from_restconf(dict(candidate))
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            where = "/".join(str(p) for p in err.get("loc", ()))
            errors.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
        return Validation(False, errors, [], None)
    except (ValueError, TypeError) as exc:
        return Validation(False, [str(exc)], [], None)
    warnings = [
        f"duplicate PW (peer {peer}, vc-id {vc_id}) on {', '.join(f'{svc}:{role.value}' for svc, role in users)}"
        " -> both down (duplicate-pw)"
        for (peer, vc_id), users in config.duplicate_pseudowires().items()
    ]
    return Validation(True, [], warnings, config)


def dumps(value: Any) -> str:
    """Compact, key-sorted JSON (stable input for hashes)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
