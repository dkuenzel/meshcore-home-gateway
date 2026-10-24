"""The telemetry schedule (ADR 0018): whom to ask next, by which route, and when.

A pure state machine over monotonic seconds; the poller (telemetry_poller.py) feeds it the
candidates, what the radio answered and what was heard, and does what it decides.

* One request in flight for the whole gateway: the firmware keeps one pending request.
* Direct only: a contact with a stored path is asked on it. One without a path is asked by flood
  only when it was heard by other means (an advert, a live message, a path it sent) within
  30 minutes, at most 4 floods an hour for the gateway and one per contact per 30 minutes
  (doubling while its floods go unanswered); a contact that has not answered within a day, once
  a day. The gateway never resets or writes a path for telemetry.
* Two lanes of airtime, separate from the message budget: the gateway's own transmit time and the
  estimated airtime of every transmission a request causes in the mesh. They start empty, so a
  restart adds no burst; a request is charged when the radio reports it sent.
* Intervals from a share of channel time per contact: 1 % while it moves, 0.1 % at rest, once an
  hour without a position; a contact that stops answering is asked less often (back-off), on its
  stored path at least every 30 minutes while it answered within the day.

Constants are read when used, so tests can change them. Positions never leave this module:
the movement reference is kept in memory only.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from meshcore_home_gateway.core import airtime
from meshcore_home_gateway.core.airtime import Costs, LoRaParams
from meshcore_home_gateway.core.budget import AirtimeBudget
from meshcore_home_gateway.core.lpp import LppPayload, centre
from meshcore_home_gateway.core.model import distance_m

# ADR 0018
OWN_S_PER_HOUR = 18.0  # the gateway's own transmit time (0.5 %)
MESH_S_PER_HOUR = 90.0  # estimated airtime in the mesh, every hop both ways (2.5 %)
MOVING_SHARE, RESTING_SHARE = 0.01, 0.001  # of channel time per contact
UNLOCATED_S = 3600.0  # answers without a position: once an hour
MAX_DELAY_S = 6 * 3600.0  # back-off cap of a contact that has not answered within a day
MOVED_M, STILL_POLLS = 50.0, 2  # moving from 50 m on; resting after two answers under 50 m
HEARD_RECENTLY_S = 1800.0  # a contact without a path is flooded only if heard this recently
FLOODS_PER_HOUR, FLOOD_GAP_S = 4, 1800.0
WAIT_MIN_S, WAIT_FACTOR = 5.0, 1.25  # answer wait: max(5 s, 1.25 x the radio's estimate)
# this design
MOVING_MIN_S, RESTING_MIN_S = 90.0, 900.0  # floors for fast presets
PROBE_S = 3600.0  # base interval of a contact that has not answered within a day
ANSWERED_RECENTLY_S = 86_400.0
AWAY_S = 1800.0  # a contact that answered within a day is asked at least this often
PROBE_GAP_S = 86_400.0  # floods to a contact that has not answered within a day
MAX_FLOOD_GAP_S = 86_400.0
FLOOD_MAX_WAIT_S = 600.0  # a due flood waits at most this long behind direct polls
PARK_S = 86_400.0  # the schedule of a contact that left the candidates is kept this long
RETRY_S = 30.0
POLL_GAP_S = 10.0  # after every outcome of a request the radio sent
LATE_ACCEPT_S = 60.0  # an answer after its wait counts this long, until the next request
DEFAULT_LPP_BYTES = 19  # voltage, position, MCU temperature
MAX_LPP_BYTES = 156  # the longest answer the USB link carries
MAX_DUE_S = 7 * 86_400.0  # due times restored from the state file
STEP_S = 60.0  # longest sleep between two decisions
MAX_FAILURES = 16
HOUR_S = 3600.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A contact the poller may ask, as the radio's table shows it."""

    key: str
    hops: int | None  # None: no stored path, the radio would flood
    path_bytes: int | None
    heard_at: float | None  # heard by other means (advert, live message, a path it sent)


@dataclass(frozen=True, slots=True)
class Poll:
    key: str
    flood: bool  # the route planned
    costs: Costs
    purpose: str  # "poll", "retry" (after a failure, answered within a day), "flood"


@dataclass(frozen=True, slots=True)
class Answer:
    """An answer the poller matched to the contact it asked."""

    payload: LppPayload
    received_at: float  # wall clock of the push
    flood: bool  # the request went by flood
    hops: int | None  # the stored path in the table the answer was accepted on
    late: bool  # after its wait, before the next request
    state: str  # Scheduler.view() after the answer


