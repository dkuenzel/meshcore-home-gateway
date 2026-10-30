"""The observer through the bridge, with the emulated radio: the providers' documents, events and
the history (ADR 0014-0016)."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.bridge import protected_keys
from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.mesh import Fix, Heard
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.core.telemetry import Answer, TelemetryState
from meshcore_home_gateway.history import History
from meshcore_home_gateway.mqtt.client import ContactRequest
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Push
from meshcore_home_gateway.radio.session import AdvertHeard, ContactsRead, PathReset
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    CurrentDocuments,
    JsonStateFile,
    RetainedKeys,
    TelemetryExclusions,
)
from meshcore_home_gateway.views import iso
from tests.radio_emulator import (
    LPP_BME280,
    LPP_FULL,
    LPP_T1000E,
    EmulatedContact,
    EmulatedRadio,
)
from tests.test_bridge import ALICE, BOB, NOW, FakeBus, make_config, running, scenario

ROOT = "meshcore/radio1"
TOPICS = Topics("meshcore", "radio1")
CAROL = EmulatedContact(bytes.fromhex("c3" * 32), "Carol", node_type=2, latitude_e6=53_550_000,
                        longitude_e6=9_990_000, last_advert=int(NOW) - 5)  # fmt: skip


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
                BOB.public_key.hex(), "Bob", "chat",
            )  # fmt: skip
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
        return Observer(bus, TOPICS, retained=RetainedKeys(store, "contacts"),
                        wall_clock=lambda: NOW)  # fmt: skip

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _n):
            await bus.wait_for(f"{ROOT}/contacts", lambda d: d["total"] == 1)
            assert retained(bus, f"{ROOT}/contacts/{'dd' * 32}") == [b""]
            assert retained(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")[-1] != b""
            events = bus.payloads(f"{ROOT}/contact")  # only what changed while stopped
            assert events == [{"event_type": "removed", "key": "dd" * 32, "prefix": "dd" * 6,
                               "name": None, "type": None, "request": None, "id": None,
                               "reason": None}]  # fmt: skip

    scenario(main)
    assert RetainedKeys(store, "contacts").keys == {ALICE.public_key.hex()}


def test_a_new_node_is_heard_with_its_signal_but_without_its_position(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.emit(advert_log(CAROL, hops=3))
            radio.advertise(CAROL)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert (event["key"], event["name"], event["type"]) == (
                CAROL.public_key.hex(), "Carol", "repeater",
            )  # fmt: skip
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
            radio.emit(advert_log(ALICE, hops=1))
            radio.advertise(ALICE)
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
                "in", "hi there", key,
            )  # fmt: skip
            assert (channel["kind"], channel["name"], channel["text"]) == (
                "channel", "Carol", "on my way",
            )  # fmt: skip
            bus.send("alice", "Back soon")
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 3)
            out = history["messages"][0]
            assert (out["direction"], out["state"], out["name"]) == ("out", "confirmed", "Alice")

    scenario(main)


def test_the_history_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    config = make_config()

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        observers.append(history)
        return Observer(bus, TOPICS, history=history, protected=protected_keys(config),
                        wall_clock=lambda: NOW)  # fmt: skip

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
    assert [row.name for row in history.nodes()] == ["Carol"]  # the history keeps 30 days
    clock.now = NOW + 31 * 86_400
    observer.tick()  # the next purge is due an hour after the first
    assert history.nodes() == []
    history.close()


def test_documents_are_published_again_when_the_broker_reconnects() -> None:
    """The MQTT adapter drops retained messages while the broker is away: a reconnect sends the
    current documents again, and the clearing of a removed contact's topic too."""
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
    assert again == current | {f"{ROOT}/history": again[f"{ROOT}/history"]}
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
    observer.radio_event(ContactsRead({k.public_key.hex(): r for k, r in
                                       ((ALICE, alice), (BOB, bob))}, NOW + 4))  # fmt: skip
    assert calls == [False, False, True]  # Bob joined
    clock[0] = NOW + 8 * 86_400  # Carol is forgotten
    observer.tick()
    assert calls == [False, False, True, False]
    assert "node listener failed" in caplog.text


