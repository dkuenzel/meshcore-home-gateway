"""The commands' MQTT client (`send`, `listen`, `contacts`, `check-config --broker`): a client
id of its own, QoS 1 and bounded waits for the broker's answers. The commands call it through the
module (`broker.mqtt_client`), so that a test replaces it in one place."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from meshcore_home_gateway.cli.common import tr
from meshcore_home_gateway.config import MqttConfig

CONNECT_TIMEOUT_S = 10.0


PUBLISH_TIMEOUT_S = 10.0


SUBSCRIBE_TIMEOUT_S = 5.0


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
    """Tracks the broker's answers to CONNECT; `on_ready` runs after each accepted one."""

    def __init__(self, on_ready: Callable[[Any], None] | None = None) -> None:
        self.answered = threading.Event()
        self.refusal: str | None = None
        self._on_ready = on_ready

    def on_connect(
        self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any
    ) -> None:
        if reason_code.is_failure:
            self.refusal = str(reason_code)
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


def subscribe(client: Any, topic: str) -> str | None:
    """Subscribe with QoS 1 and wait for the broker's answer; returns an error text."""
    answered = threading.Event()
    refused: list[str] = []

    def on_subscribe(_c: Any, _u: Any, _mid: Any, reason_codes: Any, _p: Any) -> None:
        refused.extend(str(code) for code in reason_codes if code.is_failure)
        answered.set()

    client.on_subscribe = on_subscribe
    client.subscribe(topic, qos=1)
    if not answered.wait(SUBSCRIBE_TIMEOUT_S):
        return tr("broker did not confirm the subscription")
    if refused:
        # Translators: the reason is the broker's, in English (paho's text)
        return tr(
            "broker refused the subscription to {topic}: {reason}", topic=topic, reason=refused[0]
        )
    return None


def publish(client: Any, topic: str, payload: bytes) -> str | None:
    """Publish with QoS 1 and wait for the broker's acknowledgement; returns an error text."""
    info = client.publish(topic, payload, qos=1)
    try:
        info.wait_for_publish(PUBLISH_TIMEOUT_S)
    except (RuntimeError, ValueError) as exc:
        return tr("request not sent: {detail}", detail=exc)
    return None if info.is_published() else tr("broker did not confirm the request")
