"""The observer through the bridge, with the emulated radio: the providers' documents, events and
the history (ADR 0014-0016)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import history as history_module
from meshcore_home_gateway import observer as observer_module
from meshcore_home_gateway.config import ContactsConfig
from meshcore_home_gateway.config_checks import protected_keys
from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.adverts import MAX_LOGGED
from meshcore_home_gateway.core.mesh import AdvertCheck, Fix, Heard, Signal
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.core.telemetry import Answer, TelemetryState
from meshcore_home_gateway.history import History
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.ports import (
    AdvertHeard,
    ContactRequest,
    ContactsRead,
    PacketLogged,
    PathReset,
)
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Push
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    AdvertChecks,
    CurrentDocuments,
    JsonStateFile,
    RetainedKeys,
    TelemetryExclusions,
)
from meshcore_home_gateway.views import HistoryEntry, iso
from tests.bridge_harness import (
    ALICE,
    BOB,
    NOW,
    FakeBus,
    Unreachable,
    fast_timers,  # noqa: F401 - autouse: an advert's read in 50 ms
    make_config,
    new_run,
    run_state,
    running,
    scenario,
)
from tests.radio_emulator import (
    LPP_BME280,
    LPP_FULL,
    LPP_T1000E,
    SELF_KEY,
    EmulatedContact,
    EmulatedRadio,
    advert_packet,
)

ROOT = "meshcore/radio1"
TOPICS = Topics("meshcore", "radio1")
CAROL = EmulatedContact(
    bytes.fromhex("c3" * 32),
    "Carol",
    node_type=2,
    latitude_e6=53_550_000,
    longitude_e6=9_990_000,
    last_advert=int(NOW) - 5,
)


def retained(bus: FakeBus, topic: str) -> list[bytes]:
    return [p for t, p, keep in bus.published if t == topic and keep]


def last(bus: FakeBus, topic: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(retained(bus, topic)[-1])
    return document


def advert_log(contact: EmulatedContact, hops: int, snr_q: int = 22, rssi: int = -95) -> bytes:
    """LOG_RX_DATA with the flooded advert the radio heard (src/Packet.cpp:65-85)."""
    app = bytes([0x80 | contact.node_type]) + contact.name.encode()
    payload = contact.public_key + struct.pack("<I", contact.last_advert) + bytes(64) + app
    raw = bytes([0x11, hops]) + bytes(range(hops)) + payload
    return bytes([Push.LOG_RX_DATA, snr_q & 0xFF, rssi & 0xFF]) + raw


def test_contacts_are_published_one_document_each_with_an_index(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            index = last(bus, f"{ROOT}/contacts")
            assert index["total"] == 2
            assert index["max"] == 100
            assert {c["key"] for c in index["contacts"]} == {
                ALICE.public_key.hex(),
                BOB.public_key.hex(),
            }
            alice = last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")
            assert (alice["name"], alice["type"], alice["protected"]) == ("Alice", "chat", True)
            assert alice["path_known"] is False
            bob = last(bus, f"{ROOT}/contacts/{BOB.public_key.hex()}")
            assert (bob["path_hops"], bob["protected"]) == (2, True)
            stats = last(bus, f"{ROOT}/stats")
            assert (stats["uptime_s"], stats["noise_floor_dbm"]) == (3600, -118)
            assert stats["duty_cycle_percent"] == 50.0  # factor 1: the factory default

    scenario(main)


def test_a_removed_contact_clears_its_document_and_is_announced(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.overwrite(BOB)  # the radio dropped it, and reads the table again
            event = await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "removed")
            assert (event["key"], event["name"], event["type"]) == (
                BOB.public_key.hex(),
                "Bob",
                "chat",
            )
            assert len(bus.payloads(f"{ROOT}/contact")) == 1  # the start announced nothing
            topic = f"{ROOT}/contacts/{BOB.public_key.hex()}"
            await asyncio.sleep(0.05)
            assert retained(bus, topic)[-1] == b""  # the retained document is cleared
            assert last(bus, f"{ROOT}/contacts")["total"] == 1

    scenario(main)


def test_contacts_gone_while_the_gateway_was_stopped_are_cleared(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    RetainedKeys(store, "contacts").replace({ALICE.public_key.hex(), "dd" * 32})
    radio = EmulatedRadio(contacts=[ALICE])

    def observer(bus: FakeBus) -> Observer:
        return Observer(
            bus, TOPICS, retained=RetainedKeys(store, "contacts"), wall_clock=lambda: NOW
        )

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _n):
            await bus.wait_for(f"{ROOT}/contacts", lambda d: d["total"] == 1)
            assert retained(bus, f"{ROOT}/contacts/{'dd' * 32}") == [b""]
            assert retained(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")[-1] != b""
            events = bus.payloads(f"{ROOT}/contact")  # only what changed while stopped
            assert events == [
                {
                    "event_type": "removed",
                    "key": "dd" * 32,
                    "prefix": "dd" * 6,
                    "name": None,
                    "type": None,
                    "request": None,
                    "id": None,
                    "reason": None,
                }
            ]

    scenario(main)
    assert RetainedKeys(store, "contacts").keys == {ALICE.public_key.hex()}


def test_a_new_node_is_heard_with_its_signal_but_without_its_position(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config('[contacts]\nheard_nodes = "full"'), radio, tmp_path) as (
            _,
            bus,
            _notes,
        ):
            radio.emit(advert_log(CAROL, hops=3))
            radio.advertise(CAROL)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert (event["key"], event["name"], event["type"]) == (
                CAROL.public_key.hex(),
                "Carol",
                "repeater",
            )
            assert (event["hops"], event["snr_db"], event["rssi_dbm"]) == (3, 5.5, -95)
            assert event["look_alike"] is False
            nodes = await bus.wait_for(f"{ROOT}/nodes", lambda d: d["total"] == 1)
            assert "latitude" not in json.dumps(nodes)  # strangers' positions are not kept
            radio.advertise(CAROL)  # heard again: no second event
            await asyncio.sleep(0.1)
            assert len(bus.payloads(f"{ROOT}/node")) == 1

    scenario(main)


def test_contacts_heard_by_advert_and_message_and_the_history(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            key = ALICE.public_key.hex()
            radio.send_advert(ALICE, hops=1)  # newer than her record: the radio pushes it
            doc = await bus.wait_for(f"{ROOT}/contacts/{key}", lambda d: d["heard_via"] == "advert")
            assert doc["hops"] == 1
            radio.queue_direct_message(ALICE.public_key[:6], "hi there", int(NOW) - 10)
            doc = await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["heard_via"] == "message"
            )
            radio.queue_channel_message(0, "Carol: on my way", int(NOW) - 5)
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 2)
            channel, direct = history["messages"]
            assert (direct["direction"], direct["text"], direct["contact_key"]) == (
                "in",
                "hi there",
                key,
            )
            assert (channel["kind"], channel["name"], channel["text"]) == (
                "channel",
                "Carol",
                "on my way",
            )
            bus.send("alice", "Back soon")
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 3)
            out = history["messages"][0]
            assert (out["direction"], out["state"], out["name"]) == ("out", "confirmed", "Alice")

    scenario(main)


def test_messages_that_waited_in_the_radio_share_the_time_of_the_connect(tmp_path: Path) -> None:
    """The radio's message frame carries no time of its own, only the sender's clock
    (MyMesh.cpp:440-466, 558-582): the messages taken from the radio at the connect get the
    gateway's time then, one `at` for all. Of two with one `at`, the one listed first came
    later, in the order the radio received them (#113, docs/mqtt-api.md)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    for n, text in enumerate(("one", "two", "three")):
        radio.queue_channel_message(0, f"Carol: {text}", int(NOW) - 3600 + n, push=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 3)
            messages = history["messages"]
            assert [m["text"] for m in messages] == ["three", "two", "one"]
            assert {m["at"] for m in messages} == {iso(NOW)}

    scenario(main)


def test_the_history_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    config = make_config()

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        observers.append(history)
        return Observer(
            bus, TOPICS, history=history, protected=protected_keys(config), wall_clock=lambda: NOW
        )

    observers: list[History] = []

    async def first() -> None:
        radio = EmulatedRadio(contacts=[ALICE, CAROL])  # one per event loop
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            radio.queue_channel_message(0, "Dave: remember me", int(NOW) - 5)
            await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)

    async def second() -> None:
        radio = EmulatedRadio(contacts=[ALICE, CAROL])
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            assert history["messages"][0]["text"] == "remember me"
            carol = last(bus, f"{ROOT}/contacts/{CAROL.public_key.hex()}")
            assert (carol["latitude"], carol["position_source"]) == (53.55, "advert")

    scenario(first)
    scenario(second)
    for history in observers:
        history.close()
    stored = History.open(path)
    assert stored is not None
    assert list(stored.last_fixes()) == [CAROL.public_key.hex()]  # stored once, not per run
    assert len(stored.trace(CAROL.public_key.hex(), 0)) == 1
    stored.close()


@pytest.mark.parametrize(
    ("messages", "shown", "kept"),
    [
        ("", ["to the family", "Hi from Alice", "family news"], 3),
        (
            'publish_channels = "all"',
            ["to the family", "Hi from Alice", "Dave: chatter", "family news"],
            4,
        ),
        (
            'publish_channels = "all"\nignore_channels = [0]',
            ["to the family", "Hi from Alice", "Dave: chatter"],
            4,
        ),
    ],
    ids=["targets", "all", "all-but-ignored"],
)
def test_the_history_document_shows_only_the_channels_that_are_published(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, messages: str, shown: list[str], kept: int
) -> None:
    """#103: 1.x stored the messages of every channel. With `publish_channels = "targets"` (the
    default since 2.0) the retained `history` document must not bring a stranger's text on the
    Public channel back to the broker at the start; nor the received messages of an ignored
    channel. The gateway's own message to a target's channel stays, as it does while the gateway
    runs (all-but-ignored). #136: with "targets", the start deletes the received rows of the
    other channels for good, and the log says how many; with "all" they stay, until they are
    `messages_days` old."""
    path = tmp_path / "history.sqlite3"
    stored = History.open(path)
    assert stored is not None
    for entry in (
        HistoryEntry(NOW - 40, "in", "channel", "family news", channel_index=0, name="Bob"),
        HistoryEntry(NOW - 30, "in", "channel", "Dave: chatter", channel_index=1, name="Dave"),
        HistoryEntry(NOW - 20, "in", "contact", "Hi from Alice", ALICE.public_key.hex()),
        HistoryEntry(NOW - 10, "out", "channel", "to the family", channel_index=0, state="sent"),
    ):
        stored.add_message(entry)
    stored.close()
    config = make_config(messages=messages)  # the target `alerts` is channel 0
    histories: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE, CAROL])
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            documents = bus.payloads(f"{ROOT}/history")
            assert documents
            for document in documents:
                assert [m["text"] for m in document["messages"]] == shown

    caplog.set_level(logging.INFO, logger="meshcore_home_gateway.observer")
    scenario(main)
    for history in histories:
        assert len(history.recent_messages(10)) == kept
        history.close()
    deleted = [r.getMessage() for r in caplog.records if "not published" in r.getMessage()]
    line = "history: received messages of channels not published deleted: 1"
    assert deleted == ([line] if kept == 3 else [])


def test_a_message_sent_to_an_ignored_channel_stays_in_the_history_document_at_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#103: a target's channel may also be in `ignore_channels` (alerts go to a busy channel,
    whose chatter is not published). The gateway's own message there is in the `history` document
    while it runs, so the next start must not leave it out. #136: the start deletes the chatter
    an earlier version kept of it, not the gateway's own message; a start that deletes nothing
    logs nothing."""
    path = tmp_path / "history.sqlite3"
    config = make_config(messages="ignore_channels = [0]")  # the target `alerts` is channel 0
    shown: list[list[str]] = []
    histories: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def first() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            bus.send("alerts", "Smoke in the barn")
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            shown.append([m["text"] for m in history["messages"]])

    async def second() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            shown.append([m["text"] for m in bus.payloads(f"{ROOT}/history")[-1]["messages"]])

    caplog.set_level(logging.INFO, logger="meshcore_home_gateway.observer")
    scenario(first)
    assert not [r for r in caplog.records if "not published" in r.getMessage()]
    histories[0].add_message(
        HistoryEntry(NOW - 1, "in", "channel", "chatter", channel_index=0, name="Dave")
    )
    histories[0].close()
    scenario(second)
    texts = [m.text for m in histories[1].recent_messages(10)]
    for history in histories:
        history.close()
    assert shown == [["Smoke in the barn"], ["Smoke in the barn"]]
    assert texts == ["Smoke in the barn"]
    deleted = [r.getMessage() for r in caplog.records if "not published" in r.getMessage()]
    assert deleted == ["history: received messages of channels not published deleted: 1"]


def test_the_history_document_finds_the_published_messages_behind_other_channels_rows(
    tmp_path: Path,
) -> None:
    """#103: after an update from 1.x, the 50 newest rows may all be a busy channel's that is not
    published. The document of the start still shows the newest messages that are, from further
    back in the history."""
    path = tmp_path / "history.sqlite3"
    stored = History.open(path)
    assert stored is not None
    stored.add_message(
        HistoryEntry(NOW - 200, "in", "contact", "Hi from Alice", ALICE.public_key.hex())
    )
    for n in range(observer_module.RECENT_MESSAGES):
        entry = HistoryEntry(NOW - 100 + n, "in", "channel", f"chatter {n}", channel_index=1)
        stored.add_message(entry)
    stored.close()
    histories: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _n):
            documents = bus.payloads(f"{ROOT}/history")
            assert documents
            for document in documents:
                assert [m["text"] for m in document["messages"]] == ["Hi from Alice"]

    scenario(main)
    for history in histories:
        history.close()


class Stepping(EmulatedRadio):
    """Moves the gateway's wall clock 30 s on when it takes a direct text: the final event of a
    message comes later than its request."""

    wall: Clock | None = None

    def _send_text(self, p: bytes) -> None:
        if self.wall is not None:
            self.wall.now += 30
        super()._send_text(p)


def test_a_sent_message_names_its_target_its_id_and_when_it_was_accepted(
    tmp_path: Path,
) -> None:
    """`queued_at` is the wall clock at the acceptance, not the time of the final event (`at`)
    and not the monotonic clock the queue counts with; `id` is its delivery events'."""
    wall = Clock(NOW)
    radio = Stepping(contacts=[ALICE, BOB])
    radio.wall = wall

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, wall_clock=wall) as (_, bus, _n):
            bus.send("bob", "Back soon")
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            assert history["messages"] == [
                {
                    "at": iso(NOW + 30),
                    "direction": "out",
                    "kind": "contact",
                    "contact_key": BOB.public_key.hex(),
                    "channel_index": None,
                    "name": "Bob",
                    "text": "Back soon",
                    "state": "confirmed",
                    "hops": None,
                    "snr_db": None,
                    "target": "bob",
                    "id": done["id"],
                    "queued_at": iso(NOW),
                    "suspect": False,
                    "reason": None,
                }
            ]

    scenario(main)


def test_failed_and_channel_messages_carry_their_reason_and_no_suspicion(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alerts", "Gate open")
            bus.send("bob", "Pump stopped")
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 2)
            failed, sent = history["messages"]
            assert (failed["state"], failed["reason"], failed["suspect"]) == (
                "failed",
                "no_acknowledgement",
                None,
            )
            assert (failed["target"], failed["queued_at"]) == ("bob", iso(NOW))
            assert (sent["state"], sent["reason"], sent["suspect"]) == ("sent", None, None)
            assert (sent["target"], sent["channel_index"], sent["kind"]) == ("alerts", 0, "channel")
            ids = {d["target"]: d["id"] for d in bus.payloads("/delivery")}
            assert (failed["id"], sent["id"]) == (ids["bob"], ids["alerts"])

    scenario(main)


DAVE = EmulatedContact(bytes.fromhex("d4" * 32), "Dave")  # a companion that is no target


