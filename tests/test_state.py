"""Persistent state: atomic JSON file and the ledger of accepted command timestamps."""

import json
import logging
import os
from pathlib import Path

import pytest

from meshcore_home_gateway.core.mesh import AdvertCheck
from meshcore_home_gateway.core.telemetry import Saved, SavedContact
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    AdvertChecks,
    AnnouncedComponents,
    CommandLedger,
    ContactDeviceRecords,
    CurrentDocuments,
    DeviceRecord,
    JsonStateFile,
    RemovedDevice,
    RetainedKeys,
    TelemetryExclusions,
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


def test_current_documents_are_written_on_change_and_survive_an_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    current = CurrentDocuments(store)
    assert current.documents == {}
    alice = {"key": "aa" * 32, "telemetry_current": True, "readings": {"ch2_humidity_percent": 40}}
    documents = {"aa" * 32: alice}
    current.replace(documents)
    documents["bb" * 32] = {**alice, "key": "bb" * 32}  # a change after the call is one
    current.replace(documents)
    assert CurrentDocuments(store).documents == documents
    saves: list[object] = []
    with monkeypatch.context() as patch:
        patch.setattr(store, "save", saves.append)
        current.replace(dict(documents))
    assert saves == []  # unchanged: nothing written

    def fail(_data: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "save", fail)
    current.replace({})
    assert current.documents == documents  # unchanged: the next call tries again
    assert "cannot store the current contact documents: read-only file system" in caplog.text


def test_current_documents_of_another_shape_read_as_empty(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    sections: list[object] = [[], "x", 7, None]
    for section in sections:
        store.save({"current_contacts": section})
        assert CurrentDocuments(store).documents == {}
    good = {"key": "bb" * 32}
    store.save({"current_contacts": {
        "bb" * 32: good, "BB" * 32: good, "bb": good, "cc" * 32: [], "dd" * 32: None,
    }})  # fmt: skip
    assert CurrentDocuments(store).documents == {"bb" * 32: good}
    store.path.write_bytes(b'{"current_contacts": {')  # unreadable: moved aside
    assert CurrentDocuments(store).documents == {}


# --- advert checks (ADR 0018) -------------------------------------------------------------------

CHECKED = {"aa" * 32: AdvertCheck(1_768_464_000, True), "bb" * 32: AdvertCheck(0, False)}


def test_advert_checks_round_trip_with_a_time_and_a_flag_only(tmp_path: Path) -> None:
    """The last checked advert of each companion survives a restart. The file holds its time and
    flag, never a coordinate, and the other keys of state.json stay as they are."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"other": 1, "current_contacts": {"cc" * 32: {"key": "cc" * 32}}})
    others = store.load()
    checks = AdvertChecks(store)
    assert checks.checks == {}
    checks.replace(CHECKED)
    assert AdvertChecks(store).checks == CHECKED
    data = store.load()
    assert data.pop("advert_checks") == {
        "aa" * 32: {"advert_at": 1_768_464_000, "carries": True},
        "bb" * 32: {"advert_at": 0, "carries": False},
    }  # fmt: skip
    assert data == others
    checks.checks["cc" * 32] = AdvertCheck(1, True)  # a copy: what is stored stays
    assert checks.checks == CHECKED
    checks.replace({"bb" * 32: CHECKED["bb" * 32]})  # one left the table
    assert AdvertChecks(store).checks == {"bb" * 32: AdvertCheck(0, False)}


def test_unchanged_advert_checks_write_nothing(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    AdvertChecks(store).replace({})  # none checked yet: nothing to store
    assert not store.path.exists()
    AdvertChecks(store).replace(CHECKED)
    os.utime(store.path, ns=(0, 0))
    checks = AdvertChecks(store)  # what the file holds counts as stored
    checks.replace(dict(CHECKED))
    assert store.path.stat().st_mtime_ns == 0
    checks.replace({**CHECKED, "aa" * 32: AdvertCheck(1_768_464_060, True)})
    assert store.path.stat().st_mtime_ns != 0


def test_invalid_advert_checks_are_skipped_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    good = {"advert_at": 1_768_464_000, "carries": True}
    store.save({"advert_checks": {
        "aa" * 32: good, "aa" * 6: good, "AA" * 32: good,
        "b1" * 32: {"advert_at": -1, "carries": True},
        "b2" * 32: {"advert_at": 1.5, "carries": True},
        "b3" * 32: {"advert_at": True, "carries": True},
        "b4" * 32: {"advert_at": 2**32, "carries": True},  # the sender's clock is 32 bits
        "b5" * 32: {"advert_at": "5", "carries": True},
        "c1" * 32: {"advert_at": 5, "carries": 1},
        "c2" * 32: {"advert_at": 5, "carries": None},
        "c3" * 32: {"advert_at": 5},
        "d1" * 32: [5, True],
        "d2" * 32: None,
        "e1" * 32: {"advert_at": 0xFFFFFFFF, "carries": False, "later": "ignored"},
    }})  # fmt: skip
    with caplog.at_level(logging.WARNING):
        checks = AdvertChecks(store)
    assert checks.checks == {
        "aa" * 32: AdvertCheck(1_768_464_000, True), "e1" * 32: AdvertCheck(0xFFFFFFFF, False),
    }  # fmt: skip
    assert caplog.text.count("invalid advert checks skipped, 2 kept") == 1
    assert len(caplog.records) == 1
    sections: list[object] = [[], "x", 7, None]
    for section in sections:
        store.save({"advert_checks": section})
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert AdvertChecks(store).checks == {}
        assert caplog.text.count("invalid advert checks skipped, 0 kept") == 1
    store.save({"other": 1})
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert AdvertChecks(store).checks == {}
    assert caplog.text == ""  # none stored: nothing to say


def test_advert_checks_survive_an_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The observer keeps its checks in memory; what is stored stays until a write succeeds."""
    store = JsonStateFile(tmp_path / "state.json")
    checks = AdvertChecks(store)
    checks.replace(CHECKED)

    def fail(_data: object) -> None:
        raise OSError("read-only file system")

    with monkeypatch.context() as patch:
        patch.setattr(store, "save", fail)
        checks.replace({})
    assert checks.checks == CHECKED  # unchanged: the next call tries again
    assert "cannot store the advert checks: read-only file system" in caplog.text
    checks.replace({})
    assert AdvertChecks(store).checks == {}


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
            B: SavedContact(7200.0, 2, None, 7200.0, 40_000.0, 1, False, 1, 34, 3600.0),
        },
        (1200.0,),
        0.4,
        11.5,
    )
    records.save(saved, 1_800_000_000.0)
    assert store.load()["other"] == 1  # only its own key
    assert records.load(1_800_000_060.0) == (saved, 60.0)
    assert records.load(1_799_999_000.0) == (saved, None)  # the clock went back: unknown
    raw = json.dumps(store.load()["telemetry"])
    assert "latitude" not in raw  # no position
    with_6b = Saved(
        {A: SavedContact(812.5, 0, 95.0, 96.0, None, 0, True, 2, 19, None, 8373.0, 95.0)},
        hold_left_s=1800.0,
    )
    records.save(with_6b, 1_800_000_000.0)
    assert records.load(1_800_000_000.0) == (with_6b, 0.0)


