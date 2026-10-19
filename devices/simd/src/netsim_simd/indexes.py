"""Stable SNMP indexes persisted on the device volume (contract section 7, "cpwVcIndex").

* ``cpwVcIndex`` is keyed by ``(service-id, role)``; ``ifIndex`` by interface name.
* A new key gets ``max(all indexes ever allocated) + 1``; several new keys at once are allocated in a
  deterministic order (PWs by service then role, primary first; interfaces physical first, then by name).
* Mappings are never deleted: a PW that is removed disappears from SNMP, and the same index comes back
  when it is re-created.

The file is rewritten atomically (temp file, fsync, rename), so a crash never leaves a torn map.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from netsim_common.enums import PwRole

FORMAT_VERSION = 1


def pw_key(service_id: str, role: PwRole | str) -> str:
    """Map key of a PW (same text as its ``cpwVcName``)."""
    return f"{service_id}:{PwRole(role).value}"


class IndexStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._pw: dict[str, int] = {}
        self._if: dict[str, int] = {}
        if path.exists():
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            self._pw = {str(k): int(v) for k, v in data.get("pw", {}).items()}
            self._if = {str(k): int(v) for k, v in data.get("if", {}).items()}

    # ------------------------------------------------------------------ lookups
    @property
    def pw_map(self) -> dict[str, int]:
        return dict(self._pw)

    @property
    def if_map(self) -> dict[str, int]:
        return dict(self._if)

    def pw_index(self, service_id: str, role: PwRole | str) -> int | None:
        return self._pw.get(pw_key(service_id, role))

    def if_index(self, name: str) -> int | None:
        return self._if.get(name)

    # ------------------------------------------------------------------ allocation
    def allocate_pws(self, keys: Iterable[tuple[str, PwRole]]) -> dict[tuple[str, PwRole], int]:
        """Indexes for ``keys``, allocating (and persisting) the missing ones."""
        wanted = sorted(set(keys), key=lambda k: (k[0], PwRole(k[1]).sort_index))
        changed = self._allocate(self._pw, [pw_key(*k) for k in wanted])
        if changed:
            self.save()
        return {k: self._pw[pw_key(*k)] for k in wanted}

    def allocate_ifs(self, names: Iterable[tuple[str, bool]]) -> dict[str, int]:
        """Indexes for ``(name, is_subinterface)`` pairs, allocating the missing ones."""
        wanted = sorted(set(names), key=lambda n: (n[1], n[0]))
        changed = self._allocate(self._if, [n for n, _ in wanted])
        if changed:
            self.save()
        return {n: self._if[n] for n, _ in wanted}

    @staticmethod
    def _allocate(table: dict[str, int], keys: list[str]) -> bool:
        changed = False
        for key in keys:
            if key not in table:
                table[key] = max(table.values(), default=0) + 1
                changed = True
        return changed

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        payload = {
            "version": FORMAT_VERSION,
            "pw": dict(sorted(self._pw.items(), key=lambda kv: kv[1])),
            "if": dict(sorted(self._if.items(), key=lambda kv: kv[1])),
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (same-directory temp file + fsync + rename + dir fsync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
