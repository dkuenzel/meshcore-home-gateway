"""The radio's clock on the real firmware: it takes no earlier time, but a restart starts it at its
newest contact time plus a second (BaseChatMesh.cpp:60-70), so the gateway sets a clock ahead of
the host's back with a restart and the next connect moves it forward (radio/session.py,
docs/protocol.md section 5)."""

from __future__ import annotations

import asyncio
import errno
import time

from meshcore_home_gateway.ports import RadioError, RadioEvent, RadioRestartedError
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import CompanionSession
from meshcore_home_gateway.radio.transport import SerialTransport
from tests.firmware.air import AirThread
from tests.firmware.app import OPEN_RETRY_S, OPEN_TRIES, App
from tests.firmware.conftest import Nodes
from tests.firmware.node import Node

SNR = 8.0
AHEAD_S = 300


async def connect(node: Node) -> tuple[CompanionSession, RadioError | None]:
    """The service's session on the node's port, opened also right after another program closed
    it (app.py), and what ended its start, if anything. The caller closes it."""
    for attempt in range(OPEN_TRIES):
        events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        session = CompanionSession(SerialTransport(str(node.serial)), events)
        session.may_restart = lambda: True
        try:
            await session.start()
        except OSError as exc:
            await session.close()
            if exc.errno != errno.EBUSY or attempt == OPEN_TRIES - 1:
                raise
            await asyncio.sleep(OPEN_RETRY_S)
            continue
        except RadioError as exc:
            return session, exc
        return session, None
    raise AssertionError("unreachable")


def test_a_radio_clock_ahead_is_set_back_by_a_restart(nodes: Nodes, air: AirThread) -> None:
    alice, bob = nodes.companion("A", wait=False), nodes.companion("B", wait=False)
    nodes.ready()
    air.link(alice.node_id, bob.node_id, snr=SNR)

    async def scenario() -> tuple[RadioError | None, int | None, int]:
        async with App(alice) as a, App(bob) as b:
            await a.start()  # A's clock: the host's
            bob_key = (await b.start()).public_key
            await b.advert(flood=False)
            await a.wait_for_contact(bob_key)  # its time: A's clock when the advert came
            await asyncio.sleep(3)  # that time plus a second is behind the host's
            await a.ok(codec.set_device_time(int(time.time()) + AHEAD_S), "set time")
        session, ended = await connect(alice)
        await session.close()
        await asyncio.to_thread(alice.wait_ready, starts=2)  # the firmware started again
        session, error = await connect(alice)
        try:
            assert error is None
            offset = session.clock_offset_s
            off = await session.get_time() - int(time.time())
        finally:
            await session.close()
        return ended, offset, off

    ended, offset, off = asyncio.run(scenario())
    assert isinstance(ended, RadioRestartedError)
    assert str(ended).startswith("restarted to set its clock back by ")
    assert offset == 0
    assert abs(off) <= 2
