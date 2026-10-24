"""Telemetry from the radio's companions end to end (telemetry_poller.py, ADR 0018): the bridge
with the emulated radio and its remote companions, and the poller's parts on their own."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from pathlib import Path

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import telemetry_poller as poller_module
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.airtime import Costs
from meshcore_home_gateway.core.budget import AirtimeBudget
from meshcore_home_gateway.protocol.codes import Command
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.session import TelemetryReceived
from meshcore_home_gateway.state import JsonStateFile
from meshcore_home_gateway.telemetry_poller import AnswerWaiter, OutboxActivity
from tests.radio_emulator import (
    LPP_FULL,
    EmulatedContact,
    EmulatedRadio,
    RemoteCompanion,
    telemetry_push,
)
from tests.test_bridge import ALICE, FakeBus, make_config, running, scenario

ROOT = "meshcore/radio1"
DANA = EmulatedContact(bytes.fromhex("d4" * 32), "Dana", out_path_len=0x00)  # a neighbour
DANA_TWIN = EmulatedContact(DANA.public_key[:6] + bytes(26), "Mallory", out_path_len=0x00)
KEY = DANA.public_key.hex()
LOGGER = "meshcore_home_gateway.telemetry_poller"


def config(extra: str = "") -> Config:
    return make_config(extra="[telemetry]\nenabled = true\n" + extra)


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

    scenario(main)


def test_the_path_of_its_own_flood_is_no_sign_of_life(tmp_path: Path) -> None:
    """Only a path the contact sent by itself counts as heard by other means: the 0x81 of the
    gateway's own flood comes with the answer (here even before the poller sees SENT)."""
    radio = EmulatedRadio(contacts=[ALICE], remotes={ALICE.public_key: RemoteCompanion(hops=1)})
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


def test_a_busy_radio_pauses_the_polls(tmp_path: Path) -> None:
    """ERR 3: no packet buffer free; nothing was sent or charged."""
    radio = EmulatedRadio(
        contacts=[DANA], remotes={DANA.public_key: RemoteCompanion()},
        errors={Command.SEND_TELEMETRY_REQ: 3},
    )  # fmt: skip

    async def main() -> None:
        async with running(config(), radio, tmp_path, telemetry_lanes=wide) as (bridge, bus, _n):
            await bus.wait_for(f"/contacts/{KEY}", positioned)
            assert requests(radio) == 2
            assert bridge.state_payload()["telemetry_airtime_1h_s"] == pytest.approx(0.378, 1e-2)

    scenario(main)


def test_a_flooded_answer_is_charged_from_the_packet_log(tmp_path: Path) -> None:
    remote = RemoteCompanion(flood_reply=True)
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


def answer_of(key: bytes, seq: int) -> TelemetryReceived:
    return TelemetryReceived(key[:6], LPP_FULL, 0.0, seq)


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
