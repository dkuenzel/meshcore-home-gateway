"""MQTT adapter: unit tests with a fake paho client, integration tests with mosquitto."""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import MqttConfig, parse_config
from meshcore_home_gateway.mqtt import client as client_module
from meshcore_home_gateway.mqtt import retained as retained_module
from meshcore_home_gateway.mqtt.client import MqttBus, make_paho_client
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import ContactRequest, SendRequest
from meshcore_home_gateway.wiring import event_expiries
from tests.conftest import Broker

TOPICS = Topics("meshcore", "radio1")


class FakePaho:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.properties: list[Properties | None] = []  # of each publish, as published
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args + tuple(kwargs.values())))

        return record

    def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        properties: Properties | None = None,
    ) -> Any:
        self.published.append((topic, payload, qos, retain))
        self.properties.append(properties)
        return SimpleNamespace()


def make_bus(config: MqttConfig | None = None) -> tuple[MqttBus, FakePaho]:
    fake = FakePaho()
    cfg = config or MqttConfig(
        host="broker.example.org", node_id="radio1", username="u", password="p"
    )
    return MqttBus(cfg, TOPICS, "homeassistant/status", client_factory=lambda _: fake), fake


def names(fake: FakePaho) -> list[str]:
    return [name for name, _ in fake.calls]


def test_the_default_client_id_has_a_random_part_per_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker drops a client when another connects with its id (MQTT 3.1.1 section 3.1.4):
    one that anyone can read in the topics would let any client take the gateway offline
    (SEC-006). One set in the file is used as it is."""
    given: list[str] = []
    monkeypatch.setattr(paho, "Client", lambda **kwargs: given.append(kwargs["client_id"]))
    config = MqttConfig(host="broker.example.org", node_id="radio1")
    client_module.make_paho_client(config)
    client_module.make_paho_client(config)
    first, second = given
    assert re.fullmatch(r"meshcore-home-gateway-radio1-[0-9a-f]{8}", first)
    assert first != second
    client_module.make_paho_client(MqttConfig(host="h", node_id="radio1", client_id="fixed"))
    assert given[-1] == "fixed"


def test_start_configures_will_credentials_and_reconnects() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        calls = dict(fake.calls)
        assert calls["username_pw_set"] == ("u", "p")
        assert calls["will_set"] == ("meshcore/radio1/availability", b"offline", 1, True)
        assert calls["reconnect_delay_set"] == (1, 60)
        assert calls["connect_async"] == ("broker.example.org", 1883, 60)
        assert "loop_start" in names(fake)
        assert "tls_set_context" not in names(fake)

    asyncio.run(scenario())


def test_tls_uses_a_verifying_context() -> None:
    async def scenario() -> None:
        bus, fake = make_bus(MqttConfig(host="h", node_id="radio1", tls=True))
        await bus.start()
        (context,) = dict(fake.calls)["tls_set_context"]
        assert context.check_hostname is True
        assert "username_pw_set" not in names(fake)

    asyncio.run(scenario())


def test_connection_subscribes_and_announces_availability() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        reason = SimpleNamespace(is_failure=False)
        await asyncio.to_thread(fake.on_connect, fake, None, None, reason, None)
        await asyncio.wait_for(connected.wait(), 1)
        assert dict(fake.calls)["subscribe"] == (
            [
                ("meshcore/radio1/send/+", 1),
                ("meshcore/radio1/contacts/+/send", 1),
                ("meshcore/radio1/add_contact", 1),
                ("meshcore/radio1/contacts/+/remove", 1),
                ("meshcore/radio1/contacts/+/telemetry", 1),
                *[
                    (f"meshcore/radio1/{name}", 1)
                    for name in (
                        "availability",
                        "contacts",
                        "history",
                        "nodes",
                        "pending",
                        "settings",
                        "state",
                        "stats",
                        "contacts/+",
                    )
                ],
                ("homeassistant/status", 1),
            ],
        )
        assert fake.published[-1] == ("meshcore/radio1/availability", b"online", 1, True)
        assert bus.connected

    asyncio.run(scenario())


def test_every_reconnect_subscribes_and_announces_again() -> None:
    """With a clean session the broker forgets the subscriptions with the connection."""

    async def scenario() -> None:
        bus, fake = make_bus()
        connections: list[int] = []
        bus.on_connected.append(lambda: connections.append(1))
        await bus.start()
        reason = SimpleNamespace(is_failure=False)
        for _ in range(2):
            await asyncio.to_thread(fake.on_connect, fake, None, None, reason, None)
            await asyncio.sleep(0)
            assert bus.connected
            fake.on_disconnect(fake, None, None, "lost", None)
            await asyncio.sleep(0)
            assert not bus.connected
        assert names(fake).count("subscribe") == 2
        online = [p for p in fake.published if p[:2] == (TOPICS.availability, b"online")]
        assert len(online) == 2
        assert connections == [1, 1]

    asyncio.run(scenario())


def test_refused_connection_is_only_logged(caplog: pytest.LogCaptureFixture) -> None:
    """With the user name: Mosquitto gives the same reason for every credentials problem."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        refused = ReasonCode(PacketTypes.CONNACK, identifier=135)
        fake.on_connect(fake, None, None, refused, None)
        fake.on_disconnect(fake, None, None, "Unspecified error", None)  # paho closes after it
        await asyncio.sleep(0)
        assert not bus.connected
        assert "subscribe" not in names(fake)
        assert "connection refused: Not authorized (user 'u'; test the login with" in caplog.text
        assert "check-config --broker" in caplog.text
        assert "'p'" not in caplog.text
        assert "without answering" not in caplog.text  # the refusal was the answer

    asyncio.run(scenario())


