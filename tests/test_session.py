"""Companion session against the emulated radio: handshake, commands, pushes, failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest

from meshcore_home_gateway.core.model import MessageKind, Position
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Command, Push, Response
from meshcore_home_gateway.protocol.framing import encode_frame
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.session import (
    AckReceived,
    AdvertHeard,
    CommandFailedError,
    CompanionSession,
    ContactsRead,
    MessageReceived,
    PacketLogged,
    RadioError,
    RadioEvent,
    StatsRead,
)
from meshcore_home_gateway.radio.transport import TcpTransport
from tests.radio_emulator import (
    SELF_KEY,
    START_TIME,
    EmulatedContact,
    EmulatedRadio,
    TcpRadioServer,
    WifiFrameParser,
    frame,
)

ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
BOB = EmulatedContact(bytes.fromhex("b2" * 32), "Bob", out_path_len=0x02)


WATCHED = (MessageReceived, AckReceived, PacketLogged)


async def next_event(events: asyncio.Queue[RadioEvent]) -> RadioEvent:
    """The next message, acknowledgement or packet log; most tests skip table reads, adverts and
    statistics."""
    while True:
        event = await asyncio.wait_for(events.get(), 2)
        if isinstance(event, WATCHED):
            return event


def queued(
    events: asyncio.Queue[RadioEvent], kinds: tuple[type, ...] = WATCHED
) -> list[RadioEvent]:
    """The events of these kinds already in the queue, which is emptied."""
    everything = [events.get_nowait() for _ in range(events.qsize())]
    return [event for event in everything if isinstance(event, kinds)]


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
        assert session.tuning == codec.TuningParams(0, 1000)
        assert radio.commands()[:5] == [
            Command.APP_START,
            Command.DEVICE_QUERY,
            Command.GET_AUTOADD_CONFIG,
            Command.GET_TUNING_PARAMS,
            Command.GET_DEVICE_TIME,
        ]

    run(radio, scenario)


class StaleRadio(EmulatedRadio):
    """Answers the flush with what a radio may still have queued for an earlier client: a push
    and the start of a frame whose rest never comes."""

    async def write(self, data: bytes) -> None:
        if data == bytes(len(data)):  # the flush
            self.acknowledge(b"\x01\x02\x03\x04")
            self.emit_raw(frame(bytes([Response.SELF_INFO]) + bytes(9))[:6])
        await super().write(data)


def test_whatever_arrives_during_the_flush_is_dropped() -> None:
    radio = StaleRadio()

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        info = await session.start()  # the partial frame does not swallow the first answer
        assert info.name == "Bridge"
        assert not queued(events)  # the stale acknowledgement is not passed on

    run(radio, scenario)


def test_clock_is_moved_forward_only() -> None:
    behind = EmulatedRadio(clock=START_TIME)
    run(behind, start_only, now=START_TIME + 100)
    assert behind.clock == START_TIME + 100

    ahead = EmulatedRadio(clock=START_TIME + 1000)
    run(ahead, start_only, now=START_TIME)
    assert Command.SET_DEVICE_TIME not in ahead.commands()


def test_clock_is_not_read_without_set_clock() -> None:
    radio = EmulatedRadio(clock=START_TIME)

    async def main() -> None:
        events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        session = CompanionSession(
            radio, events, wall_clock=lambda: START_TIME + 100, command_timeout_s=0.3,
            flush_settle_s=0.01, set_clock=False,
        )  # fmt: skip
        try:
            await session.start()
        finally:
            await session.close()

    asyncio.run(main())
    assert Command.GET_DEVICE_TIME not in radio.commands()
    assert radio.clock == START_TIME


def test_queued_messages_are_drained_at_start_in_both_layouts() -> None:
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "early", START_TIME, v3=False, push=False)
    radio.queue_channel_message(0, "Carol: hi", START_TIME + 1, push=False)

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        first, second = queued(events)
        assert isinstance(first, MessageReceived)
        assert isinstance(second, MessageReceived)
        assert (first.message.kind, first.message.text) == (MessageKind.CONTACT, "early")
        assert (second.message.kind, second.message.channel_index) == (MessageKind.CHANNEL, 0)

    run(radio, scenario)


def test_channel_data_and_undecodable_messages_are_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "first", START_TIME, push=False)
    radio.queue_direct_message(ALICE.public_key[:6], "?", START_TIME, text_type=9, push=False)
    radio.queue.append(bytes([Response.CHANNEL_DATA_RECV]) + bytes(12))  # binary, not a text
    radio.queue_direct_message(ALICE.public_key[:6], "last", START_TIME + 1, push=False)

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        fetched = queued(events)
        texts = [event.message.text for event in fetched if isinstance(event, MessageReceived)]
        assert texts == ["first", "last"]  # the batch goes on after both
        assert not session.closed

    caplog.set_level(logging.WARNING)
    run(radio, scenario)
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1  # channel data is skipped quietly: it is no text message
    assert "undecodable message skipped: message frame 0x10" in warnings[0]


def test_messages_fetched_before_a_sync_error_are_still_delivered() -> None:
    """The radio has handed them over; they must not be lost with the batch."""
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "kept", START_TIME, push=False)
    radio.queue.append(bytes([Response.ERR, 4]))  # the next sync is answered with an error

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        with pytest.raises(CommandFailedError, match="sync: bad_state"):
            await session.start()
        (event,) = queued(events)
        assert isinstance(event, MessageReceived)
        assert event.message.text == "kept"

    run(radio, scenario)


def test_message_waiting_push_triggers_a_sync() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.queue_direct_message(ALICE.public_key[:6], "status", START_TIME + 50)
        event = await next_event(events)
        assert isinstance(event, MessageReceived)
        assert event.message.text == "status"
        assert event.message.sender_prefix == ALICE.public_key[:6].hex()

    run(radio, scenario)


def test_periodic_sync_fetches_a_message_whose_push_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "SYNC_INTERVAL_S", 0.05)
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.queue_direct_message(ALICE.public_key[:6], "unannounced", START_TIME + 50, push=False)
        event = await next_event(events)
        assert isinstance(event, MessageReceived)
        assert event.message.text == "unannounced"

    run(radio, scenario)


def test_send_text_returns_the_expected_ack_and_the_confirmation_arrives_as_event() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        sent = await session.send_text(ALICE.public_key[:6], "Gate open", START_TIME + 100, 0)
        assert sent.flood
        assert sent.expected_ack == codec.expected_ack(START_TIME + 100, 0, "Gate open", SELF_KEY)
        event = await next_event(events)
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
        radio.errors[Command.RESET_PATH] = 2  # a command otherwise answered with OK
        with pytest.raises(CommandFailedError, match="reset path: not_found"):
            await session.reset_path(ALICE.public_key)
        assert not session.closed  # an error answer is not a broken connection

    run(radio, scenario)


def test_pushes_inside_the_contacts_stream_are_still_handled() -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.emit(bytes([Response.CONTACTS_START, 2, 0, 0, 0]))  # a stale frame from before
        radio.emit(bytes([Push.LOG_RX_DATA, 0x1F, 0x9F]) + b"\x0d\x00")
        await session.refresh_contacts()
        event = await next_event(events)
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
        radio.emit(bytes([Push.SEND_CONFIRMED, 1]))  # a push too short to decode
        battery = await session.get_battery()
        assert battery.millivolts == 4012
        assert not session.closed

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


def test_battery_heartbeat_notices_a_radio_that_went_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A radio that hangs, or a TCP link that died quietly, raises no error: only an unanswered
    command shows it."""
    monkeypatch.setattr(session_module, "HEARTBEAT_INTERVAL_S", 0.05)
    radio = EmulatedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.silent.add(Command.GET_BATT_AND_STORAGE)
        with pytest.raises(RadioError, match="no answer"):
            await asyncio.wait_for(session.wait_closed(), 2)

    run(radio, scenario)


