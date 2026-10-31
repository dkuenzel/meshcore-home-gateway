"""What the firmware does with adverts and positions, at the protocol level: the facts the
advert-position check of the next telemetry slice relies on (docs/design.md, telemetry), seen on
the companion's serial port. No gateway runs here; phone apps drive the radios."""

from __future__ import annotations

import asyncio

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Push, Response
from meshcore_home_gateway.protocol.packet import AdvertData, parse_advert, parse_packet
from tests.firmware.air import AirThread, Transmission
from tests.firmware.app import App, Frame
from tests.firmware.conftest import Nodes
from tests.firmware.household import LANDMARK, SNR

# the Reichstag building in Berlin, a public landmark, and an invented altitude
SECOND_LANDMARK = (52.518620, 13.376187, 42.0)
GPS_S = 1.5  # the firmware reads its GPS every second
CONTACTS_SAVED_S = 6.0  # the firmware writes its contacts 5 s after a change
ALLOW_ALL = 0b00_10_10  # base and location to anyone who is a contact (NodePrefs.h:7-9)


def advert_in(frame: Frame) -> AdvertData | None:
    """The advert in a packet log push (0x88), if it carries one."""
    if frame.code != Push.LOG_RX_DATA:
        return None
    logged = codec.decode(frame.payload)
    assert isinstance(logged, codec.RxLog)
    packet = parse_packet(logged.raw)
    return parse_advert(packet) if packet is not None else None


async def advert_heard(sender: App, receiver: App, key: bytes, push: int) -> AdvertData:
    """The sender's app sends a zero-hop advert; the receiver's radio pushes `push` for it. The
    frame right before that push is the packet log of the same advert, which is returned."""
    since = receiver.mark()
    await sender.advert(flood=False)
    pushed = await receiver.push(push, since, key, timeout_s=10)
    frames = receiver.frames
    before = frames[frames.index(pushed) - 1]
    logged = advert_in(before)
    assert logged is not None, f"0x{before.code:02x} before the push"
    assert logged.public_key == key
    await asyncio.sleep(1.2)  # an advert's timestamp is in seconds: the next one is newer
    return logged


def test_an_advert_is_logged_right_before_its_push_and_sets_a_flagged_position(
    nodes: Nodes, air: AirThread
) -> None:
    """At receive delay 0, an accepted advert's packet log (0x88) is the frame directly before
    the radio's push (0x80 ADVERT); a node the radio does not add arrives as NEW_ADVERT (0x8A)
    right after its log. An advert flagged with a position (0x10) sets the record's position;
    one without the flag leaves the old one; one flagged with 0, 0 zeroes it."""
    alice, bob, carol = (nodes.companion(name, wait=False) for name in ("A", "B", "C"))
    nodes.ready()
    for other in (bob, carol):
        air.link(alice.node_id, other.node_id, snr=SNR)
    latitude, longitude, _ = LANDMARK

    async def scenario() -> None:
        async with App(alice) as a, App(bob) as b, App(carol) as c:
            await a.start()
            tuning = codec.decode(
                await a.command(codec.get_tuning_params(), {Response.TUNING_PARAMS}, "tuning")
            )
            assert isinstance(tuning, codec.TuningParams)
            assert tuning.rx_delay_base_milli == 0
            bob_key = (await b.start()).public_key
            await b.set_position(latitude, longitude)
            await b.settings(0, share_position=True)
            flagged = await advert_heard(b, a, bob_key, Push.ADVERT)  # added: a fresh radio adds
            assert (flagged.latitude_e6, flagged.longitude_e6) == (
                round(latitude * 1e6),
                round(longitude * 1e6),
            )
            assert (await a.contact(bob_key)).latitude_e6 == flagged.latitude_e6
            await b.settings(0, share_position=False)
            unflagged = await advert_heard(b, a, bob_key, Push.ADVERT)
            assert unflagged.latitude_e6 is None
            record = await a.contact(bob_key)
            assert (record.latitude_e6, record.longitude_e6) == (
                flagged.latitude_e6, flagged.longitude_e6,
            )  # fmt: skip
            await b.set_position(0.0, 0.0)
            await b.settings(0, share_position=True)
            zeroed = await advert_heard(b, a, bob_key, Push.ADVERT)
            assert (zeroed.latitude_e6, zeroed.longitude_e6) == (0, 0)
            record = await a.contact(bob_key)
            assert (record.latitude_e6, record.longitude_e6) == (0, 0)
            await a.settings(0, manual_add=True)  # adding by hand only, as the gateway sets it
            await a.ok(codec.set_autoadd_config(0), "auto-add")
            carol_key = (await c.start()).public_key
            await advert_heard(c, a, carol_key, Push.NEW_ADVERT)
            assert carol_key not in await a.contacts()

    asyncio.run(scenario())


def forwarded(air: AirThread, repeater: int, key: bytes, *, after: int) -> list[Transmission]:
    """The repeater's transmissions of the node's adverts (not its own)."""
    found = []
    for sent in air.transmissions()[after:]:
        packet = parse_packet(sent.packet) if sent.sender == repeater else None
        advert = parse_advert(packet) if packet is not None else None
        if advert is not None and advert.public_key == key:
            found.append(sent)
    return found