def test_a_connection_closed_without_an_answer_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A plain connection to a TLS port, or a broker that refuses a feature, sends no CONNACK."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.on_disconnect(fake, None, None, "Unspecified error", None)
        await asyncio.sleep(0)
        assert (
            "MQTT: broker.example.org:1883 closed the connection without answering "
            "(Unspecified error)"
        ) in caplog.text

    asyncio.run(scenario())


def test_a_broker_that_cannot_be_reached_is_logged_with_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wrong name or address gets no answer from a broker: paho reports the attempt only."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        try:  # paho calls on_connect_fail while it handles the attempt's error
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        except OSError:
            fake.on_connect_fail(fake, None)
        fake.on_connect_fail(fake, None)  # without an error at hand
        assert "cannot reach broker.example.org:1883: Name or service not known" in caplog.text
        assert "cannot reach broker.example.org:1883: no connection" in caplog.text
        assert not bus.connected

    asyncio.run(scenario())


def test_with_paho_a_closed_port_is_logged_at_every_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real client: no broker listens, so no answer ever comes."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    async def scenario() -> None:
        config = MqttConfig(host="127.0.0.1", port=port, node_id="radio1")
        bus = MqttBus(config, TOPICS, None)
        await bus.start()
        try:
            for _ in range(100):
                if "cannot reach" in caplog.text:
                    break
                await asyncio.sleep(0.05)
        finally:
            await bus.stop()
        assert f"MQTT: cannot reach 127.0.0.1:{port}: Connection refused" in caplog.text

    asyncio.run(scenario())


def test_send_requests_and_home_assistant_birth_reach_the_loop() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        births: list[int] = []
        bus.on_ha_online.append(lambda: births.append(1))
        await bus.start()

        def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
            message = SimpleNamespace(topic=topic, payload=payload, retain=retain)
            fake.on_message(fake, None, message)

        key = "a1" * 32
        await asyncio.to_thread(deliver, "meshcore/radio1/send/alerts", b"Gate open")
        await asyncio.to_thread(deliver, "meshcore/radio1/send/alerts", b"old", True)
        await asyncio.to_thread(deliver, "homeassistant/status", b"online")
        await asyncio.to_thread(deliver, "homeassistant/status", b"offline")
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/send", b"Hi")
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key.upper()}/send", b"no")
        await asyncio.to_thread(deliver, "meshcore/radio1/contacts", b"{}")
        first = await asyncio.wait_for(bus.requests.get(), 1)
        second = await asyncio.wait_for(bus.requests.get(), 1)
        third = await asyncio.wait_for(bus.requests.get(), 1)
        assert first == SendRequest("alerts", b"Gate open", retained=False)
        assert second == SendRequest("alerts", b"old", retained=True)
        assert third == SendRequest(key, b"Hi", retained=False, contact=True)
        fourth = await asyncio.wait_for(bus.requests.get(), 1)  # rejected by the bridge
        assert fourth == SendRequest(key.upper(), b"no", retained=False, contact=True)
        await asyncio.sleep(0.05)
        assert bus.requests.empty()
        assert births == [1]

    asyncio.run(scenario())


