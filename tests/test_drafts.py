"""The drafts of Home Assistant's chats (drafts.py, docs/mqtt-api.md): write, then send.

The drafts part runs with the real outbox (`Delivery`) on a fake broker, a fake clock and fake
timers; the scenarios at the end run the whole gateway on the emulated radio.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from meshcore_home_gateway.announcer import Announcer
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.core.mesh import Mesh, Node
from meshcore_home_gateway.drafts import (
    CONTACT,
    ECHO_WINDOW_S,
    EXPIRY_S,
    MAX_REQUEST_BYTES,
    REPEAT_S,
    TARGET,
    Drafts,
)
from meshcore_home_gateway.mqtt.client import MqttBus
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices, contact_discovery_topic
from meshcore_home_gateway.mqtt.discovery import discovery_topic
from meshcore_home_gateway.mqtt.retained import CLEARS_BURST
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.ports import RadioInfo
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from tests.bridge_harness import (
    ALICE,
    BOB,
    FakeBus,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
)
from tests.radio_emulator import EmulatedContact, EmulatedRadio

NOW = 1_800_000_000.0
KEY_A, KEY_B = ALICE.public_key.hex(), BOB.public_key.hex()
CAROL = EmulatedContact(bytes.fromhex("c4" * 32), "Carol")  # a companion that is no target
KEY_C, KEY_D = CAROL.public_key.hex(), "d5" * 32  # Dave: a companion the radio does not hold
FAMILY, ALARM = (TARGET, "family"), (TARGET, "alarm")
ALICE_TARGET, BOB_TARGET = (TARGET, "alice"), (TARGET, "bob")
CAROL_CHAT, DAVE_CHAT = (CONTACT, KEY_C), (CONTACT, KEY_D)
CONFIG_TEXT = f"""
[radio]
transport = "tcp"
tcp_unauthenticated = true
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
insecure = true
[homeassistant]
hidden_targets = ["alarm", "bob"]
[targets.family]
channel = 1
[targets.alarm]
channel = 2
[targets.alice]
contact = "{KEY_A}"
[targets.bob]
contact = "{KEY_B}"
[messages]
dedup_window_s = 300
"""
CONFIG = parse_config(CONFIG_TEXT)
TOPICS = Topics("meshcore", "radio1")


def in_loop(test: Callable[..., None]) -> Callable[..., None]:
    """The test inside a running event loop, which the outbox schedules its `pending` on."""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        async def main() -> None:
            test(*args, **kwargs)

        asyncio.run(main())

    return run


@dataclass
class Timer:
    at: float
    callback: Callable[[], None]
    cancelled: bool = False
    fired: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class Clock:
    """The monotonic clock and the event loop's timers, both in the test's hands."""

    now: float = 1000.0
    timers: list[Timer] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Timer:
        timer = Timer(self.now + delay_s, callback)
        self.timers.append(timer)
        return timer

    def advance(self, seconds: float) -> None:
        end = self.now + seconds
        while due := sorted(
            (t for t in self.timers if not (t.cancelled or t.fired) and t.at <= end),
            key=lambda t: t.at,
        ):
            timer = due[0]
            self.now, timer.fired = max(self.now, timer.at), True
            timer.callback()
        self.now = end

    def running(self) -> list[Timer]:
        return [t for t in self.timers if not (t.cancelled or t.fired)]


class Chats:
    """The drafts part with the real outbox, on a fake broker and a fake clock."""

    def __init__(
        self,
        tmp_path: Path,
        config: Config = CONFIG,
        contacts: tuple[str, ...] = (KEY_C, KEY_D),
        topics: Topics = TOPICS,
        bus: FakeBus | None = None,
    ) -> None:
        self.clock = Clock()
        self.bus = bus or FakeBus()
        self.topics = topics
        observer = Observer(self.bus, topics, wall_clock=lambda: NOW)
        observer.mesh.contacts_read(
            [Node(k, n, NodeType.CHAT) for k, n in ((KEY_A, "Alice"), (KEY_C, "Carol"))], NOW
        )
        self.delivery = Delivery(
            config,
            self.bus,
            topics,
            radio=RadioLink(self.clock),
            observer=observer,
            store=JsonStateFile(tmp_path / f"{topics.root.replace('/', '_')}.json"),
            monotonic=self.clock,
            wall_clock=lambda: NOW,
        )
        self.drafts = Drafts(
            config,
            topics,
            self.bus,
            self.delivery.accept,
            monotonic=self.clock,
            call_later=self.clock.call_later,
            contacts=contacts,
        )

    def request(self, chat: tuple[str, str], payload: bytes, *, retained: bool = False) -> None:
        self.drafts.request(*chat, payload, retained)

    def set(self, chat: tuple[str, str], text: str) -> None:
        self.request(chat, json.dumps({"text": text}).encode())

    def press(self, chat: tuple[str, str]) -> None:
        self.request(chat, b'{"send": true}')

    def topic(self, chat: tuple[str, str]) -> str:
        kind, ident = chat
        return f"{self.topics.root}/homeassistant/draft/{kind}/{ident}"

    def states(self, chat: tuple[str, str]) -> list[str | None]:
        """The draft states published on the chat's topic; None for an empty payload."""
        return [
            json.loads(p)["text"] if p else None
            for t, p, _ in self.bus.published
            if t == self.topic(chat)
        ]

    def events(self) -> list[dict[str, Any]]:
        return self.bus.payloads(f"{self.topics.root}/delivery")

    def held(self, chat: tuple[str, str]) -> str:
        return self.drafts.chats()[chat]


