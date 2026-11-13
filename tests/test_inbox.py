"""Received messages and commands through the bridge (inbox.py, ADR 0009)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import inbox as inbox_module
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.commands import (
    CODE_FAILURE_LIMIT,
    CODE_FAILURE_WINDOW_S,
    CODE_STEP_S,
    REFUSALS_KEPT,
    code_at,
)
from meshcore_home_gateway.protocol.codes import AutoAdd, Command, Push
from meshcore_home_gateway.state import CommandLedger, JsonStateFile
from meshcore_home_gateway.views import iso
from meshcore_home_gateway.wiring import gateway
from tests.bridge_harness import (
    ALICE,
    BOB,
    FORGED,
    NOW,
    TWIN,
    FakeBus,
    InFlightRadio,
    fast_sessions,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
    stopped,
)
from tests.radio_emulator import EmulatedContact, EmulatedRadio


def test_command_is_refused_while_the_broker_is_unreachable(tmp_path: Path) -> None:
    """Queued until the reconnect, an accepted command would reach the automations at any later
    time; it is refused instead, and the sender can send it again."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.connected = False
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 10)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"] == {
                "word": "arm",
                "accepted": False,
                "reason": "broker_unavailable",
            }
            bus.connected = True
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)  # sent again
            await bus.wait_for("/command", lambda c: c["event_type"] == "arm")

    scenario(main)


def test_copy_of_a_command_refused_for_the_broker_is_no_command(tmp_path: Path) -> None:
    """The radio acknowledged the packet, so the sender does not repeat it: an identical copy is
    a replay. The sender was verified, so its timestamp is stored, also across a restart."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.connected = False
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 30)
            await bus.wait_for(
                "/received", lambda r: r["command"]["reason"] == "broker_unavailable"
            )
            bus.connected = True
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 30)  # replayed copy
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 1)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert len(bus.payloads("/received")) == 2  # the copy was dropped as a duplicate
            assert not bus.payloads("/command")
        restarted = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(make_config(), restarted, tmp_path) as (_, bus, _notes):
            restarted.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 30)  # again
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "replay"
            assert not bus.payloads("/command")

    scenario(main)


def test_received_messages_and_commands(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "ARM now", NOW - 10)
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["event_type"], command["args"], command["sender_name"]) == (
                "arm",
                "now",
                "Alice",
            )
            received = bus.payloads("/received")[0]
            assert received["target"] == "alice"
            assert received["command"] == {"word": "arm", "accepted": True, "reason": None}

            radio.queue_direct_message(ALICE.public_key[:6], "ARM now", NOW - 10)  # sender retry
            radio.queue_direct_message(ALICE.public_key[:6], "arm again", NOW - 20)  # older: replay
            radio.queue_direct_message(BOB.public_key[:6], "status", NOW - 5)  # not allowlisted
            radio.queue_channel_message(0, "Carol: arm", NOW - 1)  # channels never command
            await bus.wait_for("/received", lambda r: r["event_type"] == "channel")
            events = bus.payloads("/received")
            assert len(events) == 4  # the retry was dropped as duplicate
            assert events[1]["command"]["reason"] == "replay"
            assert events[2]["command"]["reason"] == "sender_not_allowed"
            assert events[3]["command"] is None
            assert (events[3]["sender_name"], events[3]["text"]) == ("Carol", "arm")
            assert len(bus.payloads("/command")) == 1

    scenario(main)
    ledger = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert ledger["command_timestamps"] == {ALICE.public_key.hex(): NOW - 10}


def test_command_time_is_stored_before_the_command_is_published(tmp_path: Path) -> None:
    """A crash right after publishing must not let the same packet command again (ADR 0009)."""
    state = tmp_path / "state.json"
    stored_at_publish: list[Any] = []

    class WatchingBus(FakeBus):
        def publish(
            self,
            topic: str,
            payload: bytes,
            retain: bool = False,
            qos: int = 1,
            *,
            correlation: bytes | None = None,
        ) -> None:
            if topic.endswith("/command"):
                stored = json.loads(state.read_text(encoding="utf-8"))
                stored_at_publish.append(stored["command_timestamps"])
            super().publish(topic, payload, retain, qos, correlation=correlation)

    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=WatchingBus()) as (_, bus, _):
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 7)
            await bus.wait_for("/command", lambda c: True)

    scenario(main)
    assert stored_at_publish == [{ALICE.public_key.hex(): NOW - 7}]


def test_command_is_judged_by_the_contacts_of_this_moment(tmp_path: Path) -> None:
    """A twin with Alice's 6-byte prefix that the radio added a moment ago makes her commands
    ambiguous, even when the radio did not announce the new contact (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(TWIN)  # added automatically from its advert, without a push
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"] == {
                "word": "arm",
                "accepted": False,
                "reason": "sender_ambiguous",
            }
            assert not bus.payloads("/command")

    scenario(main)


