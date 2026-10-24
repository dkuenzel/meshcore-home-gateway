"""The managed radio profile (ADR 0020): the reconcile rules, the setters on the emulated radio,
and what the gateway reports."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.config import ProfileConfig
from meshcore_home_gateway.core import profile as policy
from meshcore_home_gateway.core.profile import Applied, Desired, Mode
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Command, ContactFlag
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import CompanionSession, RadioError, RadioEvent
from meshcore_home_gateway.state import JsonStateFile, ProfileRecords
from meshcore_home_gateway.views import settings_document
from tests.radio_emulator import SELF_KEY, START_TIME, EmulatedContact, EmulatedRadio

OBSERVER: dict[str, policy.Value | None] = {
    policy.MANUAL_ADD: True, policy.AUTOADD: 0, policy.TELEMETRY: 0, policy.ADVERT_LOCATION: 0,
}  # fmt: skip
FACTORY: dict[str, policy.Value | None] = {
    policy.MANUAL_ADD: False, policy.AUTOADD: 0x1F, policy.TELEMETRY: 0x15,
    policy.ADVERT_LOCATION: 1, policy.AIRTIME_FACTOR: 1000, policy.NAME: "MeshCore-ab12",
}  # fmt: skip
SETTERS = {
    Command.SET_OTHER_PARAMS, Command.SET_AUTOADD_CONFIG, Command.SET_TUNING_PARAMS,
    Command.SET_ADVERT_NAME, Command.ADD_UPDATE_CONTACT,
}  # fmt: skip


def plan_for(
    want: Desired, live: dict[str, Any], applied: Applied | None = None, *,
    mode: Mode = Mode.AUTO, firmware: int = 14,
) -> policy.Plan:  # fmt: skip
    return policy.plan(want, applied or Applied(), live, mode=mode, firmware_code=firmware)


# --- the rules -----------------------------------------------------------------------------------


def test_the_observer_profile_and_the_ten_percent_band() -> None:
    assert policy.desired(869_618).values == {**OBSERVER, policy.AIRTIME_FACTOR: 9000}
    assert policy.desired(868_100).values == OBSERVER  # elsewhere the duty cycle stays as found
    assert policy.desired(869_400).values[policy.AIRTIME_FACTOR] == 9000  # band edges included
    assert policy.desired(869_651).values == OBSERVER
    named = policy.desired(868_100, name="Gateway", duty_cycle_percent=25.0)
    assert named.values[policy.NAME] == "Gateway"
    assert named.values[policy.AIRTIME_FACTOR] == 3000


@pytest.mark.parametrize(
    ("percent", "milli"), [(10.0, 9000), (100.0, 0), (50.0, 1000), (33.3, 2003), (12.5, 7000)]
)
def test_duty_cycle_to_airtime_factor(percent: float, milli: int) -> None:
    assert policy.airtime_factor_milli(percent) == milli


def test_a_factory_radio_gets_every_difference_written() -> None:
    want = policy.desired(869_618, name="Gateway")
    plan = plan_for(want, FACTORY)
    assert plan.writes == {**OBSERVER, policy.AIRTIME_FACTOR: 9000, policy.NAME: "Gateway"}
    assert set(plan.reasons.values()) == {"initial"}  # the first connect of this radio


def test_a_matching_radio_gets_nothing_written() -> None:
    want = policy.desired(869_618)
    live = {**OBSERVER, policy.AIRTIME_FACTOR: 9001, policy.NAME: "x"}  # float32 rounding
    assert plan_for(want, live).writes == {}


def test_a_key_the_firmware_lacks_is_left_alone() -> None:
    want = policy.desired(869_618)
    live = {**FACTORY, policy.AUTOADD: None, policy.AIRTIME_FACTOR: None}
    assert set(plan_for(want, live).writes) == {
        policy.MANUAL_ADD, policy.TELEMETRY, policy.ADVERT_LOCATION,
    }  # fmt: skip


def test_drift_of_the_policy_is_set_back_but_a_renamed_radio_is_kept() -> None:
    want = policy.desired(868_100, name="Gateway")
    applied = Applied(dict(want.values))
    live = {**OBSERVER, policy.TELEMETRY: 0x01, policy.NAME: "Renamed in the app"}
    plan = plan_for(want, live, applied)
    assert plan.writes == {policy.TELEMETRY: 0}
    assert plan.reasons == {policy.TELEMETRY: "drift"}
    assert plan.drift == {policy.NAME: "Renamed in the app"}
    changed = policy.desired(868_100, name="Gateway 2")  # the operator changes the name
    assert plan_for(changed, live, applied).writes[policy.NAME] == "Gateway 2"


def test_observing_writes_nothing() -> None:
    want = policy.desired(869_618)
    for mode, firmware in ((Mode.OBSERVE, 14), (Mode.AUTO, 15), (Mode.AUTO, 9)):
        plan = plan_for(want, FACTORY, mode=mode, firmware=firmware)
        assert (plan.writes, plan.observing) == ({}, True)
    assert plan_for(want, FACTORY, mode=Mode.ALWAYS, firmware=15).writes


def test_a_setting_that_does_not_take_is_given_up_after_three_connects() -> None:
    want, applied = policy.desired(869_618), Applied()
    live = {**OBSERVER, policy.AIRTIME_FACTOR: 1000}  # a firmware that ignores the value
    for _ in range(policy.NOT_CONVERGING_AFTER):
        plan = plan_for(want, live, applied)
        assert plan.writes == {policy.AIRTIME_FACTOR: 9000}
        applied = policy.record(applied, want, plan, live, {}, firmware_code=14)
    plan = plan_for(want, live, applied)
    assert (plan.writes, plan.not_converging) == ({}, (policy.AIRTIME_FACTOR,))
    converged = {**live, policy.AIRTIME_FACTOR: 9000}  # set by hand in the meantime
    applied = policy.record(applied, want, plan, converged, {}, firmware_code=14)
    assert policy.AIRTIME_FACTOR not in applied.attempts
    assert applied.values[policy.AIRTIME_FACTOR] == 9000


def test_a_new_configured_value_starts_afresh() -> None:
    want, applied = policy.desired(869_618), Applied()
    live = {**OBSERVER, policy.AIRTIME_FACTOR: 1000}
    for _ in range(policy.NOT_CONVERGING_AFTER):
        applied = policy.record(applied, want, plan_for(want, live, applied), live, {},
                                firmware_code=14)  # fmt: skip
    other = policy.desired(869_618, duty_cycle_percent=25.0)
    assert plan_for(other, live, applied).writes == {policy.AIRTIME_FACTOR: 3000}


def test_writes_that_take_never_count_as_not_converging() -> None:
    """The operator renames the radio again and again, or the phone app keeps switching
    telemetry on: each write takes, so none is ever given up."""
    applied = Applied()
    for name in ("A", "B", "C", "D", "E"):
        want = policy.desired(868_100, name=name)
        live = {**OBSERVER, policy.TELEMETRY: 0x15, policy.NAME: "old"}
        plan = plan_for(want, live, applied)
        assert plan.writes == {policy.TELEMETRY: 0, policy.NAME: name}
        after = {**live, policy.TELEMETRY: 0, policy.NAME: name}
        applied = policy.record(applied, want, plan, after, {}, firmware_code=14)
    assert applied.attempts == {}
    assert plan.reasons == {policy.TELEMETRY: "drift", policy.NAME: "configured"}


def test_a_configured_name_the_radio_already_has_is_remembered() -> None:
    """Else a later rename on the radio would be overwritten as a configuration change."""
    first = policy.desired(868_100, name="A")
    applied = Applied(dict(first.values))
    second = policy.desired(868_100, name="B")
    live = {**OBSERVER, policy.NAME: "B"}  # renamed by hand to the new name already
    applied = policy.record(applied, second, plan_for(second, live, applied), live, {},
                            firmware_code=14)  # fmt: skip
    renamed = {**OBSERVER, policy.NAME: "Kitchen"}
    plan = plan_for(second, renamed, applied)
    assert (plan.writes, plan.drift) == ({}, {policy.NAME: "Kitchen"})
    assert first.values[policy.NAME] == "A"


def test_unsupported_is_remembered_per_firmware() -> None:
    want, live = policy.desired(868_100), {**OBSERVER, policy.AUTOADD: 0x1F}
    plan = plan_for(want, live)
    applied = policy.record(Applied(), want, plan, live, {policy.AUTOADD: True}, firmware_code=14)
    assert applied.unsupported == {policy.AUTOADD: 14}
    assert plan_for(want, live, applied).unsupported == (policy.AUTOADD,)
    assert plan_for(want, live, applied, firmware=15, mode=Mode.ALWAYS).writes  # newer firmware


def test_a_refused_write_counts_towards_the_guard() -> None:
    want, live = policy.desired(868_100), {**OBSERVER, policy.TELEMETRY: 1}
    applied = Applied()
    for _ in range(policy.NOT_CONVERGING_AFTER):
        plan = plan_for(want, live, applied)
        applied = policy.record(applied, want, plan, live, {policy.TELEMETRY: False},
                                firmware_code=14)  # fmt: skip
    assert plan_for(want, live, applied).not_converging == (policy.TELEMETRY,)
    assert policy.TELEMETRY not in applied.values  # never taken
    assert applied.attempts[policy.TELEMETRY] == policy.Attempt(0, 3, 14)


def test_a_key_no_longer_configured_is_forgotten() -> None:
    named = policy.desired(868_100, name="Gateway")
    live = {**OBSERVER, policy.NAME: "Gateway"}
    applied = policy.record(Applied(), named, plan_for(named, live), live, {}, firmware_code=14)
    assert applied.values[policy.NAME] == "Gateway"
    unnamed = policy.desired(868_100)
    applied = policy.record(applied, unnamed, plan_for(unnamed, live), live, {}, firmware_code=14)
    assert policy.NAME not in applied.values


# --- the frames ----------------------------------------------------------------------------------


def test_setter_frames_leave_the_other_bytes_alone() -> None:
    assert codec.set_other_params(True, 0, 0) == bytes([38, 1, 0, 0])  # extra acks kept
    assert codec.set_autoadd_config(0) == bytes([58, 0])  # hop limit kept
    assert codec.set_tuning_params(250, 9000) == bytes([21]) + (250).to_bytes(4, "little") + (
        9000
    ).to_bytes(4, "little")
    assert codec.set_advert_name("Süd") == bytes([8]) + "Süd".encode()
    for bad in ("", "x" * 32, "a\x00b"):
        with pytest.raises(ValueError, match="radio name"):
            codec.set_advert_name(bad)
    with pytest.raises(ValueError, match="auto-add"):
        codec.set_autoadd_config(0x20)
    with pytest.raises(ValueError, match="telemetry"):
        codec.set_other_params(True, 0x40, 0)


def test_a_contact_is_written_back_with_other_flags_only() -> None:
    contact = EmulatedContact(bytes.fromhex("a1" * 32), "Alice", out_path_len=1, out_path=b"\x7f")
    record = codec.decode(contact.record())
    assert isinstance(record, codec.ContactRecord)
    frame = codec.add_update_contact(record, ContactFlag.FAVOURITE)
    assert frame[0] == Command.ADD_UPDATE_CONTACT
    assert frame[34] == ContactFlag.FAVOURITE
    assert frame[1:34] + frame[35:] == record.raw[:33] + record.raw[34:143]
    new = codec.new_contact(bytes.fromhex("b2" * 32), "operator", ContactFlag.FAVOURITE)
    assert len(new) == len(frame) == 144
    assert (new[33], new[34], new[35]) == (1, ContactFlag.FAVOURITE, 0xFF)


def test_a_new_contact_frame_has_the_type_and_signed_time_and_no_position() -> None:
    frame = codec.new_contact(bytes.fromhex("d4" * 32), "Dave", 0, node_type=3,
                              last_advert=1_800_000_000)  # fmt: skip
    expected = (
        bytes([Command.ADD_UPDATE_CONTACT]) + bytes.fromhex("d4" * 32) + bytes([3, 0, 0xFF])
        + bytes(64) + b"Dave".ljust(32, b"\x00") + struct.pack("<Iii", 1_800_000_000, 0, 0)
    )  # fmt: skip
    assert frame == expected
    assert codec.remove_contact(bytes.fromhex("d4" * 32)) == bytes([15]) + bytes.fromhex("d4" * 32)


@pytest.mark.parametrize(("flags", "node_type"), [(0x02, 1), (0x10, 1), (0x11, 1), (0, 0), (0, 5)])
def test_a_new_contact_gets_no_flag_but_the_favourite_and_a_real_type(
    flags: int, node_type: int
) -> None:
    with pytest.raises(ValueError, match="new_contact"):
        codec.new_contact(bytes(32), "x", flags, node_type=node_type)


def test_a_contact_written_back_keeps_its_flags_and_gets_none_but_the_favourite() -> None:
    """ADR 0017: the gateway never sets the command-line or a telemetry flag; one the user set is
    kept."""
    contact = EmulatedContact(bytes.fromhex("a1" * 32), "Alice", flags=0x10)
    record = codec.decode(contact.record())
    assert isinstance(record, codec.ContactRecord)
    assert codec.add_update_contact(record, 0x11)[34] == 0x11  # its own 0x10 stays
    plain = codec.decode(EmulatedContact(bytes.fromhex("a1" * 32), "Alice").record())
    assert isinstance(plain, codec.ContactRecord)
    for flags in (0x10, 0x03, 0x20, 0x80, 0xE1):
        with pytest.raises(ValueError, match="no flag but the favourite"):
            codec.add_update_contact(plain, flags)


def test_the_codec_has_no_command_that_could_harm_the_radio() -> None:
    """ADR 0020's deny list: radio parameters, the private key, raw data and packets, signing,
    the BLE PIN, factory reset and the radio's command line."""
    denied = {11, 23, 24, 25, 33, 34, 35, 37, 51, 65, 66}
    assert denied.isdisjoint(int(c) for c in Command)


