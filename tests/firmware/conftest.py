"""Fixtures of the firmware tests: the firmware that `make firmware` built, the simulated air and
nodes on it. `make firmware-test` runs these tests; `make test` does not collect them
(tests/conftest.py)."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.firmware.air import AirThread
from tests.firmware.node import Node, start_node

ROOT = Path(__file__).resolve().parents[2]
SOURCES = Path(__file__).with_name("sources.toml")
# the build directory of `make firmware`; the make target passes it on
BUILD = Path(
    os.environ.get("MESHCORE_HOME_GATEWAY_FIRMWARE", ROOT / "build" / "firmware")
).resolve()
LOG_LINES = 60  # of each log in the report of a failed test


@dataclass(frozen=True)
class Firmware:
    companion: Path
    repeater: Path
    version: str  # what the companion reports, from the pinned tag: companion-v1.18.0 is v1.18.0


def pinned_companion_version() -> str:
    pins = tomllib.loads(SOURCES.read_text(encoding="utf-8"))
    (tag,) = [t for t in pins["meshcore"]["tags"] if t.startswith("companion-")]
    return str(tag).removeprefix("companion-")


@pytest.fixture(scope="session")
def firmware() -> Firmware:
    binaries = BUILD / "bin"
    found = Firmware(binaries / "companion", binaries / "repeater", pinned_companion_version())
    for binary in (found.companion, found.repeater):
        if not os.access(binary, os.X_OK):
            pytest.fail(f"{binary} is missing: `make firmware-test` fetches and builds it")
    return found


@pytest.fixture
def air(tmp_path: Path) -> Iterator[AirThread]:
    """The air, with its packet log in air.log of the test's directory."""
    with AirThread(tmp_path / "air.log") as running:
        yield running


class Nodes:
    """Starts nodes on the air, each in a directory named after it; stops them all at the end.
    Node IDs count from 1; a node's seed is its ID, so its keys are the same at every run.

    A node is ready when it is returned. With `wait=False` it boots while the next one starts;
    `ready()` then waits for all of them (a node without GPS boots in about 5 s)."""

    def __init__(self, firmware: Firmware, air: AirThread, directory: Path) -> None:
        self._firmware = firmware
        self._air = air
        self.directory = directory
        self.started: list[Node] = []

    def companion(self, name: str, *, gps: str | None = None, wait: bool = True) -> Node:
        return self._start(self._firmware.companion, name, gps, wait=wait)

    def repeater(self, name: str, *, gps: str | None = None, wait: bool = True) -> Node:
        return self._start(self._firmware.repeater, name, gps, wait=wait)

    def ready(self) -> None:
        for node in self.started:
            node.wait_ready()

    def _start(self, binary: Path, name: str, gps: str | None, *, wait: bool) -> Node:
        node_id = len(self.started) + 1
        node = start_node(
            binary, self.directory / name, name=name, node_id=node_id, air_port=self._air.port,
            seed=node_id, gps=gps,
        )  # fmt: skip
        self.started.append(node)
        if wait:
            node.wait_ready()
        return node

    def stop(self) -> None:
        for node in self.started:
            node.stop()


NODES = pytest.StashKey[Nodes]()


@pytest.fixture
def nodes(
    firmware: Firmware, air: AirThread, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Nodes]:
    started = Nodes(firmware, air, tmp_path)
    request.node.stash[NODES] = started
    try:
        yield started
    finally:
        started.stop()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """A failed test's report ends with the last lines of each node's log and of the air's."""
    report = yield
    started = item.stash.get(NODES, None)
    if report.failed and started is not None:
        logs = [(f"log of {n.name}", n.log) for n in started.started]
        for title, path in [*logs, ("air", started.directory / "air.log")]:
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                report.sections.append((title, "\n".join(lines[-LOG_LINES:])))
    return report


@pytest.fixture
def companion(nodes: Nodes) -> Node:
    """Radio A of the hardware checklist: the companion on the gateway's serial port."""
    return nodes.companion("A")
