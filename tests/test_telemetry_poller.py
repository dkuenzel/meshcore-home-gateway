"""Telemetry from the radio's companions end to end (telemetry_poller.py, ADR 0018): the bridge
with the emulated radio and its remote companions, and the poller's parts on their own; its
steps also on a stand-in radio with a clock the test moves (exclusion, states, events and the
first-start hold)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import telemetry_poller as poller_module
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core import lpp, telemetry
from meshcore_home_gateway.core.airtime import Costs, meshcore_lora
from meshcore_home_gateway.core.budget import AirtimeBudget
from meshcore_home_gateway.core.mesh import ANSWERED_SLACK_S, Heard, Signal
from meshcore_home_gateway.core.telemetry import Candidate, Poll
from meshcore_home_gateway.history import History
from meshcore_home_gateway.mqtt.client import ContactRequest
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Command, Push
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.session import (
    CompanionSession,
    NotSentError,
    RadioError,
    TelemetryReceived,
    TelemetrySent,
)
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    CurrentDocuments,
    JsonStateFile,
    TelemetryExclusions,
    TelemetryRecords,
)
from meshcore_home_gateway.telemetry_poller import AnswerWaiter, OutboxActivity, TelemetryPoller
from meshcore_home_gateway.views import iso
from tests.radio_emulator import (
    LPP_BME280,
    LPP_FULL,
    SELF_KEY,
    START_TIME,
    EmulatedContact,
    EmulatedRadio,
    RemoteCompanion,
    telemetry_push,
)
from tests.test_bridge import (
    ALICE,
    BOB,
    NOW,
    FakeBus,
    OfflineBus,
    Unreachable,
    make_config,
    running,
    scenario,
)

ROOT = "meshcore/radio1"
DANA = EmulatedContact(bytes.fromhex("d4" * 32), "Dana", out_path_len=0x00)  # a neighbour
DANA_TWIN = EmulatedContact(DANA.public_key[:6] + bytes(26), "Mallory", out_path_len=0x00)
KEY = DANA.public_key.hex()
LOGGER = "meshcore_home_gateway.telemetry_poller"


def config(extra: str = "") -> Config:
    return make_config(extra=extra, telemetry=True)


def wide(capacity: Costs, now: float) -> tuple[AirtimeBudget, AirtimeBudget]:
    return AirtimeBudget(3600.0, 100.0, now), AirtimeBudget(3600.0, 1000.0, now)


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MIN_S", 0.05)
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MAX_S", 0.15)
    monkeypatch.setattr(bridge_module, "RECONNECT_MIN_S", 0.02)
    monkeypatch.setattr(bridge_module, "DISCOVERY_DELAY_S", (0.0, 0.0))
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.05)
    monkeypatch.setattr(poller_module, "QUIET_S", 0.05)
    monkeypatch.setattr(poller_module, "HOLD_MAX_S", 1.0)
    monkeypatch.setattr(poller_module, "BUSY_PAUSE_S", 0.3)
    monkeypatch.setattr(poller_module, "OUTDATED_WAIT_S", 0.05)
    monkeypatch.setattr(telemetry, "POLL_GAP_S", 0.05)
    monkeypatch.setattr(telemetry, "RETRY_S", 0.2)
    monkeypatch.setattr(telemetry, "STEP_S", 0.2)
    monkeypatch.setattr(telemetry, "WAIT_MIN_S", 0.3)
    monkeypatch.setattr(telemetry, "WAIT_FACTOR", 0.0)


@pytest.fixture
def quick_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A companion at rest is asked every 0.5 s, one gone silent at least every 0.5 s."""
    monkeypatch.setattr(telemetry, "RESTING_MIN_S", 0.5)
    monkeypatch.setattr(telemetry, "RESTING_SHARE", 1e9)
    monkeypatch.setattr(telemetry, "AWAY_S", 0.5)


async def until(check: object, within_s: float = 3.0) -> None:
    deadline = time.monotonic() + within_s
    while not check():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def requests(radio: EmulatedRadio) -> int:
    return radio.commands().count(Command.SEND_TELEMETRY_REQ)


def positioned(document: dict[str, object]) -> bool:
    return document.get("position_source") == "telemetry"


# --- end to end -------------------------------------------------------------------------------


def test_a_companion_answers_and_its_position_reaches_its_document_and_tracker(
    tmp_path: Path,
) -> None:
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()})

    async def main() -> None:
        async with running(config(), radio, tmp_path, devices=True, telemetry_lanes=wide) as (
            bridge, bus, _notes,
        ):  # fmt: skip
            document = await bus.wait_for(f"/contacts/{KEY}", positioned)
            assert (document["latitude"], document["longitude"]) == (50.12345, 8.56775)
            assert (document["altitude_m"], document["position_accuracy_m"]) == (120.5, 15.0)
            assert document["heard_via"] == "telemetry"
            device = await bus.wait_for(f"/contact_{KEY}/config", lambda d: "position" in d["cmps"])
            assert device["cmps"]["position"]["p"] == "device_tracker"
            assert radio.telemetry_requests == [(DANA.public_key, False)]
            state = bridge.state_payload()
            assert state["telemetry"] == "on"
            assert state["telemetry_airtime_1h_s"] == pytest.approx(0.378, abs=0.001)
            assert state["telemetry_mesh_airtime_1h_s"] == pytest.approx(0.887, abs=0.001)

    scenario(main)


def test_telemetry_is_off_unless_enabled(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, _bus, _notes):
            await asyncio.sleep(0.3)
            assert requests(radio) == 0
            state = bridge.state_payload()
            assert (state["telemetry"], state["telemetry_airtime_1h_s"]) == ("off", None)

    scenario(main)


def test_a_contact_without_a_path_is_flooded_only_once_it_was_heard(tmp_path: Path) -> None:
    """ADR 0018: direct only; a flood for a contact heard recently, which teaches both sides a
    path. Its answer is taken on the table read after the path it sent."""
    remote = RemoteCompanion(hops=1)
    radio = EmulatedRadio(contacts=[ALICE], remotes={ALICE.public_key: remote})
    key = ALICE.public_key.hex()

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await asyncio.sleep(0.4)
            assert requests(radio) == 0  # never heard: not flooded
            radio.advertise(ALICE)
            document = await bus.wait_for(f"/contacts/{key}", positioned)
            assert radio.telemetry_requests == [(ALICE.public_key, True)]
            assert (document["path_known"], document["path_hops"]) == (True, 1)
            assert no_path_writes(radio)

    scenario(main)


def no_path_writes(radio: EmulatedRadio) -> bool:
    """Telemetry never resets or writes a path, nor any contact."""
    writes = {Command.RESET_PATH, Command.ADD_UPDATE_CONTACT, Command.REMOVE_CONTACT}
    return not radio.path_resets and not writes & set(radio.commands())


def test_a_live_message_lets_a_contact_without_a_path_be_flooded(tmp_path: Path) -> None:
    """A message as it arrives is a sign of life; one fetched at connect is not: it may have
    waited in the radio for hours."""
    radio = EmulatedRadio(contacts=[ALICE], remotes={ALICE.public_key: RemoteCompanion()})
    radio.queue_direct_message(ALICE.public_key[:6], "old news", START_TIME - 7200, push=False)

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await bus.wait_for("/received", lambda d: d["text"] == "old news")
            await asyncio.sleep(0.4)
            assert requests(radio) == 0
            radio.queue_direct_message(ALICE.public_key[:6], "home soon", START_TIME + 50)
            await bus.wait_for(f"/contacts/{ALICE.public_key.hex()}", positioned)
            assert radio.telemetry_requests == [(ALICE.public_key, True)]

    scenario(main)


def test_the_advert_of_a_node_added_later_counts(tmp_path: Path) -> None:
    """A companion is heard advertising, then added (from Home Assistant, without a path): its
    advert counts as heard, so it is flooded."""
    radio = EmulatedRadio(remotes={ALICE.public_key: RemoteCompanion()})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            radio.advertise(ALICE)  # NEW_ADVERT: not a contact yet
            await bus.wait_for("/nodes", lambda d: d["total"] == 1)
            radio.contacts.append(replace(ALICE))
            radio.emit(bytes([Push.CONTACTS_FULL]))  # the radio's table changed: read again
            await bus.wait_for(f"/contacts/{ALICE.public_key.hex()}", positioned)
            assert radio.telemetry_requests == [(ALICE.public_key, True)]

    scenario(main)


def test_a_flood_whose_sent_never_came_counts_as_sent(tmp_path: Path) -> None:
    """The request was written, then the link broke: the radio may have flooded it, so it
    counts, and the reconnect does not flood the contact again."""
    radio = EmulatedRadio(
        contacts=[ALICE], remotes={ALICE.public_key: RemoteCompanion()},
        drop_on={Command.SEND_TELEMETRY_REQ},
    )  # fmt: skip

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _):
            radio.advertise(ALICE)
            await until(lambda: requests(radio) == 1)
            await bus.wait_for("/state", lambda d: d["radio"] == "disconnected")
            await bus.wait_for("/state", lambda d: d["radio"] == "connected")
            await asyncio.sleep(0.6)  # three retry times
            assert requests(radio) == 1
            assert bridge.state_payload()["telemetry_mesh_airtime_1h_s"] > 20

    scenario(main)


@pytest.mark.parametrize("same_write", [False, True], ids=["after-sent", "before-sent"])
def test_the_path_of_its_own_flood_is_no_sign_of_life(tmp_path: Path, same_write: bool) -> None:
    """Only a path the contact sent by itself counts as heard by other means: the 0x81 of the
    gateway's own flood comes with the answer, after SENT as on the radio, or even before the
    poller sees SENT."""
    remote = RemoteCompanion(hops=1, same_write=same_write)
    radio = EmulatedRadio(contacts=[ALICE], remotes={ALICE.public_key: remote})
    key = ALICE.public_key.hex()

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _n):
            poller = bridge._telemetry
            assert poller is not None
            radio.advertise(ALICE)
            await until(lambda: key in poller._heard)
            advert = poller._heard[key]
            await bus.wait_for(f"/contacts/{key}", positioned)
            assert poller._heard[key] == advert
            radio.emit(bytes([0x81]) + ALICE.public_key)  # a path it sent on its own
            await until(lambda: poller._heard[key] > advert)

    scenario(main)


def test_look_alikes_are_never_asked_and_an_unasked_answer_is_dropped(tmp_path: Path) -> None:
    remotes = {DANA.public_key: RemoteCompanion(), DANA_TWIN.public_key: RemoteCompanion()}
    radio = EmulatedRadio(contacts=[DANA, DANA_TWIN], remotes=remotes)

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            radio.emit(telemetry_push(DANA.public_key, LPP_FULL))
            await asyncio.sleep(0.4)
            assert requests(radio) == 0
            assert not any(positioned(d) for d in bus.payloads(f"/contacts/{KEY}"))

    scenario(main)


def test_an_answer_when_a_look_alike_came_during_the_wait_is_not_taken(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, LOGGER)
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion(auto=False)})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await until(lambda: radio.telemetry_requests)
            radio.contacts.append(replace(DANA_TWIN))  # the radio added it; its advert says so
            radio.advertise(DANA_TWIN)
            radio.answer_telemetry()
            await until(lambda: "answer not taken (not a unique contact)" in caplog.text)
            assert not any(positioned(d) for d in bus.payloads(f"/contacts/{KEY}"))

    scenario(main)


