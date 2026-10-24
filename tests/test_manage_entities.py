"""Home Assistant's controls that add contacts (mqtt/manage_entities.py, ADR 0017)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.core.contacts import key_from_label
from meshcore_home_gateway.core.mesh import Heard, Mesh, Node
from meshcore_home_gateway.mqtt import manage_entities
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.manage_entities import (
    OPTIONS_INTERVAL_S,
    TEXT_PATTERN,
    ManageEntities,
    manage_payload,
    manage_topic,
    node_options,
)
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.state import AnnouncedComponents, JsonStateFile
from meshcore_home_gateway.views import contacts_document
from tests.test_discovery import STATE_KEYS, template_keys

CONFIG = parse_config(
    '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\n[mqtt]\nhost = "broker.example.org"\n'
    'node_id = "radio1"\n'
)
TOPICS = Topics(CONFIG.mqtt.base_topic, CONFIG.mqtt.node_id)
UID = "meshcore_home_gateway_radio1"
NOW = 1_800_000_000.0
BOB, CAROL, HILL, ROOM = "b0" * 32, "c3" * 32, "e5" * 32, "d4" * 32


def heard(mesh: Mesh, key: str, name: str, node_type: int = 1, at: float = NOW) -> None:
    mesh.node_heard(Node(key, name, node_type, last_advert=int(at)), Heard(at, "advert"))


class Recorder:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        assert not retain  # discovery documents are never retained (ADR 0010)
        self.published.append((topic, payload))

    def options(self) -> list[list[str]]:
        return [json.loads(p)["cmps"]["add_node"]["options"] for _, p in self.published]


class Timers:
    """call_later for the tests: the callbacks run when the test says."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, Callable[[], None]]] = []

    def __call__(self, delay_s: float, callback: Callable[[], None]) -> Any:
        entry = (delay_s, callback)
        self.pending.append(entry)
        timers = self

        class Handle:
            def cancel(self) -> None:
                if entry in timers.pending:
                    timers.pending.remove(entry)

        return Handle()

    def run(self) -> None:
        pending, self.pending = self.pending, []
        for _, callback in pending:
            callback()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def controls(
    mesh: Mesh, *, records: AnnouncedComponents | None = None, online: list[bool] | None = None
) -> tuple[ManageEntities, Recorder, Timers, Clock]:
    bus, timers, clock, state = Recorder(), Timers(), Clock(), online or [True]
    entities = ManageEntities(
        CONFIG, TOPICS, bus, mesh, records=records, monotonic=clock, wall_clock=lambda: NOW,
        call_later=timers, connected=lambda: state[0],
    )  # fmt: skip
    return entities, bus, timers, clock


# --- the document ---------------------------------------------------------------------------------


def test_the_controls_join_the_gateway_device() -> None:
    document = manage_payload(CONFIG, TOPICS, ["x"])
    assert manage_topic(CONFIG) == f"homeassistant/device/{UID}/manage_contacts/config"
    gateway = discovery_payload(CONFIG, TOPICS)
    assert document["dev"] == {"ids": gateway["dev"]["ids"], "name": "MeshCore radio"}
    assert document["o"] == gateway["o"]
    select, text = document["cmps"]["add_node"], document["cmps"]["add_contact"]
    assert (select["p"], select["default_entity_id"]) == ("select", f"select.{UID}_add_node")
    assert (text["p"], text["default_entity_id"]) == ("text", f"text.{UID}_add_contact")
    assert select["unique_id"] == f"{UID}_add_node"
    assert select["command_topic"] == "meshcore/radio1/homeassistant/add_node"
    assert text["command_topic"] == "meshcore/radio1/add_contact"
    assert select["options"] == ["x"]
    assert select["value_template"] == "None"  # stays unknown: every choice is a change
    assert text["value_template"] == "{{ '' }}"
    assert (text["min"], text["max"]) == (0, 255)
    assert all(c["entity_category"] == "config" for c in document["cmps"].values())
    assert all("enabled_by_default" not in c for c in document["cmps"].values())
    assert document["availability_mode"] == "all"


def test_the_availability_reads_keys_with_guards() -> None:
    """Gateway online, radio connected, management on; a document of an earlier version without
    the key reads as offline, and Home Assistant logs nothing (home-assistant/tests)."""
    availability = manage_payload(CONFIG, TOPICS, [])["availability"]
    assert [a["topic"] for a in availability] == [
        TOPICS.availability,
        TOPICS.state,
        TOPICS.contacts,
    ]
    for entry, keys, document in zip(
        availability[1:], ({"radio"}, {"manage"}), (STATE_KEYS, set(contacts_document(Mesh()))),
        strict=True,
    ):  # fmt: skip
        assert entry["value_template"].startswith("{{ 'online' if value_json is defined and ")
        assert template_keys(entry["value_template"]) == keys
        assert keys <= document
    assert "is true" in availability[2]["value_template"]


