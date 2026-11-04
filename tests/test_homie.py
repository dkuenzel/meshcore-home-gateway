"""The Homie adapter (EXT-011): the contacts as the nodes of one Homie 4 device, from the
documents, on a connection of its own with the device's last will."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshcore_home_gateway.config import MqttConfig
from meshcore_home_gateway.core.telemetry import STATES
from meshcore_home_gateway.mqtt import homie_client
from meshcore_home_gateway.mqtt.homie import HomieDevice, device_id
from meshcore_home_gateway.mqtt.homie_client import HomieConnection
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import ContactsRead, SendRequest
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import default_session_factory
from meshcore_home_gateway.state import CommandLedger, JsonStateFile
from meshcore_home_gateway.wiring import gateway
from tests.bridge_harness import FakeBus, make_config
from tests.radio_emulator import EmulatedRadio

TOPICS = Topics("meshcore", "radio1")
BASE = "homie/meshcore-radio1"
ALICE, BOB, CAROL = "a1" * 32, "b2" * 32, "c3" * 32
HOMIE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def document(key: str, name: str, **fields: Any) -> dict[str, Any]:
    """A contact's document with the keys the adapter reads."""
    return {
        "key": key,
        "name": name,
        "type": "chat",
        "protected": False,
        "favourite": False,
        "heard_at": "2026-11-04T10:00:00+00:00",
        "battery_v": 3.91,
        "latitude": 47.5,
        "longitude": 8.25,
        "telemetry_state": "answering",
    } | fields


class Recorder:
    def __init__(self, limit: int = 50) -> None:
        self.published: list[tuple[str, bytes, bool]] = []
        self.sent: list[SendRequest] = []
        self.device = HomieDevice(
            "radio1",
            "MeshCore radio",
            limit,
            TOPICS,
            publish=lambda t, p, r: self.published.append((t, p, r)),
            send=self.sent.append,
        )

    def latest(self) -> dict[str, bytes]:
        """The retained value of each topic as the broker holds it (an empty one: none)."""
        held = {topic: payload for topic, payload, retained in self.published if retained}
        return {t: p for t, p in held.items() if p}

    def take(self) -> list[tuple[str, bytes, bool]]:
        published, self.published = self.published, []
        return published


def contact(device: HomieDevice, key: str, name: str, **fields: Any) -> None:
    device.document(TOPICS.contact(key), document(key, name, **fields))


def radio(device: HomieDevice, state: str) -> None:
    device.document(TOPICS.state, {"radio": state, "queue": 0})


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [("radio1", "meshcore-radio1"), ("home_2", "meshcore-home-2"), ("a-b_c", "meshcore-a-b-c")],
)
def test_the_device_id_is_a_homie_id_of_the_node_id(node_id: str, expected: str) -> None:
    assert device_id(node_id) == expected
    assert HOMIE_ID.fullmatch(expected)


def test_the_device_announces_itself_and_its_state_follows_the_radio() -> None:
    """Homie 4.0.0: `init` first, every device attribute (`$extensions` empty), then the state:
    `alert` while the radio is not connected, `ready` once it is. Everything retained."""
    recorder = Recorder()
    recorder.device.connected()
    assert recorder.take() == [
        (f"{BASE}/$state", b"init", True),
        (f"{BASE}/$homie", b"4.0.0", True),
        (f"{BASE}/$name", b"MeshCore radio", True),
        (f"{BASE}/$extensions", b"", True),
        (f"{BASE}/$implementation", b"meshcore-home-gateway", True),
        (f"{BASE}/$nodes", b"", True),
        (f"{BASE}/$state", b"alert", True),
    ]
    radio(recorder.device, "connected")
    assert recorder.take() == [(f"{BASE}/$state", b"ready", True)]
    radio(recorder.device, "connected")  # unchanged: nothing
    assert recorder.take() == []
    radio(recorder.device, "disconnected")
    assert recorder.take() == [(f"{BASE}/$state", b"alert", True)]