@dataclass(frozen=True, slots=True)
class SavedContact:
    """One contact's schedule in the state file: times relative to the save, no position."""

    due_in_s: float
    failures: int = 0
    answered_age_s: float | None = None
    polled_age_s: float | None = None
    flood_age_s: float | None = None
    floods: int = 0  # floods since its last direct answer
    located: bool = False  # the last answer carried a position
    still: int = STILL_POLLS  # answers in a row under 50 m: below STILL_POLLS it moves
    lpp_bytes: int = DEFAULT_LPP_BYTES


@dataclass(frozen=True, slots=True)
class Saved:
    contacts: Mapping[str, SavedContact] = field(default_factory=dict)
    floods_age_s: tuple[float, ...] = ()  # the gateway's floods within the last hour


LaneFactory = Callable[[Costs, float], tuple[AirtimeBudget, AirtimeBudget]]


def build_lanes(capacity: Costs, now: float) -> tuple[AirtimeBudget, AirtimeBudget]:
    """The two lanes, empty; each holds the costliest planned request, a flood."""
    return (
        AirtimeBudget(OWN_S_PER_HOUR, capacity.own_s, now, tokens=0.0),
        AirtimeBudget(MESH_S_PER_HOUR, capacity.mesh_s, now, tokens=0.0),
    )


def answer_wait_s(timeout_ms: int) -> float:
    return max(WAIT_MIN_S, WAIT_FACTOR * timeout_ms / 1000)


@dataclass(slots=True)
class _Contact:
    due_at: float
    failures: int = 0  # unanswered requests in a row
    answered_at: float | None = None
    polled_at: float | None = None  # the newest request the radio sent
    flood_at: float | None = None
    floods: int = 0
    located: bool = False
    still: int = STILL_POLLS
    anchor: tuple[int, int] | None = None  # movement reference in cells; memory only
    lpp_bytes: int = DEFAULT_LPP_BYTES
    uncertain: bool = False  # the last outcome was an uncertain timeout (TCP)
    last_flood: bool = False  # the last request went by flood
    hops: int | None = None
    path_bytes: int | None = None
    heard_at: float | None = None
    flood_waiting_since: float | None = None  # a due flood first planned
    reply_charged: float | None = None  # the request whose flooded answer was charged


def _recent(s: _Contact, now: float) -> bool:
    return s.answered_at is not None and 0 <= now - s.answered_at <= ANSWERED_RECENTLY_S


def _fresh(t: float | None, now: float) -> bool:
    return t is not None and 0 <= now - t <= HEARD_RECENTLY_S


