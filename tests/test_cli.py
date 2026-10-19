"""Command line: version, configuration, probe over TCP, send and listen through a broker."""

from __future__ import annotations

import json
import socket
import stat
import threading
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_bus import __version__
from meshcore_bus import cli as cli_module
from meshcore_bus.cli import EX_CONFIG, EX_FAILURE, EX_OK, main
from tests.conftest import Broker
from tests.radio_emulator import EmulatedContact, EmulatedRadio, TcpRadioServer

EXAMPLE = Path(__file__).resolve().parent.parent / "deploy" / "config.example.toml"
ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice\tSmith")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"meshcore-bus {__version__}"


def test_check_config_summarises_a_valid_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--config", str(EXAMPLE)]) == EX_OK
    out = capsys.readouterr().out
    assert "valid" in out
    assert "target     alerts: channel 1" in out
    assert "broker     broker.example.org:1883" in out


def test_check_config_reports_errors_with_ex_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[radio]\ntransport = "serial"\n', encoding="utf-8")
    assert main(["check-config", "--config", str(bad)]) == EX_CONFIG
    assert "radio.device" in capsys.readouterr().err
    assert main(["check-config", "--config", str(tmp_path / "missing.toml")]) == EX_CONFIG


def test_config_render_writes_a_private_valid_file(tmp_path: Path) -> None:
    answers = tmp_path / "answers"
    answers.write_text(
        "radio.transport=tcp\nradio.host=192.0.2.10\nmqtt.host=broker.example.org\n"
        "mqtt.node_id=radio1\nmqtt.password=secret\ntarget.alerts=channel:1\n",
        encoding="utf-8",
    )
    output = tmp_path / "etc" / "config.toml"
    assert (
        main(
            [
                "config",
                "render",
                "--answers",
                str(answers),
                "--output",
                str(output),
                "--group",
                "root",
            ]
        )
        == EX_OK
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
    assert main(["check-config", "--config", str(output)]) == EX_OK
    answers.write_text("mqtt.colour=blue\n", encoding="utf-8")
    assert (
        main(["config", "render", "--answers", str(answers), "--output", str(output)]) == EX_CONFIG
    )


def test_config_answers_prints_the_settings_as_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\n\n'
        '[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\n',
        encoding="utf-8",
    )
    assert main(["config", "answers", "--config", str(config)]) == EX_OK
    assert capsys.readouterr().out == (
        "radio.transport=tcp\nradio.host=192.0.2.10\nmqtt.host=broker.example.org\n"
        "mqtt.node_id=radio1\n"
    )
    assert main(["config", "answers", "--config", str(tmp_path / "missing.toml")]) == EX_CONFIG


def test_probe_lists_channels_and_contacts_over_tcp(capsys: pytest.CaptureFixture[str]) -> None:
    radio = EmulatedRadio(contacts=[ALICE], channels={0: "Public", 3: "Alerts\x1b[2J\u2028"})
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        assert main(["probe", "--tcp", address, "--list", "channels"]) == EX_OK
        assert capsys.readouterr().out == "0\tPublic\n3\tAlerts [2J \n"  # no terminal escapes
        assert main(["probe", "--tcp", address, "--list", "contacts"]) == EX_OK
        assert capsys.readouterr().out == f"{'a1' * 32}\tAlice Smith\tchat\n"  # tab made safe
        assert main(["probe", "--tcp", address, "--json"]) == EX_OK
        report = json.loads(capsys.readouterr().out)
        assert (report["name"], report["firmware"], report["spreading_factor"]) == (
            "Bridge",
            "v1.18.0",
            8,
        )
        assert main(["probe", "--tcp", address]) == EX_OK
        assert "Alerts" in capsys.readouterr().out
    assert radio.clock == 1_800_000_000  # probing never sets the radio clock


def test_probe_failure_exits_with_failure(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["probe", "--serial", "/dev/serial/by-id/does-not-exist"]) == EX_FAILURE
    assert "probe failed" in capsys.readouterr().err


def broker_config(tmp_path: Path, broker: Broker) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\n'
        f'[mqtt]\nhost = "{broker.host}"\nport = {broker.port}\nnode_id = "radio1"\n'
        "[targets.alerts]\nchannel = 0\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.broker
def test_send_publishes_a_request(tmp_path: Path, broker: Broker) -> None:
    seen: list[tuple[str, bytes]] = []
    subscribed = threading.Event()
    observer = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    observer.on_subscribe = lambda *_: subscribed.set()
    observer.on_message = lambda _c, _u, m: seen.append((m.topic, bytes(m.payload)))
    observer.connect(broker.host, broker.port)
    observer.subscribe("meshcore/#", qos=1)
    observer.loop_start()
    try:
        assert subscribed.wait(5)
        assert (
            main(["send", "--config", str(broker_config(tmp_path, broker)), "alerts", "Täst"])
            == EX_OK
        )
        for _ in range(100):
            if seen:
                break
            threading.Event().wait(0.05)
        assert seen == [("meshcore/radio1/send/alerts", "Täst".encode())]
    finally:
        observer.loop_stop()
        observer.disconnect()


