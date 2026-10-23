"""Persistent state: atomic JSON file and the ledger of accepted command timestamps."""

import json
from pathlib import Path

import pytest

from meshcore_home_gateway.state import AnnouncedComponents, CommandLedger, JsonStateFile


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


def test_state_file_that_is_not_utf8_is_kept_aside(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff\xfe not json")
    assert JsonStateFile(path).load() == {}
    assert (tmp_path / "state.json.corrupt").exists()


def test_announced_components_report_the_ones_that_are_gone(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    components = AnnouncedComponents(store)
    day = 86_400.0
    assert components.replace({"radio": "binary_sensor", "send_a": "notify"}, now=0.0) == {}
    assert components.replace({"radio": "binary_sensor"}, now=day) == {"send_a": "notify"}
    # still pending in the next run: nothing tells the gateway that Home Assistant got it
    assert components.replace({"radio": "binary_sensor"}, now=2 * day) == {"send_a": "notify"}
    assert components.replace({"radio": "binary_sensor"}, now=32 * day) == {}  # expired
    CommandLedger(store).record("a1" * 32, 5)  # both live in the same file
    assert store.load()["ha_components"] == {"radio": "binary_sensor"}
    store.save({"ha_components": ["radio"], "ha_removed": {"x": "bad"}})
    assert components.replace({}, now=0.0) == {}


def test_a_component_that_comes_back_is_no_longer_removed(tmp_path: Path) -> None:
    components = AnnouncedComponents(JsonStateFile(tmp_path / "state.json"))
    components.replace({"send_a": "notify"}, now=0.0)
    assert components.replace({}, now=1.0) == {"send_a": "notify"}
    assert components.replace({"send_a": "notify"}, now=2.0) == {}
    assert components.replace({"send_a": "notify"}, now=3.0) == {}


def test_announced_components_survive_an_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"ha_components": {"send_a": "notify"}})

    def fail(_data: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "save", fail)
    assert AnnouncedComponents(store).replace({}, now=0.0) == {"send_a": "notify"}
    assert "read-only file system" in caplog.text
