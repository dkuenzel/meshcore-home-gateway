"""Command line: version, configuration, probe over TCP, send and listen through a broker."""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway import __version__
from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import cli as cli_module
from meshcore_home_gateway.cli import EX_CONFIG, EX_FAILURE, EX_OK, main
from meshcore_home_gateway.config import MqttConfig, load_config, parse_config
from meshcore_home_gateway.core.mesh import AdvertCheck
from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.mqtt import client as client_module
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Command
from meshcore_home_gateway.radio.session import ContactsRead
from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport
from meshcore_home_gateway.state import EXCLUSIONS_FILE, JsonStateFile
from tests.conftest import Broker
from tests.radio_emulator import SELF_KEY, EmulatedContact, EmulatedRadio, TcpRadioServer
from tests.test_mqtt_client import FakePaho

EXAMPLE = Path(__file__).resolve().parent.parent / "deploy" / "config.example.toml"
ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice\tSmith")


@pytest.fixture(autouse=True)
def _no_service_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`probe` reads the service's state: not the one of this machine."""
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "service-state"))


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
    assert "  telemetry  asked of every companion not excluded\n" in out


def test_check_config_says_when_telemetry_is_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    off = tmp_path / "config.toml"
    text = EXAMPLE.read_text(encoding="utf-8").replace("# enabled = true", "enabled = false")
    off.write_text(text, encoding="utf-8")
    assert main(["check-config", "--config", str(off)]) == EX_OK
    assert "  telemetry  off\n" in capsys.readouterr().out


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
        "mqtt.node_id=radio1\nmqtt.username=bridge\nmqtt.password=secret\n"
        "target.alerts=channel:1\n",
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
                grp.getgrgid(os.getgid()).gr_name,  # a group the test user may set
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


RENDER_ANSWERS = (
    "radio.transport=tcp\nradio.host=192.0.2.10\nmqtt.host=broker.example.org\n"
    "mqtt.node_id=radio1\n"
)


@pytest.mark.parametrize(
    "where", ["file/config.toml", "directory"], ids=["below-a-file", "onto-a-directory"]
)
def test_config_render_reports_an_output_it_cannot_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    answers = tmp_path / "answers"
    answers.write_text(RENDER_ANSWERS, encoding="utf-8")
    (tmp_path / "file").write_text("x\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    output = tmp_path / where
    command = ["config", "render", "--answers", str(answers), "--output", str(output)]
    assert main(command) == EX_FAILURE
    error = capsys.readouterr().err
    assert f"cannot write {output}" in error
    assert "Traceback" not in error
    assert sorted(p.name for p in tmp_path.iterdir()) == ["answers", "directory", "file"]


def test_config_render_syncs_the_file_and_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash right after the first installation must not leave an empty or no configuration."""
    output = tmp_path / "config.toml"
    synced: list[tuple[str, bool]] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        synced.append((kind, output.exists()))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    cli_module.write_private(output, "x = 1\n", None)
    assert synced == [("file", False), ("directory", True)]  # before and after the rename
    assert output.read_text(encoding="utf-8") == "x = 1\n"


def test_config_render_makes_its_directory_reachable_whatever_the_umask(tmp_path: Path) -> None:
    """The service user must reach the file; root's umask may be 077 (the installer had one)."""
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o700)
    previous = os.umask(0o077)
    try:
        cli_module.write_private(tmp_path / "etc" / "gw" / "config.toml", "x = 1\n", None)
        cli_module.write_private(existing / "config.toml", "x = 1\n", None)
    finally:
        os.umask(previous)
    for directory in (tmp_path / "etc", tmp_path / "etc" / "gw"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "etc" / "gw" / "config.toml").stat().st_mode) == 0o640
    assert stat.S_IMODE(existing.stat().st_mode) == 0o700  # as the administrator set it