def test_mqtt_5_is_a_setting_and_3_1_1_the_default() -> None:
    """ARC-014: a session that ends at the disconnect either way (clean session, or no session
    expiry interval)."""
    old = make_paho_client(MqttConfig(host="h", node_id="radio1"))
    new = make_paho_client(MqttConfig(host="h", node_id="radio1", protocol="5"))
    assert isinstance(old, paho.Client)
    assert isinstance(new, paho.Client)
    assert (old.protocol, old._clean_session) == (MQTTProtocolVersion.MQTTv311, True)
    assert new.protocol == MQTTProtocolVersion.MQTTv5


def _properties(properties: Properties | None) -> dict[str, Any]:
    assert properties is not None
    names = ("MessageExpiryInterval", "CorrelationData", "UserProperty")
    return {name: getattr(properties, name) for name in names if hasattr(properties, name)}


def test_with_mqtt_5_messages_say_who_published_them_and_events_expire() -> None:
    async def scenario() -> None:
        config = MqttConfig(host="h", node_id="radio1", protocol="5")
        fake = FakePaho()
        bus = MqttBus(
            config,
            TOPICS,
            None,
            client_factory=lambda _: fake,
            event_expiry_s=600,
            command_expiry_s=900,
        )
        await bus.start()
        fake.on_connect(fake, None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        await asyncio.sleep(0)
        key = "a1" * 32
        bus.publish(TOPICS.delivery, b"{}", correlation=b"\x00request-1")
        bus.publish(TOPICS.received, b"{}")
        bus.publish(TOPICS.contact_received(key), b"{}")
        bus.publish(TOPICS.command, b"{}", qos=0)
        bus.publish(TOPICS.state, b"{}", retain=True)
        sent = {
            topic: _properties(p)
            for (topic, *_), p in zip(fake.published, fake.properties, strict=True)
        }
        users = sent[TOPICS.state]["UserProperty"]
        assert [name for name, _ in users] == ["gateway", "version", "published_at"]
        assert users[:2] == [("gateway", "radio1"), ("version", __version__)]
        assert datetime.fromisoformat(users[2][1]).tzinfo is not None
        expiries = {topic: p.get("MessageExpiryInterval") for topic, p in sent.items()}
        assert expiries == {
            TOPICS.availability: None,
            TOPICS.delivery: 600,
            TOPICS.received: 600,
            TOPICS.contact_received(key): 600,
            TOPICS.command: 900,
            TOPICS.state: None,
        }
        assert sent[TOPICS.delivery]["CorrelationData"] == b"\x00request-1"
        assert "CorrelationData" not in sent[TOPICS.received]

    asyncio.run(scenario())


def test_events_expire_after_the_queue_time_and_commands_after_their_age() -> None:
    radio = '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\n'
    mqtt = '[mqtt]\nhost = "h"\nnode_id = "radio1"\n'
    timed = parse_config(radio + mqtt + "[messages]\nqueue_ttl_s = 120\n")
    assert event_expiries(timed) == (120, 900)
    ageless = parse_config(radio + mqtt + "[commands]\nmax_age_s = 0\n")
    assert event_expiries(ageless) == (600, 600)


def test_with_mqtt_3_1_1_messages_carry_no_properties() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.on_connect(fake, None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        await asyncio.sleep(0)
        bus.publish(TOPICS.delivery, b"{}", correlation=b"ignored")
        assert fake.properties == [None, None]

    asyncio.run(scenario())


def test_the_correlation_data_of_a_send_request_reaches_the_loop() -> None:
    async def scenario() -> None:
        bus, fake = make_bus(MqttConfig(host="h", node_id="radio1", protocol="5"))
        await bus.start()
        properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
        properties.CorrelationData = b"r-7"
        key = "a1" * 32
        for topic in ("meshcore/radio1/send/alerts", f"meshcore/radio1/contacts/{key}/send"):
            message = SimpleNamespace(
                topic=topic, payload=b"Gate open", retain=False, properties=properties
            )
            await asyncio.to_thread(fake.on_message, fake, None, message)
        plain = SimpleNamespace(topic="meshcore/radio1/send/alerts", payload=b"x", retain=False)
        await asyncio.to_thread(fake.on_message, fake, None, plain)  # MQTT 3.1.1: no properties
        assert [await asyncio.wait_for(bus.requests.get(), 1) for _ in range(3)] == [
            SendRequest("alerts", b"Gate open", False, False, b"r-7"),
            SendRequest(key, b"Gate open", False, True, b"r-7"),
            SendRequest("alerts", b"x", False),
        ]

    asyncio.run(scenario())


def test_contact_requests_reach_their_own_queue() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()

        def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
            fake.on_message(
                fake, None, SimpleNamespace(topic=topic, payload=payload, retain=retain)
            )

        key = "a1" * 32
        await asyncio.to_thread(deliver, "meshcore/radio1/add_contact", key.encode())
        await asyncio.to_thread(deliver, "meshcore/radio1/add_contact", b"old", True)
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/remove", b"remove")
        await asyncio.to_thread(deliver, "meshcore/radio1/contacts/x/y/remove", b"")
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/telemetry", b"off")
        await asyncio.to_thread(deliver, "meshcore/radio1/contacts/x/telemetry", b"on", True)
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/x/telemetry", b"off")
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/telemetry/x", b"off")
        requests = [await asyncio.wait_for(bus.contact_requests.get(), 1) for _ in range(5)]
        assert requests == [
            ContactRequest("add", key.encode(), False),
            ContactRequest("add", b"old", True),
            ContactRequest("remove", b"remove", False, key),
            ContactRequest("telemetry", b"off", False, key),
            ContactRequest("telemetry", b"on", True, "x"),  # the manager refuses the level
        ]
        await asyncio.sleep(0.05)
        assert bus.contact_requests.empty()  # a level with a "/" names no contact
        assert bus.requests.empty()
        assert TOPICS.contact_telemetry(key) == f"meshcore/radio1/contacts/{key}/telemetry"
        assert TOPICS.telemetry_event == "meshcore/radio1/telemetry"

    asyncio.run(scenario())


def test_discovery_topics_and_the_show_request_reach_the_loop() -> None:
    """The gateway watches its discovery topics, to notice what Home Assistant clears there (a
    contact device the user deleted, the gateway's own device), its offline status, and the
    request to show deleted contact devices again."""

    async def scenario() -> None:
        fake = FakePaho()
        config = MqttConfig(host="broker.example.org", node_id="radio1")
        own = "homeassistant/device/meshcore_home_gateway_radio1/config"
        watch = "homeassistant/device/meshcore_home_gateway_radio1/+/config"
        bus = MqttBus(
            config, TOPICS, "homeassistant/status", lambda _: fake, ha_devices=[own, watch]
        )
        seen: list[tuple[str, bytes, bool]] = []
        shown: list[int] = []
        offline: list[int] = []
        bus.on_ha_device.append(lambda *message: seen.append(message))
        bus.on_show_hidden.append(lambda: shown.append(1))
        bus.on_ha_offline.append(lambda: offline.append(1))
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        subscriptions = dict(fake.calls)["subscribe"][0]
        assert subscriptions[-5:] == [
            (own, 1),
            (watch, 1),
            ("meshcore/radio1/homeassistant/show_hidden", 1),
            ("meshcore/radio1/homeassistant/add_node", 1),
            ("meshcore/radio1/homeassistant/draft/+/+/request", 1),  # the chats' drafts
        ]
        assert ("meshcore/radio1/homeassistant/draft/+/+", 1) in subscriptions  # read back

        def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
            fake.on_message(
                fake, None, SimpleNamespace(topic=topic, payload=payload, retain=retain)
            )

        device = "homeassistant/device/meshcore_home_gateway_radio1/contact_x/config"
        await asyncio.to_thread(deliver, device, b"")
        await asyncio.to_thread(deliver, device, b"", True)
        await asyncio.to_thread(deliver, own, b"")
        await asyncio.to_thread(deliver, "homeassistant/device/other/config", b"")
        await asyncio.to_thread(deliver, "homeassistant/status", b"offline")
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/show_hidden", b"show")
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/show_hidden", b"x", True)
        label = "b0b0b0b0b0b0…12121212 Bob".encode()  # the select's choice
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/add_node", label)
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/add_node", b"x", True)
        await asyncio.sleep(0.05)
        assert seen == [(device, b"", False), (device, b"", True), (own, b"", False)]
        assert (shown, offline) == ([1], [1])
        assert bus.requests.empty()
        picked = [bus.contact_requests.get_nowait() for _ in range(2)]
        assert picked == [
            ContactRequest("add", label, False, node_label=True),
            ContactRequest("add", b"x", True, node_label=True),  # refused there, with an event
        ]

    asyncio.run(scenario())


def test_without_contact_devices_their_topics_are_not_watched() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        topics = [topic for topic, _ in dict(fake.calls)["subscribe"][0]]
        assert "meshcore/radio1/homeassistant/show_hidden" not in topics
        assert "meshcore/radio1/homeassistant/add_node" not in topics
        assert not any(topic.startswith("homeassistant/device") for topic in topics)
        assert not any("/homeassistant/draft/" in topic for topic in topics)  # no chats either

    asyncio.run(scenario())


def test_disconnect_is_tracked() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=False), None)
        await asyncio.sleep(0)
        fake.on_disconnect(fake, None, None, "lost", None)
        await asyncio.sleep(0)
        assert not bus.connected

    asyncio.run(scenario())


