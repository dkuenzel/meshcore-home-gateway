"""The telemetry schedule (core/telemetry.py, ADR 0018): pure, on a clock the tests set."""

import itertools
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import get_args

import pytest

from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.airtime import Costs, meshcore_lora
from meshcore_home_gateway.core.budget import AirtimeBudget
from meshcore_home_gateway.core.telemetry import (
    Candidate,
    PauseReason,
    Poll,
    Saved,
    SavedContact,
    Scheduler,
    TelemetryEvent,
    TelemetryState,
    TelemetryStatus,
    answer_wait_s,
)
from meshcore_home_gateway.state import JsonStateFile, TelemetryRecords

EU = meshcore_lora(62_500.0, 8, 8)  # EU/UK Narrow
A, B, C, D, E = ("a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32, "e5" * 32)
HOME = (520_000, 100_000)  # cells of 1e-4 degrees: one cell of latitude is 11.1 m
HOUR = 3600.0
DAY = 86_400.0


def wide(capacity: Costs, now: float) -> tuple[AirtimeBudget, AirtimeBudget]:
    """Lanes that never hold a request back."""
    return AirtimeBudget(3600.0, 100.0, now), AirtimeBudget(3600.0, 1000.0, now)


def direct(key: str, hops: int = 0, heard_at: float | None = None) -> Candidate:
    return Candidate(key, hops, hops, heard_at)


def pathless(key: str, heard_at: float | None = None) -> Candidate:
    return Candidate(key, None, None, heard_at)


def poll(s: Scheduler, candidates: Sequence[Candidate], now: float) -> Poll:
    decision = s.decide(EU, candidates, now)
    assert isinstance(decision, Poll), f"no poll at {now}: sleep {decision}"
    return decision


def ask(
    s: Scheduler, candidates: Sequence[Candidate], now: float, *,
    cells: tuple[int, int] | None = HOME, answer: bool = True,
) -> Poll:  # fmt: skip
    """Decide, send as planned, then the answer 2 s later or the timeout after 5 s."""
    p = poll(s, candidates, now)
    s.sent(p, flood=p.flood, now=now)
    if answer:
        s.answered(p.key, now + 2, cells, 19)
    else:
        s.unanswered(p.key, now + 5)
    return p


def due_at(s: Scheduler, candidates: Sequence[Candidate], start: float, until: float) -> float:
    """When the next poll is sent, deciding as the poller does."""
    now = start
    while now < until:
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, Poll):
            return now
        now += max(decision, 0.001)
    raise AssertionError(f"no poll until {until}")


# --- lanes and one request at a time -----------------------------------------------------------


def test_the_first_poll_waits_for_the_empty_lanes() -> None:
    """No burst at a start: the lanes start empty, a direct poll costs 0.378 s of own time
    (refilled at 18 s/h) and 0.887 s in the mesh (90 s/h)."""
    s = Scheduler(0.0)
    assert s.used_s(0.0) == (0.0, 0.0)
    assert due_at(s, [direct(A)], 0.0, 200.0) == pytest.approx(75.57, abs=0.01)


def test_one_request_in_flight_and_a_gap_after_each_outcome() -> None:
    s = Scheduler(0.0, lanes=wide)
    first = poll(s, [direct(A), direct(B)], 0.0)
    s.sent(first, flood=False, now=0.0)
    assert s.in_flight == A
    assert s.decide(EU, [direct(A), direct(B)], 1.0) == telemetry.STEP_S
    s.answered(A, 3.0, HOME, 19)
    assert [s.in_flight] == [None]
    assert s.decide(EU, [direct(A), direct(B)], 5.0) == pytest.approx(8.0)  # 10 s after it
    assert poll(s, [direct(A), direct(B)], 13.0).key == B


def test_lanes_bound_the_hourly_airtime() -> None:
    """Five companions at rest with long answers want more than the own lane allows: what it
    lets through stays within its burst plus 18 s/h, and the mesh lane within 90 s/h."""
    s = Scheduler(0.0)
    candidates = [direct(k) for k in (A, B, C, D, E)]
    now, sent = 0.0, []
    while now < 4 * HOUR:
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=False, now=now)
        sent.append(now)
        s.answered(decision.key, now + 1, (HOME[0] + int(now), HOME[1]), 19)  # always moving
        now += 1
    own, mesh = s.used_s(now)
    assert own <= 0.83 + 18.0
    assert mesh <= 20.5 + 90.0
    last_hour = [t for t in sent if t > now - HOUR]
    assert len(last_hour) == pytest.approx(18.0 / 0.378, abs=2)  # own lane saturated


def test_a_direct_poll_costlier_than_a_lane_is_never_sent() -> None:
    """19 hops cost 21.3 s in the mesh, more than the lane holds (a flood, 20.4 s)."""
    s = Scheduler(0.0)
    far = Candidate(A, 19, 19, None)
    assert s.decide(EU, [far, direct(B)], 0.0) == telemetry.STEP_S
    assert due_at(s, [far, direct(B)], 0.0, 200.0) == pytest.approx(75.57, abs=0.01)
    assert poll(s, [far, direct(B)], 75.58).key == B


def test_new_radio_settings_start_the_lanes_empty() -> None:
    s = Scheduler(0.0)
    assert due_at(s, [direct(A)], 0.0, 200.0) == pytest.approx(75.57, abs=0.01)
    other = meshcore_lora(250_000.0, 11, 5)
    assert isinstance(s.decide(other, [direct(A)], 80.0), float)


def test_answer_wait() -> None:
    assert answer_wait_s(3210) == pytest.approx(5.0)
    assert answer_wait_s(7060) == pytest.approx(8.825)
    assert answer_wait_s(52_000) == pytest.approx(65.0)  # no upper cap: 18 hops


# --- routes -------------------------------------------------------------------------------------


def test_a_contact_without_a_path_is_asked_only_when_heard_recently() -> None:
    """The radio floods a contact it has no path to; a flood only for one heard by other means
    within 30 minutes (ADR 0018)."""
    s = Scheduler(0.0, lanes=wide)
    assert s.decide(EU, [pathless(A)], 0.0) == telemetry.STEP_S
    assert s.decide(EU, [pathless(A, heard_at=-1801.0)], 1.0) == telemetry.STEP_S
    flood = poll(s, [pathless(A, heard_at=-60.0)], 2.0)
    assert (flood.flood, flood.purpose) == (True, "flood")
    assert flood.costs.mesh_s == pytest.approx(20.41, abs=0.01)


