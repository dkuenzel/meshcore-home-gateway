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

The contacts the household excluded (the observer holds them) are never asked: a request that
waits for the radio is withdrawn, an answer that comes anyway is dropped. Each companion's state
(ADR 0024) is evaluated at every step and after every outcome, and the observer is told of a
change. An event when a companion that answered within the day leaves two requests in a row
unanswered, and when it answers again or for the first time. The first time telemetry runs on a
state directory it asks nobody for FIRST_START_S after the radio's table is read, so the
household can exclude strangers first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable, Collection
from dataclasses import replace

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core import telemetry as core
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS
from meshcore_home_gateway.core.telemetry import (
    Candidate,
    LaneFactory,
    PauseReason,
    Poll,
    TelemetryEvent,
    TelemetryState,
    TelemetryStatus,
)
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.protocol.packet import flooded_response_hashes
from meshcore_home_gateway.radio.session import (
    CommandFailedError,
    CompanionSession,
    RadioError,
    TelemetryReceived,
    TelemetrySent,
    added_types,
    type_names,
)
from meshcore_home_gateway.state import EXCLUSIONS_FILE, TelemetryRecords

log = logging.getLogger(__name__)

QUIET_S = 10.0  # no request until the outbox has been quiet this long
BUSY_PAUSE_S = 60.0  # after ERR 3: the radio had no packet buffer free
REFUSED_RETRY_S = 300.0  # after any other refusal
SAVE_INTERVAL_S = 300.0
MEASURED_TX_LIMIT_S = 180  # the radio's own time on air per hour (5 %) from which polls pause
HOLD_MAX_S = 15.0  # an answer waits this long for a table that shows every reported change
TAKEN_MAX_S = 600.0  # an answer counts as at most this old when taken (the wall clock may step)
OUTDATED_WAIT_S = 1.0  # while the radio reported a change that the table as read may not show
HELD_ANSWERS = 4
LOST_SENT_WINDOW_S = 120.0  # a request written whose SENT never came: its answer counts this long
SEED_HEARD = frozenset({"advert"})  # what the history's "last heard" may seed (not a message:
#   one fetched at connect may have waited in the radio for hours)
HEARD_KEPT = 512  # heard times kept before old ones are dropped
# the first time telemetry runs on a state directory, nobody is asked this long after the radio's
# table is read (read when used, so tests can change it)
FIRST_START_S = 3600.0
STATE_ERROR_LOG_S = 60.0  # an error while the states are evaluated is logged at most this often


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

    def assume_sent(self, key: str, until: float) -> None:
        """A request written whose SENT never came: the radio may hold its tag, so its answer
        is taken as late until `until`."""
        self._last, self._late_until = key, until

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
        read by restore()."""
        self._observer = observer
        self._exclusions = observer.exclusions  # the one holder: the observer's
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
        # key -> the session's serial and the frame number of its newest answer taken
        self._newest: dict[str, tuple[int, int]] = {}
        self._seeded: set[str] = set()  # keys whose heard time the history was asked for
        self._own_flood: tuple[str, float] | None = None  # (key, until): its 0x81 is ours
        self._reply_watch: tuple[str, float] | None = None  # (key, until): last direct request
        self._sending: str | None = None  # the request being written, before its SENT
        self._early_path = self._early_reply = False  # its path or flooded answer came first
        self._routes: dict[str, bool] = {}  # key -> its last request went by flood
        self._absent: dict[str, int] = {}  # key -> the table read on which the radio lacked it
        self._states: dict[str, str] = {}
        self._reported: dict[str, TelemetryState] = {}  # key -> the state the observer was told
        self._own_key: str | None = None  # the radio's, from the last session with its info
        self._session_seen: CompanionSession | None = None
        self._unsupported: CompanionSession | None = None
        self._warned: tuple[CompanionSession, str] | None = None  # the pause last warned of
        self._status: TelemetryStatus = "paused"
        self._reason: PauseReason | None = "no_radio"
        self.on_status: Callable[[], None] | None = None  # what the status reads changed
        self._restored = False
        self._hold_left: float | None = None  # the first-start hold still to come; None: none
        self._hold_until: float | None = None  # when it ends, once it runs
        self._state_error_at = -math.inf
        self._busy_until = -math.inf
        self._saved_version, self._saved_mono = -1, -math.inf

    # --- the task -------------------------------------------------------------------------------

    async def run(self) -> None:
        self.restore()
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

    def restore(self) -> None:
        """The saved schedule, before any task runs (the bridge); run() calls it if it has not
        run. The schedule of every excluded contact forgets its answers (excluded while
        telemetry was off). The first-start hold: all of it when the state file has no
        telemetry record, else what the record has left of it. The observer learns when each
        contact last answered, and last answered with a position. Never raises."""
        if self._restored:
            return
        self._restored = True
        saved: core.Saved | None = None
        elapsed: float | None = 0.0
        try:
            saved, elapsed = self._records.load(self._wall()) if self._records else (None, 0.0)
            self._scheduler = core.Scheduler(
                self._mono(), saved=saved, elapsed_s=elapsed, lanes=self._lanes
            )
        except Exception:
            log.exception("telemetry: saved schedule unusable; starting empty")
        try:
            self._hold_left = self._first_hold(saved, elapsed)
            now, wall = self._mono(), self._wall()
            for key in self._exclusions.keys:
                self._scheduler.forget(key, now)
            for key, age in self._scheduler.located_ages(now).items():
                self._observer.restore_position_received(key, wall - age)
            for key, age in self._scheduler.answered_ages(now).items():
                self._observer.restore_answered(key, wall - age)
        except Exception:
            log.exception("telemetry: the saved schedule could not be restored")

    def _first_hold(self, saved: core.Saved | None, elapsed: float | None) -> float | None:
        """What is left of the first-start hold: all of it where telemetry never ran (no
        record; no state file either), the record's rest less the time since it was saved
        (all of the rest when that time is unknown, or when the hold had not started: the
        radio's table was never read), none for a record this version cannot read."""
        if saved is not None:
            if saved.hold_left_s is None:
                return None
            left = saved.hold_left_s - (0.0 if saved.hold_pending else elapsed or 0.0)
        elif self._records is not None and self._records.ran_before():
            return None
        else:
            left = FIRST_START_S
        return left if left > 0 else None

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
            pending = self._hold_left is not None and self._hold_until is None
            saved = replace(
                scheduler.export(mono), hold_left_s=self._hold_rest(mono), hold_pending=pending
            )
            self._records.save(saved, self._wall())
            self._saved_version, self._saved_mono = scheduler.version, mono
        except Exception:
            log.exception("telemetry: the schedule could not be saved")

    def _hold_rest(self, now: float) -> float | None:
        """For the state file: all of a hold that has not started, the rest of one that runs,
        None once it is over."""
        if self._hold_left is None or self._hold_until is None:
            return self._hold_left
        left = self._hold_until - now
        return left if left > 0 else None

    async def _sleep(self, seconds: float) -> None:
        """Sleep, woken early by a late answer, a contact heard or a path it sent."""
        if self._held:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(max(0.0, seconds)):
                await self._wake.wait()

    async def _step(self) -> None:
        self._report_states()
        session = self._session()
        if session is not self._session_seen:  # marks of the last session do not apply
            self._session_seen, self._absent = session, {}
        if self._held:  # late answers before anything new
            key, event = self._held.popleft()
            await self._accept(key, event, late=True)
            return
        if session is None or session.info is None:  # also why nothing is sent
            self._set_status("paused", "no_radio")
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
        or the radio was busy; None: go on. The first reason that holds counts: the firmware
        lacks the command, the exclusions cannot be read, the first-start hold, the radio adds
        contacts by itself or transmitted too much."""
        if session is self._unsupported:
            self._set_status("unsupported", None)
            return core.STEP_S
        if self._exclusions.unreadable:  # asking nobody rather than everybody
            why = f"the exclusions in {EXCLUSIONS_FILE} cannot be read: fix or delete the file"
            self._pause(session, "exclusions_unreadable", f"{why} and restart the service",
                        logging.ERROR)  # fmt: skip
            return core.STEP_S
        if (left := self._first_start(session)) is not None:
            self._set_status("paused", "first_start")
            return left
        if (paused := self._paused(session)) is not None:
            self._pause(session, *paused)
            return core.STEP_S
        if self._warned is not None and self._warned[0] is session:
            log.info("telemetry resumed")
        self._warned = None
        self._set_status("on", None)
        busy = self._busy_until - self._mono()
        return busy if busy > 0 else None

    def _set_status(self, status: TelemetryStatus, reason: PauseReason | None) -> None:
        """Tell the listener (the bridge publishes the state) when what the status reads
        changes; never raises."""
        before = self.status, self.reason
        self._status, self._reason = status, reason
        if (self.status, self.reason) != before and self.on_status is not None:
            try:
                self.on_status()
            except Exception:
                log.exception("telemetry: the status could not be told")

    def _pause(
        self, session: CompanionSession, reason: PauseReason, why: str,
        level: int = logging.WARNING,
    ) -> None:  # fmt: skip
        self._set_status("paused", reason)
        if self._warned != (session, reason):  # once per session and reason
            self._warned = (session, reason)
            log.log(level, "telemetry paused: %s", why)

    def _first_start(self, session: CompanionSession) -> float | None:
        """How long the first-start hold still holds the requests; None: none, or it is over.
        It starts at the first step with a read table: one warning names how many companions
        it will ask, and the schedule is saved at once, so a restart holds only the rest."""
        if self._hold_left is None:
            return None
        now = self._mono()
        if self._hold_until is None:
            if session.contacts_read_at < 0:  # whom it will ask is known with the table
                return OUTDATED_WAIT_S
            self._hold_until = now + self._hold_left
            log.warning(
                "telemetry paused: the first requests go out in %d minutes, to %d companions of "
                "the radio; exclude every one that is not the household's (the Telemetry switch "
                "on its device, or meshcore-home-gateway contacts telemetry KEY off)",
                math.ceil(self._hold_left / 60), self._askable(session),
            )  # fmt: skip
            self._save(at_once=True)
        left = self._hold_until - now
        if left <= 0:
            self._hold_left = self._hold_until = None
            return None
        self._warned = (session, "first_start")  # "resumed" once it is over
        return min(left, core.STEP_S)

    def _askable(self, session: CompanionSession) -> int:
        """The companions of the table as read that would be asked: unique, not excluded."""
        prefixes = _prefixes(session)
        return sum(
            record.node_type == NodeType.CHAT and prefixes[key[:PREFIX_HEX_DIGITS]] == 1
            and key not in self._exclusions
            for key, record in session.contact_records.items()
        )  # fmt: skip

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
        before: list[int] = []

        def busy() -> bool:  # runs with the lock, right before the write
            before.append(session.commands_sent)
            return self._outbox.busy or poll.key in self._exclusions  # excluded meanwhile

        def written() -> bool:
            return bool(before) and session.commands_sent > before[0]

        try:
            sent = await session.request_telemetry(bytes.fromhex(poll.key), unless=busy)
        except CommandFailedError as exc:
            self._not_sent()
            self._refused(session, poll, exc.reason)
            return None
        except asyncio.CancelledError:  # the stop: a written request is in the last save
            if written():
                self._scheduler.sent(poll, flood=poll.flood, now=self._mono())
            raise
        except RadioError:  # no SENT: not written, or written and the link broke
            self._not_sent()
            if written():  # perhaps sent: counted as planned, its answer taken late
                now = self._mono()
                self._scheduler.sent(poll, flood=poll.flood, now=now)
                until = now + LOST_SENT_WINDOW_S
                self._waiter.assume_sent(poll.key, until)
                if poll.flood:
                    self._own_flood = (poll.key, until)
            self._scheduler.retry(poll.key, self._mono())
            self._save(at_once=self._scheduler.urgent)
            return None
        if sent is None:  # a message came, or an exclusion, while it waited: nothing written
            self._not_sent()
            self._scheduler.retry(poll.key, self._mono(), 0.0)
            return None
        self._sending = None
        return sent

    def _silence(self, session: CompanionSession, key: str, commands: int) -> None:
        """No answer within the wait. Over TCP a table read during it (also one still running)
        may have filled the radio's push queue: counted only the second time in a row. The
        second request in a row left unanswered by a companion that answered within the day is
        an event, once per absence (the count goes back only with an answer)."""
        now = self._mono()
        if key in self._exclusions:  # excluded meanwhile: nothing learned
            self._scheduler.retry(key, now)
            return
        reads = max(session.contacts_read_at, session.contacts_read_started_at)
        uncertain = self._tcp and reads > commands
        if uncertain:
            log.debug("telemetry %s: no answer during a table read: counted only if again",
                      key[:12])  # fmt: skip
        before = self._scheduler.failures(key)
        after = self._scheduler.unanswered(key, now, uncertain=uncertain)
        self._state_changed(key)
        self._report_states([key])
        if before < 2 <= after and self._scheduler.answered_within_day(key, now):
            self._event("unanswered", key)

    async def _accept(self, key: str, event: TelemetryReceived, *, late: bool) -> None:
        newest = self._newest.get(key)
        same_session = newest is not None and newest[0] == event.session_serial
        if late and newest is not None and same_session and event.seq <= newest[1]:
            log.debug("telemetry %s: late answer older than one taken, dropped", key[:12])
            return
        session = await self._fresh_table()
        if key in self._exclusions:  # excluded meanwhile, also during the wait for the table
            log.debug("telemetry %s: excluded, its answer dropped", key[:12])
            if not late:
                self._scheduler.retry(key, self._mono())  # nothing learned
            return
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
        back = self._scheduler.answered(
            key, now, found.cells if found else None, payload.lpp_bytes,
            route=(record.path_hops, record.path_bytes), late=late,
            at=now - min(self._wall() - event.received_at, TAKEN_MAX_S),  # when it came
        )  # fmt: skip
        self._newest[key] = (event.session_serial, event.seq)
        if self._own_flood is not None and self._own_flood[0] == key:
            self._own_flood = None  # its path came before its answer: later ones are the contact's
        log.debug("telemetry %s: answer of %d bytes, %d of them readings%s", key[:12],
                  len(event.data), payload.lpp_bytes, " (late)" if late else "")  # fmt: skip
        if payload.undecoded:
            log.debug("telemetry %s: %d bytes not decoded", key[:12], len(payload.undecoded))
        self._state_changed(key)
        answer = core.Answer(
            payload, event.received_at, self._routes.get(key, False), record.path_hops, late
        )
        try:
            self._observer.telemetry(key, answer)
        except Exception:
            log.exception("observer failed")
        self._report_states([key])
        if back:  # its first answer, or the first after two unanswered requests in a row
            self._event("answered", key)

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
        """Every companion of the radio's table whose prefix is its own (ADR 0018), unless it
        was excluded. The prefixes count every contact: an excluded one still blocks its twin."""
        records, prefixes = session.contact_records, _prefixes(session)
        self._prune_heard(now)
        for kept in (self._newest, self._routes, self._states):  # contacts that left
            for key in [k for k in kept if k not in records]:
                del kept[key]
        chosen = []
        for key, record in records.items():
            if record.node_type != NodeType.CHAT or prefixes[key[:PREFIX_HEX_DIGITS]] > 1:
                continue  # companions only, never a look-alike
            if key in self._exclusions:
                continue
            if self._absent.get(key) == session.contacts_read_at:
                continue  # the radio lacked it on the table as read
            if key not in self._seeded:
                self._seed(key, now)
            heard = self._heard.get(key)
            chosen.append(Candidate(key, record.path_hops, record.path_bytes, heard))
        return chosen

    def _seed(self, key: str, now: float) -> None:
        """An advert from before this run (the history keeps when a contact was last heard)
        counts as heard, so a companion added before a restart is flooded while it is fresh."""
        self._seeded.add(key)
        tracked = self._observer.mesh.contacts.get(key)
        heard = tracked.restored_heard if tracked is not None else None  # not what this run heard
        if heard is None or heard.how not in SEED_HEARD:
            return
        age = self._wall() - heard.at
        if 0 <= age <= core.HEARD_RECENTLY_S:  # a clock set back: unknown
            self._heard[key] = max(self._heard.get(key, -math.inf), now - age)

    def _prune_heard(self, now: float) -> None:
        self._heard = {k: t for k, t in self._heard.items() if now - t <= 2 * core.HEARD_RECENTLY_S}

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

    def _paused(self, session: CompanionSession) -> tuple[PauseReason, str] | None:
        """Why the radio lets telemetry wait, as a reason and a text; None: it does not."""
        info = session.info
        if info is not None and (types := added_types(info.self_info, session.autoadd)):
            # a node it adds could take a companion's prefix, and its answer with it
            names = ", ".join(type_names(types))
            return "auto_add", f"the radio adds {names} contacts automatically"
        measured = self._observer.radio_tx_1h_s()
        if measured is not None and measured >= MEASURED_TX_LIMIT_S:
            return "radio_airtime", f"the radio transmitted {measured} s in the last hour"
        return None

    def _state_changed(self, key: str) -> str:
        state = self._scheduler.view(key, self._mono()) or "new"
        if self._states.get(key) != state:
            self._states[key] = state
            log.info("telemetry %s: %s", key[:12], state.replace("_", " "))
        return state

    # --- the companions' states (ADR 0024) ------------------------------------------------------

    def _report_states(self, keys: Collection[str] | None = None) -> None:
        """_refresh_states(); an error is logged at most every STATE_ERROR_LOG_S, the step goes
        on."""
        try:
            self._refresh_states(keys)
        except Exception:
            now = self._mono()
            if now - self._state_error_at >= STATE_ERROR_LOG_S:
                self._state_error_at = now
                log.exception("telemetry: the states could not be evaluated")

    def _refresh_states(self, keys: Collection[str] | None = None) -> None:
        """The state of each companion of the table as last read (or of `keys`) now; the
        observer republishes the document of one whose state changed. A contact's first state
        is told too: its document may have been built with another (a contact the observer got
        during a request, answers that expired before the first step); the observer publishes
        only a changed document."""
        contacts = self._observer.mesh.contacts
        for key in [k for k in self._reported if k not in contacts]:  # contacts that left
            del self._reported[key]
        prefixes, now = self._mesh_prefixes(), self._mono()
        for key in contacts if keys is None else keys:
            tracked = contacts.get(key)
            if tracked is None or tracked.node.node_type != NodeType.CHAT:
                continue
            state = self._state(key, prefixes, now) or "waiting"
            before = self._reported.get(key)
            self._reported[key] = state
            if before != state:
                self._observer.telemetry_state_changed(key)

    def _state(self, key: str, prefixes: Counter[str], now: float) -> TelemetryState | None:
        if key in self._exclusions:
            return "excluded"
        if prefixes[key[:PREFIX_HEX_DIGITS]] > 1:  # an answer could not be told apart
            return "look_alike"
        return self._scheduler.state(key, now)

    def _mesh_prefixes(self) -> Counter[str]:
        """How many contacts of the table as last read, and the radio itself, have each key
        prefix: counted at each call, so a document built before any decision is right too."""
        session = self._session()
        if session is not None and session.info is not None:
            self._own_key = session.info.public_key  # remembered while the radio is away
        prefixes = Counter(key[:PREFIX_HEX_DIGITS] for key in self._observer.mesh.contacts)
        if self._own_key is not None:
            prefixes[self._own_key[:PREFIX_HEX_DIGITS]] += 1
        return prefixes

    def _event(self, kind: TelemetryEvent, key: str) -> None:
        """An event with the state just reported; none while the contact reads as a look-alike
        (a table newer than the one the outcome was taken on has its twin): no event carries
        that state."""
        state = self._reported.get(key)
        if state is None or state == "look_alike":
            return
        try:
            self._observer.telemetry_event(kind, key, state)
        except Exception:
            log.exception("observer failed")

    # --- from the event loop, inline ------------------------------------------------------------

    def exclusion_changed(self, key: str, excluded: bool) -> None:
        """Observer listener: the contact was excluded or included again. Either way its
        schedule forgets its answers, so an included contact is `waiting` and due at once, also
        after a retry while it was excluded. Its state is taken as reported (the observer
        publishes its document next); the task wakes."""
        self._scheduler.forget(key, self._mono())
        self._reported[key] = self.state_of(key) or "waiting"
        self._wake.set()

    def contact_added(self, key: str) -> None:
        """Observer listener: a table read added the contact, whose values the mesh no longer
        has. Its schedule forgets its answers as at an inclusion (one removed and added again
        within a day would get them back), so its next answer is its first; its state is
        evaluated anew (the observer publishes its document next)."""
        self._scheduler.forget(key, self._mono())
        self._reported.pop(key, None)

    def radio_connected(self) -> None:
        """The bridge has a session: the step that finds it comes now, not a STEP_S later."""
        self._wake.set()

    def _late(self, key: str, event: TelemetryReceived) -> None:
        self._held.append((key, event))
        self._wake.set()

    def answer(self, event: TelemetryReceived) -> None:
        """A 0x8B; matched by prefix and frame order only (it needs no session)."""
        self._waiter.resolve(event)

    def heard(self, key: str) -> None:
        """The contact was heard by other means: an advert, a message as it arrived."""
        now = self._mono()
        self._heard[key] = now
        if len(self._heard) > HEARD_KEPT:  # a busy mesh while nothing decides (paused)
            self._prune_heard(now)
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

    # --- for the documents ----------------------------------------------------------------------

    def state_of(self, key: str) -> TelemetryState | None:
        """excluded, look_alike, or the schedule's state now; None if the schedule does not
        know the contact."""
        return self._state(key, self._mesh_prefixes(), self._mono())

    @property
    def status(self) -> TelemetryStatus:
        """ "on", "paused" or "unsupported"; "paused" as soon as the radio is away."""
        return "paused" if self._radio_away() else self._status

    @property
    def reason(self) -> PauseReason | None:
        """Why telemetry is paused; None while it is not."""
        return "no_radio" if self._radio_away() else self._reason

    def _radio_away(self) -> bool:
        session = self._session()
        return session is None or session.info is None

    def used_s(self) -> tuple[float, float]:
        """Own and estimated mesh airtime of the last hour."""
        return self._scheduler.used_s(self._mono())


def _prefixes(session: CompanionSession) -> Counter[str]:
    """How many contacts of the session's table, and the radio itself, have each key prefix."""
    prefixes = Counter(key[:PREFIX_HEX_DIGITS] for key in session.contact_records)
    if session.info is not None:
        prefixes[session.info.public_key[:PREFIX_HEX_DIGITS]] += 1
    return prefixes