def deliver_to(fake: FakePaho) -> Callable[..., None]:
    def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
        fake.on_message(fake, None, SimpleNamespace(topic=topic, payload=payload, retain=retain))

    return deliver


def test_a_refused_subscription_is_named(caplog: pytest.LogCaptureFixture) -> None:
    """An ACL without read access would lose requests and leave forged documents unseen."""

    class Subscribing(FakePaho):
        def subscribe(self, topic: list[tuple[str, int]]) -> tuple[int, int]:
            return 0, 7

    async def scenario() -> None:
        fake = Subscribing()
        bus = MqttBus(
            MqttConfig(host="h", node_id="radio1"),
            TOPICS,
            "homeassistant/status",
            client_factory=lambda _: fake,
        )
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        granted, refused = (ReasonCode(PacketTypes.SUBACK, identifier=c) for c in (1, 0x80))
        fake.on_subscribe(fake, None, 6, [refused] * 15, None)  # another subscription's answer
        assert "refused the subscription" not in caplog.text
        fake.on_subscribe(fake, None, 7, [granted] * 5 + [refused] * 9 + [granted], None)
        assert (
            "refused the subscription to meshcore/radio1/availability, meshcore/radio1/contacts,"
        ) in caplog.text
        assert "meshcore/radio1/contacts/+ (the gateway's user needs read access" in caplog.text
        assert "homeassistant/status" not in caplog.text

    asyncio.run(scenario())


