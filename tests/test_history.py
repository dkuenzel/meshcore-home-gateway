"""The local history in SQLite (history.py, ADR 0016)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshcore_home_gateway import history as history_module
from meshcore_home_gateway.core.lpp import Named
from meshcore_home_gateway.core.mesh import Fix, Heard, Readings, Signal
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.views import HistoryEntry

T = 1_800_000_000.0
DAY = 86_400.0
KEY = "a1" * 32
BOB = "b2" * 32


OPENED: list[History] = []


@pytest.fixture(autouse=True)
def close_histories() -> Iterator[None]:
    yield
    while OPENED:
        OPENED.pop().close()


def opened(path: Path, retention: Retention | None = None) -> History:
    history = History.open(path, retention)
    assert history is not None
    OPENED.append(history)
    return history


def test_messages_survive_a_restart_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = opened(path)
    entries = [
        HistoryEntry(
            T, "in", "contact", "Hello", contact_key=KEY, name="Alice", hops=2, snr_db=6.25
        ),
        HistoryEntry(
            T + 1, "out", "channel", "Gate open", channel_index=0, name="alerts", state="sent"
        ),
    ]
    for entry in entries:
        history.add_message(entry)
    history.close()
    again = opened(path)
    assert again.recent_messages(10) == entries[::-1]
    assert again.recent_messages(1) == [entries[1]]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert again._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_newest_messages_shown_are_read_past_the_others_page_by_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#103: the observer asks for the newest messages of the channels it publishes, which may
    lie behind any number of rows of other channels."""
    monkeypatch.setattr(history_module, "READ_PAGE", 2)
    history = opened(tmp_path / "history.sqlite3")
    for n in range(7):
        history.add_message(HistoryEntry(T + n, "in", "channel", f"{n}", channel_index=n % 3))
    texts = [m.text for m in history.recent_messages(2, lambda m: m.channel_index == 0)]
    assert texts == ["6", "3"]
    texts = [m.text for m in history.recent_messages(5, lambda m: m.channel_index == 0)]
    assert texts == ["6", "3", "0"]  # to the end of the table
    assert [m.text for m in history.recent_messages(3)] == ["6", "5", "4"]


def test_the_keys_of_2_1_survive_a_restart(tmp_path: Path) -> None:
    """The target, the delivery id, the acceptance time (unix seconds, as `at`), whether the
    acknowledgement was suspect, and why a message failed (design §6.6)."""
    path = tmp_path / "history.sqlite3"
    history = opened(path)
    entries = [
        HistoryEntry(T, "in", "channel", "On my way", channel_index=1, target="family"),
        HistoryEntry(
            T + 10,
            "out",
            "contact",
            "Back soon",
            contact_key=KEY,
            state="confirmed",
            id="1a2b3c4d",
            queued_at=T + 2.5,
            suspect=True,
        ),
        HistoryEntry(
            T + 20,
            "out",
            "contact",
            "Later",
            contact_key=BOB,
            state="confirmed",
            target="bob",
            id="5e6f7a8b",
            queued_at=T + 19.0,
            suspect=False,
        ),
        HistoryEntry(
            T + 30,
            "out",
            "channel",
            "Gate open",
            channel_index=0,
            state="failed",
            target="alerts",
            id="9c0d1e2f",
            queued_at=T + 29.0,
            reason="channel_not_configured",
        ),
    ]
    for entry in entries:
        history.add_message(entry)
    history.close()
    again = opened(path)
    assert again.recent_messages(10) == entries[::-1]
    assert [type(m.suspect) for m in again.recent_messages(10)] == [
        type(None),
        bool,
        bool,
        type(None),
    ]
    stored = again._db.execute(
        "SELECT typeof(queued_at), typeof(suspect) FROM message WHERE delivery_id = '1a2b3c4d'"
    ).fetchone()
    assert stored == ("real", "integer")


