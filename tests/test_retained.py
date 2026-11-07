"""The gateway's retained topics: what other clients write there is undone (SEC-005)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from meshcore_home_gateway.mqtt import retained as retained_module
from meshcore_home_gateway.mqtt.retained import (
    CLEARS_BURST,
    HEAL_INTERVAL_S,
    WARNING_INTERVAL_S,
    RetainedTopics,
)
from meshcore_home_gateway.mqtt.topics import Topics

TOPICS = Topics("meshcore", "radio1")
KEY = "ab" * 32
CONTACT = TOPICS.contact(KEY)


@dataclass
class Timer:
    at: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class Bus:
    """The broker's side: what was published, the clock and the timers."""

    now: float = 1000.0
    published: list[tuple[str, bytes]] = field(default_factory=list)
    timers: list[Timer] = field(default_factory=list)

    def publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Timer:
        timer = Timer(self.now + delay_s, callback)
        self.timers.append(timer)
        return timer

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for timer in [t for t in self.timers if t.at <= self.now and not t.cancelled]:
            self.timers.remove(timer)
            timer.callback()

    def pending(self) -> list[Timer]:
        return [t for t in self.timers if not t.cancelled]


def make() -> tuple[RetainedTopics, Bus]:
    bus = Bus()
    return RetainedTopics(TOPICS, bus.publish, bus.call_later, lambda: bus.now), bus


def test_the_gateways_retained_topics_are_covered() -> None:
    retained, _ = make()
    for topic in (
        TOPICS.availability,
        TOPICS.state,
        TOPICS.stats,
        TOPICS.contacts,
        CONTACT,
        TOPICS.nodes,
        TOPICS.history,
        TOPICS.pending,
        TOPICS.settings,
        TOPICS.contact("not-a-key"),
    ):
        assert retained.covers(topic), topic
    for topic in (
        TOPICS.received,
        TOPICS.delivery,
        TOPICS.command,
        TOPICS.send("alerts"),
        TOPICS.contact_send(KEY),
        TOPICS.contact_remove(KEY),
        TOPICS.contacts + "/",
        TOPICS.add_contact,
        TOPICS.contact_event,
        "meshcore/radio2/state",
        "homeassistant/status",
    ):
        assert not retained.covers(topic), topic
    assert retained.filters == (
        "meshcore/radio1/availability",
        "meshcore/radio1/contacts",
        "meshcore/radio1/history",
        "meshcore/radio1/nodes",
        "meshcore/radio1/pending",
        "meshcore/radio1/settings",
        "meshcore/radio1/state",
        "meshcore/radio1/stats",
        "meshcore/radio1/contacts/+",
    )