def test_what_another_client_writes_on_a_retained_topic_is_undone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-005: a forged document (here a position) or a cleared one stands only until its copy
    comes back to the gateway; its own copies, and the broker's for the new subscription, do
    nothing."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        await asyncio.sleep(0)
        deliver = deliver_to(fake)
        contact = TOPICS.contact("ab" * 32)
        await asyncio.to_thread(deliver, contact, b"stale", True)  # the broker's copy
        bus.publish(contact, b'{"latitude": 1.0}', retain=True)
        await asyncio.to_thread(deliver, TOPICS.availability, b"online")  # copies come back
        await asyncio.to_thread(deliver, contact, b'{"latitude": 1.0}')
        await asyncio.sleep(0.05)
        assert fake.published[-1] == (contact, b'{"latitude": 1.0}', 1, True)
        before = len(fake.published)

        await asyncio.to_thread(deliver, contact, b'{"latitude": 47.0}')
        await asyncio.to_thread(deliver, TOPICS.availability, b"offline")
        await asyncio.sleep(0.05)
        assert fake.published[before:] == [
            (contact, b'{"latitude": 1.0}', 1, True),
            (TOPICS.availability, b"online", 1, True),
        ]
        assert f"another client wrote {contact}" in caplog.text
        assert bus.requests.empty()
        assert bus.contact_requests.empty()

    asyncio.run(scenario())


