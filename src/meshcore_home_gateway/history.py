"""The local history in SQLite (ADR 0016): messages in and out, positions of contacts, when
each node was last heard, and the readings of the contacts' telemetry answers.

One file in the state directory, written only by the service. A history that cannot be opened or
written is logged and switched off for the rest of the run: it never stops messaging. A file that
is not a database is moved aside and a new one started. The hourly purge runs in a thread on a
connection of its own, so the event loop goes on meanwhile (CODE-002); the writes that come in
the meantime wait in memory and follow it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.mesh import Fix, Heard, Readings, Signal
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.health import FAILURES
from meshcore_home_gateway.views import HistoryEntry

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # a bug in the purge's thread is counted like the others

SCHEMA_VERSION = 2  # 2: node.last_advert
DAY_S = 86_400.0
MAX_MESSAGES = 10_000
MAX_POSITIONS = 100_000  # beyond it the oldest go, except each contact's newest
MAX_READINGS = 200_000  # beyond it the oldest go, never a contact's newest answer
MAX_HELD = 10_000  # writes held while a purge runs; beyond them, rows are lost (logged once)
PURGE_BUSY_S = 10.0  # the purge's connection waits this long for the lock (sqlite3 timeout)
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
# added to every file of the current version without a new one: older versions of the program
# ignore these tables, so a downgrade keeps the rest of the history
_ADDED = (
    (
        "CREATE TABLE IF NOT EXISTS reading (id INTEGER PRIMARY KEY, contact_key TEXT NOT NULL, "
        "at REAL NOT NULL, quantity TEXT NOT NULL, value REAL NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS reading_contact_at ON reading (contact_key, at)",
)


_MESSAGES_BEYOND_CAP = (
    "DELETE FROM message WHERE id <= (SELECT id FROM message ORDER BY id DESC LIMIT 1 OFFSET ?)"
)
# never the newest of a contact: the row last_fixes() reads
_POSITIONS_BEYOND_CAP = (
    "DELETE FROM position WHERE id <= (SELECT id FROM position ORDER BY id DESC LIMIT 1 OFFSET ?) "
    "AND id NOT IN (SELECT (SELECT id FROM position WHERE contact_key = p.contact_key "
    "ORDER BY at DESC, id DESC LIMIT 1) FROM (SELECT DISTINCT contact_key FROM position) p)"
)
# the rows of the nodes the gateway forgot: all but those in a JSON list of keys
_NODES_FORGOTTEN = "DELETE FROM node WHERE key NOT IN (SELECT value FROM json_each(?))"
# never the rows of a contact's newest answer: those last_readings() reads
_READINGS_BEYOND_CAP = (
    "DELETE FROM reading WHERE id <= (SELECT id FROM reading ORDER BY id DESC LIMIT 1 OFFSET ?) "
    "AND at < (SELECT MAX(at) FROM reading n WHERE n.contact_key = reading.contact_key)"
)


@dataclass(frozen=True, slots=True)
class Retention:
    messages_days: float = 30.0
    positions_days: float = 30.0
    nodes_days: float = 7.0  # as the heard nodes in memory (core.mesh.NODE_TTL_S, ADR 0016)
    readings_days: float = 30.0


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


Steps = list[tuple[str, list[tuple[Any, ...]]]]  # statements, each with its rows of arguments


class History:
    def __init__(
        self, connection: sqlite3.Connection, retention: Retention, path: Path | None = None
    ) -> None:
        """`path`: the file, for the purge's own connection; None: the purge runs inline."""
        self._db = connection
        self._retention = retention
        self._path = path
        self.broken = False
        self._purging: concurrent.futures.Future[None] | None = None  # in its thread
        self._held: list[Steps] = []  # the writes that came while it ran, in their order
        self._dropped = 0  # writes beyond MAX_HELD
        self._worker: concurrent.futures.ThreadPoolExecutor | None = None

    @classmethod
    def open(cls, path: Path, retention: Retention | None = None) -> History | None:
        """The history at `path`, created if missing; None if it cannot be used."""
        retention = retention or Retention()
        try:
            return cls(_connect(path), retention, path)
        except NewerSchemaError as exc:
            log.warning("history %s: %s; running without it (a downgrade?)", path, exc)
        except sqlite3.DatabaseError as exc:
            if not isinstance(exc, sqlite3.OperationalError) or "not a database" in str(exc):
                aside = path.with_name(path.name + ".corrupt")
                with contextlib.suppress(OSError):
                    path.replace(aside)  # SQLite has removed its log files on closing it
                    log.warning("history %s unreadable (%s), moved to %s", path, exc, aside)
                    with contextlib.suppress(sqlite3.Error, OSError):
                        return cls(_connect(path), retention, path)
            log.warning("history %s cannot be used (%s): running without", path, exc)
        except OSError as exc:
            log.warning("history %s cannot be used (%s): running without", path, exc)
        return None

    def close(self) -> None:
        """Waits for a purge that runs and writes what it held."""
        self._finish_purge()
        if self._worker is not None:
            self._worker.shutdown()
        with contextlib.suppress(sqlite3.Error):
            self._db.close()

    # --- writing --------------------------------------------------------------------------------

    def add_message(self, entry: HistoryEntry) -> None:
        self._write(
            "INSERT INTO message (at, direction, kind, contact_key, channel_index, name, text, "
            "state, hops, snr_db) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.at,
                entry.direction,
                entry.kind,
                entry.contact_key,
                entry.channel_index,
                entry.name,
                entry.text,
                entry.state,
                entry.hops,
                entry.snr_db,
            ),
        )

    def add_position(self, key: str, fix: Fix) -> None:
        position = fix.position
        self._write(
            "INSERT INTO position (contact_key, at, latitude, longitude, altitude_m, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, fix.at, position.latitude, position.longitude, fix.altitude_m, fix.source),
        )

    def add_readings(self, key: str, at: float, values: Mapping[str, float]) -> None:
        """The values of one telemetry answer (`battery_v`, `mcu_temperature_c`, the reading
        keys), one row each, all or none."""
        rows = [(key, at, quantity, value) for quantity, value in values.items()]
        self._write_all(
            [("INSERT INTO reading (contact_key, at, quantity, value) VALUES (?, ?, ?, ?)", rows)]
        )

    def forget_telemetry(self, key: str) -> int | None:
        """Delete the contact's readings and its positions from telemetry, all or none; its
        positions from adverts stay. The rows deleted, or None without a working history. A
        purge that runs is waited for (rare: the household excludes a contact and forgets)."""
        self._finish_purge()
        return self._write_all(
            [
                ("DELETE FROM reading WHERE contact_key = ?", [(key,)]),
                ("DELETE FROM position WHERE contact_key = ? AND source = 'telemetry'", [(key,)]),
            ]
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
            (
                key,
                name,
                node_type,
                heard.at,
                heard.at,
                heard.how,
                signal.hops,
                signal.snr_db,
                signal.rssi_dbm,
                last_advert,
            ),
        )

    def purge(self, now: float, nodes: Iterable[str] | None = None) -> None:
        """Drop what is older than the retention, and rows beyond the caps (the oldest first;
        never a contact's newest position or answer, which only its age removes), in one
        transaction. `nodes`: the keys of the nodes the gateway keeps, its contacts and heard
        nodes; the rows of all others go, so that a flood of adverts with invented keys leaves
        no more rows than the gateway keeps in memory (SEC-010). None before the first table
        read of a run, while the rows are the heard nodes still to restore: only their age."""
        self._finish_purge()
        self._write_all(self._purge_steps(now, nodes))

    def purge_in_thread(self, now: float, nodes: Iterable[str] | None = None) -> None:
        """purge() in a thread on a connection of its own while the event loop goes on
        (CODE-002): its cost grows with the tables and the disk. The writes that come meanwhile
        are held in memory and follow it, in their order, in one transaction. Inline without a
        running loop (the command line, tests) or without the file's path; nothing while the
        last one still runs."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or self._path is None or self.broken:
            self.purge(now, nodes)
            return
        if self._purging is not None:
            return
        if self._worker is None:
            self._worker = concurrent.futures.ThreadPoolExecutor(1, "history-purge")
        future = self._worker.submit(_purge_apart, self._path, self._purge_steps(now, nodes))
        self._purging = future

        def done(_: concurrent.futures.Future[None]) -> None:  # in the purge's thread
            with contextlib.suppress(RuntimeError):  # the loop has closed: close() finished it
                loop.call_soon_threadsafe(self._purged, future)

        future.add_done_callback(done)

    def _purge_steps(self, now: float, nodes: Iterable[str] | None) -> Steps:
        r = self._retention
        steps: Steps = [
            ("DELETE FROM message WHERE at < ?", [(now - r.messages_days * DAY_S,)]),
            (_MESSAGES_BEYOND_CAP, [(MAX_MESSAGES,)]),
            ("DELETE FROM position WHERE at < ?", [(now - r.positions_days * DAY_S,)]),
            (_POSITIONS_BEYOND_CAP, [(MAX_POSITIONS,)]),
            ("DELETE FROM node WHERE heard_at < ?", [(now - r.nodes_days * DAY_S,)]),
            ("DELETE FROM reading WHERE at < ?", [(now - r.readings_days * DAY_S,)]),
            (_READINGS_BEYOND_CAP, [(MAX_READINGS,)]),
        ]
        if nodes is not None:
            steps.append((_NODES_FORGOTTEN, [(json.dumps(sorted(set(nodes))),)]))
        return steps

    def _purged(self, future: concurrent.futures.Future[None]) -> None:
        """The purge's thread has finished: its error breaks the history as one of a write
        does; the writes it held follow."""
        if future is not self._purging:
            return  # finished by close() or forget_telemetry()
        self._purging = None
        error = future.exception()
        if isinstance(error, sqlite3.Error):
            self._broke(error)
        elif error is not None:
            log.error("history: the purge failed", exc_info=error)
        held, self._held = self._held, []
        if held:
            self._write_all([step for steps in held for step in steps])

    def _finish_purge(self) -> None:
        """Wait for a purge that runs in its thread, and write what it held."""
        future = self._purging
        if future is not None:
            concurrent.futures.wait([future])
            self._purged(future)

    def _hold(self, steps: Steps) -> None:
        """A write for after the purge that runs."""
        if len(self._held) < MAX_HELD:
            self._held.append(steps)
            return
        if not self._dropped:
            log.warning(
                "history: more than %d writes while the purge ran; later ones lost", MAX_HELD
            )
        self._dropped += 1

    def _write(self, sql: str, args: tuple[Any, ...]) -> None:
        if self.broken:
            return
        if self._purging is not None:
            self._hold([(sql, [args])])
            return
        try:
            self._db.execute(sql, args)
        except sqlite3.Error as exc:
            self._broke(exc)

    def _write_all(self, steps: Iterable[tuple[str, Iterable[tuple[Any, ...]]]]) -> int | None:
        """Statements with their rows of arguments in one transaction; the rows they changed,
        or None when the history is broken (a failure breaks it, as in _write) or a purge
        runs (they follow it)."""
        if self.broken:
            return None
        if self._purging is not None:
            self._hold([(sql, list(rows)) for sql, rows in steps])
            return None
        changed = 0
        try:
            with _transaction(self._db):
                for sql, rows in steps:
                    changed += self._db.executemany(sql, rows).rowcount
        except sqlite3.Error as exc:
            self._broke(exc)
            return None
        return changed

    def _broke(self, exc: sqlite3.Error) -> None:
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

    def last_readings(self) -> dict[str, Readings]:
        """The newest answer of every contact that has one, its values in the order written. A
        quantity this version does not name (a later version's) is left out."""
        rows = self._read(
            "SELECT r.contact_key, r.at, r.quantity, r.value FROM reading r JOIN "
            "(SELECT contact_key, MAX(at) AS at FROM reading GROUP BY contact_key) n "
            "ON r.contact_key = n.contact_key AND r.at = n.at ORDER BY r.contact_key, r.id",
            (),
        )
        answers: dict[str, tuple[float, dict[str, float]]] = {}
        for key, at, quantity, value in rows:
            answers.setdefault(key, (at, {}))[1][quantity] = value
        return {key: Readings(at, _named(values)) for key, (at, values) in answers.items()}

    def _read(self, sql: str, args: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.broken:
            return []
        try:
            return list(self._db.execute(sql, args))
        except sqlite3.Error as exc:
            self.broken = True
            log.warning("history cannot be read (%s): running without it until restart", exc)
            return []


def _named(values: Mapping[str, float]) -> lpp.Named:
    """An answer's values as the documents name them, from its rows."""
    readings = {q: value for q, value in values.items() if lpp.component_of(q) is not None}
    return lpp.Named(values.get(lpp.BATTERY_V), values.get(lpp.MCU_TEMPERATURE_C), readings)


def _purge_apart(path: Path, steps: Steps) -> None:
    """The purge on a connection of its own, in one transaction (the purge's thread)."""
    db = sqlite3.connect(path, timeout=PURGE_BUSY_S, isolation_level=None)
    try:
        with _transaction(db):
            for sql, rows in steps:
                db.executemany(sql, rows)
    finally:
        db.close()


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
        version = SCHEMA_VERSION
    while version < SCHEMA_VERSION:
        with _transaction(db):
            for statement in _MIGRATIONS[version]:
                db.execute(statement)
            version += 1
            db.execute(f"PRAGMA user_version = {version}")
    with _transaction(db):
        for statement in _ADDED:
            db.execute(statement)


@contextlib.contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