def test_silence_retries_once_then_backs_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, quick_intervals: None
) -> None:
    caplog.set_level(logging.INFO, LOGGER)
    remote = RemoteCompanion()
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: remote})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            remote.lpp = None  # it went away
            await until(lambda: "telemetry d4d4d4d4d4d4: away" in caplog.text)
            states = [r.getMessage() for r in caplog.records if r.name == LOGGER]
            assert states == [
                "telemetry d4d4d4d4d4d4: resting", "telemetry d4d4d4d4d4d4: retrying",
                "telemetry d4d4d4d4d4d4: away",
            ]  # fmt: skip
            document = bus.payloads(f"/contacts/{KEY}")[-1]
            assert document["latitude"] == 50.12345  # the last position stays
            assert no_path_writes(radio)

    scenario(main)


def test_a_late_answer_is_taken_until_the_next_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, LOGGER)
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion(auto=False)})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await until(lambda: "telemetry d4d4d4d4d4d4: no answer" in caplog.text)  # 0.3 s
            radio.answer_telemetry()
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            assert requests(radio) == 1
            assert "telemetry d4d4d4d4d4d4: resting" in caplog.text

    scenario(main)


def test_messages_go_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No request while the outbox has a message, waits for its acknowledgement, or had one
    in the last QUIET_S."""
    monkeypatch.setattr(poller_module, "QUIET_S", 0.3)
    radio = EmulatedRadio(
        contacts=[DANA, ALICE], remotes={DANA.public_key: RemoteCompanion()}, auto_ack=False
    )
    bus = FakeBus()
    bus.send("alerts", "intruder")  # before the radio is there
    bus.send("alice", "are you home?")  # never acknowledged: three attempts

    async def main() -> None:
        async with running(config(), radio, tmp_path, bus=bus, telemetry_lanes=wide):
            await until(lambda: requests(radio) == 1, within_s=5)
            commands = radio.commands()
            texts = (Command.SEND_TXT_MSG, Command.SEND_CHANNEL_TXT_MSG)
            last_text = max(i for i, c in enumerate(commands) if c in texts)
            assert commands.index(Command.SEND_TELEMETRY_REQ) > last_text
            assert commands.count(Command.SEND_TXT_MSG) == 3  # all attempts went first
            assert commands.count(Command.SEND_CHANNEL_TXT_MSG) == 1

    scenario(main)


def test_messages_on_an_ignored_channel_do_not_hold_requests_back(tmp_path: Path) -> None:
    """Nothing answers them: a busy channel the configuration ignores does not stop telemetry."""
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()})
    ignoring = make_config(messages="ignore_channels = [0]", telemetry=True)

    async def main() -> None:
        async with running(ignoring, radio, tmp_path, telemetry_lanes=wide):
            for i in range(150):  # one every 20 ms, for up to 3 s (QUIET_S is 50 ms here)
                radio.queue_channel_message(0, f"chatter {i}", START_TIME + i)
                await asyncio.sleep(0.02)
                if requests(radio):
                    break
            assert requests(radio) == 1

    scenario(main)


@pytest.mark.parametrize(
    ("radio_kwargs", "status"),
    [
        ({"unknown": {Command.SEND_TELEMETRY_REQ}}, "unsupported"),  # ERR 1
        ({"manual_add": False}, "paused"),  # the radio adds contacts on its own
        ({"tx_airtime_s": 200}, None),  # measured: but only from two readings on
    ],
)
def test_telemetry_stops_where_it_cannot_work(
    tmp_path: Path, radio_kwargs: dict[str, object], status: str | None
) -> None:
    radio = EmulatedRadio(
        contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()},
        **radio_kwargs,  # type: ignore[arg-type]
    )  # fmt: skip

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, _b, _n):
            await asyncio.sleep(0.5)
            if status is None:
                assert requests(radio) == 1
                return
            assert requests(radio) <= 1
            assert bridge.state_payload()["telemetry"] == status

    scenario(main)


def timed(radio: EmulatedRadio) -> list[float]:
    """The times of the telemetry requests the radio gets."""
    times: list[float] = []
    handle = radio._handle

    def recording(p: bytes) -> None:
        if p[0] == Command.SEND_TELEMETRY_REQ:
            times.append(time.monotonic())
        handle(p)

    radio._handle = recording  # type: ignore[method-assign]
    return times


def test_a_busy_radio_pauses_the_polls(tmp_path: Path) -> None:
    """ERR 3: no packet buffer free; nothing was sent or charged, and every request waits 60 s,
    also to another companion."""
    eve = EmulatedContact(bytes.fromhex("e5" * 32), "Eve", out_path_len=0x00)
    remotes = {DANA.public_key: RemoteCompanion(), eve.public_key: RemoteCompanion()}
    radio = EmulatedRadio(
        contacts=[DANA, eve], remotes=remotes, errors={Command.SEND_TELEMETRY_REQ: 3}
    )
    times = timed(radio)

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _n):
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            await bus.wait_for(f"/contacts/{eve.public_key.hex()}", positioned)
            assert requests(radio) == 3
            assert times[1] - times[0] >= poller_module.BUSY_PAUSE_S
            used = bridge.state_payload()["telemetry_airtime_1h_s"]
            assert used == pytest.approx(2 * 0.378, abs=0.01)  # the refused one cost nothing

    scenario(main)


def test_a_contact_the_radio_lacks_waits_for_the_next_table_read(tmp_path: Path) -> None:
    """ERR 2: the table as read is behind the radio's; the contact is skipped until a read."""
    radio = EmulatedRadio(
        contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()},
        errors={Command.SEND_TELEMETRY_REQ: 2},
    )  # fmt: skip

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await until(lambda: requests(radio) == 1)
            await asyncio.sleep(0.6)  # three retry times
            assert requests(radio) == 1
            radio.emit(bytes([Push.CONTACTS_FULL]))
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            assert requests(radio) == 2

    scenario(main)


def test_the_radios_own_time_on_air_pauses_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The backstop: from 180 s of the radio's measured transmit time in an hour, nothing is
    asked; the warning comes once, although the number changes with every reading."""
    caplog.set_level(logging.INFO, LOGGER)
    monkeypatch.setattr(session_module, "HEARTBEAT_INTERVAL_S", 0.05)
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()})

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, _bus, _notes):  # empty lanes
            radio.tx_airtime_s = 41 + 180
            await until(lambda: bridge.state_payload()["telemetry"] == "paused")
            radio.tx_airtime_s = 41 + 200
            await asyncio.sleep(0.3)
            assert requests(radio) == 0
            assert caplog.text.count("telemetry paused") == 1

    scenario(main)


def test_a_message_queued_while_a_request_waits_for_the_radio_withdraws_it(
    tmp_path: Path, quick_intervals: None
) -> None:
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _):
            await bus.wait_for(f"/contacts/{KEY}", positioned)  # the first; the next in 0.5 s
            session, poller = bridge.live_session(), bridge._telemetry
            assert session is not None
            assert poller is not None
            async with session._lock:  # another command holds the radio
                await until(lambda: poller._sending == KEY)
                bus.send("alerts", "intruder")
                await until(lambda: bridge._activity.busy)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            await until(lambda: requests(radio) == 2)
            commands = radio.commands()
            second = [i for i, c in enumerate(commands) if c == Command.SEND_TELEMETRY_REQ][1]
            assert commands.index(Command.SEND_CHANNEL_TXT_MSG) < second

    scenario(main)


@pytest.mark.parametrize("same_write", [False, True], ids=["after-sent", "before-sent"])
def test_a_flooded_answer_is_charged_from_the_packet_log(tmp_path: Path, same_write: bool) -> None:
    remote = RemoteCompanion(flood_reply=True, same_write=same_write)
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: remote})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _n):
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            mesh = bridge.state_payload()["telemetry_mesh_airtime_1h_s"]
            assert mesh == pytest.approx(0.887 + 11.75, abs=0.01)

    scenario(main)


def test_a_disconnect_during_the_wait_counts_no_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, LOGGER)
    remote = RemoteCompanion(auto=False)
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: remote})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await until(lambda: requests(radio) == 1)
            radio.disconnect()
            remote.auto = True
            await bus.wait_for(f"/contacts/{KEY}", positioned, within_s=5)
            assert "retrying" not in caplog.text
            assert "no answer" not in caplog.text

    scenario(main)


def test_the_schedule_is_saved_at_the_stop_and_a_restart_asks_nobody_early(
    tmp_path: Path,
) -> None:
    def radio() -> EmulatedRadio:
        return EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()})

    first = radio()

    async def run_first() -> None:
        async with running(config(), first, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await bus.wait_for(f"/contacts/{KEY}", positioned)

    scenario(run_first)
    saved = JsonStateFile(tmp_path / "state.json").load()["telemetry"]
    entry = saved["contacts"][KEY]
    assert entry["due_in_s"] == pytest.approx(900.0, abs=5)  # at rest: in 15 minutes
    assert (entry["located"], entry["failures"]) == (True, 0)
    assert "latitude" not in json.dumps(saved)
    second = radio()

    async def run_second() -> None:
        async with running(config(), second, tmp_path, telemetry_lanes=wide):
            await asyncio.sleep(0.4)
            assert requests(second) == 0

    scenario(run_second)


# --- end to end: readings, exclusion, states and events (ADR 0024) -----------------------------

BME280_READINGS = {
    "ch2_temperature_c": 21.3, "ch2_humidity_percent": 55.5,
    "ch2_barometric_pressure_hpa": 1013.2, "ch2_altitude_m": 120.0,
}  # fmt: skip


def bme280_radio(*contacts: EmulatedContact, auto: bool = True) -> EmulatedRadio:
    """Dana's handheld with an environment sensor on channel 2."""
    remote = RemoteCompanion(lpp=LPP_BME280, auto=auto)
    return EmulatedRadio(contacts=[DANA, *contacts], remotes={DANA.public_key: remote})


def events(bus: FakeBus) -> list[dict[str, Any]]:
    """The `telemetry` events."""
    return bus.payloads(f"{ROOT}/telemetry")


def ask(bus: FakeBus, state: bytes, key: str = KEY) -> None:
    bus.contact_requests.put_nowait(ContactRequest("telemetry", state, False, key))


def keeping(tmp_path: Path, closing: list[History] | None = None) -> Callable[[FakeBus], Observer]:
    """An observer as the service builds it: the exclusions in their own file, the current
    documents in the state file, and a history when `closing` takes it."""

    def observer(bus: FakeBus) -> Observer:
        history = None
        if closing is not None:
            history = History.open(tmp_path / "history.sqlite3")
            closing.append(cast(History, history))
        exclusions = TelemetryExclusions(tmp_path / EXCLUSIONS_FILE)
        current = CurrentDocuments(JsonStateFile(tmp_path / "state.json"))
        return Observer(bus, Topics("meshcore", "radio1"), history=history,
                        exclusions=exclusions, wall_clock=lambda: NOW, current=current)  # fmt: skip

    return observer


def test_an_answer_brings_its_readings_to_the_document_and_the_device(tmp_path: Path) -> None:
    """Her first answer: the readings, her device's sensors at once, and `answered`."""
    radio = bme280_radio()

    async def main() -> None:
        async with running(config(), radio, tmp_path, devices=True, telemetry_lanes=wide) as (
            _bridge, bus, _notes,
        ):  # fmt: skip
            document = await bus.wait_for(f"/contacts/{KEY}", lambda d: d["readings"] != {})
            assert (document["battery_v"], document["mcu_temperature_c"]) == (4.12, 23.4)
            assert document["readings"] == BME280_READINGS
            assert document["readings_at"] == iso(NOW)
            assert (document["telemetry_state"], document["telemetry_current"]) == (
                "answering", True,
            )  # fmt: skip
            assert document["position_current"] is True
            device = await bus.wait_for(f"/contact_{KEY}/config", lambda d: "battery" in d["cmps"])
            assert {"battery", "mcu_temperature", "ch2_temperature", "ch2_humidity",
                    "ch2_barometric_pressure", "ch2_altitude"} <= set(device["cmps"])  # fmt: skip
            await bus.wait_for("/telemetry", lambda e: True)
            assert events(bus) == [{
                "event_type": "answered", "key": KEY, "prefix": KEY[:12], "name": "Dana",
                "state": "answering",
            }]  # fmt: skip

    scenario(main)