def test_floods_at_most_four_an_hour_and_one_a_day_to_a_contact_that_never_answered() -> None:
    s = Scheduler(0.0, lanes=wide)
    keys = [f"{i:02x}" * 32 for i in range(1, 7)]
    now, floods = 0.0, []
    while now < 2 * HOUR:
        candidates = [pathless(k, heard_at=now) for k in keys]
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(decision.key, now + 9)
        floods.append((now, decision.key))
        now += 9
    assert len(floods) == 6  # four in the first hour, two in the next
    assert len([t for t, _ in floods if t < HOUR]) == 4
    assert len({k for _, k in floods}) == 6  # none twice within a day


def test_floods_to_a_contact_that_answered_double_their_gap() -> None:
    """A companion whose path the outbox reset: flooded when heard; the gap is 30 min x 2^floods,
    so after the first flood 60, then 120 and 240 min."""
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    now, floods = 10.0, []
    while now < 8 * HOUR:
        decision = s.decide(EU, [pathless(A, heard_at=now)], now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(A, now + 9)
        floods.append(now)
        now += 9
    gaps = [b - a for a, b in itertools.pairwise(floods)]
    assert gaps[:3] == pytest.approx([3600.0, 7200.0, 14_400.0], abs=60)  # 30 min x 2^floods


def test_a_flood_answered_teaches_a_path_and_a_direct_answer_resets_the_spacing() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    flood = poll(s, [pathless(A, heard_at=900.0)], 900.0)
    assert flood.flood
    s.sent(flood, flood=True, now=900.0)
    s.answered(A, 903.0, HOME, 19, route=(1, 1))  # the table read after the path it sent
    interval = s.interval_s(A, 903.0)
    assert interval is not None
    assert interval == pytest.approx(1839.0, abs=1)  # resting, one hop
    due = 900.0 + interval
    nxt = ask(s, [direct(A, hops=1)], due, cells=(HOME[0] + 10, HOME[1]))  # moved 111 m
    assert not nxt.flood
    # a direct answer: the next flood may follow 30 min after the last one again, not 60
    moving = s.interval_s(A, due + 2)
    assert moving is not None
    later = due + moving
    assert 1800.0 <= later - 900.0 < 3600.0
    assert poll(s, [pathless(A, heard_at=later)], later).flood


def test_a_flood_for_a_poll_planned_direct_is_charged_as_a_flood() -> None:
    """The cached table still showed a path the radio no longer had: SENT says flood."""
    s = Scheduler(0.0, lanes=wide)
    p = poll(s, [direct(A)], 0.0)
    assert not p.flood
    s.sent(p, flood=True, now=0.0)
    assert s.used_s(0.0)[1] == pytest.approx(20.41, abs=0.01)
    assert s.urgent


def test_a_flooded_reply_is_charged_once() -> None:
    s = Scheduler(0.0, lanes=wide)
    p = poll(s, [direct(A)], 0.0)
    s.sent(p, flood=False, now=0.0)
    s.flooded_reply(A, 1.0)
    s.flooded_reply(A, 1.5)
    assert s.used_s(2.0)[1] == pytest.approx(0.887 + 11.75, abs=0.01)


@pytest.mark.parametrize("heard_every_s", [None, 120.0])
def test_a_due_flood_does_not_wait_forever_behind_direct_polls(heard_every_s: float | None) -> None:
    """Two moving companions, asked every 90 s, keep the own lane from filling; a flood needs
    both lanes full. After 10 minutes (the first decision after it: 603.4 s) the flood goes
    first and waits for the mesh lane, which holds 8.88 of its 20.41 s by then: at 1064.7 s, with
    no direct poll in between. Hearing the contact again does not restart the wait."""
    saved = Saved({
        k: SavedContact(0.0, answered_age_s=10.0, polled_age_s=10.0, located=True, still=0)
        for k in (A, B)
    })  # fmt: skip
    s = Scheduler(0.0, saved=saved)
    now, polls, answers = 0.0, [], {A: 0, B: 0}
    while now < 1500.0:
        heard = 0.0 if heard_every_s is None else now - now % heard_every_s
        candidates = [direct(A), direct(B), pathless(C, heard_at=heard)]
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=decision.flood, now=now)
        if decision.key in answers:  # each moves 111 m between two answers
            answers[decision.key] += 1
            cells = (HOME[0] + 10 * (answers[decision.key] % 2), HOME[1])
            s.answered(decision.key, now + 1, cells, 19)
        else:
            s.unanswered(decision.key, now + 9)
        polls.append((now, decision.key))
        now += 1
    flood_at = next(t for t, k in polls if k == C)
    assert flood_at == pytest.approx(603.4 + (20.409 - 8.878) / 0.025, abs=0.5)
    assert len([t for t, k in polls if k != C and t < 600.0]) >= 6  # direct polls went on
    assert not [t for t, k in polls if k != C and 600.0 < t < flood_at]  # then the flood first
    assert s.interval_s(A, now) == telemetry.MOVING_MIN_S  # still moving


# --- intervals, back-off and signs of life ------------------------------------------------------


def test_intervals_follow_the_share_of_channel_time() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)  # first position: resting, the class a new contact starts with
    assert s.interval_s(A, 2.0) == 900.0  # 0.887 s / 0.1 %, floor 15 min
    ask(s, [direct(A)], 900.0, cells=(HOME[0] + 5, HOME[1]))  # 55 m
    assert s.interval_s(A, 902.0) == 90.0  # 0.887 s / 1 %, floor 90 s
    assert s.view(A, 902.0) == "moving"
    ask(s, [direct(A)], 990.0, cells=None)
    assert s.interval_s(A, 992.0) == 3600.0  # no position: once an hour
    assert s.view(A, 992.0) == "without_location"
    s2 = Scheduler(0.0, lanes=wide)
    ask(s2, [direct(B, hops=2)], 0.0)
    assert s2.interval_s(B, 2.0) == pytest.approx(2791.0, abs=1)  # longer with more hops