def test_whether_the_gateway_added_the_delivery_code_survives_a_restart(tmp_path: Path) -> None:
    """#142: `nonce`, true or false on a sent message, null on a received one, kept as a truth
    value as `suspect` is."""
    path = tmp_path / "history.sqlite3"
    history = opened(path)
    entries = [
        HistoryEntry(T, "in", "contact", "Hi #0a1b2c3d", contact_key=KEY),
        HistoryEntry(
            T + 1, "out", "contact", "Door open #1a2b3c4d", KEY, state="confirmed", nonce=True
        ),
        HistoryEntry(
            T + 2, "out", "contact", "Code ends #0a1b2c3d", KEY, state="confirmed", nonce=False
        ),
        HistoryEntry(T + 3, "out", "channel", "Gate open", channel_index=0, nonce=False),
    ]
    for entry in entries:
        history.add_message(entry)
    history.close()
    again = opened(path)
    assert again.recent_messages(10) == entries[::-1]
    assert [m.nonce for m in again.recent_messages(10)] == [False, False, True, None]
    assert [type(m.nonce) for m in again.recent_messages(10)][:3] == [bool] * 3
    stored = again._db.execute("SELECT typeof(nonce) FROM message ORDER BY at").fetchall()
    assert stored == [("null",), ("integer",), ("integer",), ("integer",)]


