"""SQLite persistence (volume ``netsim_grok-bot-data``): incidents, proposals, activity, chat, counters, control.

Documents are JSON in a ``doc`` column; the few columns next to it exist for ordering and filtering. One connection,
WAL mode; every call is short, so the event loop calls it directly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from netsim_bot.models import Activity, ChatMessage, Incident, Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT, doc TEXT);
CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, incident_id TEXT, created REAL, status TEXT, doc TEXT);
CREATE TABLE IF NOT EXISTS activities (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, incident_id TEXT, chat_id TEXT, bot TEXT, doc TEXT
);
CREATE INDEX IF NOT EXISTS activities_incident ON activities (incident_id, seq);
CREATE TABLE IF NOT EXISTS chat (id TEXT PRIMARY KEY, chat_id TEXT, ts REAL, doc TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ meta, counters
    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._exec("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_meta(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, default=str)),
        )

    def next_id(self, prefix: str, width: int = 4) -> str:
        """``INC-0001``, ``CHG-0001``, ...: a per-prefix counter that survives restarts."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM meta WHERE key = ?", (f"seq:{prefix}",)).fetchone()
                n = (json.loads(row[0]) if row else 0) + 1
                self._db.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"seq:{prefix}", json.dumps(n)),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return f"{prefix}-{n:0{width}d}"

    # ------------------------------------------------------------------ incidents
    def save_incident(self, inc: Incident) -> None:
        self._exec(
            "INSERT INTO incidents (id, created, updated, status, doc) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated = excluded.updated, status = excluded.status, doc = excluded.doc",
            (inc.id, inc.created, inc.updated, inc.status, json.dumps(inc.to_dict(), default=str)),
        )

    def incident(self, incident_id: str) -> Incident | None:
        rows = self._exec("SELECT doc FROM incidents WHERE id = ?", (incident_id,))
        return Incident.from_dict(json.loads(rows[0][0])) if rows else None

    def incidents(self, limit: int = 50, active_only: bool = False) -> list[Incident]:
        where = "WHERE status != 'resolved'" if active_only else ""
        rows = self._exec(f"SELECT doc FROM incidents {where} ORDER BY created DESC LIMIT ?", (limit,))
        return [Incident.from_dict(json.loads(r[0])) for r in rows]

    # ------------------------------------------------------------------ proposals
    def save_proposal(self, p: Proposal) -> None:
        self._exec(
            "INSERT INTO proposals (id, incident_id, created, status, doc) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, doc = excluded.doc",
            (p.id, p.incident_id, p.created, p.status, json.dumps(p.to_dict(), default=str)),
        )

    def proposal(self, proposal_id: str) -> Proposal | None:
        rows = self._exec("SELECT doc FROM proposals WHERE id = ?", (proposal_id,))
        return Proposal.from_dict(json.loads(rows[0][0])) if rows else None

    def proposals(self, incident_id: str | None = None, status: str | None = None, limit: int = 100) -> list[Proposal]:
        clauses, args = [], []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            args.append(incident_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._exec(f"SELECT doc FROM proposals {where} ORDER BY created DESC LIMIT ?", (*args, limit))
        return [Proposal.from_dict(json.loads(r[0])) for r in rows]

    # ------------------------------------------------------------------ activity
    def add_activity(self, a: Activity) -> Activity:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO activities (ts, incident_id, chat_id, bot, doc) VALUES (?, ?, ?, ?, ?)",
                (a.ts, a.incident_id, a.chat_id, a.bot, json.dumps(a.to_dict(), default=str)),
            )
            a.seq = cur.lastrowid
        return a

    def activities(self, incident_id: str | None = None, after: int | None = None, limit: int = 200) -> list[Activity]:
        clauses, args = [], []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            args.append(incident_id)
        if after is not None:
            clauses.append("seq > ?")
            args.append(after)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._exec(f"SELECT seq, doc FROM activities {where} ORDER BY seq DESC LIMIT ?", (*args, limit))
        out = []
        for seq, doc in reversed(rows):
            a = Activity.from_dict(json.loads(doc))
            a.seq = seq
            out.append(a)
        return out

    # ------------------------------------------------------------------ chat
    def save_chat(self, m: ChatMessage) -> None:
        self._exec(
            "INSERT INTO chat (id, chat_id, ts, doc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET doc = excluded.doc",
            (m.id, m.chat_id, m.ts, json.dumps(m.to_dict(), default=str)),
        )

    def chat(self, chat_id: str | None = None, limit: int = 100) -> list[ChatMessage]:
        if chat_id is None:
            rows = self._exec("SELECT doc FROM chat ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        else:
            rows = self._exec(
                "SELECT doc FROM chat WHERE chat_id = ? ORDER BY ts DESC, id DESC LIMIT ?", (chat_id, limit)
            )
        return [ChatMessage.from_dict(json.loads(r[0])) for r in reversed(rows)]