def test_an_excluded_companion_is_not_asked_also_after_a_restart(
    tmp_path: Path, quick_intervals: None
) -> None:
    path = tmp_path / EXCLUSIONS_FILE
    first = bme280_radio()

    async def run_first() -> None:
        async with running(config(), first, tmp_path, observer=keeping(tmp_path),
                           telemetry_lanes=wide) as (_bridge, bus, _notes):  # fmt: skip
            await bus.wait_for(f"/contacts/{KEY}", lambda d: d["battery_v"] is not None)
            ask(bus, b"off")
            event = await bus.wait_for("/contact", lambda e: e["request"] == "telemetry")
            assert (event["event_type"], event["key"], event["reason"]) == ("excluded", KEY, None)
            asked = requests(first)
            document = bus.payloads(f"/contacts/{KEY}")[-1]
            assert (document["battery_v"], document["mcu_temperature_c"]) == (None, None)
            assert (document["readings"], document["readings_at"]) == ({}, None)
            assert (document["telemetry_state"], document["telemetry_excluded"]) == (
                "excluded", True,
            )  # fmt: skip
            await asyncio.sleep(1.0)  # she would be asked every 0.5 s
            assert requests(first) == asked

    scenario(run_first)
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [KEY]}
    second = bme280_radio()

    async def run_second() -> None:
        async with running(config(), second, tmp_path, observer=keeping(tmp_path),
                           telemetry_lanes=wide) as (_bridge, bus, _notes):  # fmt: skip
            await asyncio.sleep(0.5)
            assert requests(second) == 0
            assert bus.payloads(f"/contacts/{KEY}")[-1]["telemetry_state"] == "excluded"
            ask(bus, b"on")
            event = await bus.wait_for("/contact", lambda e: e["request"] == "telemetry")
            assert (event["event_type"], event["reason"]) == ("included", None)
            back = await bus.wait_for("/telemetry", lambda e: True)
            assert (back["event_type"], back["state"]) == ("answered", "answering")
            assert requests(second) >= 1

    scenario(run_second)
    assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": []}


def test_an_answer_in_flight_when_she_is_excluded_is_not_published(tmp_path: Path) -> None:
    radio = bme280_radio(auto=False)

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await until(lambda: requests(radio) == 1)
            ask(bus, b"off")
            await bus.wait_for("/contact", lambda e: e["event_type"] == "excluded")
            radio.answer_telemetry()
            await asyncio.sleep(0.5)
            documents = bus.payloads(f"/contacts/{KEY}")
            assert all(d["battery_v"] is None and not positioned(d) for d in documents)
            assert events(bus) == []

    scenario(main)


def test_a_companion_that_stops_answering_raises_one_event_and_one_when_back(
    tmp_path: Path, quick_intervals: None
) -> None:
    radio = bme280_radio()
    remote = radio.remotes[DANA.public_key]

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await bus.wait_for("/telemetry", lambda e: e["event_type"] == "answered")
            remote.lpp = None  # out of range
            gone = await bus.wait_for("/telemetry", lambda e: e["event_type"] == "unanswered")
            assert gone["state"] == "away"
            document = bus.payloads(f"/contacts/{KEY}")[-1]
            assert (document["telemetry_state"], document["telemetry_current"]) == ("away", False)
            assert document["position_current"] is False
            assert document["battery_v"] == 4.12  # the values stay
            asked = requests(radio)
            await until(lambda: requests(radio) >= asked + 2)  # still asked, no second event
            assert [e["event_type"] for e in events(bus)] == ["answered", "unanswered"]
            remote.lpp = LPP_BME280
            back = await bus.wait_for("/telemetry", lambda e: True, since=2)
            assert (back["event_type"], back["state"]) == ("answered", "answering")

    scenario(main)


def test_answers_expire_while_telemetry_is_paused_without_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_intervals: None
) -> None:
    """Nobody was asked, so nobody left two requests unanswered: the state says `away`, no
    event comes, and the status says why."""
    monkeypatch.setattr(telemetry, "CURRENT_MIN_S", 1.0)  # her answers count 1.5 s
    radio = bme280_radio()

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _):
            await bus.wait_for("/telemetry", lambda e: e["event_type"] == "answered")
            radio.manual_add = False  # the radio adds contacts by itself, from its next start
            radio.disconnect()
            await until(lambda: bridge.state_payload()["telemetry_reason"] == "auto_add")
            asked = requests(radio)
            await bus.wait_for(f"/contacts/{KEY}", lambda d: d["telemetry_state"] == "away")
            assert requests(radio) == asked
            assert [e["event_type"] for e in events(bus)] == ["answered"]
            assert bridge.state_payload()["telemetry"] == "paused"

    scenario(main)


def test_the_state_says_paused_while_the_radio_is_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "STATUS_INTERVAL_S", 0.1)
    radio = bme280_radio()
    plugged = [True]

    def transport() -> EmulatedRadio | Unreachable:
        return radio if plugged[0] else Unreachable()

    async def main() -> None:
        async with running(config(), radio, tmp_path, transport, telemetry_lanes=wide) as (
            _bridge, bus, _notes,
        ):  # fmt: skip
            await bus.wait_for("/state", lambda s: s["telemetry"] == "on")
            plugged[0] = False
            radio.disconnect()
            since = len(bus.payloads("/state"))
            away = await bus.wait_for("/state", lambda s: s["radio"] == "disconnected"
                                      and s["telemetry"] == "paused", since=since)  # fmt: skip
            assert away["telemetry_reason"] == "no_radio"
            plugged[0] = True
            since = len(bus.payloads("/state"))
            back = await bus.wait_for("/state", lambda s: s["telemetry"] == "on", within_s=5,
                                      since=since)  # fmt: skip
            assert back["telemetry_reason"] is None

    scenario(main)


def test_with_telemetry_off_exclusions_are_kept_and_reported(tmp_path: Path) -> None:
    """A household can exclude its strangers before it switches telemetry on."""
    path = tmp_path / EXCLUSIONS_FILE
    radio = bme280_radio(ALICE)
    alice = ALICE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=keeping(tmp_path)) as (
            bridge, bus, _notes,
        ):  # fmt: skip
            await bus.wait_for(f"/contacts/{KEY}", lambda d: d["telemetry_state"] == "off")
            ask(bus, b"off")
            excluded = await bus.wait_for("/contact", lambda e: e["event_type"] == "excluded")
            assert excluded["reason"] is None
            assert bus.payloads(f"/contacts/{KEY}")[-1]["telemetry_state"] == "excluded"
            assert bus.payloads(f"/contacts/{alice}")[-1]["telemetry_state"] == "off"
            assert json.loads(path.read_text(encoding="utf-8")) == {"excluded": [KEY]}
            ask(bus, b"on")
            await bus.wait_for("/contact", lambda e: e["event_type"] == "included")
            assert bus.payloads(f"/contacts/{KEY}")[-1]["telemetry_state"] == "off"
            assert (requests(radio), bridge.state_payload()["telemetry"]) == (0, "off")

    scenario(main)


@pytest.mark.first_start_hold
def test_by_default_telemetry_asks_after_the_first_start_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `[telemetry]` table: the poller runs. On a state directory where it never ran, it
    asks nobody for FIRST_START_S after the radio's table is read."""
    monkeypatch.setattr(poller_module, "FIRST_START_S", 2.0)
    radio = bme280_radio()

    async def main() -> None:
        async with running(make_config(telemetry=None), radio, tmp_path, telemetry_lanes=wide) as (
            bridge, _bus, _notes,
        ):  # fmt: skip
            await until(lambda: bridge.state_payload()["telemetry_reason"] == "first_start")
            held = time.monotonic()
            assert requests(radio) == 0
            await until(lambda: requests(radio) == 1, within_s=5)
            assert time.monotonic() - held > 1.5
            state = bridge.state_payload()
            assert (state["telemetry"], state["telemetry_reason"]) == ("on", None)

    scenario(main)


def test_a_restart_flips_no_state_and_raises_no_event(tmp_path: Path) -> None:
    """The states come from the saved schedule and the readings from the history: nothing
    expires and nothing is reported again."""
    histories: list[History] = []
    first = bme280_radio()

    async def run_first() -> None:
        async with running(config(), first, tmp_path, observer=keeping(tmp_path, histories),
                           telemetry_lanes=wide) as (_bridge, bus, _notes):  # fmt: skip
            await bus.wait_for("/telemetry", lambda e: e["event_type"] == "answered")

    second = bme280_radio()

    async def run_second() -> None:
        async with running(config(), second, tmp_path, observer=keeping(tmp_path, histories),
                           telemetry_lanes=wide) as (_bridge, bus, _notes):  # fmt: skip
            await bus.wait_for(f"/contacts/{KEY}", lambda d: True)
            await asyncio.sleep(0.4)
            documents = bus.payloads(f"/contacts/{KEY}")
            assert {d["telemetry_state"] for d in documents} == {"answering"}
            for document in documents:
                assert (document["battery_v"], document["readings"]) == (4.12, BME280_READINGS)
                assert document["position_current"] is True
                received = document["position_received_at"]  # the age the schedule kept
                assert received in {iso(NOW - age) for age in range(3)}  # the test's clock stood
            assert events(bus) == []
            assert requests(second) == 0

    try:
        scenario(run_first)
        scenario(run_second)
    finally:
        for history in histories:
            history.close()


def test_the_documents_of_current_companions_are_stored_at_the_stop(tmp_path: Path) -> None:
    """A change of a current document's content waits for the observer's next tick, or the
    stop."""
    store = JsonStateFile(tmp_path / "state.json")
    radio = bme280_radio()
    buses: list[FakeBus] = []

    def observer(bus: FakeBus) -> Observer:
        buses.append(bus)
        return Observer(bus, TOPICS, current=CurrentDocuments(store), wall_clock=lambda: NOW)

    async def main() -> None:
        async with running(config(), radio, tmp_path, observer=observer, telemetry_lanes=wide) as (
            _bridge, bus, _notes,
        ):  # fmt: skip
            await bus.wait_for("/telemetry", lambda e: e["event_type"] == "answered")
            radio.advertise(DANA)
            await bus.wait_for(f"/contacts/{KEY}", lambda d: d["heard_via"] == "advert")
            assert CurrentDocuments(store).documents[KEY]["heard_via"] == "telemetry"

    scenario(main)
    assert CurrentDocuments(store).documents == {KEY: buses[0].payloads(f"/contacts/{KEY}")[-1]}


def test_an_expiry_reaches_the_broker_after_a_run_that_never_reached_it(tmp_path: Path) -> None:
    """Her bound passed while the gateway was stopped. The next run publishes her expired
    document before the broker connects, which MqttBus drops, and stops before it connects: the
    run after it publishes the expiry at its connect, the radio still away."""
    store = JsonStateFile(tmp_path / "state.json")

    async def answered() -> None:
        async with running(config(), bme280_radio(), tmp_path, observer=keeping(tmp_path),
                           telemetry_lanes=wide) as (_bridge, bus, _notes):  # fmt: skip
            await bus.wait_for("/telemetry", lambda e: e["event_type"] == "answered")

    async def unreached() -> None:
        async with running(config(), bme280_radio(), tmp_path, Unreachable, bus=OfflineBus(),
                           observer=keeping(tmp_path), telemetry_lanes=wide):  # fmt: skip
            await asyncio.sleep(0.2)

    published: list[dict[str, Any]] = []

    async def reached() -> None:
        async with running(config(), bme280_radio(), tmp_path, Unreachable, telemetry_lanes=wide,
                           observer=keeping(tmp_path)) as (_bridge, bus, _notes):  # fmt: skip
            await asyncio.sleep(0.2)
            published.extend(bus.payloads(f"/contacts/{KEY}"))

    scenario(answered)
    stored = CurrentDocuments(store).documents[KEY]
    assert stored["position_current"] is True
    saved = store.load()
    saved["telemetry"]["saved_at"] -= 2 * telemetry.CURRENT_MIN_S  # stopped two hours
    store.save(saved)
    scenario(unreached)
    scenario(reached)
    expired = {"telemetry_state": "away", "telemetry_current": False, "position_current": False}
    assert published == [{**stored, **expired}]


