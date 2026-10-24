"""Telemetry from the radio's companions (ADR 0018): the task that asks them, one at a time.

The schedule (core/telemetry.py) decides whom to ask and when; this task asks the radio, waits
for the answer and accepts it only for the contact asked. Messages come first: no request while
the outbox has a message or had one in the last 10 seconds, and a request withdraws itself, with
nothing written, when a message is queued while it waits for the radio. The firmware keeps one
pending request; the gateway sends no other kind, so this task is its only user.

An answer (push 0x8B) names the 6-byte key prefix of the contact whose key decrypted it. It is
taken for the contact asked, or as the late answer of the last request until the next one, and
only while that prefix is unique among the radio's contacts and differs from the radio's own, on
a table that shows every change the radio reported. Answers to other requests (0x8C) are not
read. Nothing here raises out of `run` but a cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core import telemetry as core
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS
from meshcore_home_gateway.core.telemetry import Candidate, LaneFactory, Poll
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.protocol.packet import flooded_response_hashes
from meshcore_home_gateway.radio.session import (
    CommandFailedError,
    CompanionSession,
    NotSentError,
    RadioError,
    TelemetryReceived,
    TelemetrySent,
    added_types,
    type_names,
)
from meshcore_home_gateway.state import TelemetryRecords

log = logging.getLogger(__name__)

QUIET_S = 10.0  # no request until the outbox has been quiet this long
BUSY_PAUSE_S = 60.0  # after ERR 3: the radio had no packet buffer free
REFUSED_RETRY_S = 300.0  # after any other refusal
SAVE_INTERVAL_S = 300.0
MEASURED_TX_LIMIT_S = 180  # the radio's own time on air per hour (5 %) from which polls pause
HOLD_MAX_S = 15.0  # an answer waits this long for a table that shows every reported change
OUTDATED_WAIT_S = 1.0  # while the radio reported a change that the table as read may not show
HELD_ANSWERS = 4


class OutboxActivity:
    """Whether the outbox has work: messages queued or one being delivered (which includes
    waiting for its acknowledgement), and when a message last came or went."""

    def __init__(self, monotonic: Callable[[], float], queued: Callable[[], int]) -> None:
        self._mono = monotonic
        self._queued = queued
        self._delivering = False
        self._last = -math.inf
        self._changed = asyncio.Event()

    def touch(self) -> None:
        """A message was queued or received (an automation may answer it)."""
        self._last = self._mono()
        self._changed.set()

    def delivering(self, active: bool) -> None:
        self._delivering = active
        self.touch()

    @property
    def busy(self) -> bool:
        return self._delivering or self._queued() > 0

    async def wait_quiet(self, quiet_s: float, timeout_s: float) -> bool:
        """True once nothing is queued or delivered and nothing happened for `quiet_s`; False
        after `timeout_s` without that."""
        deadline = self._mono() + timeout_s
        while True:
            now = self._mono()
            if not self.busy and now - self._last >= quiet_s:
                return True
            if now >= deadline:
                return False
            self._changed.clear()
            limit = deadline - now
            if not self.busy:
                limit = min(limit, quiet_s - (now - self._last))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(limit):  # not wait_for: cancellation on 3.11
                    await self._changed.wait()


class AnswerWaiter:
    """The one request in flight. Frame numbers order an answer against the request's SENT: one
    read before it belongs to the previous request, whose tag the radio still held."""

    def __init__(
        self, monotonic: Callable[[], float], late: Callable[[str, TelemetryReceived], None]
    ) -> None:
        self._mono = monotonic
        self._late = late  # called with a late answer of the last request
        self._armed: str | None = None
        self._future: asyncio.Future[TelemetryReceived] | None = None
        self._early: list[TelemetryReceived] = []  # read after arm(), before SENT
        self._sent = False
        self._last: str | None = None  # the last request the radio sent, not yet answered
        self._late_until = -math.inf
        self._closed = False

    def arm(self, key: str) -> None:
        """Before the request is written: its answer can come before the poller sees SENT."""
        self._armed, self._early, self._sent = key, [], False
        self._future = asyncio.get_running_loop().create_future()

    def disarm(self) -> None:
        """No SENT: the radio still holds the previous request, its window stays."""
        early = self._early
        self._armed, self._future, self._early, self._sent = None, None, [], False
        for event in early:
            self._as_late(event)

    def sent(self, deadline: float, seq: int) -> None:
        """The radio sent the request; `seq`: the number of its SENT frame."""
        early, self._early, self._sent = self._early, [], True
        for event in early:
            if event.seq < seq:  # read before SENT: the previous request's
                self._as_late(event)
            elif self._matches(event):
                self._resolve(event)
            else:
                log.debug("telemetry answer from %s dropped: not the contact asked",
                          event.prefix.hex())  # fmt: skip
        answered = self._future is not None and self._future.done()
        self._last = None if answered else self._armed
        self._late_until = deadline + core.LATE_ACCEPT_S

    def resolve(self, event: TelemetryReceived) -> None:
        if self._closed:
            log.debug("telemetry answer after the stop dropped")
            return
        if self._armed is not None and not self._sent:
            self._early.append(event)  # ordered against SENT in sent() or disarm()
            return
        if self._matches(event):
            self._resolve(event)
            self._last = None  # the radio clears its slot on a match
            return
        self._as_late(event)

    async def wait(self, timeout_s: float) -> TelemetryReceived | None:
        future = self._future
        if future is None:
            return None
        try:
            async with asyncio.timeout(timeout_s):  # not wait_for: cancellation on 3.11
                return await future
        except TimeoutError:
            return None
        finally:
            self._future, self._armed, self._sent = None, None, False  # from now on: late

    def close(self) -> None:
        """The poller stops: nothing is taken any more."""
        self._closed, self._last, self._early = True, None, []

    def _matches(self, event: TelemetryReceived) -> bool:
        future, armed = self._future, self._armed
        return (
            future is not None and not future.done() and armed is not None
            and event.prefix.hex() == armed[:PREFIX_HEX_DIGITS]
        )  # fmt: skip

    def _resolve(self, event: TelemetryReceived) -> None:
        if self._future is not None:
            self._future.set_result(event)

    def _as_late(self, event: TelemetryReceived) -> None:
        last = self._last
        prefix = event.prefix.hex()
        if (
            last is not None
            and prefix == last[:PREFIX_HEX_DIGITS]
            and self._mono() <= self._late_until
        ):
            self._last = None
            self._late(last, event)
            return
        log.debug("telemetry answer from %s dropped: not the contact asked", prefix)


class TelemetryPoller:
    def __init__(
        self, observer: Observer, session: Callable[[], CompanionSession | None],
        outbox: OutboxActivity, *, records: TelemetryRecords | None, tcp: bool,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time, lanes: LaneFactory | None = None,
    ) -> None:  # fmt: skip
        """`session`: the session while the radio is connected. `tcp`: answers can be lost
        while the radio's push queue is full (a table read). No I/O here: the saved schedule is
        read when the task runs."""
        self._observer = observer
        self._session = session
        self._outbox = outbox
        self._records = records
        self._tcp = tcp
        self._mono = monotonic
        self._wall = wall_clock
        self._lanes = lanes
        self._scheduler = core.Scheduler(monotonic(), lanes=lanes)
        self._waiter = AnswerWaiter(monotonic, self._late)
        self._held: deque[tuple[str, TelemetryReceived]] = deque(maxlen=HELD_ANSWERS)
        self._wake = asyncio.Event()
        self._heard: dict[str, float] = {}  # key -> heard by other means (monotonic)
        # key -> the session and frame number of its newest answer taken
        self._newest: dict[str, tuple[CompanionSession, int]] = {}
        self._own_flood: tuple[str, float] | None = None  # (key, until): its 0x81 is ours
        self._reply_watch: tuple[str, float] | None = None  # (key, until): last direct request
        self._sending: str | None = None  # the request being written, before its SENT
        self._early_path = self._early_reply = False  # its path or flooded answer came first
        self._routes: dict[str, bool] = {}  # key -> its last request went by flood
        self._absent: dict[str, int] = {}  # key -> the table read on which the radio lacked it
        self._states: dict[str, str] = {}
        self._session_seen: CompanionSession | None = None
        self._unsupported: CompanionSession | None = None
        self._warned: tuple[CompanionSession, str] | None = None  # the pause last warned of
        self._status = "on"
        self._busy_until = -math.inf
        self._saved_version, self._saved_mono = -1, -math.inf

    # --- the task -------------------------------------------------------------------------------

    async def run(self) -> None:
        self._restore()
        try:
            while True:
                try:
                    await self._step()
                except asyncio.CancelledError:
                    raise
                except Exception:  # a bug stays here: messages go on
                    log.exception("telemetry: poll failed")
                    self._recover()
                    await self._sleep(core.STEP_S)
                self._save()
        finally:
            self._waiter.close()  # an answer during the stop changes nothing saved
            self._save(at_once=True)

    def _restore(self) -> None:
        try:
            saved, elapsed = self._records.load(self._wall()) if self._records else (None, 0.0)
            self._scheduler = core.Scheduler(
                self._mono(), saved=saved, elapsed_s=elapsed, lanes=self._lanes
            )
        except Exception:
            log.exception("telemetry: saved schedule unusable; starting empty")

    def _recover(self) -> None:
        try:
            self._sending = None
            self._waiter.disarm()
            if (key := self._scheduler.in_flight) is not None:
                self._scheduler.retry(key, self._mono())
        except Exception:
            log.exception("telemetry: recovery failed")

    def _save(self, *, at_once: bool = False) -> None:
        """Every 5 minutes after a change, at once after a flood (so a restart cannot repeat
        it) and at the stop. Never raises."""
        if self._records is None:
            return
        try:
            mono, scheduler = self._mono(), self._scheduler
            if not at_once and (
                scheduler.version == self._saved_version
                or (not scheduler.urgent and mono - self._saved_mono < SAVE_INTERVAL_S)
            ):
                return
            self._records.save(scheduler.export(mono), self._wall())
            self._saved_version, self._saved_mono = scheduler.version, mono
        except Exception:
            log.exception("telemetry: the schedule could not be saved")

    async def _sleep(self, seconds: float) -> None:
        """Sleep, woken early by a late answer, a contact heard or a path it sent."""
        if self._held:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(max(0.0, seconds)):
                await self._wake.wait()

    async def _step(self) -> None:
        session = self._session()
        if session is not self._session_seen:  # marks of the last session do not apply
            self._session_seen, self._absent = session, {}
        if self._held:  # late answers before anything new
            key, event = self._held.popleft()
            await self._accept(key, event, late=True)
            return
        if session is None or session.info is None:
            await self._sleep(core.STEP_S)
            return
        if (hold := self._hold(session)) is not None:
            await self._sleep(hold)
            return
        if session.contacts_outdated:  # the radio reported a change: plan on the next read
            await asyncio.sleep(OUTDATED_WAIT_S)
            return
        if not await self._outbox.wait_quiet(QUIET_S, timeout_s=core.STEP_S):
            return  # messages first
        now = self._mono()
        decision = self._scheduler.decide(session.info.lora, self._candidates(session, now), now)
        if isinstance(decision, float):
            await self._sleep(decision)
            return
        await self._poll(session, decision)

    def _not_sent(self) -> None:
        """The request never left: what arrived meanwhile was not its."""
        key, self._sending = self._sending, None
        self._waiter.disarm()
        if key is not None and self._early_path:
            self.heard(key)

    def _hold(self, session: CompanionSession) -> float | None:
        """How long to wait before deciding while telemetry is off for this session, paused,
        or the radio was busy; None: go on."""
        if session is self._unsupported:
            self._status = "unsupported"
            return core.STEP_S
        if (paused := self._paused(session)) is not None:
            self._status = "paused"
            kind, why = paused
            if self._warned != (session, kind):  # once per session and reason
                self._warned = (session, kind)
                log.warning("telemetry paused: %s", why)
            return core.STEP_S
        if self._warned is not None and self._warned[0] is session:
            log.info("telemetry resumed")
        self._warned = None
        self._status = "on"
        busy = self._busy_until - self._mono()
        return busy if busy > 0 else None

    async def _poll(self, session: CompanionSession, poll: Poll) -> None:
        sent = await self._send(session, poll)
        if sent is None:
            return
        now = self._mono()
        self._scheduler.sent(poll, flood=sent.flood, now=now)
        self._routes[poll.key] = sent.flood
        wait_s = core.answer_wait_s(sent.timeout_ms)
        until = now + wait_s + core.LATE_ACCEPT_S
        if sent.flood:
            self._own_flood, self._reply_watch = (poll.key, until), None
            if not poll.flood:
                log.warning("telemetry %s: the radio flooded a request planned direct",
                            poll.key[:12])  # fmt: skip
        else:
            self._reply_watch = (poll.key, until)
            if self._early_path:  # not the path of its own flood after all
                self.heard(poll.key)
            if self._early_reply:
                self._scheduler.flooded_reply(poll.key, now)
        self._save(at_once=self._scheduler.urgent)  # a flood is stored before the wait
        log.debug("telemetry %s: %s %s, wait %.1f s", poll.key[:12], poll.purpose,
                  "flooded" if sent.flood else "direct", wait_s)  # fmt: skip
        commands = session.commands_sent
        self._waiter.sent(deadline=now + wait_s, seq=sent.seq)
        answer = await self._waiter.wait(wait_s)
        if answer is not None:
            await self._accept(poll.key, answer, late=False)
        elif session.closed:  # cut by a disconnect: not the contact's silence
            self._scheduler.retry(poll.key, self._mono())
        else:
            self._silence(session, poll.key, commands)

    async def _send(self, session: CompanionSession, poll: Poll) -> TelemetrySent | None:
        """The request; None when the radio did not report it sent (all handled here)."""
        self._waiter.arm(poll.key)
        # pushes read before this task sees SENT: the route is known only then
        self._sending, self._early_path, self._early_reply = poll.key, False, False
        try:
            sent = await session.request_telemetry(
                bytes.fromhex(poll.key), unless=lambda: self._outbox.busy
            )
        except CommandFailedError as exc:
            self._not_sent()
            self._refused(session, poll, exc.reason)
            return None
        except NotSentError:  # nothing was written
            self._not_sent()
            self._scheduler.retry(poll.key, self._mono())
            return None
        except RadioError:  # written, but no SENT came: perhaps sent, so counted as planned
            self._not_sent()
            self._scheduler.sent(poll, flood=poll.flood, now=self._mono())
            self._scheduler.retry(poll.key, self._mono())
            self._save(at_once=self._scheduler.urgent)
            return None
        if sent is None:  # a message came while it waited for the radio: nothing written
            self._not_sent()
            self._scheduler.retry(poll.key, self._mono(), 0.0)
            return None
        self._sending = None
        return sent

    def _silence(self, session: CompanionSession, key: str, commands: int) -> None:
        """No answer within the wait. Over TCP a table read during it (also one still running)
        may have filled the radio's push queue: counted only the second time in a row."""
        reads = max(session.contacts_read_at, session.contacts_read_started_at)
        uncertain = self._tcp and reads > commands
        if uncertain:
            log.debug("telemetry %s: no answer during a table read: counted only if again",
                      key[:12])  # fmt: skip
        self._scheduler.unanswered(key, self._mono(), uncertain=uncertain)
        self._state_changed(key)

    async def _accept(self, key: str, event: TelemetryReceived, *, late: bool) -> None:
        newest = self._newest.get(key)
        if late and newest is not None and newest[0] is event.session and event.seq <= newest[1]:
            log.debug("telemetry %s: late answer older than one taken, dropped", key[:12])
            return
        session = await self._fresh_table()
        record = session.contact_records.get(key) if session is not None else None
        if session is None or record is None or not self._unique(session, key):
            why = "no current table" if session is None else "not a unique contact"
            log.warning("telemetry %s: answer not taken (%s)", key[:12], why)
            if not late:
                self._scheduler.retry(key, self._mono())  # nothing learned
            return
        payload = lpp.decode(event.data)
        found = lpp.position(payload)
        now = self._mono()
        self._scheduler.answered(
            key, now, found.cells if found else None, payload.lpp_bytes,
            route=(record.path_hops, record.path_bytes), late=late,
        )  # fmt: skip
        self._newest[key] = (event.session, event.seq)
        if self._own_flood is not None and self._own_flood[0] == key:
            self._own_flood = None  # its path came before its answer: later ones are the contact's
        log.debug("telemetry %s: answer of %d bytes, %d of them readings%s", key[:12],
                  len(event.data), payload.lpp_bytes, " (late)" if late else "")  # fmt: skip
        if payload.undecoded:
            log.debug("telemetry %s: %d bytes not decoded", key[:12], len(payload.undecoded))
        answer = core.Answer(
            payload, event.received_at, self._routes.get(key, False), record.path_hops, late,
            self._state_changed(key),
        )  # fmt: skip
        try:
            self._observer.telemetry(key, answer)
        except Exception:
            log.exception("observer failed")

    async def _fresh_table(self) -> CompanionSession | None:
        """The session once its table shows every change the radio reported; None after
        HOLD_MAX_S without one."""
        deadline = self._mono() + HOLD_MAX_S
        while True:
            session = self._session()
            if session is not None and session.info is not None and not session.contacts_outdated:
                return session
            if self._mono() >= deadline:
                return None
            await asyncio.sleep(0.25)

    def _unique(self, session: CompanionSession, key: str) -> bool:
        prefix = key[:PREFIX_HEX_DIGITS]
        same = sum(k[:PREFIX_HEX_DIGITS] == prefix for k in session.contact_records)
        info = session.info
        return same == 1 and info is not None and info.public_key[:PREFIX_HEX_DIGITS] != prefix

    def _candidates(self, session: CompanionSession, now: float) -> list[Candidate]:
        """Every companion of the radio's table whose prefix is its own (ADR 0018)."""
        records, info = session.contact_records, session.info
        prefixes = Counter(key[:PREFIX_HEX_DIGITS] for key in records)
        if info is not None:
            prefixes[info.public_key[:PREFIX_HEX_DIGITS]] += 1
        self._heard = {k: t for k, t in self._heard.items() if now - t <= 2 * core.HEARD_RECENTLY_S}
        chosen = []
        for key, record in records.items():
            if record.node_type != NodeType.CHAT or prefixes[key[:PREFIX_HEX_DIGITS]] > 1:
                continue  # companions only, never a look-alike
            if self._absent.get(key) == session.contacts_read_at:
                continue  # the radio lacked it on the table as read
            heard = self._heard.get(key)
            chosen.append(Candidate(key, record.path_hops, record.path_bytes, heard))
        return chosen

    def _refused(self, session: CompanionSession, poll: Poll, reason: str) -> None:
        now = self._mono()
        if reason == "unsupported_cmd":  # firmware without the command
            self._unsupported = session
            log.warning("telemetry: the radio does not take telemetry requests; off until it "
                        "reconnects")  # fmt: skip
            self._scheduler.retry(poll.key, now, core.STEP_S)
        elif reason == "not_found":  # the table as read is behind the radio's
            self._absent[poll.key] = session.contacts_read_at
            self._scheduler.retry(poll.key, now)
        elif reason == "table_full":  # no packet buffer free: the radio is busy
            self._busy_until = now + BUSY_PAUSE_S
            self._scheduler.retry(poll.key, now, BUSY_PAUSE_S)
        else:
            log.info("telemetry %s: refused (%s)", poll.key[:12], reason)
            self._scheduler.retry(poll.key, now, REFUSED_RETRY_S)

    def _paused(self, session: CompanionSession) -> tuple[str, str] | None:
        """Why telemetry waits, as a kind and a text; None: it does not."""
        info = session.info
        if info is not None and (types := added_types(info.self_info, session.autoadd)):
            # a node it adds could take a companion's prefix, and its answer with it
            names = ", ".join(type_names(types))
            return "adding", f"the radio adds {names} contacts automatically"
        measured = self._observer.radio_tx_1h_s()
        if measured is not None and measured >= MEASURED_TX_LIMIT_S:
            return "airtime", f"the radio transmitted {measured} s in the last hour"
        return None

    def _state_changed(self, key: str) -> str:
        state = self._scheduler.view(key, self._mono()) or "new"
        if self._states.get(key) != state:
            self._states[key] = state
            log.info("telemetry %s: %s", key[:12], state.replace("_", " "))
        return state

    # --- from the event loop, inline ------------------------------------------------------------

    def _late(self, key: str, event: TelemetryReceived) -> None:
        self._held.append((key, event))
        self._wake.set()

    def answer(self, event: TelemetryReceived) -> None:
        """A 0x8B; matched by prefix and frame order only (it needs no session)."""
        self._waiter.resolve(event)

    def heard(self, key: str) -> None:
        """The contact was heard by other means: an advert, a message as it arrived."""
        self._heard[key] = self._mono()
        self._wake.set()

    def path_changed(self, public_key: bytes) -> None:
        """A 0x81: heard by other means, unless it is the path of the gateway's own flood."""
        key, now = public_key.hex(), self._mono()
        if key == self._sending:  # decided once SENT shows whether it flooded
            self._early_path = True
            return
        own = self._own_flood
        if own is None or own[0] != key or now > own[1]:
            self.heard(key)

    def packet_logged(self, rx: codec.RxLog) -> None:
        """The packet log: a flooded answer to the last direct request is charged."""
        session = self._session()
        hashes = flooded_response_hashes(rx.raw)
        if hashes is None or session is None or session.info is None:
            return
        if hashes[0] != int(session.info.public_key[:2], 16):  # not to this radio
            return
        sending, watch = self._sending, self._reply_watch
        if sending is not None and hashes[1] == int(sending[:2], 16):
            self._early_reply = True  # charged once SENT shows the request went direct
            return
        if watch is not None and self._mono() <= watch[1] and hashes[1] == int(watch[0][:2], 16):
            self._scheduler.flooded_reply(watch[0], self._mono())

    # --- for the state document -----------------------------------------------------------------

    @property
    def status(self) -> str:
        """ "on", "paused" or "unsupported"."""
        return self._status

    def used_s(self) -> tuple[float, float]:
        """Own and estimated mesh airtime of the last hour."""
        return self._scheduler.used_s(self._mono())