def test_the_advert_button_sends_zero_hop_and_the_apps_flood_advert_is_repeated(
    nodes: Nodes, air: AirThread
) -> None:
    """The device's Advert button sends a zero-hop advert, which a repeater does not forward;
    the app's flood advert it forwards."""
    alice, repeater, bob = (
        nodes.companion("A", wait=False), nodes.repeater("R", wait=False),
        nodes.companion("B", wait=False),
    )  # fmt: skip
    nodes.ready()
    air.link(alice.node_id, repeater.node_id, snr=SNR)
    air.link(repeater.node_id, bob.node_id, snr=SNR)

    async def scenario() -> None:
        async with App(alice) as a, App(bob) as b:
            await a.start()
            bob_key = (await b.start()).public_key
            since, sent = a.mark(), air.count()
            bob.control("advert")  # the button
            button = await air.next_sent(bob.node_id, "ADVERT/", after=sent, timeout_s=10)
            await asyncio.sleep(5)  # longer than the repeater's longest delay at this length
            assert button.kind == "ADVERT/D"
            assert air.again(button).outcomes == {repeater.node_id: "received"}
            assert forwarded(air, repeater.node_id, bob_key, after=sent) == []
            assert [
                f
                for f in a.frames[since:]
                if (found := advert_in(f)) and found.public_key == bob_key
            ] == []
            sent = air.count()
            await b.advert(flood=True)
            pushed = await a.push(Push.ADVERT, since, bob_key, timeout_s=15)
            logged = advert_in(a.frames[a.frames.index(pushed) - 1])
            assert logged is not None
            assert logged.public_key == bob_key
        repeated = forwarded(air, repeater.node_id, bob_key, after=sent)
        assert [t.kind for t in repeated] == ["ADVERT/F"]
        assert repeated[0].heard_by(alice.node_id)

    asyncio.run(scenario())


def at(found: lpp.LppPosition, place: tuple[float, float, float]) -> bool:
    """Whether the position is the place's, to the cell of 1e-4 degrees."""
    latitude, longitude, _ = place
    return (
        abs(found.lat_cells / 1e4 - latitude) < 1e-4
        and abs(found.lon_cells / 1e4 - longitude) < 1e-4
    )


def test_without_a_fix_b_sends_its_last_position_and_after_a_reboot_the_saved_one(
    nodes: Nodes, air: AirThread
) -> None:
    """With the GPS on and no fix, a companion's telemetry still carries a position: the last
    fix while it runs, and after a reboot the position it saved with its settings, at altitude
    0 (the firmware saves latitude and longitude, not the altitude)."""
    alice = nodes.companion("A", wait=False)
    bob = nodes.companion("B", gps="nofix", wait=False)  # a GPS module, no fix yet
    nodes.ready()
    air.link(alice.node_id, bob.node_id, snr=SNR)

    async def position(a: App, key: bytes) -> lpp.LppPosition | None:
        since = a.mark()
        await a.request_telemetry(key)
        answer = await a.push(Push.TELEMETRY_RESPONSE, since, key, timeout_s=15)
        found = codec.decode(answer.payload)
        assert isinstance(found, codec.TelemetryResponse)
        return lpp.position(lpp.decode(found.data))

    def fix(place: tuple[float, float, float]) -> None:
        bob.control("gps {} {} {}".format(*place))

    async def scenario() -> list[lpp.LppPosition | None]:
        async with App(alice) as a, App(bob) as b:
            alice_key = (await a.start()).public_key
            bob_key = (await b.start()).public_key
            await b.settings(ALLOW_ALL)
            await b.set_var("gps", "1")
            await a.advert(flood=False)
            await b.wait_for_contact(alice_key)
            await b.advert(flood=False)
            await a.wait_for_contact(bob_key)
            found = []
            fix(LANDMARK)
            await asyncio.sleep(GPS_S)
            found.append(await position(a, bob_key))
            await b.settings(ALLOW_ALL)  # any setting saves the settings, the position with them
            fix(SECOND_LANDMARK)
            await asyncio.sleep(GPS_S)
            found.append(await position(a, bob_key))
            bob.control("gps nofix")
            await asyncio.sleep(GPS_S)
            found.append(await position(a, bob_key))
            await asyncio.sleep(CONTACTS_SAVED_S)  # B keeps A's path across the reboot
            await asyncio.to_thread(bob.reboot)  # starts again without a fix
            await asyncio.sleep(GPS_S)
            found.append(await position(a, bob_key))
        return found

    first, second, last, saved = asyncio.run(scenario())
    assert first is not None
    assert at(first, LANDMARK)
    assert first.altitude_cm == round(LANDMARK[2] * 100)
    assert second is not None
    assert at(second, SECOND_LANDMARK)  # the second fix took effect
    assert second.altitude_cm == round(SECOND_LANDMARK[2] * 100)
    assert second.cells != first.cells
    assert last == second  # no fix: the last one, with its altitude
    assert saved is not None
    assert saved.cells == first.cells  # saved with the settings, before the second fix
    assert saved.cells != last.cells
    assert saved.altitude_cm == 0
