"""SQLite persistence (volume ``netsim_grok-bot-data``): alerts, incidents, change tickets, pages, activity, questions,
counters and control.

Documents are JSON in a ``doc`` column; the few columns next to it exist for ordering and filtering. One connection,
WAL mode; every call is short, so the event loop calls it directly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from netsim_bot.models import Activity, Alert, ChatMessage, Incident, Page, Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT, doc TEXT);
CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, incident_id TEXT, created REAL, status TEXT, doc TEXT);
CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, received REAL, event_id TEXT, incident_id TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS alerts_event ON alerts (event_id);
CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, created REAL, role TEXT, incident_id TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS pages_role ON pages (role, created);
CREATE TABLE IF NOT EXISTS activities (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, incident_id TEXT, chat_id TEXT, bot TEXT, doc TEXT
);
CREATE INDEX IF NOT EXISTS activities_incident ON activities (incident_id, seq);
CREATE TABLE IF NOT EXISTS chat (id TEXT PRIMARY KEY, chat_id TEXT, ts REAL, doc TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
#: 1 = the in-house crew of session 07/08 (incidents of Zabbix problems); 2 = the office of three roles.
SCHEMA_VERSION = 2
OLD_TABLES = ("incidents", "proposals", "activities", "chat")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        self._db.executescript(SCHEMA)
        self.set_meta("schema", SCHEMA_VERSION)

    def _migrate(self) -> None:
        """A database of the earlier crew: its records are kept as ``<table>_v1`` (not shown), ids keep counting."""
        tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "meta" not in tables or "incidents" not in tables:
            return
        row = self._db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is not None and json.loads(row[0]) >= SCHEMA_VERSION:
            return
        for name in OLD_TABLES:
            if name in tables and f"{name}_v1" not in tables:
                self._db.execute(f"ALTER TABLE {name} RENAME TO {name}_v1")
        self._db.execute("DROP INDEX IF EXISTS activities_incident")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    @staticmethod
    def _where(clauses: list[str]) -> str:
        return f"WHERE {' AND '.join(clauses)}" if clauses else ""

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
        where = "WHERE status != 'closed'" if active_only else ""
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
        rows = self._exec(
            f"SELECT doc FROM proposals {self._where(clauses)} ORDER BY created DESC LIMIT ?", (*args, limit)
        )
        return [Proposal.from_dict(json.loads(r[0])) for r in rows]

    # ------------------------------------------------------------------ alerts
    def save_alert(self, a: Alert) -> None:
        self._exec(
            "INSERT INTO alerts (id, received, event_id, incident_id, doc) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET incident_id = excluded.incident_id, doc = excluded.doc",
            (a.id, a.received, a.event_id, a.incident_id, json.dumps(a.to_dict(), default=str)),
        )

    def alerts(self, incident_id: str | None = None, limit: int = 100) -> list[Alert]:
        clauses, args = [], []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            args.append(incident_id)
        rows = self._exec(
            f"SELECT doc FROM alerts {self._where(clauses)} ORDER BY received DESC, id DESC LIMIT ?", (*args, limit)
        )
        return [Alert.from_dict(json.loads(r[0])) for r in rows]

    # ------------------------------------------------------------------ pages
    def save_page(self, p: Page) -> None:
        self._exec(
            "INSERT INTO pages (id, created, role, incident_id, doc) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET doc = excluded.doc",
            (p.id, p.created, p.role, p.incident_id, json.dumps(p.to_dict(), default=str)),
        )

    def page(self, page_id: str) -> Page | None:
        rows = self._exec("SELECT doc FROM pages WHERE id = ?", (page_id,))
        return Page.from_dict(json.loads(rows[0][0])) if rows else None

    def pages(
        self,
        role: str | None = None,
        incident_id: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[Page]:
        clauses, args = [], []
        for col, value in (("role", role), ("incident_id", incident_id)):
            if value is not None:
                clauses.append(f"{col} = ?")
                args.append(value)
        if since is not None:
            clauses.append("created >= ?")
            args.append(since)
        rows = self._exec(
            f"SELECT doc FROM pages {self._where(clauses)} ORDER BY created DESC, id DESC LIMIT ?", (*args, limit)
        )
        return [Page.from_dict(json.loads(r[0])) for r in rows]

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
        rows = self._exec(
            f"SELECT seq, doc FROM activities {self._where(clauses)} ORDER BY seq DESC LIMIT ?", (*args, limit)
        )
        out = []
        for seq, doc in reversed(rows):
            a = Activity.from_dict(json.loads(doc))
            a.seq = seq
            out.append(a)
        return out

    # ------------------------------------------------------------------ chat (Ask the NOC)
    def save_chat(self, m: ChatMessage) -> None:
        self._exec(
            "INSERT INTO chat (id, chat_id, ts, doc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET doc = excluded.doc",
            (m.id, m.chat_id, m.ts, json.dumps(m.to_dict(), default=str)),
        )

    def chat_message(self, message_id: str) -> ChatMessage | None:
        rows = self._exec("SELECT doc FROM chat WHERE id = ?", (message_id,))
        return ChatMessage.from_dict(json.loads(rows[0][0])) if rows else None

    def chat(self, chat_id: str | None = None, limit: int = 100) -> list[ChatMessage]:
        if chat_id is None:
            rows = self._exec("SELECT doc FROM chat ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        else:
            rows = self._exec(
                "SELECT doc FROM chat WHERE chat_id = ? ORDER BY ts DESC, id DESC LIMIT ?", (chat_id, limit)
            )
        return [ChatMessage.from_dict(json.loads(r[0])) for r in reversed(rows)]
