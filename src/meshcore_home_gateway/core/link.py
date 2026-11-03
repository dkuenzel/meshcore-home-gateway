"""Whether another client took the radio's TCP link (SEC-003, IMP-041).

The radio's TCP interface serves one client: a new connection replaces the one before, which the
radio closes. The radio's uptime (`uptime_s` of its statistics, milliseconds since boot divided
by 1000) tells a radio that closed the link while it kept running from one that restarted. A link
that the radio closed while its uptime went on was taken by another client (or, rarely, dropped by
the radio's network): the gateway takes it back with its reconnect, and the other client could
control the radio and read its private key meanwhile (docs/design.md, section 7.4). After 49.7
days the uptime wraps, and a takeover then reads as a restart.
"""

from __future__ import annotations

from dataclasses import dataclass

UPTIME_SLACK_S = 30  # the reads' timing and the radio's rounding to whole seconds
CONTESTED_HOLD_S = 3600.0  # a takeover counts this long, so that one alert covers a fight


@dataclass
class LinkWatch:
    """The last uptime of a link the radio closed, and the time of the last takeover."""

    taken_at: float | None = None  # the host's clock at the last takeover
    known: bool = False  # the current link reports an uptime: takeovers can be told
    _closed: tuple[int, float] | None = None

    def ended(self, uptime_s: int | None, read_at: float | None, *, by_radio: bool) -> None:
        """A link ended: the uptime the radio reported last and when, and whether the radio
        closed the link itself."""
        self.known = False
        self._closed = None
        if by_radio and uptime_s is not None and read_at is not None:
            self._closed = (uptime_s, read_at)

    def started(self, uptime_s: int | None, read_at: float | None) -> bool:
        """A new link's first statistics; True: the radio closed the link before and kept
        running, so another client took it."""
        closed, self._closed = self._closed, None
        self.known = uptime_s is not None
        if closed is None or uptime_s is None or read_at is None:
            return False
        last_uptime_s, last_read_at = closed
        if uptime_s < last_uptime_s or uptime_s + UPTIME_SLACK_S < last_uptime_s + (
            read_at - last_read_at
        ):
            return False  # the radio restarted (or its uptime wrapped)
        self.taken_at = read_at
        return True

    def contested(self, now: float) -> bool | None:
        """A takeover in the last CONTESTED_HOLD_S; None while the link reports no uptime."""
        if not self.known:
            return None
        taken = self.taken_at
        return taken is not None and 0 <= now - taken < CONTESTED_HOLD_S