# --- parts ----------------------------------------------------------------------------------


def test_the_outbox_is_quiet_only_after_a_while_without_messages() -> None:
    async def main() -> None:
        queued = [0]
        activity = OutboxActivity(time.monotonic, lambda: queued[0])
        assert await activity.wait_quiet(0.05, timeout_s=0.1)  # never anything
        activity.touch()
        start = time.monotonic()
        assert await activity.wait_quiet(0.1, timeout_s=1.0)
        assert time.monotonic() - start >= 0.09
        queued[0] = 1
        assert activity.busy
        assert not await activity.wait_quiet(0.0, timeout_s=0.1)
        queued[0] = 0
        activity.delivering(True)
        waiting = asyncio.create_task(activity.wait_quiet(0.05, timeout_s=2.0))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        activity.delivering(False)  # wakes it; quiet 0.05 s later
        assert await waiting

    asyncio.run(main())


def answer_of(key: bytes, seq: int, *, session: int = 1, at: float = 0.0) -> TelemetryReceived:
    return TelemetryReceived(key[:6], LPP_FULL, at, seq, session)


def test_the_waiter_takes_the_answer_of_the_contact_asked() -> None:
    async def main() -> None:
        late: list[tuple[str, TelemetryReceived]] = []
        clock = [100.0]
        waiter = AnswerWaiter(lambda: clock[0], lambda k, e: late.append((k, e)))
        waiter.arm(KEY)
        waiter.sent(deadline=105.0, seq=10)
        waiter.resolve(answer_of(DANA_TWIN.public_key[:5] + b"\xff", 11))  # another prefix
        waiter.resolve(answer_of(DANA.public_key, 12))
        assert await waiter.wait(1.0) == answer_of(DANA.public_key, 12)
        waiter.resolve(answer_of(DANA.public_key, 13))  # a second copy: the slot was cleared
        assert late == []

    asyncio.run(main())


def test_the_waiter_orders_answers_against_sent() -> None:
    """Read before the new request's SENT: the previous request's late answer; after it: the
    new one's, also before the poller saw SENT."""

    async def main() -> None:
        late: list[tuple[str, TelemetryReceived]] = []
        waiter = AnswerWaiter(lambda: 100.0, lambda k, e: late.append((k, e)))
        waiter.arm(KEY)
        waiter.sent(deadline=100.0, seq=5)
        assert await waiter.wait(0.01) is None  # timed out: the last request is Dana's
        waiter.arm(KEY)
        waiter.resolve(answer_of(DANA.public_key, 8))  # before SENT 9: the previous one's
        waiter.resolve(answer_of(DANA.public_key, 10))  # after it: this one's
        waiter.sent(deadline=105.0, seq=9)
        assert await waiter.wait(1.0) == answer_of(DANA.public_key, 10)
        assert late == [(KEY, answer_of(DANA.public_key, 8))]

    asyncio.run(main())


def test_the_waiter_takes_a_late_answer_within_its_window_only() -> None:
    async def main() -> None:
        late: list[tuple[str, TelemetryReceived]] = []
        clock = [100.0]
        waiter = AnswerWaiter(lambda: clock[0], lambda k, e: late.append((k, e)))
        waiter.arm(KEY)
        waiter.sent(deadline=105.0, seq=1)
        assert await waiter.wait(0.01) is None
        clock[0] = 105.0 + telemetry.LATE_ACCEPT_S + 1
        waiter.resolve(answer_of(DANA.public_key, 2))
        assert late == []
        waiter.arm(KEY)
        waiter.sent(deadline=200.0, seq=3)
        assert await waiter.wait(0.01) is None
        clock[0] = 200.0 + telemetry.LATE_ACCEPT_S
        waiter.resolve(answer_of(DANA.public_key, 4))
        waiter.resolve(answer_of(DANA.public_key, 5))  # only once
        assert late == [(KEY, answer_of(DANA.public_key, 4))]
        waiter.arm(KEY)  # a request that was never sent keeps the window of the last one
        waiter.disarm()
        waiter.close()
        waiter.resolve(answer_of(DANA.public_key, 6))
        assert len(late) == 1

    asyncio.run(main())


def test_over_tcp_a_silence_during_a_table_read_still_running_counts_only_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, quick_intervals: None
) -> None:
    """The radio's WiFi queue drops pushes during a listing; a read that started during the
    wait and still runs when it ends counts too (the test radio is reached over TCP)."""
    caplog.set_level(logging.DEBUG, LOGGER)
    remote = RemoteCompanion()
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: remote})

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            remote.lpp = None
            radio.silent = {Command.GET_CONTACTS}  # the read runs until the session gives up
            await until(lambda: requests(radio) == 2)
            radio.advertise(DANA)  # a read 0.05 s later, still running when the wait ends
            await until(lambda: "no answer during a table read" in caplog.text)
            radio.silent = set()

    scenario(main)


