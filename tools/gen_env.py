#!/usr/bin/env python3
"""Create or complete .env from .env.example (stdlib only; used by `make env`).

* ``.env`` missing: copy ``.env.example``, replacing every ``__generate__`` value with a random secret.
* ``.env`` present: never change existing values; append (with generated secrets) any variable that
  ``.env.example`` defines but ``.env`` lacks, and report it.

Empty values stay empty: they are filled later by the owning component (e.g. API tokens).
"""

from __future__ import annotations

import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATE = "__generate__"
LINE_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
# Django's SECRET_KEY and NetBox's API token peppers need >= 50 characters; token_urlsafe(n) yields ~1.3*n chars.
LENGTH_BY_SUFFIX = {"SECRET_KEY": 64, "PEPPER": 64}
DEFAULT_BYTES = 24


def secret_for(key: str) -> str:
    for suffix, n in LENGTH_BY_SUFFIX.items():
        if key.endswith(suffix):
            return secrets.token_urlsafe(n)[:n]
    return secrets.token_urlsafe(DEFAULT_BYTES)


def render(line: str) -> str:
    match = LINE_RE.match(line)
    if match and match["value"] == GENERATE:
        return f"{match['key']}={secret_for(match['key'])}"
    return line


def keys(text: str) -> dict[str, str]:
    return {m["key"]: line for line in text.splitlines() if (m := LINE_RE.match(line))}


def main(example: Path = REPO_ROOT / ".env.example", target: Path = REPO_ROOT / ".env") -> int:
    template = example.read_text(encoding="utf-8")
    if not target.exists():
        target.write_text("\n".join(render(line) for line in template.splitlines()) + "\n", encoding="utf-8")
        target.chmod(0o600)
        print(f"created {target.name} from {example.name} (random secrets generated)")
        return 0
    current = target.read_text(encoding="utf-8")
    missing = [k for k in keys(template) if k not in keys(current)]
    if not missing:
        print(f"{target.name} is complete; nothing changed")
        return 0
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = [f"# added by `make env` at {stamp}"] + [render(keys(template)[k]) for k in missing]
    sep = "" if current.endswith("\n") else "\n"
    target.write_text(current + sep + "\n".join(added) + "\n", encoding="utf-8")
    print(f"appended to {target.name}: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