def test_a_received_message_names_the_target_it_came_through(tmp_path: Path) -> None:
    """The first target of the channel, or the contact target of the sender (as `received`
    names it); none for a sender that is no target. The keys of sent messages are null."""
    radio = EmulatedRadio(contacts=[ALICE, BOB, DAVE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_channel_message(0, "Carol: on my way", int(NOW) - 5)
            radio.queue_direct_message(ALICE.public_key[:6], "hi there", int(NOW) - 4)
            radio.queue_direct_message(DAVE.public_key[:6], "hello", int(NOW) - 3)
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 3)
            targets = {m["text"]: m["target"] for m in history["messages"]}
            assert targets == {"on my way": "alerts", "hi there": "alice", "hello": None}
            for entry in history["messages"]:
                assert [entry[k] for k in ("id", "queued_at", "suspect", "reason")] == [None] * 4

    scenario(main)


@pytest.mark.parametrize(("round_trip_ms", "suspect"), [(1500, False), (400, True)])
def test_a_message_to_a_contact_s_key_keeps_its_keys_across_a_restart(
    tmp_path: Path, round_trip_ms: int, suspect: bool
) -> None:
    """Sent through `contacts/<key>/send`: no target, the key is `contact_key`. Bob is two
    repeaters away: an acknowledgement in 400 ms is `suspect` (SEC-001), and stays so in the
    history after a restart."""
    path = tmp_path / "history.sqlite3"
    histories: list[History] = []
    sent: dict[str, Any] = {}

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def first() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB], ack_round_trip_ms=round_trip_ms)
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _n):
            await bus.wait_for("radio1/contacts", lambda d: d["total"] == 2)
            bus.send(BOB.public_key.hex(), "Hello you", contact=True)
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            sent.update(history["messages"][0])

    async def second() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _n):
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            assert history["messages"] == [sent]

    try:
        scenario(first)
        assert (sent["target"], sent["contact_key"]) == (None, BOB.public_key.hex())
        assert (sent["suspect"], sent["state"]) == (suspect, "confirmed")
        assert sent["queued_at"] == iso(NOW)
        assert re.fullmatch("[0-9a-f]{8}", sent["id"])
        scenario(second)
    finally:
        for history in histories:
            history.close()


def test_a_message_kept_over_a_stop_keeps_when_it_was_accepted(tmp_path: Path) -> None:
    """Accepted while the radio was away, sent after the next start: `queued_at` is the first
    run's clock, `at` the second's."""
    accepted, delivered = NOW, NOW + 3600

    async def stopped() -> None:
        config = make_config()
        clock = Clock(accepted)
        async with running(config, EmulatedRadio(), tmp_path, Unreachable, wall_clock=clock) as (
            _,
            bus,
            _n,
        ):
            bus.send("bob", "Pump stopped")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "queued")

    async def started() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB])
        clock = Clock(delivered)
        async with running(make_config(), radio, tmp_path, wall_clock=clock) as (_, bus, _n):
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            (entry,) = history["messages"]
            assert (entry["at"], entry["queued_at"]) == (iso(delivered), iso(accepted))
            assert entry["target"] == "bob"

    scenario(stopped)
    scenario(started)


def test_a_failing_observer_does_not_stop_messages(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    def broken(*_: object) -> None:
        raise RuntimeError("bug in a view")

    monkeypatch.setattr(Observer, "message", broken)
    monkeypatch.setattr(Observer, "delivery", broken)
    caplog.set_level(logging.ERROR)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "status", int(NOW) - 10)
            await bus.wait_for(f"{ROOT}/command", lambda e: e["event_type"] == "status")
            bus.send("alice", "still works")
            await bus.wait_for(f"{ROOT}/delivery", lambda e: e["event_type"] == "confirmed")

    scenario(main)
    assert "observer failed" in caplog.text


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def contact_record(contact: EmulatedContact) -> codec.ContactRecord:
    decoded = codec.decode(contact.record())
    assert isinstance(decoded, codec.ContactRecord)
    return decoded


def test_heard_nodes_come_back_after_a_restart_and_expire(tmp_path: Path) -> None:
    path, clock = tmp_path / "history.sqlite3", Clock(NOW)
    history = History.open(path)
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=clock)
    observer.radio_event(ContactsRead({}, NOW))
    carol = contact_record(CAROL)
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    history.close()

    history = History.open(path)
    assert history is not None
    clock.now = NOW + 3600
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=clock)
    observer.radio_event(ContactsRead({}, clock.now))
    nodes = last(bus, f"{ROOT}/nodes")
    assert [(n["name"], n["first_heard_at"]) for n in nodes["nodes"]] == [("Carol", iso(NOW))]
    clock.now = NOW + 8 * 86_400  # beyond the memory of heard nodes, and a purge is due
    observer.tick()
    assert last(bus, f"{ROOT}/nodes")["total"] == 0
    assert history.nodes() == []  # the history keeps them as long as the memory (ADR 0016)
    history.close()


def test_a_new_node_named_like_the_gateway_is_flagged() -> None:
    """SEC-007: the `node` event and the `nodes` document say that a node's name is taken."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW, names=frozenset({"CAROL"}))
    observer.radio_event(ContactsRead({}, NOW))
    carol = contact_record(CAROL)
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    (event,) = bus.payloads(f"{ROOT}/node")
    assert event["name_taken"] is True
    assert last(bus, f"{ROOT}/nodes")["name_taken"] == [CAROL.public_key.hex()]


def test_by_default_a_stranger_is_kept_with_its_name_times_and_signal(tmp_path: Path) -> None:
    """The default of `[contacts] heard_nodes` since 2.2, `full` (ADR 0032): the `node` event, the
    `nodes` document and the history carry a stranger's name, when it was heard and its signal,
    never its position."""
    radio, path = EmulatedRadio(contacts=[ALICE]), tmp_path / "history.sqlite3"
    histories: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _notes):
            radio.emit(advert_log(CAROL, hops=3))
            radio.advertise(CAROL)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert (event["name"], event["hops"], event["snr_db"], event["rssi_dbm"]) == (
                "Carol",
                3,
                5.5,
                -95,
            )
            assert [k for k, v in event.items() if v is None] == []
            nodes = await bus.wait_for(f"{ROOT}/nodes", lambda d: d["total"] == 1)
            (entry,) = nodes["nodes"]
            assert (entry["name"], entry["heard_via"], entry["rssi_dbm"]) == (
                "Carol",
                "advert",
                -95,
            )
            assert [k for k, v in entry.items() if v is None] == []
            assert "latitude" not in json.dumps(nodes)

    scenario(main)
    (row,) = histories[0].nodes()
    assert (row.name, row.heard.signal) == ("Carol", Signal(3, 5.5, -95))
    histories[0].close()


def test_with_clean_received_a_strangers_name_is_cleaned_everywhere(tmp_path: Path) -> None:
    """#137: with `[messages] clean_received` and the default `heard_nodes`, a heard node's name
    is cleaned where the gateway takes it from the radio: the `node` event, the `nodes` document
    and the history carry it without control and direction characters; a contact's too."""
    radio, path = EmulatedRadio(contacts=[replace(ALICE, name="Ali\x1bce")]), tmp_path / "h.db"
    hostile = replace(CAROL, name="Car\x1b[2Jol\u202e")
    histories: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def main() -> None:
        config = make_config(messages="clean_received = true")
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            radio.emit(advert_log(hostile, hops=3))
            radio.advertise(hostile)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert (event["name"], event["hops"]) == ("Car[2Jol", 3)
            nodes = await bus.wait_for(f"{ROOT}/nodes", lambda d: d["total"] == 1)
            assert nodes["nodes"][0]["name"] == "Car[2Jol"
            assert last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")["name"] == "Alice"

    scenario(main)
    (row,) = histories[0].nodes()
    assert row.name == "Car[2Jol"
    histories[0].close()


def test_with_clean_received_a_restored_strangers_name_is_cleaned(tmp_path: Path) -> None:
    """#137: a row kept before 2.2 or with `clean_received` off holds the name as it came: the
    gateway cleans it as it takes the node back."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    key = CAROL.public_key.hex()
    history.node_heard(key, "Car\x07ol\u202e", 2, Heard(NOW, "advert"), int(NOW))
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW + 60)
    observer.clean_names = True
    observer.radio_event(ContactsRead({}, NOW + 60))
    (entry,) = last(bus, f"{ROOT}/nodes")["nodes"]
    assert entry["name"] == "Carol"
    history.close()


@pytest.mark.parametrize(("clean", "taken"), [(False, False), (True, True)])
def test_a_stranger_named_like_a_contact_with_a_control_character(clean: bool, taken: bool) -> None:
    """SEC-007 with #137: a control character kept in a name reads as a space (`name_key`), so
    "Ali<BEL>ce" is not Alice; once `clean_received` takes it out, the name is Alice's, and the
    node is flagged."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    observer.clean_names = clean
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): contact_record(ALICE)}, NOW))
    twin = contact_record(EmulatedContact(b"\xee" * 32, "Ali\x07ce", last_advert=int(NOW) - 5))
    observer.radio_event(AdvertHeard(codec.Advert(twin.public_key, True, twin), NOW))
    (event,) = bus.payloads(f"{ROOT}/node")
    assert (event["name"], event["name_taken"]) == ("Alice" if clean else "Ali\x07ce", taken)
    flagged = [twin.public_key.hex()] if taken else []
    assert last(bus, f"{ROOT}/nodes")["name_taken"] == flagged


def test_by_default_a_taken_name_is_still_flagged_after_a_restart(tmp_path: Path) -> None:
    """SEC-007 across a restart: with the default the history keeps the name, so a node named
    like a contact is flagged before its next advert (with `keys` only after it)."""
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    first = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    first.heard_nodes = ContactsConfig().heard_nodes  # the run before the restart
    first.radio_event(ContactsRead({}, NOW))
    twin = contact_record(EmulatedContact(b"\xee" * 32, "Carol", last_advert=int(NOW) - 5))
    first.radio_event(AdvertHeard(codec.Advert(twin.public_key, True, twin), NOW))
    stranger = twin.public_key.hex()
    history.close()

    history = History.open(path)
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW + 60)
    observer.heard_nodes = ContactsConfig().heard_nodes
    observer.radio_event(ContactsRead({CAROL.public_key.hex(): contact_record(CAROL)}, NOW + 60))
    assert observer.mesh.nodes[stranger].node.name == "Carol"
    assert last(bus, f"{ROOT}/nodes")["name_taken"] == [stranger]
    history.close()


def test_by_default_a_node_kept_under_keys_gets_its_name_back_with_its_next_advert(
    tmp_path: Path,
) -> None:
    """The rows a run with `keys` kept have no name or signal; after the update to the default
    the node's next advert brings its name back, in the `nodes` document and in the history."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    key = CAROL.public_key.hex()
    history.node_heard(key, "", 2, Heard(NOW - 600, "advert"), CAROL.last_advert - 600)
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    observer.heard_nodes = ContactsConfig().heard_nodes
    observer.radio_event(ContactsRead({}, NOW))
    (entry,) = last(bus, f"{ROOT}/nodes")["nodes"]
    assert (entry["key"], entry["name"], entry["rssi_dbm"]) == (key, "", None)
    carol = contact_record(CAROL)
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    (entry,) = last(bus, f"{ROOT}/nodes")["nodes"]
    assert (entry["name"], entry["heard_at"]) == ("Carol", iso(NOW))
    (row,) = history.nodes()
    assert (row.name, row.last_advert) == ("Carol", CAROL.last_advert)
    history.close()


def test_with_heard_nodes_keys_a_stranger_has_no_name_times_or_signal(tmp_path: Path) -> None:
    """`[contacts] heard_nodes = "keys"` (SEC-015): the `node` event, the `nodes` document and
    the history carry a stranger's key and type, not its name, times or signal."""
    radio, path = EmulatedRadio(contacts=[ALICE]), tmp_path / "history.sqlite3"
    histories: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        histories.append(history)
        return Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)

    async def main() -> None:
        config = make_config('[contacts]\nheard_nodes = "keys"')
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            radio.emit(advert_log(CAROL, hops=3))
            radio.advertise(CAROL)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert (event["key"], event["type"]) == (CAROL.public_key.hex(), "repeater")
            assert [k for k, v in event.items() if v is None] == [
                "name",
                "hops",
                "snr_db",
                "rssi_dbm",
                "heard_at",
            ]
            nodes = await bus.wait_for(f"{ROOT}/nodes", lambda d: d["total"] == 1)
            assert "Carol" not in json.dumps(nodes)

    scenario(main)
    (row,) = histories[0].nodes()
    assert (row.key, row.name, row.heard.signal) == (CAROL.public_key.hex(), "", Signal())
    histories[0].close()


def test_with_heard_nodes_keys_the_rows_of_an_earlier_run_lose_name_and_signal(
    tmp_path: Path,
) -> None:
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    key = CAROL.public_key.hex()
    history.node_heard(key, "Carol", 2, Heard(NOW, "advert", Signal(3, 5.5, -95)), int(NOW))
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW + 60)
    observer.heard_nodes = "keys"
    observer.radio_event(ContactsRead({}, NOW + 60))
    (row,) = history.nodes()
    assert (row.name, row.heard.signal, row.heard.at) == ("", Signal(), NOW)
    (entry,) = last(bus, f"{ROOT}/nodes")["nodes"]
    assert (entry["key"], entry["name"]) == (key, None)
    history.close()


def test_with_heard_nodes_off_nothing_of_a_stranger_is_published_or_kept(tmp_path: Path) -> None:
    """`[contacts] heard_nodes = "off"` (SEC-015): no `node` event, no row, a `nodes` document
    with the look-alikes' keys only; the rows of an earlier run are not taken back and go with
    the next purge."""
    clock = Clock(NOW)
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=clock)
    observer.radio_event(ContactsRead({}, NOW))
    carol = contact_record(CAROL)
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    assert [row.name for row in history.nodes()] == ["Carol"]  # a run with `full`

    bus, alice = FakeBus(), ALICE.public_key.hex()
    observer = Observer(
        bus, TOPICS, history=history, protected=frozenset({alice}), wall_clock=clock
    )
    observer.heard_nodes = "off"
    observer.radio_event(ContactsRead({}, NOW))
    twin = contact_record(EmulatedContact(ALICE.public_key[:6] + b"\xee" * 26, "Alice"))
    observer.radio_event(AdvertHeard(codec.Advert(twin.public_key, True, twin), NOW))
    nodes = last(bus, f"{ROOT}/nodes")
    assert (nodes["total"], nodes["nodes"]) == (0, [])
    assert nodes["look_alike"] == [twin.public_key.hex()]  # a warning stays one
    assert bus.payloads(f"{ROOT}/node") == []
    clock.now = NOW + 60
    observer.tick()  # the first purge
    assert history.nodes() == []
    history.close()


def test_the_hourly_purge_runs_in_its_thread_while_the_loop_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CODE-002: the tick hands the purge to the history's thread and returns; the loop goes
    on until the purge is done."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    release = threading.Event()
    monkeypatch.setattr(history_module, "_purge_apart", lambda *_: release.wait(5))

    async def main() -> None:
        observer.tick()  # the first purge is due at once
        future = history._purging
        assert future is not None
        await asyncio.sleep(0.05)
        assert not future.done()  # it waits in its thread, the loop runs
        release.set()
        await asyncio.wait([asyncio.wrap_future(future)])

    asyncio.run(main())
    assert history._purging is None
    history.close()


def test_an_advert_flood_is_bounded_on_the_bus_and_in_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 adverts with invented keys within a second (SEC-010, SEC-011): the `nodes` document
    goes out again only with the tick after its interval, at once when a contact joins the
    table; the purge leaves the history the rows of the nodes the gateway keeps."""
    monkeypatch.setattr(observer_module, "NODES_DOCUMENT_INTERVAL_S", 30.0)  # fast timers: 0
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    clock, bus = Clock(NOW), FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=clock)
    observer.radio_event(ContactsRead({}, NOW))
    sent = len(retained(bus, f"{ROOT}/nodes"))
    records = [
        contact_record(
            EmulatedContact(bytes.fromhex(f"{i:04x}" * 16), f"Spam{i}", last_advert=int(NOW) + i)
        )
        for i in range(200)
    ]
    for i, record in enumerate(records):
        advert = codec.Advert(record.public_key, True, record)
        observer.radio_event(AdvertHeard(advert, NOW + i / 200))
    assert len(retained(bus, f"{ROOT}/nodes")) == sent
    clock.now = NOW + 29
    observer.tick()  # also the first purge
    assert len(retained(bus, f"{ROOT}/nodes")) == sent
    assert len(history.nodes()) == 50  # the heard nodes the gateway keeps
    clock.now = NOW + 31
    observer.tick()
    assert len(retained(bus, f"{ROOT}/nodes")) == sent + 1
    assert last(bus, f"{ROOT}/nodes")["total"] == 50
    observer.tick()  # nothing waits
    assert len(retained(bus, f"{ROOT}/nodes")) == sent + 1
    joined = records[-1]
    observer.radio_event(ContactsRead({joined.public_key.hex(): joined}, NOW + 32))
    assert len(retained(bus, f"{ROOT}/nodes")) == sent + 2
    assert last(bus, f"{ROOT}/nodes")["total"] == 49
    history.close()


def test_documents_are_published_again_when_the_broker_reconnects() -> None:
    """The MQTT adapter drops retained messages while the broker is away: a reconnect sends the
    current documents again, and the clearing of a removed contact's topic too; the history and
    the messages on their way also before anything was published on them."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    alice, bob = contact_record(ALICE), contact_record(BOB)
    keys = (ALICE.public_key.hex(), BOB.public_key.hex())
    observer.radio_event(ContactsRead(dict(zip(keys, (alice, bob), strict=True)), NOW))
    observer.radio_event(ContactsRead({keys[0]: alice}, NOW + 1))  # Bob was removed
    current = {t: p for t, p, keep in bus.published if keep}
    bus.published.clear()
    observer.publish_all()
    again = {t: p for t, p, keep in bus.published if keep}
    assert again == current | {
        f"{ROOT}/history": again[f"{ROOT}/history"],
        f"{ROOT}/pending": b'{"total":0,"truncated":false,"messages":[]}',
    }
    assert again[f"{ROOT}/contacts/{keys[1]}"] == b""  # cleared again
    assert again[f"{ROOT}/contacts/{keys[0]}"] != b""