# the message table and the statements of 2.0 (history.py at 2.0.0), which a downgrade runs
MESSAGE_2_0 = (
    "CREATE TABLE message (id INTEGER PRIMARY KEY, at REAL NOT NULL, direction TEXT NOT NULL, "
    "kind TEXT NOT NULL, contact_key TEXT, channel_index INTEGER, name TEXT, text TEXT NOT NULL, "
    "state TEXT, hops INTEGER, snr_db REAL)"
)
INSERT_2_0 = (
    "INSERT INTO message (at, direction, kind, contact_key, channel_index, name, text, "
    "state, hops, snr_db) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
SELECT_2_0 = (
    "SELECT at, direction, kind, text, contact_key, channel_index, name, state, hops, "
    "snr_db FROM message ORDER BY at DESC, id DESC LIMIT ?"
)


def columns(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [row[1] for row in db.execute("PRAGMA table_info(message)")]
    finally:
        db.close()


def test_a_2_0_history_gets_the_new_columns_and_2_0_still_reads_it(tmp_path: Path) -> None:
    """The columns are added after a look at the table, without a new schema version: 2.0 names
    its columns in every statement, so a downgrade reads and writes the file as before."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute(MESSAGE_2_0)
    db.execute(INSERT_2_0, (T, "out", "contact", KEY, None, "Alice", "Hi", "confirmed", None, None))
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()
    history = opened(path)
    (kept,) = history.recent_messages(5)
    assert kept == HistoryEntry(T, "out", "contact", "Hi", KEY, name="Alice", state="confirmed")
    history.add_message(
        HistoryEntry(
            T + 1,
            "out",
            "channel",
            "Gate open",
            channel_index=0,
            state="sent",
            target="alerts",
            id="1a2b3c4d",
            queued_at=T,
        )
    )
    history.close()
    added = ["target", "delivery_id", "queued_at", "suspect", "reason", "nonce"]
    assert columns(path)[-6:] == added
    version, objects = schema(path)
    assert version == 2
    opened(path).close()  # a second start changes nothing
    assert schema(path) == (version, objects)

    db = sqlite3.connect(path)  # the downgrade: 2.0's statements on this file
    db.execute(INSERT_2_0, (T + 2, "in", "channel", None, 0, "Carol", "Back", None, 1, 6.25))
    db.commit()
    rows = list(db.execute(SELECT_2_0, (5,)))
    db.close()
    assert [(row[0], row[3]) for row in rows] == [(T + 2, "Back"), (T + 1, "Gate open"), (T, "Hi")]
    assert [m.target for m in opened(path).recent_messages(5)] == [None, "alerts", None]


# the statements of 2.1 (history.py at 2.1.0): its columns, without `nonce` (#142)
INSERT_2_1 = (
    "INSERT INTO message (at, direction, kind, contact_key, channel_index, name, text, "
    "state, hops, snr_db, target, delivery_id, queued_at, suspect, reason) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
SELECT_2_1 = (
    "SELECT at, direction, kind, text, contact_key, channel_index, name, state, hops, snr_db, "
    "target, delivery_id, queued_at, suspect, reason FROM message "
    "ORDER BY at DESC, id DESC LIMIT ? OFFSET ?"
)
COLUMNS_2_1 = (  # what 2.1 added to the table of 2.0
    "ALTER TABLE message ADD COLUMN target TEXT",
    "ALTER TABLE message ADD COLUMN delivery_id TEXT",
    "ALTER TABLE message ADD COLUMN queued_at REAL",
    "ALTER TABLE message ADD COLUMN suspect INTEGER",
    "ALTER TABLE message ADD COLUMN reason TEXT",
)


def test_a_2_1_history_gets_the_nonce_column_and_2_1_still_reads_it(tmp_path: Path) -> None:
    """#142: `nonce` is added as the columns of 2.1 were, without a new schema version. A
    message 2.1 kept says nothing about the delivery code: null, not guessed from its text or
    the setting (no backfill). 2.1 names its columns, so after a downgrade it reads and writes
    the file as before, and leaves `nonce` empty."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute(MESSAGE_2_0)
    for statement in COLUMNS_2_1:
        db.execute(statement)
    row = (T, "out", "contact", KEY, None, "Alice", "Hi #1a2b3c4d", "confirmed", None, None)
    db.execute(INSERT_2_1, (*row, "alice", "5e6f7a8b", T - 2, 0, None))
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()
    history = opened(path)
    (kept,) = history.recent_messages(5)
    assert (kept.text, kept.id, kept.suspect, kept.nonce) == (
        "Hi #1a2b3c4d",
        "5e6f7a8b",
        False,
        None,
    )
    history.add_message(
        HistoryEntry(T + 1, "out", "contact", "Back #0a1b2c3d", KEY, state="confirmed", nonce=True)
    )
    history.close()
    assert columns(path)[-2:] == ["reason", "nonce"]
    version, objects = schema(path)
    assert version == 2
    opened(path).close()  # a second start changes nothing
    assert schema(path) == (version, objects)

    db = sqlite3.connect(path)  # the downgrade: 2.1's statements on this file
    db.execute(
        INSERT_2_1, (T + 2, "in", "channel", None, 0, "Carol", "Back", None, 1, 6.25, *[None] * 5)
    )
    db.commit()
    rows = list(db.execute(SELECT_2_1, (5, 0)))
    db.close()
    assert [(r[0], r[3]) for r in rows] == [
        (T + 2, "Back"),
        (T + 1, "Back #0a1b2c3d"),
        (T, "Hi #1a2b3c4d"),
    ]
    assert [m.nonce for m in opened(path).recent_messages(5)] == [None, True, None]


def test_positions_traces_and_the_newest_fix(tmp_path: Path) -> None:
    history = opened(tmp_path / "h.sqlite3")
    fixes = [Fix(Position(53.55 + i / 100, 9.99), T + i * 60, "telemetry", 10.0) for i in range(3)]
    for fix in fixes:
        history.add_position(KEY, fix)
    history.add_position("b2" * 32, Fix(Position(1, 2), T, "advert"))
    assert history.last_fixes() == {KEY: fixes[-1], "b2" * 32: Fix(Position(1, 2), T, "advert")}
    assert history.trace(KEY, T + 30) == fixes[1:]


def test_nodes_keep_their_first_and_newest_sighting(tmp_path: Path) -> None:
    history = opened(tmp_path / "h.sqlite3")
    history.node_heard(KEY, "Alice", 1, Heard(T, "advert", Signal(1, 2.0, -90)))
    history.node_heard(KEY, "Alice B", 1, Heard(T + 60, "message", Signal(None, 5.0)))
    history.node_heard(KEY, "Old", 1, Heard(T + 30, "advert"))  # older: changes nothing
    (row,) = history.nodes()
    assert (row.key, row.name, row.node_type, row.first_heard) == (KEY, "Alice B", 1, T)
    assert row.heard == Heard(T + 60, "message", Signal(None, 5.0, None))


def test_retention_by_age_and_by_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_module, "MAX_MESSAGES", 3)
    history = opened(
        tmp_path / "h.sqlite3", Retention(messages_days=2, positions_days=1, nodes_days=1)
    )
    for day in range(5):
        history.add_message(HistoryEntry(T + day * DAY, "in", "channel", f"day {day}"))
    history.add_position(KEY, Fix(Position(1, 1), T, "advert"))
    history.add_position(KEY, Fix(Position(2, 2), T + 4 * DAY, "advert"))
    history.node_heard(KEY, "Alice", 1, Heard(T, "advert"))
    history.purge(T + 4 * DAY + 1)
    assert [m.text for m in history.recent_messages(10)] == ["day 4", "day 3"]
    for day in range(5, 10):
        history.add_message(HistoryEntry(T + 4 * DAY + day, "in", "channel", f"late {day}"))
    history.purge(T + 4 * DAY + 20)
    assert [m.text for m in history.recent_messages(10)] == ["late 9", "late 8", "late 7"]
    assert list(history.last_fixes().values()) == [Fix(Position(2, 2), T + 4 * DAY, "advert")]
    assert history.nodes() == []


def test_the_node_table_keeps_the_nodes_the_gateway_keeps(tmp_path: Path) -> None:
    """A flood of adverts with invented keys writes a row per key; the purge leaves the rows of
    the contacts and heard nodes the gateway keeps (SEC-010). Before the first table read of a
    run the rows are the heard nodes still to restore: only their age counts."""
    history = opened(tmp_path / "h.sqlite3")
    assert Retention().nodes_days == 7  # as the heard nodes in memory (ADR 0016)
    for i in range(200):
        history.node_heard(f"{i:04x}" * 16, f"Spam{i}", 1, Heard(T + i, "advert"))
    history.node_heard(KEY, "Alice", 1, Heard(T, "advert"))
    history.purge(T + 600)  # not told which it keeps
    assert len(history.nodes()) == 201
    history.purge(T + 600, nodes=[KEY, "d4" * 32])  # a key without a row changes nothing
    assert [row.key for row in history.nodes()] == [KEY]
    history.purge(T + 600, nodes=[])
    assert history.nodes() == []


def test_the_position_cap_keeps_the_newest_position_of_every_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap bounds the file, the days bound the time: a contact at rest keeps its one
    position however many another one adds."""
    monkeypatch.setattr(history_module, "MAX_POSITIONS", 3)
    history = opened(tmp_path / "h.sqlite3")
    resting = Fix(Position(1, 1), T, "telemetry")
    history.add_position("b2" * 32, resting)
    moving = [Fix(Position(53 + i / 100, 9), T + 60 * i, "telemetry") for i in range(1, 6)]
    for fix in moving:
        history.add_position(KEY, fix)
    history.purge(T + 600)
    assert history.last_fixes() == {KEY: moving[-1], "b2" * 32: resting}
    assert history.trace(KEY, T) == moving[-3:]


def test_a_file_that_is_no_database_is_set_aside(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"this is not a database" * 100)
    (tmp_path / "history.sqlite3-wal").write_bytes(b"stale log")
    caplog.set_level(logging.WARNING)
    history = opened(path)
    assert history.recent_messages(5) == []
    assert (tmp_path / "history.sqlite3.corrupt").read_bytes().startswith(b"this is not")
    history.add_message(HistoryEntry(T, "in", "channel", "fresh"))
    assert [m.text for m in history.recent_messages(5)] == ["fresh"]  # the stale log is gone
    assert "moved to" in caplog.text


def test_a_newer_schema_is_left_alone(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    caplog.set_level(logging.WARNING)
    assert History.open(path) is None
    assert path.exists()
    assert not (tmp_path / "history.sqlite3.corrupt").exists()
    assert "schema version 99 is newer than 2" in caplog.text


def test_a_history_that_cannot_be_opened_is_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    caplog.set_level(logging.WARNING)
    assert History.open(blocker / "history.sqlite3") is None  # its directory is a file
    assert "running without" in caplog.text


def test_write_and_read_errors_switch_the_history_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    history = opened(tmp_path / "h.sqlite3")
    history._db.execute("DROP TABLE message")
    caplog.set_level(logging.WARNING)
    history.add_message(HistoryEntry(T, "in", "channel", "lost"))
    assert history.broken
    assert history.recent_messages(5) == []  # no reads either
    history.add_position(KEY, Fix(Position(1, 1), T, "advert"))  # ignored, no exception
    assert caplog.text.count("history cannot be written") == 1
    history.close()
    history.close()  # twice is harmless

    other = opened(tmp_path / "other.sqlite3")
    other._db.execute("DROP TABLE node")
    assert other.nodes() == []
    assert other.broken
    assert "history cannot be read" in caplog.text


def test_a_version_1_history_gets_the_advert_time_of_its_nodes(tmp_path: Path) -> None:
    """Schema 1 (before contact management) had no advert time; its rows read as 0."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE node (key TEXT PRIMARY KEY, name TEXT NOT NULL, type INTEGER NOT NULL, "
        "first_heard REAL NOT NULL, heard_at REAL NOT NULL, how TEXT NOT NULL, hops INTEGER, "
        "snr_db REAL, rssi_dbm INTEGER)"
    )
    for table in ("message (id INTEGER PRIMARY KEY)", "position (id INTEGER PRIMARY KEY)"):
        db.execute(f"CREATE TABLE {table}")
    db.execute("INSERT INTO node VALUES ('c3', 'Carol', 2, 1.0, 2.0, 'advert', NULL, NULL, NULL)")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    history = History.open(path)
    assert history is not None
    ((row,),) = [history.nodes()]
    assert (row.name, row.last_advert) == ("Carol", 0)
    history.node_heard("c3", "Carol", 2, Heard(3.0, "advert"), 1_800_000_000)
    assert history.nodes()[0].last_advert == 1_800_000_000
    history.close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    db.close()