class ReorderingRadio(EmulatedRadio):
    """Writes an older answer to the previous request before the next request's SENT."""

    def __init__(self, older: bytes, newer: bytes, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.older, self.newer = older, newer

    def _telemetry(self, key: bytes) -> None:
        if len(self.telemetry_requests) == 1:  # the second request
            self.emit(telemetry_push(key, self.older))
            super()._telemetry(key)
            self.emit(telemetry_push(key, self.newer))
            return
        super()._telemetry(key)


def test_an_older_answer_read_before_sent_never_replaces_a_newer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer to the previous request, read before this request's SENT, is taken as late
    only if it is not older than one already taken (frame numbers, not the wall clock)."""
    monkeypatch.setattr(telemetry, "PROBE_S", 0.3)  # the unanswered first request: again soon
    older = bytes.fromhex("018807a5f2014ead002f12")
    newer = bytes.fromhex("018807a62a014ead002f12")  # 56 cells (620 m) further north
    found = lpp.position(lpp.decode(newer))
    assert found is not None
    radio = ReorderingRadio(
        older, newer, contacts=[DANA], remotes={DANA.public_key: RemoteCompanion(lpp=None)}
    )

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (_b, bus, _n):
            await until(lambda: requests(radio) == 2, within_s=5)  # the first went unanswered
            document = await bus.wait_for(f"/contacts/{KEY}", positioned)
            assert document["latitude"] == found.position.latitude
            await asyncio.sleep(0.3)
            latest = bus.payloads(f"/contacts/{KEY}")[-1]
            assert latest["latitude"] == found.position.latitude

    scenario(main)


def test_a_direct_request_whose_sent_never_came_counts_and_the_polls_go_on(tmp_path: Path) -> None:
    radio = EmulatedRadio(
        contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()},
        drop_on={Command.SEND_TELEMETRY_REQ},
    )  # fmt: skip

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _):
            await bus.wait_for(f"/contacts/{KEY}", positioned, within_s=5)  # after the reconnect
            assert requests(radio) == 2
            used = bridge.state_payload()["telemetry_airtime_1h_s"]
            assert used == pytest.approx(2 * 0.378, abs=0.01)  # the lost one counted

    scenario(main)


@pytest.mark.parametrize("transport", ["tcp", "serial"])
def test_a_silence_during_a_table_read_counts_only_again_over_tcp(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, quick_intervals: None, transport: str
) -> None:
    caplog.set_level(logging.DEBUG, LOGGER)
    remote = RemoteCompanion()
    radio = EmulatedRadio(contacts=[DANA], remotes={DANA.public_key: remote})
    where = 'host = "192.0.2.10"' if transport == "tcp" else 'device = "/dev/ttyACM9"'
    settings = make_config(radio=f'transport = "{transport}"\n{where}', telemetry=True)

    async def main() -> None:
        async with running(settings, radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _):
            poller = bridge._telemetry
            assert poller is not None
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            remote.lpp = None
            await until(lambda: requests(radio) == 2)
            radio.advertise(DANA)  # a table read during the wait
            await until(lambda: "d4d4d4d4d4d4: retrying" in caplog.text
                        or "no answer during a table read" in caplog.text)  # fmt: skip
            counted = "d4d4d4d4d4d4: retrying" in caplog.text
            assert counted is (transport == "serial")

    scenario(main)


@pytest.mark.parametrize("message_waiting", [False, True])
def test_an_advert_from_before_the_restart_counts_as_heard(
    tmp_path: Path, message_waiting: bool
) -> None:
    """The history keeps when a contact last advertised: a companion added before telemetry
    was switched on (and the service restarted) is flooded while that advert is fresh, also
    when a message it sent meanwhile waited in the radio (fetched at connect: no sign of
    life, and it must not hide the advert)."""
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    advert = Heard(NOW - 600, "advert", Signal())
    history.node_heard(ALICE.public_key.hex(), "Alice", 1, advert, ALICE.last_advert)
    history.node_heard(DANA.public_key.hex(), "Dana", 1, Heard(NOW - 600, "message", Signal()))
    history.close()
    dana = replace(DANA, out_path_len=0xFF)  # without a path too, but last heard by a message
    remotes = {ALICE.public_key: RemoteCompanion(), dana.public_key: RemoteCompanion()}
    radio = EmulatedRadio(contacts=[ALICE, dana], remotes=remotes)
    if message_waiting:
        radio.queue_direct_message(ALICE.public_key[:6], "while you were away", START_TIME + 90,
                                   push=False)  # fmt: skip
    opened: list[History] = []

    def observer(bus: FakeBus) -> Observer:
        kept = History.open(path)
        assert kept is not None
        opened.append(kept)
        return Observer(bus, TOPICS, history=kept, wall_clock=lambda: NOW)

    async def main() -> None:
        async with running(config(), radio, tmp_path, observer=observer, telemetry_lanes=wide) as (
            _b, bus, _n,
        ):  # fmt: skip
            if message_waiting:
                await bus.wait_for("/received", lambda d: d["text"] == "while you were away")
            await bus.wait_for(f"/contacts/{ALICE.public_key.hex()}", positioned)
            await asyncio.sleep(0.3)
            assert radio.telemetry_requests == [(ALICE.public_key, True)]

    scenario(main)
    for kept in opened:
        kept.close()


def test_only_companions_count_among_the_nodes_heard(tmp_path: Path) -> None:
    hill = EmulatedContact(bytes.fromhex("c7" * 32), "Hill", node_type=2)
    radio = EmulatedRadio(contacts=[DANA])

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _):
            poller = bridge._telemetry
            assert poller is not None
            radio.advertise(hill)  # a repeater the radio did not add
            await bus.wait_for("/nodes", lambda d: d["total"] == 1)
            assert hill.public_key.hex() not in poller._heard

    scenario(main)


# --- the poller's parts with a stand-in session -----------------------------------------------

TOPICS = Topics("meshcore", "radio1")
EU = meshcore_lora(62_500.0, 8, 8)


class StandInSession:
    """What the poller reads of a session; `request_telemetry` fails as told."""

    def __init__(self, error: BaseException | None = None, *, write: bool = True) -> None:
        self.error, self.write = error, write
        self.commands_sent = 0
        self.contacts_read_at = self.contacts_read_started_at = 0
        self.contacts_outdated = False
        self.closed = False
        self.info = SimpleNamespace(public_key=SELF_KEY.hex())
        self.contact_records: dict[str, codec.ContactRecord] = {}

    async def request_telemetry(self, key: bytes, *, unless: object) -> None:
        if callable(unless) and unless():
            return
        if self.write:
            self.commands_sent += 1
        if self.error is not None:
            raise self.error


def stand_in_poller(
    session: StandInSession, monotonic: Callable[[], float] = time.monotonic
) -> tuple[TelemetryPoller, Observer]:
    observer = Observer(FakeBus(), TOPICS, wall_clock=lambda: NOW)
    activity = OutboxActivity(monotonic, lambda: 0)
    stand_in = cast(CompanionSession, session)
    poller = TelemetryPoller(
        observer, lambda: stand_in, activity, records=None, tcp=False, monotonic=monotonic,
        wall_clock=lambda: NOW, lanes=wide,
    )  # fmt: skip
    return poller, observer


@pytest.mark.parametrize(
    ("error", "write", "counted"),
    [
        (NotSentError("session closed"), False, False),  # never written
        (RadioError("write failed"), False, False),
        (RadioError("no answer from the radio"), True, True),  # written, no SENT
        (asyncio.CancelledError(), True, True),  # the stop, after the write
        (asyncio.CancelledError(), False, False),  # the stop, while it waited for the lock
    ],
)
def test_a_request_without_sent_counts_only_if_it_was_written(
    error: BaseException, write: bool, counted: bool
) -> None:
    async def main() -> None:
        session = StandInSession(error, write=write)
        poller, _observer = stand_in_poller(session)
        now = time.monotonic()
        flood = poller._scheduler.decide(EU, [Candidate(KEY, None, None, now)], now)
        assert isinstance(flood, Poll)
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await poller._send(cast(CompanionSession, session), flood)
        else:
            assert await poller._send(cast(CompanionSession, session), flood) is None
        exported = poller._scheduler.export(time.monotonic())
        assert (len(exported.floods_age_s) == 1) is counted
        assert (poller.used_s()[1] > 20.0) is counted
        if counted and not isinstance(error, asyncio.CancelledError):
            poller.path_changed(DANA.public_key)
            assert KEY not in poller._heard  # the path of its flood is no sign of life
            poller.answer(answer_of(DANA.public_key, 99))
            assert [key for key, _ in poller._held] == [KEY]  # its answer is taken, late

    asyncio.run(main())


def gps(lat_cells: int) -> bytes:
    return bytes([1, 136]) + lat_cells.to_bytes(3, "big") + bytes.fromhex("014ead002f12")


def test_late_answers_are_ordered_by_frame_within_a_session_only() -> None:
    """Not by the wall clock, which may be set back: within a session a lower frame number is
    older; a late answer in a new session is newer than any of the last one."""

    async def main() -> None:
        session = StandInSession()
        record = codec.decode(DANA.record())
        assert isinstance(record, codec.ContactRecord)
        session.contact_records = {KEY: record}
        poller, observer = stand_in_poller(session)
        observer.radio_event(session_module.ContactsRead({KEY: record}, NOW))
        now = time.monotonic()
        poller._scheduler.decide(EU, [Candidate(KEY, 0, 0, None)], now)
        prefix = DANA.public_key[:6]

        async def take(cells: int, seq: int, serial: int, at: float, late: bool) -> float:
            event = TelemetryReceived(prefix, gps(cells), at, seq, serial)
            await poller._accept(KEY, event, late=late)
            fix = observer.mesh.contacts[KEY].fix
            assert fix is not None
            return fix.position.latitude

        assert await take(501234, 50, 1, NOW + 10, False) == 50.12345
        assert await take(509999, 40, 1, NOW + 20, True) == 50.12345  # older frame: dropped
        assert await take(509999, 60, 1, NOW - 99, True) == 50.99995  # newer, clock set back
        assert await take(502000, 5, 2, NOW + 30, True) == 50.20005  # the next session
        assert poller._newest[KEY] == (2, 5)

    asyncio.run(main())


def chat_record(contact: EmulatedContact) -> codec.ContactRecord:
    record = codec.decode(contact.record())
    assert isinstance(record, codec.ContactRecord)
    return record


def test_an_advert_from_before_the_restart_counts_from_when_it_was_heard() -> None:
    """Heard 10 minutes before: it may be flooded for 20 minutes more, not 30."""
    session = StandInSession()
    session.contact_records = {KEY: chat_record(DANA)}
    poller, observer = stand_in_poller(session, monotonic=lambda: 5000.0)
    observer.mesh.restore(KEY, Heard(NOW - 600, "advert", Signal()), None)
    observer.radio_event(session_module.ContactsRead(session.contact_records, NOW))
    stand_in = cast(CompanionSession, session)
    assert [c.heard_at for c in poller._candidates(stand_in, 5000.0)] == [4400.0]


def test_heard_times_and_the_marks_of_contacts_that_left_are_dropped() -> None:
    """A busy mesh while nothing decides (paused) does not grow the poller's memory."""
    clock = [0.0]
    session = StandInSession()
    poller, _observer = stand_in_poller(session, monotonic=lambda: clock[0])
    for i in range(poller_module.HEARD_KEPT):
        poller.heard(f"{i:064x}")
    clock[0] = 2 * telemetry.HEARD_RECENTLY_S + 1
    poller.heard("ff" * 32)  # one more than kept: the old ones go
    assert list(poller._heard) == ["ff" * 32]
    gone = "ee" * 32
    poller._newest[gone], poller._routes[gone], poller._states[gone] = (1, 1), True, "resting"
    session.contact_records = {KEY: chat_record(DANA)}
    poller._candidates(cast(CompanionSession, session), clock[0])
    assert (gone in poller._newest, gone in poller._routes, gone in poller._states) == (
        False, False, False,
    )  # fmt: skip


# --- exclusion, states, events and the first-start hold, on a stand-in radio ------------------

BOB_KEY = BOB.public_key.hex()
ERIN = EmulatedContact(bytes.fromhex("e7" * 32), "Erin", out_path_len=0x00)
ERIN_KEY = ERIN.public_key.hex()
GINA = EmulatedContact(bytes.fromhex("9a" * 32), "Gina", out_path_len=0x00)
GINA_KEY = GINA.public_key.hex()
GINA_TWIN = EmulatedContact(GINA.public_key[:6] + bytes(26), "Trent", out_path_len=0x00)
HILL = EmulatedContact(bytes.fromhex("c7" * 32), "Hill", node_type=2)  # a repeater
MIRROR = EmulatedContact(SELF_KEY[:6] + bytes(26), "Mirror", out_path_len=0x00)  # the radio's
TWIN_KEY = DANA_TWIN.public_key.hex()
START = 10_000.0  # the stand-in clock when a test starts
HOLD_WARNING = "telemetry paused: the first requests go out in"


class StandInRadio:
    """A session with a table, as the poller reads it, whose companions answer as the test says:
    `answers` holds what each one sends, a key without an entry is silent."""

    def __init__(self, clock: list[float], *contacts: EmulatedContact) -> None:
        self.clock = clock
        self.info: Any = SimpleNamespace(  # None: a session that has not started
            public_key=SELF_KEY.hex(), lora=EU, self_info=SimpleNamespace(manual_add_contacts=True)
        )
        self.autoadd = None
        self.commands_sent = 0
        self.contacts_read_at = self.contacts_read_started_at = -1  # no table read yet
        self.contacts_outdated = False
        self.closed = False
        self.contact_records = {c.public_key.hex(): chat_record(c) for c in contacts}
        self.answers: dict[str, bytes] = {}
        self.asked: list[tuple[str, float]] = []  # whom, when
        self.lock: asyncio.Event | None = None  # another command holds the radio until it is set
        self.on_request: Callable[[str], None] | None = None  # after the write
        self.answer: Callable[[TelemetryReceived], None] | None = None
        self.seq = 0

    async def request_telemetry(
        self, key: bytes, *, unless: Callable[[], bool]
    ) -> TelemetrySent | None:
        if self.lock is not None:
            await self.lock.wait()
        if unless():
            return None
        self.commands_sent += 1
        self.seq += 2
        self.asked.append((key.hex(), self.clock[0]))
        if self.on_request is not None:
            self.on_request(key.hex())
        lpp_bytes = self.answers.get(key.hex())
        if lpp_bytes is not None and self.answer is not None:  # read before SENT is seen
            at = NOW + self.clock[0] - START
            self.answer(TelemetryReceived(key[:6], lpp_bytes, at, self.seq + 1, 1))
        return TelemetrySent(False, 0, self.seq)


@dataclass
class Rig:
    """The poller on a stand-in radio, wired to its observer as the bridge wires them. Its
    sleeps move the clock instead of waiting."""

    poller: TelemetryPoller
    observer: Observer
    bus: FakeBus
    radio: StandInRadio
    clock: list[float]
    connected: list[StandInRadio | None]  # what the poller's `session()` returns
    wall: Callable[[], float]

    async def steps(self, count: int = 1) -> None:
        for _ in range(count):
            await self.poller._step()

    async def until_asked(self, count: int, steps: int = 200) -> None:
        for _ in range(steps):
            if len(self.radio.asked) >= count:
                return
            await self.poller._step()
        raise AssertionError(f"not {count} requests in {steps} steps: {self.radio.asked}")

    def read(self) -> None:
        """A table read: the observer gets the table the radio holds."""
        self.radio.contacts_read_at = self.radio.contacts_read_started_at = self.radio.seq
        records = dict(self.radio.contact_records)
        self.observer.radio_event(session_module.ContactsRead(records, self.wall()))

    def exclude(self, key: str) -> None:
        """As the contact manager takes `off`."""
        self.observer.exclusions.exclude(key)
        self.observer.telemetry_excluded(key, True)

    def include(self, key: str) -> None:
        self.observer.exclusions.include(key)
        self.observer.telemetry_excluded(key, False)

    def state(self, key: str = KEY) -> object:
        return self.bus.payloads(f"/contacts/{key}")[-1]["telemetry_state"]

    def events(self) -> list[tuple[str, str, str]]:
        return [
            (e["event_type"], e["key"], e["state"]) for e in self.bus.payloads(f"{ROOT}/telemetry")
        ]

    def held(self) -> tuple[str, str | None]:
        return self.poller.status, self.poller.reason


def rig(
    monkeypatch: pytest.MonkeyPatch, *contacts: EmulatedContact,
    records: TelemetryRecords | None = None, exclusions: TelemetryExclusions | None = None,
    tcp: bool = False, wall: float = NOW, read: bool = True,
    current: CurrentDocuments | None = None, history: History | None = None,
) -> Rig:  # fmt: skip
    """`wall`: the wall clock at the start; `read`: the radio's table was read."""
    monkeypatch.setattr(telemetry, "STEP_S", 60.0)  # the sleeps only move the clock
    monkeypatch.setattr(telemetry, "RETRY_S", 30.0)
    monkeypatch.setattr(telemetry, "POLL_GAP_S", 10.0)
    monkeypatch.setattr(telemetry, "WAIT_MIN_S", 0.01)  # a silence takes this long for real
    monkeypatch.setattr(poller_module, "QUIET_S", 10.0)
    clock = [START]

    def wall_clock() -> float:
        return wall + clock[0] - START

    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=wall_clock, exclusions=exclusions,
                        current=current)  # fmt: skip
    radio = StandInRadio(clock, *contacts)
    connected: list[StandInRadio | None] = [radio]
    poller = TelemetryPoller(
        observer, lambda: cast(CompanionSession | None, connected[0]),
        OutboxActivity(lambda: clock[0], lambda: 0), records=records, tcp=tcp,
        monotonic=lambda: clock[0], wall_clock=wall_clock, lanes=wide,
    )  # fmt: skip
    radio.answer = poller.answer
    observer.telemetry_state = poller.state_of
    observer.on_telemetry_exclusion.append(poller.exclusion_changed)
    observer.on_contact_added.append(poller.contact_added)

    async def sleep(seconds: float) -> None:
        clock[0] += max(0.0, seconds)

    monkeypatch.setattr(poller, "_sleep", sleep)
    poller.restore()  # before the first table read, as the bridge does it
    stand_in = Rig(poller, observer, bus, radio, clock, connected, wall_clock)
    if read:
        stand_in.read()
    return stand_in


