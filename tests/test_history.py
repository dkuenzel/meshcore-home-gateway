"""The local history in SQLite (history.py, ADR 0016)."""

from __future__ import annotations

import logging
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshcore_home_gateway import history as history_module
from meshcore_home_gateway.core.mesh import Fix, Heard, Signal
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.views import HistoryEntry

T = 1_800_000_000.0
DAY = 86_400.0
KEY = "a1" * 32


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