def last(events: list[dict[str, Any]]) -> tuple[str, str | None, str | None, str | None]:
    event = events[-1]
    return event["event_type"], event["target"], event["text"], event["reason"]


# --- 1 a text is stored and echoed -------------------------------------------------------------


@in_loop
def test_a_text_is_stored_and_echoed_retained(tmp_path: Path) -> None:
    """Never an empty payload on a chat's topic: that would delete the retained draft, and Home
    Assistant would read nothing in place of a text the next press still sends."""
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Dinner is ready")
    assert chats.held(FAMILY) == "Dinner is ready"
    assert [(t, p, r) for t, p, r in chats.bus.published if "/draft/" in t] == [
        (chats.topic(FAMILY), b'{"text":"Dinner is ready"}', True)
    ]
    assert chats.bus.qos[chats.topic(FAMILY)] == {1}
    chats.clock.advance(1)
    chats.press(FAMILY)  # queued: cleared
    chats.set(FAMILY, "")
    chats.clock.advance(1)
    chats.drafts.republish()
    chats.drafts.stop()
    assert chats.states(FAMILY) == ["Dinner is ready", "", "", ""]
    assert all(p for t, p, _ in chats.bus.published if "/draft/" in t)
    assert [e["event_type"] for e in chats.events()] == ["queued"]


# --- 2 a press sends the text stored just before it --------------------------------------------


@in_loop
def test_a_press_directly_after_a_text_sends_that_text(tmp_path: Path) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "First")
    chats.press(FAMILY)  # in the same moment: no wait
    assert last(chats.events()) == ("queued", "family", "First", None)
    assert chats.held(FAMILY) == ""


@in_loop
def test_a_text_after_the_press_is_the_next_message(tmp_path: Path) -> None:
    """The press takes the draft at once: a text that comes 50 ms later belongs to the next
    message, and stays in the field."""
    chats = Chats(tmp_path)
    chats.set(FAMILY, "First")
    chats.press(FAMILY)
    chats.clock.advance(0.05)
    chats.set(FAMILY, "Second")
    chats.clock.advance(1)
    assert [e["text"] for e in chats.events()] == ["First"]
    assert chats.held(FAMILY) == "Second"
    assert chats.states(FAMILY)[-1] == "Second"


@in_loop
def test_the_second_tap_of_a_double_tap_ends_silently_and_a_later_one_is_refused(
    tmp_path: Path,
) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Hello")
    chats.press(FAMILY)
    assert last(chats.events()) == ("queued", "family", "Hello", None)
    sent = len(chats.bus.published)
    chats.clock.advance(REPEAT_S - 0.1)
    chats.press(FAMILY)  # nothing set since: the double tap's second
    assert len(chats.events()) == 1
    assert len(chats.bus.published) == sent  # no event, no state
    chats.clock.advance(0.2)  # 2.1 s after the queued press
    chats.press(FAMILY)
    assert last(chats.events()) == ("rejected", "family", None, "empty")
    assert len(chats.events()) == 2