def test_command_is_refused_when_the_contacts_cannot_be_read(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.errors[Command.GET_CONTACTS] = 1
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unverified"
            assert not bus.payloads("/command")

    scenario(main)


def test_only_possible_commands_read_the_contacts_again(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            before = radio.commands().count(Command.GET_CONTACTS)
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW - 5)
            radio.queue_channel_message(0, "Carol: arm", NOW - 1)
            await bus.wait_for("/received", lambda r: r["event_type"] == "channel")
            assert radio.commands().count(Command.GET_CONTACTS) == before
            radio.queue_direct_message(ALICE.public_key[:6], "status", NOW - 4)
            await bus.wait_for("/command", lambda c: True)
            assert radio.commands().count(Command.GET_CONTACTS) == before + 1

    scenario(main)


@pytest.mark.parametrize(
    ("unknown", "push"),
    [(set(), Push.ADVERT), ({Command.GET_AUTOADD_CONFIG}, Push.NEW_ADVERT)],
    ids=["firmware-1.12-and-newer", "firmware-1.11-and-older"],
)
def test_new_contacts_are_read_before_their_messages(
    tmp_path: Path, unknown: set[int], push: int
) -> None:
    """A contact the radio added on its own is announced with ADVERT; firmware 1.11 and older,
    without the auto-add settings, announce it with NEW_ADVERT (docs/protocol.md section 4)."""
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")
    radio = EmulatedRadio(contacts=[ALICE], manual_add=False, unknown=unknown)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(carol)
            radio.emit(bytes([push]) + carol.public_key)
            radio.queue_direct_message(carol.public_key[:6], "hello", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["sender_name"] == "Carol"

    scenario(main)


def test_command_waiting_in_the_radio_at_connect_is_rejected(tmp_path: Path) -> None:
    """Sent while no bridge listened: a look-alike may have come and gone unseen meanwhile, so the
    sender has to send it again (ADR 0009). The message itself is still published."""
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 30, push=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unverified"
            assert (received["sender_name"], received["target"]) == ("Alice", "alice")
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)  # sent again
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["event_type"], command["sender_name"]) == ("arm", "Alice")

    scenario(main)


def test_copy_of_a_command_rejected_at_connect_is_no_command(tmp_path: Path) -> None:
    """The radio acknowledged the packet when the bridge fetched it, so the sender does not
    repeat it: an identical copy is a replay and is dropped as a duplicate (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 30, push=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/received", lambda r: r["command"]["reason"] == "sender_unverified")
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 30)  # replayed copy
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 1)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert len(bus.payloads("/received")) == 2
            assert not bus.payloads("/command")

    scenario(main)


def test_look_alikes_are_remembered_across_reconnects(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # seen by this session only
            for _attempt in range(100):
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break  # the advert was handled: the table is being read again
                await asyncio.sleep(0.01)
            count = len(bus.payloads("/state"))
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected", since=count)
            count = len(bus.payloads("/state"))
            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=count)
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"

    scenario(main)


def test_overwritten_look_alike_still_makes_the_sender_ambiguous(tmp_path: Path) -> None:
    """With a full table the radio overwrites the oldest contact and says so with a push; the
    look-alike that sent the message may be gone before the bridge reads the table."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(TWIN)  # added without a push: no read of the table shows it
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5, push=False)
            radio.overwrite(TWIN)  # CONTACT_DELETED push, the only trace of it, then ...
            radio.emit(bytes([Push.MSG_WAITING]))  # ... the bridge fetches the message
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"
            assert not bus.payloads("/command")

    scenario(main)


def test_look_alike_that_left_the_table_silently_still_counts(tmp_path: Path) -> None:
    """Firmware 1.18 drops a contact without CONTACT_DELETED: after an advert that claims type 0,
    its next advert clears the slot and may not add it again (BaseChatMesh.cpp:151-182). The
    adverts' pushes carry its key, and the bridge remembers it (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], autoadd_config=AutoAdd.CHAT)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(TWIN)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # added from its advert
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5, push=False)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # claims type 0
            radio.contacts.remove(TWIN)  # the next advert clears the slot ...
            radio.emit(bytes([Push.NEW_ADVERT]) + TWIN.public_key + bytes(115))  # ... no re-add
            radio.emit(bytes([Push.MSG_WAITING]))
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"
            assert not bus.payloads("/command")

    scenario(main)


def test_a_look_alike_the_radio_did_not_add_does_not_block_the_sender(tmp_path: Path) -> None:
    """With manual adding the radio refuses the node and only reports it (NEW_ADVERT); it cannot
    decrypt that node's messages, so its key does not count (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.emit(bytes([Push.NEW_ADVERT]) + TWIN.public_key + bytes(115))
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            command = await bus.wait_for("/command", lambda c: True)
            assert command["sender_name"] == "Alice"

    scenario(main)


def test_look_alike_counts_for_a_day_after_it_was_last_seen(tmp_path: Path) -> None:
    """A node the radio did not add (NEW_ADVERT) renews a look-alike that is still remembered,
    but does not bring back one that is forgotten (ADR 0009)."""
    clock, day = [1000.0], inbox_module.LOOK_ALIKE_MEMORY_S
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    not_added = bytes([Push.NEW_ADVERT]) + TWIN.public_key + bytes(115)

    def now() -> float:
        return clock[0]

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, monotonic=now) as (_, bus, _notes):
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # a contact of the radio, once
            await asyncio.sleep(0.05)
            clock[0] += day - 10
            radio.emit(not_added)  # renews it
            await asyncio.sleep(0.05)
            clock[0] += 20  # a day after the first sighting, not after the renewal
            radio.queue_direct_message(ALICE.public_key[:6], "arm one", NOW - 9)
            first = await bus.wait_for("/received", lambda r: True)
            assert first["command"]["reason"] == "sender_ambiguous"
            clock[0] += day  # forgotten
            radio.emit(not_added)  # renews nothing
            await asyncio.sleep(0.05)
            radio.queue_direct_message(ALICE.public_key[:6], "arm two", NOW - 5)
            second = await bus.wait_for("/received", lambda r: True, since=1)
            assert second["command"] == {"word": "arm", "accepted": True, "reason": None}

    scenario(main)