def test_config_render_loads_the_tls_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Like check-config and the service: the installer learns of a missing file at once."""
    answers = tmp_path / "answers"
    answers.write_text(
        f"{RENDER_ANSWERS}mqtt.tls=true\nmqtt.ca_file={tmp_path / 'missing.pem'}\n",
        encoding="utf-8",
    )
    output = tmp_path / "config.toml"
    assert (
        main(["config", "render", "--answers", str(answers), "--output", str(output)]) == EX_CONFIG
    )
    assert "configuration error: mqtt.ca_file: cannot load" in capsys.readouterr().err
    assert not output.exists()


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
    behind = int(time.time()) - 3600  # the service would move this clock forward
    radio = EmulatedRadio(
        contacts=[ALICE], channels={0: "Public", 3: "Alerts\x1b[2J\u2028"}, clock=behind
    )
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
        assert report["position"] is None
        assert report["advert_location"] == "none"
        assert report["telemetry"] == {"base": "deny", "location": "deny", "environment": "deny"}
        assert (report["multi_acks"], report["airtime_factor"]) == (0, 1.0)
        assert report["contacts"][0]["flags"] == []
        assert main(["probe", "--tcp", address]) == EX_OK
        report = capsys.readouterr().out
        assert "Alerts" in report
        assert "\x1b" not in report  # names cannot reach the terminal as escape sequences
    assert radio.clock == behind  # probing never sets the radio clock
    assert Command.SET_DEVICE_TIME not in radio.commands()


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


def test_probe_reports_position_telemetry_airtime_and_contact_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What the managed profile looks at (SELF_INFO bytes 36-47, GET_TUNING_PARAMS, contact
    flags): a contact with the remote CLI flag can run the radio's CLI over the air."""
    admin = EmulatedContact(bytes.fromhex("c3" * 32), "Admin", flags=0x10 | 0x01 | 0x40)
    radio = EmulatedRadio(
        contacts=[admin], latitude_e6=53_550_000, longitude_e6=9_990_000, advert_location=1,
        telemetry_modes=0x26, tuning=(0, 9000),
    )  # fmt: skip
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        assert main(["probe", "--tcp", address, "--json"]) == EX_OK
        report = json.loads(capsys.readouterr().out)
        assert report["position"] == {"latitude": 53.55, "longitude": 9.99}
        assert report["advert_location"] == "share"
        assert report["telemetry"] == {
            "base": "allow_all", "location": "by_contact", "environment": "allow_all",
        }  # fmt: skip
        assert report["airtime_factor"] == 9.0
        assert report["contacts"][0]["flags"] == ["favourite", "remote_cli", "0x40"]
        assert main(["probe", "--tcp", address]) == EX_OK
        out = capsys.readouterr().out
        assert "telemetry  base allow_all, location by_contact, environment allow_all\n" in out
        assert "position   53.55, 9.99, shared in adverts\n" in out
        assert "airtime    factor 9: the radio sends at most 10 %\n" in out
        assert f"  {'c3' * 32}  chat     Admin  REMOTE CLI\n" in out
    radio = EmulatedRadio(tuning=None)
    with TcpRadioServer(radio) as server:
        assert main(["probe", "--tcp", f"127.0.0.1:{server.port}", "--json"]) == EX_OK
        assert json.loads(capsys.readouterr().out)["airtime_factor"] is None


def test_probe_shows_what_the_profile_changes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """ADR 0020: the installer shows this before the service writes anything."""
    radio = EmulatedRadio(manual_add=False, telemetry_modes=0x05, tuning=(0, 1000))
    state = ["--state-dir", str(tmp_path)]  # nothing stored: the first connect of this radio
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        assert main(["probe", "--tcp", address, "--json", *state]) == EX_OK
        profile = json.loads(capsys.readouterr().out)["profile"]
        initial = {"action": "set", "reason": "initial"}
        assert profile == {
            "manage": "auto",
            "configured": False,
            "firmware_tested": True,
            "writes": True,
            "changes": {
                "manual_add": {"live": False, "profile": True, **initial},
                "telemetry": {
                    "live": {"base": "contacts", "location": "contacts", "environment": "deny"},
                    "profile": {"base": "deny", "location": "deny", "environment": "deny"},
                    **initial,
                },
                "duty_cycle_percent": {"live": 50.0, "profile": 10.0, **initial},
            },
            "contacts": {},  # no configuration: no contact targets, no command senders
        }
        assert main(["probe", "--tcp", address, *state]) == EX_OK
        out = capsys.readouterr().out
        assert "profile    differs from the default observer profile:\n" in out
        assert "  manual_add: false -> true  (set at the next connect)\n" in out
        assert radio.commands().count(38) == 0  # the probe writes nothing
        assert main(["probe", "--tcp", address, "--list", "profile", *state]) == EX_OK
        assert capsys.readouterr().out.split("\n")[:5] == [
            "name\tBridge",
            "tested\tyes",
            "writes\tyes",
            "change\tmanual_add\tfalse\ttrue\tset",
            (
                'change\ttelemetry\t{"base": "contacts", "location": "contacts", "environment": '
                '"deny"}\t{"base": "deny", "location": "deny", "environment": "deny"}\tset'
            ),
        ]
    with TcpRadioServer(EmulatedRadio(tuning=(0, 9000), firmware_code=15)) as server:
        assert main(["probe", "--tcp", f"127.0.0.1:{server.port}", *state]) == EX_OK
        assert "profile    the radio matches the default observer profile\n" in (
            capsys.readouterr().out
        )


