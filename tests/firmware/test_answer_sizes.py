"""Hardware checklist step 34: the size of B's telemetry answers, direct and after a flood, over
no, one and two repeaters, and a late answer."""

from __future__ import annotations

import asyncio

import pytest

from tests.firmware.air import Transmission
from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import log_lines, logged, running
from tests.firmware.household import household_ready, start


@pytest.mark.broker
@pytest.mark.usefixtures("fast_schedule")
@pytest.mark.parametrize(("hops", "flooded"), [(0, 26), (1, 25), (2, 24)])
def test_an_answer_after_a_flood_is_shorter_by_one_byte_per_repeater(
    hops: int, flooded: int, scene: Scene
) -> None:
    """B's 19 bytes of readings (battery, position, MCU temperature) come padded: 28 bytes with
    9 zeros direct, over any number of repeaters; after a flood 26 with 7 zeros, one less per
    repeater (the path the answer rides back with takes the room)."""
    household = start(scene.nodes, scene.air, hops)

    async def scenario() -> None:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice:
                await household_ready(household, alice, bob, route=False)
            async with running(scene.configuration(household.alice), scene.state_dir):
                key = household.bob_key.hex()
                await scene.watcher.wait(f"contacts/{key}")
                await bob.advert(flood=bool(hops))  # heard: without a path it may flood
                first = await logged(scene.caplog, f"telemetry {key[:12]}: answer of")
                second = await logged(scene.caplog, "answer of 28 bytes")
        assert first.endswith(f"answer of {flooded} bytes, 19 of them readings")
        assert second.endswith("answer of 28 bytes, 19 of them readings")

    asyncio.run(scenario())
    routes = [t.kind for t in scene.air.sent_by(household.alice.node_id, "REQ/")]
    assert routes[:2] == ["REQ/F", "REQ/D"]
    assert not log_lines(scene.caplog, "bytes not decoded")


@pytest.mark.broker
@pytest.mark.usefixtures("fast_schedule")
def test_a_late_answer_is_taken_until_the_next_request_then_ignored(scene: Scene) -> None:
    """The link from B to A holds B's answer back (the air's delay knob). An answer after the
    gateway's wait but before its next request is still taken (`late`); one after the next
    request does not match the radio's pending request any more: the radio pushes it as 0x8C,
    and the gateway ignores it."""
    household = start(scene.nodes, scene.air)
    alice_id, bob_id = household.alice.node_id, household.bob.node_id
    caplog = scene.caplog

    async def answer_held_back(delay_s: float) -> Transmission:
        """B's next answer reaches A `delay_s` later; the answers after it come at once."""
        scene.air.link(bob_id, alice_id, both=False, delay_s=delay_s)
        held = await scene.air.next_sent(bob_id, "RESPONSE/D", after=scene.air.count())
        scene.air.link(bob_id, alice_id, both=False)
        return held

    async def scenario() -> Transmission:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice:
                await household_ready(household, alice, bob)
            async with running(scene.configuration(household.alice), scene.state_dir):
                await logged(caplog, "answer of 28 bytes, 19 of them readings")
                await answer_held_back(8.0)  # after the 5 s wait, before the retry
                await logged(caplog, "answer of 28 bytes, 19 of them readings (late)")
                ignored = await answer_held_back(25.0)  # after the next request
                await logged(caplog, "radio: push 0x8c ignored", timeout_s=40.0)
        return ignored

    held = asyncio.run(scenario())
    ignored = scene.air.again(held)
    after = [t for t in scene.air.sent_by(alice_id, "REQ/") if t.at > ignored.at]
    assert ignored.ended[alice_id] > after[0].at  # it came after the next request
    # taken: the first answer, the late one, the answer to the retry; not the one held 25 s
    assert len(log_lines(caplog, "19 of them readings")) == 3
