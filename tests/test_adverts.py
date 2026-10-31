"""The check for positions in adverts (core/adverts.py): which logged advert counts, and what a
table read shows about a companion's newest advert (ADR 0018)."""

import random

from meshcore_home_gateway.core.adverts import (
    LOGGED_S,
    MAX_LOGGED,
    WAIT_S,
    Logged,
    Outcome,
    Pushed,
    adjacent,
    candidates,
    fresh,
    from_record,
    heard,
    outcome,
)
from meshcore_home_gateway.core.mesh import AdvertCheck, Node, Signal
from meshcore_home_gateway.core.model import Position

KEY, OTHER = "a1" * 32, "b2" * 32
T = 1_800_000_000.0  # the gateway's clock at the push
TA = 1_799_999_000  # Alice's clock at her newest advert
HOME = Position(53.55, 9.99)
FIELD = Position(53.56, 10.01)
SERIAL = 7  # the session's number


def log(
    seq: int, timestamp: int = TA, position: Position | None = HOME, *, carries: bool = True,
    at: float = T, key: str = KEY, serial: int = SERIAL, hops: int = 0,
) -> Logged:  # fmt: skip
    """An advert as the radio logged it, frame `seq` of the session; `position` None with the
    flag: 0, 0."""
    return Logged(
        key, at, (serial, seq), timestamp, carries, position if carries else None,
        Signal(hops, 6.0, -96),
    )  # fmt: skip


def record(last_advert: int = TA, position: Position | None = HOME) -> Node:
    """Alice's record as a table read shows it."""
    return Node(KEY, "Alice", 1, last_advert=last_advert, position=position)


def push(logs: list[Logged], seq: int, *, at: float = T, no_delay: bool = True) -> Pushed:
    """The radio's push for Alice as frame `seq` after `logs` (in frame order), as the observer
    keeps it: the adjacent log only while the radio's receive delay is 0."""
    near = adjacent(logs[-1] if logs else None, KEY, (SERIAL, seq)) if no_delay else None
    return Pushed(at, near, candidates(logs, KEY, at))


def test_the_adjacent_log_with_the_records_time_decides() -> None:
    flagged = push([log(10)], 11)
    assert flagged.adjacent == log(10)
    assert outcome(flagged, record(), record(TA - 600), T + 2) == Outcome(AdvertCheck(TA, True))
    unflagged = push([log(10, carries=False)], 11)
    # the record keeps the position of an earlier advert (BaseChatMesh.cpp:193-196)
    assert outcome(unflagged, record(), record(TA - 600), T + 2) == Outcome(AdvertCheck(TA, False))
    never = push([log(10, position=None)], 11)  # the flag with 0, 0: it never had a position
    zeroed = record(position=None)
    assert outcome(never, zeroed, None, T + 2) == Outcome(AdvertCheck(TA, True))


def test_a_log_whose_time_the_record_does_not_show_is_not_trusted() -> None:
    """The record's `last_advert` ties a log to the advert the radio took: a log of another
    advert (a replay, a late copy, a forgery) is not it."""
    older = push([log(10, TA - 60)], 11)
    assert older.adjacent is not None
    assert outcome(older, record(), None, T + 2) == Outcome(problem="not_logged")
    window = push([log(10, TA - 60)], 11, no_delay=False)
    assert outcome(window, record(), None, T + 2) == Outcome(problem="not_logged")


def test_a_forged_copy_before_the_real_log_does_not_decide() -> None:
    """While the receive delay is 0 the radio logs and handles a packet in one pass
    (Dispatcher.cpp:191-256): a forged copy with the same time is logged in another one and is
    never the frame directly before the push (design D2)."""
    forged, real = log(10, carries=False), log(11)
    assert outcome(push([forged, real], 12), record(), None, T + 2) == Outcome(
        AdvertCheck(TA, True), problem="copies_differ"
    )
    forged, real = log(10, position=FIELD), log(11, carries=False)
    assert outcome(push([forged, real], 12), record(), None, T + 2) == Outcome(
        AdvertCheck(TA, False), problem="copies_differ"
    )


