"""The connection of the Homie adapter (EXT-011): its own, with the device's last will, `$state`
`lost` (Homie 4.0.0, section 2.4), and the broker settings of `[mqtt]`. paho runs it in its own
thread, which hands everything to the event loop (ADR 0007), as for the gateway's connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from meshcore_home_gateway.config import MqttConfig
from meshcore_home_gateway.mqtt.client import (
    RECONNECT_MAX_S,
    RECONNECT_MIN_S,
    PahoLike,
    default_client_id,
    make_paho_client,
    tls_context,
)
from meshcore_home_gateway.mqtt.homie import HomieDevice
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import SendRequest

log = logging.getLogger(__name__)


class HomieConnection:
    """The adapter's own connection to the broker, with the device's last will (ADR 0007: paho's
    thread hands everything to the event loop)."""

    def __init__(
        self,
        config: MqttConfig,
        device: HomieDevice,
        client_factory: Callable[[MqttConfig], PahoLike] = make_paho_client,
    ) -> None:
        own = f"{config.client_id or default_client_id(config.node_id)}-homie"
        self._config = config
        self._device = device
        self._client = client_factory(replace(config, client_id=own))
        self._loop: asyncio.AbstractEventLoop | None = None
        self.connected = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        client, config = self._client, self._config
        if config.username:
            client.username_pw_set(config.username, config.password)
        if config.tls:
            client.tls_set_context(tls_context(config))
        client.will_set(self._device.will, b"lost", qos=1, retain=True)
        client.reconnect_delay_set(RECONNECT_MIN_S, RECONNECT_MAX_S)
        client.on_connect = self._paho_connected
        client.on_disconnect = self._paho_disconnected
        client.on_message = self._paho_message
        client.connect_async(config.host, config.port, config.keepalive_s)
        client.loop_start()

    def publish(self, topic: str, payload: bytes, retain: bool) -> None:
        """Only while connected: the next connect publishes everything again."""
        if self.connected:
            self._client.publish(topic, payload, qos=1, retain=retain)

    async def stop(self, timeout_s: float = 2.0) -> None:
        """`$state` `disconnected`, as Homie asks before a clean disconnect, and nothing of the
        device after it (a change of its nodes may wait for its time)."""
        self._device.disconnected()
        info = self._client.publish(self._device.will, b"disconnected", qos=1, retain=True)
        wait = getattr(info, "wait_for_publish", None)
        if self.connected and callable(wait):
            try:
                async with asyncio.timeout(timeout_s + 0.5):
                    await asyncio.to_thread(wait, timeout_s)
            except (TimeoutError, RuntimeError, ValueError):
                log.warning("Homie: state disconnected not confirmed before shutdown")
        self._client.disconnect()
        self._client.loop_stop()

    # --- paho thread -----------------------------------------------------------------------------

    def _paho_connected(self, client: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
        if getattr(reason, "is_failure", False):
            log.warning("Homie: connection refused: %s", reason)
            return
        client.subscribe([(self._device.set_subscription, 1)])
        self._call(self._connected)

    def _paho_disconnected(self, _c: Any, _u: Any, _f: Any, _r: Any, _p: Any) -> None:
        self._call(self._disconnected)

    def _paho_message(self, _c: Any, _u: Any, message: Any) -> None:
        topic, payload = message.topic, bytes(message.payload)
        self._call(self._device.message, topic, payload, bool(message.retain))

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(callback, *args)

    # --- event loop ------------------------------------------------------------------------------

    def _connected(self) -> None:
        self.connected = True
        log.info("Homie: connected as %s", self._device.base)
        self._device.connected()

    def _disconnected(self) -> None:
        self.connected = False
        self._device.disconnected()

    @property
    def device(self) -> HomieDevice:
        return self._device


def homie_adapter(
    config: MqttConfig,
    limit: int,
    name: str,
    topics: Topics,
    *,
    send: Callable[[SendRequest], None],
    client_factory: Callable[[MqttConfig], PahoLike] = make_paho_client,
) -> HomieConnection:
    """The device, named `name`, with at most `limit` nodes, on a connection of its own."""
    connection: HomieConnection

    def publish(topic: str, payload: bytes, retain: bool) -> None:
        connection.publish(topic, payload, retain)

    def call_later(delay_s: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(delay_s, callback)

    device = HomieDevice(
        config.node_id, name, limit, topics, publish=publish, send=send, call_later=call_later
    )
    connection = HomieConnection(config, device, client_factory)
    return connection