def test_movement_needs_50_m_and_rest_needs_two_answers_under_it() -> None:
    s = Scheduler(0.0, lanes=wide)
    lat, lon = HOME
    now = 0.0
    for cells, view in [
        ((lat, lon), "resting"),
        ((lat + 5, lon), "moving"),  # 55.6 m
        ((lat + 6, lon), "moving"),  # 11 m from the last fix: one answer under 50 m
        ((lat + 7, lon), "resting"),  # two
        ((lat + 4, lon), "resting"),  # jitter around the reference
        ((lat + 10, lon), "resting"),  # 33 m from it
        ((lat + 12, lon), "moving"),  # slow drift adds up: 55.6 m
    ]:
        decision = poll(s, [direct(A)], now)
        s.sent(decision, flood=False, now=now)
        s.answered(A, now + 1, cells, 19)
        assert s.view(A, now + 1) == view, cells
        now = s_next(s, A, now)


def s_next(s: Scheduler, key: str, now: float) -> float:
    interval = s.interval_s(key, now + 1)
    assert interval is not None
    return now + interval


@pytest.mark.parametrize(
    ("first_cells", "moving", "delays"),
    [
        (None, False, [30.0, 3600.0, 3600.0]),  # without position: its interval caps it
        (HOME, False, [30.0, 1800.0, 1800.0]),  # resting direct neighbour: 2 x 900 s
        (HOME, True, [30.0, 180.0, 360.0, 720.0, 1440.0, 1800.0, 1800.0]),
    ],
)
def test_a_companion_that_goes_silent_is_asked_on_its_path_at_least_every_30_min(
    first_cells: tuple[int, int] | None, moving: bool, delays: list[float]
) -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0, cells=first_cells)
    now = 2.0
    if moving:
        now = s_next(s, A, 0.0)
        ask(s, [direct(A)], now, cells=(HOME[0] + 10, HOME[1]))
        assert s.view(A, now + 2) == "moving"
    now = s_next(s, A, now)
    for expected in delays:
        sent = due_at(s, [direct(A)], now, now + 10 * HOUR)
        decision = poll(s, [direct(A)], sent)
        s.sent(decision, flood=False, now=sent)
        s.unanswered(A, sent + 5)
        nxt = due_at(s, [direct(A)], sent + 5, sent + 10 * HOUR)
        assert nxt - (sent + 5) == pytest.approx(expected, abs=1), expected
        now = nxt
    assert s.view(A, now) == "away"


def test_a_contact_that_never_answered_backs_off_to_six_hours() -> None:
    s = Scheduler(0.0, lanes=wide)
    now, delays = 0.0, []
    for _ in range(5):
        sent = due_at(s, [direct(A)], now, now + 10 * HOUR)
        s.sent(poll(s, [direct(A)], sent), flood=False, now=sent)
        s.unanswered(A, sent + 5)
        now = due_at(s, [direct(A)], sent + 5, sent + 10 * HOUR)
        delays.append(round(now - (sent + 5)))
    assert delays == [3600, 7200, 14_400, 21_600, 21_600]
    assert s.view(A, now) == "no_answer"


def test_a_sign_of_life_ends_the_back_off_but_never_asks_sooner() -> None:
    s = Scheduler(0.0, lanes=wide)
    s.sent(poll(s, [direct(A)], 0.0), flood=False, now=0.0)
    s.unanswered(A, 5.0)  # never answered: next in 1 h
    s.sent(poll(s, [direct(A)], 3605.0), flood=False, now=3605.0)
    s.unanswered(A, 3610.0)  # next in 2 h
    heard = 3700.0
    assert due_at(s, [direct(A, heard_at=heard)], 3800.0, 20_000.0) == pytest.approx(
        3605.0 + 3600.0, abs=0.01
    )  # its interval after the last request
    # heard before the last request: no effect
    s2 = Scheduler(0.0, lanes=wide)
    s2.sent(poll(s2, [direct(A)], 0.0), flood=False, now=0.0)
    s2.unanswered(A, 5.0)
    assert due_at(s2, [direct(A, heard_at=-1.0)], 10.0, 20_000.0) == pytest.approx(3605.0)


def test_a_late_answer_undoes_the_timeout() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    p = poll(s, [direct(A)], 900.0)
    s.sent(p, flood=False, now=900.0)
    s.unanswered(A, 905.0)
    assert s.view(A, 905.0) == "retrying"
    s.answered(A, 930.0, HOME, 19, late=True)
    assert s.view(A, 930.0) == "resting"
    assert due_at(s, [direct(A)], 931.0, 5000.0) == pytest.approx(1800.0)  # request to request


def test_an_uncertain_timeout_counts_only_the_second_time() -> None:
    """Over TCP an answer can be lost while the radio's push queue is full."""
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    s.sent(poll(s, [direct(A)], 900.0), flood=False, now=900.0)
    assert s.unanswered(A, 905.0, uncertain=True) == 0
    assert s.view(A, 905.0) == "resting"
    sent = due_at(s, [direct(A)], 905.0, 2000.0)
    assert sent == pytest.approx(935.0)
    s.sent(poll(s, [direct(A)], sent), flood=False, now=sent)
    assert s.unanswered(A, sent + 5, uncertain=True) == 1


def test_a_retry_counts_no_failure() -> None:
    s = Scheduler(0.0, lanes=wide)
    p = poll(s, [direct(A), direct(B)], 0.0)
    s.retry(p.key, 0.0)  # refused before it was sent: no gap
    assert poll(s, [direct(A), direct(B)], 0.0).key == B
    s.sent(poll(s, [direct(A), direct(B)], 1.0), flood=False, now=1.0)
    s.retry(B, 2.0, after_s=0.0)  # cut by a disconnect after SENT: the gap holds
    assert s.decide(EU, [direct(A), direct(B)], 3.0) == pytest.approx(9.0)
    assert s.view(B, 3.0) == "unknown"  # asked, nothing learned


# --- candidates, persistence ---------------------------------------------------------------------


def test_a_contact_that_leaves_and_comes_back_keeps_its_schedule() -> None:
    s = Scheduler(0.0, lanes=wide)
    s.sent(poll(s, [direct(A)], 0.0), flood=False, now=0.0)
    s.unanswered(A, 5.0)
    assert s.decide(EU, [direct(B)], 20.0) is not None  # A left
    assert due_at(s, [direct(A)], 30.0, 10 * HOUR) == pytest.approx(3605.0)
    s.decide(EU, [direct(B)], 4000.0)
    assert due_at(s, [direct(A)], 91_000.0, 100_000.0) == 91_000.0  # parked over a day: new
    assert s.view(A, 91_000.0) == "new"


