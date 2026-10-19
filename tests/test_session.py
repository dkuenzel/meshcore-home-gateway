"""Companion session against the emulated radio: handshake, commands, pushes, failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest

from meshcore_bus.core.model import MessageKind
from meshcore_bus.protocol import codec
from meshcore_bus.protocol.codes import Command, Push, Response
from meshcore_bus.radio import session as session_module
from meshcore_bus.radio.session import (
    AckReceived,
    CommandFailedError,
    CompanionSession,
    MessageReceived,
    PacketLogged,
    RadioError,
    RadioEvent,
)
from tests.radio_emulator import SELF_KEY, START_TIME, EmulatedContact, EmulatedRadio

ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
BOB = EmulatedContact(bytes.fromhex("b2" * 32), "Bob", out_path_len=0x02)


async def start_only(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
    await session.start()


def run(
    radio: EmulatedRadio,
    scenario: Callable[[CompanionSession, asyncio.Queue[RadioEvent]], Awaitable[None]],
    now: float = START_TIME + 100,
    timeout_s: float = 0.3,
) -> None:
    async def main() -> None:
        events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        session = CompanionSession(
            radio, events, wall_clock=lambda: now, command_timeout_s=timeout_s, flush_settle_s=0.01
        )
        try:
            await asyncio.wait_for(scenario(session, events), 5)
        finally:
            await session.close()

    asyncio.run(main())


def test_start_flushes_then_identifies_and_loads_tables() -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], channels={0: "Public", 2: "Alerts"})

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        info = await session.start()
        assert radio.raw_written.startswith(bytes(256))  # resynchronisation before anything
        assert info.name == "Bridge"
        assert info.device.version == "v1.18.0"
        assert info.self_info.spreading_factor == 8
        assert set(session.contact_records) == {ALICE.public_key.hex(), BOB.public_key.hex()}
        assert session.channels == {0: "Public", 2: "Alerts"}  # empty slots are left out
        assert session.battery == codec.Battery(4012, 12, 976)
        assert radio.commands()[:4] == [
            Command.APP_START,
            Command.DEVICE_QUERY,
            Command.GET_AUTOADD_CONFIG,
            Command.GET_DEVICE_TIME,
        ]

    run(radio, scenario)


def test_clock_is_moved_forward_only() -> None:
    behind = EmulatedRadio(clock=START_TIME)
    run(behind, start_only, now=START_TIME + 100)
    assert behind.clock == START_TIME + 100

    ahead = EmulatedRadio(clock=START_TIME + 1000)
    run(ahead, start_only, now=START_TIME)
    assert Command.SET_DEVICE_TIME not in ahead.commands()


def test_queued_messages_are_drained_at_start_in_both_layouts() -> None:
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "early", START_TIME, v3=False, push=False)
    radio.queue_channel_message(0, "Carol: hi", START_TIME + 1, push=False)

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        first, second = events.get_nowait(), events.get_nowait()
        assert isinstance(first, MessageReceived)
        assert isinstance(second, MessageReceived)
        assert (first.message.kind, first.message.text) == (MessageKind.CONTACT, "early")
        assert (second.message.kind, second.message.channel_index) == (MessageKind.CHANNEL, 0)

    run(radio, scenario)


def test_message_waiting_push_triggers_a_sync() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.queue_direct_message(ALICE.public_key[:6], "status", START_TIME + 50)
        event = await asyncio.wait_for(events.get(), 2)
        assert isinstance(event, MessageReceived)
        assert event.message.text == "status"
        assert event.message.sender_prefix == ALICE.public_key[:6].hex()

    run(radio, scenario)


def test_send_text_returns_the_expected_ack_and_the_confirmation_arrives_as_event() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        sent = await session.send_text(ALICE.public_key[:6], "Gate open", START_TIME + 100, 0)
        assert sent.flood
        assert sent.expected_ack == codec.expected_ack(START_TIME + 100, 0, "Gate open", SELF_KEY)
        event = await asyncio.wait_for(events.get(), 2)
        assert event == AckReceived(sent.expected_ack, 1500)

    run(radio, scenario)


def test_radio_errors_become_command_failures() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.errors[Command.SEND_TXT_MSG] = 3
        with pytest.raises(CommandFailedError, match="table_full"):
            await session.send_text(ALICE.public_key[:6], "x", START_TIME, 0)
        with pytest.raises(CommandFailedError, match="not_found"):
            await session.send_text(bytes.fromhex("c3" * 6), "x", START_TIME, 0)
        assert not session.closed  # an error answer is not a broken connection

    run(radio, scenario)


def test_pushes_inside_the_contacts_stream_are_still_handled() -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.emit(bytes([Response.CONTACTS_START, 2, 0, 0, 0]))  # a stale frame from before
        radio.emit(bytes([Push.LOG_RX_DATA, 0x1F, 0x9F]) + b"\x0d\x00")
        await session.refresh_contacts()
        event = await asyncio.wait_for(events.get(), 2)
        assert isinstance(event, PacketLogged)
        assert (event.rx.snr_db, event.rx.rssi_dbm) == (7.75, -97)
        assert len(session.contact_records) == 2

    run(radio, scenario)


def test_noise_and_unexpected_frames_do_not_confuse_commands() -> None:
    radio = EmulatedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.emit_raw(b"debug text\r\n")
        radio.emit(bytes([0x09]) + bytes(4))  # a CURR_TIME nobody asked for
        battery = await session.get_battery()
        assert battery.millivolts == 4012

    run(radio, scenario)


def test_silence_times_out_and_closes_the_session() -> None:
    radio = EmulatedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.silent.add(Command.GET_BATT_AND_STORAGE)
        with pytest.raises(RadioError, match="no answer"):
            await session.get_battery()
        assert session.closed
        with pytest.raises(RadioError, match="no answer"):
            await session.wait_closed()
        with pytest.raises(RadioError, match="closed"):
            await session.get_battery()

    run(radio, scenario)


def test_unplugging_ends_the_session_with_an_error() -> None:
    radio = EmulatedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.disconnect()
        with pytest.raises(RadioError, match="unplugged"):
            await asyncio.wait_for(session.wait_closed(), 2)

    run(radio, scenario)


def test_advert_marks_contacts_for_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.0)
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.contacts.append(BOB)
        radio.emit(bytes([Push.ADVERT]) + BOB.public_key)
        for _attempt in range(100):
            if BOB.public_key.hex() in session.contact_records:
                break
            await asyncio.sleep(0.01)
        assert BOB.public_key.hex() in session.contact_records
        assert session.contacts.resolve(BOB.public_key[:6].hex())[0].name == "Bob"

    run(radio, scenario)


UNKNOWN_AUTOADD: set[int] = {Command.GET_AUTOADD_CONFIG}  # older firmware: unknown command


@pytest.mark.parametrize(
    ("radio", "warning"),
    [
        (EmulatedRadio(manual_add=False), "adds every new contact automatically"),
        (EmulatedRadio(autoadd_config=0x02), "adds new contacts of type chat automatically"),
        (EmulatedRadio(autoadd_config=0x1C), "of type repeater, room, sensor automatically"),
        (EmulatedRadio(manual_add=False, autoadd_config=0x01), "may overwrite its oldest contact"),
        (EmulatedRadio(manual_add=False, unknown=UNKNOWN_AUTOADD), "may overwrite its oldest"),
    ],
    ids=["all", "chat-in-manual-mode", "other-types", "overwrite", "unknown-settings"],
)
def test_automatic_contact_adding_is_warned_about(
    caplog: pytest.LogCaptureFixture, radio: EmulatedRadio, warning: str
) -> None:
    """The radio passes on direct messages from contacts of every type (BaseChatMesh.cpp)."""
    caplog.set_level(logging.WARNING)
    run(radio, start_only)
    assert warning in caplog.text


@pytest.mark.parametrize(
    "radio",
    [EmulatedRadio(), EmulatedRadio(autoadd_config=0x01), EmulatedRadio(unknown=UNKNOWN_AUTOADD)],
    ids=["no-types", "overwrite-without-types", "older-firmware"],
)
def test_manual_adding_without_automatic_types_is_quiet(
    caplog: pytest.LogCaptureFixture, radio: EmulatedRadio
) -> None:
    caplog.set_level(logging.WARNING)
    run(radio, start_only)
    assert "ADR 0009" not in caplog.text


@pytest.mark.parametrize(
    ("radio", "may_overwrite"),
    [
        (EmulatedRadio(), False),
        (EmulatedRadio(manual_add=False), False),  # a full table refuses new contacts
        (EmulatedRadio(manual_add=False, autoadd_config=0x01), True),
        (EmulatedRadio(autoadd_config=0x01), False),  # nothing is added, so nothing overwritten
        (EmulatedRadio(autoadd_config=0x03), True),
        (EmulatedRadio(autoadd_config=0x11), True),  # a look-alike can advertise as a sensor
        (EmulatedRadio(autoadd_config=0x41), True),  # unknown type bits count as types
        (EmulatedRadio(manual_add=False, unknown=UNKNOWN_AUTOADD), True),
        (EmulatedRadio(unknown=UNKNOWN_AUTOADD), False),
    ],
    ids=[
        "manual", "automatic", "automatic-overwrite", "manual-overwrite", "chat-overwrite",
        "sensor-overwrite", "unknown-type-overwrite", "automatic-unknown", "manual-unknown",
    ],
)  # fmt: skip
def test_radio_settings_that_let_it_overwrite_contacts(
    radio: EmulatedRadio, may_overwrite: bool
) -> None:
    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        assert session.may_overwrite_contacts is may_overwrite

    run(radio, scenario)


def test_firmware_without_auto_add_settings_still_starts() -> None:
    radio = EmulatedRadio(unknown=UNKNOWN_AUTOADD)

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        assert session.autoadd is None

    run(radio, scenario)


def test_overwritten_contacts_are_remembered() -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.overwrite(BOB)
        for _attempt in range(100):
            if session.departed:
                break
            await asyncio.sleep(0.01)
        assert session.take_departed() == [BOB.public_key.hex()]
        assert session.take_departed() == []

    run(radio, scenario)


def test_messages_fetched_at_connect_are_marked() -> None:
    """They may have arrived while no session watched the contact table (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "waited", START_TIME + 10, push=False)

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.queue_direct_message(ALICE.public_key[:6], "live", START_TIME + 50)
        fetched = [await asyncio.wait_for(events.get(), 2) for _ in range(2)]
        assert [
            (event.message.text, event.at_connect)
            for event in fetched
            if isinstance(event, MessageReceived)
        ] == [("waited", True), ("live", False)]

    run(radio, scenario)


