"""What the tests of the bridge and its parts share: a fake bus, the configuration, the bridge
running on an emulated radio (`running`), and the autouse fixture that shortens its timers
(`fast_timers`; a test module imports it)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import announcer as announcer_module
from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import observer as observer_module
from meshcore_home_gateway import outbox as outbox_module
from meshcore_home_gateway.bridge import Bridge
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.core.telemetry import LaneFactory
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.ports import ContactRequest, RadioEvent, SendRequest, Transport
from meshcore_home_gateway.protocol.codes import Command
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import CompanionSession, default_session_factory
from meshcore_home_gateway.state import (
    AnnouncedComponents,
    CommandLedger,
    ContactDeviceRecords,
    JsonStateFile,
    TelemetryRecords,
)
from meshcore_home_gateway.wiring import gateway
from tests.radio_emulator import START_TIME, EmulatedContact, EmulatedRadio

NOW = START_TIME + 100


GATEWAY_DOCUMENT = "/meshcore_home_gateway_radio1/config"  # its main discovery document


ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")


BOB = EmulatedContact(bytes.fromhex("b2" * 32), "Bob", out_path_len=0x02)


TWIN = EmulatedContact(ALICE.public_key[:6] + bytes(range(26)), "Mallory")  # same prefix


def make_config(
    extra: str = "",
    messages: str = "",
    airtime: str = "",
    radio: str = 'transport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"',
    *,
    telemetry: bool | None = False,
) -> Config:
    """Telemetry is off unless asked for, so the radio traffic stays exact; None: no
    `[telemetry]` table, the default (on)."""
    table = "" if telemetry is None else f"[telemetry]\nenabled = {str(telemetry).lower()}"
    return parse_config(
        f"""