@pytest.mark.parametrize(
    ("text", "taken"),
    [
        ("", True), ("b0" * 32, True), ("B0" * 32, True),
        ("meshcore://contact/add?name=Bob&public_key=" + "b0" * 32 + "&type=1", True),
        ("meshcore://" + "11" * 120, False),  # a card: cut at 255 characters it still parses
        ("b0" * 31, False), ("meshcore://contact/add?name=Bob Smith", False), ("Bob", False),
        ("b0" * 32 + "\n", True), (" " + "b0" * 32 + " ", True),  # the gateway strips them
        ("meshcore://contact/add?public_key=" + "b0" * 32 + " ", True), ("   ", False),
    ],
)  # fmt: skip
def test_the_text_takes_keys_and_contact_links(text: str, taken: bool) -> None:
    """Home Assistant checks the pattern with re.match (homeassistant/components/text); its
    frontend takes it as an HTML pattern, which knows no `\\Z`."""
    assert (re.match(TEXT_PATTERN, text) is not None) is taken
    assert manage_payload(CONFIG, TOPICS, [])["cmps"]["add_contact"]["pattern"] == TEXT_PATTERN


# --- the options ---------------------------------------------------------------------------------


def test_the_heard_companions_rooms_and_sensors_are_offered_by_name() -> None:
    mesh = Mesh()
    heard(mesh, CAROL, "carol")
    heard(mesh, HILL, "Hilltop", node_type=2)  # a repeater: not offered
    heard(mesh, BOB, "Bob")
    heard(mesh, ROOM, "Club", node_type=3)
    heard(mesh, "f1" * 32, "Odd", node_type=7)
    heard(mesh, "f4" * 32, "Weather", node_type=4)
    options = node_options(mesh)
    assert options == [
        f"{BOB[:12]}…{BOB[-8:]} Bob", f"{CAROL[:12]}…{CAROL[-8:]} carol",
        f"{ROOM[:12]}…{ROOM[-8:]} Club (room)", f"{'f4' * 6}…{'f4' * 4} Weather (sensor)",
    ]  # fmt: skip
    keys = {*mesh.nodes, *mesh.contacts}
    assert [key_from_label(o, keys) for o in options] == [BOB, CAROL, ROOM, "f4" * 32]


def test_look_alikes_are_not_offered() -> None:
    """A node with a contact's or a protected key's first 6 bytes would be refused; one with
    another heard node's would take them from it."""
    mesh = Mesh()
    mesh.contacts_read([Node(BOB, "Bob", 1)], NOW)
    twin_of_bob = BOB[:12] + "00" * 26
    twin_a, twin_b = "a7" * 6 + "01" * 26, "a7" * 6 + "02" * 26
    for key in (twin_of_bob, twin_a, twin_b, ROOM, CAROL):
        heard(mesh, key, "Bob")
    carol, room = f"{CAROL[:12]}…{CAROL[-8:]} Bob", f"{ROOM[:12]}…{ROOM[-8:]} Bob"
    assert node_options(mesh) == [carol, room]
    assert node_options(mesh, protected=frozenset({ROOM[:12] + "ff" * 26})) == [carol]


def test_the_most_recently_heard_are_offered() -> None:
    mesh = Mesh()
    keys = [f"{i:02x}" * 32 for i in range(1, 26)]
    for i, key in enumerate(keys):
        heard(mesh, key, f"node {i:02}", at=NOW + i)
    options = node_options(mesh)
    assert len(options) == manage_entities.NODE_OPTIONS == 20
    assert [o.split(" ", 1)[1] for o in options] == [f"node {i:02}" for i in range(5, 25)]


# --- publishing ---------------------------------------------------------------------------------


def test_options_follow_the_heard_nodes_at_most_every_30_seconds() -> None:
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.nodes_changed(False)
    assert bus.published == []  # nothing before the first announcement
    entities.announce()
    assert bus.options() == [[]]
    heard(mesh, BOB, "Bob")
    clock.now += 5
    entities.nodes_changed(False)
    assert len(bus.published) == 1  # too soon: later
    assert [delay for delay, _ in timers.pending] == [OPTIONS_INTERVAL_S - 5]
    heard(mesh, CAROL, "Carol")
    entities.nodes_changed(False)
    assert len(timers.pending) == 1  # one timer at a time
    clock.now += 25
    timers.run()
    assert bus.options()[-1] == node_options(mesh)
    assert len(bus.published) == 2
    heard(mesh, BOB, "Bob", at=NOW + 60)  # heard again: the same options
    clock.now += 60
    entities.nodes_changed(False)
    assert len(bus.published) == 2
    heard(mesh, ROOM, "Club", node_type=3)
    entities.nodes_changed(False)  # long enough since the last: at once
    assert len(bus.published) == 3
    assert timers.pending == []


