"""Hardware checklist step 35: messages to B while telemetry runs. The ordering rules are the
gateway's and hold here as on a household's radio; the waits measured here depend on the
simulation's timing (its loop, the pseudo terminal, its airtime model) and are only indicative."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import logged, running
from tests.firmware.household import household_ready, start

TEXTS = ("Right after a flood", "Dinner is ready", "The door is open", "Lights out")
QUIET_S = 10.0  # the checklist: no request within 10 s after a message's confirmation
SPACING_S = 4.0  # between the messages sent while the gateway polls B


def requests(scene: Scene) -> list[float]:
    """When the gateway's requests went to the radio, on the clock of the log records."""
    return [
        record.created
        for record in scene.caplog.records
        if record.name.endswith("telemetry_poller") and ", wait " in record.getMessage()
    ]


@pytest.mark.broker
@pytest.mark.usefixtures("fast_schedule")
def test_no_request_from_a_message_until_10_s_after_its_confirmation(
    scene: Scene, request: pytest.FixtureRequest
) -> None:
    """A message right after a flooded request, then three while the gateway asks B every 15 s:
    no request between a message's queueing and its confirmation, nor within 10 s after (a
    request already handed to the radio when the message came is no exception). How long the
    message after the flood waited for the radio is recorded (`pytest -rP` shows it), not
    judged. A large table read needs a full table: not covered here."""
    household = start(scene.nodes, scene.air)
    alice = household.alice.node_id
    watcher = scene.watcher

    async def scenario() -> float:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice_app:
                await household_ready(household, alice_app, bob, route=False)
            target = f'[targets.bob]\ncontact = "{household.bob_key.hex()}"\n'
            async with running(scene.configuration(household.alice, target), scene.state_dir):
                await watcher.wait(f"contacts/{household.bob_key.hex()}")
                await bob.advert(flood=False)  # heard: without a path, it floods
                flood = await scene.air.next_sent(alice, "REQ/F", after=0)
                watcher.publish("send/bob", TEXTS[0])
                handed = await watcher.wait("delivery", sent(TEXTS[0]))
                message = await scene.air.next_sent(alice, "TXT_MSG/", after=0)  # the first
                await watcher.wait("delivery", final(TEXTS[0]))
                await logged(scene.caplog, "answer of 28 bytes")  # direct now, B rests
                for text in TEXTS[1:]:
                    watcher.publish("send/bob", text)
                    await asyncio.sleep(SPACING_S)
                for text in TEXTS[1:]:
                    await watcher.wait("delivery", final(text))
                await asyncio.sleep(2 * QUIET_S + 5)  # the requests go on after
        assert flood.at < message.at
        return scene.air.started + message.at - handed.at

    waited = asyncio.run(scenario())
    measured = f"a message right after a flooded request waited {waited:.3f} s for the radio"
    request.node.add_report_section("call", "measured", measured)
    events = [m.json() for m in watcher.on("delivery")]
    assert {e["text"] for e in events if e["event_type"] == "confirmed"} == set(TEXTS)
    asked = requests(scene)
    ids = {e["text"]: e["id"] for e in events}
    for text in TEXTS:
        queued = log_time(scene, f"delivery {ids[text]} to 'bob': queued")
        confirmed = log_time(scene, f"delivery {ids[text]} to 'bob': confirmed")
        # a request handed to the radio just before the message came logs a moment later
        assert not [t for t in asked if queued + 0.1 < t < confirmed + QUIET_S - 0.01], text
    last = log_time(scene, f"delivery {ids[TEXTS[-1]]} to 'bob': confirmed")
    assert asked[-1] > last + QUIET_S


def sent(text: str) -> Callable[[Any], bool]:
    return lambda event: event["text"] == text and event["event_type"] == "sent"


def final(text: str) -> Callable[[Any], bool]:
    return lambda event: event["text"] == text and event["event_type"] == "confirmed"


def log_time(scene: Scene, line: str) -> float:
    """When the gateway logged this line (time.time())."""
    (found,) = [r.created for r in scene.caplog.records if r.getMessage() == line]
    return found
