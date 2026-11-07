"""`status`, `doctor`, `backup`, `retire` and `history export` (IMP-016, IMP-047, EXT-016,
OPS-008, OPS-011, OPS-022), against a fake broker that keeps retained messages."""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import io
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshcore_home_gateway.cli import EX_FAILURE, EX_OK, main
from meshcore_home_gateway.cli import backup as cli_backup
from meshcore_home_gateway.cli import broker as cli_broker
from meshcore_home_gateway.cli import doctor as cli_doctor
from meshcore_home_gateway.history import SCHEMA_VERSION, History
from meshcore_home_gateway.state import JsonStateFile
from meshcore_home_gateway.views import HistoryEntry

ROOT = "meshcore/radio1"
KEY = "a1" * 32
DEVICE = "homeassistant/device/meshcore_home_gateway_radio1"
HOSTILE = "Gate\x1b[2J\u202e"


class _Code:
    """paho's reason code: a refusal names its reason."""

    def __init__(self, refusal: str | None = None) -> None:
        self.is_failure = refusal is not None
        self.text = refusal or "Success"

    def __str__(self) -> str:
        return self.text


@dataclasses.dataclass
class FakeBroker:
    """paho's client without a broker: retained messages by topic, every publish recorded."""

    retained: dict[str, bytes] = dataclasses.field(default_factory=dict)
    published: list[tuple[str, bytes, bool]] = dataclasses.field(default_factory=list)
    anonymous: bool = False  # it takes clients without a user name
    clients: list[str] = dataclasses.field(default_factory=list)
    on_connect: Any = None
    on_subscribe: Any = None
    on_message: Any = None

    def connect(self, _host: str, _port: int, _keepalive: int) -> None:
        pass

    def loop_start(self) -> None:
        refused = self.clients[-1] == "anonymous" and not self.anonymous
        self.on_connect(self, None, None, _Code("Not authorized" if refused else None), None)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.on_subscribe(self, None, 1, [_Code()], None)
        for name, body in list(self.retained.items()):
            if _matches(topic, name):
                self.on_message(self, None, SimpleNamespace(topic=name, payload=body, retain=True))

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any:
        self.published.append((topic, payload, retain))
        if retain and payload:
            self.retained[topic] = payload
        elif retain:
            self.retained.pop(topic, None)
        return SimpleNamespace(wait_for_publish=lambda _t: None, is_published=lambda: True)

    def disconnect(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass


def _matches(pattern: str, topic: str) -> bool:
    wanted, given = pattern.split("/"), topic.split("/")
    for i, part in enumerate(wanted):
        if part == "#":
            return True
        if i >= len(given) or part not in ("+", given[i]):
            return False
    return len(wanted) == len(given)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    broker = FakeBroker()

    def client(_mqtt: Any, suffix: str) -> FakeBroker:
        broker.clients.append(suffix)
        return broker

    monkeypatch.setattr(cli_broker, "mqtt_client", client)
    for name in ("CONNECT_TIMEOUT_S", "SUBSCRIBE_TIMEOUT_S", "RETAINED_WAIT_S"):
        monkeypatch.setattr(cli_broker, name, 0.01)
    return broker


def config_file(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[radio]\ntransport = "serial"\ndevice = "/dev/ttyUSB0"\n'
        '[mqtt]\nhost = "broker.lan"\nnode_id = "radio1"\nusername = "gw"\npassword = "s3cret"\n'
        f"{extra}",
        encoding="utf-8",
    )
    return path


def state(**changes: Any) -> dict[str, Any]:
    health = {
        "degraded": False,
        "history": "ok",
        "state_file": "ok",
        "exclusions": "ok",
        "errors_1h": 0,
        "loop_lag_max_s": 0.01,
        "events_dropped_1h": 0,
        "disk_free_mb": 9000,
    }
    return {
        "radio": "connected",
        "battery_v": 4.05,
        "radio_clock_offset_s": 1,
        "radio_link_contested": None,
        "airtime_1h_s": 12.5,
        "airtime_available_s": 50.0,
        "airtime_budget_s_per_hour": 360.0,
        "queue": 0,
        "radio_name": "Gate",
        "model": "Heltec V3",
        "firmware": "v1.9.0",
        "telemetry": "running",
        "telemetry_reason": None,
        "health": health,
        **changes,
    }


def publish(fake: FakeBroker, documents: dict[str, Any]) -> None:
    for name, document in documents.items():
        body = document if isinstance(document, str) else json.dumps(document)
        fake.retained[f"{ROOT}/{name}"] = body.encode()


# --- status ------------------------------------------------------------------------------------


def test_status_summarises_what_the_gateway_reports(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    contacts = {"total": 7, "max": 350, "telemetry_excluded_total": 2}
    publish(
        fake, {"availability": "online", "state": state(radio_name=HOSTILE), "contacts": contacts}
    )
    assert main(["status", "--config", str(config_file(tmp_path))]) == EX_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "gateway radio1: online"
    assert "  radio      connected: Gate [2J , Heltec V3, firmware v1.9.0" in lines  # safe text
    assert "  airtime    12.5 s used in the last hour, 50.0 s available, 360.0 s per hour" in lines
    assert "  health     ok" in lines
    assert lines[-1] == "  contacts   7 of 350, 2 excluded from telemetry"
    assert fake.clients == ["status"]
    assert fake.published == []  # it only reads


@pytest.mark.parametrize(
    ("documents", "line"),
    [
        ({"availability": "offline", "state": state()}, "gateway radio1: offline"),
        ({}, "gateway radio1: never seen on this broker"),
        (
            {
                "availability": "online",
                "state": state(
                    health={
                        "degraded": True,
                        "history": "broken",
                        "errors_1h": 2,
                        "disk_free_mb": 50,
                    }
                ),
            },
            "  health     degraded (history: broken, errors_1h: 2, disk_free_mb: 50)",
        ),
    ],
)
def test_status_fails_while_the_gateway_is_offline_or_degraded(
    tmp_path: Path,
    fake: FakeBroker,
    capsys: pytest.CaptureFixture[str],
    documents: dict[str, Any],
    line: str,
) -> None:
    publish(fake, documents)
    assert main(["status", "--config", str(config_file(tmp_path))]) == EX_FAILURE
    assert line in capsys.readouterr().out.splitlines()


def test_status_json_gives_the_documents(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    publish(fake, {"availability": "online", "state": state(), "contacts": {"total": 1}})
    assert main(["status", "--json", "--config", str(config_file(tmp_path))]) == EX_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"availability": "online", "state": state(), "contacts": {"total": 1}}


# --- doctor ------------------------------------------------------------------------------------


def test_doctor_reports_each_check_and_fails_on_a_problem(
    tmp_path: Path,
    fake: FakeBroker,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_doctor, "UPGRADER", tmp_path / "unattended-upgrade")  # absent
    monkeypatch.setattr(cli_doctor, "REBOOT_REQUIRED", tmp_path / "reboot-required")
    (tmp_path / "reboot-required").touch()
    fake.anonymous = True
    contacts = {"manage_reason": "untested_firmware"}
    publish(
        fake,
        {"availability": "online", "state": state(radio_clock_offset_s=120), "contacts": contacts},
    )
    config = config_file(tmp_path)
    assert main(["doctor", "--config", str(config), "--state-dir", str(tmp_path)]) == EX_OK
    out = capsys.readouterr().out
    assert f"  ok       configuration {config}: valid" in out
    assert "  ok       broker broker.lan:1883: logged in" in out
    assert "  warning  the broker accepts clients without a user name" in out
    assert "  warning  the radio's clock is 120 s ahead of the host's: up to an hour ahead" in out
    assert "  warning  firmware v1.9.0 is not one the gateway is tested with" in out
    assert "  warning  unattended-upgrades is not installed" in out
    assert "  warning  an update waits for a restart of the container" in out
    assert "MB free for" in out
    assert fake.clients == ["doctor", "anonymous"]

    publish(fake, {"availability": "offline"})
    assert main(["doctor", "--config", str(config), "--state-dir", str(tmp_path)]) == EX_FAILURE
    assert (
        "  problem  the gateway is not online (is its service running?)" in capsys.readouterr().out
    )


def test_doctor_names_a_degraded_health_and_a_radio_away(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    health = {"degraded": True, "state_file": "unwritable"}
    publish(fake, {"availability": "online", "state": state(radio="disconnected", health=health)})
    assert (
        main(["doctor", "--config", str(config_file(tmp_path)), "--state-dir", str(tmp_path)])
        == EX_FAILURE
    )
    out = capsys.readouterr().out
    assert "  problem  the radio is not connected" in out
    assert "  problem  health     degraded (state_file: unwritable)" in out
    assert "  ok       the broker refuses clients without a user name" in out


def test_doctor_tells_a_radio_clock_behind_from_one_ahead(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behind, the gateway moves it forward; ahead, it restarts the radio (radio/session.py)."""
    publish(fake, {"availability": "online", "state": state(radio_clock_offset_s=-120)})
    main(["doctor", "--config", str(config_file(tmp_path)), "--state-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "  warning  the radio's clock is 120 s behind the host's: with [radio] set_clock" in out
    assert "ahead" not in out


# --- backup ------------------------------------------------------------------------------------


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers()}  # type: ignore[union-attr]


def _state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    JsonStateFile(directory / "state.json").save({"command_timestamps": {KEY: 1}})
    (directory / "telemetry-excluded.json").write_text('{"excluded": []}', encoding="utf-8")
    history = History.open(directory / "history.sqlite3")
    assert history is not None
    history.add_message(HistoryEntry(1_700_000_000.0, "in", "contact", "hi", KEY, name="Alice"))
    history.close()
    return directory


def test_backup_writes_the_state_and_the_configuration_without_the_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, state_dir, archive = config_file(tmp_path), _state_dir(tmp_path), tmp_path / "b.tgz"
    assert (
        main(["backup", str(archive), "--config", str(config), "--state-dir", str(state_dir)])
        == EX_OK
    )
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600
    members = _members(archive)
    assert set(members) == {
        "config.toml",
        "state/state.json",
        "state/telemetry-excluded.json",
        "state/history.sqlite3",
        "MANIFEST.json",
    }
    text = members["config.toml"].decode()
    assert "s3cret" not in text
    assert cli_backup.LEFT_OUT in text
    assert 'username = "gw"' in text
    manifest = json.loads(members["MANIFEST.json"])
    assert (manifest["password"], manifest["files"][0]) == ("left_out", "config.toml")
    copy = tmp_path / "copy.sqlite3"
    copy.write_bytes(members["state/history.sqlite3"])
    with contextlib.closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT text FROM message").fetchall() == [("hi",)]
    assert "wrote" in capsys.readouterr().out

    assert (
        main(["backup", str(archive), "--config", str(config), "--state-dir", str(state_dir)])
        == EX_FAILURE
    )
    assert "exists: choose another name" in capsys.readouterr().err


def test_backup_keeps_the_secrets_only_when_asked(tmp_path: Path) -> None:
    secret, codes = tmp_path / "password", tmp_path / "command-codes"
    secret.write_text("s3cret\n", encoding="utf-8")
    codes.write_text(f'{KEY} = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"\n', encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[mqtt]\nhost = "h"\nnode_id = "radio1"\nusername = "gw"\npassword_file = "{secret}"\n'
        f'[commands]\nsenders = ["{KEY}"]\nwords = ["arm"]\ncodes_file = "{codes}"\n',
        encoding="utf-8",
    )
    archive = tmp_path / "b.tgz"
    argv = ["backup", str(archive), "--config", str(config), "--state-dir", str(tmp_path / "none")]
    assert main([*argv, "--with-secrets"]) == EX_OK
    members = _members(archive)
    assert (members["password"], members["command-codes"]) == (
        secret.read_bytes(),
        codes.read_bytes(),
    )
    manifest = json.loads(members["MANIFEST.json"])
    assert (manifest["password"], manifest["codes"], manifest["codes_file"]) == (
        "kept",
        "kept",
        str(codes),
    )
    archive.unlink()
    assert main(argv) == EX_OK
    members = _members(archive)
    assert {"password", "command-codes"} & set(members) == set()
    assert json.loads(members["MANIFEST.json"])["codes"] == "left_out"


REPOSITORY = Path(__file__).resolve().parent.parent


def _restore_commands() -> list[list[str]]:
    """The commands of the restore in docs/deployment.md, each as its arguments."""
    guide = (REPOSITORY / "docs" / "deployment.md").read_text(encoding="utf-8")
    after = guide.partition("To restore")[2]
    block = re.search(r"```sh\n(.*?)```", after, flags=re.DOTALL)
    assert block is not None, "no commands after 'To restore' in docs/deployment.md"
    return [shlex.split(line) for line in block[1].splitlines() if line.strip()]


def _restore() -> list[list[str]]:
    """The `install` commands of the restore in docs/deployment.md, each as its arguments."""
    return [command for command in _restore_commands() if command[0] == "install"]


UNIT = REPOSITORY / "deploy" / "systemd" / "meshcore-home-gateway.service"


def _unit(key: str) -> str:
    found = re.search(rf"^{key}=(.+)$", UNIT.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert found is not None, key
    return found[1]


def test_the_guides_restore_gives_each_file_its_owner_group_and_mode(tmp_path: Path) -> None:
    """#98: the archive holds every file as root with mode 0600, and the restore said only where
    each goes. A file the copy creates keeps that, so in a new container the service could not
    read the codes file and stopped with a configuration error. The guide puts every file of an
    archive with the secrets back with the owner, group and mode the gateway gives it: the
    configuration and the secrets for the service's group to read, as `config render --group` and
    `commands enrol` write them; the state the service's own (the unit's User= and UMask=0077)."""
    secret, codes = tmp_path / "password", tmp_path / "command-codes"
    secret.write_text("s3cret\n", encoding="utf-8")
    codes.write_text(f'{KEY} = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"\n', encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[mqtt]\nhost = "h"\nnode_id = "radio1"\nusername = "gw"\npassword_file = "{secret}"\n'
        f'[commands]\nsenders = ["{KEY}"]\nwords = ["arm"]\ncodes_file = "{codes}"\n',
        encoding="utf-8",
    )
    archive, state_dir = tmp_path / "b.tgz", _state_dir(tmp_path)
    argv = ["backup", str(archive), "--config", str(config), "--state-dir", str(state_dir)]
    assert main([*argv, "--with-secrets"]) == EX_OK
    service = (_unit("User"), _unit("Group"))
    assert _unit("UMask") == "0077"
    etc = "/etc/meshcore-home-gateway/"
    expected = {  # the member: mode, owner, group, where it goes
        "config.toml": ("0640", "root", service[1], etc + "config.toml"),
        "password": ("0640", "root", service[1], etc + "mqtt-password"),  # the installer's
        "command-codes": ("0640", "root", service[1], etc + "command-codes"),
        "state/*": ("0600", *service, f"/var/lib/{_unit('StateDirectory')}/"),
    }
    assert _unit("ExecStart").endswith(f" --config {expected['config.toml'][3]}")
    restored = {}
    for arguments in _restore():
        options = dict(zip(arguments[1:-2:2], arguments[2:-2:2], strict=True))
        restored[arguments[-2]] = (options["-m"], options["-o"], options["-g"], arguments[-1])
    assert restored == expected
    members = set(_members(archive)) - {"MANIFEST.json"}
    assert {m if not m.startswith("state/") else "state/*" for m in members} == set(expected)


KILLED = """
import os, sys
from pathlib import Path
from meshcore_home_gateway.history import History
from meshcore_home_gateway.views import HistoryEntry
history = History.open(Path(sys.argv[1]))
for n in range(50):
    history.add_message(HistoryEntry(1.8e9 + n, "in", "contact", f"later {n}", name="Bob"))
os._exit(0)  # killed before it closed the history: its write-ahead log stays
"""


def test_the_guides_restore_removes_the_write_ahead_log_of_the_history_it_replaces(
    tmp_path: Path,
) -> None:
    """#98 review: the history uses SQLite's write-ahead log. A stop that was cut short (the
    stop timeout, the watchdog, a crash loop: a typical moment to restore) leaves
    `history.sqlite3-wal` and `-shm` beside the history. The restore replaced only
    `history.sqlite3`, and at the next start SQLite applied the old log to it: the integrity
    check said ok, but the history was the old one. The guide removes both after the stop and
    before it puts the state back."""
    archive = tmp_path / "b.tgz"
    argv = ["--config", str(config_file(tmp_path)), "--state-dir", str(_state_dir(tmp_path))]
    assert main(["backup", str(archive), *argv]) == EX_OK
    state = f"/var/lib/{_unit('StateDirectory')}/"
    live = tmp_path / "var-lib"
    live.mkdir()
    env = {**os.environ, "PYTHONPATH": str(REPOSITORY / "src")}
    killed = [sys.executable, "-c", KILLED, str(live / "history.sqlite3")]
    subprocess.run(killed, env=env, check=True)  # noqa: S603 - our own code, in tmp_path
    assert (live / "history.sqlite3-wal").stat().st_size > 0  # what the stop left
    commands = _restore_commands()
    stop = commands.index(["systemctl", "stop", "meshcore-home-gateway"])
    [put] = [i for i, c in enumerate(commands) if c[0] == "install" and c[-1] == state]
    removals = [c for c in commands[stop:put] if c[0] == "rm"]
    assert removals, "the guide removes nothing between the stop and the restore of the state"
    for command in removals:
        moved = [a.replace(state, f"{live}/") if a.startswith(state) else a for a in command]
        assert moved != command, command  # it removes in the state directory
        subprocess.run(moved, check=True)  # noqa: S603 - the guide's own command, in tmp_path
    with tarfile.open(archive) as backup:  # what the guide's install puts there
        for member in backup.getmembers():
            if member.name.startswith("state/"):
                extracted = backup.extractfile(member)
                assert extracted is not None
                (live / member.name.removeprefix("state/")).write_bytes(extracted.read())
    history = History.open(live / "history.sqlite3")
    assert history is not None
    try:
        assert [(m.name, m.text) for m in history.recent_messages(100)] == [("Alice", "hi")]
    finally:
        history.close()


def test_backup_refuses_a_password_it_cannot_leave_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('mqtt = { host = "h", node_id = "n", password = "x" }\n', encoding="utf-8")
    argv = [
        "backup",
        str(tmp_path / "b.tgz"),
        "--config",
        str(config),
        "--state-dir",
        str(tmp_path),
    ]
    assert main(argv) == EX_FAILURE
    assert "add --with-secrets" in capsys.readouterr().err
    assert not (tmp_path / "b.tgz").exists()


def test_backup_leaves_no_half_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def full(*_: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tarfile.TarFile, "addfile", full)
    config, state_dir, archive = config_file(tmp_path), _state_dir(tmp_path), tmp_path / "b.tgz"
    argv = ["backup", str(archive), "--config", str(config), "--state-dir", str(state_dir)]
    assert main(argv) == EX_FAILURE
    assert "No space left on device" in capsys.readouterr().err
    assert not archive.exists()


def test_without_password_comments_out_only_the_mqtt_password() -> None:
    text = '[radio]\npassword = "radio"\n[mqtt]\n  password = "s3cret"  # broker\nport = 1\n'
    kept = cli_backup.without_password(text)
    assert kept == f'[radio]\npassword = "radio"\n[mqtt]\n  {cli_backup.LEFT_OUT}\nport = 1\n'


# --- retire ------------------------------------------------------------------------------------


def _announced(state_dir: Path) -> None:
    state_dir.mkdir(exist_ok=True)
    JsonStateFile(state_dir / "state.json").save(
        {
            "ha_components": {"radio": "binary_sensor", "queue": "sensor"},
            "ha_removed": {"old_words": {"p": "event", "since": 1_700_000_000}},
            "ha_contact_devices": {
                "announced": {KEY: {"slug": "alice", "cmps": {"battery": "sensor"}}},
                "removed": {"b2" * 32: {"cmps": {"send": "notify"}, "since": 1_700_000_000}},
            },
        }
    )


def test_retire_refuses_while_the_gateway_is_online(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    publish(fake, {"availability": "online", "state": state()})
    assert main(["retire", "--yes", "--config", str(config_file(tmp_path))]) == EX_FAILURE
    assert "stop its service first" in capsys.readouterr().err
    assert fake.published == []


def test_retire_without_yes_only_counts(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    _announced(tmp_path / "state")
    publish(fake, {"availability": "offline", "state": state(), f"contacts/{KEY}": {"key": KEY}})
    argv = [
        "retire",
        "--config",
        str(config_file(tmp_path)),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    assert main(argv) == EX_OK
    out = capsys.readouterr().out
    assert "Home Assistant: 4 devices to remove" in out
    assert f"broker: 2 retained documents to clear under {ROOT}/" in out
    assert "add --yes" in out
    assert fake.published == []


def test_retire_removes_the_devices_and_clears_the_documents(
    tmp_path: Path, fake: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    _announced(tmp_path / "state")
    publish(fake, {"availability": "offline", "state": state(), f"contacts/{KEY}": {"key": KEY}})
    fake.retained["meshcore/other/state"] = b"{}"  # another gateway's: not touched
    argv = ["retire", "--yes", "--config", str(config_file(tmp_path))]
    assert main([*argv, "--state-dir", str(tmp_path / "state")]) == EX_OK
    topics = [topic for topic, _, _ in fake.published]
    gateway = f"{DEVICE}/config"
    assert topics[:2] == [gateway, gateway]
    bind = json.loads(fake.published[0][1])
    assert "meshcore_home_gateway_radio1_old_words" in {
        c["unique_id"] for c in bind["cmps"].values()
    }
    assert fake.published[1][1:] == (b"", False)
    for key in ("b2" * 32, KEY):  # a pending removal too, every device bound then emptied
        topic = f"{DEVICE}/contact_{key}/config"
        assert [p for t, p, _ in fake.published if t == topic][-1] == b""
        assert topics.count(topic) == 2
    assert (f"{ROOT}/state", b"", True) in fake.published
    assert (f"{ROOT}/contacts/{KEY}", b"", True) in fake.published
    assert fake.published[-1] == (f"{ROOT}/availability", b"offline", True)
    assert fake.retained == {f"{ROOT}/availability": b"offline", "meshcore/other/state": b"{}"}
    assert "retired" in capsys.readouterr().out


# --- history export ----------------------------------------------------------------------------


def _history(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(exist_ok=True)
    history = History.open(directory / "history.sqlite3")
    assert history is not None
    history.add_message(
        HistoryEntry(1_700_000_000.0, "in", "contact", "hi\x1b[2J", KEY, name="Alice")
    )
    history.add_message(HistoryEntry(1_800_000_000.0, "out", "channel", "later", channel_index=0))
    history.add_readings(KEY, 1_700_000_100.0, {"battery_v": 4.1})
    history.close()
    return directory


def test_history_export_writes_json_lines_with_iso_times(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _history(tmp_path)
    assert main(["history", "export", "--state-dir", str(directory)]) == EX_OK
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(r["at"], r["text"]) for r in rows] == [
        ("2023-11-14T22:13:20+00:00", "hi\x1b[2J"),
        ("2027-01-15T08:00:00+00:00", "later"),
    ]
    argv = ["history", "export", "--state-dir", str(directory), "--since", "2026-01-01"]
    assert main(argv) == EX_OK
    assert [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()] == ["later"]


def test_history_export_csv_has_a_header_and_safe_texts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _history(tmp_path)
    assert main(["history", "export", "--format", "csv", "--state-dir", str(directory)]) == EX_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        "at,direction,kind,contact_key,channel_index,name,text,state,hops,snr_db,"
        "target,delivery_id,queued_at,suspect,reason"
    )
    assert lines[1] == f"2023-11-14T22:13:20+00:00,in,contact,{KEY},,Alice,hi [2J,,,,,,,,"
    argv = ["history", "export", "--table", "readings", "--format", "csv"]
    assert main([*argv, "--state-dir", str(directory)]) == EX_OK
    assert capsys.readouterr().out.splitlines()[1:] == [
        f"{KEY},2023-11-14T22:15:00+00:00,battery_v,4.1"
    ]


def test_history_export_has_the_keys_of_2_1_at_the_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The target, the delivery id, the acceptance time (as a time, like `at`), whether the
    acknowledgement was suspect, and why a message failed, in JSON lines and in CSV."""
    directory = tmp_path / "state"
    directory.mkdir()
    history = History.open(directory / "history.sqlite3")
    assert history is not None
    for at, suspect, reason in ((0.0, True, None), (60.0, False, None), (120.0, None, "queue_ttl")):
        state = "expired" if reason else "confirmed"
        history.add_message(
            HistoryEntry(
                1_700_000_000.0 + at,
                "out",
                "contact",
                "Back soon",
                KEY,
                name="Alice",
                state=state,
                target="alice",
                id="1a2b3c4d",
                queued_at=1_699_999_990.0 + at,
                suspect=suspect,
                reason=reason,
            )
        )
    history.close()
    argv = ["history", "export", "--state-dir", str(directory)]
    assert main(argv) == EX_OK
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert list(rows[0])[-5:] == ["target", "delivery_id", "queued_at", "suspect", "reason"]
    assert [(r["queued_at"], r["suspect"], r["reason"]) for r in rows] == [
        ("2023-11-14T22:13:10+00:00", True, None),
        ("2023-11-14T22:14:10+00:00", False, None),
        ("2023-11-14T22:15:10+00:00", None, "queue_ttl"),
    ]
    assert {(r["target"], r["delivery_id"]) for r in rows} == {("alice", "1a2b3c4d")}
    assert main([*argv, "--format", "csv"]) == EX_OK
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(",")[-5:] for line in lines[1:]] == [
        ["alice", "1a2b3c4d", "2023-11-14T22:13:10+00:00", "true", ""],
        ["alice", "1a2b3c4d", "2023-11-14T22:14:10+00:00", "false", ""],
        ["alice", "1a2b3c4d", "2023-11-14T22:15:10+00:00", "", "queue_ttl"],
    ]


def test_history_export_csv_shows_a_formula_as_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    history = History.open(directory / "history.sqlite3")
    assert history is not None
    texts = ["=HYPERLINK(1)", "+1", "-1", "@a", "a=b"]
    for at, text in enumerate(texts):
        history.add_message(HistoryEntry(1_700_000_000.0 + at, "in", "contact", text, KEY))
    history.close()
    argv = ["history", "export", "--state-dir", str(directory)]
    assert main([*argv, "--format", "csv"]) == EX_OK
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))[1:]
    assert [row[6] for row in rows] == ["'=HYPERLINK(1)", "'+1", "'-1", "'@a", "a=b"]
    assert main(argv) == EX_OK  # JSON is data, not a sheet: as received
    assert [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()] == texts


