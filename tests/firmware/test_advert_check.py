"""Hardware checklist steps 41 and 43, the parts the simulated air can show: the gateway's check
whether a companion's newest advert carries its position (ADR 0025), with the real firmware, the
gateway's service and the broker. Telemetry is off in these tests, so a position on B's document
can only come from B's adverts. Home Assistant, the exclusion from telemetry and the update from
1.1.0a2 stay on the hardware checklist."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from meshcore_home_gateway.core import adverts
from meshcore_home_gateway.protocol.packet import AdvertData, parse_advert, parse_packet
from meshcore_home_gateway.views import iso
from tests.firmware.air import StandIn, Transmission, wait_for_hellos
from tests.firmware.app import BY_CONTACT, App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import Published, log_lines, logged, running
from tests.firmware.household import LANDMARK, Household, household_ready, start

# in [diagnostics], then a table: the packet log on `rx`, and no telemetry
SETTINGS = "publish_rx_log = true\n[telemetry]\nenabled = false\n"
CHECKED_S = 3.0  # the gateway reads the table 2 s after the radio's push
REPEATED_S = 5.0  # longer than a repeater's longest delay for an advert
STRANGER = 9  # a stand-in on the air: a stranger's radio in range of A
MAX_FRAME = 176  # the radio's longest frame (src/helpers/BaseSerialInterface.h:5)
# a `contact` event: never a coordinate
EVENT_KEYS = {"event_type", "key", "prefix", "name", "type", "request", "id", "reason"}


@dataclass(frozen=True)
class Sent:
    """One of B's adverts on the air."""

    transmission: Transmission
    advert: AdvertData

    @property
    def at(self) -> str | None:
        """Its time as the documents show it."""
        return iso(self.advert.timestamp)


async def button(scene: Scene, household: Household) -> Sent:
    """B's Advert button: a zero-hop advert."""
    after = scene.air.count()
    household.bob.control("advert")
    return await sent(scene, household, "ADVERT/D", after)


async def flood_advert(scene: Scene, household: Household, bob: App) -> Sent:
    """A flood advert from B's app."""
    after = scene.air.count()
    await bob.advert(flood=True)
    return await sent(scene, household, "ADVERT/F", after)


async def sent(scene: Scene, household: Household, kind: str, after: int) -> Sent:
    found = await scene.air.next_sent(household.bob.node_id, kind, after=after, timeout_s=10)
    advert = advert_in(found)
    assert advert is not None
    assert advert.public_key == household.bob_key
    return Sent(found, advert)


def advert_in(transmission: Transmission) -> AdvertData | None:
    packet = parse_packet(transmission.packet)
    return parse_advert(packet) if packet is not None else None


def copies(scene: Scene, repeater: int, advert: Sent) -> list[Transmission]:
    """The repeater's transmissions of this advert."""
    return [t for t in scene.air.sent_by(repeater, "ADVERT/") if advert_in(t) == advert.advert]


async def repeated(scene: Scene, repeater: int, receiver: int, advert: Sent) -> Transmission:
    """The repeater's copy of this advert, once the receiver has it."""
    deadline = time.monotonic() + REPEATED_S
    while not (found := [t for t in copies(scene, repeater, advert) if receiver in t.outcomes]):
        if time.monotonic() > deadline:
            raise AssertionError(f"node {repeater} did not repeat the advert to {receiver}")
        await asyncio.sleep(0.05)
    return found[0]


async def arrived(scene: Scene, advert: Sent, receiver: int) -> None:
    """Until the receiver has this advert."""
    deadline = time.monotonic() + REPEATED_S
    while not scene.air.again(advert.transmission).heard_by(receiver):
        if time.monotonic() > deadline:
            raise AssertionError(f"node {receiver} did not receive the advert")
        await asyncio.sleep(0.05)


async def checked(scene: Scene, key: str, advert: Sent) -> Published:
    """B's document once it shows the check of this advert."""
    return await scene.watcher.wait(
        f"contacts/{key}", lambda d: d["advert_checked_at"] == advert.at, timeout_s=10
    )


def in_advert(scene: Scene, key: str, since: int = 0) -> list[dict[str, Any]]:
    """The `position_in_advert` events of the contact."""
    events = [m.json() for m in scene.watcher.on("contact", since)]
    return [e for e in events if e["event_type"] == "position_in_advert" and e["key"] == key]


