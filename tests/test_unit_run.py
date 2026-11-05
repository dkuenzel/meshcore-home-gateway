"""The service as an installation runs it: the zipapp of `make dist` and the systemd unit as they
are, under the host's systemd as the service user, with an emulated radio over TCP and a private
broker. It shows what only a real service manager does (hardware checklist steps 2, 16 and 17):
the readiness the unit waits for, the unit's sandbox, the watchdog, the reconnect after the broker
restarts, and the gateway's own check (`doctor`), which the installer runs after an installation.

It installs into the system (/usr/local, /etc, /var/lib, a system user) and needs root, systemd
and the distribution's Python with paho-mqtt: `make unit-run` on a machine to throw away, which CI's
job *unit check* is. Elsewhere it is skipped."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import Broker, free_port, start_mosquitto, stop_mosquitto
from tests.radio_emulator import EmulatedContact, EmulatedRadio, TcpRadioServer
from tests.test_system import Watcher

ROOT = Path(__file__).resolve().parent.parent
PYZ = ROOT / "dist" / "meshcore-home-gateway.pyz"
UNIT = ROOT / "deploy" / "systemd" / "meshcore-home-gateway.service"
SERVICE = "meshcore-home-gateway"
LIB = Path("/usr/local/lib/meshcore-home-gateway")
COMMAND = Path("/usr/local/bin/meshcore-home-gateway")
CONFIG = Path("/etc/meshcore-home-gateway/config.toml")
INSTALLED_UNIT = Path("/etc/systemd/system/meshcore-home-gateway.service")
STATE = Path("/var/lib/meshcore-home-gateway")
ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")


def _watchdog_s() -> int:
    found = re.search(r"^WatchdogSec=(\d+)$", UNIT.read_text(encoding="utf-8"), re.MULTILINE)
    assert found is not None
    return int(found[1])


WATCHDOG_S = _watchdog_s()

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("MESHCORE_HOME_GATEWAY_UNIT_RUN") != "1",
        reason="installs into the system: `make unit-run` on a machine to throw away",
    ),
    pytest.mark.broker,
]


def run(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603


def show(name: str) -> str:
    return run("systemctl", "show", "--property", name, "--value", SERVICE).stdout.strip()


@dataclass
class Installation:
    broker: Broker
    conf: Path  # the broker's configuration, to start it again
    mosquitto: subprocess.Popen[bytes]


@pytest.fixture
def installation(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Installation]:
    if os.geteuid() != 0:
        pytest.fail("make unit-run installs the service: run it as root")
    if not PYZ.exists():
        pytest.fail(f"{PYZ} is missing: make dist")
    port = free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
    found = Installation(Broker("127.0.0.1", port), conf, start_mosquitto(conf, port))
    with TcpRadioServer(EmulatedRadio(contacts=[ALICE])) as radio:
        try:
            _install(found.broker, radio.port)
            yield found
        finally:
            journal = run("journalctl", "--unit", SERVICE, "--no-pager", "--output", "cat").stdout
            request.node.add_report_section("teardown", "journal", journal[-20_000:])
            _remove()
            stop_mosquitto(found.mosquitto)


def _install(broker: Broker, radio_port: int) -> None:
    """What the installer does in the container, but with the files of this checkout."""
    if run("id", SERVICE).returncode != 0:
        user = run(
            "useradd",
            "--system",
            "--user-group",
            "--groups",
            "dialout",
            "--no-create-home",
            "--home-dir",
            "/nonexistent",
            "--shell",
            "/usr/sbin/nologin",
            SERVICE,
        )
        assert user.returncode == 0, user.stderr
    version = run("/usr/bin/python3", "-I", str(PYZ), "--version").stdout.split()[-1]
    LIB.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PYZ, LIB / f"meshcore-home-gateway-{version}.pyz")
    (LIB / f"meshcore-home-gateway-{version}.pyz").chmod(0o755)
    (LIB / "current.pyz").symlink_to(f"meshcore-home-gateway-{version}.pyz")
    COMMAND.symlink_to(LIB / "current.pyz")
    shutil.copyfile(UNIT, INSTALLED_UNIT)
    INSTALLED_UNIT.chmod(0o644)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\n'
        f'host = "127.0.0.1"\nport = {radio_port}\n'
        f'[mqtt]\nhost = "{broker.host}"\nport = {broker.port}\nnode_id = "radio1"\n'
        "[telemetry]\nenabled = false\n",
        encoding="utf-8",
    )
    shutil.chown(CONFIG, "root", SERVICE)
    CONFIG.chmod(0o640)
    assert run("systemctl", "daemon-reload").returncode == 0


def _remove() -> None:
    run("systemctl", "stop", SERVICE, timeout=120)
    for path in (COMMAND, INSTALLED_UNIT):
        path.unlink(missing_ok=True)
    for directory in (LIB, CONFIG.parent, STATE):
        shutil.rmtree(directory, ignore_errors=True)
    run("systemctl", "daemon-reload")
    run("systemctl", "reset-failed", SERVICE)
    run("userdel", SERVICE)


def wait_until(check: Callable[[], bool], what: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} within {timeout_s:g} s")
        time.sleep(1)


@contextlib.contextmanager
def watching(broker: Broker) -> Iterator[Watcher]:
    watcher = Watcher(broker)
    try:
        yield watcher
    finally:
        watcher.close()


def test_the_unit_runs_the_service_as_an_installation_does(installation: Installation) -> None:
    """Step 2: `systemctl start` returns once the service reported itself ready, sandboxed as the
    unit says; it comes online with the radio connected and passes its own check. Step 16: the
    broker restarts, the service comes back by itself. It keeps the watchdog fed longer than
    `WatchdogSec`; step 17: stopped (SIGSTOP), it is restarted by the watchdog."""
    started = run("systemctl", "start", SERVICE, timeout=120)
    assert started.returncode == 0, started.stderr
    assert show("ActiveState") == "active"
    assert show("User") == SERVICE
    with watching(installation.broker) as watcher:
        watcher.wait("meshcore/radio1/availability", lambda p: p == b"online", 30)
        watcher.wait("meshcore/radio1/state", lambda p: json.loads(p)["radio"] == "connected", 30)
        watcher.wait("homeassistant/device/meshcore_home_gateway_radio1/config", timeout=30)

    doctor = run(str(COMMAND), "doctor", timeout=60)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "the gateway is online" in doctor.stdout

    stop_mosquitto(installation.mosquitto)
    time.sleep(2)  # the service notices the lost connection
    installation.mosquitto = start_mosquitto(installation.conf, installation.broker.port)
    with watching(installation.broker) as watcher:
        watcher.wait("meshcore/radio1/availability", lambda p: p == b"online", 60)
        watcher.wait("meshcore/radio1/state", lambda p: json.loads(p)["radio"] == "connected", 30)

    pid = show("MainPID")
    time.sleep(WATCHDOG_S + 10)  # fed by the service, the watchdog does not fire
    assert (show("NRestarts"), show("MainPID")) == ("0", pid)

    stopped = run("systemctl", "kill", "--kill-whom=main", "--signal=SIGSTOP", SERVICE)
    assert stopped.returncode == 0, stopped.stderr
    wait_until(lambda: show("NRestarts") == "1", "no restart by the watchdog", WATCHDOG_S + 60)
    wait_until(lambda: show("ActiveState") == "active", "not active again", 60)
    assert show("MainPID") != pid
    journal = run("journalctl", "--unit", SERVICE, "--no-pager", "--output", "cat").stdout
    assert "Watchdog timeout" in journal
    with watching(installation.broker) as watcher:
        watcher.wait("meshcore/radio1/availability", lambda p: p == b"online", 60)