# --- on the emulated radio -----------------------------------------------------------------------


def factory_radio(**options: Any) -> EmulatedRadio:
    factory: dict[str, Any] = {
        "name": "MeshCore-ab12", "manual_add": False, "autoadd_config": 0x1F,
        "telemetry_modes": 0x15, "advert_location": 1, "tuning": (250, 1000),
    }  # fmt: skip
    return EmulatedRadio(**{**factory, **options})


async def connect(radio: EmulatedRadio, manager: RadioProfile) -> CompanionSession:
    """One connect with the profile; the setters of earlier connects are forgotten."""
    radio.received.clear()
    session = CompanionSession(
        radio, asyncio.Queue[RadioEvent](), wall_clock=lambda: START_TIME, command_timeout_s=0.5,
        flush_settle_s=0.01, set_clock=False,
    )  # fmt: skip
    session.profile = manager
    await session.start(fetch_messages=False)
    await session.close()
    return session


def setters(radio: EmulatedRadio) -> list[bytes]:
    return [p for p in radio.received if p[0] in SETTERS]


def test_a_factory_radio_becomes_an_observer_and_stays_one(tmp_path: Path) -> None:
    radio = factory_radio()
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))

    async def main() -> None:
        manager = RadioProfile(ProfileConfig(name="Gateway"), records)
        await connect(radio, manager)
        assert (radio.manual_add, radio.autoadd_config, radio.telemetry_modes) == (True, 0, 0)
        assert (radio.advert_location, radio.tuning, radio.name) == (0, (250, 9000), "Gateway")
        assert setters(radio) == [  # one frame per command: one preferences write each
            bytes([38, 1, 0, 0]),
            bytes([58, 0]),
            codec.set_tuning_params(250, 9000),
            bytes([8]) + b"Gateway",
        ]
        report = manager.report
        assert report is not None
        assert report.written.keys() == {*OBSERVER, policy.AIRTIME_FACTOR, policy.NAME}
        assert settings_document(report, START_TIME)["status"] == "written"
        again = RadioProfile(ProfileConfig(name="Gateway"), records)  # a restart: the state file
        await connect(radio, again)
        assert setters(radio) == []
        assert again.report is not None
        assert settings_document(again.report, START_TIME)["status"] == "ok"

    asyncio.run(main())