def test_a_table_read_sends_changed_options_at_once() -> None:
    """After an add the node is no longer offered; after a removal it is offered again."""
    mesh = Mesh()
    heard(mesh, BOB, "Bob")
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    heard(mesh, CAROL, "Carol")
    clock.now += 1
    entities.nodes_changed(False)
    assert len(timers.pending) == 1
    mesh.contacts_read([Node(BOB, "Bob", 1)], NOW + 1)  # Bob was added
    entities.nodes_changed(True)
    assert bus.options()[-1] == [f"{CAROL[:12]}…{CAROL[-8:]} Carol"]
    assert timers.pending == []  # what the timer would have sent is out
    entities.nodes_changed(True)
    assert len(bus.published) == 2  # unchanged: not sent again


def test_every_announcement_sends_the_document_again() -> None:
    mesh = Mesh()
    entities, bus, _, _ = controls(mesh)
    entities.announce()
    entities.announce()  # the broker connected again, or Home Assistant started
    assert [t for t, _ in bus.published] == [manage_topic(CONFIG)] * 2


def test_components_of_an_earlier_version_are_removed(tmp_path: Path) -> None:
    """Placeholders first, which bind what Home Assistant kept, then the document without them;
    the next run removes them again, until the removal expires."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"ha_components_manage_contacts": {"add_node": "select", "add_group": "select",
                                                  "add_note": "text"}})  # fmt: skip
    entities, bus, _, _ = controls(Mesh(), records=AnnouncedComponents(store, "manage_contacts"))
    entities.announce()
    entities.announce()  # Home Assistant started meanwhile: it binds only what it discovered since
    first, second = bus.published[:2], bus.published[2:]
    assert [json.loads(p)["cmps"]["add_group"] for _, p in second] == [
        json.loads(p)["cmps"]["add_group"] for _, p in first
    ]  # placeholder and removal again
    bind, document = (json.loads(p) for _, p in first)
    assert bind["cmps"]["add_group"]["name"] == "Removed"
    assert bind["cmps"]["add_group"]["options"] == ["removed"]
    assert bind["cmps"]["add_note"]["p"] == "text"
    assert document["cmps"]["add_group"] == {"p": "select"}
    assert document["cmps"]["add_note"] == {"p": "text"}
    assert document["cmps"]["add_node"]["p"] == "select"
    data = store.load()
    assert data["ha_components_manage_contacts"] == {"add_node": "select", "add_contact": "text"}
    assert set(data["ha_removed_manage_contacts"]) == {"add_group", "add_note"}
    assert "ha_components" not in data  # the gateway's main document keeps its own


def test_an_announcement_takes_the_place_of_a_pending_timer() -> None:
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    heard(mesh, BOB, "Bob")
    clock.now += 1
    entities.nodes_changed(False)
    entities.announce()  # sends the new options
    assert timers.pending == []
    assert bus.options()[-1] == node_options(mesh)


def test_without_the_broker_new_options_wait_for_the_reconnect() -> None:
    """They would only take places of events in the backlog (mqtt/client.py); the announcement
    after the reconnect sends the current ones."""
    mesh, online = Mesh(), [True]
    entities, bus, timers, clock = controls(mesh, online=online)
    entities.announce()
    online[0] = False
    for i, key in enumerate((BOB, CAROL, ROOM)):
        heard(mesh, key, f"node {i}", at=NOW + i)
        clock.now += 60
        entities.nodes_changed(i == 1)
    assert len(bus.published) == 1
    assert timers.pending == []
    online[0] = True
    entities.announce()
    assert bus.options()[-1] == node_options(mesh)


def test_a_node_that_renames_itself_cannot_flood_home_assistant() -> None:
    """Home Assistant warns from the 101st change of a select's options within an hour
    (home-assistant/tests): a node that renames itself on every advert, or brings a new key, gets
    the options sent at most 70 times in any hour, and a request's change waits for a token."""
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    sent_at: list[float] = []
    for i in range(240):  # two hours, an advert every 30 s, each with a new name
        clock.now += 30
        heard(mesh, BOB, f"Bob {i}", at=NOW + i)
        before = len(bus.published)
        entities.nodes_changed(i % 7 == 0)  # now and then the table changed, too
        timers.run()
        sent_at.extend([clock.now] * (len(bus.published) - before))
    assert max(sum(1 for t in sent_at if start <= t < start + 3600) for start in sent_at) <= 70
    assert len(sent_at) > 100  # still following the heard nodes


def test_a_change_of_the_table_brings_a_waiting_send_forward() -> None:
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    heard(mesh, BOB, "Bob")
    clock.now += 1
    entities.nodes_changed(False)  # 29 s to wait
    (first,) = timers.pending
    mesh.contacts_read([Node(CAROL, "Carol", 1)], NOW)  # Carol joined: the table changed
    heard(mesh, ROOM, "Club", node_type=3)
    entities.nodes_changed(True)
    assert len(bus.published) == 2  # at once: a token was left
    assert timers.pending == []
    assert first not in timers.pending


