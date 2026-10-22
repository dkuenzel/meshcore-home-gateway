"""Command line: version, configuration, probe over TCP, send and listen through a broker."""

from __future__ import annotations

import contextlib
import json
import socket
import stat
import threading
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway import __version__
from meshcore_home_gateway import cli as cli_module
from meshcore_home_gateway.cli import EX_CONFIG, EX_FAILURE, EX_OK, main
from meshcore_home_gateway.protocol.codes import Command
from tests.conftest import Broker
from tests.radio_emulator import EmulatedContact, EmulatedRadio, TcpRadioServer

EXAMPLE = Path(__file__).resolve().parent.parent / "deploy" / "config.example.toml"
ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice\tSmith")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"meshcore-home-gateway {__version__}"


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
        out = capsys.readouterr().out
        assert "\x1b" not in out  # escaped in the JSON text
        assert "\u2028" not in out
        report = json.loads(out)
        assert report["channels"][1]["name"] == "Alerts\x1b[2J\u2028"
        assert (report["name"], report["firmware"], report["spreading_factor"]) == (
            "Bridge",
            "v1.18.0",
            8,
        )
        assert main(["probe", "--tcp", address]) == EX_OK
        report = capsys.readouterr().out
        assert "Alerts" in report
        assert "\x1b" not in report  # names cannot reach the terminal as escape sequences
    assert radio.clock == 1_800_000_000  # probing never sets the radio clock


def test_probe_leaves_waiting_messages_in_the_radio(capsys: pytest.CaptureFixture[str]) -> None:
    """The service fetches them later; a probe while it is stopped must not swallow them."""
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "arm", 1_800_000_000, push=False)
    with TcpRadioServer(radio) as server:
        assert main(["probe", "--tcp", f"127.0.0.1:{server.port}", "--json"]) == EX_OK
    report = json.loads(capsys.readouterr().out)
    assert len(radio.queue) == 1
    assert report["auto_add"] == {"manual": True, "types": [], "overwrite_oldest": False}


@pytest.mark.parametrize(
    ("radio", "auto_add", "line"),
    [
        (
            EmulatedRadio(autoadd_config=0x07),  # overwrite, chat, repeater
            {"manual": True, "types": ["chat", "repeater"], "overwrite_oldest": True},
            "added manually, but chat, repeater automatically; a full table overwrites the oldest",
        ),
        (
            EmulatedRadio(autoadd_config=0x41, autoadd_hops=None),  # firmware 1.12: no hop limit
            {"manual": True, "types": ["0x40"], "overwrite_oldest": True},
            "added manually, but 0x40 automatically; a full table overwrites the oldest",
        ),
        (
            EmulatedRadio(manual_add=False, unknown={Command.GET_AUTOADD_CONFIG}),
            {"manual": False, "types": ["chat", "repeater", "room", "sensor"],
             "overwrite_oldest": None},
            "all added automatically",
        ),
    ],
    ids=["manual-with-types", "unknown-type", "older-firmware"],
)  # fmt: skip
def test_probe_reports_how_the_radio_adds_contacts(
    capsys: pytest.CaptureFixture[str], radio: EmulatedRadio, auto_add: dict[str, Any], line: str
) -> None:
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        assert main(["probe", "--tcp", address, "--json"]) == EX_OK
        assert json.loads(capsys.readouterr().out)["auto_add"] == auto_add
        assert main(["probe", "--tcp", address]) == EX_OK
        assert f"contacts   {line}\n" in capsys.readouterr().out


def test_probe_failure_exits_with_failure(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["probe", "--serial", "/dev/serial/by-id/does-not-exist"]) == EX_FAILURE
    assert "probe failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "address"),
    [
        ("radio.example.org", ("radio.example.org", 5000)),
        ("192.0.2.10:5001", ("192.0.2.10", 5001)),
        ("[2001:db8::10]:5001", ("2001:db8::10", 5001)),
        ("[2001:db8::10]", ("2001:db8::10", 5000)),
        ("2001:db8::10", ("2001:db8::10", 5000)),  # a bare IPv6 address has no port
    ],
)
def test_probe_tcp_addresses(text: str, address: tuple[str, int]) -> None:
    assert cli_module.tcp_address(text) == address


@pytest.mark.parametrize(
    "text", ["radio:abc", "radio:0", "radio:70000", ":5000", "[2001:db8::10", "[2001:db8::10]x"]
)
def test_probe_refuses_malformed_tcp_addresses(
    text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["probe", "--tcp", text])
    assert exit_info.value.code == 2  # a usage error from argparse, not a traceback
    assert "argument --tcp" in capsys.readouterr().err


def test_listen_refuses_a_negative_count(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["listen", "--count", "-1"])
    assert exit_info.value.code == 2
    assert "argument --count" in capsys.readouterr().err


def test_config_render_reports_an_unknown_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = tmp_path / "answers"
    answers.write_text(
        "radio.transport=tcp\nradio.host=192.0.2.10\nmqtt.host=broker.example.org\n"
        "mqtt.node_id=radio1\n",
        encoding="utf-8",
    )
    output = tmp_path / "config.toml"
    command = ["config", "render", "--answers", str(answers), "--output", str(output)]
    assert main([*command, "--group", "no-such-group-here"]) == EX_FAILURE
    assert "no group 'no-such-group-here'" in capsys.readouterr().err
    assert not output.exists()


