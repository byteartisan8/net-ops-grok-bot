"""Read and update the lab's ``.env`` (``KEY=VALUE`` lines, as written by ``make env`` / ``tools/gen_env.py``).

Updates replace a key's line in place (or append it), never touch other lines, and keep the file private (0600).
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def default_env_file() -> Path | None:
    """The repository's ``.env`` (``None`` when there is no repository, e.g. inside a container)."""
    try:
        from netsim_common.paths import repo_root

        return repo_root() / ".env"
    except FileNotFoundError:
        return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def read_env_file(path: Path | None) -> dict[str, str]:
    """``{KEY: value}`` of an env file (missing file = empty). Comments and blank lines are ignored."""
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE_RE.match(line.strip())
        if match and match["key"] not in values:
            values[match["key"]] = _unquote(match["value"])
    return values


def update_env_file(path: Path, updates: Mapping[str, str]) -> list[str]:
    """Set ``KEY=value`` for every update; returns the keys whose value changed.

    An existing ``KEY=`` line is rewritten in place (first occurrence), a missing key is appended. The file is
    replaced atomically and stays mode 0600.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    current = read_env_file(path)
    changed = [k for k, v in updates.items() if current.get(k) != v]
    if not changed:
        return []
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = LINE_RE.match(line.strip())
        key = match["key"] if match else None
        if key in changed and key not in done:
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(line)
    out += [f"{k}={updates[k]}" for k in changed if k not in done]
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return changed