def test_a_companion_is_a_node_with_its_values_and_a_settable_send() -> None:
    recorder = Recorder()
    radio(recorder.device, "connected")
    contact(recorder.device, ALICE, "Alice")
    recorder.device.connected()
    node = f"{BASE}/{ALICE}"
    assert recorder.latest() == {
        f"{BASE}/$state": b"ready",
        f"{BASE}/$homie": b"4.0.0",
        f"{BASE}/$name": b"MeshCore radio",
        f"{BASE}/$implementation": b"meshcore-home-gateway",
        f"{BASE}/$nodes": ALICE.encode(),
        f"{node}/$name": b"Alice",
        f"{node}/$type": b"chat",
        f"{node}/$properties": b"battery,position,last-heard,telemetry-state,send",
        f"{node}/battery/$name": b"Battery",
        f"{node}/battery/$datatype": b"float",
        f"{node}/battery/$unit": b"V",
        f"{node}/battery": b"3.91",
        f"{node}/position/$name": b"Position",
        f"{node}/position/$datatype": b"string",
        f"{node}/position": b"47.5,8.25",
        f"{node}/last-heard/$name": b"Last heard",
        f"{node}/last-heard/$datatype": b"string",
        f"{node}/last-heard": b"2026-11-04T10:00:00+00:00",
        f"{node}/telemetry-state/$name": b"Telemetry",
        f"{node}/telemetry-state/$datatype": b"enum",
        f"{node}/telemetry-state/$format": ",".join(STATES).encode(),
        f"{node}/telemetry-state": b"answering",
        f"{node}/send/$name": b"Send",
        f"{node}/send/$datatype": b"string",
        f"{node}/send/$settable": b"true",
        f"{node}/send/$retained": b"false",
    }
    assert all(HOMIE_ID.fullmatch(level) for level in node.split("/")[1:])


def test_a_sensor_has_no_send_and_an_unknown_value_is_cleared() -> None:
    recorder = Recorder()
    recorder.device.connected()
    contact(recorder.device, BOB, "Bob", type="sensor", battery_v=None, latitude=None)
    node = f"{BASE}/{BOB}"
    held = recorder.latest()
    assert held[f"{node}/$properties"] == b"battery,position,last-heard,telemetry-state"
    assert not any(t.startswith(f"{node}/send") for t in held)
    assert (f"{node}/battery", b"", True) in recorder.published  # no value on the broker
    assert f"{node}/position" not in held
    recorder.take()
    contact(recorder.device, BOB, "Bob", type="sensor", battery_v=4, latitude=None)
    assert recorder.take() == [(f"{node}/battery", b"4.0", True)]  # only what changed
    contact(recorder.device, BOB, "Bob", type="sensor", battery_v=4, longitude=None)
    assert recorder.take() == []  # a position needs both


def test_a_change_of_the_nodes_goes_out_between_init_and_ready() -> None:
    """A new node, or one whose contact left the table: the device again, between `init` and
    the state, and the topics of a node that is gone are cleared."""
    recorder = Recorder()
    radio(recorder.device, "connected")
    recorder.device.connected()
    recorder.take()
    contact(recorder.device, ALICE, "Alice")
    published = recorder.take()
    assert published[0] == (f"{BASE}/$state", b"init", True)
    assert published[1] == (f"{BASE}/$nodes", ALICE.encode(), True)
    assert published[-1] == (f"{BASE}/$state", b"ready", True)
    contact(recorder.device, BOB, "Bob")
    assert (f"{BASE}/$nodes", f"{ALICE},{BOB}".encode(), True) in recorder.take()
    recorder.device.document(TOPICS.contact(ALICE), None)
    published = recorder.take()
    assert published[0] == (f"{BASE}/$state", b"init", True)
    assert published[1] == (f"{BASE}/$nodes", BOB.encode(), True)
    assert published[-1] == (f"{BASE}/$state", b"ready", True)
    cleared = {t for t, p, _ in published if t.startswith(f"{BASE}/{ALICE}/")}
    assert len(cleared) == 21  # $name, $type, $properties, 14 attributes, 4 values
    assert all(p == b"" for t, p, _ in published if t in cleared)
    assert not any(t.startswith(f"{BASE}/{ALICE}") for t in recorder.latest())


def test_other_documents_are_not_nodes() -> None:
    recorder = Recorder()
    recorder.device.connected()
    recorder.take()
    for topic in (TOPICS.contacts, TOPICS.contact_received(ALICE), TOPICS.stats, TOPICS.nodes):
        recorder.device.document(topic, document(ALICE, "Alice"))
    assert recorder.take() == []