def test_the_schedule_survives_a_restart_without_a_burst() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)  # resting: due at 900
    s.sent(poll(s, [pathless(B, heard_at=10.0)], 20.0), flood=True, now=20.0)
    s.unanswered(B, 29.0)
    s.decide(EU, [direct(A), pathless(B), direct(C)], 40.0)  # C seen, never asked
    saved = s.export(100.0)
    assert not s.urgent
    assert saved.floods_age_s == (80.0,)
    assert saved.contacts[A] == SavedContact(
        800.0, answered_age_s=98.0, polled_age_s=100.0, located=True, lpp_bytes=19,
        located_age_s=98.0,
    )  # fmt: skip
    assert saved.contacts[B].failures == 1
    assert saved.contacts[B].flood_age_s == 80.0
    # 60 s later, empty lanes: nothing sent at once
    r = Scheduler(1000.0, saved=saved, elapsed_s=60.0)
    candidates = [direct(A), pathless(B, heard_at=1000.0), direct(C)]
    first = due_at(r, candidates, 1000.0, 2000.0)
    assert first == pytest.approx(1075.57, abs=0.01)
    assert poll(r, candidates, first).key == C  # B's flood: one a day; A not due until 1740
    exported = r.export(first)
    assert exported.contacts[B].flood_age_s == pytest.approx(80.0 + 60.0 + first - 1000.0)
    assert exported.floods_age_s == pytest.approx((140.0 + first - 1000.0,))


def test_a_restored_schedule_tolerates_a_clock_that_went_back() -> None:
    saved = Saved({A: SavedContact(600.0, failures=1, polled_age_s=10.0)}, (100.0,))
    r = Scheduler(0.0, saved=saved, elapsed_s=0.0, lanes=wide)
    assert due_at(r, [direct(A)], 0.0, 2000.0) == pytest.approx(600.0)
    assert r.export(0.0).floods_age_s == (100.0,)


@pytest.mark.parametrize("elapsed", [0.0, None])
def test_after_the_clock_went_back_a_restored_answer_counts_as_a_day_old(
    elapsed: float | None,
) -> None:
    """How long the gateway was stopped is unknown when the clock went back (elapsed None): a
    contact that answered 22 h before the save may have been silent for more than a day, so it
    gets one flood a day, not the doubling gaps of one that answered within the day."""
    saved = Saved({A: SavedContact(
        0.0, failures=1, answered_age_s=22 * HOUR, polled_age_s=10.0, flood_age_s=10.0, floods=1,
    )})  # fmt: skip
    r = Scheduler(0.0, saved=saved, elapsed_s=elapsed, lanes=wide)
    floods, now = [], 0.0
    while now < 6 * HOUR:
        decision = r.decide(EU, [pathless(A, heard_at=now)], now)
        if isinstance(decision, Poll):
            floods.append(now)
            r.sent(decision, flood=True, now=now)
            r.unanswered(A, now + 9)
        now += 60.0
    if elapsed is None:
        assert floods == []
        assert r.view(A, 0.0) == "no_answer"
    else:  # answered within the day: an hour after its last flood
        assert floods[0] == pytest.approx(HOUR, abs=60.0)


def test_a_restored_contact_that_never_comes_back_is_dropped_a_day_after_the_first_table() -> None:
    """Time without the radio does not count: a candidate of the last run leaves the
    candidates only when a table read lacks it."""
    saved = Saved({A: SavedContact(0.0, failures=3)})
    r = Scheduler(0.0, saved=saved, lanes=wide)
    assert r.export(90_000.0).contacts[A].parked_age_s is None  # no table seen yet
    r.decide(EU, [direct(B)], 90_000.0)
    assert r.export(90_000.0).contacts[A].parked_age_s == 0.0
    r.decide(EU, [direct(B)], 90_000.0 + telemetry.PARK_S + 1)
    assert A not in r.export(90_000.0 + telemetry.PARK_S + 1).contacts


def test_a_restored_candidate_keeps_its_schedule_however_long_the_radio_was_away() -> None:
    saved = Saved({A: SavedContact(0.0, failures=3, flood_age_s=100.0)})
    r = Scheduler(0.0, saved=saved, lanes=wide)
    again = Scheduler(0.0, saved=r.export(3600.0), elapsed_s=90_000.0, lanes=wide)
    again.decide(EU, [pathless(A)], 0.0)
    kept = again.export(0.0).contacts[A]
    assert kept.failures == 3
    assert kept.flood_age_s == pytest.approx(100.0 + 3600.0 + 90_000.0)


def test_constants_are_read_when_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "HEARD_RECENTLY_S", 10.0)
    s = Scheduler(0.0, lanes=wide)
    assert s.decide(EU, [pathless(A, heard_at=-20.0)], 0.0) == telemetry.STEP_S


def test_views_before_any_decision() -> None:
    s = Scheduler(0.0)
    assert s.view(A, 0.0) is None
    assert s.interval_s(A, 0.0) is None
    assert s.export(0.0) == Saved({}, ())


Factory = Callable[[Costs, float], tuple[AirtimeBudget, AirtimeBudget]]


# --- review findings: paths, freshness, debt, persistence ----------------------------------------


def test_a_contact_without_a_path_heard_after_its_last_request_is_due_at_once() -> None:
    """A path-less contact may be flooded only while it was heard recently, so a sign of life
    makes it due at once, also between its requests (an hour apart without a position)."""
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0, cells=None)  # answers without a position: next in an hour
    heard = 1200.0  # its path was reset meanwhile (a message's flood fallback); it advertises
    decision = poll(s, [pathless(A, heard_at=heard)], heard)
    assert (decision.flood, decision.purpose) == (True, "flood")


def test_a_flood_planned_while_heard_stays_planned_while_it_waits_for_the_lanes() -> None:
    """Five companions heard together, each flood needing both lanes full (13.6 min at EU/UK
    Narrow): all five go out, also those that wait longer than 30 min, and the fifth once the
    first flood is an hour old (4 an hour)."""
    s = Scheduler(0.0)
    keys = (A, B, C, D, E)
    now, flooded = 0.0, {}
    while now < 5000.0:
        heard = 0.0 if now <= 2 * telemetry.HEARD_RECENTLY_S else None  # as the poller keeps it
        candidates = [pathless(k, heard_at=heard) for k in keys]
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(decision.key, now + 9)
        flooded[decision.key] = now
        now += 9
    assert set(flooded) == set(keys)
    times = sorted(flooded.values())
    assert times[3] > telemetry.HEARD_RECENTLY_S
    assert times[4] - times[0] >= HOUR


