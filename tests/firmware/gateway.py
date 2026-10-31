"""The gateway's service in the test's event loop, as `meshcore-home-gateway run` runs it, on a
node's serial port and the test's broker (tests/conftest.py); and a client of that broker that
keeps everything the gateway publishes, with the time it came.

The service runs in the test's process, so a test can shorten the schedule's constants
(conftest.py, `fast_schedule`) and read its debug log (`caplog`). It talks to the radio through
its own serial transport, as on a household's server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway import cli
from meshcore_home_gateway.bridge import Bridge
from meshcore_home_gateway.config import parse_config
from tests.conftest import Broker

BASE = "meshcore/test"  # base_topic and node_id of the configuration below
START_S = 15.0  # until the service is up
STOP_S = 15.0
WAIT_S = 30.0


def configuration(serial: Path, broker: Broker, more: str = "") -> str:
    """The service on the radio at `serial`; `more` adds settings of [diagnostics], then
    tables. Home Assistant discovery is off: these tests read the documents themselves."""
    return (
        f'[radio]\ntransport = "serial"\ndevice = "{serial}"\n'
        f'[mqtt]\nhost = "{broker.host}"\nport = {broker.port}\nbase_topic = "meshcore"\n'
        'node_id = "test"\n[homeassistant]\ndiscovery = false\n'
        f'[diagnostics]\nlog_level = "debug"\n{more}'
    )


@dataclass(frozen=True)
class Published:
    at: float  # time.monotonic() when it came
    topic: str  # below BASE
    payload: bytes
    retained: bool

    def json(self) -> Any:
        return json.loads(self.payload) if self.payload else None


class Watcher:
    """Subscribes to everything below BASE; paho runs its loop in a thread of its own."""

    def __init__(self, broker: Broker) -> None:
        self.messages: list[Published] = []
        self._lock = threading.Lock()
        self._client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(broker.host, broker.port)
        self._client.subscribe(f"{BASE}/#", qos=1)
        self._client.loop_start()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        topic = str(message.topic).removeprefix(f"{BASE}/")
        found = Published(time.monotonic(), topic, bytes(message.payload), bool(message.retain))
        with self._lock:
            self.messages.append(found)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(f"{BASE}/{topic}", payload.encode(), qos=1).wait_for_publish(5)

    def mark(self) -> int:
        with self._lock:
            return len(self.messages)

    def on(self, topic: str, since: int = 0) -> list[Published]:
        with self._lock:
            return [m for m in self.messages[since:] if m.topic == topic]

    def latest(self, topic: str) -> Any:
        """The last payload on the topic, as JSON; None if none came."""
        found = self.on(topic)
        return found[-1].json() if found else None

    async def settled(
        self, topic: str, check: Callable[[Any], bool], *, timeout_s: float = WAIT_S
    ) -> Any:
        """The last payload on the topic, as JSON, once `check` accepts it: a document that did
        not change is not published again."""
        deadline = time.monotonic() + timeout_s
        while not check(found := self.latest(topic)):
            if time.monotonic() > deadline:
                raise AssertionError(f"{topic} is not as expected after {timeout_s:g} s: {found}")
            await asyncio.sleep(0.05)
        return found

    async def wait(
        self,
        topic: str,
        check: Callable[[Any], bool] = lambda _: True,
        *,
        since: int = 0,
        timeout_s: float = WAIT_S,
    ) -> Published:
        """The first message on the topic after `since` whose JSON payload `check` accepts."""
        deadline = time.monotonic() + timeout_s
        while True:
            for message in self.on(topic, since):
                if check(message.json()):
                    return message
            if time.monotonic() > deadline:
                payloads = [m.payload.decode(errors="replace") for m in self.on(topic, since)]
                raise AssertionError(f"nothing fitting on {topic} in {timeout_s:g} s: {payloads}")
            await asyncio.sleep(0.05)


@dataclass
class Service:
    bridge: Bridge
    stop: asyncio.Event
    task: asyncio.Task[int]


@contextlib.asynccontextmanager
async def running(text: str, state_dir: Path) -> AsyncIterator[Service]:
    """The service with this configuration until the end of the block, then stopped as SIGTERM
    stops it (the stop event of `Bridge.run`)."""
    config = parse_config(text)
    started: list[tuple[Bridge, asyncio.Event]] = []
    run = Bridge.run

    async def keep(bridge: Bridge, stop: asyncio.Event) -> None:
        started.append((bridge, stop))
        await run(bridge, stop)

    with mock.patch.object(Bridge, "run", keep):
        task = asyncio.create_task(cli._serve(config, state_dir), name="gateway")
        deadline = time.monotonic() + START_S
        while not started:
            if task.done() or time.monotonic() > deadline:
                task.cancel()
                raise AssertionError(f"the service did not start: {task}")
            await asyncio.sleep(0.01)
        bridge, stop = started[0]
        try:
            yield Service(bridge, stop, task)
        finally:
            stop.set()
            async with asyncio.timeout(STOP_S):
                await task


def log_mark(caplog: pytest.LogCaptureFixture) -> int:
    return len(caplog.records)


def log_lines(caplog: pytest.LogCaptureFixture, text: str, since: int = 0) -> list[str]:
    """The service's log messages after `since` that contain `text`."""
    return [m for r in caplog.records[since:] if text in (m := r.getMessage())]


async def logged(
    caplog: pytest.LogCaptureFixture, text: str, *, since: int = 0, timeout_s: float = WAIT_S
) -> str:
    """The first log message after `since` that contains `text`."""
    deadline = time.monotonic() + timeout_s
    while not (found := log_lines(caplog, text, since)):
        if time.monotonic() > deadline:
            raise AssertionError(f"no log line with {text!r} within {timeout_s:g} s")
        await asyncio.sleep(0.05)
    return found[0]