def saved_hold(store: JsonStateFile) -> object:
    return store.load()["telemetry"]["hold_left_s"]


def changes(monkeypatch: pytest.MonkeyPatch, r: Rig) -> list[str]:
    """The keys the poller reports a changed state of, in order."""
    reported: list[str] = []
    published = r.observer.telemetry_state_changed

    def changed(key: str) -> None:
        reported.append(key)
        published(key)

    monkeypatch.setattr(r.observer, "telemetry_state_changed", changed)
    return reported


def test_tests_skip_the_first_start_hold_unless_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, LOGGER)
    assert poller_module.FIRST_START_S == 0.0
    records = TelemetryRecords(JsonStateFile(tmp_path / "state.json"))

    async def main() -> None:
        r = rig(monkeypatch, DANA, records=records)
        await r.steps(1)
        assert r.radio.asked == [(KEY, START)]

    asyncio.run(main())
    assert HOLD_WARNING not in caplog.text


@pytest.mark.first_start_hold
def test_marked_tests_keep_the_first_start_hold() -> None:
    assert poller_module.FIRST_START_S == 3600.0


def test_an_excluded_contact_is_never_asked_but_still_blocks_its_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA, DANA_TWIN, ERIN, BOB, HILL)
        r.exclude(TWIN_KEY)
        r.exclude(ERIN_KEY)
        candidates = r.poller._candidates(cast(CompanionSession, r.radio), r.clock[0])
        assert [c.key for c in candidates] == [BOB_KEY]
        await r.until_asked(1)
        await r.steps(30)
        assert {key for key, _ in r.radio.asked} == {BOB_KEY}
        states = [r.poller.state_of(key) for key in (KEY, TWIN_KEY, ERIN_KEY)]
        assert states == ["look_alike", "excluded", "excluded"]  # excluded before look-alike
        assert (r.state(KEY), r.state(TWIN_KEY), r.state(ERIN_KEY)) == (
            "look_alike", "excluded", "excluded",
        )  # fmt: skip
        assert HILL.public_key.hex() not in r.poller._reported  # a repeater has no state

    asyncio.run(main())


def test_an_exclusion_while_the_request_waits_for_the_radio_withdraws_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        r.radio.lock = asyncio.Event()  # another command holds the radio
        step = asyncio.create_task(r.steps())
        await until(lambda: r.poller._sending == KEY)
        r.exclude(KEY)
        r.radio.lock.set()
        await step
        assert (r.radio.commands_sent, r.radio.asked) == (0, [])  # nothing written
        assert r.poller._scheduler.in_flight is None
        await r.steps(30)
        assert r.radio.asked == []
        assert r.state() == "excluded"

    asyncio.run(main())


def test_an_answer_of_a_contact_excluded_during_the_table_wait_is_not_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        fresh_table = r.poller._fresh_table

        async def excluded_meanwhile() -> CompanionSession | None:
            r.exclude(KEY)  # while the answer waits for a table that shows every change
            return await fresh_table()

        monkeypatch.setattr(r.poller, "_fresh_table", excluded_meanwhile)
        await r.until_asked(1)
        assert r.poller._scheduler.state(KEY, r.clock[0]) == "waiting"  # nothing learned
        assert r.poller._scheduler.in_flight is None
        document = r.bus.payloads(f"/contacts/{KEY}")[-1]
        assert (document["battery_v"], document["telemetry_state"]) == (None, "excluded")
        assert r.events() == []

    asyncio.run(main())


def test_the_silence_of_a_contact_excluded_meanwhile_counts_no_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)  # silent
        r.radio.on_request = r.exclude  # excluded once its request went out
        await r.until_asked(1)
        assert r.poller._scheduler.failures(KEY) == 0
        assert r.poller._scheduler.in_flight is None

    asyncio.run(main())


def test_an_included_contact_is_waiting_and_asked_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exclusion forgets the answers: an inclusion soon after gets no old schedule back,
    also after its silence was a retry; the same once its schedule was dropped (PARK_S)."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        assert r.state() == "answering"
        del r.radio.answers[KEY]
        r.radio.on_request = r.exclude  # excluded while its next request is in flight
        await r.until_asked(2)
        assert r.poller._scheduler.state(KEY, r.clock[0]) == "waiting"  # answers forgotten
        r.radio.on_request = None
        r.radio.answers[KEY] = LPP_FULL
        r.clock[0] += 5.0
        included = r.clock[0]
        r.include(KEY)
        assert r.state() == "waiting"
        await r.until_asked(3)
        quick = telemetry.POLL_GAP_S + poller_module.QUIET_S
        assert r.radio.asked[2][1] - included <= quick
        assert r.events() == [("answered", KEY, "answering")] * 2
        r.exclude(KEY)
        await r.steps(1)  # no longer a candidate: parked
        r.clock[0] += telemetry.PARK_S + 1.0
        await r.steps(1)  # its schedule is dropped
        included = r.clock[0]
        r.include(KEY)
        assert r.state() == "waiting"
        await r.until_asked(4)
        assert r.radio.asked[3][1] - included <= quick
        assert r.events() == [("answered", KEY, "answering")] * 3

    asyncio.run(main())


def test_a_companion_added_again_within_a_day_is_waiting_and_asked_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed from the radio, her values are gone: added again, her schedule forgets its
    answers as at an inclusion, and her next answer is her first since she was added."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        assert r.events() == [("answered", KEY, "answering")]
        record = r.radio.contact_records.pop(KEY)
        r.read()  # removed
        await r.steps(1)  # no longer a candidate: parked
        r.bus.published.clear()  # her topic was cleared
        r.clock[0] += 300.0
        r.radio.contact_records[KEY] = record
        r.read()  # added again
        added = r.clock[0]
        assert r.state() == "waiting"
        await r.until_asked(2)
        assert r.radio.asked[1][1] - added <= telemetry.POLL_GAP_S + poller_module.QUIET_S
        assert r.events() == [("answered", KEY, "answering")]

    asyncio.run(main())


def test_a_companion_removed_and_added_again_during_her_request_has_her_state_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table read that added her again built her document `waiting`; her silence is
    published, although it gives the state reported before the request."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)  # silent
        await r.until_asked(1)
        assert r.state() == "no_answer"

        def removed_and_added(_key: str) -> None:
            r.radio.on_request = None
            record = r.radio.contact_records.pop(KEY)
            r.read()
            r.bus.published.clear()  # her topic was cleared
            r.radio.contact_records[KEY] = record
            r.read()
            assert r.state() == "waiting"

        r.radio.on_request = removed_and_added
        await r.until_asked(2)
        assert r.state() == "no_answer"

    asyncio.run(main())


def test_the_first_table_read_after_a_restart_forgets_no_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The contacts of the last run are not added: her state, her due time and her last answer
    stay (D9)."""
    store = JsonStateFile(tmp_path / "state.json")

    async def main() -> None:
        first = rig(monkeypatch, DANA, records=TelemetryRecords(store))
        first.radio.answers[KEY] = LPP_FULL
        await first.until_asked(1)
        interval = first.poller._scheduler.interval_s(KEY, first.clock[0])
        assert interval is not None
        due = first.radio.asked[0][1] + interval - first.clock[0]
        first.poller._save(at_once=True)
        second = rig(monkeypatch, DANA, records=TelemetryRecords(store), wall=first.wall())
        assert second.state() == "answering"
        second.radio.answers[KEY] = LPP_FULL
        await second.until_asked(1)
        assert second.radio.asked[0][1] - START == pytest.approx(due)
        assert second.events() == []

    asyncio.run(main())


def test_the_state_of_a_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Excluded; a look-alike from the table as last read and the radio's own key, also before
    any decision and while paused; else the schedule's state (None: it does not know it)."""

    async def main() -> None:
        r = rig(monkeypatch, DANA, DANA_TWIN, MIRROR, ERIN)
        mirror = MIRROR.public_key.hex()
        assert (r.poller.state_of(KEY), r.poller.state_of(mirror)) == ("look_alike",) * 2
        assert r.state() == "look_alike"  # the document of the first table read
        r.exclude(ERIN_KEY)
        assert r.poller.state_of(ERIN_KEY) == "excluded"
        r.radio.info.self_info.manual_add_contacts = False  # paused: nothing is decided
        await r.steps(2)
        assert r.held() == ("paused", "auto_add")
        r.connected[0] = None
        assert r.poller.state_of(mirror) == "look_alike"  # the radio's key is remembered
        r.connected[0] = r.radio
        del r.radio.contact_records[TWIN_KEY]
        r.read()  # the twin left
        assert r.poller.state_of(KEY) is None
        assert r.state() == "waiting"
        r.radio.info.self_info.manual_add_contacts = True
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        assert r.poller.state_of(KEY) == "answering"
        assert TWIN_KEY not in r.poller._reported  # what is kept of a contact that left

    asyncio.run(main())


def test_a_changed_state_is_reported_once_and_a_silence_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)
        reported = changes(monkeypatch, r)
        r.connected[0] = None  # the radio away: the steps only evaluate
        published = len(r.bus.published)
        await r.steps(3)
        assert reported == [KEY]  # the first evaluation, once
        assert len(r.bus.published) == published  # her document was built with that state
        reported.clear()
        r.connected[0] = r.radio
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        assert (reported, r.state()) == ([KEY], "answering")  # at once, with the answer
        await r.steps(5)
        assert reported == [KEY]
        del r.radio.answers[KEY]
        await r.until_asked(3)  # the second silence
        assert (reported, r.state()) == ([KEY, KEY], "away")  # at once, with the silence

    asyncio.run(main())


def test_the_first_state_of_a_contact_added_during_her_request_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session read Erin's table before the step's states were evaluated, the observer got
    it while she was asked: her document was built `waiting`, and her silence is published."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        r.radio.contact_records[ERIN_KEY] = chat_record(ERIN)

        def read_once(_key: str) -> None:
            r.radio.on_request = None
            r.read()
            assert r.state(ERIN_KEY) == "waiting"

        r.radio.on_request = read_once
        await r.until_asked(2)
        assert r.radio.asked[1][0] == ERIN_KEY
        assert r.state(ERIN_KEY) == r.poller.state_of(ERIN_KEY) == "no_answer"

    asyncio.run(main())


