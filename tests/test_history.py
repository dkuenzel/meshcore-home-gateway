"""The local history in SQLite (history.py, ADR 0016)."""

from __future__ import annotations

import logging
import sqlite3
import stat
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
        HistoryEntry(T, "in", "contact", "Hello", contact_key=KEY, name="Alice", hops=2,
                     snr_db=6.25),
        HistoryEntry(T + 1, "out", "channel", "Gate open", channel_index=0, name="alerts",
                     state="sent"),
    ]  # fmt: skip
    for entry in entries:
        history.add_message(entry)
    history.close()
    again = opened(path)
    assert again.recent_messages(10) == entries[::-1]
    assert again.recent_messages(1) == [entries[1]]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert again._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


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
    history = opened(tmp_path / "h.sqlite3", Retention(messages_days=2, positions_days=1,
                                                      nodes_days=1))  # fmt: skip
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
        KEY, T + 900,
        {"battery_v": 4.12, "ch2_temperature_c": 21.3, "ch2_humidity_percent": 55.5,
         "ch1_temperature_c": 19.5, "mcu_temperature_c": 23.4},
    )  # fmt: skip
    history.add_readings(BOB, T + 60, {"ch1_luminosity": 50.0, "ch3_weather": 1.0})
    history.close()
    assert opened(path).last_readings() == {
        KEY: Readings(T + 900, Named(4.12, 23.4, {
            "ch2_temperature_c": 21.3, "ch2_humidity_percent": 55.5, "ch1_temperature_c": 19.5,
        })),
        BOB: Readings(T + 60, Named(None, None, {"ch1_luminosity": 50.0})),  # a later version's
    }  # fmt: skip
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
        (BOB, T, "battery_v"), (BOB, T, "mcu_temperature_c"), (KEY, T + 240, "ch2_power_w"),
        (KEY, T + 300, "battery_v"), (KEY, T + 300, "ch2_power_w"),
    ]  # fmt: skip


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