def landmark(advert: AdvertData) -> bool:
    """Whether the advert carries B's GPS fix (the firmware sends whole 10^-6 degrees)."""
    latitude, longitude, _ = LANDMARK
    assert advert.latitude_e6 is not None
    assert advert.longitude_e6 is not None
    return (
        abs(advert.latitude_e6 / 1e6 - latitude) < 2e-6
        and abs(advert.longitude_e6 / 1e6 - longitude) < 2e-6
    )


def from_advert(document: dict[str, Any], advert: AdvertData) -> bool:
    """Whether the document's position is the one this advert carried."""
    assert advert.latitude_e6 is not None
    assert advert.longitude_e6 is not None
    return document["position_source"] == "advert" and (
        document["latitude"],
        document["longitude"],
    ) == (advert.latitude_e6 / 1e6, advert.longitude_e6 / 1e6)


async def share(bob: App, on: bool) -> None:
    """B's app: Share Position in Advert on or off; the telemetry settings stay as set up."""
    await bob.settings(BY_CONTACT, share_position=on)


@pytest.mark.broker
def test_b_beside_a_shares_its_position_in_an_advert_and_stops(scene: Scene) -> None:
    """Step 41, beside A. With sharing on and a GPS fix, B's Advert button gives, within about
    2 s, `advert_carries_position` true with `advert_checked_at` the advert's time, B in the
    index's `advert_carries_position` (the gateway's sensor), the advert's position on B's
    document, one `position_in_advert` without a coordinate, and the info line. With sharing off
    the next Advert gives `false`, B out of the index, the advert position gone while the radio's
    record keeps one (`advert_position` true), the info line that says so, and no event."""
    household = start(scene.nodes, scene.air)
    alice = household.alice.node_id
    watcher = scene.watcher

    async def scenario() -> tuple[str, list[Any], list[Sent], float]:
        async with App(household.bob) as bob:
            async with App(household.alice) as app:
                await household_ready(household, app, bob, route=False)
            key = household.bob_key.hex()
            async with running(scene.configuration(household.alice, SETTINGS), scene.state_dir):
                first = (await watcher.wait(f"contacts/{key}")).json()
                await share(bob, True)
                on = await button(scene, household)
                shared = await checked(scene, key, on)
                index_on = await watcher.settled("contacts", lambda d: d["advert_carries_position"])
                await share(bob, False)
                off = await button(scene, household)
                stopped = (await checked(scene, key, off)).json()
                index_off = await watcher.settled(
                    "contacts", lambda d: d["advert_carries_position_total"] == 0
                )
        heard_at = scene.air.started + scene.air.again(on.transmission).ended[alice]
        documents = [first, shared.json(), index_on, stopped, index_off]
        return key, documents, [on, off], shared.at - heard_at

    key, documents, adverts, delay = asyncio.run(scenario())
    first, shared, index_on, stopped, index_off = documents
    on, off = adverts
    assert (first["advert_carries_position"], first["advert_checked_at"]) == (False, None)
    assert landmark(on.advert)
    assert delay < CHECKED_S, delay
    assert shared["advert_carries_position"] is True
    assert shared["advert_checked_at"] == shared["last_advert_at"] == on.at
    assert shared["advert_position"] is True
    assert from_advert(shared, on.advert)
    assert (index_on["advert_carries_position"], index_on["advert_carries_position_total"]) == (
        [key], 1,
    )  # fmt: skip
    (event,) = in_advert(scene, key)
    assert set(event) == EVENT_KEYS
    # name and type as the radio's table has them
    assert (event["name"], event["type"], event["request"]) == (shared["name"], "chat", None)
    assert off.advert.latitude_e6 is None
    assert stopped["advert_carries_position"] is False
    assert stopped["advert_checked_at"] == stopped["last_advert_at"] == off.at
    assert stopped["advert_position"] is True  # the radio's record keeps the older one
    assert (stopped["latitude"], stopped["position_source"]) == (None, None)
    assert (index_off["advert_carries_position"], index_off["advert_position"]) == ([], [key])
    assert len(log_lines(scene.caplog, f"contact {key[:12]}: its advert carries its position")) == 1
    kept = f"contact {key[:12]}: its advert carries no position (the radio's record keeps an older"
    assert len(log_lines(scene.caplog, kept)) == 1