def test_a_new_advert_of_a_contact_counts_for_the_contact() -> None:
    """Firmware 1.11 also sends NEW_ADVERT for a contact it has just added."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    alice = contact_record(ALICE)
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW))
    observer.radio_event(AdvertHeard(codec.Advert(alice.public_key, True, alice), NOW + 1))
    assert last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")["heard_via"] == "advert"
    assert not bus.payloads(f"{ROOT}/node")
    assert last(bus, f"{ROOT}/nodes")["total"] == 0


def test_node_listeners_learn_of_every_change_and_cannot_stop_the_documents(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant's select of the heard nodes follows them (mqtt/manage_entities.py): at
    once after a table read, else at a bounded rate."""
    bus = FakeBus()
    clock = [NOW]
    observer = Observer(bus, TOPICS, wall_clock=lambda: clock[0])
    calls: list[bool] = []
    observer.on_nodes.append(calls.append)

    def broken(_read: bool) -> None:
        raise RuntimeError("bug")

    observer.on_nodes.append(broken)
    alice, bob, carol = contact_record(ALICE), contact_record(BOB), contact_record(CAROL)
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW))
    assert calls
    assert set(calls) == {True}
    calls.clear()
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW + 1))
    assert calls == [False]
    assert last(bus, f"{ROOT}/nodes")["total"] == 1  # published although a listener failed
    # the radio reads its table after every advert: unchanged, it is no reason to hurry
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW + 3))
    observer.radio_event(
        ContactsRead({k.public_key.hex(): r for k, r in ((ALICE, alice), (BOB, bob))}, NOW + 4)
    )
    assert calls == [False, False, True]  # Bob joined
    clock[0] = NOW + 8 * 86_400  # Carol is forgotten
    observer.tick()
    assert calls == [False, False, True, False]
    assert "node listener failed" in caplog.text


def test_the_switch_the_worker_applies_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Untested firmware takes no request (ADR 0020's firmware gate): `manage` and
    `removable` say so after the first table read; before it, the last run's documents stay."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, manage=True, wall_clock=lambda: NOW)
    observer.set_manage(False)
    assert bus.published == []
    alice = contact_record(ALICE)
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW))
    assert last(bus, f"{ROOT}/contacts")["manage"] is False
    observer.set_manage(True)
    assert last(bus, f"{ROOT}/contacts")["manage"] is True
    assert last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")["removable"] is True
    count = len(bus.published)
    observer.set_manage(True)
    assert len(bus.published) == count
    # OPS-007: why requests are refused now; the reason alone changes the index only, and the
    # contacts' documents are not even made again (what they keep would be written again)
    made: list[str] = []
    with monkeypatch.context() as patched:
        patched.setattr(observer, "_publish_contact", made.append)
        observer.set_manage(True, "no_radio")
    assert made == []
    assert [p[0] for p in bus.published[count:]] == [f"{ROOT}/contacts"]
    assert last(bus, f"{ROOT}/contacts")["manage_reason"] == "no_radio"
    observer.set_manage(False, "untested_firmware")
    assert last(bus, f"{ROOT}/contacts")["manage_reason"] == "untested_firmware"
    assert last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")["removable"] is False


def test_the_history_keeps_the_record_of_the_newest_advert(tmp_path: Path) -> None:
    """A replayed older advert is heard, but its name does not replace the newer one's, not even
    after a restart."""
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead({}, NOW))
    for name, advert in (("Carol", int(NOW) - 5), ("Mallory", int(NOW) - 500)):
        record = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), name, last_advert=advert))
        observer.radio_event(AdvertHeard(codec.Advert(record.public_key, True, record), NOW))
    assert observer.mesh.nodes["c3" * 32].node.name == "Carol"
    assert [row.name for row in history.nodes()] == ["Carol"]
    history.close()
    history = History.open(path)  # a restart: the advert time comes back with the record
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW + 60)
    observer.radio_event(ContactsRead({}, NOW + 60))
    record = contact_record(
        EmulatedContact(
            bytes.fromhex("c3" * 32), "Mallory", node_type=1, last_advert=int(NOW) - 500
        )
    )
    observer.radio_event(AdvertHeard(codec.Advert(record.public_key, True, record), NOW + 60))
    assert observer.mesh.nodes["c3" * 32].node.name == "Carol"
    assert [(row.name, row.last_advert) for row in history.nodes()] == [("Carol", int(NOW) - 5)]
    history.close()


def test_a_replay_heard_before_the_first_table_read_does_not_win(tmp_path: Path) -> None:
    """At a start, the radio's pushes can come before the first table read, while the nodes from
    the history are not in the view yet: the newer advert's record stays, in the history too, and
    the node keeps the time it was heard in this run."""
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    carol = contact_record(
        EmulatedContact(bytes.fromhex("c3" * 32), "Carol", node_type=2, last_advert=int(NOW) - 5)
    )
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead({}, NOW))
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    history.close()
    history = History.open(path)
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW + 30)
    replay = contact_record(
        EmulatedContact(
            bytes.fromhex("c3" * 32), "Mallory", node_type=1, last_advert=int(NOW) - 500
        )
    )
    observer.radio_event(AdvertHeard(codec.Advert(replay.public_key, True, replay), NOW + 30))
    observer.radio_event(ContactsRead({}, NOW + 30))  # the first table read of this run
    tracked = observer.mesh.nodes["c3" * 32]
    assert (tracked.node.name, tracked.node.node_type) == ("Carol", 2)
    assert tracked.heard is not None
    assert tracked.heard.at == NOW + 30
    assert [(row.name, row.last_advert) for row in history.nodes()] == [("Carol", int(NOW) - 5)]
    history.close()


def answer(at: float, data: bytes = LPP_FULL) -> Answer:
    return Answer(lpp.decode(data), at, False, 0, False)


def test_telemetry_answers_publish_the_position_and_store_only_changes(tmp_path: Path) -> None:
    """ADR 0018: the position goes to the contact's document (and its tracker); the history
    gets a position row when it moved and a node row at most hourly from answers alone."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    tracker_updates: list[bool] = []
    observer.on_contacts.append(lambda _mesh: tracker_updates.append(True))
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead({key: contact_record(ALICE)}, NOW))
    tracker_updates.clear()
    observer.telemetry(key, answer(NOW + 10))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["latitude"], document["longitude"], document["altitude_m"]) == (
        50.12345,
        8.56775,
        120.5,
    )
    assert (document["position_source"], document["position_accuracy_m"]) == ("telemetry", 15.0)
    assert document["heard_via"] == "telemetry"
    assert tracker_updates == [True]  # its device gets a tracker at once
    observer.telemetry(key, answer(NOW + 100))  # resting: nothing new but the time received
    assert tracker_updates == [True]
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert document["position_at"] == iso(NOW + 10)
    assert document["position_received_at"] == iso(NOW + 100)
    assert document["heard_at"] == iso(NOW + 10)  # moves hourly from answers alone
    assert len(history.trace(key, 0)) == 1
    assert [row.heard.at for row in history.nodes() if row.key == key] == [NOW + 10]
    observer.telemetry(key, answer(NOW + 3610))
    assert [row.heard.at for row in history.nodes() if row.key == key] == [NOW + 3610]
    observer.telemetry("e5" * 32, answer(NOW + 3620))  # not a contact (any more): nothing
    assert not retained(bus, f"{ROOT}/contacts/{'e5' * 32}")
    history.close()


def test_an_answer_without_position_keeps_the_last_one() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead({key: contact_record(ALICE)}, NOW))
    observer.telemetry(key, answer(NOW + 10))
    observer.telemetry(key, answer(NOW + 20, bytes.fromhex("0174018b016700d9")))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["latitude"], document["position_received_at"]) == (50.12345, iso(NOW + 10))


def test_a_path_reset_is_published_before_the_next_table_read() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    key = BOB.public_key.hex()
    observer.radio_event(ContactsRead({key: contact_record(BOB)}, NOW))
    assert last(bus, f"{ROOT}/contacts/{key}")["path_known"] is True
    observer.radio_event(PathReset(BOB.public_key))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["path_known"], document["path_hops"]) == (False, None)
    published = len(bus.published)
    observer.radio_event(PathReset(BOB.public_key))  # nothing changed
    observer.radio_event(PathReset(ALICE.public_key))  # no contact
    assert len(bus.published) == published


def test_the_radios_own_time_on_air_needs_two_readings(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, _bus, _notes):
            assert bridge.observer.radio_tx_1h_s() is None  # one reading, at the connect

    scenario(main)


# --- telemetry readings, states and exclusion (ADR 0018) ----------------------------------------

# a companion whose adverts carry a position
GREENHOUSE = EmulatedContact(
    bytes.fromhex("e7" * 32),
    "Greenhouse",
    latitude_e6=53_560_000,
    longitude_e6=10_000_000,
    last_advert=int(NOW) - 50,
)
BME280_READINGS = {
    "ch2_temperature_c": 21.3,
    "ch2_humidity_percent": 55.5,
    "ch2_barometric_pressure_hpa": 1013.2,
    "ch2_altitude_m": 120.0,
}
T1000E_READINGS = {"ch1_temperature_c": 23.4, "ch1_luminosity": 50.0}


def reading_rows(history: History, key: str | None = None) -> list[tuple[Any, ...]]:
    rows = history._db.execute(
        "SELECT contact_key, at, quantity, value FROM reading ORDER BY id"
    ).fetchall()
    return [row for row in rows if key is None or row[0] == key]


def table(*contacts: EmulatedContact) -> dict[str, codec.ContactRecord]:
    return {c.public_key.hex(): contact_record(c) for c in contacts}


def test_an_answer_publishes_its_readings_and_stores_each_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The document and the history show the same answer; the device gets a component the
    contact did not have before at once, not at the next table read."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    updates: list[bool] = []
    observer.on_contacts.append(lambda _mesh: updates.append(True))
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead(table(ALICE), NOW))
    updates.clear()
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    observer.telemetry(key, answer(NOW + 10, LPP_BME280))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["battery_v"], document["mcu_temperature_c"]) == (4.12, 23.4)
    assert list(document["readings"].items()) == list(BME280_READINGS.items())  # in order
    assert document["readings_at"] == iso(NOW + 10)
    assert reading_rows(history) == [
        (key, NOW + 10, "battery_v", 4.12),
        (key, NOW + 10, "mcu_temperature_c", 23.4),
        *((key, NOW + 10, k, v) for k, v in BME280_READINGS.items()),
    ]
    assert updates == [True]  # its tracker and four sensors, at once
    observer.telemetry(key, answer(NOW + 100, LPP_BME280))  # the same keys
    assert updates == [True]
    observer.telemetry(key, answer(NOW + 200, LPP_FULL))  # fewer keys
    assert updates == [True]
    assert last(bus, f"{ROOT}/contacts/{key}")["readings"] == {}  # absent from the newest
    observer.telemetry(key, answer(NOW + 300, LPP_T1000E))  # new keys
    assert updates == [True, True]
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["mcu_temperature_c"], document["readings"]) == (20.0, T1000E_READINGS)
    assert document["latitude"] == 50.12345  # an answer without a position keeps the last
    assert len(reading_rows(history)) == 6 + 6 + 2 + 4
    assert "readings not published" not in caplog.text  # the position is published
    written: list[str] = []
    monkeypatch.setattr(history, "add_readings", lambda k, _at, _values: written.append(k))
    # a position on channel 2, presence: decoded, not published
    observer.telemetry(key, answer(NOW + 400, bytes.fromhex("028807a5f2014ead002f12026601")))
    assert written == []  # no value: no transaction
    assert f"telemetry {key[:12]}: 2 readings not published" in caplog.text
    history.close()


def test_a_first_position_gives_the_device_its_tracker_at_once_without_new_values() -> None:
    """A companion that first answered without a position (location not allowed, GPS off) and
    then with one: the answer brings no new value, its device still gets a tracker at once."""
    observer = Observer(FakeBus(), TOPICS, wall_clock=lambda: NOW)
    updates: list[bool] = []
    observer.on_contacts.append(lambda _mesh: updates.append(True))
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead(table(ALICE), NOW))
    observer.telemetry(key, answer(NOW + 10, bytes.fromhex("0174019c016700ea")))  # no position
    updates.clear()
    observer.telemetry(key, answer(NOW + 20))  # the same values, and a position
    assert updates == [True]


def test_each_contact_a_table_read_adds_is_told_before_its_document(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """At the first table read only one the last run did not have; a listener that fails stops
    neither the others nor the documents."""
    store = JsonStateFile(tmp_path / "state.json")
    alice, bob, greenhouse = (c.public_key.hex() for c in (ALICE, BOB, GREENHOUSE))
    RetainedKeys(store, "contacts").replace({alice})
    bus = FakeBus()
    observer = Observer(
        bus, TOPICS, retained=RetainedKeys(store, "contacts"), wall_clock=lambda: NOW
    )
    states: dict[str, TelemetryState] = {alice: "answering", bob: "answering"}
    observer.telemetry_state = states.get
    told: list[str] = []

    def broken(_key: str) -> None:
        raise RuntimeError("bug")

    def forget(key: str) -> None:
        told.append(key)
        states.pop(key, None)

    observer.on_contact_added += [broken, forget]
    observer.radio_event(ContactsRead(table(ALICE, BOB), NOW))
    assert told == [bob]  # Alice is one of the last run
    assert last(bus, f"{ROOT}/contacts/{alice}")["telemetry_state"] == "answering"
    assert last(bus, f"{ROOT}/contacts/{bob}")["telemetry_state"] == "waiting"
    observer.radio_event(ContactsRead(table(ALICE), NOW + 10))  # Bob removed
    observer.radio_event(ContactsRead(table(ALICE, BOB, GREENHOUSE), NOW + 20))
    assert told == [bob, bob, greenhouse]
    assert "contact added listener failed" in caplog.text


def test_the_state_is_pulled_and_an_event_follows_the_document() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    states: dict[str, TelemetryState] = {alice: "answering"}
    observer.telemetry_state = states.get
    observer.radio_event(ContactsRead(table(ALICE, BOB), NOW))
    document = last(bus, f"{ROOT}/contacts/{alice}")
    assert (document["telemetry_state"], document["telemetry_current"]) == ("answering", True)
    assert last(bus, f"{ROOT}/contacts/{bob}")["telemetry_state"] == "waiting"  # not known yet
    states[alice] = "away"
    observer.telemetry_state_changed(alice)
    document = last(bus, f"{ROOT}/contacts/{alice}")
    assert (document["telemetry_state"], document["telemetry_current"]) == ("away", False)
    observer.telemetry_event("unanswered", alice, "away")
    assert bus.payloads(f"{ROOT}/telemetry") == [
        {
            "event_type": "unanswered",
            "key": alice,
            "prefix": alice[:12],
            "name": "Alice",
            "state": "away",
        }
    ]
    topics = [(t, keep) for t, _p, keep in bus.published]
    assert topics[-2:] == [(f"{ROOT}/contacts/{alice}", True), (f"{ROOT}/telemetry", False)]
    observer.exclusions.exclude(bob)
    observer.telemetry_event("answered", bob, "answering")  # excluded: nothing
    observer.telemetry_event("answered", "dd" * 32, "no_position")  # left the table meanwhile
    events = bus.payloads(f"{ROOT}/telemetry")
    assert [(e["key"], e["name"]) for e in events] == [(alice, "Alice"), ("dd" * 32, None)]
    published = len(bus.published)
    observer.telemetry_state_changed("dd" * 32)  # no contact: no document
    observer.telemetry_state_changed(alice)  # unchanged: not again
    assert len(bus.published) == published


def test_readings_and_when_the_position_came_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead(table(ALICE), NOW))
    observer.telemetry(key, answer(NOW + 10, LPP_BME280))
    observer.telemetry(key, answer(NOW + 20, LPP_T1000E))  # the newest: no position
    before = last(bus, f"{ROOT}/contacts/{key}")
    history.close()
    history = History.open(path)
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW + 60)
    observer.restore_position_received(key, NOW + 10)  # from the saved schedule
    observer.radio_event(ContactsRead(table(ALICE), NOW + 60))
    after = last(bus, f"{ROOT}/contacts/{key}")
    assert (after["readings"], after["readings_at"]) == (T1000E_READINGS, iso(NOW + 20))
    assert after["position_received_at"] == iso(NOW + 10)
    assert after == before
    history.close()


def test_readings_come_back_also_for_a_contact_the_history_has_no_other_row_of(
    tmp_path: Path,
) -> None:
    """Readings can be kept longer than when a node was last heard (`readings_days`)."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    history.add_readings(alice, NOW - 10, {"battery_v": 3.9, "ch2_humidity_percent": 40.0})
    history.add_readings(bob, NOW - 20, {"battery_v": 3.7})
    history.add_position(bob, Fix(Position(50.1, 8.5), NOW - 20, "telemetry"))
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead(table(ALICE, BOB), NOW))
    document = last(bus, f"{ROOT}/contacts/{alice}")
    assert (document["battery_v"], document["readings"]) == (3.9, {"ch2_humidity_percent": 40.0})
    assert document["heard_at"] is None
    document = last(bus, f"{ROOT}/contacts/{bob}")
    assert (document["battery_v"], document["latitude"]) == (3.7, 50.1)
    history.close()


