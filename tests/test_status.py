"""The state document, its health section and systemd's status (status.py)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import health as health_module
from meshcore_home_gateway import status as status_module
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.history import History
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.radio.session import default_session_factory
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    CommandLedger,
    JsonStateFile,
    TelemetryExclusions,
)
from meshcore_home_gateway.wiring import gateway
from tests.bridge_harness import (
    ALICE,
    FakeBus,
    Unreachable,
    fast_timers,  # noqa: F401 - autouse
    full_disk,
    idle_bridge,
    make_config,
    raise_a_bug,
    running,
    scenario,
    until,
)
from tests.radio_emulator import EmulatedRadio


def test_state_discovery_and_systemd_notifications(tmp_path: Path) -> None:
    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, notes):
            state = await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            assert state["battery_v"] == pytest.approx(4.012)
            assert state["radio_name"] == "Bridge"
            assert state["spreading_factor"] == 8
            assert any(t.endswith("/config") for t, _, _ in bus.published)
            before = sum(t.endswith("/config") for t, _, _ in bus.published)
            for callback in bus.on_ha_online:
                callback()
            await asyncio.sleep(0.05)  # the gateway's document and that of its contact controls
            assert sum(t.endswith("/config") for t, _, _ in bus.published) == before + 2
            assert all(retain for t, _, retain in bus.published if t.endswith("/state"))
            assert not any(retain for t, _, retain in bus.published if t.endswith("/delivery"))
        assert notes.index("READY=1") < notes.index("STOPPING=1")
        assert any(note.startswith("STATUS=radio connected") for note in notes)
        assert bus.stopped
        assert bus.payloads("/state")[-1]["radio"] == "disconnected"

    scenario(main)


def test_watchdog_is_pinged_at_half_its_interval_and_the_state_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "watchdog_interval_s", lambda: 0.1)
    monkeypatch.setattr(bridge_module, "STATUS_INTERVAL_S", 0.15)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, notes):
            pings, states = notes.count("WATCHDOG=1"), len(bus.payloads("/state"))
            await asyncio.sleep(0.6)
            assert notes.count("WATCHDOG=1") - pings >= 9  # every 0.05 s
            assert len(bus.payloads("/state")) - states >= 3  # every 0.15 s

    scenario(main)


def test_the_state_says_why_telemetry_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`paused` with `no_radio` while the radio is away; no reason while it runs or is off."""
    monkeypatch.setattr(telemetry, "STEP_S", 0.1)  # the step that finds the radio comes soon

    async def main() -> None:
        async with running(
            make_config(telemetry=True), EmulatedRadio(), tmp_path, transport=Unreachable
        ) as (_, bus, _notes):
            state = await bus.wait_for("/state", lambda s: True)
            assert (state["telemetry"], state["telemetry_reason"]) == ("paused", "no_radio")
        async with running(make_config(telemetry=True), EmulatedRadio(), tmp_path) as (
            bridge,
            _bus,
            _notes,
        ):
            await until(lambda: bridge.status.payload()["telemetry"] == "on")
            assert bridge.status.payload()["telemetry_reason"] is None
        async with running(make_config(), EmulatedRadio(), tmp_path) as (bridge, _bus, _notes):
            state = bridge.status.payload()
            assert (state["telemetry"], state["telemetry_reason"]) == ("off", None)

    scenario(main)


