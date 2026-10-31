"""Hardware checklist step 29, the part that takes minutes: the first-start hold, and on a later
start the first request with B's answer in its document."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from meshcore_home_gateway import telemetry_poller
from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import log_lines, logged, running
from tests.firmware.household import LANDMARK, household_ready, start

HOLD_S = 15.0  # the first-start hold, an hour on a household's gateway
# The first request after the radio connected (EU/UK Narrow): the gateway's own airtime lane
# starts empty and fills at 18 s an hour, until it holds the 0.377 s of a direct request.
FIRST_REQUEST_S = 75.6


def connected(state: Any) -> bool:
    return isinstance(state, dict) and state.get("radio") == "connected"


def held(state: Any) -> bool:
    return (state["telemetry"], state["telemetry_reason"]) == ("paused", "first_start")


@pytest.mark.broker
@pytest.mark.first_start_hold
def test_the_first_start_holds_then_a_later_start_asks_b_after_76_s(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where telemetry never ran: `paused` with `first_start` from connected, one warning with
    the number of companions, no request. On the later start, the hold over: the first request
    about 76 s after connected, direct, and B's document with its position from telemetry,
    accuracy 15, `answering`, its battery as its app shows it, and the event `answered` once.
    Real lanes: no fast schedule here."""
    monkeypatch.setattr(telemetry_poller, "FIRST_START_S", HOLD_S)
    household = start(scene.nodes, scene.air)
    alice_id = household.alice.node_id
    watcher = scene.watcher

    async def scenario() -> tuple[int, float, float]:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice:
                await household_ready(household, alice, bob)
            battery = await bob.battery()  # what Bob's app shows
            first = watcher.mark()
            async with running(scene.configuration(household.alice), scene.state_dir):
                await watcher.wait("state", held, since=first)
                await watcher.wait("received", since=first)  # B's message, fetched now
                over = await watcher.wait("state", lambda s: s["telemetry"] == "on", since=first,
                                          timeout_s=HOLD_S + 10)  # fmt: skip
            assert not scene.air.sent_by(alice_id, "REQ/")  # nobody asked during the hold
            states = [m for m in watcher.on("state", first) if m.at < over.at]
            while_connected = [m for m in states if connected(m.json())]
            # the state published at the connect still reads `no_radio`, for the few milliseconds
            # until the poller's next step
            first_held = next(m for m in while_connected if held(m.json()))
            assert first_held.at - while_connected[0].at < 1.0
            assert all(held(m.json()) for m in while_connected if m.at >= first_held.at)
            assert over.at - while_connected[0].at == pytest.approx(HOLD_S, abs=3)
            later = watcher.mark()
            async with running(scene.configuration(household.alice), scene.state_dir):
                up = await watcher.wait("state", connected, since=later)
                request = await scene.air.next_sent(alice_id, "REQ/", after=scene.air.count(),
                                                    timeout_s=FIRST_REQUEST_S + 15)  # fmt: skip
                key = household.bob_key.hex()
                await watcher.wait(f"contacts/{key}", lambda d: d["telemetry_state"] == "answering",
                                   since=later)  # fmt: skip
                await logged(scene.caplog, "answer of 28 bytes, 19 of them readings")
        assert request.kind == "REQ/D"
        return battery.millivolts, up.at, scene.air.started + request.at

    millivolts, up, asked = asyncio.run(scenario())
    warnings = log_lines(scene.caplog, "telemetry paused: the first requests go out in")
    assert len(warnings) == 1
    assert "to 1 companions of the radio" in warnings[0]
    assert FIRST_REQUEST_S - 1 < asked - up < FIRST_REQUEST_S + 4
    document = watcher.latest(f"contacts/{household.bob_key.hex()}")
    latitude, longitude, altitude = LANDMARK
    assert document["position_source"] == "telemetry"
    assert document["latitude"] == pytest.approx(
        latitude, abs=1e-4
    )  # the 0.0001° cell it is sent in
    assert document["longitude"] == pytest.approx(longitude, abs=1e-4)
    assert document["altitude_m"] == altitude
    assert document["position_accuracy_m"] == 15
    assert document["battery_v"] == pytest.approx(millivolts / 1000, abs=0.01)
    assert (document["path_known"], document["path_hops"]) == (True, 0)
    answered = [e.json() for e in watcher.on("telemetry")]
    assert [(e["event_type"], e["state"]) for e in answered] == [("answered", "answering")]