def test_when_the_position_came_is_shown_at_once_for_a_contact_without_one() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead(table(ALICE), NOW))
    observer.restore_position_received(key, NOW - 5)
    assert last(bus, f"{ROOT}/contacts/{key}")["position_received_at"] == iso(NOW - 5)
    observer.restore_position_received(key, NOW - 50)  # it has one now
    assert last(bus, f"{ROOT}/contacts/{key}")["position_received_at"] == iso(NOW - 5)


def test_an_excluded_contact_gets_back_neither_its_readings_nor_its_telemetry_position(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    greenhouse, alice = GREENHOUSE.public_key.hex(), ALICE.public_key.hex()
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead(table(GREENHOUSE, ALICE), NOW))
    observer.telemetry(greenhouse, answer(NOW + 10, LPP_BME280))
    observer.telemetry(alice, answer(NOW + 10, LPP_BME280))
    history.close()
    exclusions = TelemetryExclusions(tmp_path / EXCLUSIONS_FILE)
    exclusions.exclude(greenhouse)  # while the gateway was stopped (command line, telemetry off)
    history = History.open(path)
    assert history is not None
    bus = FakeBus()
    observer = Observer(
        bus,
        TOPICS,
        history=history,
        exclusions=TelemetryExclusions(tmp_path / EXCLUSIONS_FILE),
        wall_clock=lambda: NOW + 60,
    )
    observer.radio_event(ContactsRead(table(GREENHOUSE, ALICE), NOW + 60))
    document = last(bus, f"{ROOT}/contacts/{greenhouse}")
    assert (document["battery_v"], document["readings"], document["readings_at"]) == (
        None,
        {},
        None,
    )
    assert (document["position_source"], document["latitude"]) == ("advert", 53.56)
    assert (document["telemetry_state"], document["telemetry_excluded"]) == ("excluded", True)
    assert last(bus, f"{ROOT}/contacts/{alice}")["readings"] == BME280_READINGS  # not excluded
    assert last(bus, f"{ROOT}/contacts")["telemetry_excluded"] == [greenhouse]
    rows = len(reading_rows(history))
    observer.telemetry(greenhouse, answer(NOW + 70, LPP_BME280))  # an answer in flight
    assert last(bus, f"{ROOT}/contacts/{greenhouse}") == document
    assert len(reading_rows(history)) == rows
    observer.restore_position_received(greenhouse, NOW + 10)  # not for an excluded contact
    assert observer.mesh.contacts[greenhouse].position_received_at is None
    history.close()


def test_with_an_unreadable_exclusions_file_no_contact_gets_back_its_readings_or_telemetry_position(
    tmp_path: Path,
) -> None:
    """The file may exclude anyone: as telemetry asks nobody then (design §6.11), the history
    gives back when contacts were heard and their advert positions only, while their records
    hold one."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    greenhouse, alice = GREENHOUSE.public_key.hex(), ALICE.public_key.hex()
    for key in (greenhouse, alice):
        history.add_readings(key, NOW - 20, {"battery_v": 4.1, "ch2_temperature_c": 21.5})
    history.add_position(greenhouse, Fix(Position(50.1, 8.5), NOW - 20, "telemetry"))
    history.add_position(alice, Fix(Position(53.55, 9.99), NOW - 50, "advert"))
    history.node_heard(alice, "Alice", 1, Heard(NOW - 5, "message"))
    path = tmp_path / EXCLUSIONS_FILE
    path.write_text(f'{{"excluded": ["{greenhouse}",]}}')  # a hand edit's trailing comma
    exclusions = TelemetryExclusions(path)
    assert exclusions.unreadable
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, exclusions=exclusions, wall_clock=lambda: NOW)
    for key in (greenhouse, alice):
        observer.restore_position_received(key, NOW - 20)  # from the saved schedule
    observer.radio_event(ContactsRead(table(GREENHOUSE, ALICE), NOW))
    for key in (greenhouse, alice):
        document = last(bus, f"{ROOT}/contacts/{key}")
        assert (document["battery_v"], document["readings"], document["readings_at"]) == (
            None,
            {},
            None,
        )
        assert document["position_received_at"] is None
    document = last(bus, f"{ROOT}/contacts/{greenhouse}")
    assert (document["position_source"], document["latitude"]) == ("advert", 53.56)  # its record
    document = last(bus, f"{ROOT}/contacts/{alice}")
    # her record holds none: not where she is (ADR 0025)
    assert (document["position_source"], document["latitude"]) == (None, None)
    assert (document["heard_via"], document["heard_at"]) == ("message", iso(NOW - 5))
    history.close()


def test_an_excluded_contact_whose_record_holds_no_position_gets_no_advert_position_back(
    tmp_path: Path,
) -> None:
    """The record holds none (after an advert with 0, 0, or the gateway's own add): the history's
    advert position is not known to be where the contact is (ADR 0025)."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    key = ALICE.public_key.hex()
    history.add_position(key, Fix(Position(53.56, 10.0), NOW - 50, "advert"))
    exclusions = TelemetryExclusions(None)
    exclusions.exclude(key)
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, exclusions=exclusions, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead(table(ALICE), NOW))  # its record has no position
    document = last(bus, f"{ROOT}/contacts/{key}")
    position = ("latitude", "longitude", "altitude_m", "position_at", "position_source")
    assert {name: document[name] for name in position} == dict.fromkeys(position)
    assert document["position_current"] is None
    history.close()


def test_an_exclusion_forgets_the_values_and_puts_the_advert_position_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    told: list[tuple[str, bool]] = []

    def broken(_key: str, _excluded: bool) -> None:
        raise RuntimeError("bug")

    observer.on_telemetry_exclusion += [broken, lambda key, excluded: told.append((key, excluded))]
    greenhouse, alice = GREENHOUSE.public_key.hex(), ALICE.public_key.hex()
    observer.radio_event(ContactsRead(table(GREENHOUSE, ALICE), NOW))
    observer.telemetry(greenhouse, answer(NOW + 10, LPP_BME280))
    observer.telemetry(alice, answer(NOW + 10, LPP_BME280))
    assert last(bus, f"{ROOT}/contacts/{greenhouse}")["position_source"] == "telemetry"
    observer.exclusions.exclude(greenhouse)
    assert observer.telemetry_excluded(greenhouse, True) is None  # no forget asked
    document = last(bus, f"{ROOT}/contacts/{greenhouse}")
    assert (document["battery_v"], document["mcu_temperature_c"], document["readings"]) == (
        None,
        None,
        {},
    )
    assert (document["readings_at"], document["position_received_at"]) == (None, None)
    assert (document["position_source"], document["latitude"]) == ("advert", 53.56)
    assert (document["telemetry_state"], document["telemetry_excluded"]) == ("excluded", True)
    assert (document["heard_via"], document["heard_at"]) == ("telemetry", iso(NOW + 10))
    index = last(bus, f"{ROOT}/contacts")
    assert (index["telemetry_excluded"], index["telemetry_excluded_total"]) == ([greenhouse], 1)
    assert told == [(greenhouse, True)]  # also after a listener failed
    assert "telemetry exclusion listener failed" in caplog.text
    assert bus.payloads(f"{ROOT}/telemetry") == []
    # what takes the place is stored (dated by its advert, so it sorts before the answer's)
    assert [fix.source for fix in history.trace(greenhouse, 0)] == ["advert", "advert", "telemetry"]
    assert len(reading_rows(history, greenhouse)) == 6  # until readings_days
    with caplog.at_level(logging.INFO):
        assert observer.telemetry_excluded(greenhouse, True, forget=True) is True
    assert reading_rows(history, greenhouse) == []
    assert [fix.source for fix in history.trace(greenhouse, 0)] == ["advert", "advert"]
    assert len(reading_rows(history, alice)) == 6
    assert f"telemetry {greenhouse[:12]}: 7 rows deleted from the history" in caplog.text
    observer.exclusions.include(greenhouse)
    assert observer.telemetry_excluded(greenhouse, False, forget=True) is None  # not on inclusion
    assert told[-1] == (greenhouse, False)
    document = last(bus, f"{ROOT}/contacts/{greenhouse}")
    assert (document["telemetry_state"], document["telemetry_excluded"]) == ("off", False)
    assert last(bus, f"{ROOT}/contacts")["telemetry_excluded"] == []
    history.close()


def test_forgetting_needs_a_working_history_and_keeps_an_advert_position_once(
    tmp_path: Path,
) -> None:
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    greenhouse = GREENHOUSE.public_key.hex()
    observer.radio_event(ContactsRead(table(GREENHOUSE), NOW))
    observer.telemetry(greenhouse, answer(NOW + 10, LPP_T1000E))  # readings, no position
    observer.exclusions.exclude(greenhouse)
    assert observer.telemetry_excluded(greenhouse, True, forget=True) is True
    assert [fix.source for fix in history.trace(greenhouse, 0)] == ["advert"]  # not again
    observer.radio_event(ContactsRead(table(GREENHOUSE), NOW + 20))
    assert len(history.trace(greenhouse, 0)) == 1
    history._db.execute("DROP TABLE reading")
    assert observer.telemetry_excluded(greenhouse, True, forget=True) is False  # broken now
    history.close()
    without = Observer(FakeBus(), TOPICS, wall_clock=lambda: NOW)
    without.exclusions.exclude(greenhouse)
    assert without.telemetry_excluded(greenhouse, True, forget=True) is False
    assert without.telemetry_excluded("dd" * 32, True) is None  # also a key not on the radio


def test_a_key_not_on_the_radio_is_excluded_in_the_index() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    told: list[tuple[str, bool]] = []
    observer.on_telemetry_exclusion.append(lambda key, excluded: told.append((key, excluded)))
    observer.exclusions.exclude("dd" * 32)
    observer.telemetry_excluded("dd" * 32, True)  # before the first table read
    assert told == [("dd" * 32, True)]
    assert last(bus, f"{ROOT}/contacts")["telemetry_excluded"] == ["dd" * 32]
    assert not retained(bus, f"{ROOT}/contacts/{'dd' * 32}")


def test_without_telemetry_companions_are_off_and_excluded_ones_excluded() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    alice, bob, carol = ALICE.public_key.hex(), BOB.public_key.hex(), CAROL.public_key.hex()
    observer.exclusions.exclude(bob)
    observer.exclusions.exclude(carol)  # stored, then the key turned up as a repeater
    observer.radio_event(ContactsRead(table(ALICE, BOB, CAROL), NOW))
    states = {k: last(bus, f"{ROOT}/contacts/{k}")["telemetry_state"] for k in (alice, bob, carol)}
    assert states == {alice: "off", bob: "excluded", carol: None}
    assert last(bus, f"{ROOT}/contacts/{carol}")["telemetry_excluded"] is False


def test_the_observer_holds_the_one_set_of_exclusions(tmp_path: Path) -> None:
    """The contact manager (and the poller) read the observer's exclusions: a second holder
    could drift apart from it. Also an empty set given is the one held."""
    exclusions = TelemetryExclusions(tmp_path / EXCLUSIONS_FILE)
    assert len(exclusions) == 0
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    observers: list[Observer] = []

    def observer(bus: FakeBus) -> Observer:
        observers.append(Observer(bus, TOPICS, exclusions=exclusions, wall_clock=lambda: NOW))
        return observers[0]

    settings = make_config(telemetry=True)

    async def main() -> None:
        async with running(settings, radio, tmp_path, observer=observer) as (bridge, bus, _n):
            key = ALICE.public_key.hex()
            bus.contact_requests.put_nowait(ContactRequest("telemetry", b"off", False, key))
            await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "excluded")
            assert observers[0].exclusions is exclusions
            assert bridge._telemetry is not None
            assert bridge._telemetry._exclusions is exclusions

    scenario(main)
    assert TelemetryExclusions(tmp_path / EXCLUSIONS_FILE).keys == {ALICE.public_key.hex()}


# --- the documents of the last run, before the first table read (ADR 0024) ---------------------


def last_run(tmp_path: Path, *contacts: EmulatedContact) -> tuple[JsonStateFile, dict[str, Any]]:
    """A run in which the contacts answered with a position: the state file it left, and their
    documents as the broker keeps them."""
    store = JsonStateFile(tmp_path / "state.json")
    bus = FakeBus()
    observer = Observer(bus, TOPICS, current=CurrentDocuments(store), wall_clock=lambda: NOW)
    observer.telemetry_state = lambda _key: "answering"
    observer.radio_event(ContactsRead(table(*contacts), NOW))
    for contact in contacts:
        observer.telemetry(contact.public_key.hex(), answer(NOW + 10, LPP_BME280))
    observer.flush()  # the stop
    keys = [contact.public_key.hex() for contact in contacts]
    return store, {key: last(bus, f"{ROOT}/contacts/{key}") for key in keys}


