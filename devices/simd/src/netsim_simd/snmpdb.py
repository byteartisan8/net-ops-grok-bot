"""SQLite store between simd (writer) and the snmpsim variation module (reader).

Layout: snmpsim's SQL variation-module table (``oid, tag, value, maxaccess``) plus one column, ``since``:

* ``oid`` is snmpsim's sortable form: every sub-identifier right-aligned in 10 characters
  (``sql_oid("1.3.6")`` -> ``"         1.         3.         6"``), so ``ORDER BY oid`` is SNMP
  lexicographic order and GETNEXT is ``WHERE oid > ? ORDER BY oid LIMIT 1``;
* ``tag`` / ``value`` use snmprec encoding (``2`` Integer32, ``4x`` hex OCTET STRING, ``6`` OID,
  ``66`` Gauge32/Unsigned32, ``67`` TimeTicks, ...);
* ``since`` (seconds on the system-wide CLOCK_BOOTTIME) makes a TimeTicks row live: the reader returns
  ``(now - since) * 100``. That is how ``sysUpTime`` and ``cpwVcUpTime`` count without simd rewriting
  them every tick.

simd replaces the whole table in one transaction whenever the MIB view changes, so a reader sees either
the old or the new view, never a mix (rollback journal; readers open the file read-only).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: Data file anchor line: snmpsim hands every request under this OID to the ``netsim`` variation module.
VARIATION_MODULE = "netsim"
TIMETICKS_TAG = "67"
TIMETICKS_MODULO = 2**32

SCHEMA = """
CREATE TABLE IF NOT EXISTS snmprec (
    oid TEXT NOT NULL PRIMARY KEY,
    tag TEXT NOT NULL,
    value TEXT NOT NULL,
    maxaccess TEXT NOT NULL DEFAULT 'read-only',
    since REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
"""


def boottime() -> float:
    """System-wide monotonic clock shared by every process of the container (seconds)."""
    clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    return time.clock_gettime(clock)


def timeticks_since(since: float, now: float | None = None) -> int:
    """Hundredths of a second elapsed since ``since`` (TimeTicks wrap at 2**32)."""
    elapsed = (boottime() if now is None else now) - since
    return int(max(elapsed, 0.0) * 100) % TIMETICKS_MODULO


def oid_tuple(oid: str | Iterable[int]) -> tuple[int, ...]:
    if isinstance(oid, str):
        return tuple(int(x) for x in oid.strip(".").split("."))
    return tuple(int(x) for x in oid)


def oid_str(oid: str | Iterable[int]) -> str:
    return ".".join(str(x) for x in oid_tuple(oid))


def sql_oid(oid: str | Iterable[int]) -> str:
    """snmpsim's sortable OID text (see module docstring)."""
    return ".".join(f"{x:>10}" for x in oid_tuple(oid))


def unsql_oid(text: str) -> str:
    return ".".join(x.strip() for x in text.split("."))


@dataclass(frozen=True, slots=True)
class Row:
    """One SNMP variable: OID, snmprec tag and value, optional live-TimeTicks base."""

    oid: tuple[int, ...]
    tag: str
    value: str
    since: float | None = None


class SnmpDbWriter:
    """simd side: replaces the table atomically when the rows change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o755)
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.executescript(SCHEMA)
        os.chmod(path, 0o644)
        self._last: tuple[Row, ...] | None = None
        self.generation = 0

    def write(self, rows: Iterable[Row], meta: Mapping[str, str] | None = None) -> bool:
        """Replace the table with ``rows``; returns False (and writes nothing) if they did not change."""
        snapshot = tuple(sorted(rows, key=lambda r: r.oid))
        if snapshot == self._last:
            return False
        self.generation += 1
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM snmprec")
            cur.executemany(
                "INSERT INTO snmprec (oid, tag, value, maxaccess, since) VALUES (?, ?, ?, 'read-only', ?)",
                [(sql_oid(r.oid), r.tag, r.value, r.since) for r in snapshot],
            )
            values = {"generation": str(self.generation), "rows": str(len(snapshot)), **(meta or {})}
            cur.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", sorted(values.items()))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        self._last = snapshot
        return True

    def close(self) -> None:
        self._conn.close()


@dataclass(frozen=True, slots=True)
class DbValue:
    oid: str
    tag: str
    value: str


class SnmpDbReader:
    """snmpsim side (read-only). Reopens the file if simd recreated it."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._inode: int | None = None

    def _connection(self) -> sqlite3.Connection | None:
        try:
            inode = os.stat(self.path).st_ino
        except FileNotFoundError:
            self.close()
            return None
        if self._conn is None or inode != self._inode:
            self.close()
            try:
                self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2.0)
                self._conn.execute("SELECT 1 FROM snmprec LIMIT 1")
            except sqlite3.Error:
                self.close()
                return None
            self._inode = inode
        return self._conn

    def _query(self, sql: str, key: str) -> DbValue | None:
        conn = self._connection()
        if conn is None:
            return None
        try:
            row = conn.execute(sql, (key,)).fetchone()
        except sqlite3.Error:
            self.close()
            return None
        if row is None:
            return None
        oid, tag, value, since = row
        if since is not None and tag == TIMETICKS_TAG:
            value = str(timeticks_since(since))
        return DbValue(unsql_oid(oid), tag, value)

    def get(self, oid: str | Iterable[int]) -> DbValue | None:
        return self._query("SELECT oid, tag, value, since FROM snmprec WHERE oid = ?", sql_oid(oid))

    def next(self, oid: str | Iterable[int]) -> DbValue | None:
        """First variable strictly after ``oid`` in SNMP lexicographic order."""
        return self._query(
            "SELECT oid, tag, value, since FROM snmprec WHERE oid > ? ORDER BY oid LIMIT 1", sql_oid(oid)
        )

    def all(self) -> list[DbValue]:
        conn = self._connection()
        if conn is None:
            return []
        rows = conn.execute("SELECT oid, tag, value, since FROM snmprec ORDER BY oid").fetchall()
        out = []
        for oid, tag, value, since in rows:
            if since is not None and tag == TIMETICKS_TAG:
                value = str(timeticks_since(since))
            out.append(DbValue(unsql_oid(oid), tag, value))
        return out

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None
        self._inode = None


def write_datafile(data_dir: Path, community: str, anchor: str) -> Path:
    """snmpsim data file ``<community>.snmprec``: one subtree line handing ``anchor`` to our module."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{community}.snmprec"
    text = f"{oid_str(anchor)}|:{VARIATION_MODULE}|\n"
    if not path.exists() or path.read_text(encoding="ascii") != text:
        path.write_text(text, encoding="ascii")
    os.chmod(path, 0o644)
    return path