# --- 3, 4 queued clears; refused and duplicate keep the draft -----------------------------------


@in_loop
def test_a_queued_press_clears_the_draft_and_a_refused_one_keeps_it(tmp_path: Path) -> None:
    strict = parse_config(CONFIG_TEXT.replace("dedup_window_s = 300", "truncate = false"))
    chats = Chats(tmp_path, strict)
    long = "é" * 100  # 100 characters, within `max`; 200 bytes, more than the room
    chats.set(FAMILY, long)
    chats.press(FAMILY)
    assert last(chats.events()) == ("rejected", "family", None, "too_long")
    assert chats.held(FAMILY) == long
    chats.set(DAVE_CHAT, "Are you there?")  # a companion the radio no longer holds
    chats.press(DAVE_CHAT)
    assert last(chats.events()) == ("rejected", KEY_D, None, "unknown_contact")
    assert chats.held(DAVE_CHAT) == "Are you there?"
    chats.set(CAROL_CHAT, "Back soon")
    chats.press(CAROL_CHAT)
    assert last(chats.events())[:3] == ("queued", KEY_C, "Back soon")
    assert chats.held(CAROL_CHAT) == ""
    assert chats.states(CAROL_CHAT)[-1] == ""


@in_loop
def test_a_duplicate_keeps_the_draft(tmp_path: Path) -> None:
    """The duplicate line asks the user to change the text: it must still be there."""
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Same")
    chats.press(FAMILY)
    chats.clock.advance(REPEAT_S + 1)
    chats.set(FAMILY, "Same")
    chats.press(FAMILY)
    assert last(chats.events())[0] == "duplicate"
    assert chats.held(FAMILY) == "Same"
    assert chats.states(FAMILY)[-1] == "Same"


@in_loop
def test_a_blank_draft_is_refused_as_empty_and_stays(tmp_path: Path) -> None:
    chats = Chats(tmp_path)
    chats.press(FAMILY)  # nothing written at all
    assert last(chats.events()) == ("rejected", "family", None, "empty")
    chats.set(FAMILY, "   ")
    chats.press(FAMILY)
    assert last(chats.events()) == ("rejected", "family", None, "empty")
    assert chats.held(FAMILY) == "   "
    assert not [e for e in chats.events() if e["event_type"] == "queued"]


# --- 5, 6 left-overs and malformed requests ----------------------------------------------------


@in_loop
def test_a_retained_request_is_ignored_without_an_event(tmp_path: Path) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Kept")
    chats.clock.advance(1)
    before = list(chats.bus.published)
    chats.request(FAMILY, b'{"text": "Left over"}', retained=True)
    chats.request(FAMILY, b'{"send": true}', retained=True)
    chats.clock.advance(1)
    assert chats.bus.published == before
    assert chats.held(FAMILY) == "Kept"


@in_loop
def test_a_malformed_request_is_ignored_and_the_draft_published_again(tmp_path: Path) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Kept")
    room = chats.drafts._drafts[FAMILY].max  # 158 while the radio's name is not known
    assert room == 158
    for payload in (
        b'{"text": 5}',
        b'{"text": null}',
        b'{"send": false}',
        b'{"send": 1}',
        b'{"send": "true"}',
        b'{"text": "a", "send": true}',
        b"{}",
        b'{"Text": "a"}',
        b"[]",
        b'"text"',
        b"Hello",
        b"",
        b'{"text": "\xff"}',  # not UTF-8
        json.dumps({"text": "x" * (room + 1)}).encode(),  # longer than `max`
        b'{"text": "Hi"' + b" " * MAX_REQUEST_BYTES + b"}",  # larger than a request may be
    ):
        chats.clock.advance(1)
        published = len(chats.states(FAMILY))
        chats.request(FAMILY, payload)
        assert chats.held(FAMILY) == "Kept", payload
        assert chats.states(FAMILY)[published:] == ["Kept"], payload  # the field shows it again
    assert chats.events() == []
    chats.set(FAMILY, "x" * room)  # exactly `max` characters: taken
    assert chats.held(FAMILY) == "x" * room