def test_a_shared_command_keeps_the_values_it_does_not_change() -> None:
    radio = factory_radio(manual_add=True, telemetry_modes=0)  # only the advert policy differs
    asyncio.run(connect(radio, RadioProfile(ProfileConfig())))
    assert [p for p in radio.received if p[0] == Command.SET_OTHER_PARAMS] == [bytes([38, 1, 0, 0])]


def test_a_renamed_radio_keeps_its_name(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    radio = factory_radio()
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))
    manager = RadioProfile(ProfileConfig(name="Gateway"), records)

    async def main() -> None:
        await connect(radio, RadioProfile(ProfileConfig(name="Gateway"), records))
        radio.name, radio.telemetry_modes = "Kitchen radio", 0x01  # the phone app, meanwhile
        with caplog.at_level(logging.INFO):
            await connect(radio, manager)

    asyncio.run(main())
    assert setters(radio) == [bytes([38, 1, 0, 0])]  # only the telemetry is set back
    assert radio.name == "Kitchen radio"
    assert "changed on the radio, set back" in caplog.text
    assert manager.report is not None
    document = settings_document(manager.report, START_TIME)
    assert document["settings"]["name"] == {
        "live": "Kitchen radio", "desired": "Gateway", "state": "drift",
    }  # fmt: skip
    assert document["status"] == "drift"