# --- readings -------------------------------------------------------------------------------------


def schema(path: Path) -> tuple[int, list[tuple[str, str, str]]]:
    """The file's version and what it holds, read without the program."""
    db = sqlite3.connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        objects = list(db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name"))
    finally:
        db.close()
    return version, objects


def test_a_version_2_file_gets_the_reading_table_and_keeps_its_version(tmp_path: Path) -> None:
    """Earlier versions of schema 2 lack the table; they ignore it, so it needs no new version
    and a downgrade keeps the whole history."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(history_module._SCHEMA + "PRAGMA user_version = 2;")
    db.execute(
        "INSERT INTO message (at, direction, kind, text) VALUES (?, 'in', 'channel', 'kept')", (T,)
    )
    db.commit()
    db.close()
    assert "reading" not in {name for _, name, _ in schema(path)[1]}
    history = opened(path)
    assert [m.text for m in history.recent_messages(5)] == ["kept"]
    history.add_readings(KEY, T, {"battery_v": 4.12})
    history.close()
    version, objects = schema(path)
    assert version == 2
    assert {("table", "reading"), ("index", "reading_contact_at")} <= {
        (kind, name) for kind, name, _ in objects
    }
    opened(path).close()  # a second start changes nothing
    assert schema(path) == (version, objects)


def test_a_new_file_is_version_2_with_the_reading_table(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    opened(path).close()
    version, objects = schema(path)
    assert version == 2
    assert {"message", "position", "node", "reading", "reading_contact_at"} <= {
        name for _, name, _ in objects
    }


def test_a_newer_schema_gets_no_reading_table(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 3")
    db.close()
    assert History.open(path) is None
    assert schema(path) == (3, [])


def test_the_newest_answer_of_each_contact_comes_back_in_the_order_written(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = opened(path)
    history.add_readings(KEY, T, {"battery_v": 4.1, "ch2_temperature_c": 20.0})
    history.add_readings(
        KEY,
        T + 900,
        {
            "battery_v": 4.12,
            "ch2_temperature_c": 21.3,
            "ch2_humidity_percent": 55.5,
            "ch1_temperature_c": 19.5,
            "mcu_temperature_c": 23.4,
        },
    )
    history.add_readings(BOB, T + 60, {"ch1_luminosity": 50.0, "ch3_weather": 1.0})
    history.close()
    assert opened(path).last_readings() == {
        KEY: Readings(
            T + 900,
            Named(
                4.12,
                23.4,
                {
                    "ch2_temperature_c": 21.3,
                    "ch2_humidity_percent": 55.5,
                    "ch1_temperature_c": 19.5,
                },
            ),
        ),
        BOB: Readings(T + 60, Named(None, None, {"ch1_luminosity": 50.0})),  # a later version's
    }
    readings = opened(path).last_readings()[KEY].named.readings
    assert list(readings) == ["ch2_temperature_c", "ch2_humidity_percent", "ch1_temperature_c"]


def test_the_values_of_an_answer_are_written_in_one_transaction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    history = opened(tmp_path / "h.sqlite3")
    caplog.set_level(logging.WARNING)
    history.add_readings(KEY, T, {"battery_v": 4.12, "ch2_temperature_c": float("nan")})
    assert history.broken
    assert "history cannot be written" in caplog.text
    assert history._db.execute("SELECT COUNT(*) FROM reading").fetchone()[0] == 0
    history.add_readings(KEY, T, {"battery_v": 4.12})  # ignored, no exception
    assert history._db.execute("SELECT COUNT(*) FROM reading").fetchone()[0] == 0
    assert history.last_readings() == {}


def test_readings_go_after_their_days(tmp_path: Path) -> None:
    history = opened(tmp_path / "h.sqlite3", Retention(readings_days=2))
    history.add_readings(KEY, T, {"battery_v": 4.0})
    history.add_readings(BOB, T + DAY, {"battery_v": 3.9})
    history.purge(T + 2 * DAY + 1)
    assert history.last_readings() == {BOB: Readings(T + DAY, Named(3.9, None, {}))}
    assert Retention().readings_days == 30.0


def test_the_reading_cap_keeps_the_newest_answer_of_every_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap bounds the file: a contact at rest keeps its one answer however many another one
    sends; the cap may leave part of an older answer."""
    monkeypatch.setattr(history_module, "MAX_READINGS", 3)
    history = opened(tmp_path / "h.sqlite3")
    history.add_readings(BOB, T, {"battery_v": 3.9, "mcu_temperature_c": 20.0})
    for i in range(1, 6):
        history.add_readings(KEY, T + 60 * i, {"battery_v": 4.0 + i / 100, "ch2_power_w": i})
    history.purge(T + 600)
    assert history.last_readings() == {
        BOB: Readings(T, Named(3.9, 20.0, {})),
        KEY: Readings(T + 300, Named(4.05, None, {"ch2_power_w": 5.0})),
    }
    rows = history._db.execute("SELECT contact_key, at, quantity FROM reading ORDER BY id")
    assert list(rows) == [
        (BOB, T, "battery_v"),
        (BOB, T, "mcu_temperature_c"),
        (KEY, T + 240, "ch2_power_w"),
        (KEY, T + 300, "battery_v"),
        (KEY, T + 300, "ch2_power_w"),
    ]


def test_forgetting_a_contact_deletes_its_readings_and_telemetry_positions(tmp_path: Path) -> None:
    history = opened(tmp_path / "h.sqlite3")
    advert = Fix(Position(1, 1), T, "advert")
    for key in (KEY, BOB):
        history.add_readings(key, T, {"battery_v": 4.1, "ch2_temperature_c": 20.0})
        history.add_readings(key, T + 900, {"battery_v": 4.0})
        history.add_position(key, advert)
        history.add_position(key, Fix(Position(2, 2), T + 60, "telemetry"))
    assert history.forget_telemetry(KEY) == 4  # three readings, one position
    assert set(history.last_readings()) == {BOB}
    assert history.trace(KEY, 0) == [advert]
    assert len(history.trace(BOB, 0)) == 2
    assert history.forget_telemetry(KEY) == 0


def test_forgetting_is_one_transaction_and_none_without_a_working_history(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    history = opened(tmp_path / "h.sqlite3")
    history.add_readings(KEY, T, {"battery_v": 4.1})
    history._db.execute("DROP TABLE position")
    caplog.set_level(logging.WARNING)
    assert history.forget_telemetry(KEY) is None
    assert history.broken
    assert history._db.execute("SELECT COUNT(*) FROM reading").fetchone()[0] == 1  # rolled back
    assert history.forget_telemetry(KEY) is None
    assert caplog.text.count("history cannot be written") == 1


# --- the purge in its thread (CODE-002) ----------------------------------------------------------


def slow_purge(monkeypatch: pytest.MonkeyPatch, seconds: float) -> list[str]:
    """The purge's thread takes `seconds` longer; the names of the threads it ran in."""
    threads: list[str] = []
    real = history_module._purge_apart

    def slow(path: Path, steps: history_module.Steps) -> None:
        threads.append(threading.current_thread().name)
        time.sleep(seconds)
        real(path, steps)

    monkeypatch.setattr(history_module, "_purge_apart", slow)
    return threads


async def purged(history: History) -> None:
    """Until the purge that runs has finished and the writes it held are written: its thread
    schedules that before the result of the future."""
    if (future := history._purging) is not None:
        await asyncio.wait([asyncio.wrap_future(future)])  # also one that failed
    assert history._purging is None


def test_the_purge_runs_in_a_thread_and_the_writes_meanwhile_follow_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop goes on while the purge runs: a timer fires, and what is written meanwhile waits
    in memory and follows the purge in its order, also a row the purge would have dropped."""
    history = opened(tmp_path / "h.sqlite3", Retention(messages_days=1))
    history.add_message(HistoryEntry(T, "in", "channel", "old"))
    threads = slow_purge(monkeypatch, 0.3)

    async def main() -> list[str]:
        fired: list[str] = []
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, fired.append, "timer")
        history.purge_in_thread(T + 2 * DAY)
        history.add_message(HistoryEntry(T + 1, "in", "channel", "meanwhile, old too"))
        history.add_message(HistoryEntry(T + 2 * DAY, "in", "channel", "meanwhile, new"))
        await asyncio.sleep(0.1)
        fired.append("purging" if history._purging is not None else "done")
        history.purge_in_thread(T + 2 * DAY)  # one at a time: this one is left out
        await purged(history)
        return fired

    assert asyncio.run(main()) == ["timer", "purging"]
    assert threads == ["history-purge_0"]
    texts = [m.text for m in history.recent_messages(10)]
    assert texts == ["meanwhile, new", "meanwhile, old too"]  # "old" purged, the rest after


def test_without_an_event_loop_the_purge_runs_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = opened(tmp_path / "h.sqlite3", Retention(messages_days=1))
    history.add_message(HistoryEntry(T, "in", "channel", "old"))
    threads = slow_purge(monkeypatch, 0.0)
    history.purge_in_thread(T + 2 * DAY)
    assert (history.recent_messages(10), threads) == ([], [])


def test_forgetting_and_closing_wait_for_the_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forgetting tells how many rows went: it waits for a purge that runs, then the writes it
    held go first. Closing waits too, and writes what was held."""
    path = tmp_path / "h.sqlite3"
    history = opened(path)
    history.add_readings(KEY, T, {"battery_v": 4.1})
    slow_purge(monkeypatch, 0.2)

    async def main() -> int | None:
        history.purge_in_thread(T)
        history.add_readings(KEY, T + 60, {"battery_v": 4.0})  # held
        rows = history.forget_telemetry(KEY)
        history.purge_in_thread(T)
        history.add_message(HistoryEntry(T, "in", "channel", "held at the stop"))
        history.close()
        return rows

    assert asyncio.run(main()) == 2  # the held reading was written before
    again = opened(path)
    assert [m.text for m in again.recent_messages(10)] == ["held at the stop"]


def test_a_purge_that_fails_in_its_thread_breaks_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    history = opened(tmp_path / "h.sqlite3")

    def failing(_path: Path, _steps: history_module.Steps) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(history_module, "_purge_apart", failing)

    async def main() -> None:
        history.purge_in_thread(T)
        history.add_message(HistoryEntry(T, "in", "channel", "held"))
        await purged(history)

    caplog.set_level(logging.WARNING)
    asyncio.run(main())
    assert history.broken
    assert "history cannot be written (disk I/O error)" in caplog.text
    assert history._db.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 0