def test_messages_name_their_session_and_when_they_were_fetched() -> None:
    """The bridge judges a command on the table of the session that fetched it, and reads that
    table again only if no read started after the fetch."""
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.queue_direct_message(ALICE.public_key[:6], "status", START_TIME + 50)
        event = await asyncio.wait_for(events.get(), 2)
        assert isinstance(event, MessageReceived)
        assert event.session is session
        assert session.contacts_read_at < event.fetched_at
        await session.refresh_contacts()
        assert session.contacts_read_at > event.fetched_at

    run(radio, scenario)


def test_cancelled_command_leaves_no_task_behind() -> None:
    radio = EmulatedRadio(silent={Command.GET_BATT_AND_STORAGE})

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        radio.silent.clear()
        await session.start()
        radio.silent.add(Command.GET_BATT_AND_STORAGE)
        waiting = asyncio.create_task(session.get_battery())
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0)
        frame_waits = [task for task in asyncio.all_tasks() if "Queue.get" in repr(task)]
        assert frame_waits == []  # the waiter for the answer was cancelled too

    run(radio, scenario, timeout_s=5)


def test_channel_send_and_path_reset() -> None:
    radio = EmulatedRadio(contacts=[BOB])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        await session.send_channel_text(0, "hello all", START_TIME + 1)
        await session.reset_path(BOB.public_key)
        assert radio.channel_texts == [(0, "hello all", START_TIME + 1)]
        assert radio.path_resets == [BOB.public_key]

    run(radio, scenario)