def test_the_state_follows_the_telemetry_status_when_the_radio_comes_and_goes(
    tmp_path: Path,
) -> None:
    """The poller steps when the radio connects and the state follows its status at once, not
    a step of a minute later; the state published as the radio goes away says `no_radio`."""
    radio = EmulatedRadio()

    def on(state: dict[str, Any]) -> bool:
        return (state["radio"], state["telemetry"]) == ("connected", "on")

    async def main() -> None:
        async with running(make_config(telemetry=True), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/state", on)
            since = len(bus.payloads("/state"))
            radio.disconnect()
            state = await bus.wait_for(
                "/state", lambda s: s["radio"] == "disconnected", since=since
            )
            assert (state["telemetry"], state["telemetry_reason"]) == ("paused", "no_radio")
            await bus.wait_for("/state", on, since=since)

    scenario(main)


def test_a_bug_in_the_telemetry_state_leaves_the_rest_of_the_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    bridge = gateway(
        make_config(telemetry=True),
        FakeBus(),
        EmulatedRadio,
        CommandLedger(store),
        session_factory=default_session_factory,
    )
    poller = bridge._telemetry
    assert poller is not None

    def broken() -> tuple[float, float]:
        raise RuntimeError("bug")

    poller.used_s = broken  # type: ignore[method-assign]
    state = bridge.status.payload()
    assert (state["telemetry"], state["telemetry_reason"]) == (None, None)
    assert state["telemetry_airtime_1h_s"] is None
    assert state["radio"] == "disconnected"
    assert "telemetry: state unavailable" in caplog.text


def test_the_state_tells_the_gateways_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each part's trouble in the `health` section; `degraded` while one is not as it should
    be, which the status line of systemd says too. Events the broker did not take are told,
    and do not degrade: the broker was away. The free space is the disk's at first, then a
    fixed one: other processes write to the disk between two reads."""
    config = make_config(extra="[history]\nenabled = false")
    bridge, bus, notes = idle_bridge(tmp_path, config)
    real = bridge.status.payload()["health"]["disk_free_mb"]
    assert isinstance(real, int)
    assert real >= 0
    free = 5_421
    fixed = shutil.disk_usage(tmp_path)._replace(free=free * 1_000_000 + 999_999)
    monkeypatch.setattr(shutil, "disk_usage", lambda _directory: fixed)
    health = bridge.status.payload()["health"]
    assert health == {
        "degraded": False,
        "history": "off",
        "state_file": "ok",
        "exclusions": "ok",
        "errors_1h": 0,
        "loop_lag_max_s": None,
        "events_dropped_1h": 0,
        "disk_free_mb": free,
    }
    bus.dropped = 3
    bridge.status.publish()
    assert bridge.status.payload()["health"]["events_dropped_1h"] == 3
    assert notes[-1] == "STATUS=radio disconnected, queue 0"

    def degraded(**expected: Any) -> None:
        health = bridge.status.payload()["health"]
        assert health["degraded"]
        assert {key: health[key] for key in expected} == expected
        bridge.status.publish()
        assert notes[-1] == "STATUS=radio disconnected, queue 0, degraded"

    bridge._ledger.store.failed = True  # a write of state.json failed
    degraded(state_file="unwritable")
    bridge._ledger.store.failed = False
    bridge.status.lag.record(1.5, 100.0)
    degraded(loop_lag_max_s=1.5)
    bridge.status.lag = type(bridge.status.lag)()
    try:
        raise_a_bug()
    except RuntimeError:
        bridge_module.log.exception("observer failed")
    degraded(errors_1h=1)
    health_module.FAILURES.reset()
    monkeypatch.setattr(status_module, "DISK_LOW_MB", free + 1_000_000)
    degraded(disk_free_mb=free)


def test_the_health_of_the_history_and_the_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history: `off` when switched off, `broken` when it could not be opened or a write
    failed. The exclusions: `unsaved` while a change waits for its file, `unreadable` when the
    file cannot be read (telemetry is paused)."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    observer = Observer(FakeBus(), Topics("meshcore", "radio1"), history=history)
    bridge, _, _ = idle_bridge(tmp_path, make_config(), observer=observer)
    assert bridge.status.payload()["health"]["history"] == "ok"
    history.broken = True
    assert bridge.status.payload()["health"]["history"] == "broken"
    history.close()
    without, _, _ = idle_bridge(tmp_path, make_config())  # enabled, but none was opened
    assert without.status.payload()["health"]["history"] == "broken"

    unsaved = TelemetryExclusions(tmp_path / "saved" / EXCLUSIONS_FILE)
    with monkeypatch.context() as patch:
        patch.setattr(JsonStateFile, "save", full_disk)
        assert not unsaved.exclude(ALICE.public_key.hex())
    unreadable_path = tmp_path / EXCLUSIONS_FILE
    unreadable_path.write_text("{not json", encoding="utf-8")
    for exclusions, expected in (
        (unsaved, "unsaved"),
        (TelemetryExclusions(unreadable_path), "unreadable"),
    ):
        observer = Observer(FakeBus(), Topics("meshcore", "radio1"), exclusions=exclusions)
        config = make_config(extra="[history]\nenabled = false")
        bridge, _, _ = idle_bridge(tmp_path, config, observer=observer)
        health = bridge.status.payload()["health"]
        assert (health["exclusions"], health["degraded"]) == (expected, True)


def test_a_stalled_event_loop_is_measured_and_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CODE-001: the host holds the loop up (here a blocking sleep): the probe measures it, the
    log says what it means, the health section keeps the hour's largest."""
    monkeypatch.setattr(status_module, "LAG_TICK_S", 0.02)
    caplog.set_level(logging.WARNING)
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, _bus, _notes):
            await asyncio.sleep(0.1)
            assert bridge.status.payload()["health"]["loop_lag_max_s"] < 0.5
            asyncio.get_running_loop().call_soon(time.sleep, 1.1)
            await asyncio.sleep(0.1)
            assert bridge.status.payload()["health"]["loop_lag_max_s"] >= 1.0

    scenario(main)
    assert "the event loop was held up for 1." in caplog.text