def test_the_switch_the_worker_applies_is_published() -> None:
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
    record = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), "Mallory", node_type=1,
                                            last_advert=int(NOW) - 500))  # fmt: skip
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
    carol = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), "Carol", node_type=2,
                                           last_advert=int(NOW) - 5))  # fmt: skip
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead({}, NOW))
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    history.close()
    history = History.open(path)
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW + 30)
    replay = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), "Mallory", node_type=1,
                                            last_advert=int(NOW) - 500))  # fmt: skip
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
        50.12345, 8.56775, 120.5,
    )  # fmt: skip
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
GREENHOUSE = EmulatedContact(bytes.fromhex("e7" * 32), "Greenhouse", latitude_e6=53_560_000,
                             longitude_e6=10_000_000, last_advert=int(NOW) - 50)  # fmt: skip
BME280_READINGS = {
    "ch2_temperature_c": 21.3, "ch2_humidity_percent": 55.5, "ch2_barometric_pressure_hpa": 1013.2,
    "ch2_altitude_m": 120.0,
}  # fmt: skip
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
        (key, NOW + 10, "battery_v", 4.12), (key, NOW + 10, "mcu_temperature_c", 23.4),
        *((key, NOW + 10, k, v) for k, v in BME280_READINGS.items()),
    ]  # fmt: skip
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
    observer = Observer(bus, TOPICS, retained=RetainedKeys(store, "contacts"),
                        wall_clock=lambda: NOW)  # fmt: skip
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
    assert bus.payloads(f"{ROOT}/telemetry") == [{
        "event_type": "unanswered", "key": alice, "prefix": alice[:12], "name": "Alice",
        "state": "away",
    }]  # fmt: skip
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
    observer = Observer(bus, TOPICS, history=history, exclusions=TelemetryExclusions(
        tmp_path / EXCLUSIONS_FILE), wall_clock=lambda: NOW + 60)  # fmt: skip
    observer.radio_event(ContactsRead(table(GREENHOUSE, ALICE), NOW + 60))
    document = last(bus, f"{ROOT}/contacts/{greenhouse}")
    assert (document["battery_v"], document["readings"], document["readings_at"]) == (
        None, {}, None,
    )  # fmt: skip
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
    """The file may exclude anyone: as telemetry asks nobody then (D12), the history gives back
    when contacts were heard and their advert positions only."""
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
    observer = Observer(bus, TOPICS, history=history, exclusions=exclusions,
                        wall_clock=lambda: NOW)  # fmt: skip
    for key in (greenhouse, alice):
        observer.restore_position_received(key, NOW - 20)  # from the saved schedule
    observer.radio_event(ContactsRead(table(GREENHOUSE, ALICE), NOW))
    for key in (greenhouse, alice):
        document = last(bus, f"{ROOT}/contacts/{key}")
        assert (document["battery_v"], document["readings"], document["readings_at"]) == (
            None, {}, None,
        )  # fmt: skip
        assert document["position_received_at"] is None
    document = last(bus, f"{ROOT}/contacts/{greenhouse}")
    assert (document["position_source"], document["latitude"]) == ("advert", 53.56)  # its record
    document = last(bus, f"{ROOT}/contacts/{alice}")
    assert (document["position_source"], document["latitude"]) == ("advert", 53.55)
    assert (document["heard_via"], document["heard_at"]) == ("message", iso(NOW - 5))
    history.close()