def test_firmware_before_version_9_sends_a_shorter_device_info() -> None:
    radio = EmulatedRadio()
    full = radio._device_info
    radio._device_info = lambda: full()[:80]  # type: ignore[method-assign] # no v9+/v10+ fields

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        info = await session.start()
        assert (info.device.version, info.device.path_hash_mode) == ("v1.18.0", 0)

    run(radio, scenario)


def test_malformed_answer_ends_the_start_with_a_radio_error() -> None:
    radio = EmulatedRadio()
    radio._device_info = lambda: bytes([Response.DEVICE_INFO, 14, 50])  # type: ignore[method-assign]

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        with pytest.raises(RadioError, match="device query"):
            await session.start()

    run(radio, scenario)


def test_malformed_frame_during_maintenance_ends_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.01)
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")
    radio = EmulatedRadio(contacts=[carol])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.contacts[0].record = lambda code=Response.CONTACT: bytes([code]) + bytes(10)  # type: ignore[method-assign]
        radio.emit(bytes([Push.ADVERT]) + carol.public_key)
        with pytest.raises(RadioError, match="malformed"):
            await asyncio.wait_for(session.wait_closed(), 2)

    run(radio, scenario)


def test_close_passes_on_the_callers_cancellation() -> None:
    """A shutdown that arrives while the session closes must not be swallowed."""
    radio = EmulatedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        closer = asyncio.create_task(session.close())
        await asyncio.sleep(0)  # the closer now waits for the session's tasks
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert not radio.opened  # the transport is closed all the same

    run(radio, scenario)


def test_disabled_feature_is_a_command_failure_with_its_own_reason() -> None:
    radio = EmulatedRadio(disabled={Command.SEND_CHANNEL_TXT_MSG})

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        with pytest.raises(CommandFailedError) as caught:
            await session.send_channel_text(0, "x", START_TIME)
        assert caught.value.reason == "disabled"

    run(radio, scenario)