def test_at_most_the_limit_chosen_as_for_home_assistant_and_kept() -> None:
    """Protected contacts first, then favourites, companions and the most recently heard; a
    node stays while its contact does, also when one that would come first arrives."""
    recorder = Recorder(limit=2)
    device = recorder.device
    contact(device, ALICE, "Alice", heard_at="2026-11-04T09:00:00+00:00")
    contact(device, BOB, "Bob", heard_at="2026-11-04T10:00:00+00:00")
    contact(device, CAROL, "Carol", type="repeater", heard_at="2026-11-04T11:00:00+00:00")
    device.connected()
    assert recorder.latest()[f"{BASE}/$nodes"] == f"{BOB},{ALICE}".encode()
    contact(device, CAROL, "Carol", type="repeater", protected=True)
    assert recorder.latest()[f"{BASE}/$nodes"] == f"{BOB},{ALICE}".encode()
    device.document(TOPICS.contact(ALICE), None)
    assert recorder.latest()[f"{BASE}/$nodes"] == f"{BOB},{CAROL}".encode()

    fresh = Recorder(limit=3)
    contact(fresh.device, ALICE, "Alice", favourite=True, heard_at=None)
    contact(fresh.device, BOB, "Bob")
    contact(fresh.device, CAROL, "Carol", type="sensor", protected=True, heard_at=None)
    fresh.device.connected()
    assert fresh.latest()[f"{BASE}/$nodes"] == f"{CAROL},{ALICE},{BOB}".encode()


def test_a_reconnect_publishes_everything_and_clears_what_went_meanwhile() -> None:
    recorder = Recorder()
    device = recorder.device
    device.connected()
    contact(device, ALICE, "Alice")
    contact(device, BOB, "Bob")
    device.disconnected()
    device.document(TOPICS.contact(ALICE), None)  # while the connection is away
    recorder.take()
    device.connected()
    published = recorder.take()
    assert (f"{BASE}/$homie", b"4.0.0", True) in published  # again, for a broker that lost it
    assert (f"{BASE}/$nodes", BOB.encode(), True) in published
    assert (f"{BASE}/{ALICE}/battery", b"", True) in published
    assert published[-1] == (f"{BASE}/$state", b"alert", True)
    assert not any(t.startswith(f"{BASE}/{ALICE}") for t in recorder.latest())


def test_send_set_hands_the_text_over_as_contacts_send_and_echoes_it() -> None:
    """As `contacts/<key>/send`: a retained message is handed over too and refused there, so
    its delivery event tells why; the echo Homie asks for is not retained."""
    recorder = Recorder()
    device = recorder.device
    device.connected()
    contact(device, ALICE, "Alice")
    contact(device, BOB, "Bob", type="sensor")
    recorder.take()
    device.message(f"{BASE}/{ALICE}/send/set", b"hello", False)
    assert recorder.sent == [SendRequest(ALICE, b"hello", False, contact=True)]
    assert recorder.take() == [(f"{BASE}/{ALICE}/send", b"hello", False)]
    device.message(f"{BASE}/{ALICE}/send/set", b"kept", True)
    assert recorder.sent[-1] == SendRequest(ALICE, b"kept", True, contact=True)
    assert recorder.take() == []
    for topic in (
        f"{BASE}/{BOB}/send/set",  # a sensor takes no texts
        f"{BASE}/{CAROL}/send/set",  # no node
        f"{BASE}/{ALICE}/x/send/set",
        f"{BASE}/{ALICE}/send",
        f"homie/other/{ALICE}/send/set",
    ):
        device.message(topic, b"no", False)
    assert len(recorder.sent) == 2
    assert recorder.take() == []


class FakePaho:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.on_connect: Any = None
        self.on_connect_fail: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.on_subscribe: Any = None

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self.calls.append((name, args + tuple(kwargs.values())))

    def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        properties: Any = None,
    ) -> Any:
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace()


