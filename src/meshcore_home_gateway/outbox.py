"""The outgoing queue (ADR 0028): messages wait here for the radio and for airtime, alerts before
the others (IMP-032), each kind in the order it came. A message taken out keeps its progress (its
radio timestamp, the attempts sent, their acknowledgement codes), so that it can be put back:
paused for an alert, or kept for the next start when the service stops (ARC-013)."""

from __future__ import annotations

import asyncio
import heapq
import itertools
from dataclasses import dataclass, field

from meshcore_home_gateway.core.model import Target


@dataclass
class Expected:
    """The acknowledgement codes of a message's attempts, and the shortest round trip a genuine
    acknowledgement of any of them takes (SEC-001): 0 where it cannot be told."""

    codes: list[bytes] = field(default_factory=list)
    shortest_ms: float | None = None  # None: no attempt sent yet

    def add(self, code: bytes, shortest_ms: float) -> None:
        self.codes.append(code)
        if self.shortest_ms is None or shortest_ms < self.shortest_ms:
            self.shortest_ms = shortest_ms

    def suspect(self, round_trip_ms: int) -> bool:
        return self.shortest_ms is not None and round_trip_ms < self.shortest_ms


@dataclass
class Outgoing:
    id: str
    target: Target
    text: str
    created: float  # monotonic
    raw: str | None = None  # a channel text accepted before the radio's name was known
    alert: bool = False  # it overtakes the messages that are not (`[messages] alert_targets`)
    timestamp: int | None = None  # of every attempt, from the first on: retries stay recognisable
    sent: int = 0  # attempts the radio has sent
    acks: Expected = field(default_factory=Expected)


class Outbox:
    """At most `size` messages: `put` refuses one more. A message put back (paused, or kept at
    a stop) goes first among its kind, and may exceed the size: it had its place."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._heap: list[tuple[int, int, Outgoing]] = []  # (0 for an alert, order, message)
        self._order = itertools.count()
        self._front = itertools.count(-1, -1)
        self._ready = asyncio.Event()

    def qsize(self) -> int:
        return len(self._heap)

    @property
    def alert_waiting(self) -> bool:
        return bool(self._heap) and self._heap[0][0] == 0

    def put(self, item: Outgoing) -> None:
        """Raises asyncio.QueueFull when the queue holds `size` messages."""
        if len(self._heap) >= self._size:
            raise asyncio.QueueFull
        self._push(item, next(self._order))

    def put_back(self, item: Outgoing) -> None:
        self._push(item, next(self._front))

    def restore(self, item: Outgoing) -> None:
        """A message kept by the last run, in its order, also beyond the size."""
        self._push(item, next(self._order))

    async def get(self) -> Outgoing:
        while not self._heap:
            self._ready.clear()
            await self._ready.wait()
        return heapq.heappop(self._heap)[2]

    def take_all(self) -> list[Outgoing]:
        """Every message, in the order they would leave; the queue is empty then."""
        return [heapq.heappop(self._heap)[2] for _ in range(len(self._heap))]

    def _push(self, item: Outgoing, order: int) -> None:
        heapq.heappush(self._heap, (0 if item.alert else 1, order, item))
        self._ready.set()