def test_answers_expiring_after_a_restart_before_the_first_step_are_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Her answers count 20 s more at the first table read after the restart, and the first
    step comes later: her document no longer reads current."""
    store = JsonStateFile(tmp_path / "state.json")

    async def main() -> None:
        first = rig(monkeypatch, DANA, records=TelemetryRecords(store))
        first.radio.answers[KEY] = LPP_FULL
        await first.until_asked(1)
        first.poller._save(at_once=True)
        restart = NOW + telemetry.CURRENT_MIN_S - 20.0
        second = rig(monkeypatch, DANA, records=TelemetryRecords(store), wall=restart)
        assert second.state() == "answering"
        second.clock[0] += 40.0
        await second.steps(1)
        document = second.bus.payloads(f"/contacts/{KEY}")[-1]
        assert (document["telemetry_state"], document["telemetry_current"]) == ("away", False)

    asyncio.run(main())


def test_an_exclusion_is_no_change_to_report_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observer publishes the document of an exclusion or inclusion itself: the poller
    takes the state as reported, and wakes to plan."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.connected[0] = None
        await r.steps(1)
        reported = changes(monkeypatch, r)
        r.poller._wake.clear()
        r.exclude(KEY)
        assert r.poller._wake.is_set()
        await r.steps(2)
        r.poller._wake.clear()
        r.include(KEY)
        assert r.poller._wake.is_set()
        await r.steps(2)
        assert (reported, r.state()) == ([], "waiting")

    asyncio.run(main())


def test_an_event_when_a_companion_stops_answering_and_when_it_answers_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unanswered` at the second request in a row it leaves unanswered, none at the third;
    `answered` at its first answer and at its next one, none after a single failure."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        answered, unanswered = ("answered", KEY, "answering"), ("unanswered", KEY, "away")
        assert r.events() == [answered]
        del r.radio.answers[KEY]
        await r.until_asked(2)  # one silence, then the retry answers
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(3)
        assert r.events() == [answered]
        del r.radio.answers[KEY]
        await r.until_asked(5)
        assert r.events() == [answered, unanswered]
        assert r.state() == "away"
        await r.until_asked(6)  # the third
        assert r.events() == [answered, unanswered]
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(7)
        assert r.events() == [answered, unanswered, answered]

    asyncio.run(main())


def test_no_event_for_a_companion_that_never_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)  # silent: it does not allow the gateway
        await r.until_asked(3)
        assert r.poller._scheduler.failures(KEY) == 3
        assert (r.state(), r.events()) == ("no_answer", [])

    asyncio.run(main())


def test_no_event_when_answers_expire_while_the_radio_is_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        r.connected[0] = None
        await r.steps(int(telemetry.CURRENT_MIN_S / telemetry.STEP_S) + 2)
        assert r.state() == "away"  # by the time bound: nothing was asked
        assert r.events() == [("answered", KEY, "answering")]

    asyncio.run(main())


def test_an_uncertain_silence_does_not_repeat_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over TCP a silence during a table read counts only the second time: the count stays at
    two, and the event does not come again."""

    async def main() -> None:
        r = rig(monkeypatch, DANA, tcp=True)
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        del r.radio.answers[KEY]
        await r.until_asked(3)
        assert [kind for kind, _, _ in r.events()] == ["answered", "unanswered"]

        def table_read(_key: str) -> None:  # started during the wait
            r.radio.contacts_read_started_at = r.radio.commands_sent + 1

        r.radio.on_request = table_read
        await r.until_asked(4)
        assert r.poller._scheduler.failures(KEY) == 2
        assert [kind for kind, _, _ in r.events()] == ["answered", "unanswered"]

    asyncio.run(main())


def test_no_event_while_the_contact_reads_as_a_look_alike(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observer's table already has its twin, the radio's as read does not: the answer is
    taken, but no event carries `look_alike`."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        twin = {**r.radio.contact_records, TWIN_KEY: chat_record(DANA_TWIN)}
        r.observer.radio_event(session_module.ContactsRead(twin, NOW))
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        assert r.poller._scheduler.state(KEY, r.clock[0]) == "answering"
        del r.radio.answers[KEY]
        await r.until_asked(3)
        assert r.poller._scheduler.state(KEY, r.clock[0]) == "away"
        assert (r.state(), r.events()) == ("look_alike", [])

    asyncio.run(main())


def test_without_a_radio_telemetry_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the first session and while the radio is away: `paused`, reason `no_radio`."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        assert r.held() == ("paused", "no_radio")
        r.connected[0] = None
        await r.steps(2)
        assert r.held() == ("paused", "no_radio")
        r.connected[0] = r.radio
        await r.steps(1)
        assert r.held() == ("on", None)
        r.radio.info = None  # a session that has not started
        await r.steps(1)
        assert r.held() == ("paused", "no_radio")

    asyncio.run(main())


# what holds the requests -> the status and reason: the first that holds, in this order
HOLDS: dict[str, tuple[tuple[str, ...], str, str | None]] = {
    "on": ((), "on", None),
    "no_radio": (("no_radio", "unsupported", "hold"), "paused", "no_radio"),
    "unsupported": (("unsupported", "unreadable", "hold"), "unsupported", None),
    "unreadable": (("unreadable", "hold", "auto_add"), "paused", "exclusions_unreadable"),
    "first_start": (("hold", "auto_add", "airtime"), "paused", "first_start"),
    "auto_add": (("auto_add", "airtime"), "paused", "auto_add"),
    "radio_airtime": (("airtime",), "paused", "radio_airtime"),
}


def test_every_status_and_reason_is_reached() -> None:
    assert {status for _, status, _ in HOLDS.values()} == set(telemetry.STATUSES) - {"off"}
    assert {reason for *_, reason in HOLDS.values()} == {*telemetry.PAUSE_REASONS, None}


def test_the_status_follows_the_radio_at_once_and_each_change_is_told(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`paused` with `no_radio` as soon as the radio is away, before a step sees it; the
    listener hears each change of what the status reads, and a bug in it stops no step."""

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        told: list[tuple[str, str | None]] = []
        r.poller.on_status = lambda: told.append(r.held())
        await r.steps(1)
        assert told == [("on", None)]
        r.connected[0] = None
        assert r.held() == ("paused", "no_radio")
        await r.steps(2)
        assert told == [("on", None)]  # it read so already
        r.connected[0] = r.radio
        await r.steps(2)
        assert told == [("on", None), ("on", None)]

        def broken() -> None:
            raise RuntimeError("bug")

        r.poller.on_status = broken
        r.connected[0] = None
        await r.steps(1)
        r.connected[0] = r.radio
        await r.until_asked(2)
        assert caplog.text.count("telemetry: the status could not be told") == 1

    asyncio.run(main())


@pytest.mark.first_start_hold
@pytest.mark.parametrize("case", HOLDS)
def test_the_status_says_what_holds_the_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    conditions, status, reason = HOLDS[case]
    store = JsonStateFile(tmp_path / "state.json")
    if "hold" not in conditions:
        TelemetryRecords(store).save(telemetry.Saved(), NOW)  # telemetry ran here before
    exclusions = None
    if "unreadable" in conditions:
        (tmp_path / EXCLUSIONS_FILE).write_text("{", encoding="utf-8")
        exclusions = TelemetryExclusions(tmp_path / EXCLUSIONS_FILE)

    async def main() -> None:
        r = rig(monkeypatch, DANA, records=TelemetryRecords(store), exclusions=exclusions)
        r.radio.answers[KEY] = LPP_FULL
        if "unsupported" in conditions:
            r.poller._unsupported = cast(CompanionSession, r.radio)
        if "auto_add" in conditions:
            r.radio.info.self_info.manual_add_contacts = False
        if "airtime" in conditions:
            limit = poller_module.MEASURED_TX_LIMIT_S
            monkeypatch.setattr(r.observer, "radio_tx_1h_s", lambda: limit)
        if "no_radio" in conditions:
            r.connected[0] = None
        await r.steps(3)
        assert r.held() == (status, reason)
        assert bool(r.radio.asked) is (status == "on")

    asyncio.run(main())


def test_unreadable_exclusions_log_one_error_per_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / EXCLUSIONS_FILE).write_text("[]", encoding="utf-8")
    exclusions = TelemetryExclusions(tmp_path / EXCLUSIONS_FILE)
    caplog.set_level(logging.ERROR, LOGGER)

    def errors() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == LOGGER]

    async def main() -> None:
        r = rig(monkeypatch, DANA, exclusions=exclusions)
        await r.steps(3)
        assert len(errors()) == 1
        assert EXCLUSIONS_FILE in errors()[0]
        r.connected[0] = StandInRadio(r.clock, DANA)  # the radio reconnected
        await r.steps(3)
        assert len(errors()) == 2
        assert r.radio.asked == []

    asyncio.run(main())


def test_a_bug_in_the_states_is_logged_once_a_minute_and_the_step_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, LOGGER)

    def broken(keys: object = None) -> None:
        raise RuntimeError("a bug")

    def logged() -> int:
        return caplog.text.count("telemetry: the states could not be evaluated")

    async def main() -> None:
        r = rig(monkeypatch, DANA)
        r.radio.answers[KEY] = LPP_FULL
        monkeypatch.setattr(r.poller, "_refresh_states", broken)
        first = r.clock[0]
        await r.steps(1)  # also the answer's evaluation fails
        assert r.radio.asked == [(KEY, first)]  # the step went on
        assert logged() == 1
        r.clock[0] = first + poller_module.STATE_ERROR_LOG_S - 1.0
        r.poller._report_states()
        assert logged() == 1
        r.clock[0] = first + poller_module.STATE_ERROR_LOG_S
        r.poller._report_states()
        assert logged() == 2

    asyncio.run(main())


def test_restore_forgets_excluded_contacts_and_hands_on_when_positions_came(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Excluded while telemetry was off: its answers are forgotten. The documents of the first
    table read carry the restored states and `position_received_at`; no event comes."""
    records = TelemetryRecords(JsonStateFile(tmp_path / "state.json"))
    answered = telemetry.SavedContact(
        600.0, answered_age_s=100.0, polled_age_s=100.0, located=True, located_age_s=100.0
    )
    records.save(telemetry.Saved({KEY: answered, ERIN_KEY: answered}), NOW - 50.0)
    exclusions = TelemetryExclusions(None)
    exclusions.exclude(ERIN_KEY)

    async def main() -> None:
        r = rig(monkeypatch, DANA, ERIN, records=records, exclusions=exclusions)
        dana = r.bus.payloads(f"/contacts/{KEY}")[-1]
        assert (dana["telemetry_state"], dana["position_received_at"]) == (
            "answering", iso(NOW - 150.0),
        )  # fmt: skip
        erin = r.bus.payloads(f"/contacts/{ERIN_KEY}")[-1]
        assert (erin["telemetry_state"], erin["position_received_at"]) == ("excluded", None)
        assert r.poller._scheduler.state(ERIN_KEY, r.clock[0]) == "waiting"
        scheduler = r.poller._scheduler
        r.poller.restore()  # once only
        assert r.poller._scheduler is scheduler
        published = len(r.bus.published)
        await r.steps(2)
        assert r.bus.payloads(f"/contacts/{KEY}")[-1] == dana
        assert len(r.bus.published) == published
        assert r.events() == []

    asyncio.run(main())


def test_restored_readings_of_an_answer_older_than_the_saved_one_are_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The history missed Dana's later answers (off or broken for a while, or 1.1.0a1 ran): what
    it kept would read as current. Gina's are those of her saved answer, a little earlier after
    the round trip between the clocks, and stay (a restart flips nothing); excluded Erin's
    document is as before."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    records = TelemetryRecords(JsonStateFile(tmp_path / "state.json"))
    answered = telemetry.SavedContact(600.0, answered_age_s=300.0, polled_age_s=300.0)
    records.save(telemetry.Saved(dict.fromkeys((KEY, GINA_KEY, ERIN_KEY), answered)), NOW)
    history.add_readings(KEY, NOW - 7 * 86_400.0, {"battery_v": 3.95})  # a week old
    history.add_readings(GINA_KEY, NOW - 330.0, {"battery_v": 4.1})
    history.add_readings(ERIN_KEY, NOW - 300.0, {"battery_v": 4.0})
    exclusions = TelemetryExclusions(None)
    exclusions.exclude(ERIN_KEY)

    async def main() -> None:
        r = rig(monkeypatch, DANA, GINA, ERIN, records=records, exclusions=exclusions,
                history=history)  # fmt: skip
        dana = r.bus.payloads(f"/contacts/{KEY}")[-1]
        assert (dana["telemetry_current"], dana["battery_v"], dana["readings_at"]) == (
            True, None, None,
        )  # fmt: skip
        gina = r.bus.payloads(f"/contacts/{GINA_KEY}")[-1]
        assert (gina["telemetry_current"], gina["battery_v"], gina["readings_at"]) == (
            True, 4.1, iso(NOW - 330.0),
        )  # fmt: skip
        erin = r.bus.payloads(f"/contacts/{ERIN_KEY}")[-1]
        assert (erin["telemetry_state"], erin["battery_v"], erin["readings_at"]) == (
            "excluded", None, None,
        )  # fmt: skip

    asyncio.run(main())
    history.close()


def test_readings_of_a_late_answer_taken_a_while_after_it_came_stay_after_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gina's answer came after her wait, while a message was delivered, and was taken after
    that and Dana's wait, more than ANSWERED_SLACK_S later: the saved schedule knows it from
    when it came, so the readings the history kept of it come back after a restart."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    store = JsonStateFile(tmp_path / "state.json")
    pushed: list[float] = []

    async def main() -> None:
        first = rig(monkeypatch, GINA, DANA, records=TelemetryRecords(store), history=history)
        await first.until_asked(1)  # silent

        async def delivering(_quiet_s: float, *, timeout_s: float) -> bool:
            first.clock[0] += 2.0
            pushed.append(first.wall())
            first.poller.answer(TelemetryReceived(GINA.public_key[:6], LPP_FULL, pushed[0], 99, 1))
            first.clock[0] += 50.0  # the message delivered, then quiet
            return True

        def waited(_key: str) -> None:
            first.clock[0] += 15.0

        monkeypatch.setattr(first.poller._outbox, "wait_quiet", delivering)
        first.radio.on_request = waited
        await first.steps(2)  # Dana asked and silent; then Gina's answer taken
        assert [key for key, _ in first.radio.asked] == [GINA_KEY, KEY]
        gina = first.bus.payloads(f"/contacts/{GINA_KEY}")[-1]
        assert (gina["battery_v"], gina["readings_at"]) == (4.12, iso(pushed[0]))
        assert first.wall() - pushed[0] > ANSWERED_SLACK_S
        first.poller._save(at_once=True)
        second = rig(monkeypatch, GINA, DANA, records=TelemetryRecords(store), history=history,
                     wall=first.wall() + 300.0)  # fmt: skip
        gina = second.bus.payloads(f"/contacts/{GINA_KEY}")[-1]
        assert (gina["telemetry_current"], gina["battery_v"], gina["readings_at"]) == (
            True, 4.12, iso(pushed[0]),
        )  # fmt: skip

    asyncio.run(main())
    history.close()


@pytest.mark.first_start_hold
def test_the_first_start_holds_the_requests_for_an_hour_after_the_table_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Telemetry never ran on this state directory: nothing is asked for FIRST_START_S from the
    first step with a read table; one warning names how many companions it will ask (neither
    excluded, nor look-alikes, nor repeaters); the hold is saved at once."""
    caplog.set_level(logging.INFO, LOGGER)
    store = JsonStateFile(tmp_path / "state.json")
    exclusions = TelemetryExclusions(None)
    exclusions.exclude(ERIN_KEY)

    async def main() -> None:
        r = rig(
            monkeypatch, DANA, BOB, ERIN, HILL, GINA, GINA_TWIN, records=TelemetryRecords(store),
            exclusions=exclusions, read=False,
        )  # fmt: skip
        await r.steps(3)
        assert r.held() == ("paused", "first_start")
        assert HOLD_WARNING not in caplog.text  # not before the table is read
        r.clock[0] += 600.0
        r.read()
        start = r.clock[0]
        await r.steps(1)
        warned = [m.getMessage() for m in caplog.records if HOLD_WARNING in m.getMessage()]
        assert warned == [
            (
                "telemetry paused: the first requests go out in 60 minutes, to 2 companions of "
                "the radio; exclude every one that is not the household's (the Telemetry switch "
                "on its device, or meshcore-home-gateway contacts telemetry KEY off)"
            )
        ]
        assert saved_hold(store) == 3600.0  # saved at once
        await r.until_asked(1, steps=100)
        assert r.radio.asked[0][1] - start == pytest.approx(poller_module.FIRST_START_S)
        assert r.held() == ("on", None)
        await r.until_asked(2)
        assert {key for key, _ in r.radio.asked} == {KEY, BOB_KEY}
        assert caplog.text.count(HOLD_WARNING) == 1
        assert caplog.text.count("telemetry resumed") == 1

    asyncio.run(main())


@pytest.mark.first_start_hold
@pytest.mark.parametrize(
    "times",
    [
        (None, NOW + 1800.0, 1800.0),  # it stopped abruptly: the save at the hold's start counts
        (NOW + 1800.0, NOW + 2400.0, 1200.0),  # stopped halfway, started 10 minutes later
        (None, NOW - 100.0, 3600.0),  # the clock set back: all that was saved is held
        (None, NOW + 7200.0, 0.0),  # started after the hour: nothing is left
    ],
    ids=["abrupt", "stopped", "clock-set-back", "after-the-hour"],
)
def test_a_restart_holds_what_is_left_of_the_first_start_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    times: tuple[float | None, float, float],
) -> None:  # fmt: skip
    """`times`: when the first run saved at its stop (None: it did not), when the second one
    starts, and how long it holds."""
    stop, restart, left = times
    caplog.set_level(logging.WARNING, LOGGER)
    records = TelemetryRecords(JsonStateFile(tmp_path / "state.json"))

    async def main() -> None:
        first = rig(monkeypatch, DANA, records=records)
        await first.steps(30)  # the hold starts: half an hour
        assert first.radio.asked == []
        if stop is not None:
            assert first.wall() == stop
            first.poller._save(at_once=True)
        second = rig(monkeypatch, DANA, records=records, wall=restart)
        await second.until_asked(1)
        assert second.radio.asked[0][1] - START == pytest.approx(left)

    asyncio.run(main())
    assert caplog.text.count(HOLD_WARNING) == (2 if left else 1)


@pytest.mark.first_start_hold
@pytest.mark.parametrize("version", [1, 2])
def test_telemetry_that_ran_before_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: int
) -> None:
    """A record as 1.1.0a1 wrote it, or one of another version: telemetry ran here."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"telemetry": {
        "version": version, "saved_at": NOW - 60.0, "contacts": {}, "floods_age_s": [],
        "own_debt_s": 0.0, "mesh_debt_s": 0.0,
    }})  # fmt: skip

    async def main() -> None:
        r = rig(monkeypatch, DANA, records=TelemetryRecords(store))
        await r.steps(1)
        assert r.radio.asked == [(KEY, START)]

    asyncio.run(main())