def test_a_companion_heard_while_the_hourly_limit_holds_is_planned_for_it() -> None:
    """At a fast preset (62.5 kHz, SF7, CR5) four floods fill the hour within 23 minutes; a fifth
    companion heard after them waits for the hour, it is not lost when its hearing gets older
    than 30 minutes meanwhile."""
    fast = meshcore_lora(62_500.0, 7, 5)
    s = Scheduler(0.0)
    keys = (A, B, C, D, E)
    now, flooded = 0.0, {}
    while now < 5000.0:
        candidates = [pathless(k, heard_at=0.0 if now <= HOUR else None) for k in keys[:4]]
        heard = 1400.0 if 1400.0 <= now <= 1400.0 + HOUR else None  # as the poller keeps it
        candidates.append(pathless(E, heard_at=heard))
        decision = s.decide(fast, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(decision.key, now + 9)
        flooded[decision.key] = now
        now += 9
    assert set(flooded) == set(keys)
    assert flooded[E] >= min(flooded.values()) + HOUR


def test_the_flood_count_stops_at_the_limit_the_state_file_takes() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    for i in range(20):
        now = 10.0 + i * 100.0
        p = Poll(A, True, Costs(0.0, 0.0), "flood")
        s.sent(p, flood=True, now=now)
        s.answered(A, now + 1, HOME, 19)
    assert s.export(3000.0).contacts[A].floods == telemetry.MAX_FAILURES


def test_a_debt_survives_a_restart_and_new_radio_settings() -> None:
    """A flooded answer is charged beyond the level: the debt is saved, so a restart does not
    forgive it, and carried over to the lanes of new settings."""
    s = Scheduler(0.0)
    sent = due_at(s, [direct(A)], 0.0, 200.0)
    s.sent(poll(s, [direct(A)], sent), flood=False, now=sent)
    s.flooded_reply(A, sent + 1)  # 11.75 s in the mesh, beyond the level
    s.answered(A, sent + 2, HOME, 19)
    saved = s.export(sent + 2)
    refilled = 90.0 / 3600 * (sent + 2)  # the mesh lane filled while the own lane did
    assert saved.mesh_debt_s == pytest.approx(0.887 + 11.75 - refilled, abs=0.01)
    assert saved.own_debt_s > 0
    restarted = Scheduler(1000.0, saved=saved, elapsed_s=10.0)
    fresh = Scheduler(1000.0)
    late = due_at(restarted, [direct(B)], 1000.0, 3000.0)
    owed = saved.mesh_debt_s - 90.0 / 3600 * 10.0  # paid back while it was stopped
    assert late - 1000.0 == pytest.approx((owed + 0.887) / (90.0 / 3600), abs=0.5)
    assert due_at(fresh, [direct(B)], 1000.0, 3000.0) == pytest.approx(1075.57, abs=0.01)
    # new radio settings carry the debt over
    other = meshcore_lora(125_000.0, 8, 8)
    t = sent + 2
    s.decide(other, [direct(A)], t)
    assert s.export(t).mesh_debt_s == pytest.approx(saved.mesh_debt_s, abs=0.01)


def test_a_departed_contact_keeps_its_schedule_for_a_day_across_restarts() -> None:
    s = Scheduler(0.0, lanes=wide)
    s.decide(EU, [direct(A)], 0.0)
    s.decide(EU, [direct(B)], 100.0)  # A left the table
    saved = s.export(80_000.0)
    assert saved.contacts[A].parked_age_s == pytest.approx(79_900.0)
    assert saved.contacts[B].parked_age_s is None
    r = Scheduler(0.0, saved=saved, elapsed_s=1000.0, lanes=wide)
    r.decide(EU, [direct(B)], 6000.0)  # a day after it left
    assert A not in r.export(6000.0).contacts
    assert B in r.export(6000.0).contacts


# --- review round two: plans, retries, carried debt -----------------------------------------------


def test_a_planned_flood_stays_planned_when_heard_again_or_no_longer() -> None:
    """Hearing the contact again does not restart its plan, and nor does losing the heard time
    (the poller keeps it an hour): four companions heard together are all flooded."""
    s = Scheduler(0.0)
    keys = (A, B, C, D)
    now, flooded = 0.0, {}
    while now < 4000.0:
        heard = now if now < 600.0 else None  # heard again and again, then forgotten
        candidates = [pathless(k, heard_at=heard) for k in keys]
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(decision.key, now + 9)
        flooded[decision.key] = now
        now += 9
    assert set(flooded) == set(keys)


def test_a_contact_that_leaves_loses_its_planned_flood() -> None:
    """Removed (or a look-alike appeared) while its flood waited, and back without being heard
    again: no flood for the old hearing."""
    s = Scheduler(0.0)
    assert isinstance(s.decide(EU, [pathless(A, heard_at=0.0)], 0.0), float)  # planned, waits
    s.decide(EU, [direct(B)], 100.0)  # A left
    assert s.decide(EU, [pathless(A)], 6 * HOUR) == telemetry.STEP_S


@pytest.mark.parametrize("hour_full_first", [True, False])
def test_a_planned_flood_ends_when_the_contacts_own_gap_holds_again(hour_full_first: bool) -> None:
    """Planned while the hourly limit holds, or planned, refused and then held by it, as its last
    answer turns a day old: its gap grows from an hour to a day, the plan ends, and it is not
    flooded later for that hearing, also once the hour has room again."""
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)  # answers at 2 s
    flood = poll(s, [pathless(A, heard_at=1990.0)], 2000.0)  # its path was reset; heard
    s.sent(flood, flood=True, now=2000.0)
    s.unanswered(A, 2009.0)  # its next flood an hour after this one, a day once it is silent
    heard = 86_300.0  # 102 s before the answer is a day old
    others_from = heard - 300.0 if hour_full_first else heard + 10.0  # four floods fill the hour
    refused = False
    now = 85_900.0
    while now < 95_000.0:
        a_heard = heard if heard <= now <= heard + HOUR else None  # as the poller keeps it
        candidates = [pathless(A, heard_at=a_heard)]
        if now >= others_from:
            candidates += [pathless(k, heard_at=others_from) for k in (B, C, D, E)]
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += min(max(decision, 0.001), 60.0)
            continue
        if decision.key == A:
            assert not refused, f"A flooded at {now}"
            assert not hour_full_first, f"A asked at {now} while the hour was full"
            s.retry(A, now, after_s=300.0)  # refused (no free packet buffer, a message)
            refused = True
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(decision.key, now + 9)
        now += 9
    assert refused is not hour_full_first