[radio]
{radio}
[mqtt]
host = "broker.example.org"
node_id = "radio1"
insecure = true
[targets.alerts]
channel = 0
[targets.alice]
contact = "{ALICE.public_key.hex()}"
[targets.bob]
contact = "{BOB.public_key.hex()}"
[commands]
senders = ["{ALICE.public_key.hex()}"]
words = ["status", "arm"]
{table}
[messages]
dedup_window_s = 300
{messages}
[airtime]
{airtime}
{extra}
"""
    )


class FakeBus:
    def __init__(self) -> None:
        self.requests: asyncio.Queue[SendRequest] = asyncio.Queue()
        self.contact_requests: asyncio.Queue[ContactRequest] = asyncio.Queue()
        self.on_connected: list[Callable[[], None]] = []
        self.on_ha_online: list[Callable[[], None]] = []
        self.on_ha_offline: list[Callable[[], None]] = []
        self.on_ha_device: list[Callable[[str, bytes, bool], None]] = []
        self.on_show_hidden: list[Callable[[], None]] = []
        self.published: list[tuple[str, bytes, bool]] = []
        self.qos: dict[str, set[int]] = {}
        self.correlations: list[tuple[str, Any, bytes]] = []  # MQTT 5 (ARC-014)
        self.stopped = False
        self.connected = True
        self.dropped = 0  # events of the last hour the broker did not take

    async def start(self) -> None:
        for callback in self.on_connected:
            callback()

    async def stop(self) -> None:
        self.stopped = True

    def dropped_1h(self) -> int:
        return self.dropped

    def publish(
        self,
        topic: str,
        payload: bytes,
        retain: bool = False,
        qos: int = 1,
        *,
        correlation: bytes | None = None,
    ) -> None:
        self.published.append((topic, payload, retain))
        self.qos.setdefault(topic, set()).add(qos)
        if correlation is not None:
            self.correlations.append((topic, json.loads(payload), correlation))

    def send(
        self, alias: str, text: str | bytes, retained: bool = False, *, contact: bool = False
    ) -> None:
        payload = text.encode() if isinstance(text, str) else text
        self.requests.put_nowait(SendRequest(alias, payload, retained, contact=contact))

    def payloads(self, suffix: str) -> list[dict[str, Any]]:
        """Payloads on the topics ending in `suffix`; those of single contacts only if the suffix
        names contacts."""
        return [
            json.loads(p)
            for t, p, _ in self.published
            if t.endswith(suffix) and ("contact" in suffix or "/contact" not in t)
        ]

    async def wait_for(
        self,
        suffix: str,
        check: Callable[[dict[str, Any]], bool],
        within_s: float = 3,
        since: int = 0,
    ) -> dict[str, Any]:
        """First payload on the topic (from index `since`) that passes `check`."""

        async def poll() -> dict[str, Any]:
            while True:
                for payload in self.payloads(suffix)[since:]:
                    if check(payload):
                        return payload
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), within_s)


def fast_sessions(
    transport: Transport, events: asyncio.Queue[RadioEvent], set_clock: bool
) -> CompanionSession:
    return CompanionSession(
        transport,
        events,
        wall_clock=lambda: NOW,
        command_timeout_s=0.5,
        flush_settle_s=0.01,
        set_clock=set_clock,
    )


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 0.05)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 0.15)
    monkeypatch.setattr(bridge_module, "RECONNECT_MIN_S", 0.02)
    monkeypatch.setattr(announcer_module, "DISCOVERY_DELAY_S", (0.0, 0.0))
    monkeypatch.setattr(announcer_module, "ANNOUNCE_MIN_INTERVAL_S", 0)  # its own test has it
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.05)
    # the nodes document follows every change: the tests check its content, not its rate
    monkeypatch.setattr(observer_module, "NODES_DOCUMENT_INTERVAL_S", 0.0)


@asynccontextmanager
async def running(
    config: Config,
    radio: EmulatedRadio,
    tmp_path: Path,
    transport: Callable[[], Transport] | None = None,
    *,
    sessions: Callable[..., CompanionSession] = fast_sessions,
    bus: FakeBus | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = lambda: NOW,
    observer: Callable[[FakeBus], Observer] | None = None,
    devices: bool = False,
    profile: RadioProfile | None = None,
    telemetry_lanes: LaneFactory | None = None,
) -> AsyncIterator[tuple[Bridge, FakeBus, list[str]]]:
    bus = bus or FakeBus()
    stop, notes = asyncio.Event(), []

    def note(message: str) -> bool:
        notes.append(message)
        return True

    store = new_run(tmp_path)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    contact_devices = None
    if devices:
        records = ContactDeviceRecords(store)
        contact_devices = ContactDevices(config, topics, bus, records, wall_clock=lambda: NOW)
    bridge = gateway(
        config,
        bus,
        transport or (lambda: radio),
        CommandLedger(store),
        session_factory=sessions,
        wall_clock=wall_clock,
        monotonic=monotonic,
        notifier=note,
        components=AnnouncedComponents(store),
        observer=observer(bus) if observer else None,
        devices=contact_devices,
        profile=profile,
        manage_components=AnnouncedComponents(store, "manage_contacts"),
        telemetry_records=TelemetryRecords(store),
        telemetry_lanes=telemetry_lanes,
    )
    task = asyncio.create_task(bridge.run(stop))
    try:
        if transport is None:
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
        yield bridge, bus, notes
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def scenario(coro: Callable[[], Any]) -> None:
    asyncio.run(asyncio.wait_for(coro(), 20))


_RUNS: dict[Path, JsonStateFile] = {}  # the state file of the last run per directory


def new_run(tmp_path: Path, *, crash: bool = False) -> JsonStateFile:
    """The state file of a run that starts on `tmp_path`, one for all its sections as the
    service has (wiring.serve), after the last run there stopped and wrote its state; after a
    `crash` what it had not written yet is lost."""
    if (last := _RUNS.pop(tmp_path, None)) is not None and not crash:
        assert last.flush()
    store = _RUNS[tmp_path] = JsonStateFile(tmp_path / "state.json")
    return store


def run_state(tmp_path: Path) -> JsonStateFile:
    """The state file of the run on `tmp_path`, which an observer built for it shares."""
    return _RUNS[tmp_path]


FINAL = {"confirmed", "failed", "expired", "rejected", "duplicate"}


class Unreachable:
    """A radio that is not plugged in."""

    description = "unreachable"
    needs_flush = False

    async def open(self) -> None:
        raise FileNotFoundError("no such device")

    async def read(self) -> bytes:
        raise ConnectionError

    async def write(self, data: bytes) -> None:
        raise ConnectionError

    async def close(self) -> None:
        return None


NEIGHBOUR = EmulatedContact(bytes.fromhex("c3" * 32), "Carol", out_path_len=0x00)


FORGED = "\u202e\nERROR x\x1b[31m\u2028"  # direction override, line breaks, terminal escape


def births(bus: FakeBus, count: int) -> None:
    for _ in range(count):
        for birth in bus.on_ha_online:
            birth()


def gateway_documents(bus: FakeBus) -> int:
    return sum(t.endswith(GATEWAY_DOCUMENT) for t, _, _ in bus.published)


async def until(check: Callable[[], bool], within_s: float = 3.0) -> None:
    deadline = time.monotonic() + within_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class OfflineBus(FakeBus):
    """A broker that is never reached in this run."""

    def __init__(self) -> None:
        super().__init__()
        self.connected = False

    async def start(self) -> None:
        pass


class InFlightRadio(EmulatedRadio):
    """Takes a message out of its queue when SYNC_NEXT_MESSAGE arrives, as the firmware does; the
    answer reaches the host a few milliseconds later (the transfer time)."""

    armed = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.synced = asyncio.Event()

    def _handle(self, p: bytes) -> None:
        if self.armed and p[0] == Command.SYNC_NEXT_MESSAGE and self.queue:
            self.received.append(p)
            asyncio.get_running_loop().call_later(0.005, self.emit, self.queue.pop(0))
            self.synced.set()
            return
        super()._handle(p)


def idle_bridge(
    tmp_path: Path, config: Config, *, observer: Observer | None = None
) -> tuple[Bridge, FakeBus, list[str]]:
    """A bridge that is not run: its state document as the status loop publishes it."""
    bus, notes = FakeBus(), []
    ledger = CommandLedger(JsonStateFile(tmp_path / "state.json"))

    def note(message: str) -> bool:
        notes.append(message)
        return True

    bridge = gateway(
        config,
        bus,
        EmulatedRadio,
        ledger,
        session_factory=default_session_factory,
        notifier=note,
        observer=observer,
        state_dir=tmp_path,
    )
    return bridge, bus, notes


def raise_a_bug() -> None:
    raise RuntimeError("a bug")


def full_disk(*_: object) -> None:
    raise OSError(28, "No space left on device")
