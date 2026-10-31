"""A firmware node as a process on the simulated air (board/runtime.cpp has its environment).

Each node has a directory of its own: `flash/` (its file system), `serial` (a symlink to its
pseudo terminal, the USB serial port) and `node.log` (its packet log, debug output and, for the
repeater, its command line's replies). Control lines on its standard input steer the board:
`gps <lat> <lon> [<alt>]`, `gps nofix`, `gps detach`, `batt <mV>`, `temp <C>`, `reboot`, and
`advert` presses a companion's Advert button. The end of its input stops it, so no node outlives
the test that started it.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

READY = "setup done"  # board/runtime.cpp, after the firmware's setup()
START_S = 10.0
STOP_S = 5.0


class NodeError(Exception):
    pass


@dataclass
class Node:
    name: str
    node_id: int  # on the air
    directory: Path
    process: subprocess.Popen[bytes]

    @property
    def serial(self) -> Path:
        return self.directory / "serial"

    @property
    def log(self) -> Path:
        return self.directory / "node.log"

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def control(self, line: str) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise NodeError(f"{self.name} is not running")
        self.process.stdin.write(line.encode() + b"\n")
        self.process.stdin.flush()

    def wait_ready(self, timeout: float = START_S, *, starts: int = 1) -> None:
        """Until the firmware's setup() is done (the `starts`th time) and the serial port is
        there. A node without GPS takes about 5 s: the firmware looks that long for one."""
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            if self.serial.is_symlink() and self.log_text().count(READY) >= starts:
                return
            time.sleep(0.02)
        self.stop()
        tail = "\n".join(self.log_text().splitlines()[-20:])
        raise NodeError(f"{self.name} did not start, exit {self.process.returncode}:\n{tail}")

    def reboot(self) -> None:
        """Restart the firmware on the same flash and serial port, and wait for its setup()."""
        starts = self.log_text().count(READY)
        self.control("reboot")
        self.wait_ready(starts=starts + 1)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(STOP_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(STOP_S)
        if self.process.stdin is not None:
            self.process.stdin.close()


def start_node(
    binary: Path,
    directory: Path,
    *,
    name: str,
    node_id: int,
    air_port: int,
    seed: int | None = None,
    gps: str | None = None,
) -> Node:
    """Start the firmware; `Node.wait_ready()` waits for its setup() to finish.

    `seed` gives the node the same keys at every start; `gps` is `"lat,lon,alt"` or `"nofix"`
    (a GPS module without a fix); without it the board has no GPS.
    """
    directory.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SIM_NAME": name,
        "SIM_NODE_ID": str(node_id),
        "SIM_AIR_PORT": str(air_port),
        "SIM_FS_DIR": str(directory / "flash"),
        "SIM_PTY_LINK": str(directory / "serial"),
    }
    if seed is not None:
        env["SIM_SEED"] = str(seed)
    if gps is not None:
        env["SIM_GPS"] = gps
    with (directory / "node.log").open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - the firmware built from the pinned sources
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=directory,
        )
    return Node(name, node_id, directory, process)
