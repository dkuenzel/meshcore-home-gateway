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
        TOPICS.config,
        TOPICS.settings,
        TOPICS.refused_commands,
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
        "meshcore/radio1/config",
        "meshcore/radio1/contacts",
        "meshcore/radio1/history",
        "meshcore/radio1/nodes",
        "meshcore/radio1/pending",
        "meshcore/radio1/refused_commands",
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


def test_a_forged_configuration_is_undone() -> None:
    """Another client's `config` would otherwise show every user of Home Assistant settings the
    gateway does not have, such as a target that is not hidden."""
    retained, bus = make()
    retained.sent(TOPICS.config, b'{"targets": {"total": 0}}', published=True)
    retained.received(TOPICS.config, b'{"targets": {"total": 0}}', retained=False)
    retained.received(TOPICS.config, b'{"targets": {"total": 9}}', retained=False)
    assert bus.published == [(TOPICS.config, b'{"targets": {"total": 0}}')]


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


def test_what_paho_sends_again_after_a_reconnect_is_the_gateways_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#101: paho sends a QoS 1 message again after a reconnect when the broker had not
    acknowledged it, such as the state written as the broker stopped. Its copy is the gateway's,
    not another client's write; the connect's own payload follows it, so nothing is restored."""
    retained, bus = make()
    retained.sent(TOPICS.state, b'{"radio": "disconnected"}', published=True)  # broker stopping
    retained.connected()  # the broker is back
    retained.sent(TOPICS.state, b'{"radio": "connected"}', published=True)  # the connect's
    retained.received(TOPICS.state, b'{"radio": "disconnected"}', retained=False)  # paho's
    retained.received(TOPICS.state, b'{"radio": "connected"}', retained=False)
    assert bus.published == []
    assert caplog.text == ""


def test_a_payload_sent_again_after_the_current_one_is_followed_by_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """paho may send the old payload after the connect's: the broker would keep the old one, so
    the current one goes out again, without a warning. Each copy is taken as paho's once."""
    retained, bus = make()
    retained.sent(TOPICS.state, b"old", published=True)
    retained.connected()
    retained.sent(TOPICS.state, b"current", published=True)
    retained.received(TOPICS.state, b"current", retained=False)
    retained.received(TOPICS.state, b"old", retained=False)
    assert bus.published == [(TOPICS.state, b"current")]
    assert caplog.text == ""
    retained.received(TOPICS.state, b"current", retained=False)  # the restore comes back
    retained.received(TOPICS.state, b"old", retained=False)  # paho's was taken: a forgery now
    assert f"another client wrote {TOPICS.state}; restoring" in caplog.text


def test_what_was_in_flight_stays_the_gateways_over_two_reconnects() -> None:
    """The broker may go away again before paho's second try is acknowledged."""
    retained, bus = make()
    retained.sent(CONTACT, b"old", published=True)
    retained.connected()
    retained.sent(CONTACT, b"newer", published=True)
    retained.connected()
    retained.sent(CONTACT, b"current", published=True)
    for payload in (b"old", b"newer", b"current"):
        retained.received(CONTACT, payload, retained=False)
    assert bus.published == []


def test_what_paho_sends_again_after_its_copy_came_back_is_the_gateways_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The broker sent the copy and then stopped, before paho read its acknowledgement: paho
    sends the payload again after the reconnect (docs/platform-notes.md). That copy is the
    gateway's too; it came after the current one, so the current one goes out again."""
    retained, bus = make()
    retained.sent(TOPICS.state, b"old", published=True)
    retained.received(TOPICS.state, b"old", retained=False)  # its copy, before the stop
    retained.connected(unacked=[(TOPICS.state, b"old")])  # no acknowledgement came
    retained.sent(TOPICS.state, b"current", published=True)
    retained.received(TOPICS.state, b"current", retained=False)
    retained.received(TOPICS.state, b"old", retained=False)  # paho's
    assert bus.published == [(TOPICS.state, b"current")]
    assert caplog.text == ""


def test_an_acknowledged_payload_written_after_a_reconnect_is_another_clients(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """paho sends again only what the broker had not acknowledged: an older payload whose copy
    and acknowledgement both came, written after the reconnect, is another client's write (a
    replay), with the warning."""
    retained, bus = make()
    retained.sent(TOPICS.state, b"acknowledged", published=True)
    retained.received(TOPICS.state, b"acknowledged", retained=False)
    retained.sent(TOPICS.state, b"unacknowledged", published=True)
    retained.received(TOPICS.state, b"unacknowledged", retained=False)
    retained.connected(unacked=[(TOPICS.state, b"unacknowledged")])
    retained.sent(TOPICS.state, b"current", published=True)
    retained.received(TOPICS.state, b"unacknowledged", retained=False)  # paho's
    retained.received(TOPICS.state, b"current", retained=False)
    assert caplog.text == ""
    retained.received(TOPICS.state, b"acknowledged", retained=False)  # another client's
    assert f"another client wrote {TOPICS.state}; restoring" in caplog.text
    assert bus.published == [(TOPICS.state, b"current")]


def test_a_payload_paho_sends_again_twice_is_the_gateways_both_times(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same payload published twice, both unacknowledged, one copy back already: paho sends
    both again."""
    retained, bus = make()
    retained.sent(CONTACT, b"same", published=True)
    retained.sent(CONTACT, b"same", published=True)
    retained.received(CONTACT, b"same", retained=False)
    retained.connected(unacked=[(CONTACT, b"same"), (CONTACT, b"same")])
    retained.sent(CONTACT, b"current", published=True)
    for payload in (b"same", b"same", b"current"):
        retained.received(CONTACT, payload, retained=False)
    assert bus.published == []
    assert caplog.text == ""


def test_a_payload_neither_back_nor_acknowledged_is_taken_for_pahos_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#101's case, now also told by the missing acknowledgement: paho sends it once."""
    retained, _ = make()
    retained.sent(TOPICS.state, b"old", published=True)
    retained.connected(unacked=[(TOPICS.state, b"old")])
    retained.sent(TOPICS.state, b"current", published=True)
    for payload in (b"old", b"current"):
        retained.received(TOPICS.state, payload, retained=False)
    assert caplog.text == ""
    retained.received(TOPICS.state, b"old", retained=False)  # paho's was taken: a forgery now
    assert f"another client wrote {TOPICS.state}; restoring" in caplog.text


def test_an_unacknowledged_payload_of_a_topic_never_written_is_not_expected() -> None:
    retained, bus = make()
    retained.connected(unacked=[(CONTACT, b"{}")])
    retained.received(CONTACT, b"{}", retained=False)
    assert bus.published == [(CONTACT, b"")]  # cleared, as any write there


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