def test_a_saved_schedule_forgets_the_answers_of_a_contact_excluded_meanwhile(
    tmp_path: Path,
) -> None:
    """Excluded or included while telemetry is off (D13): as Scheduler.forget, its answers are
    gone and it is due at once; when it was asked and flooded stays."""
    store = JsonStateFile(tmp_path / "state.json")
    records = TelemetryRecords(store)
    asked = SavedContact(812.5, 1, 95.0, 96.0, 4000.0, 1, True, 0, 19, None, 8373.0, 95.0)
    records.save(Saved({A: asked, B: SavedContact(10.0)}), 1_800_000_000.0)
    records.forget(A)
    records.forget("c" * 64)  # not in the schedule: nothing
    forgotten = SavedContact(0.0, 0, None, 96.0, 4000.0, 1, False, lpp_bytes=19)
    assert records.load(1_800_000_000.0) == (Saved({A: forgotten, B: SavedContact(10.0)}), 0.0)


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
        {"due_in_s": 1.0, "parked_age_s": -1.0},
        {"due_in_s": 1.0, "current_s": -1.0},
        {"due_in_s": 1.0, "current_s": float("nan")},
        {"due_in_s": 1.0, "current_s": True},
        {"due_in_s": 1.0, "current_s": None},
        {"due_in_s": 1.0, "located_age_s": -1.0},
        {"due_in_s": 1.0, "located_age_s": float("nan")},
        {"due_in_s": 1.0, "located_age_s": False},
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
    huge = {"version": 1, "saved_at": 10**400, "contacts": {A: {"due_in_s": 10**400}},
            "own_debt_s": 10**400, "mesh_debt_s": -1.0}  # fmt: skip
    store.save({"telemetry": huge})
    assert TelemetryRecords(store).load(0.0) == (Saved({}, (), 0.0, 0.0), None)  # never raises
    capped = {"version": 1, "saved_at": 0.0, "contacts": {A: {"due_in_s": 1e9}}}
    store.save({"telemetry": capped})
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved is not None
    assert saved.contacts[A].due_in_s == 7 * 86_400.0


