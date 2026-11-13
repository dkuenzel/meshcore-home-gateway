"""The gateway's advert nearby (ADR 0033) with the real firmware, the gateway's service and the
broker: a request on `advert/send` makes radio A send its advert zero hop, which gives a neighbour
the radio's current name; no repeater passes it on; a radio that would put its position into the
advert sends none."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from meshcore_home_gateway.core.airtime import self_advert_bytes
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Push
from meshcore_home_gateway.protocol.packet import AdvertData, parse_advert, parse_packet
from tests.firmware.air import Transmission
from tests.firmware.app import App, Frame
from tests.firmware.conftest import Scene
from tests.firmware.gateway import running
from tests.firmware.household import LANDMARK, introduce, start

NAME = "Home gateway"  # the name the gateway's profile gives radio A
SETTINGS = f'[telemetry]\nenabled = false\n[radio.profile]\nname = "{NAME}"\n'
OBSERVED = '[telemetry]\nenabled = false\n[radio.profile]\nmanage = "observe"\n'
HEARD_S = 10.0  # a zero-hop advert reaches a neighbour within a second or two
REPEATED_S = 5.0  # longer than a repeater's longest delay for an advert
ADVERT_TIME_S = 1.2  # an advert's time is in seconds: the next one is newer


def advert_in(packet: bytes) -> AdvertData | None:
    found = parse_packet(packet)
    return parse_advert(found) if found is not None else None


def logged(frame: Frame) -> bytes:
    """The packet a packet log push (0x88) carries."""
    assert frame.code == Push.LOG_RX_DATA, f"0x{frame.code:02x}"
    decoded = codec.decode(frame.payload)
    assert isinstance(decoded, codec.RxLog)
    return decoded.raw


async def ask(scene: Scene, request_id: str) -> dict[str, Any]:
    """A request on `advert/send`, as any client publishes it, and its event."""
    since = scene.watcher.mark()
    scene.watcher.publish("advert/send", json.dumps({"id": request_id}))
    found = await scene.watcher.wait("advert", lambda e: e["id"] == request_id, since=since)
    event = found.json()
    assert isinstance(event, dict)
    return event


async def connected(scene: Scene, since: int) -> None:
    """Until the run that started after `since` has its radio."""
    await scene.watcher.wait("state", lambda d: d["radio"] == "connected", since=since)


def adverts_of(scene: Scene, sender: int, key: bytes, after: int) -> list[Transmission]:
    """The sender's transmissions of the adverts of `key` after the first `after`."""
    return [
        t
        for t in scene.air.transmissions()[after:]
        if t.sender == sender and (found := advert_in(t.packet)) and found.public_key == key
    ]


@pytest.mark.broker
def test_the_gateways_advert_gives_a_neighbour_its_current_name(scene: Scene) -> None:
    """A and B in direct range know each other by their first adverts. The gateway's profile
    renames A, which no advert announces by itself; a request makes A send its advert zero hop:
    the event says `sent` with the new name and the airtime of the advert as the air measured
    it, and B's radio stores the new name. The packet carries no path and no position."""
    household = start(scene.nodes, scene.air)

    async def scenario() -> tuple[Any, ...]:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice:
                await introduce(household, alice, bob)
            before = await bob.contact(household.alice_key)
            await asyncio.sleep(ADVERT_TIME_S)
            mark = scene.watcher.mark()
            async with running(scene.configuration(household.alice, SETTINGS), scene.state_dir):
                await connected(scene, mark)
                since, after = bob.mark(), scene.air.count()
                event = await ask(scene, "t1")
                pushed = await bob.push(Push.ADVERT, since, household.alice_key, HEARD_S)
                frames = bob.frames
                packet = logged(frames[frames.index(pushed) - 1])
                after_record = await bob.contact(household.alice_key)
                (sent,) = adverts_of(scene, household.alice.node_id, household.alice_key, after)
        return before, event, packet, after_record, sent

    before, event, packet, record, sent = asyncio.run(scenario())
    assert before.name not in ("", NAME)  # its first name, from the app's advert
    assert event["event_type"] == "sent"
    assert (event["id"], event["reason"], event["name"]) == ("t1", None, NAME)
    assert sent.kind == "ADVERT/D"  # zero hop: route direct with an empty path
    assert len(sent.packet) == self_advert_bytes(len(NAME.encode()))  # the estimate's size
    assert abs(event["airtime_ms"] - sent.airtime_ms) <= 1  # the estimate's time on air
    route = parse_packet(packet)
    assert route is not None
    assert (route.route, route.path_hashes) == (2, ())
    heard = parse_advert(route)
    assert heard is not None
    assert (heard.public_key, heard.name) == (household.alice_key, NAME)
    assert heard.latitude_e6 is None  # no flag 0x10: no position
    assert record.name == NAME
    assert record.last_advert > before.last_advert