def restarted(
    store: JsonStateFile, states: dict[str, TelemetryState] | None
) -> tuple[Observer, FakeBus]:
    """The next run, a minute later, with no table read yet; `states`: the poller's (None:
    telemetry is off)."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, current=CurrentDocuments(store), wall_clock=lambda: NOW + 60)
    if states is not None:
        observer.telemetry_state = states.get
    return observer, bus


def on_disk(store: JsonStateFile) -> dict[str, dict[str, Any]]:
    """The documents the state file holds on the disk, as a crash would leave them: the
    observer stores them at once or at its tick, never only in memory."""
    return CurrentDocuments(JsonStateFile(store.path)).documents


FORGOTTEN = {
    "telemetry_state": "excluded",
    "telemetry_excluded": True,
    "telemetry_current": False,
    "position_current": None,
    "readings_at": None,
    "battery_v": None,
    "mcu_temperature_c": None,
    "readings": {},
    "latitude": None,
    "longitude": None,
    "altitude_m": None,
    "position_at": None,
    "position_source": None,
    "position_accuracy_m": None,
    "position_received_at": None,
}


def test_a_document_of_the_last_run_expires_also_while_the_radio_is_away(tmp_path: Path) -> None:
    """The observer has no contact before the first table read: it publishes the stored document
    again once the companion's answers expire, with only its state changed."""
    store, before = last_run(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    assert (before[key]["position_current"], before[key]["readings"]) == (True, BME280_READINGS)
    states: dict[str, TelemetryState] = {key: "answering"}
    observer, bus = restarted(store, states)
    observer.publish_all()  # the broker connected
    observer.tick()
    assert last(bus, f"{ROOT}/contacts/{key}") == before[key]  # unchanged: the broker may lack it
    states[key] = "away"  # her bound passed
    observer.tick()
    expired = {"telemetry_state": "away", "telemetry_current": False, "position_current": False}
    assert last(bus, f"{ROOT}/contacts/{key}") == {**before[key], **expired}
    assert on_disk(store) == {key: {**before[key], **expired}}  # at once
    observer.tick()
    assert len(retained(bus, f"{ROOT}/contacts/{key}")) == 2
    observer.publish_all()  # the broker connected again
    assert last(bus, f"{ROOT}/contacts/{key}") == {**before[key], **expired}
    states[key] = "waiting"  # a day later
    observer.telemetry_state_changed(key)
    waiting = {**before[key], **expired, "telemetry_state": "waiting"}
    assert last(bus, f"{ROOT}/contacts/{key}") == waiting
    assert on_disk(store) == {key: waiting}


def test_with_clean_received_a_document_of_the_last_run_is_published_cleaned(
    tmp_path: Path,
) -> None:
    """#137: a run with `clean_received` off stored its contact's name as it came; the next run
    with the setting on cleans it in the stored document before the first table read too, while
    the radio stays away, and stores it so at once."""
    named = replace(ALICE, name="Ali\x1b[2J\u202ece")
    store, before = last_run(tmp_path, named)
    key = named.public_key.hex()
    assert before[key]["name"] == "Ali\x1b[2J\u202ece"
    observer, bus = restarted(store, {key: "answering"})
    observer.clean_names = True
    observer.publish_all()  # the broker connected
    cleaned = {**before[key], "name": "Ali[2Jce"}
    assert last(bus, f"{ROOT}/contacts/{key}") == cleaned
    assert on_disk(store) == {key: cleaned}
    observer.tick()
    assert len(retained(bus, f"{ROOT}/contacts/{key}")) == 1  # unchanged since


@pytest.mark.parametrize("clean", [False, True])
def test_without_clean_received_a_document_of_the_last_run_keeps_its_name(
    tmp_path: Path, clean: bool
) -> None:
    """The other way round: off, the stored name stays as it came; a clean name stays as it is."""
    name = "Alice" if clean else "Ali\x1b[2Jce"
    store, before = last_run(tmp_path, replace(ALICE, name=name))
    key = ALICE.public_key.hex()
    observer, bus = restarted(store, {key: "answering"})
    observer.clean_names = clean
    observer.publish_all()
    assert last(bus, f"{ROOT}/contacts/{key}") == before[key]
    assert on_disk(store) == {key: before[key]}


def test_an_expiry_the_broker_missed_is_published_at_the_next_connect(tmp_path: Path) -> None:
    """A run that stopped before the broker connected published it to nobody: the state file
    keeps it expired, and the next run publishes it at each connect; one still answering too,
    unchanged (the broker may have missed it as well)."""
    store, before = last_run(tmp_path, ALICE, BOB)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    states: dict[str, TelemetryState] = {alice: "away", bob: "answering"}
    observer, _nobody = restarted(store, states)
    observer.tick()  # her bound passed while the gateway was stopped
    observer.flush()  # the stop, before the broker connected
    observer, bus = restarted(store, states)
    observer.publish_all()
    expired = {"telemetry_state": "away", "telemetry_current": False, "position_current": False}
    assert last(bus, f"{ROOT}/contacts/{alice}") == {**before[alice], **expired}
    assert len(retained(bus, f"{ROOT}/contacts/{bob}")) == 1
    assert last(bus, f"{ROOT}/contacts/{bob}") == before[bob]
    observer.publish_all()
    assert len(retained(bus, f"{ROOT}/contacts/{alice}")) == 2


def test_a_position_expiry_the_broker_missed_is_published_at_the_next_connect(
    tmp_path: Path,
) -> None:
    """Her answers are still current, her position no longer: the broker's copy says it is."""
    store, before = last_run(tmp_path, ALICE)
    alice = ALICE.public_key.hex()
    states: dict[str, TelemetryState] = {alice: "no_position"}
    observer, _nobody = restarted(store, states)
    observer.tick()
    observer.flush()  # the stop, before the broker connected
    observer, bus = restarted(store, states)
    observer.publish_all()
    expired = {
        "telemetry_state": "no_position",
        "telemetry_current": True,
        "position_current": False,
    }
    assert last(bus, f"{ROOT}/contacts/{alice}") == {**before[alice], **expired}


def test_with_telemetry_off_a_document_of_the_last_run_is_off_at_the_connect(
    tmp_path: Path,
) -> None:
    store, before = last_run(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    observer, bus = restarted(store, None)
    observer.publish_all()
    off = {"telemetry_state": "off", "telemetry_current": False, "position_current": False}
    assert last(bus, f"{ROOT}/contacts/{key}") == {**before[key], **off}
    assert on_disk(store) == {key: {**before[key], **off}}


def test_an_exclusion_before_the_first_table_read_forgets_the_stored_values_at_once(
    tmp_path: Path,
) -> None:
    store, before = last_run(tmp_path, ALICE, BOB)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    observer, bus = restarted(store, {alice: "answering", bob: "answering"})
    observer.exclusions.exclude(alice)
    observer.telemetry_excluded(alice, True)
    assert last(bus, f"{ROOT}/contacts/{alice}") == {**before[alice], **FORGOTTEN}
    stored = {alice: {**before[alice], **FORGOTTEN}, bob: before[bob]}
    assert on_disk(store) == stored  # gone from the disk at once
    assert retained(bus, f"{ROOT}/contacts/{bob}") == []


def test_an_exclusion_and_an_inclusion_once_it_expired_are_published_and_stored_at_once(
    tmp_path: Path,
) -> None:
    """Before the first table read too: the exclusion forgets its values, the inclusion turns
    its switch on again."""
    store, before = last_run(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    states: dict[str, TelemetryState] = {key: "away"}  # her bound passed while it was stopped
    observer, bus = restarted(store, states)
    observer.publish_all()
    assert last(bus, f"{ROOT}/contacts/{key}")["telemetry_state"] == "away"
    observer.exclusions.exclude(key)
    observer.telemetry_excluded(key, True)
    assert last(bus, f"{ROOT}/contacts/{key}") == {**before[key], **FORGOTTEN}
    assert on_disk(store) == {key: {**before[key], **FORGOTTEN}}
    observer.exclusions.include(key)
    states[key] = "waiting"  # the poller forgot her answers
    observer.telemetry_excluded(key, False)
    included = {
        **before[key],
        **FORGOTTEN,
        "telemetry_state": "waiting",
        "telemetry_excluded": False,
    }
    assert last(bus, f"{ROOT}/contacts/{key}") == included
    assert on_disk(store) == {key: included}


def test_the_first_table_read_replaces_the_documents_of_the_last_run(tmp_path: Path) -> None:
    store, _before = last_run(tmp_path, ALICE, BOB)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    observer, bus = restarted(store, {alice: "answering", bob: "answering"})
    observer.radio_event(ContactsRead(table(ALICE), NOW + 60))  # Bob left the radio meanwhile
    assert on_disk(store).keys() == {alice}  # at once
    observer.tick()
    assert on_disk(store) == {alice: last(bus, f"{ROOT}/contacts/{alice}")}
    published = len(bus.published)
    observer.telemetry_state = lambda _key: "away"
    observer.tick()  # the poller reports the states of the table's contacts now
    assert len(bus.published) == published
    observer.radio_event(ContactsRead({}, NOW + 120))  # and Alice now
    assert on_disk(store) == {}


def test_at_the_first_table_read_an_expired_document_of_the_last_run_is_dropped(
    tmp_path: Path,
) -> None:
    """Kept until then also once it expired; from then on only while the answers are current."""
    store, _before = last_run(tmp_path, ALICE, BOB)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    observer, _bus = restarted(store, {alice: "away", bob: "answering"})
    observer.publish_all()
    assert on_disk(store).keys() == {alice, bob}
    observer.radio_event(ContactsRead(table(ALICE, BOB), NOW + 60))
    assert on_disk(store).keys() == {bob}  # at once


def test_a_document_is_stored_at_once_when_it_turns_current_or_not_else_at_the_tick(
    tmp_path: Path,
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    bus = FakeBus()
    observer = Observer(bus, TOPICS, current=CurrentDocuments(store), wall_clock=lambda: NOW)
    key = ALICE.public_key.hex()
    states: dict[str, TelemetryState] = {key: "waiting"}
    observer.telemetry_state = states.get
    observer.radio_event(ContactsRead(table(ALICE, CAROL), NOW))
    assert on_disk(store) == {}  # not current; Carol is no companion
    states[key] = "answering"
    observer.telemetry_state_changed(key)
    assert on_disk(store) == {key: last(bus, f"{ROOT}/contacts/{key}")}
    observer.telemetry(key, answer(NOW + 10, LPP_BME280))
    assert on_disk(store)[key]["readings"] == {}  # until the tick
    observer.tick()
    assert on_disk(store) == {key: last(bus, f"{ROOT}/contacts/{key}")}
    states[key] = "away"
    observer.telemetry_state_changed(key)
    assert on_disk(store) == {}


# --- the check for positions in adverts (ADR 0018, ADR 0025) ----------------------------------

SHARED = (50_123_456, 8_654_321)  # a position as an advert carries it, int32 x 10^-6
MOVED = (50_124_000, 8_655_000)
NO_DELAY = codec.TuningParams(0, 1000)  # the radio handles a packet in the pass that logs it
DELAYED = codec.TuningParams(10_000, 1000)  # a receive delay: a flood waits up to 32 s
DAVE = EmulatedContact(bytes.fromhex("d4" * 32), "Dave", last_advert=int(NOW) - 30)
EXPORT_CONTACT = 17  # the stored copy of an advert: never asked for (ADR 0025)


class Air:
    """What a radio writes about adverts, fed to the observer as one session numbers its frames,
    at the clock's time; and its table: the records as the firmware keeps them
    (BaseChatMesh.cpp:122-200)."""

    def __init__(self, observer: Observer, clock: Clock, *contacts: EmulatedContact) -> None:
        self.observer, self.clock = observer, clock
        self.records = {contact.public_key: replace(contact) for contact in contacts}
        self.serial, self.seq = 1, 0

    def frame(self) -> int:
        """The next frame's number; on its own, a frame of another kind."""
        self.seq += 1
        return self.seq

    def log(
        self,
        contact: EmulatedContact,
        timestamp: int,
        position: tuple[int, int] | None = None,
        *,
        hops: int = 0,
        snr_db: float = 6.0,
    ) -> None:
        rx = codec.RxLog(snr_db, -96, advert_packet(contact, timestamp, position, hops=hops))
        self.observer.radio_event(PacketLogged(rx, self.clock.now, self.frame(), self.serial))

    def advert(
        self,
        contact: EmulatedContact,
        position: tuple[int, int] | None = None,
        *,
        timestamp: int | None = None,
        logged: bool = True,
        pushed: bool = True,
        between: bool = False,
        delay_s: float = 0.0,
    ) -> int:
        """An advert of a contact the radio takes: its log, the record (lat/lon only with the
        flag), the push `delay_s` later. Returns its time (default: a minute after the record's)."""
        record = self.records[contact.public_key]
        timestamp = record.last_advert + 60 if timestamp is None else timestamp
        if logged:
            self.log(record, timestamp, position)
        if between:
            self.frame()
        self.clock.now += delay_s
        record.last_advert = timestamp
        if position is not None:
            record.latitude_e6, record.longitude_e6 = position
        if pushed:
            self.push(record)
        return timestamp

    def push(self, contact: EmulatedContact) -> None:
        advert = codec.Advert(contact.public_key, False)
        self.observer.radio_event(AdvertHeard(advert, self.clock.now, self.frame(), self.serial))

    def heard(
        self,
        node: EmulatedContact,
        position: tuple[int, int] | None = None,
        *,
        timestamp: int,
        logged: bool = True,
    ) -> None:
        """An advert of a node the radio does not hold: its log, then NEW_ADVERT with the record
        made from this advert alone (BaseChatMesh.cpp:108-120, 156-179)."""
        if logged:
            self.log(node, timestamp, position)
        latitude, longitude = position or (0, 0)
        made = replace(node, last_advert=timestamp, latitude_e6=latitude, longitude_e6=longitude)
        advert = codec.Advert(node.public_key, True, contact_record(made))
        self.observer.radio_event(AdvertHeard(advert, self.clock.now, self.frame(), self.serial))

    def read(self) -> None:
        self.observer.radio_event(ContactsRead(table(*self.records.values()), self.clock.now))


def checking(
    tmp_path: Path,
    *contacts: EmulatedContact,
    tuning: codec.TuningParams | None = NO_DELAY,
    current: bool = False,
    **options: Any,
) -> tuple[Observer, FakeBus, Air]:
    """An observer that keeps its checks in the state file (and, `current`, the documents of
    the companions whose answers are current), after the radio connected and its first table
    read."""
    bus, clock = FakeBus(), Clock(NOW)
    store = new_run(tmp_path)
    if current:
        options["current"] = CurrentDocuments(store)
    observer = Observer(bus, TOPICS, advert_checks=AdvertChecks(store), wall_clock=clock, **options)
    observer.radio_connected(100, tuning)
    air = Air(observer, clock, *contacts)
    air.read()
    return observer, bus, air


def restarted_on(
    tmp_path: Path, air: Air, *, current: bool = False, crash: bool = False, **options: Any
) -> FakeBus:
    """The next run of the observer on the same state file, after a stop that wrote it (or a
    `crash`); the radio keeps its records."""
    bus, store = FakeBus(), new_run(tmp_path, crash=crash)
    if current:
        options["current"] = CurrentDocuments(store)
    air.observer = Observer(
        bus, TOPICS, advert_checks=AdvertChecks(store), wall_clock=air.clock, **options
    )
    return bus


def stored_checks(tmp_path: Path) -> dict[str, Any]:
    checks: dict[str, Any] = JsonStateFile(tmp_path / "state.json").load()["advert_checks"]
    return checks


def contact_events(bus: FakeBus, kind: str = "position_in_advert") -> list[str]:
    """The keys of the `contact` events of that type, in order."""
    return [e["key"] for e in bus.payloads(f"{ROOT}/contact") if e["event_type"] == kind]


def before_the_event(bus: FakeBus, topic: str) -> dict[str, Any]:
    """The newest document on `topic` that went out before the first `position_in_advert`."""
    found: dict[str, Any] = {}
    for t, payload, keep in bus.published:
        if t == f"{ROOT}/contact" and json.loads(payload)["event_type"] == "position_in_advert":
            return found
        if t == topic and keep:
            found = json.loads(payload)
    raise AssertionError("no position_in_advert event")


CHECKED = ("advert_carries_position", "advert_checked_at", "latitude", "position_source")


def checked(bus: FakeBus, key: str) -> tuple[Any, ...]:
    """What a contact's document says about its adverts' position."""
    document = last(bus, f"{ROOT}/contacts/{key}")
    return tuple(document[name] for name in CHECKED)


def test_an_advert_that_carries_the_position_is_checked_then_announced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Its log, the radio's push in the next frame, the table read with its time and position:
    the documents show the check, then one `position_in_advert` follows; a further advert that
    carries one raises none. The check is kept in the state file (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    caplog.set_level(logging.INFO, logger="meshcore_home_gateway.observer")
    shared = air.advert(ALICE, SHARED)
    air.read()
    assert checked(bus, key) == (True, iso(shared), 50.123456, "advert")
    index = last(bus, f"{ROOT}/contacts")
    assert (index["advert_carries_position"], index["advert_carries_position_total"]) == ([key], 1)
    assert bus.payloads(f"{ROOT}/contact") == [
        {
            "event_type": "position_in_advert",
            "key": key,
            "prefix": key[:12],
            "name": "Alice",
            "type": "chat",
            "request": None,
            "id": None,
            "reason": None,
        }
    ]
    assert before_the_event(bus, f"{ROOT}/contacts/{key}")["advert_carries_position"] is True
    assert before_the_event(bus, f"{ROOT}/contacts")["advert_carries_position"] == [key]
    assert stored_checks(tmp_path) == {key: {"advert_at": shared, "carries": True}}
    moved = air.advert(ALICE, MOVED)
    air.read()
    assert checked(bus, key) == (True, iso(moved), 50.124, "advert")
    assert contact_events(bus) == [key]
    assert caplog.text.count(f"contact {key[:12]}: its advert carries its position") == 2


def test_an_advert_without_the_position_turns_the_check_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The radio's record keeps the old position (BaseChatMesh.cpp:193-196), the gateway no
    longer shows it: an advert position goes, a telemetry position stays (ADR 0025). The
    index drops them; no event when they stop."""
    observer, bus, air = checking(tmp_path, ALICE, GREENHOUSE)
    alice, greenhouse = ALICE.public_key.hex(), GREENHOUSE.public_key.hex()
    caplog.set_level(logging.INFO, logger="meshcore_home_gateway.observer")
    air.advert(ALICE, SHARED)
    air.advert(GREENHOUSE, (53_560_000, 10_000_000))  # the position its record holds
    air.read()
    observer.telemetry(greenhouse, answer(NOW + 10))  # and one from telemetry
    stopped = [air.advert(contact) for contact in (ALICE, GREENHOUSE)]
    air.read()
    assert checked(bus, alice) == (False, iso(stopped[0]), None, None)
    assert checked(bus, greenhouse) == (False, iso(stopped[1]), 50.12345, "telemetry")
    assert last(bus, f"{ROOT}/contacts/{alice}")["advert_position"] is True  # the record's
    index = last(bus, f"{ROOT}/contacts")
    assert (index["advert_carries_position"], index["advert_carries_position_total"]) == ([], 0)
    assert index["advert_position_total"] == 2
    assert contact_events(bus) == [alice, greenhouse]  # when they started
    stopped_line = "its advert carries no position (the radio's record keeps an older one)"
    assert caplog.text.count(f"contact {alice[:12]}: {stopped_line}") == 1


def test_an_advert_that_shares_0_0_carries_the_position(tmp_path: Path) -> None:
    """A companion that never had a position shares 0, 0: the record turns to none and an advert
    position goes (ADR 0025); its first located advert raises no second event."""
    _observer, bus, air = checking(tmp_path, GREENHOUSE)
    key = GREENHOUSE.public_key.hex()
    never = air.advert(GREENHOUSE, (0, 0))
    air.read()
    assert checked(bus, key) == (True, iso(never), None, None)
    assert last(bus, f"{ROOT}/contacts/{key}")["advert_position"] is False
    assert contact_events(bus) == [key]
    located = air.advert(GREENHOUSE, SHARED)
    air.read()
    assert checked(bus, key) == (True, iso(located), 50.123456, "advert")
    assert contact_events(bus) == [key]


def test_an_excluded_companion_is_checked_and_announced_once_included(tmp_path: Path) -> None:
    """Not the household's (docs/companions.md): its document shows the check, the index and the
    event leave it out. Included again while its check carries, the event follows its document
    and the index; one whose check carries none raises none (ADR 0025). A repeater is
    not checked."""
    alice, bob, carol = (contact.public_key.hex() for contact in (ALICE, BOB, CAROL))
    exclusions = TelemetryExclusions(None)
    for key in (alice, bob):
        exclusions.exclude(key)
    observer, bus, air = checking(tmp_path, ALICE, BOB, CAROL, exclusions=exclusions)
    shared = air.advert(ALICE, SHARED)
    air.advert(BOB)
    air.advert(CAROL, SHARED)
    air.read()
    assert checked(bus, alice) == (True, iso(shared), 50.123456, "advert")
    assert last(bus, f"{ROOT}/contacts/{bob}")["advert_carries_position"] is False
    assert checked(bus, carol)[:2] == (None, None)
    assert observer._pending == {}  # its push is not kept
    index = last(bus, f"{ROOT}/contacts")
    assert (index["advert_carries_position"], index["advert_carries_position_total"]) == ([], 0)
    assert bus.payloads(f"{ROOT}/contact") == []
    for key in (bob, alice):
        observer.exclusions.include(key)
        observer.telemetry_excluded(key, False)
    assert contact_events(bus) == [alice]
    assert before_the_event(bus, f"{ROOT}/contacts")["advert_carries_position"] == [alice]
    assert before_the_event(bus, f"{ROOT}/contacts/{alice}")["telemetry_excluded"] is False
    observer.telemetry_excluded(alice, False)  # called again: still one event per inclusion
    assert len(contact_events(bus)) == 2


def test_a_restart_neither_raises_nor_clears_a_check(tmp_path: Path) -> None:
    """The checks come back from the state file before the first table read, which compares
    nothing: no event, the same documents. An advert position the history kept stays away while
    the last check carried none (ADR 0025)."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    _observer, bus, air = checking(tmp_path, ALICE, GREENHOUSE, history=history)
    keys = (ALICE.public_key.hex(), GREENHOUSE.public_key.hex())
    air.advert(ALICE, SHARED)
    air.advert(GREENHOUSE, MOVED)
    air.read()
    air.advert(GREENHOUSE)  # it stopped sharing: its record keeps the position
    air.read()
    assert history.last_fixes()[keys[1]].source == "advert"  # stored while it shared
    before = [checked(bus, key) for key in keys]
    assert before[1][2] is None
    history.close()
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = restarted_on(tmp_path, air, history=history)
    air.read()
    assert [checked(bus, key) for key in keys] == before
    assert last(bus, f"{ROOT}/contacts")["advert_carries_position"] == [keys[0]]
    assert bus.payloads(f"{ROOT}/contact") == []
    history.close()


def test_an_advert_made_while_stopped_shows_its_position_while_the_last_check_carried_one(
    tmp_path: Path,
) -> None:
    """The first read of a run compares nothing: an advert made while the gateway was stopped is
    not checked and raises no event. While the last check carried one, the record's position is
    shown, also a new one from that advert, in place of the history's (design.md §6)."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    _observer, bus, air = checking(tmp_path, ALICE, history=history)
    key = ALICE.public_key.hex()
    shared = air.advert(ALICE, SHARED)
    air.read()
    history.close()
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = restarted_on(tmp_path, air, history=history)
    moved = air.advert(ALICE, MOVED, logged=False, pushed=False)  # while it was stopped
    air.read()
    assert checked(bus, key) == (True, iso(shared), 50.124, "advert")
    assert last(bus, f"{ROOT}/contacts/{key}")["position_at"] == iso(moved)
    assert bus.payloads(f"{ROOT}/contact") == []
    history.close()


def test_with_no_receive_delay_the_log_directly_before_the_push_decides(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A forged copy with her key and time, logged first, is not the frame before the push: her
    own log decides, with a warning. With a frame between log and push every copy must agree;
    differing ones decide nothing (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    caplog.set_level(logging.WARNING, logger="meshcore_home_gateway.observer")
    shared = ALICE.last_advert + 60
    air.log(ALICE, shared)  # forged: without the position
    air.advert(ALICE, SHARED, timestamp=shared)
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))
    warning = f"contact {key[:12]}: a logged copy of its advert differs from the one the radio took"
    assert caplog.text.count(warning) == 1
    later = shared + 60
    air.log(ALICE, later, MOVED)  # forged: another position
    air.advert(ALICE, timestamp=later, between=True)  # hers, without the position
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))  # nothing learned
    assert caplog.text.count(warning) == 2


@pytest.mark.parametrize("tuning", [DELAYED, None])
def test_with_a_receive_delay_the_frame_before_the_push_is_not_its_own_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, tuning: codec.TuningParams | None
) -> None:
    """A delayed advert is handled in a later pass, after whatever that pass logged
    (Dispatcher.cpp:138-145): a forged copy can be the frame before the push. So also while the
    radio did not report its delay, the copies must agree; they differ: nothing is learned and
    the record decides (ADR 0025)."""
    alice = replace(ALICE, latitude_e6=SHARED[0], longitude_e6=SHARED[1])  # an earlier advert's
    _observer, bus, air = checking(tmp_path, alice, tuning=tuning)
    caplog.set_level(logging.WARNING, logger="meshcore_home_gateway.observer")
    shared = alice.last_advert + 60
    air.log(alice, shared, SHARED)  # hers, logged; the radio handles it in a later pass
    air.log(alice, shared)  # a forged copy without the position, logged in that pass
    air.advert(alice, SHARED, timestamp=shared, logged=False)
    air.read()
    assert checked(bus, alice.public_key.hex())[:2] == (True, None)
    assert "differs from the one the radio took" in caplog.text


def test_an_advert_the_gateway_cannot_check_is_logged_as_not_checked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One debug line for each advert the radio took that the gateway could not check, with the
    reason (hardware row 44): a push without its log; a newer advert in a read without a push;
    at a start, an advert made while the gateway was stopped. A new position in the record still
    counts, and an unchecked advert keeps the last check (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    line = f"contact {key[:12]}: its advert at {{}} was not checked ({{}})"
    unlogged = air.advert(ALICE, logged=False)  # too long for a frame
    air.read()
    assert caplog.text.count(line.format(iso(unlogged), "not logged")) == 1
    assert checked(bus, key)[:2] == (False, None)
    shared = air.advert(ALICE, SHARED, logged=False)
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))  # the record took a new position
    dropped = air.advert(ALICE, pushed=False)  # its push lost over TCP
    air.read()
    air.read()
    assert caplog.text.count(line.format(iso(dropped), "no report from the radio")) == 1
    assert checked(bus, key)[:2] == (True, iso(shared))
    assert caplog.text.count("was not checked") == 2
    caplog.clear()
    restarted_on(tmp_path, air)
    air.read()  # the next run's first read: the stored check is older than the record's advert
    assert caplog.text.count(line.format(iso(dropped), "no report from the radio")) == 1
    assert caplog.text.count("was not checked") == 1


def not_checked(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """The lines about an advert not checked, with their level."""
    return [(r.levelno, r.getMessage()) for r in caplog.records if "was not checked" in r.message]


TURNED_ON = (
    "; the radio's record holds a position, so Position in adverts is on until an advert without "
    "one is checked"
)


def test_an_unchecked_advert_that_turns_the_warning_on_is_logged_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The owner's case: Alice's record keeps a position from an older advert, her checked advert
    carried none. An advert she made while the gateway was stopped is not checked at the next
    start, and the record's position turns *Position in adverts* on (ADR 0025): one line at info
    level says why, so the journal shows it. A further unchecked advert while it is on, and one
    in the same run whose log was lost, which turns it on again after a checked one, say it at
    debug and at info level."""
    alice = replace(ALICE, latitude_e6=SHARED[0], longitude_e6=SHARED[1])
    key = alice.public_key.hex()
    _observer, _bus, air = checking(tmp_path, alice)
    air.advert(alice)  # checked: no position; the record keeps the older one
    air.read()
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    bus = restarted_on(tmp_path, air)
    missed = air.advert(alice, logged=False, pushed=False)  # while the gateway was stopped
    air.read()
    assert checked(bus, key)[0] is True
    line = f"contact {key[:12]}: its advert at {{}} was not checked ({{}})"
    assert not_checked(caplog) == [
        (logging.INFO, line.format(iso(missed), "no report from the radio") + TURNED_ON)
    ]
    caplog.clear()
    again = air.advert(alice, pushed=False)  # its push lost: still on
    air.read()
    assert not_checked(caplog) == [
        (logging.DEBUG, line.format(iso(again), "no report from the radio"))
    ]
    caplog.clear()
    air.advert(alice)  # checked: off
    air.read()
    assert checked(bus, key)[0] is False
    unlogged = air.advert(alice, logged=False)  # too long for a frame: on again
    air.read()
    assert checked(bus, key)[0] is True
    assert not_checked(caplog) == [
        (logging.INFO, line.format(iso(unlogged), "not logged") + TURNED_ON)
    ]


@pytest.mark.parametrize("carried", [False, True])
def test_an_unchecked_advert_that_leaves_the_warning_as_it_was_is_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, *, carried: bool
) -> None:
    """At a start and in the same run: a record without a position leaves *Position in adverts*
    off, and while the last check carried one it was on already; neither is worth more than the
    debug line (hardware row 44)."""
    alice = replace(ALICE, latitude_e6=SHARED[0], longitude_e6=SHARED[1]) if carried else ALICE
    key = alice.public_key.hex()
    _observer, _bus, air = checking(tmp_path, alice)
    air.advert(alice, SHARED if carried else None)
    air.read()
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    bus = restarted_on(tmp_path, air)
    missed = air.advert(alice, logged=False, pushed=False)
    air.read()
    unlogged = air.advert(alice, logged=False)
    air.read()
    assert checked(bus, key)[0] is carried
    line = f"contact {key[:12]}: its advert at {{}} was not checked ({{}})"
    assert not_checked(caplog) == [
        (logging.DEBUG, line.format(iso(missed), "no report from the radio")),
        (logging.DEBUG, line.format(iso(unlogged), "not logged")),
    ]


def test_a_heard_companions_advert_without_its_log_is_logged_once_when_it_comes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Gail's NEW_ADVERT without its log (too long for a frame) is not checked: one debug line
    when it comes, with that reason. Her heard node keeps the check before. The read that adds her
    shows that advert, which the radio did report: no second line (hardware row 44)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    gail = EmulatedContact(bytes.fromhex("e9" * 32), "Gail")
    key = gail.public_key.hex()
    first = gail.last_advert + 60
    air.heard(gail, timestamp=first)
    air.clock.now += 3600
    unlogged = first + 3600
    air.heard(gail, timestamp=unlogged, logged=False)
    line = f"node {key[:12]}: its advert at {iso(unlogged)} was not checked (not logged)"
    assert caplog.text.count(line) == 1
    air.records[gail.public_key] = replace(gail, last_advert=unlogged)  # added by the gateway
    air.read()
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["advert_checked_at"], document["last_advert_at"]) == (
        iso(first),
        iso(unlogged),
    )
    assert caplog.text.count("was not checked") == 1


def test_an_advert_the_radio_took_before_the_read_that_adds_a_heard_companion_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Gail was heard and checked, and the household adds her. Before the read the radio takes her
    next advert as a contact's, and its report is lost (TCP). The read that adds her shows that
    newer advert: one debug line for it, as for any contact (hardware row 44)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    gail = EmulatedContact(bytes.fromhex("e9" * 32), "Gail")
    key = gail.public_key.hex()
    heard = gail.last_advert + 60
    air.heard(gail, timestamp=heard)
    air.records[gail.public_key] = replace(gail, last_advert=heard)  # added by the gateway
    air.clock.now += 120
    lost = air.advert(gail, timestamp=heard + 1, pushed=False)  # newer by one second is enough
    air.read()
    line = f"contact {key[:12]}: its advert at {iso(lost)} was not checked"
    assert caplog.text.count(f"{line} (no report from the radio)") == 1
    assert caplog.text.count("was not checked") == 1
    assert checked(bus, key)[:2] == (False, iso(heard))


def test_a_table_read_older_than_the_advert_waits_for_the_next_one(tmp_path: Path) -> None:
    """A read that started before the radio took the advert (radio/session.py:491-515) does not
    show it yet: the advert waits for the next read, which decides (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    older = table(*air.records.values())
    stopped = air.advert(ALICE)  # without the position: only its log can tell
    air.observer.radio_event(ContactsRead(older, air.clock.now))
    assert checked(bus, key)[:2] == (False, None)
    air.clock.now += 2
    air.read()
    assert checked(bus, key)[:2] == (False, iso(stopped))


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        ((None, SHARED, None), (False, None, None)),  # she stops sharing
        ((SHARED, None, SHARED), (True, 50.123456, "advert")),  # she shares again from that place
        ((None, None, (0, 0)), (True, None, None)),  # she shares, never having had a position
    ],
    ids=["stop", "start", "0,0"],
)
def test_a_read_that_sent_her_record_before_her_next_advert_waits_for_the_next_one(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    positions: tuple[tuple[int, int] | None, ...],
    expected: tuple[Any, ...],
) -> None:
    """The radio writes one record per pass and handles the mesh between them (MyMesh.cpp:
    2388-2410): a read can show her record from before the advert whose push came during it,
    while the log of her advert before is still in the window. The read waits, the next read
    decides, with no "not checked" line: her newest advert's log is newer than the record, or,
    with a receive delay, the record is no newer than at the read before (ADR 0025)."""
    held, before, after = positions  # her record's, her advert's before, her newest advert's
    alice = replace(ALICE, latitude_e6=held[0], longitude_e6=held[1]) if held else ALICE
    key = alice.public_key.hex()
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    for tuning in (NO_DELAY, DELAYED):
        state = tmp_path / str(tuning.rx_delay_base_milli)
        state.mkdir()
        _observer, bus, air = checking(state, alice, tuning=tuning)
        air.advert(alice, before)
        air.read()
        first = checked(bus, key)
        air.clock.now += 10
        sent = table(*air.records.values())  # the read had sent her record when the radio took it
        newest = air.advert(alice, after)
        air.observer.radio_event(ContactsRead(sent, air.clock.now))
        assert checked(bus, key) == first
        air.clock.now += 2
        air.read()
        assert checked(bus, key) == (expected[0], iso(newest), *expected[1:])
        assert contact_events(bus) == [key]  # when her check turned to carrying
    assert "was not checked" not in caplog.text


@pytest.mark.parametrize("tuning", [DELAYED, None], ids=["delayed", "unknown"])
def test_the_first_read_after_a_restart_waits_for_an_advert_pushed_during_it(
    tmp_path: Path, tuning: codec.TuningParams | None
) -> None:
    """With a receive delay, or none reported, no log is tied to the push, and a run's first read
    has no read before. A read that shows her record from before the push, while her newer log is
    in the window, still waits for the next one, which decides (ADR 0025)."""
    alice = replace(ALICE, latitude_e6=SHARED[0], longitude_e6=SHARED[1])
    key = alice.public_key.hex()
    _observer, bus, air = checking(tmp_path, alice, tuning=tuning)
    shared = air.advert(alice, SHARED)
    air.read()
    bus = restarted_on(tmp_path, air)
    air.observer.radio_connected(100, tuning)
    sent = table(*air.records.values())  # the start's read had sent her record
    stopped = air.advert(alice)  # she stopped sharing
    air.observer.radio_event(ContactsRead(sent, air.clock.now))
    assert checked(bus, key) == (True, iso(shared), 50.123456, "advert")
    air.clock.now += 2
    air.read()
    assert checked(bus, key) == (False, iso(stopped), None, None)


def test_a_check_that_turns_to_carrying_raises_the_event(tmp_path: Path) -> None:
    """She was added after an advert without her position, or stopped sharing; then she shares
    it: `position_in_advert` (ADR 0025, the guide's automation)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    stopped = air.advert(ALICE)
    air.read()
    assert checked(bus, key)[:2] == (False, iso(stopped))
    assert contact_events(bus) == []
    shared = air.advert(ALICE, SHARED)
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))
    assert contact_events(bus) == [key]


def test_sharing_again_from_another_place_replaces_a_telemetry_position(tmp_path: Path) -> None:
    """She stopped sharing and telemetry gave her position. She shares again from another place:
    the changed position in her adverts replaces the telemetry one, as when her last check
    carried one (ADR 0025); the same place again does not."""
    observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    air.advert(ALICE, SHARED)
    air.read()
    stopped = air.advert(ALICE)
    air.read()
    observer.telemetry(key, answer(NOW + 10))
    assert checked(bus, key) == (False, iso(stopped), 50.12345, "telemetry")
    moved = air.advert(ALICE, MOVED)
    air.read()
    assert checked(bus, key) == (True, iso(moved), 50.124, "advert")
    assert last(bus, f"{ROOT}/contacts/{key}")["position_current"] is None  # does not expire
    observer.telemetry(key, answer(NOW + 20))
    air.advert(ALICE)
    air.read()
    again = air.advert(ALICE, MOVED)  # the record's position did not change
    air.read()
    assert checked(bus, key) == (True, iso(again), 50.12345, "telemetry")


def test_sharing_again_from_another_place_without_a_fix_gives_the_adverts_time(
    tmp_path: Path,
) -> None:
    """She stopped sharing and no telemetry gave her position. She shares again from another
    place: the position comes back at the time of the advert that carried it, not at the read
    (docs/mqtt-api.md, `position_at`)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    air.advert(ALICE, SHARED)
    air.read()
    stopped = air.advert(ALICE)
    air.read()
    assert checked(bus, key) == (False, iso(stopped), None, None)
    moved = air.advert(ALICE, MOVED)
    air.clock.now += 5
    air.read()
    assert checked(bus, key) == (True, iso(moved), 50.124, "advert")
    assert last(bus, f"{ROOT}/contacts/{key}")["position_at"] == iso(moved)


def test_a_logged_position_that_is_not_the_records_decides_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The radio copies the position of the advert it took into the record (BaseChatMesh.cpp:
    193-196): a flagged log with another one, directly before the push, is not that advert.
    Nothing is learned, with a warning (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    caplog.set_level(logging.WARNING, logger="meshcore_home_gateway.observer")
    shared = air.advert(ALICE, SHARED)
    air.read()
    record = air.records[ALICE.public_key]
    record.last_advert += 60
    air.log(record, record.last_advert, MOVED)  # the record keeps SHARED
    air.push(record)
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))
    warning = f"contact {key[:12]}: a logged copy of its advert differs from the one the radio took"
    assert caplog.text.count(warning) == 1


def test_a_logged_advert_without_its_push_decides_nothing(tmp_path: Path) -> None:
    """Anyone can transmit a log (unverified, Dispatcher.cpp:199): without the radio's push it is
    never trusted, also when a read shows its time; a new position in the record is (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    air.advert(ALICE, pushed=False)
    air.read()
    assert checked(bus, key)[:2] == (False, None)
    shared = air.advert(ALICE, SHARED, pushed=False)
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))


def test_the_radios_own_advert_heard_back_is_not_checked(tmp_path: Path) -> None:
    observer, bus, air = checking(tmp_path, ALICE)
    published = len(bus.published)
    air.log(EmulatedContact(SELF_KEY, "Bridge"), int(NOW), SHARED)
    air.read()
    assert len(bus.published) == published
    assert observer.mesh.nodes == {}


def test_the_signal_of_an_advert_comes_from_its_own_log(tmp_path: Path) -> None:
    """While the receive delay is 0 the radio writes an advert's log directly before its push,
    also when the push is read late: its hops and SNR. Any other log is another packet (a late
    copy of an older advert, a forged one): without its own log a push takes no signal, as an
    advert without any log; a NEW_ADVERT's log only with the record's time. With a delay, or one
    not known, a log of the key within 10 s, as before (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    first = ALICE.last_advert + 60
    air.log(ALICE, first, hops=1)
    air.push(ALICE)
    air.log(ALICE, first, hops=3, snr_db=-2.0)  # a later flood copy of that advert
    air.log(ALICE, first + 60, hops=2, snr_db=4.5)  # her next advert
    air.clock.now += 15
    air.push(ALICE)

    def signal() -> tuple[Any, ...]:
        document = last(bus, f"{ROOT}/contacts/{key}")
        return document["hops"], document["snr_db"], document["heard_at"]

    assert signal() == (2, 4.5, iso(air.clock.now))
    air.log(ALICE, first + 60, hops=4, snr_db=1.0)  # a late copy of her advert before
    air.log(BOB, BOB.last_advert + 60)  # a stranger's advert: hers was too long for a frame
    air.push(ALICE)
    assert signal() == (None, None, iso(air.clock.now))  # heard, without a signal
    air.log(ALICE, first + 180, hops=5)
    air.frame()  # another frame between
    air.push(ALICE)
    assert signal() == (None, None, iso(air.clock.now))
    for tuning in (DELAYED, None):  # the window, as before
        _observer, bus, air = checking(tmp_path, ALICE, tuning=tuning)
        air.log(ALICE, first, hops=4, snr_db=1.0)
        air.log(BOB, BOB.last_advert + 60)
        air.clock.now += 10
        air.push(ALICE)
        assert signal()[:2] == (4, 1.0)
        air.log(ALICE, first + 60, hops=1)
        air.clock.now += 11
        air.push(ALICE)  # the frame before it, but no longer in the window
        assert signal()[:2] == (None, None)


def test_a_log_no_newer_than_the_record_read_gives_a_push_no_signal(tmp_path: Path) -> None:
    """The radio pushes a contact's advert only when it is newer than its record
    (BaseChatMesh.cpp:133-136), which is at least the one the gateway read. Her newest advert is
    too long for a frame, and a late copy of the advert the gateway read is the frame directly
    before its push: that copy gives no signal. Her next advert's own log gives its signal, also
    before the gateway read the record of the one before (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = ALICE.public_key.hex()
    read = air.advert(ALICE)
    air.read()

    def signal() -> tuple[Any, ...]:
        document = last(bus, f"{ROOT}/contacts/{key}")
        return document["hops"], document["snr_db"], document["heard_at"]

    air.clock.now += 5
    air.log(ALICE, read, hops=3, snr_db=-2.0)  # a late copy of the advert the gateway read
    air.advert(ALICE, logged=False)  # too long for a frame
    assert signal() == (None, None, iso(air.clock.now))  # heard, without a signal
    air.clock.now += 5
    air.log(ALICE, read + 120, hops=2, snr_db=4.5)  # her next advert's own log
    air.push(ALICE)
    assert signal() == (2, 4.5, iso(air.clock.now))


def test_a_heard_nodes_signal_comes_from_its_own_log(tmp_path: Path) -> None:
    """A NEW_ADVERT carries the record made from its advert: while the receive delay is 0 the
    log directly before it is its own only with the record's time. A late copy of an older
    advert there gives no signal (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    key = DAVE.public_key.hex()
    older = DAVE.last_advert + 60
    air.log(DAVE, older, hops=3, snr_db=-2.0)  # a late copy of his advert before
    air.heard(DAVE, timestamp=older + 60, logged=False)  # too long for a frame

    def signal() -> tuple[Any, ...]:
        node = next(n for n in last(bus, f"{ROOT}/nodes")["nodes"] if n["key"] == key)
        return node["hops"], node["snr_db"]

    assert signal() == (None, None)
    event = bus.payloads(f"{ROOT}/node")[0]
    assert (event["key"], event["hops"], event["snr_db"]) == (key, None, None)
    air.heard(DAVE, timestamp=older + 120)  # logged directly before
    assert signal() == (0, 6.0)


def test_with_a_receive_delay_a_log_up_to_40_s_before_the_push_counts(tmp_path: Path) -> None:
    """A flood waits at most 32 s in the radio's queue (Dispatcher.cpp:11): a log 35 s before the
    push decides, one 45 s before does not (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE, BOB, tuning=DELAYED)
    alice = air.advert(ALICE, delay_s=35)
    air.advert(BOB, delay_s=45)
    air.read()
    assert checked(bus, ALICE.public_key.hex())[:2] == (False, iso(alice))
    assert checked(bus, BOB.public_key.hex())[:2] == (False, None)


def test_a_contact_that_leaves_keeps_its_check_only_as_a_heard_node(tmp_path: Path) -> None:
    """Its stored check and its pending advert go; the heard node it becomes keeps the time and
    flag of its check, never a position, and the add takes it back (ADR 0025)."""
    observer, bus, air = checking(tmp_path, ALICE, BOB)
    key = ALICE.public_key.hex()
    shared = air.advert(ALICE, SHARED)
    air.read()
    air.advert(ALICE)  # pushed, not read yet: she stopped sharing
    record = air.records.pop(ALICE.public_key)  # the radio removed her
    air.read()
    assert stored_checks(tmp_path) == {}
    tracked = observer.mesh.nodes[key]
    assert (tracked.advert, tracked.node.position, tracked.fix) == (
        AdvertCheck(shared, True),
        None,
        None,
    )
    assert "latitude" not in json.dumps(last(bus, f"{ROOT}/nodes"))
    air.records[ALICE.public_key] = record  # added back
    air.read()
    assert checked(bus, key)[:2] == (True, iso(shared))
    assert contact_events(bus) == [key, key]  # added with a check that carries


def test_an_exclusion_brings_back_no_position_the_adverts_no_longer_carry(tmp_path: Path) -> None:
    """Excluded, a companion's telemetry position goes, and its record's takes the place only
    while its adverts carry it (ADR 0025)."""
    observer, bus, air = checking(tmp_path, GREENHOUSE)
    key = GREENHOUSE.public_key.hex()
    stopped = air.advert(GREENHOUSE)  # its record keeps the position of an earlier advert
    air.read()
    observer.telemetry(key, answer(NOW + 10))
    assert checked(bus, key) == (False, iso(stopped), 50.12345, "telemetry")
    observer.exclusions.exclude(key)
    observer.telemetry_excluded(key, True)
    assert checked(bus, key) == (False, iso(stopped), None, None)
    assert last(bus, f"{ROOT}/contacts/{key}")["advert_position"] is True


@pytest.mark.parametrize(
    ("state", "data", "then", "expected"),
    [
        ("no_position", LPP_T1000E, None, (None, None)),
        ("no_position", LPP_T1000E, (0, 0), (None, None)),  # the check carries, the place goes
        ("answering", LPP_FULL, None, (50.12345, "telemetry")),
        ("answering", LPP_FULL, (0, 0), (50.12345, "telemetry")),
    ],
    ids=["a check without", "0,0", "telemetry", "telemetry, then 0,0"],
)
def test_a_kept_document_that_loses_its_advert_position_is_stored_at_once(
    tmp_path: Path,
    state: TelemetryState,
    data: bytes,
    then: tuple[int, int] | None,
    expected: tuple[Any, ...],
) -> None:
    """Alice's answers are current, so her document is kept for a restart, with the position from
    her adverts. When that goes (a check that carries none, an advert that shares 0, 0, or a
    position from telemetry), the document is stored at once, not at the tick: after a stop
    without a tick (a crash), the next start brings back no place she stopped sharing, also while
    the radio stays away (ADR 0025, ADR 0024)."""
    key = ALICE.public_key.hex()
    _observer, bus, air = checking(tmp_path, ALICE, current=True)
    air.observer.telemetry_state = lambda _key: state
    air.advert(ALICE, SHARED)
    air.read()
    air.observer.tick()
    assert CurrentDocuments(run_state(tmp_path)).documents[key]["position_source"] == "advert"
    air.clock.now += 30
    air.observer.telemetry(key, answer(air.clock.now, data))
    air.advert(ALICE, then)  # she stopped sharing, or shares 0, 0
    air.read()
    assert checked(bus, key)[0] is (then is not None)
    bus = restarted_on(tmp_path, air, current=True, crash=True)
    air.observer.telemetry_state = lambda _key: state
    air.observer.publish_all()  # the broker connected; the radio is away
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["latitude"], document["position_source"]) == expected


def test_a_kept_document_that_keeps_its_advert_position_is_stored_at_the_tick(
    tmp_path: Path,
) -> None:
    """Alice answers without GPS, and her adverts carry her position. A change that keeps a
    position from adverts (an answer, an advert from another place) waits for the tick, as for
    every kept document: no write of the state file per answer or per advert."""
    key = ALICE.public_key.hex()
    _observer, bus, air = checking(tmp_path, ALICE, current=True)
    store = run_state(tmp_path)
    air.observer.telemetry_state = lambda _key: "no_position"
    air.advert(ALICE, SHARED)
    air.read()
    air.observer.tick()
    stored = on_disk(store)[key]
    air.clock.now += 30
    air.observer.telemetry(key, answer(air.clock.now, LPP_T1000E))
    assert last(bus, f"{ROOT}/contacts/{key}") != stored
    assert on_disk(store)[key] == stored  # until the tick
    air.advert(ALICE, MOVED)
    air.read()
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["latitude"], document["position_source"]) == (50.124, "advert")
    assert on_disk(store)[key] == stored
    air.observer.tick()
    assert on_disk(store)[key] == document


