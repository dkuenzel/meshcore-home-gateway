"""Minimal client for the systemd notification protocol (sd_notify), without libsystemd.

Protocol: a datagram with newline-separated KEY=VALUE pairs to the AF_UNIX socket named in
$NOTIFY_SOCKET; a leading "@" denotes the abstract namespace. See sd_notify(3).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping


def notify(message: str, environ: Mapping[str, str] | None = None) -> bool:
    """Send `message` (e.g. "READY=1") to systemd; False if not under systemd or on error."""
    env = os.environ if environ is None else environ
    address = env.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
    except OSError:
        return False
    return True


def watchdog_interval_s(environ: Mapping[str, str] | None = None) -> float | None:
    """The watchdog timeout systemd expects pings within, or None if no watchdog applies."""
    env = os.environ if environ is None else environ
    usec = env.get("WATCHDOG_USEC")
    pid = env.get("WATCHDOG_PID")
    if not usec or (pid and int(pid) != os.getpid()):
        return None
    return int(usec) / 1_000_000