def test_untested_firmware_is_only_observed(caplog: pytest.LogCaptureFixture) -> None:
    radio = factory_radio(firmware_code=15)
    manager = RadioProfile(ProfileConfig())
    asyncio.run(connect(radio, manager))
    assert setters(radio) == []
    assert "is not tested; observed only" in caplog.text
    assert manager.report is not None
    document = settings_document(manager.report, START_TIME)
    assert (document["status"], document["observing"]) == ("differs", True)
    assert document["settings"]["manual_add"] == {
        "live": False,
        "desired": True,
        "state": "differs",
    }
    assert document["settings"]["telemetry"]["live"] == {
        "base": "contacts", "location": "contacts", "environment": "contacts",
    }  # fmt: skip


def test_a_setter_the_firmware_lacks_is_marked_unsupported(tmp_path: Path) -> None:
    radio = factory_radio(unknown={Command.SET_AUTOADD_CONFIG})
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))
    manager = RadioProfile(ProfileConfig(), records)

    async def main() -> None:
        await connect(radio, manager)
        assert manager.report is not None
        assert manager.report.unsupported == (policy.AUTOADD,)
        await connect(radio, RadioProfile(ProfileConfig(), records))
        assert setters(radio) == []  # not tried again on this firmware

    asyncio.run(main())


