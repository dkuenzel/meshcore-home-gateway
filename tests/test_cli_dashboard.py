"""`meshcore-home-gateway dashboard`: the YAML on standard output, how many contacts the map lists
on standard error."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from meshcore_home_gateway import dashboard
from meshcore_home_gateway.cli import EX_CONFIG, EX_FAILURE, EX_OK, main
from meshcore_home_gateway.config import load_config
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.protocol.codes import NodeType
from tests.test_contact_devices import ALICE, BOB, HILL, Clock, devices, mesh_of, node

CONFIG = (
    '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\n'
    '[mqtt]\nhost = "broker.example.org"\nnode_id = "radio-1"\n'
)
HERE = Position(52.1, 9.2)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _state(directory: Path, *located: str) -> Path:
    """The service's state with a device for Bob and a tracker for each of `located`."""
    contacts, _ = devices(directory, clock=Clock())
    names = {ALICE: "Alice", HILL: "Hilltop"}
    contacts.update(mesh_of(
        node(BOB, "Bob"),
        *(node(key, names[key], NodeType.REPEATER, position=HERE) for key in located),
    ))  # fmt: skip
    return directory


def test_the_dashboard_goes_to_standard_output(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(tmp_path / "state", ALICE)
    assert main(["dashboard", "--config", str(config), "--state-dir", str(state)]) == EX_OK
    printed = capsys.readouterr()
    expected, count = dashboard.dashboard(load_config(config), state)
    assert count == 1
    assert printed.out == expected
    assert printed.out.startswith("# meshcore-home-gateway dashboard for node radio-1, ")
    assert "binary_sensor.meshcore_home_gateway_radio_1_radio" in printed.out
    assert 'entities: ["device_tracker.mesh_alice"]' in printed.out
    assert printed.err == "The map lists 1 contact with a position.\n"


def test_the_state_directory_is_the_services_by_default(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    state = _state(tmp_path / "state", ALICE, HILL)
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    assert main(["dashboard", "--config", str(config)]) == EX_OK
    printed = capsys.readouterr()
    assert 'entities: ["device_tracker.mesh_alice", "device_tracker.mesh_hilltop"]' in printed.out
    assert printed.err == "The map lists 2 contacts with a position.\n"
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "nothing"))
    assert main(["dashboard", "--config", str(config)]) == EX_OK
    printed = capsys.readouterr()
    assert "type: map" not in printed.out
    assert printed.err == "The map lists 0 contacts with a position.\n"
    assert not (tmp_path / "nothing").exists()  # read only


def test_without_discovery_there_is_no_dashboard(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text(CONFIG + "[homeassistant]\ndiscovery = false\n", encoding="utf-8")
    status = main(["dashboard", "--config", str(config), "--state-dir", str(tmp_path)])
    assert status == EX_FAILURE
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err == (
        "meshcore-home-gateway: Home Assistant discovery is off ([homeassistant] discovery = "
        "false): the dashboard would find no entities.\n"
    )


def test_a_configuration_error_is_one(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text('[radio]\ntransport = "serial"\n', encoding="utf-8")
    status = main(["dashboard", "--config", str(config), "--state-dir", str(tmp_path)])
    assert status == EX_CONFIG
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.startswith("meshcore-home-gateway: configuration error: ")


def test_the_yaml_is_utf8_whatever_the_terminal_takes(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    assert main(["dashboard", "--config", str(config), "--state-dir", str(tmp_path)]) == EX_OK
    assert "\u2013".encode() in raw.getvalue()  # the dash of a time not known
    assert raw.getvalue().decode("utf-8") == dashboard.dashboard(load_config(config), tmp_path)[0]