@pytest.mark.broker
def test_behind_a_repeater_only_the_apps_flood_advert_is_heard_and_checked(scene: Scene) -> None:
    """Step 41, behind the repeater (B out of A's direct range), sharing on. The device's Advert
    button sends zero hop: the repeater hears it and does not forward it, so B's document keeps
    its `last_advert_at` and has no check. The app's flood advert reaches A over the repeater and
    is checked, one hop, with one `position_in_advert`; the next one moves `advert_checked_at`
    and raises no second event."""
    household = start(scene.nodes, scene.air, hops=1)
    alice, repeater, _ = (n.node_id for n in household.chain)

    async def scenario() -> tuple[str, list[Any], list[Sent]]:
        async with App(household.bob) as bob:
            async with App(household.alice) as app:
                await household_ready(household, app, bob, route=False)
            key = household.bob_key.hex()
            topic = f"contacts/{key}"
            async with running(scene.configuration(household.alice, SETTINGS), scene.state_dir):
                first = (await scene.watcher.wait(topic)).json()
                await share(bob, True)
                pressed = await button(scene, household)
                await asyncio.sleep(REPEATED_S)
                unheard = scene.watcher.latest(topic)
                flooded = await flood_advert(scene, household, bob)
                over = (await checked(scene, key, flooded)).json()
                again = await flood_advert(scene, household, bob)
                moved = (await checked(scene, key, again)).json()
        return key, [first, unheard, over, moved], [pressed, flooded, again]

    key, documents, adverts = asyncio.run(scenario())
    first, unheard, over, moved = documents
    pressed, flooded, again = adverts
    assert scene.air.again(pressed.transmission).outcomes == {repeater: "received"}
    assert copies(scene, repeater, pressed) == []
    for advert in (flooded, again):
        (copy,) = copies(scene, repeater, advert)
        assert copy.heard_by(alice)
    assert (unheard["last_advert_at"], unheard["advert_checked_at"]) == (
        first["last_advert_at"], None,
    )  # fmt: skip
    assert unheard["last_advert_at"] != pressed.at
    assert landmark(flooded.advert)
    assert over["advert_carries_position"] is True
    assert over["advert_checked_at"] == over["last_advert_at"] == flooded.at
    assert over["hops"] == 1
    assert from_advert(over, flooded.advert)
    assert moved["advert_carries_position"] is True
    assert moved["advert_checked_at"] == again.at != flooded.at
    assert len(in_advert(scene, key)) == 1


@pytest.mark.broker
def test_a_companion_heard_before_the_household_adds_it_starts_checked(scene: Scene) -> None:
    """Step 41, the last part. B shares its position and A does not hold B: the gateway's profile
    makes A add contacts by hand only, so B's Advert arrives as NEW_ADVERT, and the gateway
    checks it then. Added through the gateway (`add_contact`, the request `contacts add` sends),
    B starts with `advert_carries_position` true and `advert_checked_at` that advert's time; one
    `position_in_advert` follows the request's `added`. The radio's new record holds no position,
    so B's document has none until B's next Advert, which raises no second event."""
    household = start(scene.nodes, scene.air)

    async def scenario() -> tuple[str, list[Any], list[Sent]]:
        async with App(household.bob) as bob:
            household.bob_key = (await bob.start()).public_key
            async with App(household.alice) as app:
                await app.start()  # A's clock, as the app sets it
            await bob.settings(BY_CONTACT, share_position=True)
            await bob.set_var("gps", "1")
            key = household.bob_key.hex()
            async with running(scene.configuration(household.alice, SETTINGS), scene.state_dir):
                index = await scene.watcher.wait("contacts")  # read after the profile's writes
                pressed = await button(scene, household)
                await scene.watcher.wait(
                    "nodes", lambda d: any(n["key"] == key for n in d["nodes"])
                )
                request = json.dumps({"contact": key, "id": "add-bob"})
                scene.watcher.publish("add_contact", request)
                added = (await checked(scene, key, pressed)).json()
                await scene.watcher.wait(
                    "contact", lambda e: e["key"] == key and e["request"] is None
                )
                following = await button(scene, household)
                later = (await checked(scene, key, following)).json()
        return key, [index.json(), added, later], [pressed, following]

    key, documents, adverts = asyncio.run(scenario())
    index, added, later = documents
    pressed, following = adverts
    assert key not in {entry["key"] for entry in index["contacts"]}
    assert landmark(pressed.advert)
    assert added["advert_carries_position"] is True
    assert added["advert_checked_at"] == added["last_advert_at"] == pressed.at
    assert added["advert_position"] is False  # the record the gateway wrote holds none
    assert (added["latitude"], added["position_source"]) == (None, None)
    events = [m.json() for m in scene.watcher.on("contact")]
    assert [(e["event_type"], e["key"], e["request"], e["id"]) for e in events] == [
        ("added", key, "add", "add-bob"), ("position_in_advert", key, None, None),
    ]  # fmt: skip
    assert set(events[1]) == EVENT_KEYS
    assert later["advert_checked_at"] == following.at
    assert later["advert_carries_position"] is True
    assert from_advert(later, following.advert)