def test_the_connection_is_its_own_with_the_devices_last_will() -> None:
    """Homie 4.0.0, section 2.4: `$state` `lost` as the will, which the gateway's connection
    cannot carry beside `availability`; the broker settings of `[mqtt]`, another client ID."""
    fake, given = FakePaho(), []
    config = MqttConfig(host="broker.example.org", node_id="radio1", username="u", password="p")

    def factory(own: MqttConfig) -> FakePaho:
        given.append(own)
        return fake

    async def scenario() -> None:
        sent: list[SendRequest] = []
        connection = homie_client.homie_adapter(
            config, 50, "MeshCore radio", TOPICS, send=sent.append, client_factory=factory
        )
        await connection.start()
        calls = dict(fake.calls)
        assert calls["will_set"] == (f"{BASE}/$state", b"lost", 1, True)
        assert calls["username_pw_set"] == ("u", "p")
        assert calls["connect_async"] == ("broker.example.org", 1883, 60)
        assert re.fullmatch(
            r"meshcore-home-gateway-radio1-[0-9a-f]{8}-homie", str(given[0].client_id)
        )
        connection.publish("homie/x", b"1", True)  # not connected: dropped
        assert fake.published == []
        contact(connection.device, ALICE, "Alice")
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=False), None)
        await asyncio.sleep(0)
        assert calls_of(fake, "subscribe") == [([(f"{BASE}/+/send/set", 1)],)]
        assert fake.published[0] == (f"{BASE}/$state", b"init", 1, True)
        assert (f"{BASE}/{ALICE}/$name", b"Alice", 1, True) in fake.published
        message = SimpleNamespace(topic=f"{BASE}/{ALICE}/send/set", payload=b"hi", retain=False)
        fake.on_message(fake, None, message)  # on paho's thread: to the event loop
        assert sent == []
        await asyncio.sleep(0)
        assert sent == [SendRequest(ALICE, b"hi", False, contact=True)]
        assert fake.published[-1] == (f"{BASE}/{ALICE}/send", b"hi", 1, False)
        fake.on_disconnect(fake, None, None, None, None)
        await asyncio.sleep(0)
        assert not connection.connected
        await connection.stop()
        assert fake.published[-1] == (f"{BASE}/$state", b"disconnected", 1, True)
        assert [n for n, _ in fake.calls][-2:] == ["disconnect", "loop_stop"]

    asyncio.run(scenario())
    HomieConnection(
        MqttConfig(host="h", node_id="radio1", client_id="fixed"), Recorder().device, factory
    )
    assert given[-1].client_id == "fixed-homie"


def calls_of(fake: FakePaho, name: str) -> list[tuple[Any, ...]]:
    return [args for called, args in fake.calls if called == name]


def test_a_refused_connection_announces_nothing() -> None:
    fake = FakePaho()

    async def scenario() -> None:
        recorder = Recorder()
        connection = HomieConnection(
            MqttConfig(host="h", node_id="r"), recorder.device, lambda _: fake
        )
        await connection.start()
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=True), None)
        await asyncio.sleep(0)
        assert not connection.connected
        assert fake.published == []

    asyncio.run(scenario())


def test_the_wiring_feeds_the_device_the_documents(tmp_path: Path) -> None:
    """The observer's contact documents, also cleared ones, and the state document reach the
    device."""
    recorder = Recorder()
    bridge = gateway(
        make_config(),
        FakeBus(),
        EmulatedRadio,
        CommandLedger(JsonStateFile(tmp_path / "state.json")),
        session_factory=default_session_factory,
        notifier=lambda _: True,
        state_dir=tmp_path,
        homie=recorder.device,
    )
    recorder.device.connected()
    bridge.observer.radio_connected(350, codec.TuningParams(0, 9000))
    alice = codec.ContactRecord(bytes.fromhex(ALICE), 1, 0, 2, "Alice", 100, 100)
    bob = codec.ContactRecord(bytes.fromhex(BOB), 1, 0, 2, "Bob", 100, 100)
    bridge.observer.radio_event(ContactsRead({ALICE: alice, BOB: bob}, 200.0))
    assert recorder.latest()[f"{BASE}/{ALICE}/$name"] == b"Alice"
    bridge.observer.radio_event(ContactsRead({ALICE: alice}, 201.0))  # Bob left the table
    assert recorder.latest()[f"{BASE}/$nodes"] == ALICE.encode()
    assert not any(t.startswith(f"{BASE}/{BOB}") for t in recorder.latest())
    bridge.radio.ready.set()
    bridge.status.publish()
    assert recorder.latest()[f"{BASE}/$state"] == b"ready"  # the state: the radio connected


def test_the_adapter_is_built_with_the_gateways_name_and_limit() -> None:
    connection = homie_client.homie_adapter(
        MqttConfig(host="h", node_id="radio1"),
        7,
        "Radio",
        TOPICS,
        send=lambda _: None,
        client_factory=lambda _: FakePaho(),
    )
    assert connection.device.base == BASE
    assert connection.device._limit == 7
    assert connection.device._name == "Radio"
