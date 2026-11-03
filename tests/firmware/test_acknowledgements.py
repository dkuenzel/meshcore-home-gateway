"""SEC-001 on the real firmware: the acknowledgement of a direct message is a hash of its text,
timestamp, attempt and the sender's key, sent in the clear, so whoever knows the text can confirm
a message nobody received (docs/mqtt-api.md, "What `confirmed` proves"). A stranger beside the
gateway's radio A is a stand-in on the air that sends the acknowledgements the test computes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from meshcore_home_gateway.core.airtime import (
    meshcore_lora,
    shortest_acknowledgement_s,
    time_on_air_s,
)
from meshcore_home_gateway.protocol.codec import expected_ack
from tests.firmware.air import RadioSettings, StandIn, Transmission, wait_for_hellos
from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import running
from tests.firmware.household import household_ready, start

STRANGER = 9  # a stand-in on the air: a stranger's radio in range of A
# header: payload type ACK, route direct (src/Packet.h); with no path, as a neighbour sends it
ACK_DIRECT = (0x03 << 2) | 0x02
ALARM = "Smoke in the kitchen"
GENUINE = "Dinner is ready"
# long, so that a genuine acknowledgement over a repeater takes a second more than a forged one
LONG_ALARM = (
    "Smoke in the kitchen and in the hall: leave the house by the garden door, call the fire "
    "brigade and wait for them at the gate. Do not go back inside!"
)
# in [diagnostics], then tables: no telemetry requests on the air, two attempts
SETTINGS = "[telemetry]\nenabled = false\n[messages]\nmax_attempts = 2\n"
FINAL = {"confirmed", "failed", "expired", "rejected"}


def final(text: str) -> Any:
    return lambda e: e["event_type"] in FINAL and (e["text"] or "").startswith(text)


async def forge(
    scene: Scene,
    stranger: StandIn,
    message: Transmission,
    *,
    key: bytes,
    text: str,
    timestamps: list[int],
    radio: RadioSettings,
) -> None:
    """Once A's message is over (A hears nothing while it sends), the stranger sends the
    acknowledgement of attempt 0 for each timestamp, one after the other (the air loses
    overlapping packets)."""
    lora = meshcore_lora(float(radio.bandwidth_hz), radio.spreading_factor, radio.coding_rate)
    over = message.at + message.airtime_ms / 1000 - scene.air.elapsed()
    await asyncio.sleep(max(over, 0.0) + 0.02)
    for timestamp in timestamps:
        packet = bytes([ACK_DIRECT, 0]) + expected_ack(timestamp, 0, text, key)
        airtime_ms = round(time_on_air_s(len(packet), lora) * 1000)
        stranger.transmit(packet, airtime_ms)
        await asyncio.sleep(airtime_ms / 1000 + 0.03)


def seconds_around(published_at: float) -> list[int]:
    """The timestamps the gateway may have given the message: it takes its clock when the
    message leaves the queue, between the request and now, most likely the latest second."""
    return sorted(set(range(int(published_at), int(time.time()) + 1)), reverse=True)


@pytest.mark.broker
@pytest.mark.parametrize("nonce", [False, True], ids=["plain", "confirm_nonce"])
def test_a_stranger_who_knows_the_text_confirms_what_nobody_received(
    scene: Scene, nonce: bool
) -> None:
    """B is switched off. A stranger in A's range who knows the household's alarm text sends the
    acknowledgement for the seconds around the message: A's radio takes it, the gateway reports
    `confirmed` and sends no second attempt. It flooded (no path to B), so nothing tells the
    forgery: not `suspect`. With `confirm_nonce` the text ends in 8 random hex digits, the
    forged codes miss, and the message fails after both attempts."""
    household = start(scene.nodes, scene.air)
    alice, bob = household.alice.node_id, household.bob.node_id
    stranger = StandIn(STRANGER, scene.air.port, scene.air.settings()[alice])
    wait_for_hellos(scene.air, [stranger])
    scene.air.link(STRANGER, alice, snr=6.0)
    more = SETTINGS + ("confirm_nonce = true\n" if nonce else "")

    async def scenario() -> Any:
        async with App(household.bob) as bob_app, App(household.alice) as alice_app:
            await household_ready(household, alice_app, bob_app, route=False)
        scene.air.unlink(alice, bob)  # B is switched off
        target = f'[targets.bob]\ncontact = "{household.bob_key.hex()}"\n'
        async with running(scene.configuration(household.alice, more + target), scene.state_dir):
            await scene.watcher.wait(f"contacts/{household.bob_key.hex()}")
            after, published_at = scene.air.count(), time.time()
            scene.watcher.publish("send/bob", ALARM)
            message = await scene.air.next_sent(alice, "TXT_MSG/", after=after)
            timestamps = seconds_around(published_at)
            radio = scene.air.settings()[alice]
            await forge(
                scene,
                stranger,
                message,
                key=household.alice_key,
                text=ALARM,
                timestamps=timestamps,
                radio=radio,
            )
            done = await scene.watcher.wait("delivery", final(ALARM), timeout_s=90)
        return done.json()

    try:
        done = asyncio.run(scenario())
    finally:
        stranger.socket.close()
    sent = scene.air.sent_by(alice, "TXT_MSG/")
    if nonce:
        assert (done["event_type"], done["reason"], done["attempt"]) == (
            "failed",
            "no_acknowledgement",
            2,
        )
        assert done["text"] != ALARM
        assert len(sent) == 2
    else:
        assert (done["event_type"], done["attempt"], done["suspect"]) == ("confirmed", 1, False)
        assert [t.kind for t in sent] == ["TXT_MSG/F"]  # sent once: no retry
        assert not [t for t in sent if t.heard_by(bob)]  # nobody received it


@pytest.mark.broker
def test_an_acknowledgement_sooner_than_the_path_allows_is_suspect(scene: Scene) -> None:
    """A reaches B over a repeater. B confirms a message, genuinely: not `suspect`. Then B is
    switched off, and a stranger beside A, whom A hears better than the repeater, sends the
    acknowledgement of the next message as soon as A has sent it: sooner than the message can
    have crossed the repeater, so the gateway reports `confirmed` with `suspect`."""
    household = start(scene.nodes, scene.air, hops=1)
    alice, repeater, bob = (n.node_id for n in household.chain)
    stranger = StandIn(STRANGER, scene.air.port, scene.air.settings()[alice])
    wait_for_hellos(scene.air, [stranger])
    scene.air.link(STRANGER, alice, snr=6.0)

    async def scenario() -> list[Any]:
        async with App(household.bob) as bob_app, App(household.alice) as alice_app:
            await household_ready(household, alice_app, bob_app)
        target = f'[targets.bob]\ncontact = "{household.bob_key.hex()}"\n'
        text = scene.configuration(household.alice, SETTINGS + target)
        async with running(text, scene.state_dir):
            found = await scene.watcher.wait(f"contacts/{household.bob_key.hex()}")
            assert found.json()["path_hops"] == 1
            scene.watcher.publish("send/bob", GENUINE)
            genuine = await scene.watcher.wait("delivery", final(GENUINE), timeout_s=60)
            scene.air.unlink(repeater, bob)  # B is switched off
            scene.air.unlink(repeater, alice, both=False)  # the stranger drowns the repeater
            after, published_at = scene.air.count(), time.time()
            scene.watcher.publish("send/bob", LONG_ALARM)
            message = await scene.air.next_sent(alice, "TXT_MSG/", after=after)
            assert message.kind == "TXT_MSG/D"
            timestamps = seconds_around(published_at)
            radio = scene.air.settings()[alice]
            await forge(
                scene,
                stranger,
                message,
                key=household.alice_key,
                text=LONG_ALARM,
                timestamps=timestamps,
                radio=radio,
            )
            forged = await scene.watcher.wait("delivery", final("Smoke"), timeout_s=60)
        return [genuine.json(), forged.json()]

    try:
        genuine, forged = asyncio.run(scenario())
    finally:
        stranger.socket.close()
    radio = scene.air.settings()[alice]
    lora = meshcore_lora(float(radio.bandwidth_hz), radio.spreading_factor, radio.coding_rate)

    def shortest_ms(text: str) -> float:
        return 1000 * shortest_acknowledgement_s(len(text), 1, 1, lora)

    assert (genuine["event_type"], genuine["suspect"]) == ("confirmed", False)
    assert genuine["rtt_ms"] > shortest_ms(GENUINE)  # the real firmware takes longer
    assert (forged["event_type"], forged["suspect"]) == ("confirmed", True)
    assert forged["rtt_ms"] < shortest_ms(LONG_ALARM)
    assert any("may be forged (SEC-001)" in r.getMessage() for r in scene.caplog.records)