def advert_checks(state_dir: Path) -> Any:
    return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))["advert_checks"]


@pytest.mark.broker
def test_a_restart_keeps_the_check_and_an_advert_while_stopped_stays_unchecked(
    scene: Scene,
) -> None:
    """Step 43. B shares and is checked `true`; a restart between that advert and the next keeps
    the check (`state.json` holds B's advert time and flag, no position) and raises nothing.
    Sharing off, checked `false`; the service stops, B shares again and advertises beside A, the
    service starts: the radio's record took the position, but the advert is unchecked, so
    `advert_checked_at` is older than `last_advert_at`, `advert_carries_position` errs to
    `true`, no position comes back to the document, no event, one debug line "not checked". The
    next Advert checks it: `advert_checked_at` moves, one `position_in_advert`."""
    household = start(scene.nodes, scene.air)
    watcher = scene.watcher
    stored: list[Any] = []

    async def scenario(key: str, bob: App) -> tuple[list[Any], list[Sent], list[int]]:
        topic, config = f"contacts/{key}", scene.configuration(household.alice, SETTINGS)
        async with running(config, scene.state_dir):
            await watcher.wait(topic)
            await share(bob, True)
            on = await button(scene, household)
            shared = (await checked(scene, key, on)).json()
        stored.append(advert_checks(scene.state_dir))
        second = watcher.mark()
        async with running(config, scene.state_dir):
            restarted = (await watcher.wait(topic, since=second)).json()
            await share(bob, False)
            off = await button(scene, household)
            stopped = (await checked(scene, key, off)).json()
        stored.append(advert_checks(scene.state_dir))
        await share(bob, True)
        unchecked = await button(scene, household)  # A takes it, nobody listens
        await arrived(scene, unchecked, household.alice.node_id)
        await asyncio.sleep(0.5)  # A handles it
        third = watcher.mark()
        async with running(config, scene.state_dir):
            started = (await watcher.wait(topic, since=third)).json()
            index = await watcher.settled("contacts", lambda d: d["advert_carries_position"])
            before_next = watcher.mark()
            following = await button(scene, household)
            last = (await checked(scene, key, following)).json()
        stored.append(advert_checks(scene.state_dir))
        documents = [shared, restarted, stopped, started, index, last]
        return documents, [on, off, unchecked, following], [second, before_next]

    async def everything() -> tuple[str, list[Any], list[Sent], list[int]]:
        async with App(household.bob) as bob:
            async with App(household.alice) as app:
                await household_ready(household, app, bob, route=False)
            key = household.bob_key.hex()
            return key, *await scenario(key, bob)

    key, documents, adverts, marks = asyncio.run(everything())
    shared, restarted, stopped, started, index, last = documents
    on, off, unchecked, following = adverts
    second, before_next = marks
    assert shared["advert_carries_position"] is True
    assert stored[0] == {key: {"advert_at": on.advert.timestamp, "carries": True}}
    assert restarted["advert_checked_at"] == on.at
    assert restarted["advert_carries_position"] is True
    assert from_advert(restarted, on.advert)
    # no contact event from the second start to the Advert after the third start
    assert len(watcher.on("contact", second)) == len(watcher.on("contact", before_next))
    assert stopped["advert_carries_position"] is False
    assert (stopped["latitude"], stopped["position_source"]) == (None, None)
    assert stored[1] == {key: {"advert_at": off.advert.timestamp, "carries": False}}
    assert landmark(unchecked.advert)
    assert started["last_advert_at"] == unchecked.at
    assert started["advert_checked_at"] == off.at  # older: the advert while stopped
    assert started["advert_carries_position"] is True  # the record took a position: errs to warn
    assert started["advert_position"] is True
    assert (started["latitude"], started["position_source"]) == (None, None)
    assert index["advert_carries_position"] == [key]
    not_checked = f"contact {key[:12]}: its advert at {unchecked.at} was not checked"
    assert len(log_lines(scene.caplog, not_checked)) == 1
    assert last["advert_checked_at"] == following.at
    assert last["advert_carries_position"] is True
    assert from_advert(last, following.advert)
    assert len(in_advert(scene, key, before_next)) == 1
    assert len(in_advert(scene, key)) == 2  # the first check and the one after the stop
    assert stored[2] == {key: {"advert_at": following.advert.timestamp, "carries": True}}
    assert all(m.payload for m in watcher.on(f"contacts/{key}"))  # never cleared