def test_a_table_change_waiting_for_a_token_goes_out_when_the_token_comes() -> None:
    """Not 30 s after the last send: a node just added should leave the select soon."""
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()  # one of the start's two tokens
    clock.now += 50  # 1.83 tokens
    heard(mesh, BOB, "Bob")
    entities.nodes_changed(False)  # sent: 0.83 left
    clock.now += 5
    heard(mesh, CAROL, "Carol")
    entities.nodes_changed(True)  # a contact joined: waits only for the token
    ((delay, _),) = timers.pending
    assert delay == pytest.approx(5.0)
    clock.now += delay
    timers.run()
    assert len(bus.published) == 3  # now, not 30 s after the last send
    assert timers.pending == []


def test_an_announcement_without_a_token_repeats_the_options_sent() -> None:
    """Home Assistant counts a change that comes with an announcement (a reconnect, its start)
    too: the new options follow when the budget allows."""
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    heard(mesh, BOB, "Bob")
    entities.nodes_changed(True)  # the second token
    sent = bus.options()[-1]
    heard(mesh, CAROL, "Carol")
    entities.announce()  # the broker connected again
    assert bus.options()[-1] == sent
    assert len(timers.pending) == 1
    clock.now += 60
    timers.run()
    assert bus.options()[-1] == node_options(mesh)


def test_a_start_of_the_gateway_begins_with_a_small_budget() -> None:
    """Home Assistant keeps counting while the gateway restarts: each start may change the
    options twice at once (the connect, the first table read), not ten times."""
    mesh = Mesh()
    entities, bus, timers, _clock = controls(mesh)
    entities.announce()
    heard(mesh, BOB, "Bob")
    entities.nodes_changed(True)
    heard(mesh, CAROL, "Carol")
    entities.nodes_changed(True)
    assert len(bus.published) == 2
    assert len(timers.pending) == 1


def test_an_announcement_does_not_put_a_waiting_change_back() -> None:
    """Neither the urgency of a table change nor the time it waits for its token: the repeated
    options are no change, so the 30 s interval does not start again."""
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    clock.now += 50
    heard(mesh, BOB, "Bob")
    entities.nodes_changed(False)  # sent: 0.83 tokens left
    clock.now += 5
    heard(mesh, CAROL, "Carol")
    entities.nodes_changed(True)  # waits 5 s for its token
    clock.now += 1
    entities.announce()  # Home Assistant started: the options sent before, once more
    ((delay, _),) = timers.pending
    assert delay == pytest.approx(4.0)
    clock.now += delay
    timers.run()
    assert bus.options()[-1] == node_options(mesh)


def test_a_change_waits_at_most_a_minute_also_across_an_announcement() -> None:
    mesh = Mesh()
    entities, bus, timers, clock = controls(mesh)
    entities.announce()
    heard(mesh, BOB, "Bob")
    entities.nodes_changed(True)  # the start's second token
    heard(mesh, CAROL, "Carol")
    changed_at = clock.now
    entities.nodes_changed(False)  # no token: 60 s
    clock.now += 59
    entities.announce()  # still none
    ((delay, _),) = timers.pending
    clock.now += delay
    timers.run()
    assert bus.options()[-1] == node_options(mesh)
    assert clock.now - changed_at == pytest.approx(60.0)


def test_a_table_change_undone_while_the_broker_is_away_leaves_nothing_urgent() -> None:
    """An ordinary change after it still waits for the 30 s interval."""
    mesh, online = Mesh(), [True]
    entities, bus, timers, clock = controls(mesh, online=online)
    entities.announce()
    clock.now += 50
    heard(mesh, BOB, "Bob")
    entities.nodes_changed(False)  # sent: 0.83 tokens left
    sent_at = clock.now
    clock.now += 1
    mesh.contacts_read([Node(CAROL, "Carol", 1)], NOW)
    heard(mesh, ROOM, "Club", node_type=3)
    entities.nodes_changed(True)  # a contact joined: urgent, waits for its token
    online[0] = False
    clock.now += 1
    del mesh.nodes[ROOM]  # the change is gone again
    entities.nodes_changed(True)
    heard(mesh, "f4" * 32, "Weather", node_type=4)  # an ordinary change
    online[0] = True
    clock.now += 1
    entities.announce()  # no token: the options sent before
    timers.run()  # the token's time has not come: re-armed
    while timers.pending:
        clock.now += timers.pending[0][0]
        timers.run()
    assert bus.options()[-1] == node_options(mesh)
    assert clock.now - sent_at == pytest.approx(30.0)  # the interval, not the token alone