def test_probe_follows_what_the_service_stored(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The service's own reconcile: a name renamed on the radio is kept, a setting that did not
    take effect is given up, one the firmware refused is left; the state file stays as it is."""
    radio = EmulatedRadio(manual_add=False, tuning=(0, 1000), name="Renamed")
    config = tmp_path / "config.toml"
    config.write_text(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[radio.profile]\nname = "Gateway"\n'
        '[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    stored = {
        "values": {"name": "Gateway", "telemetry": 0, "autoadd": 0, "advert_location": 0},
        "attempts": {"manual_add": {"value": True, "connects": 3, "firmware": 14}},
        "unsupported": {"airtime_factor_milli": 14},
    }
    state = tmp_path / "state" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"radio_profile": {SELF_KEY.hex(): stored}}))
    before = state.read_bytes()
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        args = ["--config", str(config), "--state-dir", str(state.parent)]
        assert main(["probe", "--tcp", address, *args, "--json"]) == EX_OK
        changes = json.loads(capsys.readouterr().out)["profile"]["changes"]
        assert {name: change["action"] for name, change in changes.items()} == {
            "manual_add": "given_up", "duty_cycle_percent": "unsupported", "name": "kept",
        }  # fmt: skip
        assert main(["probe", "--tcp", address, *args]) == EX_OK
        out = capsys.readouterr().out
        assert "profile    differs from the configured profile:\n" in out
        assert '  name: "Renamed" -> "Gateway"  (changed on the radio; kept)\n' in out
        given_up = "(did not take effect at 3 connects; no longer set)"
        assert f"  manual_add: false -> true  {given_up}\n" in out
        assert "  duty_cycle_percent: 50.0 -> 10.0  (the firmware does not support it)\n" in out
    assert state.read_bytes() == before


def test_probe_leaves_an_unreadable_state_file_alone(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "state.json").write_text("{not json")
    with TcpRadioServer(EmulatedRadio(manual_add=False)) as server:
        address = f"127.0.0.1:{server.port}"
        assert main(["probe", "--tcp", address, "--state-dir", str(tmp_path), "--json"]) == EX_OK
        changes = json.loads(capsys.readouterr().out)["profile"]["changes"]
        assert changes["manual_add"]["reason"] == "initial"
    assert (tmp_path / "state.json").read_text() == "{not json"
    assert not (tmp_path / "state.json.corrupt").exists()


def test_probe_refuses_a_bad_configuration(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[radio.profile]\nmanage = "sometimes"\n')
    assert main(["probe", "--tcp", "127.0.0.1:1", "--config", str(config)]) == EX_CONFIG
    assert "configuration error" in capsys.readouterr().err


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


def test_the_service_opens_the_configured_radio() -> None:
    serial = cli_module.transport_for(load_config(EXAMPLE))
    assert isinstance(serial, SerialTransport)
    assert serial.device == "/dev/serial/by-id/usb-Example_Radio_0001-if00"
    tcp = cli_module.transport_for(
        parse_config(
            '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\nport = 5001\n'
            '[mqtt]\nhost = "h"\nnode_id = "n"\n'
        )
    )
    assert isinstance(tcp, TcpTransport)
    assert tcp.description == "tcp 192.0.2.10:5001"


def test_send_and_listen_log_in_with_the_configured_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePaho()
    monkeypatch.setattr(client_module, "make_paho_client", lambda _config: fake)
    with_login = MqttConfig(host="h", node_id="n", username="u", password="p")
    assert cli_module._mqtt_client(with_login, "send") is fake
    assert ("username_pw_set", ("u", "p")) in fake.calls
    fake.calls.clear()
    cli_module._mqtt_client(MqttConfig(host="h", node_id="n"), "listen")
    assert "username_pw_set" not in [name for name, _ in fake.calls]


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


def test_send_refuses_an_alias_that_is_not_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tls_config(tmp_path, "")  # never connects: the alias check comes first
    for alias in ["alrets", "+", "a/b"]:
        assert main(["send", "--config", str(config), alias, "x"]) == EX_FAILURE
        error = capsys.readouterr().err
        assert f"no target {alias!r}" in error
        assert "Traceback" not in error


@pytest.mark.parametrize("wait", ["-1", "inf", "nan", "1e9"])
def test_send_refuses_an_impossible_wait(wait: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["send", "--wait", wait, "alerts", "x"])
    assert exit_info.value.code == 2
    assert "argument --wait" in capsys.readouterr().err


@pytest.mark.broker
@pytest.mark.parametrize("topic", ["telemetry", "all"])
def test_listen_follows_telemetry_events(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str], topic: str
) -> None:
    publisher = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect(broker.host, broker.port)
    publisher.loop_start()
    event = {"event_type": "unanswered", "key": "a1" * 32, "prefix": "a1" * 6, "name": "Alice",
             "state": "away"}  # fmt: skip
    done = threading.Event()

    def publish() -> None:  # once the listener has subscribed: events are not retained
        while not done.wait(0.1):
            publisher.publish("meshcore/radio1/telemetry", json.dumps(event), qos=1)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        config = str(broker_config(tmp_path, broker))
        assert main(["listen", "--config", config, "--topic", topic, "--count", "1"]) == EX_OK
    finally:
        done.set()
        thread.join()
        publisher.loop_stop()
        publisher.disconnect()
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"topic": "meshcore/radio1/telemetry", "payload": event}


def test_the_service_keeps_exclusions_in_their_file_and_readings_their_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusions outlive a restart (their own file in the state directory), and readings
    go after `[history] readings_days`."""
    bridges: list[bridge_module.Bridge] = []
    retentions: list[Retention] = []

    async def stopped(bridge: bridge_module.Bridge, _stop: asyncio.Event) -> None:
        bridges.append(bridge)

    opened = History.open

    def open_history(path: Path, retention: Retention) -> History | None:
        retentions.append(retention)
        return opened(path, retention)

    monkeypatch.setattr(bridge_module.Bridge, "run", stopped)
    monkeypatch.setattr(History, "open", staticmethod(open_history))
    config = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        "[history]\nmessages_days = 7\npositions_days = 9\nreadings_days = 3.5\n"
    )
    assert asyncio.run(cli_module._serve(config, tmp_path)) == EX_OK
    assert retentions == [Retention(7.0, 9.0, readings_days=3.5)]
    (bridge,) = bridges
    assert bridge.observer.exclusions.exclude("a1" * 32)
    stored = json.loads((tmp_path / EXCLUSIONS_FILE).read_text(encoding="utf-8"))
    assert stored == {"excluded": ["a1" * 32]}