def logs_of(scene: Scene, advert: Sent) -> list[tuple[int, float, int]]:
    """The advert's copies in the packet log (`rx`): their hops, SNR and RSSI."""
    found = []
    for message in scene.watcher.on("rx"):
        entry = message.json()
        packet = parse_packet(bytes.fromhex(entry["raw"]))
        if packet is not None and parse_advert(packet) == advert.advert:
            found.append((len(packet.path_hashes), entry["snr_db"], entry["rssi_dbm"]))
    return found


@pytest.mark.broker
def test_an_adverts_signal_is_that_of_the_reception_the_radio_took(scene: Scene) -> None:
    """B's flood advert reaches A twice: directly, which the radio takes, and over the repeater,
    which it logs as well and drops as a copy. `rx` shows both. The hops, SNR and RSSI on B's
    document are those of the direct reception: the repeater's copy is logged after the radio's
    push and does not replace them. A gateway that drops the signal fails. At receive delay 0 an
    advert's own log comes directly before its push, so any rule that reads a log at the push
    gives these values here; the next test tells the rules apart."""
    household = start(scene.nodes, scene.air, hops=1)
    alice, repeater, bob_id = (n.node_id for n in household.chain)
    scene.air.link(bob_id, alice, snr=6.5, rssi=-96)  # in range of A as well
    scene.air.link(repeater, alice, snr=2.25, rssi=-106, both=False)  # the copy comes weaker

    async def scenario() -> tuple[dict[str, Any], Sent]:
        async with App(household.bob) as bob:
            async with App(household.alice) as app:
                await household_ready(household, app, bob, route=False)
            key = household.bob_key.hex()
            topic = f"contacts/{key}"
            async with running(scene.configuration(household.alice, SETTINGS), scene.state_dir):
                await scene.watcher.wait(topic)
                flooded = await flood_advert(scene, household, bob)
                await checked(scene, key, flooded)
                await repeated(scene, repeater, alice, flooded)
                await asyncio.sleep(0.5)  # its log reaches the broker
                document = scene.watcher.latest(topic)  # after both logs
        return document, flooded

    document, flooded = asyncio.run(scenario())
    logs = logs_of(scene, flooded)
    assert logs == [(0, 6.5, -96), (1, 2.25, -106)]  # direct, then the repeater's copy
    assert (document["hops"], document["snr_db"], document["rssi_dbm"]) == logs[0]
    assert document["advert_checked_at"] == flooded.at


def forwarded(advert: Sent, path: bytes) -> bytes:
    """The advert as a flood that passed the repeaters of `path` (one-byte hashes). Its signature
    does not cover the path (src/Mesh.cpp:271-280)."""
    packet = parse_packet(advert.transmission.packet)
    assert packet is not None
    header = (advert.transmission.packet[0] & ~0x03) | 0x01  # the route: flood
    return bytes([header, len(path)]) + path + packet.payload


async def relay(scene: Scene, stranger: StandIn, packet: bytes, airtime_ms: int, to: int) -> Sent:
    """The stranger sends the packet, until `to` has it."""
    after = scene.air.count()
    stranger.transmit(packet, airtime_ms)
    found = await scene.air.next_sent(stranger.node_id, "ADVERT/F", after=after, timeout_s=10)
    advert = advert_in(found)
    assert advert is not None
    relayed = Sent(found, advert)
    await arrived(scene, relayed, to)
    return relayed


