"""Persistent state: atomic JSON file and the ledger of accepted command timestamps."""

import json
import os
from pathlib import Path

import pytest

from meshcore_home_gateway.core.telemetry import Saved, SavedContact
from meshcore_home_gateway.state import (
    AnnouncedComponents,
    CommandLedger,
    ContactDeviceRecords,
    DeviceRecord,
    JsonStateFile,
    RemovedDevice,
    RetainedKeys,
    TelemetryRecords,
)


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


def test_each_discovery_document_keeps_its_own_components(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    main, controls = AnnouncedComponents(store), AnnouncedComponents(store, "manage_contacts")
    main.replace({"radio": "binary_sensor"}, now=0.0)
    controls.replace({"add_node": "select", "add_group": "select"}, now=0.0)
    assert controls.replace({"add_node": "select"}, now=1.0) == {"add_group": "select"}
    assert main.replace({"radio": "binary_sensor"}, now=1.0) == {}
    data = store.load()
    assert data["ha_components"] == {"radio": "binary_sensor"}
    assert data["ha_components_manage_contacts"] == {"add_node": "select"}
    assert data["ha_removed_manage_contacts"]["add_group"]["p"] == "select"


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


def test_a_failed_save_leaves_no_temporary_file_and_keeps_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    ledger = CommandLedger(JsonStateFile(path))
    ledger.record("aa" * 32, 100)

    def full_disk(_src: object, _dst: object) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", full_disk)  # the rename of the temporary file
        with pytest.raises(OSError, match="No space left"):
            ledger.record("aa" * 32, 200)
    assert ledger.last("aa" * 32) == 100
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    assert CommandLedger(JsonStateFile(path)).last("aa" * 32) == 100


def test_retained_keys_are_remembered_and_survive_an_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    keys = RetainedKeys(store, "contacts")
    assert keys.keys == frozenset()
    keys.replace({"aa" * 32})
    assert RetainedKeys(store, "contacts").keys == {"aa" * 32}
    store.save({**store.load(), "retained_contacts": ["bb" * 32, 7]})  # foreign entries skipped
    assert RetainedKeys(store, "contacts").keys == {"bb" * 32}

    def fail(_data: object) -> None:
        raise OSError("read-only file system")

    keys = RetainedKeys(store, "contacts")
    monkeypatch.setattr(store, "save", fail)
    keys.replace({"cc" * 32})
    assert keys.keys == {"bb" * 32}  # unchanged: the next change tries again
    assert "cannot store the published retained_contacts: read-only file system" in caplog.text


def test_contact_device_records_survive_and_skip_foreign_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    records = ContactDeviceRecords(store)
    assert (records.announced, records.removed, records.hidden) == ({}, {}, set())
    records.announced["aa" * 32] = DeviceRecord("alice", {"send": "notify"})
    records.removed["bb" * 32] = RemovedDevice({"hops": "sensor"}, 1.5)
    records.hidden.add("cc" * 32)
    records.save()
    again = ContactDeviceRecords(store)
    assert again.announced == {"aa" * 32: DeviceRecord("alice", {"send": "notify"})}
    assert again.removed == {"bb" * 32: RemovedDevice({"hops": "sensor"}, 1.5)}
    assert again.hidden == {"cc" * 32}
    store.save({**store.load(), "ha_contact_devices": {
        "announced": {"dd" * 32: {"slug": 7}, "ee" * 32: {"slug": "e", "cmps": {"x": 1}}},
        "removed": {"ff" * 32: {"since": "never"}}, "hidden": ["11" * 32, None],
    }})  # fmt: skip
    foreign = ContactDeviceRecords(store)
    assert foreign.announced == {"ee" * 32: DeviceRecord("e", {})}
    assert (foreign.removed, foreign.hidden) == ({}, {"11" * 32})

    def fail(_data: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "save", fail)
    foreign.hidden.clear()
    foreign.save()
    assert "cannot store the Home Assistant contact devices: read-only file system" in caplog.text


# --- telemetry schedule (ADR 0018) --------------------------------------------------------------

A, B = "a1" * 32, "b2" * 32


def test_the_telemetry_schedule_round_trips_relative_to_its_save(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"other": 1})
    records = TelemetryRecords(store)
    saved = Saved(
        {
            A: SavedContact(812.5, 0, 95.0, 96.0, None, 0, True, 2, 19),
            B: SavedContact(7200.0, 2, None, 7200.0, 40_000.0, 1, False, 1, 34),
        },
        (1200.0,),
    )
    records.save(saved, 1_800_000_000.0)
    assert store.load()["other"] == 1  # only its own key
    assert records.load(1_800_000_060.0) == (saved, 60.0)
    assert records.load(1_799_999_000.0) == (saved, 0.0)  # the clock went back
    raw = json.dumps(store.load()["telemetry"])
    assert "latitude" not in raw  # no position


@pytest.mark.parametrize(
    "entry",
    [
        {"due_in_s": -1.0},
        {"due_in_s": float("nan")},
        {"due_in_s": "1"},
        {"due_in_s": 1.0, "failures": True},
        {"due_in_s": 1.0, "failures": 17},
        {"due_in_s": 1.0, "answered_age_s": -5.0},
        {"due_in_s": 1.0, "located": 1},
        {"due_in_s": 1.0, "still": 3},
        {"due_in_s": 1.0, "lpp_bytes": 0},
        {"due_in_s": 1.0, "lpp_bytes": 157},
        [],
    ],
)
def test_invalid_telemetry_entries_are_skipped(tmp_path: Path, entry: object) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    good = {"due_in_s": 10.0, "unknown": "ignored"}
    store.save({"telemetry": {
        "version": 1, "saved_at": 0.0, "contacts": {A: entry, B: good, "B2" * 32: good},
        "floods_age_s": [10.0, -1.0, 4000.0, "x"],
    }})  # fmt: skip
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved == Saved({B: SavedContact(10.0)}, (10.0,))  # upper-case key: not one


def test_an_unknown_or_unreadable_telemetry_schedule_starts_empty(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"telemetry": {"version": 2, "contacts": {A: {"due_in_s": 1.0}}}})
    assert TelemetryRecords(store).load(0.0) == (None, 0.0)
    store.save({"telemetry": []})
    assert TelemetryRecords(store).load(0.0) == (None, 0.0)
    (tmp_path / "dir").mkdir()
    unreadable = TelemetryRecords(JsonStateFile(tmp_path / "dir"))
    assert unreadable.load(0.0) == (None, 0.0)  # an OSError: logged, never raised
    unreadable.save(Saved(), 0.0)  # logged too
    capped = {"version": 1, "saved_at": 0.0, "contacts": {A: {"due_in_s": 1e9}}}
    store.save({"telemetry": capped})
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved is not None
    assert saved.contacts[A].due_in_s == 7 * 86_400.0
