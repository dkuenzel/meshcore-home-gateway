"""Persistent state: atomic JSON file and the ledger of accepted command timestamps."""

import json
from pathlib import Path

from meshcore_bus.state import CommandLedger, JsonStateFile


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    assert JsonStateFile(tmp_path / "state.json").load() == {}


def test_save_and_load_roundtrip_with_private_permissions(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"a": 1})
    assert store.load() == {"a": 1}
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_file_is_kept_aside_and_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert JsonStateFile(path).load() == {}
    assert (tmp_path / "state.json.corrupt").read_text(encoding="utf-8") == "{not json"


def test_ledger_remembers_the_newest_timestamp_per_sender(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ledger = CommandLedger(JsonStateFile(path))
    assert ledger.last("aa" * 32) is None
    ledger.record("aa" * 32, 100)
    ledger.record("aa" * 32, 90)  # never goes back
    assert ledger.last("aa" * 32) == 100
    reloaded = CommandLedger(JsonStateFile(path))
    assert reloaded.last("aa" * 32) == 100
    assert json.loads(path.read_text(encoding="utf-8")) == {"command_timestamps": {"aa" * 32: 100}}


def test_ledger_ignores_malformed_content(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"command_timestamps": {"x": "not a number"}}', encoding="utf-8")
    assert CommandLedger(JsonStateFile(path)).last("x") is None