def test_a_value_the_radio_ignores_is_given_up(caplog: pytest.LogCaptureFixture) -> None:
    radio = factory_radio(ignored={Command.SET_TUNING_PARAMS})
    manager = RadioProfile(ProfileConfig())  # one run: its memory, no state file

    async def main() -> None:
        for _ in range(policy.NOT_CONVERGING_AFTER + 1):
            await connect(radio, manager)

    asyncio.run(main())
    assert setters(radio) == []
    assert "does not take 9000 after 3 connects" in caplog.text
    assert manager.report is not None
    assert manager.report.not_converging == (policy.AIRTIME_FACTOR,)
    assert settings_document(manager.report, START_TIME)["status"] == "problem"


def test_observe_mode_never_writes() -> None:
    radio = factory_radio()
    asyncio.run(connect(radio, RadioProfile(ProfileConfig(manage="observe"))))
    assert setters(radio) == []


def test_protected_contacts_are_kept_on_the_radio_as_favourites() -> None:
    alice = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
    radio = EmulatedRadio(contacts=[alice])
    operator = "b2" * 32
    manager = RadioProfile(ProfileConfig(), protected={alice.public_key.hex(): "alice",
                                                       operator: "operator"})  # fmt: skip

    async def main() -> None:
        session = await connect(radio, manager)
        flags = {c.public_key.hex(): (c.name, c.flags) for c in radio.contacts}
        assert flags == {
            alice.public_key.hex(): ("Alice", ContactFlag.FAVOURITE),
            operator: ("operator", ContactFlag.FAVOURITE),
        }
        assert set(session.contact_records) == set(flags)  # read again after the change
        assert manager.report is not None
        assert manager.report.contacts_added == (operator,)
        assert manager.report.contacts_favoured == (alice.public_key.hex(),)
        await connect(radio, manager)
        assert setters(radio) == []

    asyncio.run(main())


def test_protected_contacts_only_reported_while_observing() -> None:
    radio = EmulatedRadio()
    manager = RadioProfile(ProfileConfig(manage="observe"), protected={"b2" * 32: "operator"})
    off = RadioProfile(ProfileConfig(protect_contacts=False), protected={"b2" * 32: "x"})

    async def main() -> None:
        await connect(radio, manager)
        assert radio.contacts == []
        assert manager.report is not None
        assert manager.report.contacts_missing == ("b2" * 32,)
        await connect(radio, off)
        assert radio.contacts == []

    asyncio.run(main())


