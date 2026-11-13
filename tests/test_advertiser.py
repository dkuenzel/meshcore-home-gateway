"""Advert requests through the bridge, with the emulated radio (ADR 0033): one zero-hop advert per
accepted request, each check with its reason, the claim at the radio's lock, the 10 minutes, the
airtime budget and one `advert` event per request."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import advertiser as advertiser_module
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.airtime import (
    direct_attempt_airtime_s,
    meshcore_lora,
    self_advert_bytes,
    time_on_air_s,
)
from meshcore_home_gateway.ports import AdvertRequest, NotSentError, RadioPort
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES
from meshcore_home_gateway.protocol.codes import Command
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import CompanionSession
from meshcore_home_gateway.state import AirtimeLevel
from meshcore_home_gateway.views import iso
from tests.bridge_harness import (
    ALICE,
    NOW,
    FakeBus,
    Unreachable,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    run_state,
    running,
    scenario,
    until,
)
from tests.radio_emulator import EmulatedRadio

ROOT = "meshcore/radio1"
EVENT = f"{ROOT}/advert"
EU = meshcore_lora(62_500, 8, 8)  # the emulated radio's modulation
BRIDGE_S = time_on_air_s(self_advert_bytes(len(b"Bridge")), EU)  # the emulated radio's name
LONGEST_S = direct_attempt_airtime_s(MAX_TEXT_BYTES, 0, True, EU)
ANSWER = (Command.GET_DEVICE_TIME, Command.SEND_SELF_ADVERT)  # what a request writes


class Clock:
    """The monotonic clock, moved on by a test."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def ask(bus: FakeBus, payload: bytes | str | dict[str, Any] = b"send", **options: bool) -> None:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    body = payload.encode() if isinstance(payload, str) else payload
    bus.advert(body, **options)


def events(bus: FakeBus) -> list[dict[str, Any]]:
    return [json.loads(p) for t, p, _ in bus.published if t == EVENT]


async def outcome(bus: FakeBus, count: int = 1, within_s: float = 3.0) -> dict[str, Any]:
    """The `count`-th event."""
    await until(lambda: len(events(bus)) >= count, within_s)
    return events(bus)[count - 1]


def written(radio: EmulatedRadio, since: int = 0) -> list[int]:
    return [c for c in radio.commands()[since:] if c in ANSWER]


def config(extra: str = "", airtime: str = "") -> Config:
    return make_config(extra, airtime=airtime)


# --- sent ---------------------------------------------------------------------------------------