@in_loop
def test_a_text_of_max_characters_is_taken_as_home_assistant_writes_it(tmp_path: Path) -> None:
    """Home Assistant's `tojson` writes every character outside ASCII as an escape, an emoji as
    two: 12 bytes for one character of `max`."""
    chats = Chats(tmp_path)
    room = chats.drafts._drafts[FAMILY].max
    text = "\N{SMILING FACE WITH OPEN MOUTH}" * room
    payload = json.dumps({"text": text}).encode()  # ensure_ascii, as Jinja's tojson
    assert len(payload) == 12 * room + len('{"text": ""}')
    chats.request(FAMILY, payload)
    assert chats.held(FAMILY) == text
    assert chats.states(FAMILY) == [text]


# --- 7 the echo limiter ------------------------------------------------------------------------


@in_loop
def test_echoes_go_out_on_the_leading_edge_and_the_last_of_a_burst_follows(
    tmp_path: Path,
) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "a")
    assert chats.states(FAMILY) == ["a"]  # at once
    chats.clock.advance(0.05)
    chats.set(FAMILY, "ab")
    chats.clock.advance(0.05)
    chats.set(FAMILY, "abc")
    assert chats.states(FAMILY) == ["a"]  # held
    chats.clock.advance(ECHO_WINDOW_S - 0.1 + 0.001)  # the window of the first ends
    assert chats.states(FAMILY) == ["a", "abc"]  # only the last
    chats.clock.advance(1)
    assert chats.states(FAMILY) == ["a", "abc"]
    chats.set(FAMILY, "abcd")  # a new burst: at once again
    assert chats.states(FAMILY) == ["a", "abc", "abcd"]


@in_loop
def test_the_clear_after_a_press_goes_out_at_once_and_no_held_echo_follows(
    tmp_path: Path,
) -> None:
    """The blur's text comes 1-4 ms before the press: a clear that waited for the limiter's
    window would cut the next message being typed (design §6.7)."""
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Hi")
    chats.clock.advance(0.002)
    chats.press(FAMILY)
    assert chats.states(FAMILY) == ["Hi", ""]  # inside the press's handler
    assert chats.clock.running() == []  # no timer: the clear waits for nothing
    chats.clock.advance(1)
    chats.set(CAROL_CHAT, "A")
    chats.set(CAROL_CHAT, "AB")  # held by the window
    chats.clock.advance(0.002)
    chats.press(CAROL_CHAT)
    assert chats.states(CAROL_CHAT) == ["A", ""]
    chats.clock.advance(1)
    assert chats.states(CAROL_CHAT) == ["A", ""]  # the held "AB" never follows the clear
    assert last(chats.events())[:3] == ("queued", KEY_C, "AB")


# --- 8, 9 connects, births, start and stop -----------------------------------------------------


@in_loop
def test_every_draft_goes_out_as_held_at_a_connect_and_a_birth(tmp_path: Path) -> None:
    """Never "" in place of a held draft: the next press would send a text nobody sees."""
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Half a sent")
    chats.set(FAMILY, "Half a sentence")  # held by the window
    chats.drafts.republish()  # at once, also inside the window
    assert chats.states(FAMILY) == ["Half a sent", "Half a sentence"]
    assert chats.states(ALICE_TARGET) == chats.states(CAROL_CHAT) == [""]
    chats.clock.advance(1)
    assert chats.states(FAMILY) == ["Half a sent", "Half a sentence"]  # nothing held any more
    assert chats.states(ALARM) == chats.states(BOB_TARGET) == []  # hidden: no chat


@in_loop
def test_a_clean_stop_clears_every_draft_that_is_not_empty(tmp_path: Path) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Unsent")
    chats.set(CAROL_CHAT, "Also unsent")
    chats.drafts.stop()
    assert chats.states(FAMILY) == ["Unsent", ""]
    assert chats.states(CAROL_CHAT) == ["Also unsent", ""]
    assert chats.states(ALICE_TARGET) == []  # empty already
    assert chats.clock.running() == []
    published = list(chats.bus.published)
    chats.set(FAMILY, "Typed while the gateway stops")  # it would stay retained
    chats.press(CAROL_CHAT)
    assert chats.bus.published == published
    assert chats.held(FAMILY) == ""