def test_a_bug_in_the_profile_does_not_keep_the_radio_away(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken(RadioProfile):
        async def before_contacts(self, session: CompanionSession) -> None:
            raise KeyError("bug")

    session = asyncio.run(connect(EmulatedRadio(), Broken(ProfileConfig())))
    assert session.info is not None
    assert session.info.self_info.public_key == SELF_KEY
    assert "radio profile: before_contacts failed" in caplog.text


def test_unsupported_wins_over_failed_and_an_unreported_setting_is_unknown() -> None:
    radio = factory_radio(unknown={Command.SET_AUTOADD_CONFIG}, tuning=None)
    manager = RadioProfile(ProfileConfig())
    asyncio.run(connect(radio, manager))
    assert manager.report is not None
    assert (manager.report.failed, manager.report.unsupported) == ((), (policy.AUTOADD,))
    settings = settings_document(manager.report, START_TIME)["settings"]
    assert settings["autoadd"]["state"] == "unsupported"
    assert settings["duty_cycle_percent"] == {"live": None, "desired": 10.0, "state": "unknown"}


def test_a_protected_contact_the_radio_refuses_is_a_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    radio = EmulatedRadio(errors={Command.ADD_UPDATE_CONTACT: 3})  # ERR 3: the table is full
    manager = RadioProfile(ProfileConfig(), protected={"b2" * 32: "operator"})
    asyncio.run(connect(radio, manager))
    assert manager.report is not None
    document = settings_document(manager.report, START_TIME)
    assert document["protected_contacts"]["failed"] == ["b2" * 32]
    assert document["status"] == "problem"
    assert "not written: table_full (the contact table is full)" in caplog.text


def test_drift_and_not_converging_are_reported_once_per_run() -> None:
    from meshcore_home_gateway.mqtt.topics import Topics
    from meshcore_home_gateway.observer import Observer

    class Bus:
        def __init__(self) -> None:
            self.events: list[str] = []

        def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
            if topic.endswith("/setting"):
                self.events.append(json.loads(payload)["event_type"])

    bus = Bus()
    observer = Observer(bus, Topics("meshcore", "radio1"), wall_clock=lambda: START_TIME)
    report = policy.ProfileReport(
        "auto", 14, True, False, live={policy.NAME: "Kitchen"}, desired={policy.NAME: "A"},
        drift={policy.NAME: "Kitchen"}, not_converging=(policy.AIRTIME_FACTOR,),
    )  # fmt: skip
    observer.profile_applied(report)
    observer.profile_applied(report)  # the next connect finds the same
    assert bus.events == ["not_converging", "drift"]
    observer.profile_applied(replace(report, drift={policy.NAME: "Hall"}))
    assert bus.events == ["not_converging", "drift", "drift"]


def test_events_are_reported_per_radio() -> None:
    """A replaced radio with the same drift reports it again; so do written settings."""
    from meshcore_home_gateway.mqtt.topics import Topics
    from meshcore_home_gateway.observer import Observer

    events: list[tuple[str, str | None]] = []

    class Bus:
        def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
            if topic.endswith("/setting"):
                event = json.loads(payload)
                events.append((event["event_type"], event["reason"]))

    observer = Observer(Bus(), Topics("meshcore", "radio1"), wall_clock=lambda: START_TIME)
    report = policy.ProfileReport(
        "auto", 14, True, False, live={policy.NAME: "Kitchen", policy.AIRTIME_FACTOR: 1000},
        desired={policy.NAME: "A", policy.AIRTIME_FACTOR: 9000}, drift={policy.NAME: "Kitchen"},
        not_taken=(policy.AIRTIME_FACTOR,), reasons={policy.AIRTIME_FACTOR: "initial"},
        radio="aa" * 32,
    )  # fmt: skip
    observer.profile_applied(report)
    observer.profile_applied(replace(report, radio="bb" * 32))
    assert events == [("not_taken", "initial"), ("drift", None)] * 2


# --- the stored state ----------------------------------------------------------------------------


def test_what_was_applied_survives_a_restart(tmp_path: Path) -> None:
    applied = Applied(
        values={policy.MANUAL_ADD: True, policy.NAME: "Gateway"},
        attempts={policy.AIRTIME_FACTOR: policy.Attempt(9000, 2, 14)},
        unsupported={policy.AUTOADD: 14},
    )
    ProfileRecords(JsonStateFile(tmp_path / "state.json")).save("aa" * 32, applied)
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))
    assert records.load("aa" * 32) == applied
    assert records.load("bb" * 32) == Applied()  # another radio starts afresh


@pytest.mark.parametrize(
    "attempt",
    [
        {"value": 9000, "connects": 2},  # written by a version without the firmware
        {"value": 9000, "connects": 2, "firmware": "14"},
        {"value": [9000], "connects": 2, "firmware": 14},
        {"value": 9000, "connects": True, "firmware": 14},
        {"value": 9000, "connects": 0, "firmware": 14},
        "9000",
    ],
)
def test_a_malformed_attempt_is_dropped(tmp_path: Path, attempt: object) -> None:
    entry = {
        "values": {policy.MANUAL_ADD: True, policy.NAME: None},
        "attempts": {policy.AIRTIME_FACTOR: attempt},
        "unsupported": {policy.AUTOADD: 14},
    }
    (tmp_path / "state.json").write_text(json.dumps({"radio_profile": {"aa" * 32: entry}}))
    applied = ProfileRecords(JsonStateFile(tmp_path / "state.json")).load("aa" * 32)
    assert applied == Applied({policy.MANUAL_ADD: True}, {}, {policy.AUTOADD: 14})