def test_a_companion_heard_before_it_is_added_starts_checked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The gateway adds a heard node without a position and with its advert's time, which the
    radio then refuses as not newer: the advert its NEW_ADVERT carried is the check, and
    the event follows `added` (ADR 0025). A position in the record it made tells also
    without a log. A heard node keeps no position, a heard repeater no check; a stranger's
    differing copy is only a debug line."""
    observer, bus, air = checking(tmp_path, ALICE)
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway.observer")
    erin = EmulatedContact(bytes.fromhex("e1" * 32), "Erin", last_advert=int(NOW) - 40)
    hill = EmulatedContact(bytes.fromhex("e5" * 32), "Hilltop", node_type=2)
    gail = EmulatedContact(bytes.fromhex("e9" * 32), "Gail")
    times = [node.last_advert + 60 for node in (DAVE, erin, hill, gail)]
    air.heard(DAVE, SHARED, timestamp=times[0])
    air.log(erin, times[1], SHARED)  # a forged copy of Erin's advert
    air.heard(erin, timestamp=times[1])
    air.log(hill, times[2], SHARED)  # and of the repeater's
    air.heard(hill, timestamp=times[2])
    air.heard(gail, SHARED, timestamp=times[3], logged=False)  # too long for a frame
    nodes = [observer.mesh.nodes[node.public_key.hex()] for node in (DAVE, erin, hill, gail)]
    assert [tracked.advert for tracked in nodes] == [
        AdvertCheck(times[0], True),
        AdvertCheck(times[1], False),
        None,
        AdvertCheck(times[3], True),  # the position in her record tells, without a log
    ]
    assert [tracked.node.position for tracked in nodes] == [None, None, None, None]
    assert caplog.text.count("node ") == 1  # Erin's copy only
    for node, at in ((DAVE, times[0]), (erin, times[1]), (gail, times[3])):  # added by the gateway
        air.records[node.public_key] = replace(node, last_advert=at)
    air.read()
    dave, erin_key, gail_key = (node.public_key.hex() for node in (DAVE, erin, gail))
    assert checked(bus, dave) == (True, iso(times[0]), None, None)
    assert checked(bus, erin_key)[:2] == (False, iso(times[1]))
    assert checked(bus, gail_key) == (True, iso(times[3]), None, None)
    events = [(e["event_type"], e["key"]) for e in bus.payloads(f"{ROOT}/contact")]
    assert events == [
        ("added", dave),
        ("added", erin_key),
        ("added", gail_key),
        ("position_in_advert", dave),
        ("position_in_advert", gail_key),
    ]
    copy = f"node {erin_key[:12]}: a logged copy of its advert differs from the one the radio took"
    assert copy in caplog.text
    assert "WARNING" not in caplog.text


def test_the_check_of_an_older_advert_does_not_replace_a_newer_one(tmp_path: Path) -> None:
    """Bob was heard, then added with no advert time (a protected contact the profile adds); his
    next advert is older than the one checked (his clock went back): the check of the newer
    advert stays, without an event (ADR 0025)."""
    _observer, bus, air = checking(tmp_path, ALICE)
    heard = DAVE.last_advert + 60
    air.heard(DAVE, timestamp=heard)
    air.records[DAVE.public_key] = replace(DAVE, last_advert=0)
    air.read()
    key = DAVE.public_key.hex()
    assert checked(bus, key)[:2] == (False, iso(heard))
    air.advert(DAVE, SHARED, timestamp=heard - 100)
    air.read()
    assert checked(bus, key)[:2] == (False, iso(heard))
    assert contact_events(bus) == []


def test_a_contact_the_radio_added_by_itself_is_checked_from_its_push(tmp_path: Path) -> None:
    """A radio that adds a node by itself pushes ADVERT before the gateway's table has the
    contact (BaseChatMesh.cpp:156, 200), and firmware 1.11 sends NEW_ADVERT for a contact it has
    just added: either push is checked at the next table read, with one event (ADR 0025)."""
    observer, bus, air = checking(tmp_path, ALICE)
    frank = EmulatedContact(bytes.fromhex("f6" * 32), "Frank", last_advert=int(NOW) - 20)
    air.records[frank.public_key] = replace(frank)  # the radio added him
    added = air.advert(frank, SHARED)
    air.read()
    key = frank.public_key.hex()
    assert checked(bus, key)[:2] == (True, iso(added))
    events = [(e["event_type"], e["key"]) for e in bus.payloads(f"{ROOT}/contact")]
    assert events == [("added", key), ("position_in_advert", key)]
    record = air.records[ALICE.public_key]
    record.last_advert += 60
    air.log(record, record.last_advert)
    advert = codec.Advert(record.public_key, True, contact_record(record))
    observer.radio_event(AdvertHeard(advert, air.clock.now, air.frame(), air.serial))
    air.read()
    assert checked(bus, ALICE.public_key.hex())[:2] == (False, iso(record.last_advert))


def test_a_logged_position_is_held_only_while_it_can_decide(tmp_path: Path) -> None:
    """A log goes after 40 s, a pushed advert without a read after 70 s: at the next log, push
    or read, and on a quiet mesh at the tick. No position from a log is held for longer than
    about two minutes, and no more than 64 logs (ADR 0025)."""
    observer, bus, air = checking(tmp_path, ALICE, BOB)
    for timestamp in range(int(NOW), int(NOW) + MAX_LOGGED + 5):  # a burst, forged ones perhaps
        air.log(ALICE, timestamp, SHARED)
    assert len(observer._logged_adverts) == MAX_LOGGED
    air.clock.now += 41
    air.log(ALICE, int(NOW), SHARED)
    air.clock.now += 41
    air.push(BOB)
    assert list(observer._logged_adverts) == []
    air.log(ALICE, int(NOW), SHARED)
    air.clock.now += 41
    observer.tick()
    assert list(observer._logged_adverts) == []
    air.advert(ALICE, SHARED)
    air.clock.now += 71
    observer.tick()
    assert (list(observer._logged_adverts), observer._pending) == ([], {})
    air.log(ALICE, int(NOW), SHARED)
    air.clock.now += 41
    air.log(BOB, int(NOW))
    assert [entry.key for entry in observer._logged_adverts] == [BOB.public_key.hex()]
    air.advert(BOB)  # the session ends before the table read
    air.clock.now += 71
    air.read()  # the next session's: too late for the pushed advert
    assert checked(bus, BOB.public_key.hex())[:2] == (False, None)
    for timestamp in range(int(NOW), int(NOW) + MAX_LOGGED + 5):  # also once some were dropped
        air.log(ALICE, timestamp, SHARED)
    assert len(observer._logged_adverts) == MAX_LOGGED


def test_after_an_advert_the_gateway_missed_the_value_errs_to_warn(tmp_path: Path) -> None:
    """The last check carried none; while the gateway was stopped the companion adverted, and
    its record holds a position: true from the record, the check's time stays, no event, no
    advert position (ADR 0025)."""
    key = ALICE.public_key.hex()
    stopped = ALICE.last_advert
    AdvertChecks(JsonStateFile(tmp_path / "state.json")).replace({key: AdvertCheck(stopped, False)})
    missed = replace(ALICE, last_advert=stopped + 60, latitude_e6=SHARED[0], longitude_e6=SHARED[1])
    _observer, bus, _air = checking(tmp_path, missed)
    assert checked(bus, key) == (True, iso(stopped), None, None)
    assert bus.payloads(f"{ROOT}/contact") == []


# --- the check end to end, with the radio's frames (ADR 0025) ----------------------------


def with_checks(
    tmp_path: Path, histories: list[History] | None = None
) -> Callable[[FakeBus], Observer]:
    """The bridge's observer, keeping its checks in the state file; with a history when a list
    is given to close it."""

    def observer(bus: FakeBus) -> Observer:
        history = None
        if histories is not None:
            history = History.open(tmp_path / "history.sqlite3")
            assert history is not None
            histories.append(history)
        checks = AdvertChecks(run_state(tmp_path))
        return Observer(bus, TOPICS, history=history, advert_checks=checks, wall_clock=lambda: NOW)

    return observer


async def position_in_advert(bus: FakeBus, key: str) -> None:
    await bus.wait_for(
        f"{ROOT}/contact", lambda e: (e["event_type"], e["key"]) == ("position_in_advert", key)
    )


@pytest.mark.parametrize(("telemetry", "delay"), [(False, 0), (True, 0), (False, 10_000)])
def test_a_companion_shares_its_position_in_adverts_then_stops(
    tmp_path: Path, telemetry: bool, delay: int
) -> None:
    """The event and the index, then both cleared once an advert carries none; the radio's
    record keeps the old position, and so does `advert_position`. The check runs with telemetry
    on or off (ADR 0025), also with a receive delay (its one log decides), and sends no
    command."""
    radio = EmulatedRadio(contacts=[ALICE], tuning=(delay, 1000))
    key = ALICE.public_key.hex()

    async def main() -> None:
        config = make_config(telemetry=telemetry)
        async with running(config, radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            shared = radio.send_advert(ALICE, position=SHARED)
            await position_in_advert(bus, key)
            assert checked(bus, key) == (True, iso(shared), 50.123456, "advert")
            assert last(bus, f"{ROOT}/contacts")["advert_carries_position"] == [key]
            stopped = radio.send_advert(ALICE)
            document = await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["advert_checked_at"] == iso(stopped)
            )
            assert (document["advert_carries_position"], document["advert_position"]) == (
                False,
                True,
            )
            assert document["latitude"] is None
            assert last(bus, f"{ROOT}/contacts")["advert_carries_position_total"] == 0
            assert contact_events(bus) == [key]

    scenario(main)
    assert (radio.contacts[0].latitude_e6, radio.contacts[0].longitude_e6) == SHARED
    assert EXPORT_CONTACT not in radio.commands()


def test_the_buttons_advert_of_a_companion_that_never_had_a_position(tmp_path: Path) -> None:
    """The device's button sends zero hop (no path: `hops` null), and sharing without ever having
    had a position, 0, 0: the advert carries it, the record turns to none (ADR 0025)."""
    radio = EmulatedRadio(contacts=[GREENHOUSE])  # its record holds an earlier advert's position
    key = GREENHOUSE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            never = radio.send_advert(GREENHOUSE, position=(0, 0), zero_hop=True)
            await position_in_advert(bus, key)
            assert checked(bus, key) == (True, iso(never), None, None)
            document = last(bus, f"{ROOT}/contacts/{key}")
            assert (document["advert_position"], document["hops"], document["snr_db"]) == (
                False,
                None,
                6.0,
            )

    scenario(main)
    assert EXPORT_CONTACT not in radio.commands()


@pytest.mark.parametrize("delay", [0, 10_000])
def test_a_forged_copy_logged_before_the_real_advert(tmp_path: Path, delay: int) -> None:
    """Her key and time without the position, logged first. With no receive delay her own log is
    the frame before the push and decides; with a delay the copies differ and only a new
    position in her record tells (ADR 0025)."""
    alice = replace(ALICE, latitude_e6=SHARED[0], longitude_e6=SHARED[1])  # an earlier advert's
    radio = EmulatedRadio(contacts=[alice], tuning=(delay, 1000))
    key = alice.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            same = alice.last_advert + 60
            radio.log_advert(alice, timestamp=same)
            radio.send_advert(alice, timestamp=same, position=SHARED)
            document = await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["last_advert_at"] == iso(same)
            )
            assert document["advert_checked_at"] == (None if delay else iso(same))
            assert document["advert_carries_position"] is True  # checked, or from the record
            radio.log_advert(alice, timestamp=same + 60)
            moved = radio.send_advert(alice, timestamp=same + 60, position=MOVED)
            await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["advert_checked_at"] == iso(moved)
            )
            assert contact_events(bus) == [key]

    scenario(main)
    assert EXPORT_CONTACT not in radio.commands()


def test_with_a_frame_between_log_and_push_the_logs_of_the_advert_decide(tmp_path: Path) -> None:
    """A frame the radio writes between an advert's log and its push (a receive delay above 0):
    the logs with the record's time agree and decide (ADR 0025)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    key = ALICE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            radio.send_advert(ALICE, position=SHARED)
            await position_in_advert(bus, key)
            path = bytes([Push.PATH_UPDATED]) + BOB.public_key
            stopped = radio.send_advert(ALICE, between=path)
            document = await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["advert_checked_at"] == iso(stopped)
            )
            assert document["advert_carries_position"] is False

    scenario(main)
    assert EXPORT_CONTACT not in radio.commands()