def test_look_alike_seen_only_in_a_table_read_still_counts(tmp_path: Path) -> None:
    """Over TCP the radio drops pushes when its send queue is full; a key from an earlier read
    of the table is remembered as well."""
    radio = EmulatedRadio(contacts=[ALICE, TWIN])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            radio.contacts.remove(TWIN)  # gone without any push
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.emit(bytes([Push.ADVERT]) + BOB.public_key)  # the bridge reads the table again
            for _attempt in range(100):
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # the read is complete: its table no longer holds the twin
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"

    scenario(main)


def test_sender_known_only_from_memory_is_not_accepted(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5, push=False)
            radio.overwrite(ALICE)
            radio.emit(bytes([Push.MSG_WAITING]))
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unknown"
            assert not bus.payloads("/command")

    scenario(main)


def test_contact_reads_are_spent_only_where_they_can_change_the_verdict(tmp_path: Path) -> None:
    """Senders the cached table already rejects need no read; one read serves every message
    fetched before it."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            before = radio.commands().count(Command.GET_CONTACTS)
            for second in range(5):
                radio.queue_direct_message(BOB.public_key[:6], "arm", NOW - 20 + second, push=False)
            for second in range(3):
                radio.queue_direct_message(
                    ALICE.public_key[:6], "status", NOW - 10 + second, push=False
                )
            radio.emit(bytes([Push.MSG_WAITING]))
            await bus.wait_for("/received", lambda r: True, since=7)
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == ["sender_not_allowed"] * 5 + [None] * 3
            assert len(bus.payloads("/command")) == 3
            assert radio.commands().count(Command.GET_CONTACTS) == before + 1

    scenario(main)


def test_unwritable_state_file_rejects_the_command_and_keeps_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    def full_disk(_store: object, _data: object) -> None:
        raise OSError(28, "No space left on device")

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            monkeypatch.setattr(JsonStateFile, "save", full_disk)
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "state_unwritable"
            monkeypatch.undo()
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 4)  # sent again
            await bus.wait_for("/command", lambda c: True)  # the service is still there

    scenario(main)


def test_a_command_sent_anew_after_an_unverified_one_is_judged_again(tmp_path: Path) -> None:
    """The identical copy (a retry or a replay, which look the same) stays a duplicate."""
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.errors[Command.GET_CONTACTS] = 1
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            await bus.wait_for("/received", lambda r: r["command"]["reason"] == "sender_unverified")
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)  # identical
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 4)  # sent anew
            await bus.wait_for("/command", lambda c: True)
            assert len(bus.payloads("/received")) == 2  # the identical copy gave no event

    scenario(main)


def test_a_sender_with_a_code_secret_commands_only_with_a_fresh_code(tmp_path: Path) -> None:
    """IMP-002: the code leaves the arguments, its time step is stored before the command is
    published, and a code serves once, also after a restart."""
    secret = b"12345678901234567890"
    config = make_config()
    sender = ALICE.public_key.hex()
    config = dataclasses.replace(
        config, commands=dataclasses.replace(config.commands, secrets=((sender, secret),))
    )
    code = code_at(secret, int(NOW // CODE_STEP_S))
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], f"arm now {code}", NOW - 10)
            command = await bus.wait_for("/command", lambda c: True)
            assert command["args"] == "now"
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.queue_direct_message(ALICE.public_key[:6], f"arm {code}", NOW - 9)
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 8)
            radio.queue_direct_message(ALICE.public_key[:6], "arm 000000", NOW - 7)
            await bus.wait_for("/received", lambda r: r["text"] == "arm 000000")
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == [None, "code_reused", "code_missing", "code_invalid"]
            assert radio.commands().count(Command.GET_CONTACTS) == reads  # the cache's verdict
        restarted = EmulatedRadio(contacts=[ALICE])
        async with running(config, restarted, tmp_path) as (_, bus, _notes):
            restarted.queue_direct_message(ALICE.public_key[:6], f"arm {code}", NOW - 6)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "code_reused"
            assert not bus.payloads("/command")

    assert code != "000000"
    scenario(main)
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert stored["command_code_steps"] == {sender: int(NOW // CODE_STEP_S)}


def test_wrong_codes_lock_the_sender_out_for_a_while_also_across_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#79, RFC 4226 section 7.3: each guess of a code wins with 3 in 10^6, and a host posing
    as the radio feeds thousands a second. After CODE_FAILURE_LIMIT wrong codes within
    CODE_FAILURE_WINDOW_S (a command without one counts too) the sender's commands are refused
    from the cached table, the right code's too, and the log says so once; the count is in the
    state file, so a restart does not end it."""
    caplog.set_level(logging.WARNING)
    secret = b"12345678901234567890"
    config = make_config()
    sender = ALICE.public_key.hex()
    config = dataclasses.replace(
        config, commands=dataclasses.replace(config.commands, secrets=((sender, secret),))
    )
    step = int(NOW // CODE_STEP_S)
    good = {code_at(secret, step + d) for d in (-1, 0, 1)}
    wrong = [c for c in (f"{n:06d}" for n in range(20)) if c not in good]
    alice = ALICE.public_key[:6]
    tries = CODE_FAILURE_LIMIT + 2

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE])
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            reads = radio.commands().count(Command.GET_CONTACTS)
            for n in range(tries):
                text = f"arm {wrong[n]}" if n % 2 else "arm now"
                radio.queue_direct_message(alice, text, NOW - 100 + n)
            right = f"arm {code_at(secret, step)}"
            radio.queue_direct_message(alice, right, NOW - 50)
            await bus.wait_for("/received", lambda r: r["text"] == right)
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            wrong_ones = ["code_missing", "code_invalid"] * CODE_FAILURE_LIMIT
            assert reasons == wrong_ones[:CODE_FAILURE_LIMIT] + ["code_locked"] * 3
            assert not bus.payloads("/command")
            assert radio.commands().count(Command.GET_CONTACTS) == reads  # the cache's verdict
        assert caplog.text.count("wrong command codes") == 1
        restarted = EmulatedRadio(contacts=[ALICE])
        async with running(config, restarted, tmp_path) as (_, bus, _notes):
            restarted.queue_direct_message(alice, f"status {code_at(secret, step)}", NOW - 40)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "code_locked"
        later = NOW + CODE_FAILURE_WINDOW_S
        again = EmulatedRadio(contacts=[ALICE])
        async with running(config, again, tmp_path, wall_clock=lambda: later) as (_, bus, _n):
            right = code_at(secret, int(later // CODE_STEP_S))
            again.queue_direct_message(alice, f"status {right}", int(later) - 5)
            await bus.wait_for("/command", lambda c: c["event_type"] == "status")

    scenario(main)


def test_genuine_codes_that_came_late_do_not_lock_their_sender_out(tmp_path: Path) -> None:
    """#79: commands that waited in the radio through a restart, an update or a re-plug end
    with the code of the time they were sent, which the bridge clock no longer accepts
    (code_invalid). They are no guesses: they are not counted, so the sender's next command,
    sent with the current code, is accepted, not code_locked."""
    secret = b"12345678901234567890"
    config = make_config()
    sender = ALICE.public_key.hex()
    config = dataclasses.replace(
        config, commands=dataclasses.replace(config.commands, secrets=((sender, secret),))
    )
    radio = EmulatedRadio(contacts=[ALICE])
    alice = ALICE.public_key[:6]
    for n in range(CODE_FAILURE_LIMIT):  # what the radio kept while the bridge was away
        sent = NOW - 300 + CODE_STEP_S * n
        code = code_at(secret, sent // CODE_STEP_S)
        radio.queue_direct_message(alice, f"arm {code}", sent, push=False)

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for(
                "/received", lambda r: len(bus.payloads("/received")) == CODE_FAILURE_LIMIT
            )
            right = f"arm {code_at(secret, int(NOW // CODE_STEP_S))}"
            radio.queue_direct_message(alice, right, NOW - 2)
            await bus.wait_for("/command", lambda c: c["event_type"] == "arm")
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == ["code_invalid"] * CODE_FAILURE_LIMIT + [None]

    scenario(main)
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert not stored.get("command_code_failures")


SECRET = b"12345678901234567890"
FORMER = "d4" * 32  # a key with a secret in the codes file that is no sender (any more)


def ignoring(enrolled: bool = False, revoked: bool = False) -> Config:
    """Alice and Bob are senders and Alice has a code secret; the codes file also holds the
    secret of FORMER, as after a sender's key was corrected by hand, unless revoked; with
    `enrolled`, Bob has a secret too. As load_config reads the files at the start."""
    base = make_config()
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    secrets = ((alice, SECRET), (bob, SECRET[::-1])) if enrolled else ((alice, SECRET),)
    commands = dataclasses.replace(
        base.commands,
        senders=frozenset({alice, bob}),
        secrets=secrets,
        ignored_code_keys=() if revoked else (FORMER,),
    )
    return dataclasses.replace(base, commands=commands)


def test_while_a_code_secret_is_ignored_senders_without_one_cannot_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#80: a sender's key corrected by hand leaves its secret under the old key, ignored, and
    the corrected key without one. 2.0 did not start then (exit 78). Now the gateway runs, and
    while a secret is ignored the commands of a sender without one are refused as code_missing:
    published on `received`, never as a command event. They are no wrong codes: no count, no
    lockout warning. A sender with a secret commands with its code. The codes file is read at
    the start: after `commands revoke` and a restart the sender without a secret commands."""
    caplog.set_level(logging.WARNING)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    tries = CODE_FAILURE_LIMIT + 1

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(ignoring(), radio, tmp_path) as (_, bus, _notes):
            for n in range(tries):
                radio.queue_direct_message(BOB.public_key[:6], "arm now", NOW - 100 + n)
            right = f"arm now {code_at(SECRET, int(NOW // CODE_STEP_S))}"
            radio.queue_direct_message(ALICE.public_key[:6], right, NOW - 50)
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["sender_key"], command["args"]) == (alice, "now")
            refused = [(r["sender_key"], r["command"]) for r in bus.payloads("/received")]
            assert refused == [
                (bob, {"word": "arm", "accepted": False, "reason": "code_missing"})
            ] * tries + [(alice, {"word": "arm", "accepted": True, "reason": None})]
            assert len(bus.payloads("/command")) == 1
        assert "wrong command codes" not in caplog.text
        restarted = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(ignoring(revoked=True), restarted, tmp_path) as (_, bus, _notes):
            restarted.queue_direct_message(BOB.public_key[:6], "arm now", NOW - 40)
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["sender_key"], command["args"]) == (bob, "now")

    scenario(main)
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert not stored.get("command_code_failures")


def test_while_a_code_secret_is_ignored_wrong_codes_still_lock_a_sender_with_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#79 and #80: the refusals of a sender without a secret are no wrong codes, but those of
    a sender with one are, while a secret is ignored as at any time. After CODE_FAILURE_LIMIT
    of them its right code is refused (code_locked), the log warns of guessing and the state
    file keeps the count."""
    caplog.set_level(logging.WARNING)
    step = int(NOW // CODE_STEP_S)
    good = {code_at(SECRET, step + d) for d in (-1, 0, 1)}
    wrong = [c for c in (f"{n:06d}" for n in range(20)) if c not in good]
    alice = ALICE.public_key[:6]

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(ignoring(), radio, tmp_path) as (_, bus, _notes):
            for n in range(CODE_FAILURE_LIMIT):
                radio.queue_direct_message(alice, f"arm {wrong[n]}", NOW - 100 + n)
            right = f"arm {code_at(SECRET, step)}"
            radio.queue_direct_message(alice, right, NOW - 50)
            await bus.wait_for("/received", lambda r: r["text"] == right)
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == ["code_invalid"] * CODE_FAILURE_LIMIT + ["code_locked"]
            assert not bus.payloads("/command")
        assert caplog.text.count("wrong command codes") == 1

    scenario(main)
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert list(stored["command_code_failures"]) == [ALICE.public_key.hex()]
    assert len(stored["command_code_failures"][ALICE.public_key.hex()]) == CODE_FAILURE_LIMIT


def test_a_sender_enrolled_while_a_secret_is_ignored_commands_with_its_code(
    tmp_path: Path,
) -> None:
    """#80: `commands enrol` and a restart are the other way out: the sender then commands with
    its code, though the old secret is still ignored. Its refusals before were no wrong codes,
    so its codes are not locked (code_locked)."""
    bob = BOB.public_key[:6]

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(ignoring(), radio, tmp_path) as (_, bus, _notes):
            for n in range(CODE_FAILURE_LIMIT):
                radio.queue_direct_message(bob, "arm", NOW - 100 + n)
            await bus.wait_for(
                "/received", lambda r: len(bus.payloads("/received")) == CODE_FAILURE_LIMIT
            )
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == ["code_missing"] * CODE_FAILURE_LIMIT
        restarted = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(ignoring(enrolled=True), restarted, tmp_path) as (_, bus, _notes):
            right = code_at(SECRET[::-1], int(NOW // CODE_STEP_S))
            restarted.queue_direct_message(bob, "arm now", NOW - 41)
            restarted.queue_direct_message(bob, f"arm now {right}", NOW - 40)
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["sender_key"], command["args"]) == (BOB.public_key.hex(), "now")
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == ["code_missing", None]

    scenario(main)


def test_commands_are_answered_when_asked(tmp_path: Path) -> None:
    """EXT-009: with `reply = "all"` the sender learns that its command was published, or why
    not, through the queue as any message; also when the same command comes again. A sender the
    checks did not identify learns nothing."""
    config = make_config()
    config = dataclasses.replace(config, commands=dataclasses.replace(config.commands, reply="all"))
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    alice = ALICE.public_key[:6]

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(alice, "arm now", NOW - 30)
            radio.queue_direct_message(alice, "arm", NOW - 40)  # older: a replay
            radio.queue_direct_message(BOB.public_key[:6], "arm", NOW - 5)  # not allowed
            radio.queue_direct_message(alice, "arm now", NOW - 20)  # the same command again

            def answered(_: dict[str, Any]) -> bool:
                events = bus.payloads("/delivery")
                return [d["event_type"] for d in events].count("confirmed") == 3

            await bus.wait_for("/delivery", answered, within_s=10)
            assert [t.text for t in radio.texts] == ["ok: arm", "rejected: replay", "ok: arm"]
            assert {t.prefix for t in radio.texts} == {alice}
            assert {d["target"] for d in bus.payloads("/delivery")} == {ALICE.public_key.hex()}
            assert len(bus.payloads("/command")) == 2

    scenario(main)


def test_commands_are_not_answered_by_default(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 10)
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 20)  # a replay
            await bus.wait_for("/received", lambda r: r["command"]["reason"] == "replay")
            await asyncio.sleep(0.5)
            assert not radio.texts
            assert not bus.payloads("/delivery")

    scenario(main)


def test_names_come_from_the_table_just_read(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.clear()  # removed in the app a moment ago
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unknown"
            assert received["sender_name"] is None

    scenario(main)


def test_names_from_the_mesh_cannot_forge_log_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Contact names come from the mesh: quoted in the log, they cannot start a line of their
    own, turn a line around or send escapes to a terminal."""
    caplog.set_level(logging.INFO)
    eve = EmulatedContact(bytes.fromhex("e5" * 32), "Eve" + FORGED)
    radio = EmulatedRadio(contacts=[ALICE, eve])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(eve.public_key[:6], "hello", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["sender_name"] == "Eve" + FORGED  # the event has the name as it is

    scenario(main)
    assert "message from 'Eve\\u202e\\nERROR x\\x1b[31m\\u2028'" in caplog.text
    assert not any(ch in caplog.text for ch in "\u202e\x1b\u2028")
    assert "\nERROR x" not in caplog.text


def test_messages_taken_from_the_radio_are_published_before_stopping(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On stop the radio goes first; what it has handed over is still published. A command that
    waits for its table read is rejected, as the read can no longer happen."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    bus = FakeBus()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus):
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.silent.add(Command.GET_CONTACTS)  # the command waits for its table read
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5, push=False)
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 4, push=False)
            radio.emit(bytes([Push.MSG_WAITING]))
            for _attempt in range(100):
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break  # both messages are out of the radio, and the command waits
                await asyncio.sleep(0.01)
            assert not radio.queue
        # the service stops here

    scenario(main)
    received = bus.payloads("/received")
    assert [r["text"] for r in received] == ["arm", "hello"]
    assert received[0]["command"]["reason"] == "sender_unverified"
    assert "cannot be verified: session closed" in caplog.text  # not "no answer"
    assert not bus.payloads("/command")


def test_ignored_channels_are_not_published(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(messages="ignore_channels = [0]"), radio, tmp_path) as (
            _,
            bus,
            _,
        ):
            radio.queue_channel_message(0, "Dave: chatter", NOW)
            radio.queue_direct_message(bytes.fromhex("c3" * 6), "hi", NOW)
            await bus.wait_for("/received", lambda r: r["event_type"] == "contact")
            assert all(r["event_type"] != "channel" for r in bus.payloads("/received"))

    scenario(main)


def test_command_events_are_never_delivered_late(tmp_path: Path) -> None:
    """QoS 0: a command event that cannot be delivered at once (a broker loss that paho has not
    noticed yet) is lost, not resent after the reconnect to trigger automations at any time."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "status", NOW - 5)
            await bus.wait_for("/command", lambda c: True)
            assert bus.qos["meshcore/radio1/command"] == {0}
            assert bus.qos["meshcore/radio1/received"] == {1}

    scenario(main)


def test_a_message_whose_answer_is_on_its_way_at_stop_is_published(tmp_path: Path) -> None:
    """The radio has already taken it out of its queue: lost if the stop does not wait for it."""
    radio = InFlightRadio(contacts=[ALICE, BOB])
    bus = FakeBus()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus):
            radio.armed = True
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 4)
            await asyncio.wait_for(radio.synced.wait(), 3)
            assert not radio.queue  # the stop begins while the answer is on its way

    scenario(main)
    assert [r["text"] for r in bus.payloads("/received")] == ["hello"]


@pytest.mark.parametrize("at_connect", [True, False], ids=["at-connect", "table-read-failed"])
def test_a_rejected_command_cannot_be_replayed_while_its_age_window_lasts(
    tmp_path: Path, at_connect: bool
) -> None:
    """With max_age_s = 3600 and the sender's clock 3000 s ahead, a copy passes the age check
    until 6600 s after the original: the duplicate memory must last that long."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    sent = NOW + 3000
    if at_connect:
        radio.queue_direct_message(ALICE.public_key[:6], "arm", sent, push=False)
    offset = [0.0]
    config = make_config()
    config = dataclasses.replace(
        config, commands=dataclasses.replace(config.commands, max_age_s=3600.0)
    )
    bus = FakeBus()
    store = JsonStateFile(tmp_path / "state.json")

    async def main() -> None:
        stop = asyncio.Event()
        bridge = gateway(
            config,
            bus,
            lambda: radio,
            CommandLedger(store),
            session_factory=fast_sessions,
            wall_clock=lambda: NOW + offset[0],
            monotonic=lambda: time.monotonic() + offset[0],
            notifier=lambda _m: True,
        )
        task = asyncio.create_task(bridge.run(stop))
        try:
            if not at_connect:
                await bus.wait_for("/state", lambda s: s["radio"] == "connected")
                radio.errors[Command.GET_CONTACTS] = 1
                radio.queue_direct_message(ALICE.public_key[:6], "arm", sent)
            first = await bus.wait_for("/received", lambda r: True)
            assert first["command"]["reason"] == "sender_unverified"
            offset[0] = 3601.0  # an hour later, no restart
            radio.queue_direct_message(ALICE.public_key[:6], "arm", sent)  # the replayed copy
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW + 3601)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert not bus.payloads("/command")
        finally:
            stop.set()
            await stopped(task)

    scenario(main)


def test_messages_of_a_contact_also_go_to_its_own_topic(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW - 5)
            topic = f"radio1/contacts/{ALICE.public_key.hex()}/received"
            event = await bus.wait_for(topic, lambda e: e["text"] == "hello")
            assert event == bus.payloads("radio1/received")[-1]
            radio.queue_channel_message(0, "Bob: hi all", NOW - 4)
            await bus.wait_for("radio1/received", lambda e: e["event_type"] == "channel")
            assert len(bus.payloads(topic)) == 1  # channel messages are no contact's

    scenario(main)


def test_a_flooding_sender_or_channel_is_not_published_beyond_its_rate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-012, SEC-013: a contact or a channel cannot fill the bus, the history and Home
    Assistant's recorder; the log counts what was left out. An accepted command still goes out,
    and other senders have their own rate."""
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(inbox_module, "RECEIVED_PER_SENDER_PER_S", 1e-6)  # no refill meanwhile
    monkeypatch.setattr(inbox_module, "RECEIVED_PER_CHANNEL_PER_S", 1e-6)
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    burst = int(inbox_module.RECEIVED_PER_SENDER_BURST)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            for n in range(burst + 5):
                radio.queue_direct_message(BOB.public_key[:6], f"spam {n}", NOW - 100 + n)
            radio.queue_direct_message(ALICE.public_key[:6], "status", NOW - 50)  # a command
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW - 49)
            for n in range(25):
                radio.queue_channel_message(0, f"Carol: chatter {n}", NOW - 40 + n)
            await bus.wait_for("/command", lambda c: c["event_type"] == "status")
            await bus.wait_for("/received", lambda e: e["text"] == "chatter 19")
            await asyncio.sleep(0.1)
            texts = [e["text"] for e in bus.payloads("radio1/received")]
            assert texts == [
                *(f"spam {n}" for n in range(burst)),
                "status",
                "hello",
                *(f"chatter {n}" for n in range(20)),
            ]
            history = [m["text"] for m in bus.payloads("radio1/history")[-1]["messages"]]
            assert "spam 10" not in history
            assert "chatter 20" not in history
            assert "chatter 19" in history
            # the first one left out is logged at once, the others at most a minute later
            assert "not published nor recorded (limit" in caplog.text
            assert ": 1 direct, 0 channel" in caplog.text
            bridge.inbox._received.report(time.monotonic() + 120)
            assert ": 4 direct, 5 channel" in caplog.text

    scenario(main)


def test_an_accepted_command_goes_out_after_its_sender_used_up_the_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-013: the rate of received messages bounds what a sender makes the gateway publish,
    never an accepted command. Its timestamp is in the ledger before it goes out, so one left out
    would be lost for good: the sender would only learn of it as a replay."""
    monkeypatch.setattr(inbox_module, "RECEIVED_PER_SENDER_PER_S", 1e-6)  # no refill meanwhile
    radio = EmulatedRadio(contacts=[ALICE])
    burst = int(inbox_module.RECEIVED_PER_SENDER_BURST)
    alice = ALICE.public_key[:6]

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            for n in range(burst + 3):
                radio.queue_direct_message(alice, f"hello {n}", NOW - 100 + n)
            radio.queue_direct_message(alice, "arm now", NOW - 50)  # the burst is used up
            command = await bus.wait_for("/command", lambda c: c["event_type"] == "arm")
            assert command["args"] == "now"
            received = bus.payloads("radio1/received")
            assert [e["text"] for e in received] == [
                *(f"hello {n}" for n in range(burst)),
                "arm now",
            ]
            assert received[-1]["command"] == {"word": "arm", "accepted": True, "reason": None}

    scenario(main)


def test_received_texts_are_published_cleaned_only_when_asked(tmp_path: Path) -> None:
    """SEC-017: texts from the mesh are data and stay as they came; `clean_received` drops
    control and direction characters, which a terminal or a file could take for instructions."""
    for clean in (False, True):
        radio = EmulatedRadio(contacts=[ALICE])
        config = make_config(messages=f"clean_received = {str(clean).lower()}")

        async def main() -> None:
            async with running(config, radio, tmp_path) as (_, bus, _notes):  # noqa: B023
                radio.queue_direct_message(ALICE.public_key[:6], "hi\x1b[2J\u202ethere", NOW - 5)  # noqa: B023
                event = await bus.wait_for("/received", lambda e: e["event_type"] == "contact")
                # the escape character goes, what followed it stays as text
                assert event["text"] == ("hi[2Jthere" if clean else "hi\x1b[2J\u202ethere")  # noqa: B023

        scenario(main)


@pytest.mark.parametrize("clean", [False, True])
def test_a_contacts_name_is_published_cleaned_only_when_asked(tmp_path: Path, clean: bool) -> None:
    """#137: `clean_received` cleans the contacts' names too, where the gateway takes them from
    the radio's table: in the `received` and `command` events, the history and the contact's
    document. The command is judged as it came. Off, names stay as they came."""
    hostile = "Ali\x1b[31mce\u202e"
    shown = "Ali[31mce" if clean else hostile
    radio = EmulatedRadio(contacts=[dataclasses.replace(ALICE, name=hostile)])
    config = make_config(messages=f"clean_received = {str(clean).lower()}")
    alice = ALICE.public_key.hex()

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "status", NOW - 5)
            command = await bus.wait_for("/command", lambda c: c["event_type"] == "status")
            received = await bus.wait_for("/received", lambda e: e["event_type"] == "contact")
            own = await bus.wait_for(f"/contacts/{alice}/received", lambda e: True)
            history = await bus.wait_for("/history", lambda d: d["total"] >= 1)
            document = (await bus.wait_for(f"/contacts/{alice}", lambda d: "name" in d))["name"]
            assert command["sender_name"] == received["sender_name"] == shown
            assert own["sender_name"] == shown
            assert {m["name"] for m in history["messages"]} == {shown}  # also the answer's
            assert document == shown

    scenario(main)


@pytest.mark.parametrize("publish", ["all", "targets"])
def test_only_the_channels_of_targets_are_published_when_asked(
    tmp_path: Path, publish: str
) -> None:
    """SEC-012: the *Public* channel's key is in the firmware; with `publish_channels =
    "targets"` the neighbours' chatter there is neither published nor kept, the channels the
    household chose as targets are."""
    radio = EmulatedRadio(contacts=[ALICE], channels={0: "Alerts", 1: "Public"})
    config = make_config(messages=f'publish_channels = "{publish}"')  # channel 0 is a target

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            radio.queue_channel_message(1, "Carol: anyone around?", NOW - 6)
            radio.queue_channel_message(0, "Alice: gate open", NOW - 5)
            await bus.wait_for("/received", lambda e: e["text"] == "gate open")
            await asyncio.sleep(0.05)
            texts = [e["text"] for e in bus.payloads("radio1/received")]
            history = [m["text"] for m in bus.payloads("radio1/history")[-1]["messages"]]
            if publish == "all":
                assert texts == ["anyone around?", "gate open"]
            else:
                assert texts == ["gate open"]
                assert "anyone around?" not in history

    scenario(main)


# --- the refused commands (#141) -------------------------------------------------------------


def _refused(bus: FakeBus) -> list[dict[str, Any]]:
    return bus.payloads("radio1/refused_commands")


def test_refused_commands_are_kept_without_their_word_until_the_next_refusal(
    tmp_path: Path,
) -> None:
    """#141: each refusal published on `received` also goes to the retained document of the
    latest ones, the newest first, with who sent it, when and why, never its word, arguments or
    code (ADR 0009). A message that is no command and an accepted command leave it as it is,
    so that a front end shows the refusal after the next message too."""
    secret = b"12345678901234567890"
    config = make_config()
    sender = ALICE.public_key.hex()
    config = dataclasses.replace(
        config, commands=dataclasses.replace(config.commands, secrets=((sender, secret),))
    )
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            # at the start and at the broker's connect, also with nothing refused yet
            empty = {"total": 0, "truncated": False, "commands": []}
            assert _refused(bus)
            assert all(d == empty for d in _refused(bus))
            radio.queue_direct_message(ALICE.public_key[:6], "arm now 000000", NOW - 10)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "code_invalid"
            document = await bus.wait_for("radio1/refused_commands", lambda d: d["total"] == 1)
            assert document == {
                "total": 1,
                "truncated": False,
                "commands": [
                    {
                        "at": received["received_at"],
                        "sender_key": sender,
                        "sender_prefix": sender[:12],
                        "sender_name": "Alice",
                        "reason": "code_invalid",
                    }
                ],
            }
            retained = [p for t, p, r in bus.published if t.endswith("/refused_commands")]
            assert all(r for t, _, r in bus.published if t.endswith("/refused_commands"))
            assert not any(b"arm" in p or b"000000" in p for p in retained)
            published = len(_refused(bus))
            radio.queue_channel_message(0, "Carol: arm", NOW - 9)  # no command
            right = code_at(secret, int(NOW // CODE_STEP_S))
            radio.queue_direct_message(ALICE.public_key[:6], f"arm {right}", NOW - 8)
            await bus.wait_for("/command", lambda c: c["event_type"] == "arm")
            assert len(_refused(bus)) == published  # neither publishes it
            assert _refused(bus)[-1] == document
            radio.queue_direct_message(BOB.public_key[:6], "status", NOW - 7)  # not allowed
            newer = await bus.wait_for("radio1/refused_commands", lambda d: d["total"] == 2)
            assert [(c["sender_name"], c["reason"]) for c in newer["commands"]] == [
                ("Bob", "sender_not_allowed"),
                ("Alice", "code_invalid"),
            ]

    scenario(main)


def test_a_refusal_is_dated_when_the_gateway_took_the_message_from_the_radio(
    tmp_path: Path,
) -> None:
    """#141, mqtt-api.md: a refusal's `at` is the `received_at` of its `received` event, the time
    the session took the message from the radio, not the time the inbox judged it, which may come
    later (it can wait for a contacts read). Here the inbox's clock is a minute ahead."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        later = lambda: NOW + 60  # noqa: E731
        async with running(make_config(), radio, tmp_path, wall_clock=later) as (_, bus, _n):
            radio.queue_direct_message(BOB.public_key[:6], "status", NOW - 7)  # not allowed
            received = await bus.wait_for("/received", lambda r: True)
            document = await bus.wait_for("radio1/refused_commands", lambda d: d["total"] == 1)
            assert received["command"]["reason"] == "sender_not_allowed"
            assert received["received_at"] == iso(NOW)  # the session's clock
            assert document["commands"][0]["at"] == received["received_at"]

    scenario(main)


def test_the_latest_refused_commands_outlive_a_restart_and_go_out_at_each_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#141: the latest REFUSALS_KEPT are kept in the state file, published at the start (also
    without command words) and again at each connect to the broker. A refusal that the rate of
    received messages leaves out (SEC-012, SEC-013) is not kept either."""
    monkeypatch.setattr(inbox_module, "RECEIVED_PER_SENDER_PER_S", 1e-6)  # no refill meanwhile
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    alice, bob, stranger = ALICE.public_key[:6], BOB.public_key[:6], bytes.fromhex("d4" * 6)
    not_allowed, unknown = "sender_not_allowed", "sender_unknown"

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(alice, "arm", NOW - 10)  # accepted
            for n in range(3):
                radio.queue_direct_message(bob, f"status {n}", NOW - 100 + n)
                radio.queue_direct_message(stranger, f"status {n}", NOW - 100 + n)
                radio.queue_direct_message(alice, f"arm {n}", NOW - 100 + n)  # a replay
            reasons = [not_allowed, unknown, "replay"] * 3
            await bus.wait_for("/received", lambda r: r["text"] == "arm 2")
            document = _refused(bus)[-1]
            assert [c["reason"] for c in document["commands"]] == reasons[::-1][:REFUSALS_KEPT]
            assert document["total"] == REFUSALS_KEPT
            published = len(_refused(bus))
            for callback in bus.on_connected:  # the broker connected again
                callback()
            assert _refused(bus)[published:] == [document]
            burst = int(inbox_module.RECEIVED_PER_SENDER_BURST)
            for n in range(burst - 3):
                radio.queue_direct_message(bob, f"hello {n}", NOW - 50 + n)
            radio.queue_direct_message(bob, "status", NOW - 10)  # beyond the burst: left out
            radio.queue_direct_message(stranger, "hello", NOW - 9)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert _refused(bus)[-1] == document
            assert bus.payloads("/received")[-2]["text"] == f"hello {burst - 4}"
        restarted = EmulatedRadio(contacts=[ALICE, BOB])
        quiet = make_config()
        quiet = dataclasses.replace(
            quiet, commands=dataclasses.replace(quiet.commands, words=frozenset())
        )
        async with running(quiet, restarted, tmp_path) as (_, bus, _notes):
            assert _refused(bus)[0] == document

    scenario(main)


@pytest.mark.parametrize("clean", [False, True])
def test_a_refused_senders_name_is_kept_cleaned_only_when_asked(
    tmp_path: Path, clean: bool
) -> None:
    """#141: the name of a refused command's sender as the radio's table gives it, without
    control and direction characters with `[messages] clean_received` (SEC-017)."""
    forger = EmulatedContact(bytes.fromhex("c3" * 32), "Mal\x1b[2J\u202elory")
    radio = EmulatedRadio(contacts=[ALICE, forger])
    config = make_config(messages=f"clean_received = {str(clean).lower()}")

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(forger.public_key[:6], "arm", NOW - 5)
            document = await bus.wait_for("radio1/refused_commands", lambda d: d["total"] == 1)
            (refusal,) = document["commands"]
            assert refusal["reason"] == "sender_not_allowed"
            assert refusal["sender_name"] == ("Mal[2Jlory" if clean else "Mal\x1b[2J\u202elory")

    scenario(main)