def tls_config(tmp_path: Path, files: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\n'
        f'[mqtt]\nhost = "127.0.0.1"\nnode_id = "radio1"\ntls = true\n{files}\n'
        "[targets.alerts]\nchannel = 0\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "command",
    [["check-config"], ["send", "alerts", "x"], ["listen", "--count", "1"], ["run"]],
    ids=lambda c: c[0],
)
def test_unreadable_tls_files_are_configuration_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    """Exit code 78, so systemd does not restart the service in a loop over a missing file."""
    monkeypatch.setattr(cli_module, "setup_logging", lambda _level: None)
    not_a_certificate = tmp_path / "ca.pem"
    not_a_certificate.write_text("not a certificate\n", encoding="utf-8")
    for files, key in [
        (f'ca_file = "{tmp_path / "missing.pem"}"', "mqtt.ca_file"),
        (f'ca_file = "{not_a_certificate}"', "mqtt.ca_file"),
        (f'cert_file = "{tmp_path / "client.pem"}"', "mqtt.cert_file"),
    ]:
        config = tls_config(tmp_path, files)
        extra = ["--state-dir", str(tmp_path / "state")] if command == ["run"] else []
        assert main([command[0], "--config", str(config), *extra, *command[1:]]) == EX_CONFIG
        error = capsys.readouterr().err
        assert f"configuration error: {key}: cannot load" in error
        assert "Traceback" not in error


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
    retained = {"radio": "connected", "radio_name": "Gate\u009b2J \u202eevil\u2028\U000e0001 Grüße"}
    publisher.publish(
        "meshcore/radio1/state", json.dumps(retained), qos=1, retain=True
    ).wait_for_publish(5)
    try:
        assert main(["listen", "--config", config, "--topic", "state", "--count", "1"]) == EX_OK
    finally:
        publisher.loop_stop()
        publisher.disconnect()
    out = capsys.readouterr().out
    for escaped in ("\\u009b", "\\u202e", "\\u2028", "\\udb40\\udc01"):  # not raw
        assert escaped in out
    assert "Grüße" in out  # ordinary letters stay readable
    line: dict[str, Any] = json.loads(out.strip())
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


class ScriptedBroker:
    """A minimal MQTT 3.1.1 broker on a local port that answers as told: accept the connection,
    acknowledge publications or not, grant or refuse subscriptions."""

    def __init__(self, *, ack_publish: bool = True, grant_subscribe: bool = True) -> None:
        self._ack, self._grant = ack_publish, grant_subscribe
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        with contextlib.suppress(OSError):
            connection, _ = self._server.accept()
            with connection:
                while (packet := _read_packet(connection)) is not None:
                    kind, body = packet
                    if kind == 0x10:  # CONNECT
                        connection.sendall(bytes([0x20, 2, 0, 0]))
                    elif kind == 0x82:  # SUBSCRIBE: packet id, then the filters
                        code = 0x01 if self._grant else 0x80
                        connection.sendall(bytes([0x90, 3]) + body[:2] + bytes([code]))
                    elif kind & 0xF0 == 0x30 and self._ack:  # PUBLISH with QoS 1
                        topic_length = int.from_bytes(body[:2], "big")
                        packet_id = body[2 + topic_length : 4 + topic_length]
                        connection.sendall(bytes([0x40, 2]) + packet_id)
                    elif kind == 0xE0:  # DISCONNECT
                        return

    def close(self) -> None:
        self._server.close()

    def config(self, tmp_path: Path) -> str:
        path = tmp_path / "config.toml"
        path.write_text(
            '[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\n[mqtt]\nhost = "127.0.0.1"\n'
            f'port = {self.port}\nnode_id = "radio1"\n[targets.alerts]\nchannel = 0\n',
            encoding="utf-8",
        )
        return str(path)


def _read_packet(connection: socket.socket) -> tuple[int, bytes] | None:
    head = connection.recv(1)
    if not head:
        return None
    length, shift = 0, 0
    while True:
        byte = connection.recv(1)[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    body = b""
    while len(body) < length:
        chunk = connection.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return head[0], body


def test_send_fails_when_the_broker_does_not_confirm_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "PUBLISH_TIMEOUT_S", 0.5)
    broker = ScriptedBroker(ack_publish=False)
    try:
        assert main(["send", "--config", broker.config(tmp_path), "alerts", "x"]) == EX_FAILURE
    finally:
        broker.close()
    assert "did not confirm" in capsys.readouterr().err


def test_send_wait_fails_when_the_broker_refuses_the_subscription(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = ScriptedBroker(grant_subscribe=False)
    try:
        config = broker.config(tmp_path)
        assert main(["send", "--config", config, "--wait", "5", "alerts", "x"]) == EX_FAILURE
    finally:
        broker.close()
    assert "refused the subscription" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["send", "alerts", "x"], ["listen", "--count", "1"]])
def test_unreachable_broker_is_reported_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()  # nothing listens there now
    config = tmp_path / "config.toml"
    config.write_text(
        f'[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\n[mqtt]\nhost = "127.0.0.1"\n'
        f'port = {port}\nnode_id = "radio1"\n[targets.alerts]\nchannel = 0\n',
        encoding="utf-8",
    )
    assert main([command[0], "--config", str(config), *command[1:]]) == EX_FAILURE
    error = capsys.readouterr().err
    assert "cannot connect to the broker" in error
    assert "Traceback" not in error