@pytest.mark.parametrize("comes_back", [True, False])
def test_own_messages_that_never_come_back_are_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, comes_back: bool
) -> None:
    """Mosquitto's acl_file grants every subscription and leaves out what may not be read: the
    gateway notices only that its `online` does not come back."""
    monkeypatch.setattr(client_module, "ECHO_WAIT_S", 0.05)

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        await asyncio.sleep(0)
        if comes_back:
            await asyncio.to_thread(deliver_to(fake), TOPICS.availability, b"online")
        await asyncio.sleep(0.1)
        assert ("did not come back within" in caplog.text) is not comes_back
        caplog.clear()
        fake.on_disconnect(fake, None, None, "lost", None)  # a check due after a loss: none
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        await asyncio.sleep(0)
        fake.on_disconnect(fake, None, None, "lost", None)
        await asyncio.sleep(0.1)
        assert "did not come back" not in caplog.text

        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        await asyncio.sleep(0)
        await bus.stop()  # before the check is due
        await asyncio.sleep(0.1)
        assert "did not come back" not in caplog.text

    asyncio.run(scenario())


def test_a_stopping_gateway_restores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its `offline` must stay the last word, also with a restore waiting for its interval."""
    monkeypatch.setattr(retained_module, "HEAL_INTERVAL_S", 0.05)

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        await asyncio.sleep(0)
        bus.publish(TOPICS.state, b"{}", retain=True)
        for forged in (b"forged", b"again"):  # the second restore waits
            await asyncio.to_thread(deliver_to(fake), TOPICS.state, forged)
        await asyncio.sleep(0.01)
        await bus.stop()
        await asyncio.to_thread(deliver_to(fake), TOPICS.availability, b"offline")
        await asyncio.to_thread(deliver_to(fake), TOPICS.state, b"forged")
        await asyncio.sleep(0.1)
        assert fake.published[-1] == (TOPICS.availability, b"offline", 1, True)

    asyncio.run(scenario())


def test_events_are_bounded_while_the_broker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """paho's own queue limit would also drop `online`, discovery and state at the reconnect,
    after a long outage; the bus bounds only the events and skips retained documents, which
    are published again when the connection is back."""
    monkeypatch.setattr(client_module, "MAX_QUEUED_EVENTS", 2)

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        assert dict(fake.calls)["max_queued_messages_set"] == (0,)  # no limit inside paho
        for n in range(4):
            bus.publish("meshcore/radio1/received", b"%d" % n)
        bus.publish("meshcore/radio1/state", b"{}", retain=True)
        assert [p[1] for p in fake.published] == [b"0", b"1"]
        assert caplog.text.count("dropping events") == 1
        bus._handle_connected()  # the connection is back: the bound starts again
        bus.publish("meshcore/radio1/state", b"{}", retain=True)
        bus.publish("meshcore/radio1/received", b"4")
        assert [p[1] for p in fake.published][-2:] == [b"{}", b"4"]
        assert "2 events were dropped" in caplog.text
        assert bus.dropped_1h() == 2  # the health section tells them for an hour (ARC-012)

    asyncio.run(scenario())


def test_stop_warns_when_offline_is_not_confirmed(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        fake.publish = lambda *a, **k: SimpleNamespace(  # type: ignore[method-assign]
            wait_for_publish=lambda timeout: None, is_published=lambda: False
        )
        await bus.start()
        bus.connected = True
        await bus.stop()
        bus._handle_disconnected("Normal disconnection")  # paho reports the clean disconnect
        assert "offline status not confirmed" in caplog.text
        assert "reconnecting" not in caplog.text

    asyncio.run(scenario())


def test_stop_announces_offline_and_disconnects() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await bus.stop()
        assert fake.published[-1] == ("meshcore/radio1/availability", b"offline", 1, True)
        assert names(fake)[-2:] == ["disconnect", "loop_stop"]

    asyncio.run(scenario())


def test_what_a_stop_must_publish_goes_out_before_offline() -> None:
    """A clean stop clears the chats' drafts (drafts.py) while the connection is still up, in
    the order of QoS 1 before `offline`."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        await asyncio.sleep(0.05)
        draft = "meshcore/radio1/homeassistant/draft/target/family"
        bus.on_stopping.append(lambda: bus.publish(draft, b'{"text":""}', retain=True))
        await bus.stop()
        assert fake.published[-2:] == [
            (draft, b'{"text":""}', 1, True),
            ("meshcore/radio1/availability", b"offline", 1, True),
        ]

    asyncio.run(scenario())


