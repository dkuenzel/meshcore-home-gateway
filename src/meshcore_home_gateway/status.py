"""The state document and systemd's status line (docs/mqtt-api.md, State; ARC-009): the radio,
the queue and the airtime, the telemetry, and how the gateway itself is doing (ARC-012)."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.telemetry import TelemetryStatus
from meshcore_home_gateway.health import DISK_LOW_MB, FAILURES, LAG_TICK_S, LAG_WARN_S, LoopLag
from meshcore_home_gateway.mqtt.payloads import encode
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.ports import Bus
from meshcore_home_gateway.protocol.links import contact_link
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.state import JsonStateFile
from meshcore_home_gateway.telemetry_poller import TelemetryPoller

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out


class Status:
    """Builds the state document and publishes it with systemd's status line."""

    def __init__(
        self,
        config: Config,
        bus: Bus,
        topics: Topics,
        *,
        radio: RadioLink,
        outbox: Delivery,
        observer: Observer,
        store: JsonStateFile,
        telemetry: TelemetryPoller | None,
        notifier: Callable[[str], bool],
        monotonic: Callable[[], float],
        wall_clock: Callable[[], float],
        state_dir: Path | None = None,
    ) -> None:
        """`state_dir`: where the state and the history are kept, for the free space in the
        health section; None: not measured (tests)."""
        self.config = config
        self._bus, self._topics, self._radio = bus, topics, radio
        self._outbox, self._observer, self._store = outbox, observer, store
        self._telemetry = telemetry
        self._notify, self._mono, self._wall = notifier, monotonic, wall_clock
        self._state_dir = state_dir
        self.lag = LoopLag()  # how late the event loop ran (CODE-001)
        # called with the state document each time it is published: the Homie adapter
        self.on_document: list[Callable[[str, dict[str, Any] | None], None]] = []

    def payload(self) -> dict[str, Any]:
        info, session = self._radio.info, self._radio.session
        connected = self._radio.ready.is_set()
        battery = session.battery if (session and connected) else None
        offset = session.clock_offset_s if (session and connected) else None
        now, budget = self._mono(), self._outbox.budget
        return {
            "radio": "connected" if connected else "disconnected",
            "battery_v": round(battery.millivolts / 1000, 3) if battery else None,
            # the radio's clock minus the host's: it cannot be set back (SEC-002)
            "radio_clock_offset_s": offset,
            # over TCP: another client took the link in the last hour (SEC-003); null over USB
            "radio_link_contested": self._radio.watch.contested(self._wall()),
            "airtime_1h_s": round(budget.used_s(now), 3),
            "airtime_available_s": round(budget.available_s(now), 3),
            "airtime_budget_s_per_hour": budget.per_hour_s,
            "queue": self._outbox.queued(),
            "radio_name": info.name if info else None,
            "public_key": info.public_key if info else None,
            "model": info.device.model if info else None,
            "firmware": info.device.version if info else None,
            "frequency_mhz": info.self_info.frequency_khz / 1000 if info else None,
            "bandwidth_khz": info.self_info.bandwidth_hz / 1000 if info else None,
            "spreading_factor": info.self_info.spreading_factor if info else None,
            "coding_rate": info.self_info.coding_rate if info else None,
            "tx_power_dbm": info.self_info.tx_power_dbm if info else None,
            # adds the gateway in the MeshCore app (as a QR code): it needs no write, no airtime
            "contact_link": contact_link(info.name, info.public_key) if info else None,
            "version": __version__,
            **self._telemetry_state(),
            "health": self._health_section(),
        }

    def _health_section(self) -> dict[str, Any]:
        """How the gateway itself is doing (ARC-012): `degraded` while any part is not as it
        should be, which the log tells in detail (docs/mqtt-api.md, State)."""
        history = self._observer.history
        history_state = (
            "off"
            if not self.config.history.enabled
            else "broken"
            if history is None or history.broken
            else "ok"
        )
        state_file = "unwritable" if self._store.failed else "ok"
        exclusions = self._observer.exclusions.health
        errors, lag = FAILURES.last_hour(), self.lag.worst()
        disk = _free_mb(self._state_dir)
        degraded = (
            history_state == "broken"
            or state_file != "ok"
            or exclusions != "ok"
            or errors > 0
            or (lag is not None and lag >= LAG_WARN_S)
            or (disk is not None and disk < DISK_LOW_MB)
        )
        return {
            "degraded": degraded,
            "history": history_state,
            "state_file": state_file,
            "exclusions": exclusions,
            "errors_1h": errors,
            "loop_lag_max_s": None if lag is None else round(lag, 3),
            "events_dropped_1h": self._bus.dropped_1h(),
            "disk_free_mb": disk,
        }

    def _telemetry_state(self) -> dict[str, Any]:
        off: TelemetryStatus = "off"
        state: dict[str, Any] = {
            "telemetry": off,
            "telemetry_reason": None,
            "telemetry_airtime_1h_s": None,
            "telemetry_mesh_airtime_1h_s": None,
        }
        if self._telemetry is None:
            return state
        try:
            own, mesh = self._telemetry.used_s()
            return {
                "telemetry": self._telemetry.status,
                "telemetry_reason": self._telemetry.reason,  # why it is paused
                "telemetry_airtime_1h_s": round(own, 3),
                "telemetry_mesh_airtime_1h_s": round(mesh, 3),
            }
        except Exception:  # the rest of the state goes out
            log.exception("telemetry: state unavailable")
            return {**state, "telemetry": None}

    def publish(self) -> None:
        state = self.payload()
        self._bus.publish(self._topics.state, encode(state), retain=True)
        for listener in self.on_document:
            listener(self._topics.state, state)
        degraded = ", degraded" if state["health"]["degraded"] else ""
        self._notify(f"STATUS=radio {state['radio']}, queue {state['queue']}{degraded}")

    async def measure_lag(self) -> None:
        """How late the event loop runs (CODE-001): a host that gives the gateway no CPU, or a
        disk that holds it up, shows here before it shows as a radio that does not answer."""
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + LAG_TICK_S
            await asyncio.sleep(LAG_TICK_S)
            now = loop.time()
            if self.lag.record(now - due, now):
                log.warning(
                    "the event loop was held up for %.1f s: the host gave the gateway no CPU, or "
                    "its disk was slow (a backup, swapping)",
                    now - due,
                )


def _free_mb(directory: Path | None) -> int | None:
    """Free space in MB where the state and the history are kept; None unknown."""
    if directory is None:
        return None
    try:
        return shutil.disk_usage(directory).free // 1_000_000
    except OSError:
        return None
