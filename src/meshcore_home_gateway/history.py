"""The local history in SQLite (ADR 0016): messages in and out, positions of contacts, and when
each node was last heard.

One file in the state directory, written only by the service. A history that cannot be opened or
written is logged and switched off for the rest of the run: it never stops messaging. A file that
is not a database is moved aside and a new one started.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshcore_home_gateway.core.mesh import Fix, Heard, Signal
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.views import HistoryEntry

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # 2: node.last_advert
DAY_S = 86_400.0
MAX_MESSAGES = 10_000
MAX_POSITIONS = 100_000  # beyond it the oldest go, except each contact's newest
_SCHEMA = """
CREATE TABLE message (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    contact_key TEXT,
    channel_index INTEGER,
    name TEXT,
    text TEXT NOT NULL,
    state TEXT,
    hops INTEGER,
    snr_db REAL
);
CREATE INDEX message_at ON message (at);
CREATE TABLE position (
    id INTEGER PRIMARY KEY,
    contact_key TEXT NOT NULL,
    at REAL NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude_m REAL,
    source TEXT NOT NULL
);
CREATE INDEX position_contact_at ON position (contact_key, at);
CREATE TABLE node (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type INTEGER NOT NULL,
    first_heard REAL NOT NULL,
    heard_at REAL NOT NULL,
    how TEXT NOT NULL,
    hops INTEGER,
    snr_db REAL,
    rssi_dbm INTEGER,
    last_advert INTEGER NOT NULL DEFAULT 0
);
"""
_MIGRATIONS = {  # from version n to n + 1
    1: ["ALTER TABLE node ADD COLUMN last_advert INTEGER NOT NULL DEFAULT 0"],
}


@dataclass(frozen=True, slots=True)
class Retention:
    messages_days: float = 30.0
    positions_days: float = 30.0
    nodes_days: float = 30.0


@dataclass(frozen=True, slots=True)
class NodeRow:
    key: str
    name: str
    node_type: int
    first_heard: float
    heard: Heard
    last_advert: int = 0  # the node's clock at the advert behind name and type


class NewerSchemaError(Exception):
    """The file was written by a newer version of the program."""


class History:
    def __init__(self, connection: sqlite3.Connection, retention: Retention) -> None:
        self._db = connection
        self._retention = retention
        self.broken = False

    @classmethod
    def open(cls, path: Path, retention: Retention | None = None) -> History | None:
        """The history at `path`, created if missing; None if it cannot be used."""
        retention = retention or Retention()
        try:
            return cls(_connect(path), retention)
        except NewerSchemaError as exc:
            log.warning("history %s: %s; running without it (a downgrade?)", path, exc)
        except sqlite3.DatabaseError as exc:
            if not isinstance(exc, sqlite3.OperationalError) or "not a database" in str(exc):
                aside = path.with_name(path.name + ".corrupt")
                with contextlib.suppress(OSError):
                    path.replace(aside)  # SQLite has removed its log files on closing it
                    log.warning("history %s unreadable (%s), moved to %s", path, exc, aside)
                    with contextlib.suppress(sqlite3.Error, OSError):
                        return cls(_connect(path), retention)
            log.warning("history %s cannot be used (%s): running without", path, exc)
        except OSError as exc:
            log.warning("history %s cannot be used (%s): running without", path, exc)
        return None

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._db.close()

    # --- writing --------------------------------------------------------------------------------

    def add_message(self, entry: HistoryEntry) -> None:
        self._write(
            "INSERT INTO message (at, direction, kind, contact_key, channel_index, name, text, "
            "state, hops, snr_db) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.at, entry.direction, entry.kind, entry.contact_key, entry.channel_index,
             entry.name, entry.text, entry.state, entry.hops, entry.snr_db),
        )  # fmt: skip

    def add_position(self, key: str, fix: Fix) -> None:
        position = fix.position
        self._write(
            "INSERT INTO position (contact_key, at, latitude, longitude, altitude_m, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, fix.at, position.latitude, position.longitude, fix.altitude_m, fix.source),
        )

    def node_heard(
        self, key: str, name: str, node_type: int, heard: Heard, last_advert: int = 0
    ) -> None:
        """`last_advert`: the node's clock at the advert that gave name and type."""
        signal = heard.signal
        self._write(
            "INSERT INTO node (key, name, type, first_heard, heard_at, how, hops, snr_db, "
            "rssi_dbm, last_advert) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO "
            "UPDATE SET name = excluded.name, type = excluded.type, "
            "heard_at = excluded.heard_at, how = excluded.how, hops = excluded.hops, "
            "snr_db = excluded.snr_db, rssi_dbm = excluded.rssi_dbm, "
            "last_advert = excluded.last_advert WHERE excluded.heard_at >= node.heard_at",
            (key, name, node_type, heard.at, heard.at, heard.how, signal.hops, signal.snr_db,
             signal.rssi_dbm, last_advert),
        )  # fmt: skip

    def purge(self, now: float) -> None:
        """Drop what is older than the retention, and messages and positions beyond their caps
        (the oldest first; never a contact's newest position, which only its age removes)."""
        r = self._retention
        self._write("DELETE FROM message WHERE at < ?", (now - r.messages_days * DAY_S,))
        self._write(
            "DELETE FROM message WHERE id <= "
            "(SELECT id FROM message ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (MAX_MESSAGES,),
        )
        self._write("DELETE FROM position WHERE at < ?", (now - r.positions_days * DAY_S,))
        self._write(  # never the newest of a contact: the row last_fixes() reads
            "DELETE FROM position WHERE id <= (SELECT id FROM position ORDER BY id DESC LIMIT 1 "
            "OFFSET ?) AND id NOT IN (SELECT (SELECT id FROM position WHERE contact_key = "
            "p.contact_key ORDER BY at DESC, id DESC LIMIT 1) FROM (SELECT DISTINCT contact_key "
            "FROM position) p)",
            (MAX_POSITIONS,),
        )
        self._write("DELETE FROM node WHERE heard_at < ?", (now - r.nodes_days * DAY_S,))

    def _write(self, sql: str, args: tuple[Any, ...]) -> None:
        if self.broken:
            return
        try:
            self._db.execute(sql, args)
        except sqlite3.Error as exc:
            self.broken = True
            log.warning("history cannot be written (%s): running without it until restart", exc)

    # --- reading --------------------------------------------------------------------------------

    def recent_messages(self, limit: int) -> list[HistoryEntry]:
        rows = self._read(
            "SELECT at, direction, kind, text, contact_key, channel_index, name, state, hops, "
            "snr_db FROM message ORDER BY at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [HistoryEntry(*row) for row in rows]

    def last_fixes(self) -> dict[str, Fix]:
        """The newest position of every contact that has one."""
        rows = self._read(
            "SELECT contact_key, at, latitude, longitude, altitude_m, source FROM position p "
            "WHERE id = (SELECT id FROM position WHERE contact_key = p.contact_key "
            "ORDER BY at DESC, id DESC LIMIT 1)",
            (),
        )
        return {
            key: Fix(Position(lat, lon), at, source, altitude)
            for key, at, lat, lon, altitude, source in rows
        }

    def nodes(self) -> list[NodeRow]:
        rows = self._read(
            "SELECT key, name, type, first_heard, heard_at, how, hops, snr_db, rssi_dbm, "
            "last_advert FROM node ORDER BY heard_at",
            (),
        )
        return [
            NodeRow(key, name, kind, first, Heard(at, how, Signal(hops, snr, rssi)), advert)
            for key, name, kind, first, at, how, hops, snr, rssi, advert in rows
        ]

    def trace(self, key: str, since: float) -> list[Fix]:
        rows = self._read(
            "SELECT at, latitude, longitude, altitude_m, source FROM position "
            "WHERE contact_key = ? AND at >= ? ORDER BY at, id",
            (key, since),
        )
        return [Fix(Position(lat, lon), at, source, alt) for at, lat, lon, alt, source in rows]

    def _read(self, sql: str, args: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.broken:
            return []
        try:
            return list(self._db.execute(sql, args))
        except sqlite3.Error as exc:
            self.broken = True
            log.warning("history cannot be read (%s): running without it until restart", exc)
            return []


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit: every statement is its own transaction; with the write-ahead log and
    # synchronous NORMAL a power cut can lose the last ones, never the database
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=True)
    try:
        path.chmod(0o600)
        _prepare(db)
    except BaseException:
        db.close()
        raise
    return db


def _prepare(db: sqlite3.Connection) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    if version > SCHEMA_VERSION:
        raise NewerSchemaError(f"schema version {version} is newer than {SCHEMA_VERSION}")
    if version == 0:
        with _transaction(db):
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    while version < SCHEMA_VERSION:
        with _transaction(db):
            for statement in _MIGRATIONS[version]:
                db.execute(statement)
            version += 1
            db.execute(f"PRAGMA user_version = {version}")


@contextlib.contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