def test_a_planned_flood_survives_a_retry() -> None:
    """Withdrawn for a message, or ERR 3: the flood planned while the contact was heard goes
    out after the retry, also when the hearing is older than 30 minutes by then."""
    s = Scheduler(0.0, lanes=wide)
    flood = poll(s, [pathless(A, heard_at=0.0)], 1700.0)
    s.retry(A, 1700.0, after_s=200.0)  # no free packet buffer
    assert isinstance(s.decide(EU, [pathless(A, heard_at=0.0)], 1800.0), float)  # not yet
    again = poll(s, [pathless(A, heard_at=0.0)], 1900.0)
    assert (again.key, again.flood) == (flood.key, True)


def test_plans_are_dropped_when_nothing_was_decided_for_a_while() -> None:
    """The radio was away: a flood planned before is not sent for an old hearing."""
    s = Scheduler(0.0)
    assert isinstance(s.decide(EU, [pathless(A, heard_at=0.0)], 0.0), float)  # waits for lanes
    later = telemetry.PLAN_KEEP_S + 1000.0
    assert s.decide(EU, [pathless(A, heard_at=None)], later) == telemetry.STEP_S


def test_a_sign_of_life_does_not_undo_a_retry_delay() -> None:
    """A refusal sets a delay without a request sent: hearing the contact before it must not
    bring it forward again (no tight loop of refused requests)."""
    s = Scheduler(0.0, lanes=wide)
    poll(s, [pathless(A, heard_at=10.0)], 20.0)
    s.retry(A, 20.0, after_s=300.0)  # refused
    assert isinstance(s.decide(EU, [pathless(A, heard_at=10.0)], 21.0), float)
    ask(s, [direct(B)], 30.0)  # resting: due 15 min after
    ask(s, [direct(B)], 930.0, answer=False)  # then silent: retried after 30 s
    ask(s, [direct(B)], 965.0, answer=False)  # backed off to 30 min
    heard = [direct(B, heard_at=1100.0)]  # a sign of life: due 15 min after the last request
    p = poll(s, heard, 1865.0)
    s.retry(p.key, 1865.0, after_s=300.0)  # refused
    assert isinstance(s.decide(EU, heard, 1866.0), float)
    assert poll(s, heard, 2165.0).key == B


def test_a_debt_carried_over_is_paid_back_also_before_the_first_decision() -> None:
    saved = Saved({}, (), own_debt_s=0.5, mesh_debt_s=20.0)
    r = Scheduler(100.0, saved=saved, elapsed_s=200.0)
    exported = r.export(100.0 + 400.0)  # never decided: the radio was not there
    assert exported.mesh_debt_s == pytest.approx(20.0 - 90.0 * 600.0 / 3600)
    assert exported.own_debt_s == pytest.approx(0.0)  # 0.5 s paid back at 18 s/h in 100 s
    again = Scheduler(1000.0, saved=exported, elapsed_s=0.0)
    first = due_at(again, [direct(A)], 1000.0, 3000.0)
    assert first - 1000.0 == pytest.approx((exported.mesh_debt_s + 0.887) / (90.0 / 3600), abs=0.5)


# --- states and expiry (slice 6b, ADR 0024) ------------------------------------------------------


def current_s(s: Scheduler, key: str, now: float) -> float:
    return s.export(now).contacts[key].current_s


def slow(capacity: Costs, now: float) -> tuple[AirtimeBudget, AirtimeBudget]:
    """Lanes that refill so slowly that a planned flood keeps waiting."""
    return (
        AirtimeBudget(0.01, capacity.own_s, now, tokens=0.0),
        AirtimeBudget(0.01, capacity.mesh_s, now, tokens=0.0),
    )


def test_state_answering_then_no_position_after_an_hour_without_a_position() -> None:
    s = Scheduler(0.0, lanes=wide)
    assert s.state(A, 0.0) is None
    ask(s, [direct(A)], 0.0)  # answers at 2 s with a position
    assert s.state(A, 2.0) == "answering"
    ask(s, [direct(A)], 900.0, cells=None)  # GPS off: the next answer has none
    assert s.state(A, 902.0) == "answering"  # one within the hour carried a position
    assert s.state(A, 2.0 + telemetry.LOCATED_S) == "answering"
    assert s.state(A, 2.0 + telemetry.LOCATED_S + 1) == "no_position"
    ask(s, [direct(A)], 4500.0)  # a position again
    assert s.state(A, 4502.0) == "answering"


def test_one_failure_keeps_the_state_two_make_it_away() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    ask(s, [direct(A)], 900.0, answer=False)
    assert s.failures(A) == 1
    assert s.state(A, 905.0) == "answering"  # a single lost answer changes nothing
    ask(s, [direct(A)], 935.0, answer=False)  # the retry 30 s later
    assert s.failures(A) == 2
    assert s.state(A, 940.0) == "away"
    assert s.failures(B) == 0  # not known


def test_away_without_requests_after_the_time_bound() -> None:
    """The radio away: no decision, no request; the answers expire by the time bound alone."""
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)  # resting neighbour: 3 x 900 s is less than an hour
    bound = 2.0 + telemetry.CURRENT_MIN_S
    assert s.state(A, bound) == "answering"
    assert s.state(A, bound + 1) == "away"
    assert s.answered_within_day(A, 2.0 + DAY)
    assert s.state(A, 2.0 + DAY + 1) == "waiting"  # not asked since an answer over a day old
    assert not s.answered_within_day(A, 2.0 + DAY + 1)
    assert not s.answered_within_day(B, 2.0)


def test_current_s_is_three_intervals_or_an_hour() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)  # resting neighbour: 900 s
    assert current_s(s, A, 2.0) == telemetry.CURRENT_MIN_S
    ask(s, [direct(A)], 900.0, cells=(HOME[0] + 5, HOME[1]))  # moving: 90 s
    assert s.interval_s(A, 902.0) == 90.0
    assert current_s(s, A, 902.0) == telemetry.CURRENT_MIN_S
    ask(s, [direct(A)], 990.0, cells=None)  # no position: once an hour
    assert current_s(s, A, 992.0) == 3 * 3600.0
    s2 = Scheduler(0.0, lanes=wide)
    ask(s2, [direct(B, hops=2)], 0.0)  # resting behind two repeaters: about 47 min
    interval = s2.interval_s(B, 2.0)
    assert interval is not None
    assert interval == pytest.approx(2791.0, abs=1)
    assert current_s(s2, B, 2.0) == telemetry.CURRENT_INTERVALS * interval
    assert s2.state(B, 2.0 + 3 * interval - 1) == "answering"
    assert s2.state(B, 2.0 + 3 * interval + 1) == "away"