def test_a_request_sends_one_zero_hop_advert_and_one_sent_event(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            since = len(radio.received)
            ask(bus)
            event = await outcome(bus)
            assert event == {
                "event_type": "sent",
                "id": event["id"],
                "reason": None,
                "name": "Bridge",
                "airtime_ms": round(BRIDGE_S * 1000),
                "next_at": iso(NOW + 600),
            }
            assert re.fullmatch(r"[0-9a-f]{8}", event["id"])
            assert radio.adverts == [b"\x07"]  # the code alone: zero hop, never the flood byte
            assert radio.commands()[since:] == [Command.GET_DEVICE_TIME, Command.SEND_SELF_ADVERT]
            assert [r for t, _, r in bus.published if t == EVENT] == [False]
            assert bus.qos[EVENT] == {1}
            await asyncio.sleep(0.1)
            assert len(events(bus)) == 1

    scenario(main)


def test_the_id_of_a_json_request_comes_back(tmp_path: Path) -> None:
    """Other keys are ignored; the second, 10 minutes too soon, carries its id as well."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            ask(bus, {"id": "x1"})
            assert (await outcome(bus))["id"] == "x1"
            ask(bus, {"id": "Kitchen:2.b_c-9", "other": 1})
            refused = await outcome(bus, 2)
            assert (refused["event_type"], refused["id"]) == ("refused", "Kitchen:2.b_c-9")
            ask(bus, b"  [1]")  # no object: a request without an id
            plain = await outcome(bus, 3)
            assert (plain["reason"], len(plain["id"])) == ("too_soon", 8)

    scenario(main)


def test_sixty_seconds_ahead_still_sends(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            radio.clock = int(NOW) + 60
            ask(bus)
            assert (await outcome(bus))["event_type"] == "sent"

    scenario(main)


def test_the_profile_clears_the_position_policy_before_the_advert(tmp_path: Path) -> None:
    """On tested firmware the managed profile writes policy 0 at the connect (ADR 0020): the
    advert goes out, and the radio sends it without a position."""
    radio = EmulatedRadio(advert_location=1, manual_add=False)
    profile = RadioProfile(make_config().radio.profile)

    async def main() -> None:
        async with running(config(), radio, tmp_path, profile=profile) as (_, bus, _n):
            assert radio.advert_location == 0
            ask(bus)
            assert (await outcome(bus))["event_type"] == "sent"
            assert radio.advert_location == 0

    scenario(main)


# --- refused --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"x" * 1025, "too_large"),
        (b"\xff", "invalid_utf8"),
        (b"{", "invalid_request"),
        (b"{1}", "invalid_request"),
        (b' {"id": 5}', "invalid_request"),
        (b'{"id": "bad id"}', "invalid_request"),
        (b'{"id": ""}', "invalid_request"),
        (b'{"id": "' + b"x" * 65 + b'"}', "invalid_request"),
        (b'{"id": "\\ud800"}', "invalid_request"),  # the id's check comes first
        (b"", "empty"),
    ],
)
def test_a_request_in_a_wrong_form_is_refused_and_writes_nothing(
    tmp_path: Path, payload: bytes, reason: str
) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            since = len(radio.received)
            ask(bus, payload)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", reason)
            assert (event["name"], event["airtime_ms"], event["next_at"]) == (None, None, None)
            await asyncio.sleep(0.05)
            assert written(radio, since) == []
            assert len(events(bus)) == 1

    scenario(main)


def test_a_payload_of_one_kibibyte_and_a_surrogate_beside_the_id_are_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = EmulatedRadio()
    monkeypatch.setattr(advertiser_module, "INTERVAL_S", 0.0)

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            ask(bus, b"x" * 1024)
            assert (await outcome(bus))["event_type"] == "sent"
            ask(bus, b'{"x": "\\ud800", "id": "y"}')
            assert (await outcome(bus, 2))["event_type"] == "sent"

    scenario(main)


def test_a_retained_request_is_refused(tmp_path: Path) -> None:
    """A left-over: refused at each delivery, with the id of its envelope."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            since = len(radio.received)
            ask(bus, {"id": "old"}, retained=True)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"], event["id"]) == (
                "refused",
                "retained",
                "old",
            )
            assert written(radio, since) == []

    scenario(main)


def test_without_a_radio_it_is_refused(tmp_path: Path) -> None:
    async def main() -> None:
        bus = FakeBus()
        async with running(config(), EmulatedRadio(), tmp_path, Unreachable, bus=bus):
            await until(lambda: bool(bus.payloads("/state")))
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "no_radio")

    scenario(main)


@pytest.mark.parametrize("manage", ["auto", "always"])
def test_untested_firmware_is_refused_also_with_manage_always(tmp_path: Path, manage: str) -> None:
    """An advert's route and position are facts of the tested firmware, which the gateway cannot
    read back: `always` lets the profile write, not this."""
    radio = EmulatedRadio(firmware_code=15)

    async def main() -> None:
        settings = config(f'[radio.profile]\nmanage = "{manage}"')
        async with running(settings, radio, tmp_path) as (_, bus, _n):
            since = len(radio.received)
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "untested_firmware")
            assert written(radio, since) == []

    scenario(main)


def test_a_radio_that_would_send_its_position_is_refused_also_when_observed(
    tmp_path: Path,
) -> None:
    """The policy as the radio reports it decides: with `observe` the profile leaves it."""
    radio = EmulatedRadio(advert_location=1)
    settings = config('[radio.profile]\nmanage = "observe"')
    profile = RadioProfile(settings.radio.profile)

    async def main() -> None:
        async with running(settings, radio, tmp_path, profile=profile) as (_, bus, _n):
            since = len(radio.received)
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "advert_location")
            assert written(radio, since) == []
            assert radio.advert_location == 1

    scenario(main)


def test_a_radio_without_a_name_is_refused(tmp_path: Path) -> None:
    """Every node drops an advert without a name (BaseChatMesh.cpp:122-127)."""
    radio = EmulatedRadio(name="")

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "no_name")
            assert radio.adverts == []

    scenario(main)


