"""Hardware checklist steps 33 and 39: how the routes to B are learned over native repeaters.
The gateway never resets or writes a path for telemetry; the firmware learns them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from meshcore_home_gateway.core import airtime
from meshcore_home_gateway.protocol.codes import Push
from meshcore_home_gateway.protocol.packet import PAYLOAD_TYPE_RESPONSE, parse_packet
from tests.firmware.air import Transmission
from tests.firmware.app import App, AppError
from tests.firmware.conftest import Scene
from tests.firmware.gateway import log_lines, log_mark, logged, running
from tests.firmware.household import SNR, Household, household_ready, start

ANSWER = "answer of 28 bytes, 19 of them readings"


def answering(document: Any) -> bool:
    return bool(document["telemetry_state"] == "answering")


@pytest.mark.broker
@pytest.mark.usefixtures("fast_schedule")
def test_a_stale_direct_path_fails_until_b_floods_a_message(scene: Scene) -> None:
    """Step 33. B is carried out of A's range, a repeater still hears both: the requests on the
    old direct path go unanswered, and the gateway neither resets the path nor floods. B's app
    gives up its direct attempts and floods a message (scripted: the app's own retry policy),
    A stores the path B sends back (0x81), and the next request goes direct over the repeater
    and is answered."""
    household = start(scene.nodes, scene.air, hops=1)
    alice, repeater, bob = (n.node_id for n in household.chain)
    for near, far in ((alice, repeater), (repeater, bob)):
        scene.air.unlink(near, far)  # set up beside each other, the repeater out of range
    scene.air.link(alice, bob, snr=SNR)
    key = ""

    async def scenario(app: App) -> None:
        topic = f"contacts/{key}"
        async with running(scene.configuration(household.alice), scene.state_dir):
            found = await scene.watcher.wait(topic, answering)
            assert found.json()["path_hops"] == 0
            scene.air.unlink(alice, bob)  # out of direct range, the repeater hears both
            since = scene.air.count()
            await logged(scene.caplog, f"telemetry {key[:12]}: away", timeout_s=60)
            silent = [t.kind for t in scene.air.transmissions()[since:] if t.sender == alice]
            assert silent == ["REQ/D", "REQ/D"]
            await message_after_direct_attempts(app, household)
            moved = await scene.watcher.wait(topic, lambda d: d["path_hops"] == 1)
            after = scene.air.count()
            await scene.watcher.wait(topic, answering, since=scene.watcher.mark())
            asked = await scene.air.next_sent(alice, "REQ/", after=after, timeout_s=1)
            assert asked.kind == "REQ/D"
            assert asked.outcomes.get(repeater) == "received"
            assert moved.json()["path_known"]

    async def everything() -> None:
        nonlocal key
        async with App(household.bob) as bob_app:
            async with App(household.alice) as alice_app:
                await household_ready(household, alice_app, bob_app)
            for near, far in ((alice, repeater), (repeater, bob)):
                scene.air.link(near, far, snr=SNR)  # the repeater hears both
            key = household.bob_key.hex()
            await scenario(bob_app)

    asyncio.run(everything())
    requests = scene.air.sent_by(alice, "REQ/")
    assert {t.kind for t in requests} == {"REQ/D"}  # never a flood
    documents = [m.json() for m in scene.watcher.on(f"contacts/{key}")]
    assert all(d["path_known"] for d in documents)  # never a path reset
    assert len(log_lines(scene.caplog, ANSWER)) == 2  # beside A, then over the repeater
    events = [(e.json()["event_type"], e.json()["state"]) for e in scene.watcher.on("telemetry")]
    assert events == [("answered", "answering"), ("unanswered", "away"), ("answered", "answering")]


async def message_after_direct_attempts(app: App, household: Household) -> None:
    """What B's app does with a message the gateway's radio does not confirm: a direct attempt,
    then the path reset and a flood. A's radio stores the path B's sends back."""
    since = app.mark()
    sent = await app.send_text(household.alice_key, "Back in range?")
    assert not sent.flood
    with pytest.raises(AppError):
        await app.push(Push.SEND_CONFIRMED, since, timeout_s=sent.timeout_ms / 1000 + 1)
    await app.reset_path(household.alice_key)
    since = app.mark()
    assert (await app.send_text(household.alice_key, "Back in range?")).flood
    await app.push(Push.SEND_CONFIRMED, since, timeout_s=20)


@pytest.mark.broker
@pytest.mark.usefixtures("fast_schedule")
def test_a_flooded_answer_brings_the_path_back_three_seconds_later(scene: Scene) -> None:
    """Step 39. B two hops away resets its path to the gateway: it floods its answer to the next
    direct request. The packet log shows the flooded RESPONSE; the gateway counts the direct
    request plus about 12.6 s of mesh airtime for it; A's radio sends its path back 3 s later,
    and B's next answer comes direct."""
    household = start(scene.nodes, scene.air, hops=2)
    alice, bob = household.alice.node_id, household.bob.node_id
    more = "publish_rx_log = true\n"  # in [diagnostics]

    async def scenario(app: App) -> tuple[Transmission, float, float]:
        async with running(scene.configuration(household.alice, more), scene.state_dir):
            await logged(scene.caplog, ANSWER)
            await asyncio.sleep(2.5)  # a state document after the charge of the first request
            before = scene.watcher.latest("state")["telemetry_mesh_airtime_1h_s"]
            await app.reset_path(household.alice_key)
            since, logs = scene.air.count(), log_mark(scene.caplog)
            request = await scene.air.next_sent(alice, "REQ/", after=since)
            flooded = await scene.air.next_sent(bob, "RESPONSE/F", after=since)
            path = await scene.air.next_sent(alice, "PATH/D", after=since)
            mark = scene.watcher.mark()
            await logged(scene.caplog, ANSWER, since=logs)
            state = await scene.watcher.wait("state", since=mark)
            after = state.json()["telemetry_mesh_airtime_1h_s"]
            replies = await scene.air.next_sent(bob, "RESPONSE/", after=scene.air.count())
        assert request.kind == "REQ/D"
        assert replies.kind == "RESPONSE/D"
        return path, after - before, flooded.at

    async def everything() -> tuple[Transmission, float, float]:
        async with App(household.bob) as bob_app:
            async with App(household.alice) as alice_app:
                await household_ready(household, alice_app, bob_app)
            return await scenario(bob_app)

    path, charged, flooded_at = asyncio.run(everything())
    heard = [t for t in scene.air.transmissions() if t.kind == "RESPONSE/F" and t.at >= flooded_at]
    first_heard = min(t.ended[alice] for t in heard if t.heard_by(alice))
    # the firmware's 3000 ms on its millisecond clock can come up to 1 ms early, the air rounds
    # its times to milliseconds, and A's duty cycle may hold the path a little longer
    waited_ms = round((path.at - first_heard) * 1000)
    assert 2998 <= waited_ms < 3500
    lora = airtime.LoRaParams(62_500, 8, 8, 32)  # EU/UK Narrow with MeshCore's preamble
    direct = airtime.direct_poll_costs(2, 2, 19, lora).mesh_s
    assert charged - direct == pytest.approx(12.6, abs=0.05)
    packets = [parse_packet(bytes.fromhex(m.json()["raw"])) for m in scene.watcher.on("rx")]
    assert any(
        p is not None and p.flood and p.payload_type == PAYLOAD_TYPE_RESPONSE for p in packets
    )
