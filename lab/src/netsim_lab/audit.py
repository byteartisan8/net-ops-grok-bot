"""lab-api's audit log: one JSON line per write (who, when, what, diff, result), on lab-api's volume.

``who`` is the ``X-Lab-Actor`` header (e.g. ``grok-bot``, ``labctl``; ``unknown`` if absent) plus the client
address; NetBox webhooks are recorded as actor ``netbox-webhook``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        actor: str,
        client: str | None,
        action: str,
        target: str | None = None,
        request: Any = None,
        diff: Any = None,
        result: Any = None,
        status: int | None = None,
        ok: bool = True,
    ) -> dict[str, Any]:
        entry = {
            "ts": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": actor,
            "client": client,
            "action": action,
            "target": target,
            "request": request,
            "diff": diff,
            "result": result,
            "status": status,
            "ok": ok,
        }
        line = json.dumps(entry, default=str, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return entry

    def read(self, since: float | None = None, limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self._lock, self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if since is not None and entry.get("ts", 0) <= since:
                    continue
                if action is not None and entry.get("action") != action:
                    continue
                out.append(entry)
        return out[-limit:] if limit else out
