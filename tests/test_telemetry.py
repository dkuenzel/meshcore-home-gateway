"""The telemetry schedule (core/telemetry.py, ADR 0018): pure, on a clock the tests set."""

import itertools
from collections.abc import Callable, Sequence

import pytest

from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.airtime import Costs, meshcore_lora
from meshcore_home_gateway.core.budget import AirtimeBudget
from meshcore_home_gateway.core.telemetry import (
    Candidate,
    Poll,
    Saved,
    SavedContact,
    Scheduler,
    answer_wait_s,
)

EU = meshcore_lora(62_500.0, 8, 8)  # EU/UK Narrow
A, B, C, D, E = ("a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32, "e5" * 32)
HOME = (520_000, 100_000)  # cells of 1e-4 degrees: one cell of latitude is 11.1 m
HOUR = 3600.0


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


def test_a_due_flood_does_not_wait_forever_behind_direct_polls() -> None:
    """Two moving companions saturate the own lane; a flood needs both lanes full. After 10
    minutes the flood goes first and waits for them: at most 164 s for the own lane, 816 s for
    the mesh lane, from empty."""
    saved = Saved({
        k: SavedContact(0.0, answered_age_s=10.0, polled_age_s=10.0, located=True, still=0)
        for k in (A, B)
    })  # fmt: skip
    s = Scheduler(0.0, saved=saved)
    now, polls, step = 0.0, [], 0
    while now < 1500.0:
        candidates = [direct(A), direct(B), pathless(C, heard_at=now)]
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=decision.flood, now=now)
        step += 1
        s.answered(decision.key, now + 1, (HOME[0] + 10 * (step % 2), HOME[1]), 19)
        polls.append((now, decision.key))
        now += 1
    flood_at = next(t for t, k in polls if k == C)
    assert 600.0 <= flood_at <= 600.0 + 817.0 + 15.0
    assert len([t for t, k in polls if k != C and t < 600.0]) >= 6  # direct polls went on


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
        800.0, answered_age_s=98.0, polled_age_s=100.0, located=True, lpp_bytes=19
    )
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


def test_a_restored_contact_that_never_comes_back_is_dropped_after_a_day() -> None:
    saved = Saved({A: SavedContact(0.0)})
    r = Scheduler(0.0, saved=saved, lanes=wide)
    assert A in r.export(10.0).contacts
    r.decide(EU, [direct(B)], 90_000.0)
    assert A not in r.export(90_000.0).contacts


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
    """Four companions heard together, each flood needing both lanes full (13.6 min at EU/UK
    Narrow): all four go out, also those that wait longer than 30 min."""
    s = Scheduler(0.0)
    keys = (A, B, C, D)
    candidates = [pathless(k, heard_at=0.0) for k in keys]
    now, flooded = 0.0, {}
    while now < 4000.0:
        decision = s.decide(EU, candidates, now)
        if isinstance(decision, float):
            now += max(decision, 0.001)
            continue
        s.sent(decision, flood=True, now=now)
        s.unanswered(decision.key, now + 9)
        flooded[decision.key] = now
        now += 9
    assert set(flooded) == set(keys)
    assert max(flooded.values()) > telemetry.HEARD_RECENTLY_S


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