def test_the_gateway_starts_with_empty_drafts_and_clears_them_when_it_stops(
    tmp_path: Path,
) -> None:
    """At the connect every chat's draft is "", after the discovery; at the stop a draft that is
    not empty is cleared before the gateway disconnects."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    config = make_config()

    async def main() -> None:
        bus = FakeBus()
        stopped_at: list[int] = []
        bus.on_stopping.append(lambda: stopped_at.append(len(bus.published)))
        async with running(config, radio, tmp_path, bus=bus) as (_, bus, _n):
            topics = [t for t, _, _ in bus.published]
            for alias in ("alerts", "alice", "bob"):
                topic = f"meshcore/radio1/homeassistant/draft/target/{alias}"
                assert (topic, b'{"text":""}', True) in bus.published
                assert topics.index(topic) > topics.index("homeassistant" + GATEWAY_DOCUMENT)
            for callback in bus.on_draft_request:
                callback(TARGET, "alerts", b'{"text": "Unsent"}', False)
        draft = "meshcore/radio1/homeassistant/draft/target/alerts"
        states = [(i, p) for i, (t, p, _) in enumerate(bus.published) if t == draft]
        assert [p for _, p in states] == [b'{"text":""}', b'{"text":"Unsent"}', b'{"text":""}']
        (stopping,) = stopped_at
        assert states[-1][0] >= stopping  # cleared at the stop, before the bus disconnects
        assert bus.stopped

    scenario(main)


GATEWAY_DOCUMENT = "/device/meshcore_home_gateway_radio1/config"


def test_home_assistants_birth_publishes_every_draft_as_held(tmp_path: Path) -> None:
    """A Home Assistant that started again reads the drafts the gateway holds, at once."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            for callback in bus.on_draft_request:
                callback(TARGET, "alerts", b'{"text": "Half a sentence"}', False)
            start = len(bus.published)
            for birth in bus.on_ha_online:
                birth()
            draft = "meshcore/radio1/homeassistant/draft/target/alerts"
            published = [p for t, p, _ in bus.published[start:] if t == draft]
            assert published == [b'{"text":"Half a sentence"}']

    scenario(main)


# --- 10 expiry ---------------------------------------------------------------------------------


@in_loop
def test_a_draft_unchanged_for_15_minutes_is_cleared(tmp_path: Path) -> None:
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Forgotten")
    chats.clock.advance(10 * 60)
    chats.set(FAMILY, "Forgotten")  # the same text again: no change
    chats.clock.advance(EXPIRY_S - 10 * 60 - 1)
    assert chats.held(FAMILY) == "Forgotten"
    chats.clock.advance(1)
    assert chats.held(FAMILY) == ""
    assert chats.states(FAMILY)[-1] == ""
    chats.set(CAROL_CHAT, "Changed later")
    chats.clock.advance(10 * 60)
    chats.set(CAROL_CHAT, "Changed later, twice")
    chats.clock.advance(EXPIRY_S - 1)
    assert chats.held(CAROL_CHAT) == "Changed later, twice"  # 15 minutes from its last change
    chats.clock.advance(1)
    assert chats.held(CAROL_CHAT) == ""
    assert chats.events() == []  # nothing was sent


@in_loop
def test_an_expiry_timer_that_fires_early_or_late_clears_only_an_unchanged_draft(
    tmp_path: Path,
) -> None:
    """The event loop's timers may fire a little early; a timer whose draft was sent meanwhile
    clears nothing."""
    chats = Chats(tmp_path)
    chats.set(FAMILY, "Forgotten")
    (expiry,) = [t for t in chats.clock.running() if t.at == chats.clock.now + EXPIRY_S]
    chats.clock.now += EXPIRY_S - 5
    expiry.fired = True
    expiry.callback()  # 5 s early
    assert chats.held(FAMILY) == "Forgotten"
    chats.clock.advance(5)
    assert chats.held(FAMILY) == ""
    chats.set(CAROL_CHAT, "Sent soon")
    (stale,) = [t for t in chats.clock.running() if t.at == chats.clock.now + EXPIRY_S]
    chats.press(CAROL_CHAT)  # queued: cleared, its timer cancelled
    published = len(chats.bus.published)
    stale.callback()  # had it fired all the same
    assert len(chats.bus.published) == published