def test_the_service_keeps_the_documents_of_current_companions_in_its_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So that they expire after a restart without the radio (ADR 0024)."""
    bridges: list[bridge_module.Bridge] = []

    async def stopped(bridge: bridge_module.Bridge, _stop: asyncio.Event) -> None:
        bridges.append(bridge)

    monkeypatch.setattr(bridge_module.Bridge, "run", stopped)
    store = JsonStateFile(tmp_path / "state.json")
    document = {"key": "a1" * 32, "telemetry_state": "answering", "telemetry_current": True}
    store.save({"current_contacts": {"a1" * 32: document}})
    config = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        "[history]\nenabled = false\n[telemetry]\nenabled = false\n"
    )
    assert asyncio.run(cli_module._serve(config, tmp_path)) == EX_OK
    (bridge,) = bridges
    bridge.observer.publish_all()  # the broker connected: telemetry is off now
    assert store.load()["current_contacts"]["a1" * 32]["telemetry_state"] == "off"


def test_the_service_keeps_the_advert_checks_in_its_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So that a restart neither raises nor clears a check (design 6c D14)."""
    bridges: list[bridge_module.Bridge] = []

    async def stopped(bridge: bridge_module.Bridge, _stop: asyncio.Event) -> None:
        bridges.append(bridge)

    monkeypatch.setattr(bridge_module.Bridge, "run", stopped)
    key, at = "a1" * 32, 1_768_464_000
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"advert_checks": {key: {"advert_at": at, "carries": True}}})
    config = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        "[history]\nenabled = false\n[telemetry]\nenabled = false\n"
    )
    assert asyncio.run(cli_module._serve(config, tmp_path)) == EX_OK
    (bridge,) = bridges
    record = codec.decode(EmulatedContact(bytes.fromhex(key), "Alice", last_advert=at).record())
    assert isinstance(record, codec.ContactRecord)
    bridge.observer.radio_event(ContactsRead({key: record}, at + 60.0))
    assert bridge.observer.mesh.contacts[key].advert == AdvertCheck(at, True)


