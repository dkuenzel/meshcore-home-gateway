"""Persistent state: atomic JSON file and the ledger of accepted command timestamps."""

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.core.mesh import AdvertCheck
from meshcore_home_gateway.core.telemetry import Saved, SavedContact
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    SLUG_KEEP_S,
    SLUGS_LEFT,
    AdvertChecks,
    AirtimeLevel,
    AnnouncedComponents,
    CommandLedger,
    ContactDeviceRecords,
    CurrentDocuments,
    DeviceRecord,
    JsonStateFile,
    OutboxRecords,
    RemovedDevice,
    RetainedKeys,
    SavedMessage,
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


def read_only_disk(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every write of a state file fails from now on; returns the log of the attempts."""
    attempts: list[str] = []

    def fail(_src: object, dst: object) -> None:
        attempts.append(Path(str(dst)).name)
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(os, "replace", fail)  # the rename of the temporary file
    return attempts


def counted_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every write of a state file, which still happens."""
    writes: list[str] = []
    replace = os.replace

    def counted(src: str, dst: str) -> None:
        writes.append(Path(dst).name)
        replace(src, dst)

    monkeypatch.setattr(os, "replace", counted)
    return writes


def test_the_state_file_is_read_once_and_the_sections_written_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARC-004: the sections change the document in memory, and a flush writes them in one go;
    a section written at once (the advert checks, the ledger) takes the others' changes with
    it. A file replaced meanwhile (a backup restored while the service runs) is overwritten
    with every section (CODE-004)."""
    path = tmp_path / "state.json"
    JsonStateFile(path).save({"retained_contacts": ["aa" * 32], "other": 1})
    store = JsonStateFile(path)
    keys, records = RetainedKeys(store, "contacts"), ContactDeviceRecords(store)
    writes = counted_writes(monkeypatch)
    path.write_text("{}", encoding="utf-8")  # replaced on disk: never read again
    keys.replace({"bb" * 32})
    records.hidden.add("cc" * 32)
    records.save()
    assert (writes, path.read_text(encoding="utf-8")) == ([], "{}")
    assert RetainedKeys(store, "contacts").keys == {"bb" * 32}  # a section reads the memory
    assert store.flush()
    assert writes == ["state.json"]
    assert store.flush()  # nothing changed since
    assert writes == ["state.json"]
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["other"] == 1
    assert written["retained_contacts"] == ["bb" * 32]
    assert ContactDeviceRecords(JsonStateFile(path)).hidden == {"cc" * 32}
    keys.replace({"cc" * 32})
    AdvertChecks(store).replace(CHECKED)  # at once, and the keys with it
    assert writes == ["state.json"] * 2
    assert RetainedKeys(JsonStateFile(path), "contacts").keys == {"cc" * 32}
    keys.replace({"dd" * 32})
    CommandLedger(store).record("a1" * 32, 5)  # at once too
    assert writes == ["state.json"] * 3
    assert RetainedKeys(JsonStateFile(path), "contacts").keys == {"dd" * 32}
    assert store.flush()
    assert writes == ["state.json"] * 3


def test_a_state_file_that_cannot_be_written_is_written_once_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The changes stay in memory and in effect; a warning once, `failed` for the health
    section, and an info line once the file is written again."""
    path = tmp_path / "state.json"
    store = JsonStateFile(path)
    keys = RetainedKeys(store, "contacts")
    keys.replace({"aa" * 32})
    with monkeypatch.context() as patch:
        attempts = read_only_disk(patch)
        assert not store.flush()
        assert not store.flush()
        assert attempts == ["state.json"] * 2
        assert (store.failed, keys.keys) == (True, frozenset({"aa" * 32}))
    assert caplog.text.count("cannot write the state file") == 1
    assert "Read-only file system" in caplog.text
    assert not path.exists()
    assert [p.name for p in tmp_path.iterdir()] == []  # no temporary file left
    with caplog.at_level(logging.INFO):
        assert store.flush()
    assert not store.failed
    assert "state file" in caplog.records[-1].message
    assert "written again" in caplog.records[-1].message
    assert RetainedKeys(JsonStateFile(path), "contacts").keys == {"aa" * 32}


def test_a_corrupt_state_file_is_moved_aside_once_for_every_section(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = JsonStateFile(path)
    keys = RetainedKeys(store, "contacts")
    path.write_text('{"advert_checks": {}}', encoding="utf-8")  # never read: the file is read once
    assert (keys.keys, AdvertChecks(store).checks, CurrentDocuments(store).documents) == (
        frozenset(),
        {},
        {},
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.corrupt"]


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


def test_ledger_remembers_the_time_step_of_the_newest_code(tmp_path: Path) -> None:
    """A code serves once (IMP-002), also across a restart; a sender without one stores none."""
    path = tmp_path / "state.json"
    ledger = CommandLedger(JsonStateFile(path))
    ledger.record("aa" * 32, 100, 7)
    ledger.record("aa" * 32, 110, 6)  # a newer command, an older code: the step stays
    ledger.record("bb" * 32, 120)
    assert dict(ledger.code_steps) == {"aa" * 32: 7}
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "command_timestamps": {"aa" * 32: 110, "bb" * 32: 120},
        "command_code_steps": {"aa" * 32: 7},
    }
    assert dict(CommandLedger(JsonStateFile(path)).code_steps) == {"aa" * 32: 7}


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal that was never written comes back at the next start from the components
    stored before."""
    path = tmp_path / "state.json"
    JsonStateFile(path).save({"ha_components": {"send_a": "notify"}})
    store = JsonStateFile(path)
    assert AnnouncedComponents(store).replace({}, now=0.0) == {"send_a": "notify"}
    read_only_disk(monkeypatch)
    assert not store.flush()
    assert AnnouncedComponents(JsonStateFile(path)).replace({}, now=0.0) == {"send_a": "notify"}


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
    RetainedKeys(ledger.store, "contacts").replace({"bb" * 32})  # another section's write
    assert ledger.store.flush()
    assert CommandLedger(JsonStateFile(path)).last("aa" * 32) == 100  # not the refused one


def test_retained_keys_are_remembered_and_survive_an_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    keys = RetainedKeys(store, "contacts")
    assert keys.keys == frozenset()
    keys.replace({"aa" * 32})
    assert RetainedKeys(store, "contacts").keys == {"aa" * 32}
    store.save({**store.load(), "retained_contacts": ["bb" * 32, 7]})  # foreign entries skipped
    assert RetainedKeys(store, "contacts").keys == {"bb" * 32}
    keys = RetainedKeys(store, "contacts")
    with monkeypatch.context() as patch:
        read_only_disk(patch)
        keys.replace({"cc" * 32})
        assert not store.flush()
    assert keys.keys == {"cc" * 32}  # in effect, and written once it can be
    assert RetainedKeys(JsonStateFile(store.path), "contacts").keys == {"bb" * 32}
    assert store.flush()
    assert RetainedKeys(JsonStateFile(store.path), "contacts").keys == {"cc" * 32}


def test_current_documents_are_written_on_change_and_survive_an_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    current = CurrentDocuments(store)
    assert current.documents == {}
    alice = {"key": "aa" * 32, "telemetry_current": True, "readings": {"ch2_humidity_percent": 40}}
    documents = {"aa" * 32: alice}
    current.replace(documents)
    documents["bb" * 32] = {**alice, "key": "bb" * 32}  # a change after the call is one
    current.replace(documents)
    assert store.flush()
    assert CurrentDocuments(JsonStateFile(store.path)).documents == documents
    with monkeypatch.context() as patch:
        writes = counted_writes(patch)
        current.replace(dict(documents))
        assert store.flush()
    assert writes == []  # unchanged: nothing written
    with monkeypatch.context() as patch:
        read_only_disk(patch)
        current.replace({})
        assert not store.flush()
    assert current.documents == {}  # in effect, and written once it can be
    assert CurrentDocuments(JsonStateFile(store.path)).documents == documents
    assert store.flush()
    assert CurrentDocuments(JsonStateFile(store.path)).documents == {}


def test_current_documents_of_another_shape_read_as_empty(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    sections: list[object] = [[], "x", 7, None]
    for section in sections:
        store.save({"current_contacts": section})
        assert CurrentDocuments(store).documents == {}
    good = {"key": "bb" * 32}
    store.save(
        {
            "current_contacts": {
                "bb" * 32: good,
                "BB" * 32: good,
                "bb": good,
                "cc" * 32: [],
                "dd" * 32: None,
            }
        }
    )
    assert CurrentDocuments(store).documents == {"bb" * 32: good}
    store.path.write_bytes(b'{"current_contacts": {')  # unreadable: moved aside at the start
    assert CurrentDocuments(JsonStateFile(store.path)).documents == {}


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
    }
    assert data == others
    checks.checks["cc" * 32] = AdvertCheck(1, True)  # a copy: what is stored stays
    assert checks.checks == CHECKED
    checks.replace({"bb" * 32: CHECKED["bb" * 32]})  # one left the table
    assert AdvertChecks(store).checks == {"bb" * 32: AdvertCheck(0, False)}


def test_unchanged_advert_checks_write_nothing(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    AdvertChecks(store).replace({})  # none checked yet: nothing to store
    assert store.flush()
    assert not store.path.exists()
    AdvertChecks(store).replace(CHECKED)
    assert store.flush()
    os.utime(store.path, ns=(0, 0))
    checks = AdvertChecks(JsonStateFile(store.path))  # what the file holds counts as stored
    checks.replace(dict(CHECKED))
    assert store.flush()
    assert store.path.stat().st_mtime_ns == 0
    checks = AdvertChecks(store)
    checks.replace({**CHECKED, "aa" * 32: AdvertCheck(1_768_464_060, True)})
    assert store.flush()
    assert store.path.stat().st_mtime_ns != 0


def test_invalid_advert_checks_are_skipped_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    good = {"advert_at": 1_768_464_000, "carries": True}
    store.save(
        {
            "advert_checks": {
                "aa" * 32: good,
                "aa" * 6: good,
                "AA" * 32: good,
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
            }
        }
    )
    with caplog.at_level(logging.WARNING):
        checks = AdvertChecks(store)
    assert checks.checks == {
        "aa" * 32: AdvertCheck(1_768_464_000, True),
        "e1" * 32: AdvertCheck(0xFFFFFFFF, False),
    }
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observer's checks are in effect at once; what is stored stays until a write
    succeeds."""
    store = JsonStateFile(tmp_path / "state.json")
    checks = AdvertChecks(store)
    checks.replace(CHECKED)
    assert store.flush()
    with monkeypatch.context() as patch:
        read_only_disk(patch)
        checks.replace({})
        assert not store.flush()
    assert checks.checks == {}
    assert AdvertChecks(JsonStateFile(store.path)).checks == CHECKED
    assert store.flush()
    assert AdvertChecks(JsonStateFile(store.path)).checks == {}


def test_contact_device_records_survive_and_skip_foreign_entries(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    records = ContactDeviceRecords(store)
    assert (records.announced, records.removed, records.hidden) == ({}, {}, set())
    records.announced["aa" * 32] = DeviceRecord("alice", {"send": "notify"})
    records.removed["bb" * 32] = RemovedDevice({"hops": "sensor"}, 1.5)
    records.hidden.add("cc" * 32)
    records.save()
    assert store.flush()
    again = ContactDeviceRecords(JsonStateFile(store.path))
    assert again.announced == {"aa" * 32: DeviceRecord("alice", {"send": "notify"})}
    assert again.removed == {"bb" * 32: RemovedDevice({"hops": "sensor"}, 1.5)}
    assert again.hidden == {"cc" * 32}
    store.save(
        {
            **store.load(),
            "ha_contact_devices": {
                "announced": {"dd" * 32: {"slug": 7}, "ee" * 32: {"slug": "e", "cmps": {"x": 1}}},
                "removed": {"ff" * 32: {"since": "never"}},
                "hidden": ["11" * 32, None],
                "left": {"ee" * 32: "yesterday", "99" * 32: 5.0},
            },
        }
    )
    foreign = ContactDeviceRecords(store)
    assert foreign.announced == {"ee" * 32: DeviceRecord("e", {})}
    assert (foreign.removed, foreign.hidden, foreign.left) == ({}, {"11" * 32}, {})


def test_the_slugs_of_contacts_that_left_are_kept_for_a_while(tmp_path: Path) -> None:
    """CODE-003: 2,000 contacts passing through the table leave at most SLUGS_LEFT slugs
    behind, the newest; one that left SLUG_KEEP_S ago is forgotten, one in the table never."""
    store = JsonStateFile(tmp_path / "state.json")
    records = ContactDeviceRecords(store)
    household = "ab" * 32
    records.slugs[household] = "home"
    for n in range(2000):
        key = f"{n:064x}"
        records.slugs[key] = f"node_{n}"
        records.prune({household, key}, float(n))
    assert len(records.slugs) == SLUGS_LEFT + 2  # those that left, the household, the last one
    assert records.slugs[household] == "home"
    assert f"{998:064x}" not in records.slugs
    assert records.slugs[f"{999:064x}"] == "node_999"
    records.save()
    assert store.flush()
    again = ContactDeviceRecords(JsonStateFile(store.path))
    assert (again.slugs, again.left) == (records.slugs, records.left)
    assert again.left[f"{1998:064x}"] == 1999.0  # when the read found it gone
    again.prune({household}, 1999.0 + SLUG_KEEP_S)
    assert again.slugs == {household: "home", f"{1999:064x}": "node_1999"}
    assert again.left == {f"{1999:064x}": 1999.0 + SLUG_KEEP_S}  # it left only now
    again.prune({household, f"{1999:064x}"}, 1999.0 + 2 * SLUG_KEEP_S)  # back in the table
    assert again.slugs == {household: "home", f"{1999:064x}": "node_1999"}
    assert again.left == {}


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
    """Excluded or included while telemetry is off (design §6.11): as Scheduler.forget, its
    answers are gone and it is due at once; when it was asked and flooded stays."""
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
    store.save(
        {
            "telemetry": {
                "version": 1,
                "saved_at": 0.0,
                "contacts": {A: entry, B: good, "B2" * 32: good},
                "floods_age_s": [10.0, -1.0, 4000.0, "x"],
            }
        }
    )
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
    huge = {
        "version": 1,
        "saved_at": 10**400,
        "contacts": {A: {"due_in_s": 10**400}},
        "own_debt_s": 10**400,
        "mesh_debt_s": -1.0,
    }
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
    store.save(
        {
            "telemetry": {
                "version": 1,
                "saved_at": 0.0,
                "contacts": {},
                "floods_age_s": [3300.0, 2700.0, 2100.0, 1500.0, 300.0],
            }
        }
    )
    saved, _ = TelemetryRecords(store).load(0.0)
    assert saved is not None
    assert saved.floods_age_s == (300.0, 1500.0, 2100.0, 2700.0)


def test_the_new_telemetry_fields_are_checked_and_capped(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    entry = {"due_in_s": 1.0, "current_s": 1e9, "located_age_s": 0.0}
    store.save(
        {
            "telemetry": {
                "version": 1,
                "saved_at": 0.0,
                "contacts": {A: entry},
                "hold_left_s": 1e9,
            }
        }
    )
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


# --- the message budget and the queue across a stop (ARC-005, ARC-013) -------------------------


def test_the_airtime_level_is_written_at_once_with_the_longest_message(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    level = AirtimeLevel(JsonStateFile(path))
    assert level.load(1_000.0) is None  # none saved: a full budget
    level.longest_s = 1.5
    level.save(2.25, 1_000.0)
    again = AirtimeLevel(JsonStateFile(path))  # on the disk at once
    assert again.load(1_060.0) == (2.25, 60.0)
    assert again.longest_s == 1.5
    assert again.load(900.0) == (2.25, None)  # the clock went back: unknown
    invalid_entries: list[object] = [
        {"level_s": "2", "saved_at": 1.0},
        {"level_s": 2.0},
        [],
        {"level_s": 1e999},
    ]
    for bad in invalid_entries:
        JsonStateFile(path).save({"airtime": bad})
        assert AirtimeLevel(JsonStateFile(path)).load(1_000.0) is None
    JsonStateFile(path).save({"airtime": {"level_s": 1.0, "saved_at": 0.0, "longest_s": -1}})
    invalid = AirtimeLevel(JsonStateFile(path))
    assert (invalid.load(10.0), invalid.longest_s) == ((1.0, 10.0), None)


KEPT = [
    SavedMessage("7f3a91c2", "alerts", "Gate open", None, 12.5, None, 0, 1_799_999_987.5),
    SavedMessage("0b1c2d3e", "aa" * 32, "Hello #00ff00ff", None, 3.0, 1_800_000_000, 2),
    SavedMessage("11223344", "alerts", "Pump", "Pump", 0.0, None, 0, 1_800_000_003.25),
]


def test_the_messages_of_a_stop_are_taken_once(tmp_path: Path) -> None:
    """Written with the stop, taken at the next start, and gone from the disk at once: a crash
    after the start does not bring them back a second time."""
    path = tmp_path / "state.json"
    store = JsonStateFile(path)
    OutboxRecords(store).save(KEPT, 1_000.0)
    assert store.flush()
    taken = JsonStateFile(path)
    assert OutboxRecords(taken).take(1_030.0) == (KEPT, 30.0)
    assert "outbox" not in JsonStateFile(path).load()
    assert OutboxRecords(taken).take(1_030.0) == ([], None)
    OutboxRecords(store).save([], 1_100.0)  # nothing kept: no section
    assert store.flush()
    assert "outbox" not in JsonStateFile(path).load()
    OutboxRecords(store).save(KEPT[:1], 2_000.0)
    assert store.flush()
    assert OutboxRecords(JsonStateFile(path)).take(1_000.0) == (KEPT[:1], None)  # clock back


def test_a_kept_message_keeps_when_it_was_accepted(tmp_path: Path) -> None:
    """The wall clock at its acceptance, for the history (design §6.6): written with the stop,
    and `null` for a message kept by 2.0, which has none, or with a value that is no time. 2.0
    reads only its own keys, so a downgrade takes the messages as before."""
    path = tmp_path / "state.json"
    store = JsonStateFile(path)
    OutboxRecords(store).save(KEPT, 1_000.0)
    assert store.flush()
    written = JsonStateFile(path).load()["outbox"]["messages"]
    assert [m["queued_at"] for m in written] == [1_799_999_987.5, None, 1_800_000_003.25]
    entries: list[dict[str, Any]] = [
        {"id": "a", "target": "alerts", "text": "x", "age_s": 1.0},  # 2.0's
        {"id": "b", "target": "alerts", "text": "x", "age_s": 1.0, "queued_at": "now"},
        {"id": "c", "target": "alerts", "text": "x", "age_s": 1.0, "queued_at": 1e999},
        {"id": "d", "target": "alerts", "text": "x", "age_s": 1.0, "queued_at": True},
        {"id": "e", "target": "alerts", "text": "x", "age_s": 1.0, "queued_at": 1_800_000_000},
    ]
    JsonStateFile(path).save({"outbox": {"saved_at": 0.0, "messages": entries}})
    messages, _ = OutboxRecords(JsonStateFile(path)).take(1.0)
    assert [(m.id, m.queued_at) for m in messages] == [
        ("a", None),
        ("b", None),
        ("c", None),
        ("d", None),
        ("e", 1_800_000_000.0),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "", "target": "alerts", "text": "x", "age_s": 1.0},
        {"id": "a" * 65, "target": "alerts", "text": "x", "age_s": 1.0},
        {"id": "a", "target": 7, "text": "x", "age_s": 1.0},
        {"id": "a", "target": "alerts", "text": "é" * 513, "age_s": 1.0},
        {"id": "a", "target": "alerts", "text": "x", "age_s": -1.0},
        {"id": "a", "target": "alerts", "text": "x", "age_s": 1.0, "sent": 5},
        {"id": "a", "target": "alerts", "text": "x", "age_s": 1.0, "timestamp": -1},
        {"id": "a", "target": "alerts", "text": "x", "age_s": 1.0, "raw": 3},
        ["a", "alerts", "x"],
    ],
)
def test_invalid_kept_messages_are_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, entry: object
) -> None:
    path = tmp_path / "state.json"
    good = {"id": "b", "target": "alerts", "text": "y", "age_s": 2.0}
    JsonStateFile(path).save({"outbox": {"saved_at": 0.0, "messages": [entry, good]}})
    with caplog.at_level(logging.WARNING):
        messages, _ = OutboxRecords(JsonStateFile(path)).take(1.0)
    assert messages == [SavedMessage("b", "alerts", "y", None, 2.0, None, 0)]
    assert "invalid messages of the last stop skipped, 1 kept" in caplog.text


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
        0,
        frozenset(),
        False,
        False,
    )
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


def test_with_ask_household_only_included_and_protected_keys_are_asked(tmp_path: Path) -> None:
    """`[telemetry] ask = "household"` (SEC-016): `key in exclusions` means not asked; the
    inclusions are kept beside the exclusions, which an earlier version reads as before."""
    path = exclusions_file(tmp_path)
    household = TelemetryExclusions(path, household=True, protected=frozenset({C}))
    assert (A in household, C in household, len(household), household.household) == (
        True,
        False,
        0,
        True,
    )
    assert household.include(A)
    assert A not in household
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [], "included": [A]}
    assert household.exclude(A)
    assert A in household
    assert household.exclude(C)  # a protected key can be excluded too
    assert C in household
    assert household.include(B)
    stored = {"excluded": [A, C], "included": [B]}
    assert json.loads(path.read_text(encoding="utf-8")) == stored
    again = TelemetryExclusions(path, household=True)
    assert (again.keys, again.included) == (frozenset({A, C}), frozenset({B}))
    assert (B in again, D in again) == (False, True)
    every = TelemetryExclusions(path)  # `all`: the inclusions do not count, and are kept
    assert (B in every, D in every) == (False, False)
    assert every.include(D)  # not excluded: nothing to store
    assert every.include(A)
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [C], "included": [B]}
    path.write_text(json.dumps({"excluded": [A], "included": [A, B]}), encoding="utf-8")
    assert TelemetryExclusions(path, household=True).included == {B}  # excluded wins


def test_invalid_and_too_many_exclusions_are_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = exclusions_file(tmp_path)
    path.write_text(
        json.dumps(
            {
                "excluded": [B, "B2" * 32, 7, "ab", A, None, A],
                "later": "ignored",
            }
        ),
        encoding="utf-8",
    )
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
    [
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'{"excluded": {}}',
        b'{"other": []}',
        b"",
        b'{"excluded": [], "included": {}}',
    ],
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
