"""Advert requests (ADR 0033): make the radio send its advert once to the radios in direct range,
zero hop, never with a position.

A request on `advert/send` is checked at once, in this order, and refused with the first reason
that holds: its form (`too_large`, `invalid_utf8`, `invalid_request`), a left-over (`retained`,
`empty`), the radio (`no_radio`, `untested_firmware`, `advert_location`, `no_name`), and then
what passes (`busy`, `too_soon`, `messages_waiting`, `airtime_budget`, and `rate_limited`, the
radio work of the requests that pass). A request that repeats the id of the one in progress, or
of the advert that started the 10 minutes, is a copy (a QoS 1 resend) and is ignored. One worker
takes the requests that pass, one at a time: it reads the radio's clock afresh (`clock_unknown`,
`clock_ahead`), then writes SEND_SELF_ADVERT without its flood byte. Once the command holds the
radio, the claim checks the radio's firmware, its position policy, its name, the 10 minutes, the
messages and the budget again, and only then charges the advert's airtime and starts the 10
minutes: a message that came while the clock was read goes first, and nothing is counted for an
advert that was not written. A radio that answers with an error built no advert: its airtime is
given back and the 10 minutes do not start. The 10 minutes are kept in memory only.

Every request that is not a copy ends in one `advert` event: `sent` (the radio queued it),
`refused` (nothing was written; at a bounded rate) or `failed` (written and not confirmed: the
radio's error, a connection lost after the write, or a bug).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from meshcore_home_gateway import request_form
from meshcore_home_gateway.core import profile as policy
from meshcore_home_gateway.core.airtime import self_advert_bytes, time_on_air_s
from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.health import FAILURES
from meshcore_home_gateway.mqtt.payloads import encode
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Publisher
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.ports import (
    AdvertRequest,
    CommandFailedError,
    NotSentError,
    RadioError,
    RadioInfo,
    RadioPort,
)
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.request_form import RefusedError
from meshcore_home_gateway.views import self_advert_event

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out

INTERVAL_S = 600.0  # at most one advert written to the radio per this
# radio.session's CLOCK_AHEAD_WARNING_S: nodes keep a contact's newest advert time, and ignore
# the radio's later adverts while its clock is behind it (BaseChatMesh.cpp:133-136); with
# INTERVAL_S between two adverts, one stamped up to this far ahead never blocks the next
CLOCK_AHEAD_MAX_S = 60
REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST = 20.0, 100.0  # as the contact requests'
REFUSED_REPORT_INTERVAL_S = 60.0
# radio work: each request that passes its checks reads the radio's clock, also when the worker
# then refuses it (a clock ahead); as the contact requests'
WORK_PER_S, WORK_BURST = 1 / 60, 10.0


@dataclass(slots=True)
class _Claim:
    """What the claim at the radio's lock decided for one advert."""

    reason: str | None = None  # why it withdrew the advert
    name: str | None = None  # the name the advert carries, once claimed
    airtime_s: float | None = None
    # the last advert written before this one, and its request's id
    before: tuple[tuple[float, float] | None, str | None] = (None, None)