def test_a_radio_that_goes_away_after_a_setter_counts_as_an_attempt(tmp_path: Path) -> None:
    """The attempt is stored before the setters: a radio that resets at the setter cannot keep the
    gateway writing forever."""
    radio = factory_radio(drop_on={Command.SET_TUNING_PARAMS})
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))

    async def main() -> None:
        with pytest.raises(RadioError):  # the connection is gone
            await connect(radio, RadioProfile(ProfileConfig(), records))
        attempts = records.load(SELF_KEY.hex()).attempts
        assert attempts[policy.AIRTIME_FACTOR] == policy.Attempt(9000, 1, 14)
        sent = [policy.MANUAL_ADD, policy.TELEMETRY, policy.ADVERT_LOCATION, policy.AUTOADD,
                policy.AIRTIME_FACTOR]  # fmt: skip
        assert {k: a.connects for k, a in attempts.items()} == dict.fromkeys(
            sent, 1
        )  # not read back
        radio.opened = False
        await connect(radio, RadioProfile(ProfileConfig(), records))  # a restart, the radio back
        assert radio.tuning == (250, 9000)
        assert records.load(SELF_KEY.hex()).attempts == {}

    asyncio.run(main())


def test_a_given_up_value_is_tried_again_on_other_firmware() -> None:
    want, live = policy.desired(869_618), {**OBSERVER, policy.AIRTIME_FACTOR: 1000}
    applied = Applied(attempts={policy.AIRTIME_FACTOR: policy.Attempt(9000, 3, 14)})
    assert plan_for(want, live, applied).not_converging == (policy.AIRTIME_FACTOR,)
    upgraded = plan_for(want, live, applied, mode=Mode.ALWAYS, firmware=15)
    assert upgraded.writes == {policy.AIRTIME_FACTOR: 9000}
    pending = policy.pending(applied, upgraded.writes, firmware_code=15)
    assert pending.attempts[policy.AIRTIME_FACTOR] == policy.Attempt(9000, 1, 15)


def test_initial_only_for_a_radio_without_a_stored_entry() -> None:
    want = policy.desired(869_618)
    assert set(plan_for(want, FACTORY).reasons.values()) == {"initial"}
    known = Applied(values={policy.MANUAL_ADD: True})
    reasons = plan_for(want, FACTORY, known).reasons
    assert reasons[policy.MANUAL_ADD] == "drift"
    assert reasons[policy.TELEMETRY] == "configured"  # not yet applied on this radio


def test_a_write_that_does_not_read_back_is_not_taken(tmp_path: Path) -> None:
    radio = factory_radio(ignored={Command.SET_TUNING_PARAMS})
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))
    manager = RadioProfile(ProfileConfig(), records)
    asyncio.run(connect(radio, manager))
    report = manager.report
    assert report is not None
    assert report.not_taken == (policy.AIRTIME_FACTOR,)
    assert policy.AIRTIME_FACTOR not in report.written
    document = settings_document(report, START_TIME)
    assert document["settings"]["duty_cycle_percent"]["state"] == "not_taken"
    assert document["status"] == "problem"
    assert records.load(SELF_KEY.hex()).attempts[policy.AIRTIME_FACTOR].connects == 1


def test_observing_reports_protected_contacts_that_are_missing_or_no_favourite() -> None:
    alice = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
    radio = EmulatedRadio(contacts=[alice])  # has the observer profile
    protected = {alice.public_key.hex(): "alice", "b2" * 32: "operator"}
    manager = RadioProfile(ProfileConfig(manage="observe"), protected=protected)
    asyncio.run(connect(radio, manager))
    assert setters(radio) == []
    assert manager.report is not None
    document = settings_document(manager.report, START_TIME)
    assert document["protected_contacts"]["missing"] == ["b2" * 32]
    assert document["protected_contacts"]["not_favourite"] == ["a1" * 32]
    assert document["status"] == "differs"