# --- integration with a real broker -----------------------------------------------------------


class Observer:
    """A plain paho client that records what arrives on the bridge's topics."""

    def __init__(self, broker: Broker) -> None:
        self.messages: list[tuple[str, bytes, bool]] = []
        self.event = threading.Event()
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.client.on_message = self._message
        self.client.connect(broker.host, broker.port)
        self.client.subscribe("meshcore/#", qos=1)
        self.client.loop_start()

    def _message(self, _c: Any, _u: Any, message: Any) -> None:
        self.messages.append((message.topic, bytes(message.payload), bool(message.retain)))
        self.event.set()

    def wait_for(self, topic: str, payload: bytes, timeout: float = 5.0) -> None:
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.start()
        try:
            while not deadline.is_set():
                if any(t == topic and p == payload for t, p, _ in self.messages):
                    return
                self.event.wait(0.05)
                self.event.clear()
        finally:
            timer.cancel()
        raise AssertionError(f"{topic} {payload!r} not seen; got {self.messages}")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.mark.broker
def test_with_broker_availability_requests_and_last_will(broker: Broker) -> None:
    async def scenario() -> None:
        observer = Observer(broker)
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", keepalive_s=10)
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        try:
            await asyncio.wait_for(connected.wait(), 5)
            await asyncio.to_thread(observer.wait_for, TOPICS.availability, b"online")

            observer.client.publish(TOPICS.send("alerts"), b"Hello", qos=1)
            request = await asyncio.wait_for(bus.requests.get(), 5)
            assert request == SendRequest("alerts", b"Hello", retained=False)

            # Abrupt loss of the connection: the broker publishes the last will.
            client: Any = bus._client  # test reaches into the adapter
            client.loop_stop()
            sock = client.socket()
            assert isinstance(sock, socket.socket)
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            await asyncio.to_thread(observer.wait_for, TOPICS.availability, b"offline")
        finally:
            observer.close()

    asyncio.run(scenario())


@pytest.mark.broker
def test_with_broker_mqtt_5_carries_expiry_correlation_and_user_properties(
    broker: Broker,
) -> None:
    """ARC-014 against Mosquitto: a request's correlation data reaches the gateway, and its
    event carries it back, with the expiry and the gateway's name."""
    received: list[Any] = []
    arrived, subscribed = threading.Event(), threading.Event()
    observer = paho.Client(
        callback_api_version=CallbackAPIVersion.VERSION2, protocol=MQTTProtocolVersion.MQTTv5
    )

    def on_message(_c: Any, _u: Any, message: Any) -> None:
        received.append(message)
        arrived.set()

    observer.on_message = on_message
    observer.on_subscribe = lambda *_: subscribed.set()

    async def scenario() -> None:
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", protocol="5")
        bus = MqttBus(config, TOPICS, None, event_expiry_s=600)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        try:
            await asyncio.wait_for(connected.wait(), 5)
            properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
            properties.CorrelationData = b"r-1"
            observer.publish(TOPICS.send("alerts"), b"Hello", qos=1, properties=properties)
            request = await asyncio.wait_for(bus.requests.get(), 5)
            assert request == SendRequest("alerts", b"Hello", False, False, b"r-1")
            bus.publish(TOPICS.delivery, b"{}", correlation=request.correlation)
            assert await asyncio.to_thread(arrived.wait, 5)
        finally:
            await bus.stop()

    observer.connect(broker.host, broker.port)
    observer.subscribe(TOPICS.delivery, qos=1)
    observer.loop_start()
    try:
        assert subscribed.wait(5)
        asyncio.run(scenario())
        echoed = received[0].properties
        assert echoed.CorrelationData == b"r-1"
        assert 0 < echoed.MessageExpiryInterval <= 600
        assert ("gateway", "radio1") in echoed.UserProperty
    finally:
        observer.loop_stop()
        observer.disconnect()


@pytest.mark.broker
def test_with_broker_clean_stop_leaves_offline_retained(broker: Broker) -> None:
    async def scenario() -> None:
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1")
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        await asyncio.wait_for(connected.wait(), 5)
        await bus.stop()
        late = Observer(broker)  # subscribes after the fact: sees the retained value
        try:
            await asyncio.to_thread(late.wait_for, TOPICS.availability, b"offline")
            assert (TOPICS.availability, b"offline", True) in late.messages
        finally:
            late.close()

    asyncio.run(scenario())


