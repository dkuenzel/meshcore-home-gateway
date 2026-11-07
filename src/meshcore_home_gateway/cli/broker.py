"""The commands' MQTT client (`send`, `listen`, `contacts`, `check-config --broker`): a client
id of its own, QoS 1 (0 for a sweep of retained messages) and bounded waits for the broker's
answers. The commands call it through the module (`broker.mqtt_client`), so that a test
replaces it in one place."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Sequence
from typing import Any

from meshcore_home_gateway.cli.common import tr
from meshcore_home_gateway.config import Config, MqttConfig

CONNECT_TIMEOUT_S = 10.0
PUBLISH_TIMEOUT_S = 10.0
SUBSCRIBE_TIMEOUT_S = 5.0
RETAINED_WAIT_S = 2.0  # for the gateway's retained documents; brokers send them on SUBACK
RETAINED_QUIET_S = 1.0  # a sweep of retained messages ends when none has come for this long,
RETAINED_MAX_S = 60.0  # at the latest after this long


def mqtt_client(mqtt: MqttConfig, suffix: str) -> Any:
    import dataclasses  # noqa: PLC0415

    from meshcore_home_gateway.mqtt.client import make_paho_client, tls_context  # noqa: PLC0415

    client_id = f"meshcore-home-gateway-{suffix}-{os.getpid()}"
    client = make_paho_client(dataclasses.replace(mqtt, client_id=client_id))
    if mqtt.username:
        client.username_pw_set(mqtt.username, mqtt.password)
    if mqtt.tls:
        client.tls_set_context(tls_context(mqtt))
    return client


class Connection:
    """Tracks the broker's answers to CONNECT; `on_ready` runs after each accepted one. A refusal
    leaves its text (paho's) and its reason code's value."""

    def __init__(self, on_ready: Callable[[Any], None] | None = None) -> None:
        self.answered = threading.Event()
        self.refusal: str | None = None
        self.refusal_value: int | None = None
        self._on_ready = on_ready

    def on_connect(
        self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any
    ) -> None:
        if reason_code.is_failure:
            self.refusal = str(reason_code)
            self.refusal_value = getattr(reason_code, "value", None)
        elif self._on_ready is not None:
            self._on_ready(client)
        self.answered.set()

    def open(self, client: Any, mqtt: MqttConfig) -> str | None:
        """Connect and wait for the answer; returns an error text, or None when connected."""
        client.on_connect = self.on_connect
        try:
            client.connect(mqtt.host, mqtt.port, mqtt.keepalive_s)
        except (OSError, UnicodeError) as exc:  # refused, unreachable, unknown name, TLS failure
            return tr(
                "cannot connect to the broker {host}:{port}: {detail}",
                host=mqtt.host,
                port=mqtt.port,
                detail=exc,
            )
        client.loop_start()
        if not self.answered.wait(CONNECT_TIMEOUT_S):
            return tr("broker did not answer")
        if self.refusal is not None:
            # Translators: the reason is the broker's, in English (paho's text)
            return tr("broker refused the connection: {reason}", reason=self.refusal)
        return None


def subscribe(client: Any, topic: str, qos: int = 1) -> str | None:
    """Subscribe and wait for the broker's answer; returns an error text. With QoS 1, Mosquitto
    passes on at most 1040 retained messages to a new subscription and drops the others; with
    QoS 0, all (docs/platform-notes.md, Mosquitto)."""
    answered = threading.Event()
    refused: list[str] = []

    def on_subscribe(_c: Any, _u: Any, _mid: Any, reason_codes: Any, _p: Any) -> None:
        refused.extend(str(code) for code in reason_codes if code.is_failure)
        answered.set()

    client.on_subscribe = on_subscribe
    client.subscribe(topic, qos=qos)
    if not answered.wait(SUBSCRIBE_TIMEOUT_S):
        return tr("broker did not confirm the subscription")
    if refused:
        # Translators: the reason is the broker's, in English (paho's text)
        return tr(
            "broker refused the subscription to {topic}: {reason}", topic=topic, reason=refused[0]
        )
    return None


def unsubscribe(client: Any, topic: str) -> str | None:
    """End a subscription and wait for the broker's answer, which Mosquitto sends after every
    message it queued for the subscription; returns an error text."""
    answered = threading.Event()
    client.on_unsubscribe = lambda *_: answered.set()
    client.unsubscribe(topic)
    if not answered.wait(SUBSCRIBE_TIMEOUT_S):
        return tr("broker did not confirm the end of the subscription")
    return None


def publish(client: Any, topic: str, payload: bytes, *, retain: bool = False) -> str | None:
    """Publish with QoS 1 and wait for the broker's acknowledgement; returns an error text.
    `retain` with an empty payload clears the topic's retained message."""
    info = client.publish(topic, payload, qos=1, retain=retain)
    try:
        info.wait_for_publish(PUBLISH_TIMEOUT_S)
    except (RuntimeError, ValueError) as exc:
        return tr("request not sent: {detail}", detail=exc)
    return None if info.is_published() else tr("broker did not confirm the request")


class Retained:
    """The retained messages of some topics (paho thread); JSON where they are JSON."""

    def __init__(self, topics: Sequence[str]) -> None:
        self.topics = tuple(topics)
        self.messages: dict[str, Any] = {}
        self.complete = threading.Event()

    def on_message(self, message: Any) -> None:
        if message.topic not in self.topics or not message.retain or not message.payload:
            return
        try:
            self.messages[message.topic] = json.loads(message.payload)
        except (ValueError, RecursionError):
            self.messages[message.topic] = message.payload.decode("utf-8", errors="replace")
        if set(self.messages) == set(self.topics):
            self.complete.set()


class GatewaySession:
    """A broker connection to the gateway's topics: its availability and some of its retained
    documents (`documents`: the names of `Topics` attributes, such as "contacts", "nodes",
    "state"), with a client id of the command's own (`suffix`)."""

    def __init__(
        self, config: Config, documents: Sequence[str] = (), suffix: str = "status"
    ) -> None:
        from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

        self.config = config
        self.topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
        names = [self.topics.availability, *(getattr(self.topics, d) for d in documents)]
        self.retained = Retained(names)
        self.client = mqtt_client(config.mqtt, suffix)
        self.client.on_message = self._on_message

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        self.retained.on_message(message)

    def open(self) -> str | None:
        """Connect and read the retained documents; an error text, or None."""
        error = Connection().open(self.client, self.config.mqtt)
        for topic in () if error else self.retained.topics:
            error = error or subscribe(self.client, topic)
        if error is None:
            self.retained.complete.wait(RETAINED_WAIT_S)
        return error

    def document(self, name: str) -> Any:
        return self.retained.messages.get(getattr(self.topics, name))

    @property
    def online(self) -> bool:
        return bool(self.document("availability") == "online")

    def close(self) -> None:
        self.client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged
        self.client.loop_stop()