@pytest.mark.broker
def test_no_repeater_passes_the_gateways_advert_on(scene: Scene) -> None:
    """A - R1 - B: B knows A from its flooded first advert. The gateway's advert reaches R1,
    which passes on no zero-hop packet: B hears nothing and keeps A's record as it was."""
    household = start(scene.nodes, scene.air, hops=1)
    alice, repeater = household.alice.node_id, household.repeaters[0].node_id

    async def scenario() -> tuple[Any, ...]:
        async with App(household.bob) as bob:
            async with App(household.alice) as app:
                await introduce(household, app, bob)
            before = await bob.contact(household.alice_key)
            await asyncio.sleep(ADVERT_TIME_S)
            mark = scene.watcher.mark()
            async with running(scene.configuration(household.alice, SETTINGS), scene.state_dir):
                await connected(scene, mark)
                since, after = bob.mark(), scene.air.count()
                event = await ask(scene, "t1")
                await asyncio.sleep(REPEATED_S)
                ours = adverts_of(scene, alice, household.alice_key, after)
                passed = adverts_of(scene, repeater, household.alice_key, after)
                pushes = [
                    f
                    for f in bob.frames[since:]
                    if f.code in (Push.ADVERT, Push.NEW_ADVERT)
                    and f.payload[1:33] == household.alice_key
                ]
                record = await bob.contact(household.alice_key)
        return before, event, ours, passed, pushes, record

    before, event, ours, passed, pushes, record = asyncio.run(scenario())
    assert event["event_type"] == "sent"
    (sent,) = ours
    assert sent.kind == "ADVERT/D"
    assert sent.outcomes == {repeater: "received"}
    assert passed == []
    assert pushes == []
    assert (record.name, record.last_advert) == (before.name, before.last_advert)


@pytest.mark.broker
def test_a_radio_that_shares_its_position_sends_no_advert(scene: Scene) -> None:
    """A's app has set a position and Share Position in Advert. With `manage = "observe"` the
    gateway leaves that setting, and refuses the advert (`advert_location`): A sends none. Once
    the app switched sharing off, the next run's advert goes out without a position."""
    radio = scene.nodes.companion("A")
    config = scene.configuration(radio, OBSERVED)
    latitude, longitude, _ = LANDMARK

    async def scenario() -> tuple[Any, ...]:
        async with App(radio) as app:
            key = (await app.start()).public_key
            await app.set_position(latitude, longitude)
            await app.settings(0, manual_add=True, share_position=True)
        mark = scene.watcher.mark()
        async with running(config, scene.state_dir):
            await connected(scene, mark)
            after = scene.air.count()
            refused = await ask(scene, "t2")
            await asyncio.sleep(REPEATED_S)
            unsent = adverts_of(scene, radio.node_id, key, after)
        async with App(radio) as app:
            await app.start()
            await app.settings(0, manual_add=True, share_position=False)
        await asyncio.sleep(ADVERT_TIME_S)
        mark = scene.watcher.mark()
        async with running(config, scene.state_dir):
            await connected(scene, mark)
            after = scene.air.count()
            sent = await ask(scene, "t3")
            transmission = await scene.air.next_sent(radio.node_id, "ADVERT/", after=after)
        return refused, unsent, sent, transmission

    refused, unsent, sent, transmission = asyncio.run(scenario())
    assert (refused["event_type"], refused["reason"]) == ("refused", "advert_location")
    assert unsent == []
    assert sent["event_type"] == "sent"
    assert transmission.kind == "ADVERT/D"
    advert = advert_in(transmission.packet)
    assert advert is not None
    assert advert.latitude_e6 is None
