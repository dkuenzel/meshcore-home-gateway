"""The radio's clock on the real firmware: it takes no earlier time, but a restart starts it at its
newest contact time plus a second (BaseChatMesh.cpp:60-70), so the gateway sets a clock ahead of
the host's back with a restart, after the contact times the clock stamped ahead, and the next
connect moves it forward (radio/session.py, docs/protocol.md section 5)."""

from __future__ import annotations

import asyncio
import errno
import re
import time

from meshcore_home_gateway.ports import RadioError, RadioEvent, RadioRestartedError
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import CLOCK_JUMP_S, CompanionSession
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


def test_a_radio_clock_a_jump_ahead_is_set_back_with_the_contact_times_it_stamped(
    nodes: Nodes, air: AirThread
) -> None:
    """On nRF52 without a clock chip the clock jumps 100,663 s ahead every 48.5 days of uptime
    (MeshCore #3565); the native build does not, so the test sets the clock that far ahead. An
    advert then stamps the contact's time ahead (BaseChatMesh.cpp:198), where a restart alone
    would start the clock again: the gateway writes that time back first."""
    alice, bob = nodes.companion("A", wait=False), nodes.companion("B", wait=False)
    nodes.ready()
    air.link(alice.node_id, bob.node_id, snr=SNR)
    jump = round(CLOCK_JUMP_S) + 20

    async def scenario() -> tuple[RadioError | None, int | None, int, int]:
        async with App(alice) as a, App(bob) as b:
            await a.start()
            bob_key = (await b.start()).public_key
            await b.advert(flood=False)
            await a.wait_for_contact(bob_key)
            await a.ok(codec.set_device_time(int(time.time()) + jump), "set time")
            await asyncio.sleep(1.1)  # B's next advert carries a later time of its own
            await b.advert(flood=False)
            deadline = time.monotonic() + 30
            while (await a.contact(bob_key)).last_modified < time.time() + jump - 120:
                assert time.monotonic() < deadline, "A did not take B's second advert"
                await asyncio.sleep(0.5)
        session, ended = await connect(alice)
        await session.close()
        await asyncio.to_thread(alice.wait_ready, starts=2)
        session, error = await connect(alice)
        try:
            assert error is None
            offset = session.clock_offset_s
            off = await session.get_time() - int(time.time())
            stamped = session.contact_records[bob_key.hex()].last_modified - int(time.time())
        finally:
            await session.close()
        return ended, offset, off, stamped

    ended, offset, off, stamped = asyncio.run(scenario())
    assert isinstance(ended, RadioRestartedError)
    found = re.fullmatch(r"restarted to set its clock back by (\d+) s", str(ended))
    assert found is not None
    assert abs(int(found.group(1)) - jump) <= 2
    assert offset == 0
    assert abs(off) <= 2
    assert -120 < stamped <= 0  # the time B's advert came, by the host's clock