def test_a_second_request_while_the_first_is_handled_is_busy(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            ask(bus, {"id": "first"})
            ask(bus, {"id": "second"})  # the same turn of the loop: the first is not done
            await outcome(bus, 2)
            got = {e["id"]: (e["event_type"], e["reason"]) for e in events(bus)}
            assert got == {"first": ("sent", None), "second": ("refused", "busy")}
            assert radio.adverts == [b"\x07"]

    scenario(main)


def test_at_most_one_advert_in_ten_minutes(tmp_path: Path) -> None:
    radio = EmulatedRadio()
    clock = Clock()

    async def main() -> None:
        async with running(config(), radio, tmp_path, monotonic=clock) as (_, bus, _n):
            ask(bus)
            assert (await outcome(bus))["event_type"] == "sent"
            clock.offset += 599
            ask(bus)
            soon = await outcome(bus, 2)
            assert (soon["event_type"], soon["reason"]) == ("refused", "too_soon")
            assert soon["next_at"] == iso(NOW + 600)
            clock.offset += 1
            ask(bus)
            again = await outcome(bus, 3)
            assert again["event_type"] == "sent"
            assert radio.adverts == [b"\x07", b"\x07"]

    scenario(main)


def test_the_ten_minutes_are_kept_in_memory_only(tmp_path: Path) -> None:
    """A restart allows the next advert at once; the budget's level, which the next start takes
    back (ARC-005), is lower by the first advert's airtime."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            ask(bus)
            assert (await outcome(bus))["event_type"] == "sent"
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            assert bridge.outbox.budget.available_s(time.monotonic()) == pytest.approx(
                10.0 - BRIDGE_S, abs=0.05
            )
            ask(bus)
            assert (await outcome(bus))["event_type"] == "sent"
        assert radio.adverts == [b"\x07", b"\x07"]

    scenario(main)


def test_a_message_on_its_way_refuses_the_advert(tmp_path: Path) -> None:
    """Messages go first: one being delivered, waiting for the radio's answer here."""
    radio = EmulatedRadio(contacts=[ALICE], held={Command.SEND_TXT_MSG})

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            bus.send("alice", "Dinner is ready")
            await until(lambda: Command.SEND_TXT_MSG in radio.commands())
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "messages_waiting")
            radio.release(Command.SEND_TXT_MSG)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert radio.adverts == []

    scenario(main)


def test_a_message_that_comes_while_the_clock_is_read_goes_first(tmp_path: Path) -> None:
    """The claim at the radio's lock checks the messages again: nothing is counted, and the next
    request, once the message is delivered, sends."""
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)  # the message waits for its ack

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            budget = bridge.outbox.budget
            radio.held.add(Command.GET_DEVICE_TIME)
            since = len(radio.received)
            ask(bus, {"id": "early"})
            await until(lambda: Command.GET_DEVICE_TIME in radio.commands()[since:])
            bus.send("alice", "Dinner is ready")
            await until(lambda: bridge.outbox.activity.busy)
            radio.release(Command.GET_DEVICE_TIME)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "messages_waiting")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed", within_s=5)
            assert radio.adverts == []
            message_s = budget.used_s(time.monotonic())
            ask(bus, {"id": "later"})
            assert (await outcome(bus, 2))["event_type"] == "sent"  # the 10 minutes not started
            assert budget.used_s(time.monotonic()) == pytest.approx(message_s + BRIDGE_S)

    scenario(main)


def test_the_advert_is_charged_to_the_airtime_budget_at_the_claim(tmp_path: Path) -> None:
    """Taken before the radio answers, and its level stored at once (ARC-005)."""
    radio = EmulatedRadio(held={Command.SEND_SELF_ADVERT})

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            budget = bridge.outbox.budget
            before = budget.available_s(time.monotonic())
            ask(bus)
            await until(lambda: Command.SEND_SELF_ADVERT in radio.commands())
            assert budget.used_s(time.monotonic()) == pytest.approx(BRIDGE_S)
            level = AirtimeLevel(run_state(tmp_path)).load(NOW)
            assert level is not None
            assert level[0] == pytest.approx(before - BRIDGE_S, abs=0.01)
            radio.release(Command.SEND_SELF_ADVERT)
            assert (await outcome(bus))["event_type"] == "sent"
            bridge.status.publish()
            state = bus.payloads(f"{ROOT}/state")[-1]
            assert state["airtime_1h_s"] == pytest.approx(BRIDGE_S, abs=0.001)

    scenario(main)