@pytest.mark.broker
def test_an_advert_without_its_own_log_has_no_signal_and_no_check(scene: Scene) -> None:
    """B shares and is checked. A no longer hears B, and B advertises again. A stranger's radio
    (a stand-in on the air) sends A a late copy of B's first advert over one hop, then B's new
    advert over 63 hops. The radio logs no packet whose log would not fit its 176-byte frame
    (examples/companion_radio/MyMesh.cpp:291-302), so it takes the new advert without a log, and
    the frame right before its push is the log of the late copy. The gateway gives the push no
    hops, SNR or RSSI and does not check it: `advert_checked_at` stays at the first advert while
    `last_advert_at` moves, one debug line "not checked (not logged)", no second event. A
    gateway that takes the late copy's signal fails here: one that reads any log of the key within
    10 s, the newest log, or the log right before the push whatever its time."""
    household = start(scene.nodes, scene.air)
    alice, bob_id = household.alice.node_id, household.bob.node_id
    stranger = StandIn(STRANGER, scene.air.port, scene.air.settings()[alice])
    wait_for_hellos(scene.air, [stranger])
    scene.air.link(STRANGER, alice, snr=3.5, rssi=-104, both=False)
    adjacent: list[adverts.Logged | None] = []  # what the gateway found before each push
    original = adverts.adjacent

    def spy(last: adverts.Logged | None, key: str, frame: adverts.FrameId) -> adverts.Logged | None:
        adjacent.append(found := original(last, key, frame))
        return found

    async def scenario() -> tuple[str, list[Any], list[Sent]]:
        async with App(household.bob) as bob:
            async with App(household.alice) as app:
                await household_ready(household, app, bob, route=False)
            key = household.bob_key.hex()
            topic, config = f"contacts/{key}", scene.configuration(household.alice, SETTINGS)
            with mock.patch.object(adverts, "adjacent", spy):
                async with running(config, scene.state_dir):
                    await scene.watcher.wait(topic)
                    await share(bob, True)
                    first = await button(scene, household)
                    shared = (await checked(scene, key, first)).json()
                    scene.air.unlink(bob_id, alice, both=False)
                    second = await button(scene, household)  # nobody hears it
                    hashes = bytes(range(0x40, 0x7F))  # 63 repeaters
                    late = forwarded(first, hashes[:1])
                    new = forwarded(second, hashes)
                    assert len(new) + 3 > MAX_FRAME  # the log: code, SNR, RSSI, the packet
                    copy = await relay(scene, stranger, late, first.transmission.airtime_ms, alice)
                    await relay(scene, stranger, new, second.transmission.airtime_ms, alice)
                    taken = await scene.watcher.wait(
                        topic, lambda d: d["last_advert_at"] == second.at
                    )
                    not_checked = f"contact {key[:12]}: its advert at {second.at} was not checked"
                    await logged(scene.caplog, f"{not_checked} (not logged)")
        return key, [shared, taken.json()], [first, second, copy]

    try:
        key, documents, sent = asyncio.run(scenario())
    finally:
        stranger.socket.close()
    shared, taken = documents
    first, second, copy = sent
    assert second.advert.timestamp > first.advert.timestamp
    assert copy.advert == first.advert
    assert logs_of(scene, first) == [(0, 8.0, -102), (1, 3.5, -104)]  # then the late copy
    assert logs_of(scene, second) == []  # too long for a log
    assert (shared["advert_checked_at"], shared["advert_carries_position"]) == (first.at, True)
    assert (taken["hops"], taken["snr_db"], taken["rssi_dbm"]) == (None, None, None)
    assert taken["heard_via"] == "advert"
    assert taken["heard_at"] > shared["heard_at"]
    assert len(adjacent) == 2  # the radio's two pushes
    before_push = adjacent[1]
    assert before_push is not None
    assert before_push.timestamp == first.advert.timestamp  # the late copy's log
    assert (taken["advert_checked_at"], taken["advert_carries_position"]) == (first.at, True)
    assert len(log_lines(scene.caplog, f"contact {key[:12]}: its advert at {second.at}")) == 1
    assert len(in_advert(scene, key)) == 1