def test_a_late_copy_of_an_older_advert_is_never_adjacent() -> None:
    """Each flood copy is logged and only the first is handled: a late copy of Alice's advert
    comes after its push. At her next advert its own log is the frame before the push and gives
    the signal (design D13), not the late copy."""
    first = log(10, TA - 60, at=T - 30, hops=1)
    late = log(12, TA - 60, at=T - 29, hops=3)  # frame 11 was the push of the first
    newest = log(13, carries=False, hops=0)
    pushed = push([first, late, newest], 14)
    assert pushed.adjacent == newest
    assert pushed.adjacent.signal == Signal(0, 6.0, -96)
    assert outcome(pushed, record(), None, T + 2) == Outcome(AdvertCheck(TA, False))
    lost = push([first, late], 13)  # her newest log was too long for a frame: no log decides
    assert outcome(lost, record(), None, T + 2) == Outcome(problem="not_logged")


def test_without_an_adjacent_log_all_copies_with_the_time_must_agree() -> None:
    """A receive delay above 0 (or one not known) lets other frames come between an advert's log
    and its push: every log of the key with the record's time must say the same. The signal does
    not count."""
    copies = [log(10, at=T - 20, hops=1), log(12, at=T - 19, hops=2), log(13, at=T - 18, hops=3)]
    between = push(copies, 15)  # a frame of something else before the push
    assert between.adjacent is None
    assert outcome(between, record(), None, T + 2) == Outcome(AdvertCheck(TA, True))
    delayed = push(copies, 14, no_delay=False)
    assert delayed.adjacent is None
    assert outcome(delayed, record(), None, T + 2) == Outcome(AdvertCheck(TA, True))
    forged = log(13, carries=False, at=T - 18)
    differ = push([copies[0], copies[1], forged], 14, no_delay=False)
    assert outcome(differ, record(), None, T + 2) == Outcome(problem="copies_differ")
    early = log(9, carries=False, at=T - 21)
    differ = push([early, copies[0], copies[1]], 14, no_delay=False)
    assert outcome(differ, record(), None, T + 2) == Outcome(problem="copies_differ")


def test_frames_of_another_session_are_never_adjacent() -> None:
    last = log(10)
    assert adjacent(last, KEY, (SERIAL, 11)) == last
    assert adjacent(last, KEY, (SERIAL + 1, 11)) is None  # a session numbers its frames anew
    assert adjacent(last, KEY, (SERIAL, 12)) is None
    assert adjacent(last, KEY, (SERIAL, 10)) is None
    assert adjacent(last, OTHER, (SERIAL, 11)) is None
    assert adjacent(None, KEY, (SERIAL, 11)) is None
    assert adjacent(log(10, serial=0), KEY, (0, 11)) is None  # not from a session (tests)


def test_a_flagged_log_that_is_not_the_records_position_decides_nothing() -> None:
    """The radio copies the accepted advert's lat/lon into the record (BaseChatMesh.cpp:193-196):
    a flagged log with another position is not that advert."""
    elsewhere = log(10, position=FIELD)
    assert outcome(push([elsewhere], 11), record(), None, T + 2) == Outcome(
        problem="record_differs"
    )
    window = push([elsewhere], 11, no_delay=False)
    assert outcome(window, record(), None, T + 2) == Outcome(problem="record_differs")
    zeroes = push([log(10, position=None)], 11)  # 0, 0 would have zeroed the record
    assert outcome(zeroes, record(), None, T + 2) == Outcome(problem="record_differs")
    unflagged = push([log(10, carries=False)], 11)  # leaves the record as it was
    assert outcome(unflagged, record(position=FIELD), None, T + 2) == Outcome(
        AdvertCheck(TA, False)
    )