@pytest.mark.broker
def test_with_broker_a_retained_request_is_flagged_only_when_replayed(broker: Broker) -> None:
    """MQTT 3.1.1 clears the retain flag of a live message: a retained request published while
    the bridge is connected arrives unflagged and is sent once. The copy the broker keeps arrives
    flagged after every reconnect, so the bridge rejects it (docs/mqtt-api.md, Sending)."""

    async def scenario() -> None:
        observer = Observer(broker)
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", keepalive_s=10)
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        try:
            await asyncio.wait_for(connected.wait(), 5)
            await asyncio.to_thread(observer.wait_for, TOPICS.availability, b"online")
            observer.client.publish(TOPICS.send("alerts"), b"Gate open", qos=1, retain=True)
            live = await asyncio.wait_for(bus.requests.get(), 5)
            assert live == SendRequest("alerts", b"Gate open", retained=False)

            connected.clear()
            client: Any = bus._client  # drop the connection; paho reconnects by itself
            client.socket().shutdown(socket.SHUT_RDWR)
            await asyncio.wait_for(connected.wait(), 10)
            replayed = await asyncio.wait_for(bus.requests.get(), 5)
            assert replayed == SendRequest("alerts", b"Gate open", retained=True)
            assert bus.requests.empty()
        finally:
            await bus.stop()
            observer.close()

    asyncio.run(scenario())


@pytest.mark.broker
def test_with_broker_a_forged_document_is_restored(broker: Broker) -> None:
    """SEC-005 against Mosquitto: the forged state comes back to the gateway unflagged, the
    gateway writes its own again, and the broker keeps that one."""
    own, forged = b'{"radio": "connected"}', b'{"radio": "forged"}'

    async def scenario() -> None:
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", keepalive_s=10)
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        forger = Observer(broker)
        await bus.start()
        try:
            await asyncio.wait_for(connected.wait(), 5)
            bus.publish(TOPICS.state, own, retain=True)
            await asyncio.to_thread(forger.wait_for, TOPICS.state, own)
            forger.client.publish(TOPICS.state, forged, qos=1, retain=True)

            def restored() -> bool:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    states = [p for t, p, _ in forger.messages if t == TOPICS.state]
                    if forged in states and states[-1] == own:
                        return True
                    forger.event.wait(0.05)
                    forger.event.clear()
                return False

            assert await asyncio.to_thread(restored), forger.messages
        finally:
            await bus.stop()
            forger.close()
        late = Observer(broker)  # subscribes after the fact: sees what the broker keeps
        try:
            await asyncio.to_thread(late.wait_for, TOPICS.state, own)
            assert (TOPICS.state, forged, True) not in late.messages
        finally:
            late.close()

    asyncio.run(scenario())


def test_events_queued_for_the_reconnect_are_not_logged_as_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """paho keeps a QoS 1 message for the reconnect and still answers MQTT_ERR_NO_CONN."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.publish = lambda *a, **k: SimpleNamespace(rc=paho.MQTT_ERR_NO_CONN)  # type: ignore[method-assign]
        bus.publish("meshcore/radio1/received", b"queued")
        assert "refused" not in caplog.text
        fake.publish = lambda *a, **k: SimpleNamespace(rc=paho.MQTT_ERR_QUEUE_SIZE)  # type: ignore[method-assign]
        bus.publish("meshcore/radio1/received", b"lost")
        assert "refused" in caplog.text

    asyncio.run(scenario())


def test_the_quality_of_service_reaches_paho() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        bus._handle_connected()
        bus.publish("meshcore/radio1/command", b"{}", qos=0)
        bus.publish("meshcore/radio1/received", b"{}")
        assert [(p[0], p[2]) for p in fake.published][-2:] == [
            ("meshcore/radio1/command", 0),
            ("meshcore/radio1/received", 1),
        ]
        bus.connected = False
        before = len(fake.published)
        bus.publish("meshcore/radio1/command", b"{}", qos=0)  # nothing to keep for later
        assert len(fake.published) == before

    asyncio.run(scenario())