def test_no_answer_after_a_day_and_waiting_before_any_request() -> None:
    s = Scheduler(0.0, lanes=wide)
    p = poll(s, [direct(A)], 0.0)
    assert s.state(A, 0.0) == "waiting"
    s.sent(p, flood=False, now=0.0)
    assert s.state(A, 1.0) == "waiting"  # in flight
    s.unanswered(A, 5.0)
    assert s.state(A, 5.0) == "no_answer"  # never answered: not away
    s2 = Scheduler(0.0, lanes=wide)
    ask(s2, [direct(A)], 0.0)
    ask(s2, [direct(A)], 900.0, answer=False)
    ask(s2, [direct(A)], 935.0, answer=False)
    assert s2.state(A, 2.0 + DAY) == "away"
    assert s2.state(A, 2.0 + DAY + 1) == "no_answer"


def test_no_path_for_a_candidate_without_a_path_not_heard_lately() -> None:
    s = Scheduler(0.0, lanes=wide)
    assert s.decide(EU, [pathless(A)], 0.0) == telemetry.STEP_S
    assert s.state(A, 0.0) == "no_path"
    # flooded (a disconnect cut it: nothing learned), heard again within its flood gap of a day
    s2 = Scheduler(0.0, lanes=wide)
    s2.sent(poll(s2, [pathless(A, heard_at=0.0)], 0.0), flood=True, now=0.0)
    s2.retry(A, 9.0)
    assert s2.decide(EU, [pathless(A, heard_at=100.0)], 100.0) == telemetry.STEP_S  # not planned
    assert s2.state(A, 100.0 + 29 * 60) == "waiting"  # heard 29 min ago
    assert s2.state(A, 100.0 + 31 * 60) == "no_path"
    # a flood planned (it waits for the lanes): not no_path, also once the hearing is older
    s3 = Scheduler(0.0, lanes=slow)
    for now in range(0, 2000, 250):
        assert isinstance(s3.decide(EU, [pathless(A, heard_at=0.0)], float(now)), float)
    assert s3.state(A, 1900.0) == "waiting"
    # restored, not yet compared with a table: its path is not known
    r = Scheduler(0.0, saved=Saved({A: SavedContact(0.0)}), lanes=wide)
    assert r.state(A, 0.0) == "waiting"
    # answered within the day: away first
    s4 = Scheduler(0.0, lanes=wide)
    ask(s4, [direct(A)], 0.0)
    s4.decide(EU, [pathless(A)], 7200.0)  # its path was reset
    assert s4.state(A, 7200.0) == "away"


def test_state_of_restored_and_parked_contacts() -> None:
    saved = Saved({
        A: SavedContact(0.0, answered_age_s=100.0, located=True),
        B: SavedContact(0.0, failures=2, answered_age_s=7200.0, parked_age_s=50.0),
    })  # fmt: skip
    r = Scheduler(1000.0, saved=saved, elapsed_s=10.0, lanes=wide)
    assert r.state(A, 1000.0) == "answering"  # restored, not yet compared with a table
    assert r.state(B, 1000.0) == "away"  # parked
    assert r.failures(B) == 2
    assert r.answered_within_day(B, 1000.0)
    assert r.state(C, 1000.0) is None
    r.decide(EU, [direct(C)], 1000.0)  # A left: parked, its answer expires all the same
    answered = 1000.0 - 110.0
    assert r.state(A, answered + telemetry.CURRENT_MIN_S) == "answering"
    assert r.state(A, answered + telemetry.CURRENT_MIN_S + 1) == "away"


def test_answered_returns_true_for_the_first_answer_and_after_two_failures() -> None:
    s = Scheduler(0.0, lanes=wide)
    s.sent(poll(s, [direct(A)], 0.0), flood=False, now=0.0)
    assert s.answered(A, 2.0, HOME, 19) is True  # its first
    s.sent(poll(s, [direct(A)], 900.0), flood=False, now=900.0)
    assert s.answered(A, 902.0, HOME, 19) is False
    ask(s, [direct(A)], 1800.0, answer=False)
    s.sent(poll(s, [direct(A)], 1835.0), flood=False, now=1835.0)
    assert s.answered(A, 1837.0, HOME, 19) is False  # after one failure
    ask(s, [direct(A)], 2735.0, answer=False)
    ask(s, [direct(A)], 2770.0, answer=False)
    s.sent(poll(s, [direct(A)], 4575.0), flood=False, now=4575.0)
    assert s.answered(A, 4577.0, HOME, 19) is True  # after two
    assert s.answered(B, 4600.0, HOME, 19) is False  # a key it does not know
    r = Scheduler(0.0, saved=Saved({A: SavedContact(0.0, answered_age_s=100.0)}), lanes=wide)
    r.sent(poll(r, [direct(A)], 0.0), flood=False, now=0.0)
    assert r.answered(A, 2.0, HOME, 19) is False  # it answered before the restart
    r.forget(A, 10.0)
    r.sent(poll(r, [direct(A)], 20.0), flood=False, now=20.0)
    assert r.answered(A, 22.0, HOME, 19) is True  # its first since it was included again