def test_the_budget_must_hold_the_advert_and_one_long_message_beside_it(tmp_path: Path) -> None:
    """An alert after the advert goes out at once: refused with the reserve short by a little,
    sent with it."""
    radio = EmulatedRadio()
    slow = "budget_s_per_hour = 0.001"  # no refill to speak of within the test

    async def main() -> None:
        async with running(config(airtime=slow), radio, tmp_path) as (bridge, bus, _n):
            budget = bridge.outbox.budget
            budget.consume(10.0 - (BRIDGE_S + LONGEST_S) + 0.01, time.monotonic())
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "airtime_budget")
            budget.charge(-0.02, time.monotonic())  # given back: the reserve is there
            ask(bus)
            assert (await outcome(bus, 2))["event_type"] == "sent"
            assert radio.adverts == [b"\x07"]

    scenario(main)


def test_at_a_slow_preset_the_advert_needs_the_full_burst(tmp_path: Path) -> None:
    """At SF 10 the advert (4.1 s) and a long message (8 s) do not fit into the burst of 10 s
    together: only a full budget pays the advert, which leaves the rest of the burst."""
    radio = EmulatedRadio(spreading_factor=10)
    slow = "budget_s_per_hour = 0.001"

    async def main() -> None:
        async with running(config(airtime=slow), radio, tmp_path) as (bridge, bus, _n):
            budget = bridge.outbox.budget
            budget.consume(0.01, time.monotonic())
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "airtime_budget")
            budget.charge(-0.01, time.monotonic())  # given back: the burst is full
            ask(bus)
            assert (await outcome(bus, 2))["event_type"] == "sent"

    scenario(main)


def test_an_advert_longer_than_the_burst_is_refused(tmp_path: Path) -> None:
    """At SF 12 the advert takes longer than the burst holds: it could never be paid."""
    radio = EmulatedRadio(spreading_factor=12)

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "airtime_budget")
            assert radio.adverts == []

    scenario(main)


def test_after_a_restart_with_an_empty_budget_the_reserve_is_not_spent(tmp_path: Path) -> None:
    """A start grants the level of one long message (ARC-005): not enough for an advert beside
    it, so a restart funds no advert at EU/UK Narrow."""
    radio = EmulatedRadio()
    slow = "budget_s_per_hour = 0.001"

    async def main() -> None:
        async with running(config(airtime=slow), radio, tmp_path) as (bridge, _b, _n):
            budget = bridge.outbox.budget
            budget.consume(budget.available_s(time.monotonic()), time.monotonic())
        async with running(config(airtime=slow), radio, tmp_path) as (bridge, bus, _n):
            assert bridge.outbox.budget.available_s(time.monotonic()) == pytest.approx(
                LONGEST_S, abs=0.01
            )
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "airtime_budget")

    scenario(main)


@pytest.mark.parametrize(("ahead", "sent"), [(61, False), (60, True), (-30, True)])
def test_a_radio_clock_ahead_by_more_than_a_minute_is_refused(
    tmp_path: Path, ahead: int, sent: bool
) -> None:
    """Read afresh before the advert: nodes keep the newest advert time of a contact, and would
    ignore the radio's later adverts (BaseChatMesh.cpp:133-136)."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            radio.clock = int(NOW) + ahead
            ask(bus)
            event = await outcome(bus)
            if sent:
                assert event["event_type"] == "sent"
            else:
                assert (event["event_type"], event["reason"]) == ("refused", "clock_ahead")
                assert radio.adverts == []

    scenario(main)


def test_a_radio_lost_while_its_clock_is_read_is_no_radio(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            radio.drop_on.add(Command.GET_DEVICE_TIME)
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("refused", "no_radio")
            assert radio.adverts == []

    scenario(main)


def test_a_radio_lost_before_the_worker_takes_the_request_is_no_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The radio can go between the checks and the worker: refused, nothing written, nothing
    counted."""
    radio = EmulatedRadio()
    real = advertiser_module.Advertiser._checked

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):

            def checked(self: advertiser_module.Advertiser, request: AdvertRequest) -> RadioPort:
                session = real(self, request)
                monkeypatch.setattr(bridge.radio, "live", lambda: None)  # gone right after
                return session

            monkeypatch.setattr(advertiser_module.Advertiser, "_checked", checked)
            since = len(radio.received)
            ask(bus)
            event = await outcome(bus)
            monkeypatch.undo()
            assert (event["event_type"], event["reason"], event["next_at"]) == (
                "refused",
                "no_radio",
                None,
            )
            assert written(radio, since) == []
            assert bridge.outbox.budget.used_s(time.monotonic()) == 0

    scenario(main)


