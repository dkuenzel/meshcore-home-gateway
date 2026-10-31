"""The simulated air on its own, with stand-in nodes: UDP sockets that send and read the datagrams
of board/SimRadio.h."""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections.abc import Iterator

import pytest

from tests.firmware.air import HELLO, TRANSMIT, Air, AirThread, RadioSettings

EU_NARROW = RadioSettings(869_618, 62_500, 8, 8, 20)
QUIET_S = 0.3  # nothing arrives within this time: nothing was sent


class StandIn:
    """A node as the air sees it."""

    def __init__(self, node_id: int, port: int, settings: RadioSettings = EU_NARROW) -> None:
        self.node_id = node_id
        self._air = ("127.0.0.1", port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.hello(settings)

    def hello(self, settings: RadioSettings) -> None:
        self.socket.sendto(
            HELLO.pack(
                b"H", self.node_id, settings.frequency_khz, settings.bandwidth_hz,
                settings.spreading_factor, settings.coding_rate, settings.tx_power_dbm,
            ),
            self._air,
        )  # fmt: skip

    def transmit(self, packet: bytes, airtime_ms: int) -> None:
        self.socket.sendto(TRANSMIT.pack(b"T", self.node_id, airtime_ms) + packet, self._air)

    def receive(self, timeout: float = 2.0) -> bytes | None:
        self.socket.settimeout(timeout)
        try:
            return self.socket.recv(1024)
        except TimeoutError:
            return None


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


def wait_for_hellos(air: AirThread, nodes: list[StandIn]) -> None:
    deadline = time.monotonic() + 2.0
    while not all(n.node_id in air.settings() for n in nodes):
        assert time.monotonic() < deadline, "the air heard no hello"
        time.sleep(0.01)


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