def test_forget_drops_the_answers_and_keeps_the_flood_limits() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(A)], 0.0)
    flood = poll(s, [pathless(A, heard_at=900.0)], 900.0)  # its path was reset; heard
    s.sent(flood, flood=True, now=900.0)
    s.answered(A, 903.0, HOME, 19, route=(1, 1))
    version = s.version
    s.forget(A, 963.0)  # excluded a minute after the answer
    assert s.version != version  # saved with the next save
    assert s.state(A, 963.0) == "waiting"
    assert s.failures(A) == 0
    assert not s.answered_within_day(A, 963.0)
    assert s.located_ages(963.0) == {}
    assert s.export(963.0).contacts[A] == SavedContact(
        0.0, polled_age_s=63.0, flood_age_s=63.0, floods=1
    )
    # a candidate again: due at once, and its flood gap of a day holds
    assert s.decide(EU, [pathless(A, heard_at=1000.0)], 1000.0) == telemetry.STEP_S
    assert poll(s, [direct(A, hops=1)], 1000.0).key == A
    # parked
    p = Scheduler(0.0, lanes=wide)
    ask(p, [direct(A)], 0.0)
    p.decide(EU, [direct(B)], 10.0)  # A left
    p.forget(A, 60.0)
    assert p.state(A, 60.0) == "waiting"
    assert p.export(60.0).contacts[A].parked_age_s == 50.0  # still parked since it left
    assert poll(p, [direct(A)], 100.0).key == A  # back: due at once, not at 900 s
    # restored, not yet compared with a table
    saved = Saved({A: SavedContact(
        500.0, answered_age_s=10.0, polled_age_s=12.0, flood_age_s=20.0, floods=2, located=True,
        current_s=7200.0, located_age_s=10.0,
    )})  # fmt: skip
    r = Scheduler(0.0, saved=saved, lanes=wide)
    r.forget(A, 0.0)
    assert r.state(A, 0.0) == "waiting"
    assert r.export(0.0).contacts[A] == SavedContact(
        0.0, polled_age_s=12.0, flood_age_s=20.0, floods=2
    )
    assert poll(r, [direct(A)], 0.0).key == A
    # the outcome of a request in flight still clears it
    f = Scheduler(0.0, lanes=wide)
    f.sent(poll(f, [direct(A)], 0.0), flood=False, now=0.0)
    f.forget(A, 1.0)
    assert f.in_flight == A
    f.retry(A, 2.0)
    assert [f.in_flight] == [None]
    f.forget(B, 3.0)  # not known: nothing to forget


def test_current_s_and_located_age_are_saved_and_restored() -> None:
    s = Scheduler(0.0, lanes=wide)
    ask(s, [direct(B, hops=2)], 0.0)  # answers at 2 s with a position
    bound = current_s(s, B, 2.0)
    assert bound == pytest.approx(3 * 2791.0, abs=3)
    saved = s.export(102.0)
    assert saved.contacts[B].located_age_s == 100.0
    assert saved.contacts[B].current_s == bound
    r = Scheduler(1000.0, saved=saved, elapsed_s=50.0, lanes=wide)
    assert r.located_ages(1000.0) == {B: 150.0}
    assert r.state(B, 1000.0) == "answering"
    answered = 1000.0 - 150.0
    assert r.state(B, answered + bound - 1) == "answering"
    assert r.state(B, answered + bound + 1) == "away"
    assert r.export(1000.0).contacts[B].current_s == bound
    assert r.export(1000.0).contacts[B].located_age_s == 150.0
    # how long it was stopped is unknown (the clock went back): not current, no located time
    unknown = Scheduler(1000.0, saved=saved, elapsed_s=None, lanes=wide)
    assert unknown.state(B, 1000.0) == "waiting"
    assert unknown.located_ages(1000.0) == {}
    assert unknown.export(1000.0).contacts[B].located_age_s is None


def test_located_ages() -> None:
    s = Scheduler(0.0, lanes=wide)
    assert s.located_ages(0.0) == {}
    ask(s, [direct(A), direct(B)], 0.0)  # A, with a position
    ask(s, [direct(A), direct(B)], 12.0, cells=None)  # B, without
    assert s.located_ages(100.0) == {A: 98.0}
    ask(s, [direct(A), direct(B)], 900.0, cells=None)  # A without: its last one stays
    assert s.located_ages(1000.0) == {A: 998.0}
    s.decide(EU, [direct(B)], 1000.0)  # A left: parked, still counted
    assert s.located_ages(1100.0) == {A: 1098.0}


def test_answered_ages() -> None:
    s = Scheduler(0.0, lanes=wide)
    assert s.answered_ages(0.0) == {}
    ask(s, [direct(A), direct(B)], 0.0)  # A
    ask(s, [direct(A), direct(B)], 12.0, cells=None)  # B, also without a position
    assert s.answered_ages(100.0) == {A: 98.0, B: 86.0}
    s.decide(EU, [direct(B)], 1000.0)  # A left: parked, still counted
    s.forget(B, 1000.0)  # excluded
    assert s.answered_ages(1100.0) == {A: 1098.0}


def test_a_file_without_the_new_fields_reads_as_an_hour_and_no_located_time(
    tmp_path: Path,
) -> None:
    """A schedule saved by 1.1.0a1: its answers count an hour, its positions as the answer
    said (`located`), never longer."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"telemetry": {
        "version": 1, "saved_at": 1000.0,
        "contacts": {
            A: {"due_in_s": 600.0, "answered_age_s": 100.0, "located": True},
            B: {"due_in_s": 600.0, "answered_age_s": 100.0, "located": False},
        },
    }})  # fmt: skip
    saved, elapsed = TelemetryRecords(store).load(1000.0)
    assert saved is not None
    assert saved.contacts[A].current_s == telemetry.CURRENT_MIN_S
    assert saved.contacts[A].located_age_s is None
    assert saved.hold_left_s is None
    r = Scheduler(0.0, saved=saved, elapsed_s=elapsed, lanes=wide)
    assert r.located_ages(0.0) == {}
    assert r.state(A, 0.0) == "answering"
    assert r.state(B, 0.0) == "no_position"
    assert r.state(A, telemetry.CURRENT_MIN_S - 100.0) == "answering"
    assert r.state(A, telemetry.CURRENT_MIN_S - 99.0) == "away"


def test_the_lists_are_the_literal_types() -> None:
    assert telemetry.STATES == get_args(TelemetryState) == (
        "waiting", "answering", "no_position", "away", "no_answer", "no_path", "look_alike",
        "excluded", "off",
    )  # fmt: skip
    assert telemetry.STATUSES == get_args(TelemetryStatus) == ("off", "on", "paused", "unsupported")
    assert telemetry.PAUSE_REASONS == get_args(PauseReason) == (
        "first_start", "no_radio", "exclusions_unreadable", "auto_add", "radio_airtime",
    )  # fmt: skip
    assert telemetry.EVENTS == get_args(TelemetryEvent) == ("answered", "unanswered")
    assert {"answering", "no_position"} == telemetry.CURRENT
    assert set(telemetry.EVENT_STATES) <= set(telemetry.STATES)
    assert set(telemetry.EVENT_STATES) >= telemetry.CURRENT