def test_the_gateways_own_payloads_come_back_without_a_word(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Also two quick changes whose copies arrive after both went out."""
    retained, bus = make()
    retained.sent(CONTACT, b"A", published=True)
    retained.sent(CONTACT, b"B", published=True)
    retained.received(CONTACT, b"A", retained=False)
    retained.received(CONTACT, b"B", retained=False)
    retained.received(CONTACT, b"B", retained=False)  # a duplicate of QoS 1: still its own
    assert bus.published == []
    assert caplog.text == ""


def test_another_clients_write_is_undone_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    retained, bus = make()
    retained.sent(CONTACT, b'{"latitude": 1}', published=True)
    retained.received(CONTACT, b'{"latitude": 1}', retained=False)
    retained.received(CONTACT, b'{"latitude": 47}', retained=False)  # a forged position
    assert bus.published == [(CONTACT, b'{"latitude": 1}')]
    assert f"another client wrote {CONTACT}; restoring" in caplog.text
    assert "docs/mqtt-api.md, Securing the broker" in caplog.text
    retained.received(CONTACT, b'{"latitude": 1}', retained=False)  # the restore comes back
    assert len(bus.published) == 1


def test_a_forged_list_of_messages_on_their_way_is_undone() -> None:
    """Another client's `pending` would otherwise show messages the gateway never accepted."""
    retained, bus = make()
    retained.sent(TOPICS.pending, b'{"total": 0}', published=True)
    retained.received(TOPICS.pending, b'{"total": 0}', retained=False)
    retained.received(TOPICS.pending, b'{"total": 1}', retained=False)
    assert bus.published == [(TOPICS.pending, b'{"total": 0}')]


def test_a_cleared_topic_is_restored() -> None:
    """An empty retained message removes the document: Home Assistant's entities go unknown."""
    retained, bus = make()
    retained.sent(TOPICS.state, b'{"radio": "connected"}', published=False)
    retained.received(TOPICS.state, b"", retained=False)
    assert bus.published == [(TOPICS.state, b'{"radio": "connected"}')]


def test_a_writer_that_keeps_writing_is_answered_once_per_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No fight that floods the bus, and no forgery that outlasts the interval."""
    retained, bus = make()
    retained.sent(CONTACT, b"own", published=False)
    retained.received(CONTACT, b"forged 1", retained=False)
    assert len(bus.published) == 1
    bus.advance(1.0)
    retained.received(CONTACT, b"forged 2", retained=False)
    retained.received(CONTACT, b"forged 3", retained=False)
    assert len(bus.published) == 1
    (timer,) = bus.pending()
    assert timer.at == pytest.approx(1000.0 + HEAL_INTERVAL_S)
    bus.advance(HEAL_INTERVAL_S)
    assert bus.published == [(CONTACT, b"own")] * 2
    assert caplog.text.count("another client wrote") == 1  # once a minute per topic
    bus.advance(WARNING_INTERVAL_S)
    retained.received(CONTACT, b"forged 4", retained=False)
    assert "(3 times since the last warning)" in caplog.text
    assert bus.published == [(CONTACT, b"own")] * 3


def test_a_new_payload_of_the_gateway_replaces_a_waiting_restore() -> None:
    retained, bus = make()
    retained.sent(CONTACT, b"own", published=True)
    retained.received(CONTACT, b"forged 1", retained=False)
    retained.received(CONTACT, b"forged 2", retained=False)
    (timer,) = bus.pending()
    retained.sent(CONTACT, b"newer", published=True)
    assert timer.cancelled
    bus.advance(HEAL_INTERVAL_S)
    assert bus.published == [(CONTACT, b"own")]


def test_the_brokers_copies_for_a_new_subscription_are_not_compared() -> None:
    """The connect publishes every document after subscribing: a stale copy is overwritten."""
    retained, bus = make()
    retained.sent(TOPICS.nodes, b"own", published=False)
    retained.received(TOPICS.nodes, b"stale", retained=True)
    retained.received(TOPICS.contact("cd" * 32), b"of the last run", retained=True)
    assert bus.published == []


def test_a_topic_the_gateway_has_not_written_is_cleared_and_not_remembered(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contact's document is invented, or that of a contact the gateway has not read yet in
    this run; its own clear comes back empty and is left alone."""
    retained, bus = make()
    stranger = TOPICS.contact("cd" * 32)
    retained.received(stranger, b'{"name": "Alice"}', retained=False)
    retained.received(stranger, b"", retained=False)  # the clear comes back
    assert bus.published == [(stranger, b"")]
    assert f"another client wrote {stranger}, where the gateway has published nothing" in (
        caplog.text
    )
    assert retained._topics == {}  # anyone can invent topics: none is kept
    monkeypatch.setattr(retained_module.log, "warning", lambda *a: pytest.fail("not again"))
    retained.received(TOPICS.contact("ef" * 32), b"{}", retained=False)  # warned this minute


def test_clears_are_bounded_together() -> None:
    retained, bus = make()
    for n in range(int(CLEARS_BURST) + 5):
        retained.received(TOPICS.contact(f"{n:064x}"), b"{}", retained=False)
    assert len(bus.published) == CLEARS_BURST
    bus.advance(1.0)
    retained.received(TOPICS.contact("ab" * 32), b"{}", retained=False)
    assert len(bus.published) == CLEARS_BURST + 1


def test_a_connect_forgets_what_was_in_flight_and_what_waited() -> None:
    retained, bus = make()
    retained.sent(CONTACT, b"own", published=True)
    retained.received(CONTACT, b"forged 1", retained=False)
    retained.received(CONTACT, b"forged 2", retained=False)
    (timer,) = bus.pending()
    retained.connected()
    assert timer.cancelled
    retained.received(CONTACT, b"own", retained=False)  # equal to the gateway's: no restore
    assert bus.published == [(CONTACT, b"own")]


def test_nothing_is_restored_after_the_close() -> None:
    retained, bus = make()
    retained.sent(CONTACT, b"own", published=True)
    retained.received(CONTACT, b"forged 1", retained=False)
    retained.received(CONTACT, b"forged 2", retained=False)
    (timer,) = bus.pending()
    retained.close()
    assert timer.cancelled
    retained.received(CONTACT, b"forged 3", retained=False)
    timer.callback()  # a timer that fires anyway
    assert bus.published == [(CONTACT, b"own")]


def test_logging_names_the_topic_once_per_minute(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    retained, bus = make()
    retained.sent(TOPICS.state, b"own", published=True)
    for _ in range(3):
        retained.received(TOPICS.state, b"forged", retained=False)
        bus.advance(HEAL_INTERVAL_S)
    assert caplog.text.count("another client wrote") == 1


def test_with_the_drafts_the_chats_draft_topics_are_covered() -> None:
    """The drafts of Home Assistant's chats are the gateway's too: another client's write is
    undone with the draft the gateway holds; their requests are no retained topic."""
    bus = Bus()
    retained = RetainedTopics(TOPICS, bus.publish, bus.call_later, lambda: bus.now, drafts=True)
    draft = TOPICS.target_draft("family")
    for topic in (draft, TOPICS.contact_draft(KEY), "meshcore/radio1/homeassistant/draft/x/y"):
        assert retained.covers(topic), topic
    for topic in (
        TOPICS.target_draft_request("family"),
        "meshcore/radio1/homeassistant/draft/target",
        "meshcore/radio2/homeassistant/draft/target/family",
    ):
        assert not retained.covers(topic), topic
    assert retained.filters[-1] == "meshcore/radio1/homeassistant/draft/+/+"
    retained.sent(draft, b'{"text":"Half a sentence"}', published=True)
    retained.received(draft, b'{"text":"Half a sentence"}', retained=False)
    retained.received(draft, b'{"text":"Send money"}', retained=False)  # forged
    assert bus.published == [(draft, b'{"text":"Half a sentence"}')]
    plain, _ = make()
    assert not plain.covers(draft)  # without Home Assistant's discovery there are no drafts