def test_history_export_reads_a_history_the_service_has_not_migrated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _history(tmp_path)
    with contextlib.closing(sqlite3.connect(directory / "history.sqlite3")) as db:
        db.execute("ALTER TABLE message DROP COLUMN snr_db")
    assert main(["history", "export", "--state-dir", str(directory)]) == EX_OK
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(r["text"], r["snr_db"]) for r in rows] == [("hi\x1b[2J", None), ("later", None)]


def test_history_export_refuses_a_missing_or_newer_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["history", "export", "--state-dir", str(tmp_path)]) == EX_FAILURE
    assert "no history at" in capsys.readouterr().err
    directory = _history(tmp_path)
    with contextlib.closing(sqlite3.connect(directory / "history.sqlite3")) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    assert main(["history", "export", "--state-dir", str(directory)]) == EX_FAILURE
    assert "newer version" in capsys.readouterr().err


def test_history_export_takes_a_date_with_or_without_a_zone() -> None:
    from meshcore_home_gateway.cli.history import since_time

    assert since_time("2026-11-01").isoformat() == "2026-11-01T00:00:00+00:00"
    assert since_time("2026-11-01T08:00:00+01:00").isoformat() == "2026-11-01T08:00:00+01:00"
    with pytest.raises(Exception, match="not a date"):
        since_time("yesterday")


def test_the_archive_reader_of_the_tests_reads_what_backup_writes(tmp_path: Path) -> None:
    archive = tmp_path / "x.tgz"
    cli_backup._write(archive, {"a": b"1"})
    assert _members(archive) == {"a": b"1"}
    with tarfile.open(archive) as opened:
        assert opened.getmember("a").mode == 0o600