class Advertiser:
    def __init__(
        self,
        bus: Publisher,
        topics: Topics,
        *,
        radio: RadioLink,
        outbox: Delivery,
        monotonic: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        self._bus, self._topics = bus, topics
        self._radio, self._outbox = radio, outbox
        self._mono, self._wall = monotonic, wall_clock
        self._jobs: asyncio.Queue[tuple[str, RadioPort]] = asyncio.Queue(maxsize=1)
        self._active: str | None = None  # the id of the request between its checks and outcome
        self._last: tuple[float, float] | None = None  # the last advert written: monotonic, wall
        self._last_id: str | None = None  # the id of the request that wrote it
        self._refusals = RateLimit(REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, now=monotonic())
        self._work = RateLimit(WORK_PER_S, WORK_BURST, now=monotonic())
        self._unpublished = 0
        self._next_report = monotonic()

    # --- accept ---------------------------------------------------------------------------------

    def accept(self, request: AdvertRequest) -> None:
        """Ignore a copy, refuse, or hand the request to the worker, at once."""
        request_id = secrets.token_hex(4)
        try:
            given = _form(request.payload)
            request_id = given or request_id
            if given is not None and self._copy(given):
                log.info("advert request %s ignored: a copy of one taken", given)
                return
            session = self._checked(request)
        except RefusedError as exc:
            self._refuse(request_id, exc.reason)
            return
        except Exception:  # a bug: the request still ends, the next one is checked
            log.exception("advert request %s: checks failed", request_id)
            self._publish(self_advert_event("failed", request_id, reason="internal_error"))
            return
        self._active = request_id
        self._jobs.put_nowait((request_id, session))

    def _copy(self, request_id: str) -> bool:
        """Whether this is the id of the request in progress, or of the one whose advert went
        out, or may have, in the last INTERVAL_S. A request that ended with nothing sent leaves
        no id: the same request again is one of its own."""
        if request_id == self._active:
            return True
        return request_id == self._last_id and self._next_at() is not None

    def _checked(self, request: AdvertRequest) -> RadioPort:
        """The checks after the form, in their documented order; the session they found."""
        if request.retained:
            raise RefusedError("retained")
        if not request.payload:
            raise RefusedError("empty")
        session = self._radio.live()
        info = session.info if session is not None else None
        if session is None or info is None:
            raise RefusedError("no_radio")
        busy = "busy" if self._active is not None else None
        reason = self._radio_refusal(info) or busy or self._refusal(info)
        if reason is not None:
            raise RefusedError(reason)
        if not self._work.allow(self._mono()):  # last: only a request the radio would serve
            raise RefusedError("rate_limited")
        return session

    def _radio_refusal(self, info: RadioInfo) -> str | None:
        """Why this radio sends no advert: firmware the gateway is not tested with, a position
        in it (its policy as the radio reports it, also while the profile only observes), or no
        name, which every node drops."""
        if info.device.firmware_code not in policy.TESTED_FIRMWARE:
            return "untested_firmware"
        if info.self_info.advert_location != 0:
            return "advert_location"
        if not info.name:
            return "no_name"
        return None

    def _refusal(self, info: RadioInfo) -> str | None:
        """Why no advert may go now: the 10 minutes, a message on its way, the budget."""
        if self._last is not None and self._mono() - self._last[0] < INTERVAL_S:
            return "too_soon"
        if self._outbox.activity.busy:
            return "messages_waiting"
        if not self._outbox.airtime_free(_airtime_s(info), info.lora):
            return "airtime_budget"
        return None

    # --- the worker -----------------------------------------------------------------------------

    async def run(self) -> None:
        """One advert at a time."""
        while True:
            request_id, session = await self._jobs.get()
            claim = _Claim()
            try:
                await self._handle(request_id, session, claim)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug: the request still ends, the worker goes on
                log.exception("advert request %s failed", request_id)
                event = self_advert_event(
                    "failed",
                    request_id,
                    reason="internal_error",
                    name=claim.name,
                    airtime_s=claim.airtime_s,
                    next_at=self._next_at(),
                )
                self._publish(event)
            finally:
                self._active = None

    async def _handle(self, request_id: str, checked: RadioPort, claim: _Claim) -> None:
        try:
            await self._write(request_id, checked, claim)
        except RefusedError as exc:
            self._refuse(request_id, exc.reason)
            return
        except CommandFailedError as exc:  # the radio built no advert (MyMesh.cpp:1304-1316)
            self._unclaim(claim)
            self._failed(request_id, exc.reason, claim)
            return
        except RadioError:  # lost after the write: it may have gone out
            self._failed(request_id, "outcome_unknown", claim)
            return
        log.info(
            "advert request %s: sent with the name %r, %.2f s of airtime",
            request_id,
            claim.name,
            claim.airtime_s,
        )
        event = self_advert_event(
            "sent", request_id, name=claim.name, airtime_s=claim.airtime_s, next_at=self._next_at()
        )
        self._publish(event)

    async def _write(self, request_id: str, checked: RadioPort, claim: _Claim) -> None:
        """The clock read afresh, then the advert with its claim; RefusedError when nothing was
        written."""
        session = self._radio.live()
        if session is not checked or session.info is None:
            raise RefusedError("no_radio")
        try:
            offset = await session.read_clock_offset()  # never the heartbeat's: it can jump
        except CommandFailedError:  # an answer, but no time: never send with a clock unchecked
            raise RefusedError("clock_unknown") from None
        except RadioError:
            raise RefusedError("no_radio") from None
        if offset > CLOCK_AHEAD_MAX_S:
            raise RefusedError("clock_ahead")
        try:
            written = await session.send_self_advert(
                unless=lambda: self._claim(session, request_id, claim)
            )
        except NotSentError:  # closed before the claim: nothing counted
            raise RefusedError("no_radio") from None
        if not written:
            raise RefusedError(claim.reason or "internal_error")

    def _claim(self, session: RadioPort, request_id: str, claim: _Claim) -> bool:
        """At the radio's lock, before the write: True withdraws the advert (nothing counted,
        `claim.reason` says why); else its airtime is charged and the 10 minutes start, with
        this request's id as the one whose copies are ignored."""
        info = session.info
        if info is None:
            claim.reason = "no_radio"
            return True
        claim.reason = self._radio_refusal(info) or self._refusal(info)
        if claim.reason is not None:
            return True
        airtime = _airtime_s(info)
        self._outbox.spend_airtime(airtime)
        claim.before = (self._last, self._last_id)
        self._last, self._last_id = (self._mono(), self._wall()), request_id
        claim.name, claim.airtime_s = info.name, airtime
        return False

    def _unclaim(self, claim: _Claim) -> None:
        """The radio built no advert: its airtime back (the level stored at once), the 10
        minutes and the id as before the claim."""
        if claim.airtime_s is None:  # not claimed: nothing to undo
            return
        self._outbox.refund_airtime(claim.airtime_s)
        self._last, self._last_id = claim.before
        claim.airtime_s = None

    def _next_at(self) -> float | None:
        """When the next advert may go (wall clock), if one was written in the last 10 minutes."""
        last = self._last
        if last is None or self._mono() - last[0] >= INTERVAL_S:
            return None
        return last[1] + INTERVAL_S

    # --- events ---------------------------------------------------------------------------------

    def _failed(self, request_id: str, reason: str, claim: _Claim) -> None:
        log.warning("advert request %s failed: %s", request_id, reason)
        event = self_advert_event(
            "failed",
            request_id,
            reason=reason,
            name=claim.name,
            airtime_s=claim.airtime_s,
            next_at=self._next_at(),
        )
        self._publish(event)

    def _refuse(self, request_id: str, reason: str) -> None:
        """Nothing was written; at a bounded rate: a flood of requests must not flood the bus."""
        log.info("advert request %s refused: %s", request_id, reason)
        if not self._refusals.allow(self._mono()):
            self._unpublished += 1
            self.report_unpublished()
            return
        self._publish(
            self_advert_event("refused", request_id, reason=reason, next_at=self._next_at())
        )

    def _publish(self, event: dict[str, object]) -> None:
        self._bus.publish(self._topics.advert_event, encode(event))

    def report_unpublished(self) -> None:
        now = self._mono()
        if self._unpublished and now >= self._next_report:
            log.warning(
                "advert events of refused requests not published (limit %g per second, bursts "
                "of %g): %d",
                REFUSED_EVENTS_PER_S,
                REFUSED_EVENTS_BURST,
                self._unpublished,
            )
            self._unpublished = 0
            self._next_report = now + REFUSED_REPORT_INTERVAL_S


def _form(payload: bytes) -> str | None:
    """The request's id, if its JSON form gives one; RefusedError for a wrong form. Any other
    text is a request without an id."""
    text = request_form.request_text(payload)
    if not text.lstrip().startswith("{"):
        return None
    return request_form.request_id(request_form.json_object(text))


def _airtime_s(info: RadioInfo) -> float:
    """The advert's time on air at the radio's settings: its name as it is now, never a position
    (an advert that would carry one is never sent). A name that did not decode only
    overestimates."""
    return time_on_air_s(self_advert_bytes(len(info.name.encode("utf-8"))), info.lora)
