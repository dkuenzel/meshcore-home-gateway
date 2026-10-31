"""Fixtures of the firmware tests: the firmware that `make firmware` built, the simulated air and
nodes on it. `make firmware-test` runs these tests; `make test` does not collect them
(tests/conftest.py)."""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from meshcore_home_gateway import bridge
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.airtime import Costs
from meshcore_home_gateway.core.budget import AirtimeBudget
from tests.conftest import Broker
from tests.firmware.air import AirThread
from tests.firmware.gateway import Watcher, configuration
from tests.firmware.node import Node, start_node

ROOT = Path(__file__).resolve().parents[2]
SOURCES = Path(__file__).with_name("sources.toml")
# the build directory of `make firmware`; the make target passes it on
BUILD = Path(
    os.environ.get("MESHCORE_HOME_GATEWAY_FIRMWARE", ROOT / "build" / "firmware")
).resolve()
LOG_LINES = 60  # of each log in the report of a failed test
# the schedule's intervals in the scenarios (fast_schedule): a request about every 15 s
SCHEDULE_S = 15.0


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


@dataclass
class Scene:
    """What a scenario with the gateway uses: the air, its nodes, the broker with a client that
    keeps what the gateway publishes, the test's directory and the service's log."""

    air: AirThread
    nodes: Nodes
    broker: Broker
    watcher: Watcher
    directory: Path
    caplog: pytest.LogCaptureFixture

    def configuration(self, radio: Node, more: str = "") -> str:
        return configuration(radio.serial, self.broker, more)

    @property
    def state_dir(self) -> Path:
        return self.directory / "state"


@pytest.fixture
def scene(
    air: AirThread, nodes: Nodes, broker: Broker, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Scene]:  # fmt: skip
    caplog.set_level(logging.DEBUG, logger="meshcore_home_gateway")
    watcher = Watcher(broker)
    try:
        yield Scene(air, nodes, broker, watcher, tmp_path, caplog)
    finally:
        watcher.close()


@pytest.fixture
def fast_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The telemetry schedule in seconds instead of minutes, so a scenario fits in a test: the
    airtime lanes start full and refill a second a second, every interval is SCHEDULE_S, a
    retry comes after 10 s, and the state document every 2 s. Nothing else changes: the 10 s
    quiet after a message, the 10 s gap after every outcome, the answer waits, the flood limits,
    the routes. The intervals of the hardware checklist need a clock of their own
    (docs/testing.md)."""

    def full_lanes(capacity: Costs, now: float) -> tuple[AirtimeBudget, AirtimeBudget]:
        own, mesh = capacity.own_s, capacity.mesh_s
        return AirtimeBudget(3600.0, own, now), AirtimeBudget(3600.0, mesh, now)

    monkeypatch.setattr(telemetry, "build_lanes", full_lanes)
    for interval in ("MOVING_MIN_S", "RESTING_MIN_S", "UNLOCATED_S", "PROBE_S"):
        monkeypatch.setattr(telemetry, interval, SCHEDULE_S)
    for share in ("MOVING_SHARE", "RESTING_SHARE"):
        monkeypatch.setattr(telemetry, share, 1.0)  # the interval is never the airtime's
    monkeypatch.setattr(telemetry, "RETRY_S", 10.0)
    monkeypatch.setattr(bridge, "STATUS_INTERVAL_S", 2.0)