def test_a_session_closed_before_the_advert_is_written_is_no_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session ended while the advert waited for the radio: nothing was written, so the
    request is refused, nothing is charged and the 10 minutes do not start."""
    radio = EmulatedRadio()
    real = CompanionSession.send_self_advert
    closed = {"times": 1}

    async def send(session: CompanionSession, *, unless: Any = None) -> bool:
        if closed["times"]:
            closed["times"] -= 1
            raise NotSentError("session closed")
        return await real(session, unless=unless)

    monkeypatch.setattr(CompanionSession, "send_self_advert", send)

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"], event["next_at"]) == (
                "refused",
                "no_radio",
                None,
            )
            assert bridge.outbox.budget.used_s(time.monotonic()) == 0
            ask(bus)
            assert (await outcome(bus, 2))["event_type"] == "sent"
            assert radio.adverts == [b"\x07"]

    scenario(main)


# --- failed ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("refusal", "reason"), [("errors", "table_full"), ("disabled", "disabled")]
)
def test_a_radio_refusal_fails_with_its_name_and_leaves_the_ten_minutes(
    tmp_path: Path, refusal: str, reason: str
) -> None:
    """Nothing was sent: the next request goes at once; the airtime stays charged."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            if refusal == "errors":
                radio.errors[Command.SEND_SELF_ADVERT] = 3
            else:
                radio.disabled.add(Command.SEND_SELF_ADVERT)
            ask(bus)
            event = await outcome(bus)
            assert event == {
                "event_type": "failed",
                "id": event["id"],
                "reason": reason,
                "name": "Bridge",
                "airtime_ms": round(BRIDGE_S * 1000),
                "next_at": None,
            }
            radio.disabled.clear()
            ask(bus)
            assert (await outcome(bus, 2))["event_type"] == "sent"
            used = bridge.outbox.budget.used_s(time.monotonic())
            assert used == pytest.approx(2 * BRIDGE_S)

    scenario(main)


def test_a_connection_lost_after_the_write_fails_outcome_unknown(tmp_path: Path) -> None:
    """The advert may have gone out: the 10 minutes count."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            lost = bridge.radio.session
            radio.drop_on.add(Command.SEND_SELF_ADVERT)
            ask(bus)
            event = await outcome(bus)
            assert (event["event_type"], event["reason"]) == ("failed", "outcome_unknown")
            assert (event["name"], event["next_at"]) == ("Bridge", iso(NOW + 600))
            await until(lambda: bridge.radio.live() not in (None, lost))
            ask(bus)
            soon = await outcome(bus, 2)
            assert (soon["event_type"], soon["reason"]) == ("refused", "too_soon")

    scenario(main)


def test_a_bug_fails_internal_error_and_the_requests_go_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in the checks or in the worker ends its request with `failed`, logged with its
    traceback; the next request is handled."""
    radio = EmulatedRadio()
    monkeypatch.setattr(advertiser_module, "INTERVAL_S", 0.0)
    real_bytes = self_advert_bytes
    real_read = CompanionSession.read_clock_offset
    bugs = {"checks": 1, "worker": 1}

    def estimate(name_bytes: int) -> int:
        if bugs["checks"]:
            bugs["checks"] -= 1
            raise RuntimeError("a bug in the checks")
        return real_bytes(name_bytes)

    async def read(session: CompanionSession) -> int:
        if bugs["worker"]:
            bugs["worker"] -= 1
            raise RuntimeError("a bug in the worker")
        return await real_read(session)

    monkeypatch.setattr(advertiser_module, "self_advert_bytes", estimate)
    monkeypatch.setattr(CompanionSession, "read_clock_offset", read)

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            for count, expected in ((1, "failed"), (2, "failed"), (3, "sent")):
                ask(bus, {"id": f"r{count}"})
                event = await outcome(bus, count)
                assert (event["event_type"], event["id"]) == (expected, f"r{count}")
                if expected == "failed":
                    assert event["reason"] == "internal_error"
                    assert (event["name"], event["airtime_ms"]) == (None, None)
            assert radio.adverts == [b"\x07"]

    with caplog.at_level(logging.ERROR):
        scenario(main)
    traced = [r for r in caplog.records if r.exc_info and "advert request" in r.getMessage()]
    assert len(traced) == 2