@pytest.mark.broker
def test_send_wait_without_bridge_times_out(tmp_path: Path, broker: Broker) -> None:
    config = str(broker_config(tmp_path, broker))
    assert main(["send", "--config", config, "--wait", "0.5", "alerts", "x"]) == EX_FAILURE


@pytest.mark.broker
def test_listen_prints_events_as_json_lines(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    config = str(broker_config(tmp_path, broker))
    publisher = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect(broker.host, broker.port)
    publisher.loop_start()
    retained = {"radio": "connected"}
    publisher.publish(
        "meshcore/radio1/state", json.dumps(retained), qos=1, retain=True
    ).wait_for_publish(5)
    try:
        assert main(["listen", "--config", config, "--topic", "state", "--count", "1"]) == EX_OK
    finally:
        publisher.loop_stop()
        publisher.disconnect()
    line: dict[str, Any] = json.loads(capsys.readouterr().out.strip())
    assert line == {"topic": "meshcore/radio1/state", "payload": retained}


@pytest.mark.broker
@pytest.mark.parametrize("command", [["send", "alerts", "x"], ["listen", "--count", "1"]])
def test_refused_broker_connection_is_an_error(
    tmp_path: Path, refusing_broker: Broker, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    config = str(broker_config(tmp_path, refusing_broker))
    assert main([command[0], "--config", config, *command[1:]]) == EX_FAILURE
    assert "refused" in capsys.readouterr().err


class FakeBridge:
    """Answers send requests like the bridge does, with events of other requests mixed in."""

    def __init__(self, broker: Broker, answer: Any) -> None:
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.ready = threading.Event()
        self.client.on_subscribe = lambda *_: self.ready.set()
        self.client.on_message = lambda _c, _u, m: answer(self, m.payload.decode())
        self.client.connect(broker.host, broker.port)
        self.client.subscribe("meshcore/radio1/send/+", qos=1)
        self.client.loop_start()
        assert self.ready.wait(5)

    def event(self, **fields: Any) -> None:
        body = {"target": "alerts", "attempt": 1, "reason": None, **fields}
        self.client.publish("meshcore/radio1/delivery", json.dumps(body), qos=1)

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.mark.broker
def test_send_wait_reports_the_result_of_its_own_request(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    def answer(bridge: FakeBridge, text: str) -> None:
        bridge.event(event_type="failed", id="other1", text="somebody else's")
        bridge.event(event_type="queued", id="mine", text=text[:5] + "...")  # shortened
        bridge.event(event_type="sent", id="other2", text="another one")
        bridge.event(event_type="sent", id="mine", text=text[:5] + "...")

    fake = FakeBridge(broker, answer)
    try:
        config = str(broker_config(tmp_path, broker))
        code = main(["send", "--config", config, "--wait", "5", "alerts", "Hello world"])
    finally:
        fake.stop()
    assert code == EX_OK
    assert json.loads(capsys.readouterr().out)["id"] == "mine"


@pytest.mark.broker
def test_send_wait_reports_a_rejection(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeBridge(broker, lambda b, _t: b.event(event_type="rejected", id="r1", text=None))
    try:
        config = str(broker_config(tmp_path, broker))
        code = main(["send", "--config", config, "--wait", "5", "alerts", "x"])
    finally:
        fake.stop()
    assert code == EX_FAILURE
    assert json.loads(capsys.readouterr().out)["event_type"] == "rejected"


def test_send_fails_when_the_broker_does_not_confirm_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broker that accepts the connection but never acknowledges the publication."""
    monkeypatch.setattr(cli_module, "PUBLISH_TIMEOUT_S", 0.5)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def silent_broker() -> None:
        connection, _ = server.accept()
        with connection:
            connection.recv(1024)  # CONNECT
            connection.sendall(bytes([0x20, 0x02, 0x00, 0x00]))  # CONNACK: accepted
            while connection.recv(1024):  # PUBLISH, never answered
                pass

    thread = threading.Thread(target=silent_broker, daemon=True)
    thread.start()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\n[mqtt]\nhost = "127.0.0.1"\n'
        f'port = {port}\nnode_id = "radio1"\n[targets.alerts]\nchannel = 0\n',
        encoding="utf-8",
    )
    try:
        assert main(["send", "--config", str(config), "alerts", "x"]) == EX_FAILURE
    finally:
        server.close()
    assert "did not confirm" in capsys.readouterr().err