def test_a_read_older_than_the_push_waits_then_gives_up() -> None:
    """A table read that started before the radio took the advert shows the record before it;
    the session reads again (radio/session.py, refresh_contacts)."""
    pushed = push([log(10)], 11)
    stale = record(TA - 60)
    assert outcome(pushed, stale, None, T + 1) == Outcome(wait=True)
    assert outcome(pushed, stale, None, T + WAIT_S) == Outcome(wait=True)
    assert outcome(pushed, stale, None, T + WAIT_S + 1) == Outcome(problem="not_logged")
    assert outcome(pushed, record(), stale, T + 3) == Outcome(AdvertCheck(TA, True))  # the next
    # a new position in the stale record is a check of its own meanwhile
    moved = outcome(pushed, stale, record(TA - 120, None), T + 1)
    assert moved == Outcome(AdvertCheck(TA - 60, True), wait=True)
    older = push([log(10, TA - 120)], 11)  # no log newer than the record: nothing to wait for
    assert outcome(older, stale, None, T + 1) == Outcome(problem="not_logged")
    back = push([log(10, at=T + 1)], 11)  # the wall clock went back between the log and the push
    assert (back.adjacent is not None, back.logged) == (True, ())
    assert outcome(back, stale, None, T + 1) == Outcome(wait=True)


def test_no_log_decides_nothing_but_a_new_record_position_does() -> None:
    lost = push([], 11)  # too long for a log frame
    assert outcome(lost, record(), record(TA - 60), T + 2) == Outcome(problem="not_logged")
    moved = record(TA - 60, FIELD)
    assert outcome(lost, record(), moved, T + 2) == Outcome(
        AdvertCheck(TA, True), problem="not_logged"
    )
    # no push: the radio's report was dropped (TCP), or the advert came while the gateway was away
    assert outcome(None, record(), moved, T + 2) == Outcome(AdvertCheck(TA, True))
    assert outcome(None, record(), record(TA - 60), T + 2) == Outcome()
    assert outcome(None, record(), None, T + 2) == Outcome()  # a run's first read


def test_a_new_record_position_needs_a_newer_advert() -> None:
    """Only an accepted advert with the flag changes a contact's lat/lon (BaseChatMesh.cpp:
    193-196), or a client's write of the record."""
    assert from_record(record(), record(TA, FIELD)) is None  # the same advert: a write
    assert from_record(record(TA - 60, FIELD), record()) is None  # an older one
    assert from_record(record(), None) is None  # a run's first read: nothing to compare
    assert from_record(record(), record(TA - 60)) is None  # the same position
    assert from_record(record(), record(TA - 60, FIELD)) == AdvertCheck(TA, True)
    assert from_record(record(), record(TA - 60, None)) == AdvertCheck(TA, True)
    assert from_record(record(position=None), record(TA - 60)) == AdvertCheck(TA, True)  # 0, 0


def test_a_new_record_position_wins_over_a_log_without_the_flag() -> None:
    """Two adverts between two reads, the first with the flag: the record took its position, the
    newest log has none. It errs to warn (design D3)."""
    pushed = push([log(10, carries=False)], 11)
    assert outcome(pushed, record(), record(TA - 60, FIELD), T + 2) == Outcome(
        AdvertCheck(TA, True)
    )
    assert outcome(pushed, record(), record(TA - 60), T + 2) == Outcome(AdvertCheck(TA, False))
    differ = push([log(9, position=FIELD), log(10)], 11, no_delay=False)
    assert outcome(differ, record(), record(TA - 60, FIELD), T + 2) == Outcome(
        AdvertCheck(TA, True), problem="copies_differ"
    )
    elsewhere = push([log(10, position=FIELD)], 11)
    assert outcome(elsewhere, record(), record(TA - 60, FIELD), T + 2) == Outcome(
        AdvertCheck(TA, True), problem="record_differs"
    )
    forged = push([log(10, position=FIELD), log(11, carries=False)], 12)  # the adjacent decides
    assert outcome(forged, record(), record(TA - 60, FIELD), T + 2) == Outcome(
        AdvertCheck(TA, True), problem="copies_differ"
    )