# --- 11 chats the gateway does not have --------------------------------------------------------


@in_loop
def test_a_hidden_target_and_a_contact_targets_key_have_no_chat(tmp_path: Path) -> None:
    """A press on the kept companion pair of a contact target's key would send without the
    target's prefix, suffix and priority."""
    chats = Chats(tmp_path, contacts=(KEY_A, KEY_B, KEY_C))  # kept pairs of Alice and Bob
    chats.drafts.contacts(frozenset({KEY_A, KEY_B, KEY_C}))
    no_chat = (BOB_TARGET, ALARM, (CONTACT, KEY_A), (CONTACT, KEY_B), (TARGET, "nowhere"))
    for chat in (*no_chat, ("other", "family")):
        chats.set(chat, "Hello")
        chats.press(chat)
    assert chats.events() == []
    assert [t for t, _, _ in chats.bus.published if "/draft/" in t] == []
    assert set(chats.drafts.chats()).isdisjoint(no_chat)
    assert CAROL_CHAT in chats.drafts.chats()


# --- 12 chats that go, and the broker's copies ------------------------------------------------


@in_loop
def test_a_removed_contacts_draft_topic_is_deleted_after_its_device(tmp_path: Path) -> None:
    chats = Chats(tmp_path, contacts=())
    devices = ContactDevices(
        CONFIG,
        TOPICS,
        chats.bus,
        ContactDeviceRecords(JsonStateFile(tmp_path / "devices.json")),
        wall_clock=lambda: NOW,
    )
    devices.on_writable.append(chats.drafts.contacts)
    mesh = Mesh()
    nodes = [Node(KEY_A, "Alice", NodeType.CHAT), Node(KEY_C, "Carol", NodeType.CHAT)]
    mesh.contacts_read(nodes, NOW)
    devices.update(mesh)
    assert chats.states(CAROL_CHAT) == [""]  # a new chat
    assert chats.states((CONTACT, KEY_A)) == []  # a contact target: the target's chat
    chats.set(CAROL_CHAT, "See you")
    mesh.contacts_read(nodes[:1], NOW)
    devices.update(mesh)
    topics = [(t, p) for t, p, _ in chats.bus.published]
    gone = topics.index((chats.topic(CAROL_CHAT), b""))
    assert topics.index((contact_discovery_topic(CONFIG, KEY_C), b"")) < gone
    assert CAROL_CHAT not in chats.drafts.chats()
    chats.set(CAROL_CHAT, "Too late")
    assert chats.states(CAROL_CHAT) == ["", "See you", None]


@in_loop
def test_the_brokers_copies_of_drafts_no_chat_owns_are_deleted_at_a_bounded_rate(
    tmp_path: Path,
) -> None:
    chats = Chats(tmp_path)
    chats.drafts.retained_copy(*FAMILY, b'{"text": "Of an earlier run"}')
    assert chats.bus.published == []  # a chat of this run: the connect's republish answers it
    for index in range(int(CLEARS_BURST) + 5):
        chats.drafts.retained_copy(CONTACT, f"{index:064x}", b'{"text": "Stale"}')
    deleted = [t for t, p, r in chats.bus.published if p == b"" and r]
    assert len(deleted) == CLEARS_BURST
    assert deleted[0] == chats.topic((CONTACT, f"{0:064x}"))
    chats.clock.advance(1)
    chats.drafts.retained_copy(CONTACT, f"{99:064x}", b'{"text": "Stale"}')
    chats.drafts.retained_copy(CONTACT, f"{98:064x}", b"")  # deleted already
    assert len([p for _, p, _ in chats.bus.published if p == b""]) == CLEARS_BURST + 1