class Scheduler:
    def __init__(
        self, now: float, *, saved: Saved | None = None, elapsed_s: float = 0.0,
        lanes: LaneFactory | None = None,
    ) -> None:  # fmt: skip
        """`saved` and `elapsed_s`: the schedule of the last run and the time since it was
        saved. `lanes`: builds the lanes (tests); they are built at the first decision, when
        the radio's settings are known."""
        self._lanes_factory = lanes or build_lanes
        self._lora: LoRaParams | None = None
        self._own: AirtimeBudget | None = None
        self._mesh: AirtimeBudget | None = None
        self._contacts: dict[str, _Contact] = {}
        self._parked: dict[str, tuple[float, _Contact]] = {}  # key -> (since, schedule)
        self._floods: list[float] = []
        self._in_flight: str | None = None
        self._gap_until = -math.inf
        self.version = 0  # changes with every change of the schedule
        self.urgent = False  # a flood was sent since the last export: save at once
        if saved is not None:
            self._restore(saved, now, max(0.0, elapsed_s))

    @property
    def in_flight(self) -> str | None:
        """The contact asked and not yet answered, timed out or retried."""
        return self._in_flight

    # --- decisions ------------------------------------------------------------------------------

    def decide(self, lora: LoRaParams, candidates: Sequence[Candidate], now: float) -> Poll | float:
        """The next request to send now, or how long to sleep before deciding again."""
        self._use(lora, now)
        self._track(candidates, now)
        planned = self._plan(candidates, now)
        if self._in_flight is not None:
            return STEP_S
        if now < self._gap_until:
            return self._gap_until - now

        def starved(s: _Contact) -> bool:  # a due flood that waited long enough goes first
            since = s.flood_waiting_since
            return s.due_at <= now and since is not None and now - since >= FLOOD_MAX_WAIT_S

        def order(c: Candidate) -> tuple[bool, float, str]:
            s = self._contacts[c.key]
            return not starved(s), s.due_at, c.key

        sleep = STEP_S
        for c in sorted(candidates, key=order):
            s = self._contacts[c.key]
            if s.due_at > now:
                return min(sleep, s.due_at - now)
            poll = planned.get(c.key)
            if poll is None:
                continue
            wait = self._wait(poll, now)
            if math.isinf(wait):  # a direct poll costing more than a lane holds: never sent
                s.due_at = now + MAX_DELAY_S
                self.version += 1
                continue
            if wait > 0:
                if poll.flood and not starved(s):  # a flood does not hold direct polls back
                    sleep = min(sleep, wait)
                    continue
                return min(sleep, wait)  # the most overdue goes first
            return poll
        return sleep

    def _plan(self, candidates: Sequence[Candidate], now: float) -> dict[str, Poll | None]:
        """What each due candidate would be asked; also when a due flood was first planned."""
        planned: dict[str, Poll | None] = {}
        for c in candidates:
            if c.key == self._in_flight:
                continue
            s = self._contacts[c.key]
            self._sign_of_life(s, now)
            poll = self._poll_for(c, s, now) if s.due_at <= now else None
            planned[c.key] = poll
            if poll is None or not poll.flood:
                s.flood_waiting_since = None
            elif s.flood_waiting_since is None:
                s.flood_waiting_since = now
        return planned

    def _poll_for(self, c: Candidate, s: _Contact, now: float) -> Poll | None:
        if c.path_bytes is None:  # the radio would flood: only if heard recently
            if _fresh(s.heard_at, now) and self._flood_allowed(s, now):
                return Poll(c.key, True, self._flood_costs(s), "flood")
            return None
        purpose = "retry" if s.failures and _recent(s, now) else "poll"
        costs = airtime.direct_poll_costs(c.hops or 0, c.path_bytes, s.lpp_bytes, self._params)
        return Poll(c.key, False, costs, purpose)

    def _sign_of_life(self, s: _Contact, now: float) -> None:
        """Heard by other means after the last unanswered request: the back-off ends, the next
        request follows the normal interval after the last one, never sooner (ADR 0018)."""
        if not s.failures or s.polled_at is None or s.heard_at is None:
            return
        if s.heard_at <= s.polled_at:
            return
        target = s.polled_at + self._interval(s, now)
        if s.due_at > target:
            s.due_at = target
            self.version += 1

    def _flood_allowed(self, s: _Contact, now: float) -> bool:
        self._floods = [t for t in self._floods if 0 <= now - t < HOUR_S]
        if len(self._floods) >= FLOODS_PER_HOUR:
            return False
        gap = min(MAX_FLOOD_GAP_S, FLOOD_GAP_S * 2.0**s.floods) if _recent(s, now) else PROBE_GAP_S
        return s.flood_at is None or now - s.flood_at >= gap

    def _flood_costs(self, s: _Contact) -> Costs:
        return airtime.flood_poll_costs(s.lpp_bytes, self._params)

    def _wait(self, poll: Poll, now: float) -> float:
        own, mesh = self._lane(self._own), self._lane(self._mesh)
        own_s, mesh_s = poll.costs.own_s, poll.costs.mesh_s
        if poll.flood:  # waits for full lanes at most; the rest becomes debt
            own_s, mesh_s = min(own_s, own.capacity_s), min(mesh_s, mesh.capacity_s)
        return max(own.wait_s(own_s, now), mesh.wait_s(mesh_s, now))

    def _interval(self, s: _Contact, now: float) -> float:
        """The normal interval, request to request, without the back-off."""
        if not _recent(s, now):
            return PROBE_S
        hops, path_bytes = s.hops or 0, s.path_bytes or 0
        cost = airtime.direct_poll_costs(hops, path_bytes, s.lpp_bytes, self._params).mesh_s
        if not s.located:
            return max(UNLOCATED_S, cost / RESTING_SHARE)
        if s.still < STILL_POLLS:
            return max(MOVING_MIN_S, cost / MOVING_SHARE)
        return max(RESTING_MIN_S, cost / RESTING_SHARE)

    def _backoff(self, s: _Contact, base: float, now: float) -> float:
        """Doubling from the contact's own interval: up to 6 h for one that has not answered
        within a day (ADR 0018), up to max(30 min, its interval) for one that has."""
        cap = max(AWAY_S, base) if _recent(s, now) else max(MAX_DELAY_S, base)
        return min(cap, base * 2.0 ** (s.failures - 1))

    def _use(self, lora: LoRaParams, now: float) -> None:
        """Lanes for the radio's settings, empty; new settings start them afresh."""
        if lora == self._lora:
            return
        self._lora = lora
        capacity = airtime.flood_poll_costs(DEFAULT_LPP_BYTES, lora)
        self._own, self._mesh = self._lanes_factory(capacity, now)

    def _track(self, candidates: Sequence[Candidate], now: float) -> None:
        """New candidates are due at once; the schedule of one that left is parked for a day,
        and comes back with its failures and flood times."""
        keys = {c.key for c in candidates}
        for key in [k for k in self._contacts if k not in keys and k != self._in_flight]:
            self._parked[key] = (now, self._contacts.pop(key))
            self.version += 1
        for key in [k for k, (since, _) in self._parked.items() if now - since > PARK_S]:
            del self._parked[key]
            self.version += 1
        for c in candidates:
            s = self._contacts.get(c.key)
            if s is None:
                parked = self._parked.pop(c.key, None)
                s = parked[1] if parked else _Contact(due_at=now)
                self._contacts[c.key] = s
                self.version += 1
            s.hops, s.path_bytes, s.heard_at = c.hops, c.path_bytes, c.heard_at

    # --- outcomes -------------------------------------------------------------------------------

    def sent(self, poll: Poll, *, flood: bool, now: float) -> None:
        """The radio sent the request (SENT): charged in full, answered or not. `flood` is the
        route the radio reports; a flood for a poll planned direct is charged as a flood."""
        s = self._contacts.get(poll.key)
        costs = poll.costs
        if flood and not poll.flood and s is not None:
            costs = self._flood_costs(s)
        self._lane(self._own).charge(costs.own_s, now)
        self._lane(self._mesh).charge(costs.mesh_s, now)
        if flood:
            self._floods.append(now)
            self.urgent = True
        if s is not None:
            if flood:
                s.flood_at = now
                if _recent(s, now):
                    s.floods += 1
            s.polled_at, s.last_flood = now, flood
            s.flood_waiting_since = None
        self._in_flight = poll.key
        self.version += 1

    def answered(
        self, key: str, now: float, cells: tuple[int, int] | None, lpp_bytes: int, *,
        route: tuple[int | None, int | None] | None = None, late: bool = False,
    ) -> None:  # fmt: skip
        """An answer of the contact asked; `late`: after its wait, before the next request.
        `route`: hops and path bytes in the table the answer was accepted on."""
        if not late:
            self._in_flight, self._gap_until = None, now + POLL_GAP_S
        s = self._contacts.get(key)
        if s is None:
            return
        s.failures, s.uncertain, s.answered_at = 0, False, now
        s.flood_waiting_since = None
        if not s.last_flood:
            s.floods = 0  # a direct answer: the route works
        s.lpp_bytes = min(MAX_LPP_BYTES, lpp_bytes) or DEFAULT_LPP_BYTES
        if route is not None:
            s.hops, s.path_bytes = route
        _movement(s, cells)
        s.due_at = (s.polled_at if s.polled_at is not None else now) + self._interval(s, now)
        self.version += 1

    def unanswered(self, key: str, now: float, *, uncertain: bool = False) -> int:
        """The wait ended without an answer; returns the failures in a row. `uncertain`: the
        answer may have been lost on the way to the gateway (TCP), counted only the second
        time in a row."""
        self._in_flight, self._gap_until = None, now + POLL_GAP_S
        s = self._contacts.get(key)
        if s is None:
            return 0
        s.flood_waiting_since = None
        if uncertain and not s.uncertain:
            s.uncertain, s.due_at = True, now + RETRY_S
            self.version += 1
            return s.failures
        s.uncertain = False
        base = self._interval(s, now)  # before this failure counts
        s.failures = min(MAX_FAILURES, s.failures + 1)
        if s.failures == 1 and _recent(s, now) and not s.last_flood:
            s.due_at = now + RETRY_S  # one quick retry: a single lost packet
        else:
            s.due_at = now + self._backoff(s, base, now)
        self.version += 1
        return s.failures

    def retry(self, key: str, now: float, after_s: float | None = None) -> None:
        """Nothing was learned (refused, withdrawn, cut by a disconnect): no failure. After a
        request the radio sent, the gap holds as after any outcome."""
        if self._in_flight == key:
            self._gap_until = now + POLL_GAP_S
            self._in_flight = None
        s = self._contacts.get(key)
        if s is not None:
            s.due_at = now + (RETRY_S if after_s is None else after_s)
            s.flood_waiting_since = None
            self.version += 1

    def flooded_reply(self, key: str, now: float) -> None:
        """The companion flooded its answer to the last direct request (the packet log shows
        it): its flood and the path the radio sends back are charged, once per request."""
        s = self._contacts.get(key)
        if s is None or s.last_flood or s.polled_at is None or s.reply_charged == s.polled_at:
            return
        s.reply_charged = s.polled_at
        costs = airtime.flooded_reply_costs(
            s.lpp_bytes, s.hops or 0, s.path_bytes or 0, self._params
        )
        self._lane(self._own).charge(costs.own_s, now)
        self._lane(self._mesh).charge(costs.mesh_s, now)
        self.version += 1

    # --- views ----------------------------------------------------------------------------------

    def used_s(self, now: float) -> tuple[float, float]:
        """Own and estimated mesh airtime of the last hour; (0, 0) before the first decision."""
        if self._own is None or self._mesh is None:
            return 0.0, 0.0
        return self._own.used_s(now), self._mesh.used_s(now)

    def view(self, key: str, now: float) -> str | None:
        """The contact's state: new, moving, resting, without_location, retrying, away (it
        answered within a day), no_answer, unknown (neither asked nor answered lately)."""
        s = self._contacts.get(key)
        if s is None:
            return None
        if s.polled_at is None and s.answered_at is None:
            return "new"
        if not _recent(s, now):
            return "no_answer" if s.failures else "unknown"
        if s.failures:
            return "away" if s.failures >= 2 else "retrying"
        if not s.located:
            return "without_location"
        return "moving" if s.still < STILL_POLLS else "resting"

    def interval_s(self, key: str, now: float) -> float | None:
        s = self._contacts.get(key)
        if s is None or self._lora is None:
            return None
        return self._interval(s, now)

    # --- persistence ----------------------------------------------------------------------------

    def export(self, now: float) -> Saved:
        """The schedule for the state file, also of parked contacts."""
        self.urgent = False
        everyone = {**{k: s for k, (_, s) in self._parked.items()}, **self._contacts}

        def age(t: float | None) -> float | None:
            return None if t is None else max(0.0, now - t)

        contacts = {
            key: SavedContact(
                due_in_s=min(MAX_DUE_S, max(0.0, s.due_at - now)), failures=s.failures,
                answered_age_s=age(s.answered_at), polled_age_s=age(s.polled_at),
                flood_age_s=age(s.flood_at), floods=s.floods, located=s.located,
                still=s.still, lpp_bytes=s.lpp_bytes,
            )
            for key, s in everyone.items()
        }  # fmt: skip
        floods = tuple(now - t for t in self._floods if 0 <= now - t < HOUR_S)
        return Saved(contacts, floods)

    def _restore(self, saved: Saved, now: float, elapsed: float) -> None:
        def at(age: float | None) -> float | None:
            return None if age is None else now - (age + elapsed)

        for key, c in saved.contacts.items():
            s = _Contact(
                due_at=now + max(0.0, min(MAX_DUE_S, c.due_in_s) - elapsed),
                failures=c.failures, answered_at=at(c.answered_age_s),
                polled_at=at(c.polled_age_s), flood_at=at(c.flood_age_s), floods=c.floods,
                located=c.located, still=c.still, lpp_bytes=c.lpp_bytes,
            )  # fmt: skip
            self._parked[key] = (now, s)  # until it is a candidate again
        self._floods = [now - (a + elapsed) for a in saved.floods_age_s if a + elapsed < HOUR_S]

    # --- helpers ----------------------------------------------------------------------------------

    @property
    def _params(self) -> LoRaParams:
        if self._lora is None:
            raise RuntimeError("no decision yet: the radio's settings are not known")
        return self._lora

    @staticmethod
    def _lane(lane: AirtimeBudget | None) -> AirtimeBudget:
        if lane is None:
            raise RuntimeError("no decision yet: the lanes are not built")
        return lane


def _movement(s: _Contact, cells: tuple[int, int] | None) -> None:
    """Moving from a displacement of 50 m on; resting after two answers under 50 m. At rest the
    reference stays, so slow drift adds up and jitter around it never counts as a move."""
    if cells is None:
        s.located = False  # reference and class kept
        return
    s.located = True
    if s.anchor is None:  # the first position of this run: the class stays
        s.anchor = cells
        return
    if distance_m(centre(*s.anchor), centre(*cells)) >= MOVED_M:
        s.anchor, s.still = cells, 0
    elif s.still < STILL_POLLS:  # while moving: measured from the last fix
        s.anchor, s.still = cells, s.still + 1
