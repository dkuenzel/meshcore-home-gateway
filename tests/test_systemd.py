"""sd_notify client: readiness, watchdog and status messages to systemd."""

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshcore_home_gateway.systemd import notify, watchdog_interval_s


@pytest.fixture
def notify_socket(tmp_path: Path) -> Iterator[socket.socket]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(str(tmp_path / "notify"))
        sock.settimeout(1.0)
        yield sock


def test_notify_sends_a_datagram(notify_socket: socket.socket, tmp_path: Path) -> None:
    assert notify("READY=1", environ={"NOTIFY_SOCKET": str(tmp_path / "notify")})
    assert notify_socket.recv(100) == b"READY=1"


def test_notify_supports_the_abstract_namespace() -> None:
    name = f"meshcore-home-gateway-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        sock.settimeout(1.0)
        assert notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": "@" + name})
        assert sock.recv(100) == b"WATCHDOG=1"


def test_notify_without_systemd_does_nothing() -> None:
    assert not notify("READY=1", environ={})


def test_notify_failure_is_reported_not_raised(tmp_path: Path) -> None:
    assert not notify("READY=1", environ={"NOTIFY_SOCKET": str(tmp_path / "missing")})


def test_watchdog_interval_for_this_process() -> None:
    env = {"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid())}
    assert watchdog_interval_s(env) == pytest.approx(30.0)


def test_watchdog_meant_for_another_process_is_ignored() -> None:
    assert watchdog_interval_s({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "1"}) is None
    assert watchdog_interval_s({}) is None
