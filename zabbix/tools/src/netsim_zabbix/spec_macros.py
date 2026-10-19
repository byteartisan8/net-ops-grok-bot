"""``{$NETSIM.PW.SPEC}`` values per host (contract 9.2), split into chunks that fit a Zabbix 8.0 macro value.

The first chunk is exactly ``netsim_common.pw_spec_macro_value(spec, host)`` whenever that fits in 2048 characters
(every host of the lab: a hub's 6 entries are about 1 KB). A longer spec continues in ``{$NETSIM.PW.SPEC.2}`` ..
``{$NETSIM.PW.SPEC.8}`` (each a compact JSON list, entries in the same order); the template defaults them to ``[]``
and the "Expected PWs" discovery concatenates them. That is about 12 PWs per chunk, ~96 per host.
"""

from __future__ import annotations

import json

from netsim_common.bsm import BsmSpec, PwSpecEntry, pw_spec_for_host
from netsim_zabbix import names as N


class SpecTooLarge(ValueError):
    """A host's spec does not fit into the available macro chunks."""


def _dump(entries: list[PwSpecEntry]) -> str:
    return json.dumps([e.model_dump(mode="json") for e in entries], separators=(",", ":"))


def spec_chunks(entries: list[PwSpecEntry], limit: int = N.SPEC_MACRO_MAX_LEN) -> list[str]:
    """Compact JSON lists, each at most ``limit`` characters, together holding ``entries`` in order."""
    chunks: list[str] = []
    current: list[PwSpecEntry] = []
    for entry in entries:
        if current and len(_dump([*current, entry])) > limit:
            chunks.append(_dump(current))
            current = []
        current.append(entry)
        if len(_dump(current)) > limit:
            raise SpecTooLarge(f"one spec entry alone exceeds {limit} characters: {entry}")
    chunks.append(_dump(current))
    if len(chunks) > len(N.SPEC_MACROS):
        raise SpecTooLarge(
            f"spec needs {len(chunks)} macros of {limit} characters, the template has {len(N.SPEC_MACROS)}"
        )
    return chunks


def host_spec_macros(spec: BsmSpec, host: str) -> dict[str, str]:
    """``{macro: value}`` for ``host``: always ``{$NETSIM.PW.SPEC}`` (``[]`` without PWs), overflow only if needed."""
    chunks = spec_chunks(pw_spec_for_host(spec, host))
    return dict(zip(N.SPEC_MACROS, chunks, strict=False))