# --- every request one event ----------------------------------------------------------------------


def test_a_copy_of_a_request_in_progress_or_sent_gets_no_second_event(tmp_path: Path) -> None:
    """A QoS 1 resend of the command line's request: the same id, ignored for 10 minutes."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            radio.held.add(Command.GET_DEVICE_TIME)
            since = len(radio.received)
            ask(bus, {"id": "cli-1a2b3c4d"})
            await until(lambda: Command.GET_DEVICE_TIME in radio.commands()[since:])
            ask(bus, {"id": "cli-1a2b3c4d"})  # while the first is in progress
            await asyncio.sleep(0.05)
            radio.release(Command.GET_DEVICE_TIME)
            assert (await outcome(bus))["event_type"] == "sent"
            ask(bus, {"id": "cli-1a2b3c4d"})  # after it was sent
            ask(bus, {"id": "cli-5e6f7a8b"})  # another request: too soon
            second = await outcome(bus, 2)
            assert (second["id"], second["reason"]) == ("cli-5e6f7a8b", "too_soon")
            assert len(events(bus)) == 2
            assert radio.adverts == [b"\x07"]

    scenario(main)


def test_a_copy_is_a_request_of_its_own_after_ten_minutes(tmp_path: Path) -> None:
    """The ids are kept for 10 minutes: the same id after them is a new request."""
    radio = EmulatedRadio()
    clock = Clock()

    async def main() -> None:
        async with running(config(), radio, tmp_path, monotonic=clock) as (_, bus, _n):
            ask(bus, {"id": "x1"})
            assert (await outcome(bus))["event_type"] == "sent"
            clock.offset += 600
            ask(bus, {"id": "x1"})
            again = await outcome(bus, 2)
            assert (again["id"], again["event_type"]) == ("x1", "sent")
            assert radio.adverts == [b"\x07", b"\x07"]

    scenario(main)


def test_only_the_ids_of_the_newest_requests_taken_are_kept(tmp_path: Path) -> None:
    """A request taken and then refused by the worker starts no 10 minutes, so many can be taken
    within them: only the newest KEPT_IDS ids are kept as copies to ignore."""
    radio = EmulatedRadio()
    kept = advertiser_module.KEPT_IDS

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            radio.clock = int(NOW) + 61  # every request is taken, then refused by the worker
            for count in range(1, kept + 2):
                ask(bus, {"id": f"x{count - 1}"})
                assert (await outcome(bus, count))["reason"] == "clock_ahead"
            ask(bus, {"id": f"x{kept}"})  # the newest: still a copy
            ask(bus, {"id": "x0"})  # the oldest: no longer kept, so a request of its own
            again = await outcome(bus, kept + 2)
            assert (again["id"], again["reason"]) == ("x0", "clock_ahead")
            await asyncio.sleep(0.1)
            assert len(events(bus)) == kept + 2
            assert radio.adverts == []

    scenario(main)


def test_every_request_ends_in_one_event_in_order(tmp_path: Path) -> None:
    radio = EmulatedRadio()
    payloads: list[tuple[bytes, bool]] = [
        (b"\xff", False),
        (b'{"id": "a"}', False),
        (b'{"id": "b"}', True),
        (b'{"id": "c"}', False),
        (b"", False),
        (b'{"id": "d"}', False),
    ]

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (_, bus, _n):
            for count, (payload, retained) in enumerate(payloads, 1):
                ask(bus, payload, retained=retained)
                await outcome(bus, count)
            await asyncio.sleep(0.1)
            got = [(e["id"] if len(e["id"]) < 8 else "*", e["event_type"]) for e in events(bus)]
            assert got == [
                ("*", "refused"),
                ("a", "sent"),
                ("b", "refused"),
                ("c", "refused"),
                ("*", "refused"),
                ("d", "refused"),
            ]

    scenario(main)


def test_refused_events_are_limited_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(config(), radio, tmp_path) as (bridge, bus, _n):
            for _ in range(150):
                ask(bus, b"\xff")
            await asyncio.sleep(0.3)
            assert 100 <= len(events(bus)) <= 110
            bridge.advertiser.report_unpublished()

    with caplog.at_level(logging.WARNING):
        scenario(main)
    assert "advert events of refused requests not published" in caplog.text