def test_only_the_setters_sent_count_when_the_radio_goes_away(tmp_path: Path) -> None:
    """The radio resets at the first setter: the setters after it were never sent, so they are
    not given up after three such connects."""
    radio = factory_radio()
    records = ProfileRecords(JsonStateFile(tmp_path / "state.json"))
    other = {policy.MANUAL_ADD, policy.TELEMETRY, policy.ADVERT_LOCATION}  # SET_OTHER_PARAMS

    async def main() -> None:
        for connects in range(1, policy.NOT_CONVERGING_AFTER + 1):
            radio.drop_on, radio.opened = {Command.SET_OTHER_PARAMS}, False
            with pytest.raises(RadioError):
                await connect(radio, RadioProfile(ProfileConfig(), records))
            attempts = records.load(SELF_KEY.hex()).attempts
            assert {k: a.connects for k, a in attempts.items()} == dict.fromkeys(other, connects)
        radio.opened = False
        manager = RadioProfile(ProfileConfig(), records)
        await connect(radio, manager)
        assert [p[0] for p in setters(radio)] == [Command.SET_AUTOADD_CONFIG,
                                                  Command.SET_TUNING_PARAMS]  # fmt: skip
        assert (radio.autoadd_config, radio.tuning) == (0, (250, 9000))
        assert manager.report is not None
        assert set(manager.report.not_converging) == other

    asyncio.run(main())


def test_a_refused_setting_keeps_its_reason() -> None:
    radio = factory_radio(errors={Command.SET_AUTOADD_CONFIG: 6})
    manager = RadioProfile(ProfileConfig())
    asyncio.run(connect(radio, manager))
    assert manager.report is not None
    assert manager.report.failed == (policy.AUTOADD,)
    assert manager.report.reasons[policy.AUTOADD] == "initial"


def test_a_connect_without_its_own_report_writes_no_contacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in the settings step leaves this connect without a report: the contacts step does
    not act on the previous connect's (here another radio's, on untested firmware)."""
    manager = RadioProfile(ProfileConfig(), protected={"b2" * 32: "operator"})

    def bug(*args: object, **kwargs: object) -> policy.Plan:
        raise KeyError("bug")

    async def main() -> None:
        await connect(EmulatedRadio(), manager)
        assert len(manager.take_reports()) == 1
        monkeypatch.setattr(policy, "plan", bug)
        untested = EmulatedRadio(firmware_code=15)
        await connect(untested, manager)
        assert Command.ADD_UPDATE_CONTACT not in untested.commands()
        assert manager.take_reports() == []

    asyncio.run(main())


def test_a_state_file_that_cannot_be_read_does_not_stop_the_profile(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """At the radio's second connect of a run, its memory is in the process: the file is read
    only to store, and a file that cannot be read is logged; the connect keeps its report."""
    radio = factory_radio()
    state = tmp_path / "state.json"
    manager = RadioProfile(ProfileConfig(), ProfileRecords(JsonStateFile(state)),
                           protected={"b2" * 32: "operator"})  # fmt: skip

    async def main() -> None:
        await connect(radio, manager)
        assert len(manager.take_reports()) == 1
        state.unlink()
        state.mkdir()  # cannot be read as a file any more
        radio.telemetry_modes, radio.contacts = 0x15, []  # something to write
        await connect(radio, manager)

    asyncio.run(main())
    assert "cannot store the radio profile" in caplog.text
    assert "before_contacts failed" not in caplog.text
    (report,) = manager.take_reports()
    assert report.written == {policy.TELEMETRY: 0}
    assert report.contacts_added == ("b2" * 32,)


def test_a_name_is_cut_between_characters() -> None:
    assert codec.radio_name("x" * 32) == "x" * 31
    assert codec.radio_name("x" * 30 + "ä") == "x" * 30  # the second byte of "ä" would not fit
    assert codec.new_contact(b"\x01" * 32, "x" * 30 + "ä", 1)[36 + 64 : 36 + 64 + 32] == (
        b"x" * 30 + b"\x00\x00"
    )


def test_a_protected_contact_with_a_look_alike_on_the_radio_is_not_added(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The radio could not tell them apart: the look-alike would receive the protected contact's
    messages and could pass for it (ADR 0009)."""
    twin = EmulatedContact(bytes.fromhex("b2" * 6 + "ee" * 26), "Bob?")
    radio = EmulatedRadio(contacts=[twin])
    manager = RadioProfile(ProfileConfig(), protected={"b2" * 32: "operator"})
    asyncio.run(connect(radio, manager))
    assert [c.public_key for c in radio.contacts] == [twin.public_key]
    assert manager.report is not None
    document = settings_document(manager.report, START_TIME)
    assert document["protected_contacts"]["failed"] == ["b2" * 32]
    assert "not added: b2b2b2b2b2b2 on the radio has the same prefix" in caplog.text