def test_a_failed_write_ends_the_session() -> None:
    radio = EmulatedRadio()

    async def unplugged(data: bytes) -> None:
        raise ConnectionError("emulated radio: unplugged")

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.write = unplugged  # type: ignore[method-assign]
        with pytest.raises(RadioError, match="write failed: emulated radio: unplugged"):
            await session.get_battery()
        assert session.closed
        with pytest.raises(RadioError, match="write failed"):
            await session.wait_closed()

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


def test_full_contact_table_push_rereads_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.0)
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        reads = radio.commands().count(Command.GET_CONTACTS)
        radio.emit(bytes([Push.CONTACTS_FULL]))
        for _attempt in range(100):
            if radio.commands().count(Command.GET_CONTACTS) > reads:
                break
            await asyncio.sleep(0.01)
        assert radio.commands().count(Command.GET_CONTACTS) == reads + 1

    run(radio, scenario)


UNKNOWN_AUTOADD: set[int] = {Command.GET_AUTOADD_CONFIG}  # older firmware: unknown command


@pytest.mark.parametrize(
    ("radio", "warning"),
    [
        (EmulatedRadio(manual_add=False), "adds every new contact automatically"),
        (EmulatedRadio(manual_add=False, unknown=UNKNOWN_AUTOADD), "adds every new contact"),
        (EmulatedRadio(autoadd_config=0x02), "adds new contacts of type chat automatically"),
        (EmulatedRadio(autoadd_config=0x1C), "of type repeater, room, sensor automatically"),
        (EmulatedRadio(autoadd_config=0x41), "of type 0x40 automatically"),
    ],
    ids=["all", "all-older-firmware", "chat-in-manual-mode", "other-types", "unknown-type"],
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


def test_firmware_without_auto_add_settings_still_starts() -> None:
    radio = EmulatedRadio(unknown=UNKNOWN_AUTOADD)

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        assert session.autoadd is None

    run(radio, scenario)


def test_firmware_without_the_hop_limit_still_starts() -> None:
    """Firmware 1.12 and 1.13 answer the auto-add query without the hop limit."""
    radio = EmulatedRadio(autoadd_config=0x02, autoadd_hops=None)

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        assert session.autoadd == codec.AutoAddConfig(flags=0x02, max_hops=None)

    run(radio, scenario)


def test_every_key_the_radio_reports_is_passed_on() -> None:
    """Keys from table reads and from the pushes that carry one (advert, new advert, path,
    overwrite) reach the callback before any later frame, marked whether the radio holds or held
    the contact: a new advert is from a node it did not add (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    carol, dave, erin = (bytes.fromhex(b * 32) for b in ("c3", "d4", "e5"))

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        keys: list[tuple[str, bool]] = []
        session.on_contact_key = lambda key, held: keys.append((key[:4], held))
        await session.start()
        assert keys == [("a1a1", True), ("b2b2", True)]  # the table read at connect
        radio.emit(bytes([Push.ADVERT]) + carol)
        radio.emit(bytes([Push.NEW_ADVERT]) + dave + bytes(115))
        radio.emit(bytes([Push.PATH_UPDATED]) + erin)
        radio.overwrite(BOB)
        radio.queue_direct_message(ALICE.public_key[:6], "after the pushes", START_TIME + 50)
        event = await next_event(events)
        assert isinstance(event, MessageReceived)
        assert keys[2:6] == [("c3c3", True), ("d4d4", False), ("e5e5", True), ("b2b2", True)]

    run(radio, scenario)


def test_a_read_in_progress_serves_a_request_for_a_newer_one() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        reads = radio.commands().count(Command.GET_CONTACTS)
        mark = session.contacts_read_at
        running = asyncio.create_task(session.refresh_contacts())
        await asyncio.sleep(0)  # it holds the command lock and has sent its request
        await session.refresh_contacts(newer_than=mark)  # waits for it instead of reading
        await running
        assert radio.commands().count(Command.GET_CONTACTS) == reads + 1
        await session.refresh_contacts(newer_than=session.contacts_read_at)  # none newer yet
        assert radio.commands().count(Command.GET_CONTACTS) == reads + 2

    run(radio, scenario)


def test_messages_fetched_at_connect_are_marked() -> None:
    """They may have arrived while no session watched the contact table (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "waited", START_TIME + 10, push=False)

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        radio.queue_direct_message(ALICE.public_key[:6], "live", START_TIME + 50)
        fetched = [await next_event(events) for _ in range(2)]
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
        event = await next_event(events)
        assert isinstance(event, MessageReceived)
        assert event.session is session
        assert session.contacts_read_at < event.fetched_at
        await session.refresh_contacts()
        assert session.contacts_read_at > event.fetched_at

    run(radio, scenario)


def test_cancelled_command_leaves_no_task_behind() -> None:
    radio = EmulatedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        await asyncio.sleep(0)
        before = asyncio.all_tasks()
        radio.silent.add(Command.GET_BATT_AND_STORAGE)
        waiting = asyncio.create_task(session.get_battery())
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0)
        assert asyncio.all_tasks() - before == set()  # its waits for the answer were cancelled

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


