"""Shared fixtures. The broker fixture starts a private mosquitto on a free local port; telemetry's
first-start hold is skipped unless a test is marked `first_start_hold`."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from meshcore_home_gateway import telemetry_poller

SKIP_BROKER = os.environ.get("MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS") == "1"
# The firmware tests need the firmware built with a C++ compiler: `make firmware-test` runs them by
# naming the directory, which pytest then collects; `make test` leaves them out.
collect_ignore = ["firmware"]


@dataclass(frozen=True)
class Broker:
    host: str
    port: int


@pytest.fixture(autouse=True)
def no_first_start_hold(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry asks nobody for an hour the first time it runs on a state directory: only the
    tests marked `first_start_hold` wait for it."""
    if request.node.get_closest_marker("first_start_hold") is None:
        monkeypatch.setattr(telemetry_poller, "FIRST_START_S", 0.0)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mosquitto() -> str:
    found = shutil.which("mosquitto") or shutil.which("mosquitto", path="/usr/sbin:/usr/local/sbin")
    if found is None:
        pytest.fail(
            "mosquitto not found: install it (apt install mosquitto) or set "
            "MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1 to skip the broker tests deliberately"
        )
    return found


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    yield from _run_broker(tmp_path, anonymous=True)


@pytest.fixture
def refusing_broker(tmp_path: Path) -> Iterator[Broker]:
    """A broker that refuses every client (no anonymous access, no users)."""
    yield from _run_broker(tmp_path, anonymous=False)


def _run_broker(tmp_path: Path, *, anonymous: bool) -> Iterator[Broker]:
    if SKIP_BROKER:
        pytest.skip("MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1")
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous {str(anonymous).lower()}\npersistence false\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(  # noqa: S603 - fixed arguments, test only
        [_mosquitto(), "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and process.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        process.kill()
        _, errors = process.communicate(timeout=5)  # also closes the pipe
        pytest.fail(f"mosquitto did not start: {errors.decode(errors='replace').strip()}")
    try:
        yield Broker("127.0.0.1", port)
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()