@pytest.mark.first_start_hold
def test_without_a_state_file_the_first_start_hold_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA)
        await r.steps(10)
        assert (r.held(), r.radio.asked) == (("paused", "first_start"), [])

    asyncio.run(main())


@pytest.mark.first_start_hold
def test_the_state_file_keeps_what_is_left_of_the_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All of it while it waits for the radio's table, the rest while it runs, nothing once it
    is over."""
    store = JsonStateFile(tmp_path / "state.json")

    async def main() -> None:
        r = rig(monkeypatch, DANA, records=TelemetryRecords(store), read=False)
        r.connected[0] = None
        await r.steps(5)
        r.poller._save(at_once=True)
        assert saved_hold(store) == 3600.0
        r.connected[0] = r.radio
        r.read()
        await r.steps(10)  # the hold starts: 10 minutes
        r.poller._save(at_once=True)
        assert saved_hold(store) == pytest.approx(3000.0)
        r.clock[0] += 3000.0  # its end, before a step sees it
        r.poller._save(at_once=True)
        assert saved_hold(store) is None
        await r.until_asked(1)
        r.poller._save(at_once=True)
        assert saved_hold(store) is None

    asyncio.run(main())


@pytest.mark.first_start_hold
def test_the_hold_is_saved_as_started_also_after_the_save_of_an_earlier_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The service saves after every step, the first before the radio's table is read: the
    save when the hold starts must not wait for a change of the schedule, or an abrupt stop
    makes the next start hold the whole hour again."""
    store = JsonStateFile(tmp_path / "state.json")
    records = TelemetryRecords(store)

    async def main() -> None:
        first = rig(monkeypatch, DANA, records=records, read=False)
        await first.steps(1)
        first.poller._save()  # as run() does after each step
        assert store.load()["telemetry"]["hold_pending"] is True
        first.clock[0] += 120.0
        first.read()
        started = first.wall()
        await first.steps(1)  # the hold starts
        first.poller._save()
        assert store.load()["telemetry"]["hold_pending"] is False
        second = rig(monkeypatch, DANA, records=records, wall=started + 3000.0)  # stopped abruptly
        await second.until_asked(1)
        assert second.radio.asked[0][1] - START == pytest.approx(600.0)

    asyncio.run(main())


@pytest.mark.first_start_hold
def test_a_hold_that_has_not_started_is_kept_whole_over_a_long_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The radio's table was never read, so the hold never started: a restart a day later holds
    all of it, counted from its own first read table."""
    store = JsonStateFile(tmp_path / "state.json")
    records = TelemetryRecords(store)

    async def main() -> None:
        first = rig(monkeypatch, DANA, records=records, read=False)
        await first.steps(5)
        first.poller._save(at_once=True)
        assert store.load()["telemetry"]["hold_pending"] is True
        second = rig(monkeypatch, DANA, records=records, wall=NOW + 86_400.0)
        await second.until_asked(1, steps=100)
        assert second.radio.asked[0][1] - START == pytest.approx(poller_module.FIRST_START_S)
        second.poller._save(at_once=True)
        assert store.load()["telemetry"]["hold_pending"] is False

    asyncio.run(main())


@pytest.mark.first_start_hold
def test_a_contact_excluded_during_the_hold_is_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        r = rig(monkeypatch, DANA, BOB)
        await r.steps(10)
        r.exclude(KEY)
        await r.until_asked(1)
        await r.steps(60)
        assert {key for key, _ in r.radio.asked} == {BOB_KEY}

    asyncio.run(main())


def test_a_restart_without_the_radio_still_expires_her_answers_and_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR 0024: an outage of the radio expires every companion at its bound, also when the
    gateway restarted meanwhile and has read no table since: nothing changes while her answers
    are current, then only her state."""
    store = JsonStateFile(tmp_path / "state.json")
    retained: list[dict[str, Any]] = []

    async def first() -> None:
        r = rig(monkeypatch, DANA, records=TelemetryRecords(store), current=CurrentDocuments(store))
        r.radio.answers[KEY] = LPP_FULL
        await r.until_asked(1)
        retained.append(r.bus.payloads(f"/contacts/{KEY}")[-1])
        r.observer.flush()  # the stop
        r.poller._save(at_once=True)

    async def second() -> None:  # a minute later; the radio does not come up
        r = rig(monkeypatch, DANA, records=TelemetryRecords(store), wall=NOW + 60, read=False,
                current=CurrentDocuments(store))  # fmt: skip
        r.connected[0] = None
        r.observer.publish_all()  # the broker connects
        for _ in range(int(2 * telemetry.CURRENT_MIN_S / telemetry.STEP_S)):  # two hours
            await r.steps()
            r.observer.tick()
            published = r.bus.payloads(f"/contacts/{KEY}")
            assert len(published) == (r.poller.state_of(KEY) not in telemetry.CURRENT)
        expired = {"telemetry_state": "away", "telemetry_current": False, "position_current": False}
        assert published == [{**retained[0], **expired}]
        assert r.held() == ("paused", "no_radio")

    asyncio.run(first())
    assert retained[0]["position_current"] is True
    asyncio.run(second())