def test_a_bug_in_the_maintenance_ends_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Else the session would look alive but never fetch a message again."""
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.0)
    radio = EmulatedRadio(contacts=[ALICE])

    def broken(_key: str, _held: bool) -> None:
        raise KeyError("bug")

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        session.on_contact_key = broken  # fails in the table read after the next push
        radio.emit(bytes([Push.CONTACTS_FULL]))
        with pytest.raises(RadioError, match="maintenance failed: KeyError"):
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


def test_a_serial_flush_would_misalign_the_wifi_firmware_parser() -> None:
    """Why the session sends no flush over TCP: 256 zeros are 85 empty headers plus one byte,
    so the WiFi parser reads every later header one byte off and never sees APP_START."""
    start = encode_frame(codec.app_start("x"))
    assert WifiFrameParser().feed(bytes(256) + start) == []
    assert WifiFrameParser().feed(start) == [codec.app_start("x")]


def test_tcp_session_starts_against_the_wifi_firmware_parser() -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        with TcpRadioServer(radio) as server:
            transport = TcpTransport("127.0.0.1", server.port)
            session = CompanionSession(transport, events, command_timeout_s=1, set_clock=False)
            try:
                info = await asyncio.wait_for(session.start(fetch_messages=False), 5)
            finally:
                await session.close()
        assert info.name == "Bridge"
        assert not radio.raw_written.startswith(b"\x00")  # no flush over TCP

    asyncio.run(main())


def test_a_command_waiting_behind_a_dead_session_is_never_sent() -> None:
    """A command queued on the lock must not reach the radio after the session ended: the
    bridge repeats the attempt on the next session, so the radio would send the text twice."""
    radio = EmulatedRadio(contacts=[ALICE], silent={Command.RESET_PATH})

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        reset = asyncio.create_task(session.reset_path(ALICE.public_key))
        await asyncio.sleep(0)  # the reset holds the lock and waits for an answer that never comes
        send = asyncio.create_task(session.send_text(ALICE.public_key[:6], "hi", START_TIME, 0))
        results = await asyncio.gather(reset, send, return_exceptions=True)
        assert all(isinstance(r, RadioError) for r in results)
        assert "no answer from the radio" in str(results[1])  # the cause, not a bare "closed"
        assert Command.SEND_TXT_MSG not in [payload[0] for payload in radio.received]

    run(radio, scenario)


def test_table_reads_statistics_and_tuning_at_connect() -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        reads = queued(events, (ContactsRead,))
        assert len(reads) == 1
        assert isinstance(reads[0], ContactsRead)
        assert reads[0].records is session.contact_records
        assert reads[0].read_at == START_TIME + 100
        assert session.tuning == codec.TuningParams(0, 1000)
        stats = session.stats
        assert stats is not None
        assert stats.battery == codec.Battery(4012, 12, 976)
        assert stats.core == codec.CoreStats(4012, 3600, 0, 0)
        assert stats.radio is not None
        assert stats.radio.noise_floor_dbm == -118
        assert stats.packets is not None
        assert stats.packets.receive_errors == 2

    run(radio, scenario)


def test_every_table_read_and_heartbeat_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "HEARTBEAT_INTERVAL_S", 0.05)
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        assert len(queued(events, (StatsRead,))) == 1  # at connect
        radio.packets_received = 121
        await asyncio.sleep(0.15)
        beats = queued(events, (StatsRead,))
        assert beats
        last = beats[-1]
        assert isinstance(last, StatsRead)
        assert last.packets is not None
        assert last.packets.received == 121
        await session.refresh_contacts()
        assert len(queued(events, (ContactsRead,))) == 1

    run(radio, scenario)


def test_firmware_without_statistics_and_tuning(caplog: pytest.LogCaptureFixture) -> None:
    """Both answer ERR 1 there (MyMesh.cpp:2059-2061): the session goes on without them and
    does not ask for the statistics again."""
    radio = EmulatedRadio(stats=False, tuning=None)

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        assert session.tuning is None
        stats = await session.read_status()
        assert (stats.core, stats.radio, stats.packets) == (None, None, None)
        assert stats.battery.millivolts == 4012
        assert radio.commands().count(Command.GET_STATS) == 1
        assert len(queued(events, (StatsRead,))) == 2

    caplog.set_level(logging.INFO)
    run(radio, scenario)
    assert "radio: no statistics (firmware without GET_STATS)" in caplog.text


def test_adverts_are_reported_with_the_record_of_a_node_not_added() -> None:
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol", latitude_e6=53_550_000,
                            longitude_e6=9_990_000)  # fmt: skip
    radio = EmulatedRadio(contacts=[ALICE])

    async def scenario(session: CompanionSession, events: asyncio.Queue[RadioEvent]) -> None:
        await session.start()
        queued(events)
        radio.advertise(ALICE)
        radio.advertise(carol)
        heard: list[AdvertHeard] = []
        while len(heard) < 2:
            event = await asyncio.wait_for(events.get(), 2)
            if isinstance(event, AdvertHeard):
                heard.append(event)
        contact, node = heard
        assert (contact.advert.public_key, contact.advert.new) == (ALICE.public_key, False)
        assert contact.advert.record is None
        assert (node.advert.new, node.received_at) == (True, START_TIME + 100)
        assert node.advert.record is not None
        assert node.advert.record.name == "Carol"
        assert node.advert.record.position == Position(53.55, 9.99)

    run(radio, scenario)


class ConfusedRadio(EmulatedRadio):
    """Answers every statistics request with the radio statistics."""

    def _stats(self, kind: int) -> None:
        super()._stats(1)


def test_statistics_of_another_type_end_the_session() -> None:
    """Out of step with the radio: the bridge reconnects and resynchronises."""
    radio = ConfusedRadio()

    async def scenario(session: CompanionSession, _: asyncio.Queue[RadioEvent]) -> None:
        with pytest.raises(RadioError, match="get statistics: unexpected answer RadioStats"):
            await session.start()
        assert session.closed

    run(radio, scenario)