def test_candidates_are_the_keys_logs_within_the_window() -> None:
    """A flood is handled at most 32 s after its log (Dispatcher.cpp:11): LOGGED_S covers it."""
    logs = [
        log(1, at=T - LOGGED_S - 0.5), log(2, at=T - LOGGED_S), log(3, at=T - 5, key=OTHER),
        log(4, at=T), log(6, at=T + 1),
    ]  # fmt: skip
    assert candidates(logs, KEY, T) == (logs[1], logs[3])
    assert LOGGED_S == 40.0


def test_outcome_is_total() -> None:
    """Whatever the radio logged, pushed and recorded, in any order: an outcome, no exception."""
    rng = random.Random(6)  # noqa: S311 - test data
    times, places = (0, TA - 60, TA, TA + 60), (None, HOME, FIELD)

    def any_log(seq: int) -> Logged:
        return Logged(
            rng.choice((KEY, OTHER)), T - rng.uniform(-5, 50), (rng.randrange(3), seq),
            rng.choice(times), rng.random() < 0.5, rng.choice(places), Signal(),
        )  # fmt: skip

    def any_node() -> Node:
        return Node(
            KEY, "Alice", rng.choice((1, 2)), last_advert=rng.choice(times),
            position=rng.choice(places),
        )  # fmt: skip

    for _ in range(20_000):
        logs = [any_log(seq) for seq in range(rng.randrange(5))]
        near = adjacent(logs[-1] if logs else None, KEY, (rng.randrange(3), rng.randrange(6)))
        pushed = Pushed(T + rng.uniform(-5, 5), near, candidates(logs, KEY, T))
        node = any_node()
        result = outcome(rng.choice((pushed, None)), node, rng.choice((any_node(), None)),
                         T + rng.uniform(-10, 80))  # fmt: skip
        assert result.check is None or result.check.at == node.last_advert
        assert result.problem in (None, "not_logged", "copies_differ", "record_differs")
        found = heard(pushed, node)
        assert not found.wait
        assert found.check is None or found.check.at == node.last_advert


def test_a_heard_record_with_a_position_is_a_check_that_carries() -> None:
    """NEW_ADVERT carries the record the radio made from this advert alone, lat/lon only with the
    flag (BaseChatMesh.cpp:108-120): a position in it is evidence on its own (design D27)."""
    assert heard(push([], 11), record()) == Outcome(AdvertCheck(TA, True))
    unflagged = push([log(10, carries=False)], 11)  # a forged log: the radio took the flag
    assert heard(unflagged, record()) == Outcome(AdvertCheck(TA, True))


def test_a_heard_advert_is_checked_by_its_adjacent_log() -> None:
    bare = record(position=None)
    assert heard(push([log(10, position=None)], 11), bare) == Outcome(AdvertCheck(TA, True))
    assert heard(push([log(10, carries=False)], 11), bare) == Outcome(AdvertCheck(TA, False))
    assert heard(push([log(10)], 11), bare) == Outcome(problem="record_differs")
    window = push([log(9, carries=False), log(10, carries=False)], 12)  # a frame between
    assert heard(window, bare) == Outcome(AdvertCheck(TA, False))
    newer = push([log(10, TA + 60)], 11)  # the record is this advert's own: nothing to wait for
    assert heard(newer, bare) == Outcome(problem="not_logged")
    assert heard(push([], 11), bare) == Outcome(problem="not_logged")


def test_fresh_drops_old_and_future_entries() -> None:
    """A logged position is held only while it can still decide (design §3.1)."""
    pending = LOGGED_S + WAIT_S
    assert fresh(T, T, LOGGED_S)
    assert fresh(T, T + 40, LOGGED_S)
    assert not fresh(T, T + 40.5, LOGGED_S)
    assert fresh(T, T + 70, pending)
    assert not fresh(T, T + 70.5, pending)
    assert not fresh(T, T - 1, LOGGED_S)  # the wall clock went back
    assert MAX_LOGGED == 64
