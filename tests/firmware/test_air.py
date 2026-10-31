"""The simulated air on its own, with stand-in nodes: UDP sockets that send and read the datagrams
of board/SimRadio.h."""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Iterator

import pytest

from tests.firmware.air import (
    EU_NARROW,
    Air,
    AirThread,
    Outcome,
    RadioSettings,
    StandIn,
    wait_for_hellos,
)

QUIET_S = 0.3  # nothing arrives within this time: nothing was sent


@pytest.fixture
def air() -> Iterator[AirThread]:
    with AirThread() as running:
        yield running


@pytest.fixture
def stand_ins(air: AirThread) -> Iterator[list[StandIn]]:
    nodes = [StandIn(node_id, air.port) for node_id in (1, 2, 3)]
    wait_for_hellos(air, nodes)
    yield nodes
    for node in nodes:
        node.socket.close()


def test_a_linked_node_hears_a_transmission_after_its_time_on_air(
    air: AirThread, stand_ins: list[StandIn]
) -> None:
    alice, bob, carol = stand_ins
    air.link(alice.node_id, bob.node_id, snr=6.5, rssi=-100)
    started = time.monotonic()
    alice.transmit(b"\x11packet", 200)
    assert bob.receive() == b"B" + struct.pack("<I", 200)  # channel busy at once
    assert bob.receive() == b"R" + struct.pack("<hh", 26, -100) + b"\x11packet"
    assert time.monotonic() - started >= 0.19
    assert carol.receive(QUIET_S) is None  # not linked
    (sent,) = air.transmissions()
    assert (sent.sender, sent.airtime_ms, sent.outcomes) == (alice.node_id, 200, {2: "received"})
    assert sent.kind == "ADVERT/F"  # header 0x11: payload type 4 in bits 2-5, route 1 in 0-1


def test_overlapping_receptions_are_both_lost(air: AirThread, stand_ins: list[StandIn]) -> None:
    alice, bob, carol = stand_ins
    air.link(alice.node_id, bob.node_id)
    air.link(carol.node_id, bob.node_id)
    alice.transmit(b"\x05first", 300)
    time.sleep(0.1)
    carol.transmit(b"\x05second", 300)
    received = [bob.receive() for _ in range(4)]
    assert received.count(b"X") == 2
    assert [t.outcomes for t in air.transmissions()] == [{2: "collided"}, {2: "collided"}]


def test_a_node_hears_nothing_while_it_transmits(air: AirThread, stand_ins: list[StandIn]) -> None:
    """Half duplex: a reception that overlaps the receiver's own transmission is lost to it,
    whether the reception or the transmission began first; the air records it as `deaf`."""
    alice, bob, _ = stand_ins
    air.link(alice.node_id, bob.node_id)
    alice.transmit(b"\x05long", 300)
    time.sleep(0.1)
    bob.transmit(b"\x05short", 50)  # during alice's packet, which alice still sends
    assert [bob.receive(), bob.receive()] == [b"B" + struct.pack("<I", 300), b"X"]
    assert [alice.receive(), alice.receive()] == [b"B" + struct.pack("<I", 50), b"X"]
    bob.transmit(b"\x05later", 50)  # alice's packet is over: she hears this one
    assert alice.receive() == b"B" + struct.pack("<I", 50)
    assert (alice.receive() or b"")[:1] == b"R"
    sent = air.transmissions()
    assert [t.outcomes for t in sent] == [{2: "deaf"}, {1: "deaf"}, {1: "received"}]
    assert not sent[1].heard_by(alice.node_id)


def test_a_link_can_be_one_way_and_goes_with_unlink(
    air: AirThread, stand_ins: list[StandIn]
) -> None:
    alice, bob, _ = stand_ins
    air.link(alice.node_id, bob.node_id, both=False)
    bob.transmit(b"\x05from bob", 50)
    assert alice.receive(QUIET_S) is None
    alice.transmit(b"\x05from alice", 50)
    assert bob.receive() is not None
    assert bob.receive() is not None
    air.unlink(alice.node_id, bob.node_id)
    alice.transmit(b"\x05again", 50)
    assert bob.receive(QUIET_S) is None


def test_a_lossy_link_drops_packets_the_same_way_at_every_run() -> None:
    def outcomes(seed: int) -> list[Outcome]:
        with AirThread(seed=seed) as air:
            alice, bob = StandIn(1, air.port), StandIn(2, air.port)
            wait_for_hellos(air, [alice, bob])
            air.link(alice.node_id, bob.node_id, loss=0.5)
            for n in range(20):
                alice.transmit(bytes([0x05, n]), 10)
                time.sleep(0.02)  # one after the other, no collisions
            time.sleep(QUIET_S)
            found = [t.outcomes[bob.node_id] for t in air.transmissions()]
            alice.socket.close()
            bob.socket.close()
            return found

    first = outcomes(seed=7)
    assert first == outcomes(seed=7)
    assert set(first) == {"received", "lost"}


def test_a_lost_packet_is_not_even_heard_as_busy(air: AirThread, stand_ins: list[StandIn]) -> None:
    alice, bob, _ = stand_ins
    air.link(alice.node_id, bob.node_id, loss=1.0)
    alice.transmit(b"\x05gone", 50)
    assert bob.receive(QUIET_S) is None
    assert [t.outcomes for t in air.transmissions()] == [{2: "lost"}]


def test_a_delayed_link_hands_over_the_packet_later(
    air: AirThread, stand_ins: list[StandIn]
) -> None:
    alice, bob, carol = stand_ins
    air.link(alice.node_id, bob.node_id, delay_s=0.5)
    air.link(alice.node_id, carol.node_id)
    started = time.monotonic()
    alice.transmit(b"\x05late", 100)
    assert carol.receive() == b"B" + struct.pack("<I", 100)
    assert bob.receive() == b"B" + struct.pack("<I", 100)
    assert time.monotonic() - started >= 0.49  # busy only once the packet reaches it
    assert bob.receive() is not None
    (sent,) = air.transmissions()
    assert sent.ended[bob.node_id] - sent.ended[carol.node_id] == pytest.approx(0.5, abs=0.05)


def test_only_the_coding_rate_may_differ(air: AirThread, stand_ins: list[StandIn]) -> None:
    """The coding rate is in the packet's header; frequency, bandwidth and spreading factor are
    not, so a receiver set to others hears nothing, not even the channel busy."""
    alice, bob, carol = stand_ins
    bob.hello(RadioSettings(869_618, 62_500, 9, 8, 20))
    carol.hello(RadioSettings(869_618, 62_500, 8, 5, 20))
    deadline = time.monotonic() + 2.0
    while air.settings()[bob.node_id].spreading_factor != 9:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    air.link(alice.node_id, bob.node_id)
    air.link(alice.node_id, carol.node_id)
    alice.transmit(b"\x05hello", 50)
    assert carol.receive() is not None
    assert bob.receive(QUIET_S) is None


def test_the_air_runs_in_the_callers_event_loop() -> None:
    async def scenario() -> RadioSettings | None:
        async with Air() as air:
            node = StandIn(7, air.port)
            await asyncio.sleep(QUIET_S)  # the hello crosses the loopback
            node.socket.close()
            return air.settings.get(7)

    assert asyncio.run(scenario()) == EU_NARROW
