"""How the gateway itself is doing (ARC-012): what its parts count of their own trouble, for the
`health` section of the `state` document and the problem sensor *Gateway degraded*.

The design keeps failures local: a bug in a part is logged with its traceback and the rest goes
on (design §6.6). FAILURES, a filter on the loggers of those parts, counts every such record
(an error with a traceback) for the last hour, and lets the traceback of each place through at
most once a minute, with how many it held back (CODE-008). LoopLag holds how late the event
loop ran: a host that starves the gateway of CPU or disk shows there before it shows as a radio
that drops (CODE-001).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

WINDOW_S = 3600.0  # the health section counts the last hour
FAILURE_LOG_S = 60.0  # the traceback of one place at most this often
LAG_TICK_S = 1.0  # the loop-lag probe sleeps this long between measurements
LAG_WARN_S = 1.0  # a lag from this on is logged (at most every LAG_LOG_S) and degrades
LAG_LOG_S = 60.0
DISK_LOW_MB = 100  # free space below this, where the state and the history are kept, degrades


class MinuteCounts:
    """Events of the last hour, counted per minute: 60 numbers at most, at any rate."""

    def __init__(self) -> None:
        self._minutes: deque[tuple[int, int]] = deque()  # (minute, count), oldest first

    def add(self, now: float) -> None:
        minute = int(now // 60)
        if self._minutes and self._minutes[-1][0] == minute:
            self._minutes[-1] = (minute, self._minutes[-1][1] + 1)
        else:
            self._minutes.append((minute, 1))

    def total(self, now: float) -> int:
        oldest = int((now - WINDOW_S) // 60)
        while self._minutes and self._minutes[0][0] <= oldest:
            self._minutes.popleft()
        return sum(count for _, count in self._minutes)


class Failures(logging.Filter):
    """A filter for the loggers of the parts that catch their own bugs (`log.addFilter`): every
    error record with a traceback is counted; one of a place (logger and message) logged less
    than FAILURE_LOG_S ago is held back, and the next one let through says how many were."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._mono = monotonic
        self.reset()

    def reset(self) -> None:
        self._counts = MinuteCounts()
        self._logged: dict[tuple[str, str], float] = {}  # place -> when last let through
        self._held: dict[tuple[str, str], int] = {}  # place -> held back since

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR or not record.exc_info:
            return True
        now = self._mono()
        self._counts.add(now)
        place = (record.name, str(record.msg))
        last = self._logged.get(place)
        if last is not None and now - last < FAILURE_LOG_S:
            self._held[place] = self._held.get(place, 0) + 1
            return False
        self._logged[place] = now
        if held := self._held.pop(place, 0):
            record.msg, record.args = "%s (%d more since)", (record.getMessage(), held)
        return True

    def count(self) -> None:
        """A failure the caller logs on its own schedule."""
        self._counts.add(self._mono())

    def last_hour(self) -> int:
        return self._counts.total(self._mono())


FAILURES = Failures()  # one per process, on the loggers of the service's parts


class LoopLag:
    """How late the event loop ran: the largest lateness in the last hour, kept per minute."""

    def __init__(self) -> None:
        self._minutes: deque[tuple[int, float]] = deque()  # (minute, largest lag)
        self._warned: float | None = None

    def record(self, lag: float, now: float) -> bool:
        """A measurement at `now` on the loop's clock; True when it is to be logged: from
        LAG_WARN_S on, at most once every LAG_LOG_S."""
        minute = int(now // 60)
        if self._minutes and self._minutes[-1][0] == minute:
            self._minutes[-1] = (minute, max(self._minutes[-1][1], lag))
        else:
            self._minutes.append((minute, lag))
        oldest = int((now - WINDOW_S) // 60)
        while self._minutes and self._minutes[0][0] <= oldest:
            self._minutes.popleft()
        if lag < LAG_WARN_S or (self._warned is not None and now - self._warned < LAG_LOG_S):
            return False
        self._warned = now
        return True

    def worst(self) -> float | None:
        """The largest lag of the last hour; None before the first measurement."""
        return max((lag for _, lag in self._minutes), default=None)