@pytest.mark.broker
def test_listen_ends_when_its_reader_goes_away(tmp_path: Path, broker: Broker) -> None:
    """`listen | head -1`: a closed pipe ends the command instead of hanging forever."""
    config = broker_config(tmp_path, broker)
    process = subprocess.Popen(  # noqa: S603 - our own command line
        [sys.executable, "-m", "meshcore_home_gateway", "listen", "--config", str(config)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip
    assert process.stdout is not None
    process.stdout.close()  # the reader is gone before the first event
    publisher = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect(broker.host, broker.port)
    publisher.loop_start()
    try:
        for _ in range(100):  # until listen has subscribed, printed and noticed
            publisher.publish("meshcore/radio1/received", b"{}", qos=1).wait_for_publish(2)
            if process.poll() is not None:
                break
            threading.Event().wait(0.1)
        assert process.poll() is not None, "listen still runs with nobody reading"
        assert process.returncode != 0
        assert process.stderr is not None
        assert b"Traceback" not in process.stderr.read()
    finally:
        publisher.disconnect()
        publisher.loop_stop()
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stderr is not None:
            process.stderr.close()


@pytest.mark.broker
def test_listen_prints_a_payload_it_cannot_decode_as_text(tmp_path: Path, broker: Broker) -> None:
    """Also one nested too deeply for the JSON parser (RecursionError, not ValueError)."""
    nested = b"[" * 100_000
    publisher = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect(broker.host, broker.port)
    publisher.loop_start()
    publisher.publish(  # retained: delivered as soon as listen subscribes
        "meshcore/radio1/received", nested, qos=1, retain=True
    ).wait_for_publish(5)
    config = broker_config(tmp_path, broker)
    output = tmp_path / "output"  # a file: the long line would fill a pipe nobody reads yet
    with output.open("wb") as out:
        process = subprocess.Popen(  # noqa: S603 - our own command line
            [sys.executable, "-m", "meshcore_home_gateway", "listen", "--config", str(config),
             "--topic", "received", "--count", "2"],
            stdout=out, stderr=subprocess.PIPE,
        )  # fmt: skip
    try:
        for _ in range(100):  # until listen has subscribed and printed both
            publisher.publish("meshcore/radio1/received", b"{}", qos=1).wait_for_publish(2)
            if process.poll() is not None:
                break
            threading.Event().wait(0.1)
        assert process.poll() is not None, "listen hangs after a payload it cannot decode"
        assert process.returncode == EX_OK
        assert process.stderr is not None
        assert b"Traceback" not in process.stderr.read()
    finally:
        publisher.disconnect()
        publisher.loop_stop()
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stderr is not None:
            process.stderr.close()
    first = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert first == {"topic": "meshcore/radio1/received", "payload": nested.decode()}


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


@pytest.mark.broker
def test_check_config_logs_in_to_the_broker_on_request(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    config = str(broker_config(tmp_path, broker))
    assert main(["check-config", "--config", config]) == EX_OK
    assert "login" not in capsys.readouterr().out  # only with --broker
    assert main(["check-config", "--config", config, "--broker"]) == EX_OK
    out = capsys.readouterr().out
    assert "no user name" in out
    assert f"login      accepted by {broker.host}:{broker.port}" in out


@pytest.mark.broker
def test_check_config_reports_a_refused_login_with_the_user_name(
    tmp_path: Path, refusing_broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    path = broker_config(tmp_path, refusing_broker)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            'node_id = "radio1"\n',
            'node_id = "radio1"\nusername = "gw"\npassword = "S3cret-pw-7"\n',
        ),
        encoding="utf-8",
    )
    assert main(["check-config", "--config", str(path), "--broker"]) == EX_FAILURE
    captured = capsys.readouterr()
    assert "user 'gw' with a password" in captured.out
    assert "broker refused the connection: Not authorized (user 'gw' with a password)" in (
        captured.err
    )
    assert "unknown user and missing credentials alike" in captured.err
    assert "systemctl reload mosquitto" in captured.err
    assert "S3cret" not in captured.out + captured.err


def test_check_config_reports_an_unreachable_broker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    path = tmp_path / "config.toml"
    path.write_text(
        f'[radio]\ntransport = "tcp"\nhost = "127.0.0.1"\n'
        f'[mqtt]\nhost = "127.0.0.1"\nport = {port}\nnode_id = "radio1"\n',
        encoding="utf-8",
    )
    assert main(["check-config", "--config", str(path), "--broker"]) == EX_FAILURE
    error = capsys.readouterr().err
    assert f"cannot connect to the broker 127.0.0.1:{port}" in error
    assert "alike" not in error  # no broker answered: not a matter of credentials


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
def test_send_wait_ignores_events_of_other_requests(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    """Other targets with the same text, other texts for the same target, undecodable payloads."""

    def answer(bridge: FakeBridge, text: str) -> None:
        bridge.client.publish("meshcore/radio1/delivery", b"not json", qos=1)
        bridge.client.publish("meshcore/radio1/delivery", b"[" * 100_000, qos=1)  # too deep
        bridge.event(event_type="queued", id="elsewhere", text=text, target="other")
        bridge.event(event_type="failed", id="elsewhere", text=text, target="other")
        bridge.event(event_type="queued", id="other", text="somebody else's")
        bridge.event(event_type="failed", id="other", text="somebody else's")
        bridge.event(event_type="queued", id="mine", text=text)
        bridge.event(event_type="sent", id="mine", text=text)

    fake = FakeBridge(broker, answer)
    try:
        config = str(broker_config(tmp_path, broker))
        code = main(["send", "--config", config, "--wait", "5", "alerts", "Hello world"])
    finally:
        fake.stop()
    assert json.loads(capsys.readouterr().out)["id"] == "mine"
    assert code == EX_OK


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


def test_send_refuses_a_text_that_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bytes that are not UTF-8 reach argv as surrogates (PEP 383)."""
    config = tls_config(tmp_path, "")  # never connects: the text check comes first
    text = b"caf\xe9".decode("utf-8", "surrogateescape")
    assert main(["send", "--config", str(config), "alerts", text]) == EX_FAILURE
    error = capsys.readouterr().err
    assert "not valid UTF-8" in error
    assert "Traceback" not in error


def test_probe_shows_the_plan_of_a_configuration(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    radio = EmulatedRadio(frequency_khz=868_100, tuning=(0, 1000), name="Bridge")
    config = tmp_path / "config.toml"
    config.write_text(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[radio.profile]\nmanage = "observe"\n'
        'name = "Gateway"\nduty_cycle_percent = 25.0\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        args = ["--config", str(config), "--state-dir", str(tmp_path)]
        assert main(["probe", "--tcp", address, *args, "--json"]) == EX_OK
        profile = json.loads(capsys.readouterr().out)["profile"]
        assert (profile["manage"], profile["writes"]) == ("observe", False)
        reported = {"action": "reported", "reason": None}
        assert profile["changes"]["name"] == {"live": "Bridge", "profile": "Gateway", **reported}
        assert profile["changes"]["duty_cycle_percent"] == {
            "live": 50.0, "profile": 25.0, **reported,
        }  # fmt: skip
        assert main(["probe", "--tcp", address, *args]) == EX_OK
        assert "  (only reported: radio.profile.manage = observe)\n" in capsys.readouterr().out


def test_probe_shows_the_protected_contacts_the_service_writes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The contact targets and command senders are part of the plan: a target that is no
    favourite is made one, a missing sender is added; while observing, both are reported."""
    sender = "e5" * 32
    radio = EmulatedRadio(contacts=[ALICE], tuning=(0, 9000))  # the settings match
    config = tmp_path / "config.toml"
    text = (
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        f'[targets.alice]\ncontact = "{ALICE.public_key.hex()}"\n'
        f'[commands]\nsenders = ["{sender}"]\nwords = ["status"]\n'
    )
    config.write_text(text)
    with TcpRadioServer(radio) as server:
        address = f"127.0.0.1:{server.port}"
        args = ["--config", str(config), "--state-dir", str(tmp_path)]
        assert main(["probe", "--tcp", address, *args, "--json"]) == EX_OK
        profile = json.loads(capsys.readouterr().out)["profile"]
        assert profile["changes"] == {}
        assert profile["contacts"] == {
            ALICE.public_key.hex(): {"name": "Alice\tSmith", "action": "favourite"},
            sender: {"name": "sender-e5e5", "action": "add"},
        }
        assert main(["probe", "--tcp", address, *args, "--list", "profile"]) == EX_OK
        lines = capsys.readouterr().out.splitlines()
        assert f"contact\t{ALICE.public_key.hex()}\tfavourite\tAlice Smith" in lines
        assert f"contact\t{sender}\tadd\tsender-e5e5" in lines
        assert main(["probe", "--tcp", address, *args]) == EX_OK
        out = capsys.readouterr().out
        assert "profile    differs from the configured profile:\n" in out
        assert (
            "  contact e5e5e5e5e5e5 sender-e5e5  (added as a favourite at the next connect)\n"
            in (out)
        )
        config.write_text(text.replace("[mqtt]", '[radio.profile]\nmanage = "observe"\n[mqtt]'))
        assert main(["probe", "--tcp", address, *args, "--json"]) == EX_OK
        contacts = json.loads(capsys.readouterr().out)["profile"]["contacts"]
        actions = {key: contact["action"] for key, contact in contacts.items()}
        assert actions == {ALICE.public_key.hex(): "not_favourite", sender: "missing"}
        config.write_text(
            text.replace("[mqtt]", "[radio.profile]\nprotect_contacts = false\n[mqtt]")
        )
        assert main(["probe", "--tcp", address, *args, "--json"]) == EX_OK
        assert json.loads(capsys.readouterr().out)["profile"]["contacts"] == {}
    assert radio.commands().count(Command.ADD_UPDATE_CONTACT) == 0


def test_probe_warns_when_it_cannot_read_the_service_state(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a logging setup (probe), the warning goes to stderr through logging's last
    resort; the plan then assumes a first connect."""
    state = tmp_path / "state.json"
    state.write_text("{}")
    real = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == state:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with TcpRadioServer(EmulatedRadio()) as server:
        address = f"127.0.0.1:{server.port}"
        assert main(["probe", "--tcp", address, "--state-dir", str(tmp_path), "--json"]) == EX_OK
    assert f"cannot read {state} (Permission denied); taken as empty" in caplog.text
    changes = json.loads(capsys.readouterr().out)["profile"]["changes"]
    assert {change["reason"] for change in changes.values()} == {"initial"}


def test_probe_shows_the_name_a_contact_gets_on_the_radio(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An alias has up to 32 characters, the radio keeps 31 bytes: the plan shows those."""
    key = "e5" * 32
    config = tmp_path / "config.toml"
    config.write_text(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        f'[targets.{"x" * 32}]\ncontact = "{key}"\n'
    )
    with TcpRadioServer(EmulatedRadio()) as server:
        args = ["--config", str(config), "--state-dir", str(tmp_path), "--json"]
        assert main(["probe", "--tcp", f"127.0.0.1:{server.port}", *args]) == EX_OK
    contacts = json.loads(capsys.readouterr().out)["profile"]["contacts"]
    assert contacts == {key: {"name": "x" * 31, "action": "add"}}