def test_the_gateway_deletes_the_brokers_copy_of_a_draft_no_chat_owns(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            start = len(bus.published)
            for copy in bus.on_draft_retained:
                copy(CONTACT, KEY_D, b'{"text": "Of an earlier run"}')
                copy(TARGET, "alerts", b'{"text": "Of an earlier run"}')  # the republish's
            deleted = [(t, p) for t, p, r in bus.published[start:] if r]
            assert deleted == [(f"meshcore/radio1/homeassistant/draft/contact/{KEY_D}", b"")]

    scenario(main)


def test_the_brokers_copies_of_drafts_reach_the_drafts_before_the_retained_guard() -> None:
    """The guard takes no retained copy: behind it, a stale draft would never be deleted."""

    async def main() -> None:
        paho = SimpleNamespace(
            published=[],
            calls=[],
            on_connect=None,
            on_message=None,
        )

        class Paho:
            on_connect: Any = None
            on_message: Any = None

            def __getattr__(self, name: str) -> Callable[..., None]:
                return lambda *_, **__: None

            def publish(self, topic: str, payload: bytes, *args: Any, **kwargs: Any) -> Any:
                paho.published.append((topic, payload))
                return SimpleNamespace()

        fake = Paho()
        own = "homeassistant/device/meshcore_home_gateway_radio1/config"
        bus = MqttBus(CONFIG.mqtt, TOPICS, "homeassistant/status", lambda _: fake, ha_devices=[own])
        copies: list[tuple[str, str, bytes]] = []
        requests: list[tuple[str, str, bytes, bool]] = []
        bus.on_draft_retained.append(lambda *copy: copies.append(copy))
        bus.on_draft_request.append(lambda *request: requests.append(request))
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)

        def deliver(topic: str, payload: bytes, retain: bool) -> None:
            fake.on_message(
                fake, None, SimpleNamespace(topic=topic, payload=payload, retain=retain)
            )

        stale = f"meshcore/radio1/homeassistant/draft/contact/{KEY_D}"
        await asyncio.to_thread(deliver, stale, b'{"text": "Stale"}', True)
        await asyncio.to_thread(deliver, stale, b'{"text": "Forged"}', False)  # the guard's
        await asyncio.to_thread(deliver, stale + "/request", b'{"send": true}', False)
        await asyncio.to_thread(deliver, stale + "/request", b'{"send": true}', True)
        other = "meshcore/radio2/homeassistant/draft/target/family/request"
        await asyncio.to_thread(deliver, other, b'{"send": true}', False)
        await asyncio.sleep(0.05)
        assert copies == [(CONTACT, KEY_D, b'{"text": "Stale"}')]
        assert requests == [
            (CONTACT, KEY_D, b'{"send": true}', False),
            (CONTACT, KEY_D, b'{"send": true}', True),
        ]
        assert (stale, b"") in paho.published  # another client's draft of no chat: cleared

    asyncio.run(main())


# --- 13 two gateways ---------------------------------------------------------------------------


@in_loop
def test_two_node_ids_keep_their_drafts_apart(tmp_path: Path) -> None:
    bus = FakeBus()
    one = Chats(tmp_path, bus=bus)
    two = Chats(tmp_path, topics=Topics("meshcore", "radio2"), bus=bus)
    one.set(FAMILY, "From one")
    two.set(FAMILY, "From two")
    assert (one.held(FAMILY), two.held(FAMILY)) == ("From one", "From two")
    assert one.states(FAMILY) == ["From one"]
    assert two.states(FAMILY) == ["From two"]
    request = two.topics.target_draft_request("family")
    assert one.topics.draft_from_request_topic(request) is None
    assert two.topics.draft_from_request_topic(request) == FAMILY


# --- 14 the press takes the outbox's way at once -----------------------------------------------


