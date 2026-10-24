"""The whole system as processes: `meshcore-home-gateway run` against a broker and a radio over TCP.

This is the closest the automated tests get to an installation: a real mosquitto, the service
started as a subprocess like systemd would start it, the CLI as a client, SIGTERM to stop.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway.cli import EX_OK, main
from meshcore_home_gateway.history import History
from tests.conftest import Broker
from tests.radio_emulator import EmulatedContact, EmulatedRadio, TcpRadioServer

ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
SRC = Path(__file__).resolve().parent.parent / "src"


class Watcher:
    def __init__(self, broker: Broker) -> None:
        self.messages: list[tuple[str, bytes, bool]] = []
        self._lock = threading.Lock()
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.client.on_message = self._on_message
        self.client.connect(broker.host, broker.port)
        self.client.subscribe([("meshcore/#", 1), ("homeassistant/#", 1)])
        self.client.loop_start()

    def _on_message(self, _c: Any, _u: Any, m: Any) -> None:
        with self._lock:
            self.messages.append((m.topic, bytes(m.payload), bool(m.retain)))

    def wait(self, topic: str, check: Any = lambda p: True, timeout: float = 10) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for t, p, _ in self.messages:
                    if t == topic and check(p):
                        return p
            time.sleep(0.05)
        raise AssertionError(f"nothing on {topic}; got {[t for t, _, _ in self.messages]}")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.mark.broker
def test_service_end_to_end(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    behind = int(time.time()) - 30
    radio = EmulatedRadio(contacts=[ALICE], clock=behind)
    with TcpRadioServer(radio) as server:
        config = tmp_path / "config.toml"
        config.write_text(
            f'[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\nport = {server.port}\n'
            f'[mqtt]\nhost = "{broker.host}"\nport = {broker.port}\nnode_id = "radio1"\n'
            f'[targets.alerts]\nchannel = 0\n[targets.alice]\ncontact = "{"a1" * 32}"\n'
            f'[commands]\nsenders = ["{"a1" * 32}"]\nwords = ["status"]\n',
            encoding="utf-8",
        )
        watcher = Watcher(broker)
        service = subprocess.Popen(  # noqa: S603 - our own module with fixed arguments
            [sys.executable, "-m", "meshcore_home_gateway", "run", "--config", str(config),
             "--state-dir", str(tmp_path / "state")],
            stderr=subprocess.PIPE,
            env={**os.environ, "JOURNAL_STREAM": "1:1", "PYTHONPATH": str(SRC)},
        )  # fmt: skip
        try:
            watcher.wait("meshcore/radio1/availability", lambda p: p == b"online")
            watcher.wait("meshcore/radio1/state", lambda p: json.loads(p)["radio"] == "connected")
            watcher.wait("homeassistant/device/meshcore_home_gateway_radio1/config")
            index = json.loads(watcher.wait("meshcore/radio1/contacts"))  # retained documents
            assert index["contacts"][0]["key"] == "a1" * 32

            assert (
                main(["send", "--config", str(config), "--wait", "10", "alerts", "Gate open"])
                == EX_OK
            )
            assert (
                main(["send", "--config", str(config), "--wait", "10", "alice", "Hello"]) == EX_OK
            )
            confirmed = watcher.wait(
                "meshcore/radio1/delivery", lambda p: json.loads(p)["event_type"] == "confirmed"
            )
            assert json.loads(confirmed)["target"] == "alice"
            assert '"event_type": "confirmed"' in capsys.readouterr().out

            now = int(time.time())
            server.call(
                lambda: radio.queue_direct_message(ALICE.public_key[:6], "status please", now)
            )
            command = json.loads(watcher.wait("meshcore/radio1/command"))
            assert (command["event_type"], command["args"]) == ("status", "please")
        finally:  # a failure above stays the reported one; a hanging service must not outlive it
            service.send_signal(signal.SIGTERM)
            try:
                _, errors = service.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                service.kill()
                _, errors = service.communicate()
                errors += b"\nkilled: no exit within 15 s of SIGTERM"
            finally:
                watcher.close()
        assert service.returncode == 0, errors.decode()
        assert b"Traceback" not in errors
    history = History.open(tmp_path / "state" / "history.sqlite3")
    assert history is not None
    assert {m.text for m in history.recent_messages(5)} == {"Gate open", "Hello", "status please"}
    history.close()
    assert radio.channel_texts[0][1] == "Gate open"
    assert [t.text for t in radio.texts] == ["Hello"]
    assert radio.clock > behind + 20  # the service moved the radio clock forward
    late = Watcher(broker)
    try:
        assert late.wait("meshcore/radio1/availability") == b"offline"
    finally:
        late.close()
