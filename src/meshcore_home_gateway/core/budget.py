"""Airtime budget for outgoing messages and deduplication of repeated messages (ADR 0008).

All of them take the current time as an argument, so tests control the clock. Times are seconds from
any monotonic origin.
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from collections.abc import Hashable

HOUR_S = 3600.0


class AirtimeBudget:
    """Token bucket over seconds of transmission time.

    The bucket holds at most `burst_s` seconds and refills at `per_hour_s` seconds per hour, so
    over any interval T the bridge transmits at most burst_s + per_hour_s * T / 3600 seconds.
    That holds per bucket: a new one starts full, and the bridge creates one at every start of
    the service, so each restart within T adds up to burst_s.
    """

    def __init__(self, per_hour_s: float, burst_s: float, now: float) -> None:
        if not 0.0 < per_hour_s <= HOUR_S:
            raise ValueError(f"airtime budget per hour must be in (0, 3600] s, got {per_hour_s}")
        if burst_s <= 0.0:
            raise ValueError(f"airtime budget burst must be positive, got {burst_s}")
        self._per_hour = per_hour_s
        self._rate = per_hour_s / HOUR_S
        self._capacity = burst_s
        self._tokens = burst_s
        self._last = now
        self._log: deque[tuple[float, float]] = deque()

    @property
    def capacity_s(self) -> float:
        return self._capacity

    @property
    def per_hour_s(self) -> float:
        return self._per_hour  # not _rate * HOUR_S, which reads 7.1 back as 7.099999999999999

    def available_s(self, now: float) -> float:
        self._refill(now)
        return self._tokens

    def wait_s(self, airtime_s: float, now: float) -> float:
        """Seconds until `airtime_s` can be spent; infinite if it exceeds the burst."""
        if airtime_s > self._capacity:
            return math.inf
        missing = airtime_s - self.available_s(now)
        return max(0.0, missing / self._rate)

    def consume(self, airtime_s: float, now: float) -> None:
        if airtime_s > self.available_s(now) + 1e-9:
            raise ValueError(f"{airtime_s:.3f} s requested, {self._tokens:.3f} s available")
        self._tokens = max(0.0, self._tokens - airtime_s)
        self._log.append((now, airtime_s))

    def used_s(self, now: float, window_s: float = HOUR_S) -> float:
        """Airtime spent in the last `window_s` seconds (for status reports)."""
        while self._log and self._log[0][0] <= now - window_s:
            self._log.popleft()
        return sum(airtime for _, airtime in self._log)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = max(self._last, now)


class Deduplicator:
    """Suppresses a repeated key within a window: one message per cause.

    A key is any hashable value; the bridge uses (alias, text) for send requests and the sender,
    its timestamp and the text for received messages, whose retries arrive as repeats. A
    duplicate does not extend the window, so a condition that keeps firing produces one message
    per window rather than none.
    """

    def __init__(self, window_s: float) -> None:
        if window_s < 0.0:
            raise ValueError(f"deduplication window must not be negative, got {window_s}")
        self._window = window_s
        self._accepted: OrderedDict[Hashable, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._accepted)

    def is_duplicate(self, key: Hashable, now: float) -> bool:
        """True if `key` was accepted less than the window ago; otherwise remember it."""
        if self._window == 0.0:
            return False
        self._prune(now)
        if key in self._accepted:
            return True
        self._accepted[key] = now
        return False

    def forget(self, key: Hashable) -> None:
        """Undo an acceptance whose send request was not taken after all (a full queue), so that
        its repeat gets through. Received messages are never forgotten: a copy of one, also of a
        rejected command, stays a duplicate (ADR 0009)."""
        self._accepted.pop(key, None)

    def rekey(self, old: Hashable, new: Hashable, now: float) -> None:
        """Replace an accepted key by the one it turned out to be (the final text)."""
        if self._accepted.pop(old, None) is not None and new not in self._accepted:
            self._accepted[new] = now

    def _prune(self, now: float) -> None:
        while self._accepted:
            oldest_key, accepted_at = next(iter(self._accepted.items()))
            if now - accepted_at < self._window:
                break
            del self._accepted[oldest_key]


_ROUNDING = 1e-9


class RateLimit:
    """Token bucket over events: at most `burst` at once, refilled at `per_s` per second."""

    def __init__(self, per_s: float, burst: float, now: float, tokens: float | None = None) -> None:
        """`tokens`: those at the start, if not a full burst."""
        if per_s <= 0.0 or burst < 1.0:
            raise ValueError(
                f"rate limit needs a positive rate and a burst of at least 1: {per_s}, {burst}"
            )
        self._rate = per_s
        self._capacity = burst
        self._tokens = burst if tokens is None else max(0.0, min(burst, tokens))
        self._last = now

    def allow(self, now: float) -> bool:
        """Take one event's token if there is one."""
        if not self.ready(now):
            return False
        self._tokens -= 1.0
        return True

    def ready(self, now: float) -> bool:
        """Whether there is a token, without taking it."""
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = max(self._last, now)
        return self._tokens >= 1.0 - _ROUNDING  # a refill to exactly 1 can come out just below

    def wait_s(self, now: float) -> float:
        """How long until there is a token; 0 if there is one."""
        if self.ready(now):
            return 0.0
        return (1.0 - self._tokens) / self._rate