def test_the_press_is_queued_or_refused_inside_its_handler(tmp_path: Path) -> None:
    """No queue between the text and the press: the press's delivery event and the clear are
    published before the handler returns, and nothing waits in the request queue."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            before = len(bus.published)
            for payload in (b'{"text": "Gate open"}', b'{"send": true}'):
                for callback in bus.on_draft_request:
                    callback(TARGET, "alerts", payload, False)
            published = [(t, json.loads(p)) for t, p, _ in bus.published[before:]]
            assert bus.requests.empty()
            queued = [p for t, p in published if t.endswith("/delivery")]
            assert [(q["event_type"], q["text"]) for q in queued] == [("queued", "Gate open")]
            draft = "meshcore/radio1/homeassistant/draft/target/alerts"
            assert [p["text"] for t, p in published if t == draft] == ["Gate open", ""]
            await bus.wait_for("/history", lambda d: d["total"] == 1)

    scenario(main)


# --- 15 a lower `max` ------------------------------------------------------------------------


def radio_named(name: str) -> RadioInfo:
    return RadioInfo(
        SelfInfo(bytes.fromhex("e6" * 32), name, 22, 22, 869_618, 62_500, 8, 8, True),
        DeviceInfo(9, 350, 40, "1 Oct 2026", "Heltec V3", "v1.18.0", 0),
    )


@in_loop
def test_a_draft_longer_than_a_lowered_max_is_cut_before_the_discovery(tmp_path: Path) -> None:
    """Home Assistant cannot take a state longer than `max`, and writes the state again as it
    takes a discovery (home-assistant/tests/test_drafts.py): the radio's name lowers a channel's
    room once the radio connects, so the cut draft goes out first, under the old `max`."""
    chats = Chats(tmp_path)
    link = RadioLink(chats.clock)
    announcer = Announcer(
        CONFIG,
        TOPICS,
        chats.bus,
        link,
        publish_state=lambda: None,
        drafts=chats.drafts,
        monotonic=chats.clock,
        wall_clock=lambda: NOW,
    )
    announcer.announce()  # the radio not known yet: 158
    chats.set(FAMILY, "x" * 155)
    assert chats.held(FAMILY) == "x" * 155
    chats.clock.advance(1)
    link.info = radio_named("Garden")  # 6 bytes: 160 - 6 - 2 = 152
    start = len(chats.bus.published)
    announcer.announce()
    after = [(t, p) for t, p, _ in chats.bus.published[start:]]
    documents = [json.loads(p) for t, p in after if t == discovery_topic(CONFIG)]
    assert documents[-1]["cmps"]["write_family"]["max"] == 152
    discovery = max(i for i, (t, _) in enumerate(after) if t == discovery_topic(CONFIG))
    drafts = [
        (i, json.loads(p)["text"]) for i, (t, p) in enumerate(after) if t == chats.topic(FAMILY)
    ]
    assert drafts == [(0, "x" * 152)]  # before the discovery
    assert discovery > 0
    assert chats.held(FAMILY) == "x" * 152
    chats.set(FAMILY, "x" * 153)  # over the new `max`: ignored
    assert chats.held(FAMILY) == "x" * 152


# --- the emulated radio ----------------------------------------------------------------------


def test_a_draft_and_its_press_are_sent_once_and_reach_the_history(tmp_path: Path) -> None:
    """A target's chat and a companion's: each message goes out once, and the history names the
    target, or none for a companion (its key is `contact_key`)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB, CAROL])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, devices=True) as (_, bus, _n):
            await bus.wait_for(f"/draft/contact/{KEY_C}", lambda d: d["text"] == "")

            def chat(kind: str, ident: str, *payloads: bytes) -> None:
                for payload in payloads:
                    for callback in bus.on_draft_request:
                        callback(kind, ident, payload, False)

            chat(TARGET, "alerts", b'{"text": "Dinner is ready"}', b'{"send": true}')
            chat(CONTACT, KEY_C, b'{"text": "Back soon"}', b'{"send": true}', b'{"send": true}')
            history = await bus.wait_for("/history", lambda d: d["total"] == 2)
            entries = {m["text"]: m for m in history["messages"]}
            assert entries["Dinner is ready"]["target"] == "alerts"
            assert entries["Back soon"]["target"] is None
            assert entries["Back soon"]["contact_key"] == KEY_C
            assert [text for _, text, _ in radio.channel_texts] == ["Dinner is ready"]
            await asyncio.sleep(0.2)
            assert len(bus.payloads("/history")[-1]["messages"]) == 2  # once each

    scenario(main)
