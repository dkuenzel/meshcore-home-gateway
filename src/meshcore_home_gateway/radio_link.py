"""The radio as the parts of the application see it (ARC-009): the session while it is connected,
what the radio said it is, and whether another client took the link. The radio supervisor
(bridge.py) keeps it; the outbox, the announcer, the status, the contact manager and the telemetry
poller read it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from meshcore_home_gateway.core.link import LinkWatch
from meshcore_home_gateway.ports import RadioInfo, RadioPort


class RadioLink:
    """The radio's session while it is connected (`live`), and what it said it is (`info`)."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._mono = monotonic
        self.session: RadioPort | None = None
        self.info: RadioInfo | None = None  # of the last connect; kept while the radio is away
        self.ready = asyncio.Event()  # set while a session is connected
        self.watch = LinkWatch()  # over TCP: whether another client took the link (SEC-003)

    def live(self) -> RadioPort | None:
        """The session, while the radio is connected."""
        session = self.session
        if session is None or session.closed or not self.ready.is_set():
            return None
        return session

    async def until(self, deadline: float) -> RadioPort | None:
        """The connected session, waiting for it until the deadline; never a closed one."""
        while True:
            remaining = deadline - self._mono()
            if remaining <= 0:
                return None
            try:
                # asyncio.timeout, not wait_for: on Python 3.11 wait_for can swallow a
                # cancellation that arrives as the awaited event fires, and shutdown then hangs
                async with asyncio.timeout(remaining):
                    await self.ready.wait()
            except TimeoutError:
                return None
            session = self.session
            if session is not None and not session.closed:
                return session
            await asyncio.sleep(0.05)  # the radio loop is replacing a closed session