def test_the_newest_floods_of_the_hour_are_kept(tmp_path: Path) -> None:
    """Only four count against the hourly limit: the newest, so a restart frees nothing early."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"telemetry": {
        "version": 1, "saved_at": 0.0, "contacts": {},
        "floods_age_s": [3300.0, 2700.0, 2100.0, 1500.0, 300.0],
    }})  # fmt: skip
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved is not None
    assert saved.floods_age_s == (300.0, 1500.0, 2100.0, 2700.0)


def test_the_new_telemetry_fields_are_checked_and_capped(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    entry = {"due_in_s": 1.0, "current_s": 1e9, "located_age_s": 0.0}
    store.save({"telemetry": {
        "version": 1, "saved_at": 0.0, "contacts": {A: entry}, "hold_left_s": 1e9,
    }})  # fmt: skip
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved is not None
    assert saved.contacts[A].current_s == 7 * 86_400.0
    assert saved.contacts[A].located_age_s == 0.0
    assert saved.hold_left_s == 7 * 86_400.0


@pytest.mark.parametrize("hold", [-1.0, float("nan"), True, "60", None, [60.0]])
def test_an_invalid_hold_reads_as_none(tmp_path: Path, hold: object) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"telemetry": {"version": 1, "saved_at": 0.0, "contacts": {}, "hold_left_s": hold}})
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved == Saved()


def test_telemetry_ran_before_with_a_record_of_any_version(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    records = TelemetryRecords(store)
    assert not records.ran_before()  # no file
    store.save({"command_timestamps": {}})
    assert not records.ran_before()  # a file without the key
    store.save({"telemetry": {"version": 1, "saved_at": 0.0, "contacts": {}}})
    assert records.ran_before()
    store.save({"telemetry": {"version": 2}})
    assert records.ran_before()  # a later version's: it ran
    store.save({"telemetry": []})
    assert not records.ran_before()  # no record
    (tmp_path / "dir").mkdir()
    assert not TelemetryRecords(JsonStateFile(tmp_path / "dir")).ran_before()  # an OSError


# --- telemetry exclusions -------------------------------------------------------------------------

C, D = "c3" * 32, "d4" * 32


def exclusions_file(tmp_path: Path) -> Path:
    return tmp_path / EXCLUSIONS_FILE


def failing_rename(_src: object, _dst: object) -> None:
    raise OSError(30, "Read-only file system")


def test_exclusions_round_trip_in_their_own_private_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    state = tmp_path / "state.json"
    JsonStateFile(state).save({"other": 1})
    before = state.read_bytes()
    path = exclusions_file(tmp_path)
    assert path.name == "telemetry-excluded.json"
    excluded = TelemetryExclusions(path)
    assert (len(excluded), excluded.keys, excluded.unreadable, excluded.pending) == (
        0, frozenset(), False, False,
    )  # fmt: skip
    assert not path.exists()  # nothing written until something changes
    assert excluded.exclude(B)
    assert excluded.exclude(A)
    assert A in excluded
    assert C not in excluded
    assert 7 not in excluded
    assert len(excluded) == 2
    assert excluded.keys == {A, B}
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [A, B]}  # sorted
    assert path.stat().st_mode & 0o777 == 0o600
    assert state.read_bytes() == before
    again = TelemetryExclusions(path)
    assert again.keys == {A, B}
    assert not again.pending
    assert excluded.include(A)
    assert TelemetryExclusions(path).keys == {B}
    assert not excluded.pending
    assert not list(tmp_path.glob("*.tmp"))
    many = [f"{i:02x}" * 32 for i in range(40, 0, -1)]
    for key in many:
        assert excluded.exclude(key)
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": sorted([*many, B])}
    assert "telemetry exclusions stored" not in caplog.text  # only after a failed write


def test_invalid_and_too_many_exclusions_are_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = exclusions_file(tmp_path)
    path.write_text(json.dumps({
        "excluded": [B, "B2" * 32, 7, "ab", A, None, A], "later": "ignored",
    }), encoding="utf-8")  # fmt: skip
    with caplog.at_level(logging.WARNING):
        excluded = TelemetryExclusions(path)
    assert excluded.keys == {A, B}
    assert not excluded.unreadable
    assert "4 entries that are no contact keys skipped" in caplog.text
    many = [f"{i:064x}" for i in range(TelemetryExclusions.LIMIT + 6)]
    path.write_text(json.dumps({"excluded": many}), encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        capped = TelemetryExclusions(path)
    assert capped.keys == set(many[: TelemetryExclusions.LIMIT])
    assert "1030 excluded contacts, 1024 kept" in caplog.text


def test_an_unchanged_exclusion_writes_nothing(tmp_path: Path) -> None:
    path = exclusions_file(tmp_path)
    excluded = TelemetryExclusions(path)
    assert excluded.exclude(A)
    os.utime(path, ns=(0, 0))
    assert excluded.exclude(A)
    assert excluded.include(B)  # not excluded: nothing to store
    assert path.stat().st_mtime_ns == 0


def test_an_exclusion_that_cannot_be_written_is_in_effect_and_stored_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = exclusions_file(tmp_path)
    excluded = TelemetryExclusions(path)
    assert excluded.exclude(A)
    with caplog.at_level(logging.INFO), monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing_rename)
        assert not excluded.exclude(B)
        assert B in excluded  # in effect
        assert excluded.pending
        assert not excluded.exclude(C)
        assert not excluded.exclude(B)  # repeated while unsaved: still not stored
        excluded.flush()  # still read-only
        assert not excluded.include(A)  # only once stored
        assert A in excluded
    assert caplog.text.count("cannot store the telemetry exclusions") == 1  # warned once
    assert "Read-only file system" in caplog.text
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [A]}
    assert not list(tmp_path.glob("*.tmp"))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        excluded.flush()  # writable again
        excluded.flush()
    assert [excluded.pending] == [False]
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [A, B, C]}
    assert caplog.text.count("telemetry exclusions stored") == 1
    assert caplog.text.count("cannot store") == 0
    with caplog.at_level(logging.INFO), monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing_rename)
        assert not excluded.exclude(D)
    assert caplog.text.count("cannot store the telemetry exclusions") == 1  # once per failure


def test_an_inclusion_that_cannot_be_written_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = exclusions_file(tmp_path)
    excluded = TelemetryExclusions(path)
    assert excluded.exclude(A)
    monkeypatch.setattr(os, "replace", failing_rename)
    assert not excluded.include(A)
    assert A in excluded
    assert not excluded.pending
    assert TelemetryExclusions(path).keys == {A}
    assert f"(Read-only file system): {A[:12]} stays excluded" in caplog.text
    assert "in effect" not in caplog.text
    assert not excluded.exclude(B)  # not stored: pending
    monkeypatch.undo()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert excluded.include(A)  # stores the pending exclusion with it
    assert TelemetryExclusions(path).keys == {B}
    assert not excluded.pending
    assert caplog.text.count("telemetry exclusions stored") == 1


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"\xff\xfe", b"[]", b'{"excluded": {}}', b'{"other": []}', b""],
)
def test_an_unreadable_exclusions_file_is_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes
) -> None:
    path = exclusions_file(tmp_path)
    path.write_bytes(content)
    with caplog.at_level(logging.ERROR):
        excluded = TelemetryExclusions(path)
    assert excluded.unreadable
    assert caplog.text.count("cannot be read") == 1
    assert len(excluded) == 0
    assert not excluded.exclude(A)  # in effect, never written
    assert not excluded.exclude(B)
    assert A in excluded
    assert excluded.pending
    assert caplog.text.count("is unreadable): in effect, until the service stops") == 1
    assert not excluded.include(A)
    assert A in excluded
    assert not excluded.include(C)  # nobody is included again while the file is unreadable
    excluded.flush()
    assert path.read_bytes() == content
    assert sorted(p.name for p in tmp_path.iterdir()) == [EXCLUSIONS_FILE]


def test_exclusions_in_memory_only_and_a_file_that_cannot_be_read(tmp_path: Path) -> None:
    memory = TelemetryExclusions(None)
    assert memory.exclude(A)
    assert A in memory
    assert not memory.pending
    memory.flush()
    assert memory.include(A)
    assert len(memory) == 0
    assert not list(tmp_path.iterdir())
    (tmp_path / EXCLUSIONS_FILE).mkdir()
    with pytest.raises(OSError, match="directory"):  # stops the service, as state.json does
        TelemetryExclusions(tmp_path / EXCLUSIONS_FILE)