def test_an_advert_whose_push_was_dropped_counts_only_by_a_new_position(tmp_path: Path) -> None:
    """Over TCP the radio drops a frame while its queue is full (SerialWifiInterface.cpp:21-40):
    the next table read for another reason shows her newer advert, which only a new position in
    her record can confirm (ADR 0025)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    key = ALICE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            stopped = radio.send_advert(ALICE, pushed=False)
            radio.advertise(BOB)  # a table read follows
            document = await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["last_advert_at"] == iso(stopped)
            )
            assert document["advert_checked_at"] is None
            shared = radio.send_advert(ALICE, position=SHARED, pushed=False)
            radio.advertise(BOB)
            await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["advert_checked_at"] == iso(shared)
            )
            await position_in_advert(bus, key)

    scenario(main)
    assert EXPORT_CONTACT not in radio.commands()


def test_an_advert_too_long_for_the_packet_log_is_checked_by_the_record(tmp_path: Path) -> None:
    """58 one-byte hashes leave no room in a log frame for Alice's advert with a position
    (MyMesh.cpp:291-302): no log, no signal; the new position in her record tells
    (ADR 0025)."""
    radio = EmulatedRadio(contacts=[ALICE])
    key = ALICE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            shared = radio.send_advert(ALICE, position=SHARED, hops=58)
            await position_in_advert(bus, key)
            assert checked(bus, key) == (True, iso(shared), 50.123456, "advert")
            document = last(bus, f"{ROOT}/contacts/{key}")
            assert (document["hops"], document["snr_db"]) == (None, None)

    scenario(main)
    assert EXPORT_CONTACT not in radio.commands()


def test_restarts_raise_nothing_and_an_advert_while_stopped_errs_to_warn(tmp_path: Path) -> None:
    """A restart between two adverts changes nothing. An advert with a position made while the
    gateway was stopped is not checked: `advert_checked_at` stays older than `last_advert_at`,
    the value is the record's (true), no event, and the old advert position stays away
    (ADR 0025)."""
    histories: list[History] = []
    key = ALICE.public_key.hex()
    radios = [EmulatedRadio(contacts=[ALICE])]
    documents: list[dict[str, Any]] = []

    async def run(advert: bool) -> None:
        radio = radios[-1]
        async with running(
            make_config(), radio, tmp_path, observer=with_checks(tmp_path, histories)
        ) as (_, bus, _notes):
            if advert:
                radio.send_advert(ALICE, position=SHARED)
                await position_in_advert(bus, key)
                stopped = radio.send_advert(ALICE)
                await bus.wait_for(
                    f"{ROOT}/contacts/{key}", lambda d: d["advert_checked_at"] == iso(stopped)
                )
            else:
                await bus.wait_for(f"{ROOT}/contacts/{key}", lambda _d: True)
                await asyncio.sleep(0.1)  # the table reads of the start
            documents.append(last(bus, f"{ROOT}/contacts/{key}"))
            assert bus.payloads(f"{ROOT}/contact") == (
                [
                    {
                        "event_type": "position_in_advert",
                        "key": key,
                        "prefix": key[:12],
                        "name": "Alice",
                        "type": "chat",
                        "request": None,
                        "id": None,
                        "reason": None,
                    }
                ]
                if advert
                else []
            )
        radios.append(EmulatedRadio(contacts=radio.contacts))  # the radio keeps its records

    scenario(lambda: run(advert=True))
    scenario(lambda: run(advert=False))
    missed = radios[-1].send_advert(ALICE, position=MOVED, logged=False, pushed=False)
    scenario(lambda: run(advert=False))
    for history in histories:
        history.close()
    first, again, after = documents
    assert again == first
    stopped = first["advert_checked_at"]
    assert (after["advert_carries_position"], after["advert_checked_at"]) == (True, stopped)
    assert (after["last_advert_at"], after["latitude"]) == (iso(missed), None)


def test_a_companion_heard_and_then_added_is_checked_at_the_add(tmp_path: Path) -> None:
    """Its NEW_ADVERT and log are checked at once; the gateway's add writes no position: at
    the table read that adds it, it carries from that advert, and one event follows `added`
    (ADR 0025)."""
    radio = EmulatedRadio(contacts=[ALICE])
    key = DAVE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=with_checks(tmp_path)) as (
            _,
            bus,
            _notes,
        ):
            heard = radio.send_advert(DAVE, position=SHARED)
            await bus.wait_for(f"{ROOT}/nodes", lambda d: [n["key"] for n in d["nodes"]] == [key])
            request = json.dumps({"contact": key, "id": "t1"}).encode()
            bus.contact_requests.put_nowait(ContactRequest("add", request, False))
            await position_in_advert(bus, key)
            assert checked(bus, key) == (True, iso(heard), None, None)
            await asyncio.sleep(0.1)  # the reads after the add
            events = [(e["event_type"], e["key"]) for e in bus.payloads(f"{ROOT}/contact")]
            assert events == [("added", key), ("position_in_advert", key)]

    scenario(main)
    added = next(c for c in radio.contacts if c.public_key == DAVE.public_key)
    assert (added.latitude_e6, added.longitude_e6) == (0, 0)
    assert EXPORT_CONTACT not in radio.commands()