def test_an_excluded_contact_gets_back_its_last_advert_position(tmp_path: Path) -> None:
    """Its newest advert no longer carries one: the history's advert position stays."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    key = ALICE.public_key.hex()
    history.add_position(key, Fix(Position(53.56, 10.0), NOW - 50, "advert"))
    exclusions = TelemetryExclusions(None)
    exclusions.exclude(key)
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, exclusions=exclusions,
                        wall_clock=lambda: NOW)  # fmt: skip
    observer.radio_event(ContactsRead(table(ALICE), NOW))  # its record has no position
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["position_source"], document["latitude"]) == ("advert", 53.56)
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
        None, None, {},
    )  # fmt: skip
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


FORGOTTEN = {
    "telemetry_state": "excluded", "telemetry_excluded": True, "telemetry_current": False,
    "position_current": None, "readings_at": None, "battery_v": None, "mcu_temperature_c": None,
    "readings": {}, "latitude": None, "longitude": None, "altitude_m": None, "position_at": None,
    "position_source": None, "position_accuracy_m": None, "position_received_at": None,
}  # fmt: skip


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
    assert retained(bus, f"{ROOT}/contacts/{key}") == []  # current: the broker keeps hers
    states[key] = "away"  # her bound passed
    observer.tick()
    expired = {"telemetry_state": "away", "telemetry_current": False, "position_current": False}
    assert last(bus, f"{ROOT}/contacts/{key}") == {**before[key], **expired}
    assert CurrentDocuments(store).documents == {key: {**before[key], **expired}}  # at once
    observer.tick()
    assert len(retained(bus, f"{ROOT}/contacts/{key}")) == 1
    observer.publish_all()  # the broker connected again
    assert last(bus, f"{ROOT}/contacts/{key}") == {**before[key], **expired}
    states[key] = "waiting"  # a day later
    observer.telemetry_state_changed(key)
    waiting = {**before[key], **expired, "telemetry_state": "waiting"}
    assert last(bus, f"{ROOT}/contacts/{key}") == waiting
    assert CurrentDocuments(store).documents == {key: waiting}


def test_an_expiry_the_broker_missed_is_published_at_the_next_connect(tmp_path: Path) -> None:
    """A run that stopped before the broker connected published it to nobody: the state file
    keeps it expired, and the next run publishes it at each connect. One still answering is not
    published again (the broker has it)."""
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
    assert retained(bus, f"{ROOT}/contacts/{bob}") == []
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
    expired = {"telemetry_state": "no_position", "telemetry_current": True,
               "position_current": False}  # fmt: skip
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
    assert CurrentDocuments(store).documents == {key: {**before[key], **off}}


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
    assert CurrentDocuments(store).documents == stored  # gone from the disk at once
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
    assert CurrentDocuments(store).documents == {key: {**before[key], **FORGOTTEN}}
    observer.exclusions.include(key)
    states[key] = "waiting"  # the poller forgot her answers
    observer.telemetry_excluded(key, False)
    included = {**before[key], **FORGOTTEN, "telemetry_state": "waiting",
                "telemetry_excluded": False}  # fmt: skip
    assert last(bus, f"{ROOT}/contacts/{key}") == included
    assert CurrentDocuments(store).documents == {key: included}


def test_the_first_table_read_replaces_the_documents_of_the_last_run(tmp_path: Path) -> None:
    store, _before = last_run(tmp_path, ALICE, BOB)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    observer, bus = restarted(store, {alice: "answering", bob: "answering"})
    observer.radio_event(ContactsRead(table(ALICE), NOW + 60))  # Bob left the radio meanwhile
    assert CurrentDocuments(store).documents.keys() == {alice}  # at once
    observer.tick()
    assert CurrentDocuments(store).documents == {alice: last(bus, f"{ROOT}/contacts/{alice}")}
    published = len(bus.published)
    observer.telemetry_state = lambda _key: "away"
    observer.tick()  # the poller reports the states of the table's contacts now
    assert len(bus.published) == published
    observer.radio_event(ContactsRead({}, NOW + 120))  # and Alice now
    assert CurrentDocuments(store).documents == {}


def test_at_the_first_table_read_an_expired_document_of_the_last_run_is_dropped(
    tmp_path: Path,
) -> None:
    """Kept until then also once it expired; from then on only while the answers are current."""
    store, _before = last_run(tmp_path, ALICE, BOB)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()
    observer, _bus = restarted(store, {alice: "away", bob: "answering"})
    observer.publish_all()
    assert CurrentDocuments(store).documents.keys() == {alice, bob}
    observer.radio_event(ContactsRead(table(ALICE, BOB), NOW + 60))
    assert CurrentDocuments(store).documents.keys() == {bob}  # at once


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
    assert CurrentDocuments(store).documents == {}  # not current; Carol is no companion
    states[key] = "answering"
    observer.telemetry_state_changed(key)
    assert CurrentDocuments(store).documents == {key: last(bus, f"{ROOT}/contacts/{key}")}
    observer.telemetry(key, answer(NOW + 10, LPP_BME280))
    assert CurrentDocuments(store).documents[key]["readings"] == {}  # until the tick
    observer.tick()
    assert CurrentDocuments(store).documents == {key: last(bus, f"{ROOT}/contacts/{key}")}
    states[key] = "away"
    observer.telemetry_state_changed(key)
    assert CurrentDocuments(store).documents == {}
