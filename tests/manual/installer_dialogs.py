"""Manual check: the installer's default dialog flow with the real whiptail.

The automated tests drive the installer with a whiptail stub. This script runs the real whiptail
in a pseudo-terminal against the same stub Proxmox commands and types the answers, to catch
differences between the stub and the real program (screen output, option parsing). It is not
collected by pytest because it sends keys on timing, which is too fragile for CI.

    .venv/bin/python -I tests/manual/installer_dialogs.py

Expected output: the resulting configuration's targets and broker settings, exit code 0.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tests import test_installer as stubs  # noqa: E402

KEYS = [  # one entry per dialog, in the order of the default setup
    b"\r",  # setup: default
    b"\r",  # radio: first device
    b"broker.example.org\r",  # broker address
    b"\r",  # user: default
    b"pw-secret\r",  # password
    b"\r",  # create the container: yes
    b" \x1b[B \r",  # channels: tick the first two
    b" \r",  # contacts: tick the first
    b"\r",  # command senders: none
    b"\r",  # summary box
]
QUIET_S = 0.7  # a dialog is waiting when the screen has not changed for this long


def main() -> int:
    if shutil.which("whiptail") is None:
        print("whiptail is not installed", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        host = stubs.install_stubs(root, real=frozenset({"whiptail"}))
        code = drive(root, host)
        print("exit code", code)
        config = host.config()
        print("targets", sorted((t.alias, t.channel_index, t.public_key) for t in config.targets))
        print(
            "broker",
            config.mqtt.host,
            config.mqtt.username,
            "password set:",
            bool(config.mqtt.password),
        )
        print("password in the call log:", "pw-secret" in host.log_text())
        return code


def drive(root: Path, host: stubs.Host) -> int:
    master, slave = pty.openpty()
    env = {
        **os.environ, "TERM": "xterm", "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
        "STUB": str(root), "ALICE": stubs.ALICE, "RELAY": stubs.RELAY,
        "MESHCORE_HOME_GATEWAY_SERIAL_DIR": str(host.serial.parent),
        "COLUMNS": "100", "LINES": "30",
    }  # fmt: skip
    process = subprocess.Popen(  # noqa: S603 - our script
        [stubs.BASH, str(stubs.SCRIPT)], stdin=slave, stdout=slave, stderr=slave, env=env,
        start_new_session=True,
    )  # fmt: skip
    os.close(slave)
    keys, deadline = list(KEYS), time.monotonic() + 60
    while process.poll() is None and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], QUIET_S)
        if ready:
            try:
                os.read(master, 65536)
            except OSError:
                break
        elif keys:
            os.write(master, keys.pop(0))
    process.wait(timeout=5)
    os.close(master)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
