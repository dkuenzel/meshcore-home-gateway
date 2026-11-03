"""The outgoing queue: alerts first, each kind in order, bounded (IMP-032, ADR 0028)."""

import asyncio

import pytest

from meshcore_home_gateway.core.model import MessageKind, Target
from meshcore_home_gateway.outbox import Outbox, Outgoing

CHANNEL = Target("alerts", MessageKind.CHANNEL, channel_index=0)


def message(name: str, *, alert: bool = False) -> Outgoing:
    return Outgoing(name, CHANNEL, name, 0.0, alert=alert)


def drained(outbox: Outbox) -> list[str]:
    async def take() -> list[str]:
        return [(await outbox.get()).id for _ in range(outbox.qsize())]

    return asyncio.run(take())


def test_alerts_leave_first_and_each_kind_in_order() -> None:
    outbox = Outbox(10)
    for item in (message("a"), message("A", alert=True), message("b"), message("B", alert=True)):
        outbox.put(item)
    assert outbox.alert_waiting
    assert drained(outbox) == ["A", "B", "a", "b"]
    assert not outbox.alert_waiting


def test_a_message_put_back_goes_first_among_its_kind_also_beyond_the_size() -> None:
    """Paused for an alert, or kept at a stop: it had its place."""
    outbox = Outbox(2)
    outbox.put(message("b"))
    outbox.put(message("c"))
    with pytest.raises(asyncio.QueueFull):
        outbox.put(message("d"))
    outbox.put_back(message("a"))
    outbox.restore(message("e"))  # kept by the last run: in its order
    assert outbox.qsize() == 4
    assert drained(outbox) == ["a", "b", "c", "e"]


def test_take_all_empties_the_queue_in_the_order_it_would_leave() -> None:
    outbox = Outbox(5)
    outbox.put(message("a"))
    outbox.put(message("A", alert=True))
    outbox.put_back(message("first"))
    assert [item.id for item in outbox.take_all()] == ["A", "first", "a"]
    assert outbox.qsize() == 0


def test_get_waits_for_a_message() -> None:
    async def main() -> None:
        outbox = Outbox(1)
        waiting = asyncio.create_task(outbox.get())
        await asyncio.sleep(0)
        assert not waiting.done()
        outbox.put(message("a"))
        assert (await asyncio.wait_for(waiting, 1)).id == "a"

    asyncio.run(main())
