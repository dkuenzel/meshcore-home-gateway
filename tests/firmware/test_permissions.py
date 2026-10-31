"""Hardware checklist step 30, B's part: what B answers the gateway under each of its telemetry
permissions, and what the gateway makes of it. B2's environment sensor and C, the tracker board,
need sensors this board does not have; they stay on the hardware checklist."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from meshcore_home_gateway.protocol.codes import ContactFlag
from tests.firmware.air import Transmission
from tests.firmware.app import BY_CONTACT, App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import log_lines, running
from tests.firmware.household import Household, household_ready, start

ANSWER_S = 6.5  # the gateway waits 5 s for a direct answer
BASE, LOCATION = ContactFlag.TELEMETRY_BASE, ContactFlag.TELEMETRY_LOCATION
EVERYTHING = BASE | LOCATION | ContactFlag.TELEMETRY_ENVIRONMENT


@dataclass(frozen=True)
class Outcome:
    request: Transmission
    answered: bool
    document: dict[str, Any]


@pytest.mark.broker
@pytest.mark.usefixtures("fast_schedule")
def test_b_answers_only_what_it_allows_the_gateway(scene: Scene) -> None:
    """Telemetry requests "No"; "From Specific Contacts" with the gateway allowed nothing, base
    only, base and location, everything; the gateway removed from B's contacts: silence;
    silence; battery and MCU temperature only; with the position; the same (B has no environment
    sensor); silence. Every request direct on B's path, never a flood. B never answered before
    its base permission: `no_answer` and no `unanswered`; base only: `no_position`; with the
    location: `answering`."""
    household = start(scene.nodes, scene.air)
    key = ""

    async def next_outcome(state: str | None) -> Outcome:
        """The gateway's next request, whether B answered it, and B's document after it, once
        it reads `state` (an answer publishes it again; a silence that changes nothing does
        not)."""
        request = await scene.air.next_sent(household.alice.node_id, "REQ/",
                                            after=scene.air.count())  # fmt: skip
        since = scene.watcher.mark()
        await asyncio.sleep(ANSWER_S)
        answers = [t for t in scene.air.sent_by(household.bob.node_id) if t.at > request.at
                   and t.kind.startswith(("RESPONSE/", "PATH/"))]  # fmt: skip
        topic = f"contacts/{key}"
        if answers:
            await scene.watcher.wait(topic, since=since)
        document = await scene.watcher.settled(
            topic, lambda d: state is None or d["telemetry_state"] == state
        )
        return Outcome(request, bool(answers), document)

    async def scenario(bob: App) -> list[Outcome]:
        alice_key = household.alice_key
        steps: list[tuple[Callable[[], Awaitable[None]], str | None]] = [
            (lambda: bob.settings(0), "no_answer"),  # "No"
            (lambda: grant(bob, household, 0), "no_answer"),  # the gateway allowed nothing
            (lambda: grant(bob, household, BASE), "no_position"),
            (lambda: grant(bob, household, BASE | LOCATION), "answering"),
            (lambda: grant(bob, household, EVERYTHING), "answering"),
            (lambda: bob.remove_contact(alice_key), None),
        ]
        outcomes = []
        async with running(scene.configuration(household.alice), scene.state_dir):
            for change, state in steps:
                await change()
                outcomes.append(await next_outcome(state))
        return outcomes

    async def everything() -> list[Outcome]:
        nonlocal key
        async with App(household.bob) as bob:
            async with App(household.alice) as alice:
                await household_ready(household, alice, bob)
            key = household.bob_key.hex()
            return await scenario(bob)

    outcomes = asyncio.run(everything())
    assert [o.answered for o in outcomes] == [False, False, True, True, True, False]
    assert {o.request.kind for o in outcomes} == {"REQ/D"}
    assert scene.air.sent_by(household.alice.node_id, "REQ/F") == []
    _, _, base, located, full, _ = (o.document for o in outcomes)
    assert (base["battery_v"], base["mcu_temperature_c"]) == (3.95, 23.4)
    assert (base["latitude"], base["position_source"], base["readings"]) == (None, None, {})
    assert located["position_source"] == "telemetry"
    assert (full["readings"], full["latitude"]) == ({}, located["latitude"])
    assert all(o.document["path_known"] for o in outcomes)
    events = [m.json() for m in scene.watcher.on("telemetry")]
    assert [(e["event_type"], e["state"]) for e in events] == [("answered", "no_position")]
    sizes = log_lines(scene.caplog, "of them readings")
    assert [s.split(": ")[1] for s in sizes] == [
        "answer of 12 bytes, 8 of them readings",  # battery and MCU temperature
        "answer of 28 bytes, 19 of them readings",
        "answer of 28 bytes, 19 of them readings",
    ]


async def grant(bob: App, household: Household, flags: int) -> None:
    """ "From Specific Contacts" for every kind, the gateway's contact with these permissions."""
    await bob.settings(BY_CONTACT)
    await bob.grant(household.alice_key, flags)
