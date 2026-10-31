"""The check for positions in adverts (ADR 0018): does a companion's newest advert carry its
position, which everyone in range can read?

Pure. Fed with the adverts the radio logged (unverified: logged before the radio checks the
signature, src/Dispatcher.cpp:199 at companion-v1.18.0), the radio's push for a contact (its
key only, examples/companion_radio/MyMesh.cpp:358-366) and the record of the next table read,
whose `last_advert` is the timestamp of the newest advert the radio accepted
(src/helpers/BaseChatMesh.cpp:197). While the radio's receive delay is 0, the log that is the
frame directly before the push is that advert's own: the radio logs and handles a packet in one
pass (src/Dispatcher.cpp:191-256); with a delay above 0 a delayed advert is handled at the start
of a later pass (:138-145), so the caller passes no adjacent log then. Without it, every log of
the key with the record's timestamp must agree. A flagged log must hold the position the record
took (BaseChatMesh.cpp:193-196). A new position in the record since the read before is evidence
on its own: only an accepted advert with the flag changes it (or a client's write). A node the
radio did not add comes with its record in the push (NEW_ADVERT, BaseChatMesh.cpp:108-120,
156-179): the same rules apply at once. The caller drops a log after LOGGED_S and a pending
advert after LOGGED_S + WAIT_S (`fresh`); a logged position is compared, never stored.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from meshcore_home_gateway.core.mesh import AdvertCheck, Node, Signal
from meshcore_home_gateway.core.model import Position

LOGGED_S = 40.0  # a flood is handled at most 32 s after its log (src/Dispatcher.cpp:11)
WAIT_S = 30.0  # a pushed advert waits this long for a table read that shows it
MAX_LOGGED = 64  # logged adverts kept, of any key: forged ones push others out, never decide

FrameId = tuple[int, int]  # (session serial, frame number); (0, 0): not from a session


@dataclass(frozen=True, slots=True)
class Logged:
    """An advert as the radio logged it (unverified)."""

    key: str  # 64 lower-case hex digits
    at: float  # the gateway's clock
    frame: FrameId
    timestamp: int  # the sender's clock
    carries: bool  # app data flag 0x10, also with 0, 0
    position: Position | None  # None also for 0, 0; compared with the record only
    signal: Signal


@dataclass(frozen=True, slots=True)
class Pushed:
    """The radio accepted an advert of this contact (its push)."""

    at: float
    # the frame directly before the push in the same session; None also while the radio's
    # receive delay is not known to be 0 (design 6c D2)
    adjacent: Logged | None
    logged: tuple[Logged, ...]  # the key's logs within LOGGED_S before the push


@dataclass(frozen=True, slots=True)
class Outcome:
    check: AdvertCheck | None = None  # None: nothing learned, the last check stays
    wait: bool = False  # the read is older than the logged advert: keep it for the next read
    problem: str | None = None  # "not_logged", "copies_differ", "record_differs" (log lines)


def adjacent(last: Logged | None, key: str, frame: FrameId) -> Logged | None:
    """The newest logged advert, if it is of `key` and the frame directly before `frame` in the
    same session."""
    if last is None or last.key != key or frame[0] == 0:
        return None
    return last if last.frame == (frame[0], frame[1] - 1) else None


def candidates(logged: Iterable[Logged], key: str, at: float) -> tuple[Logged, ...]:
    """The logs of `key` received within LOGGED_S up to `at`, in order."""
    return tuple(entry for entry in logged if entry.key == key and 0 <= at - entry.at <= LOGGED_S)


def fresh(at: float, now: float, limit: float) -> bool:
    """Whether a log or a pending advert received at `at` may still be held: at most `limit`
    seconds old (LOGGED_S for a log, LOGGED_S + WAIT_S for a pending advert); one from the future
    (the wall clock went back) is dropped too."""
    return 0 <= now - at <= limit


def from_record(node: Node, previous: Node | None) -> AdvertCheck | None:
    """A new position in the record since the read before, with a newer advert: an advert since
    then had the flag (a record that turned to none: 0, 0). None at a run's first read."""
    if previous is None or node.last_advert <= previous.last_advert:
        return None
    if node.position == previous.position:
        return None
    return AdvertCheck(node.last_advert, True)


def _content(entry: Logged) -> tuple[bool, Position | None]:
    return entry.carries, entry.position


def outcome(  # noqa: PLR0911 - one return per row of design 6c §3.3
    pushed: Pushed | None, node: Node, previous: Node | None, now: float
) -> Outcome:
    """What this table read shows about the companion's newest advert (design 6c §3.3). Total."""
    record = from_record(node, previous)
    if pushed is None:
        return Outcome(record)
    t = node.last_advert
    same = [entry for entry in pushed.logged if entry.timestamp == t]
    problem = None
    if pushed.adjacent is not None and pushed.adjacent.timestamp == t:
        chosen = pushed.adjacent  # receive delay 0: a forged copy is never adjacent (D2)
        if any(_content(entry) != _content(chosen) for entry in same):
            problem = "copies_differ"  # a warning; the adjacent log decides
    elif not same:
        newer = [entry.timestamp > t for entry in (*pushed.logged, pushed.adjacent) if entry]
        if any(newer) and now - pushed.at <= WAIT_S:  # the read started before the radio took it
            return Outcome(record, wait=True)
        return Outcome(record, problem="not_logged")
    elif len({_content(entry) for entry in same}) > 1:  # copies of one advert are equal
        return Outcome(record, problem="copies_differ")
    else:
        chosen = same[0]
    if chosen.carries and chosen.position != node.position:  # the radio copies its values
        return Outcome(record, problem="record_differs")
    if record is not None and not chosen.carries:  # a new position in the record wins (D3)
        return Outcome(record, problem=problem)
    return Outcome(AdvertCheck(t, chosen.carries), problem=problem)


def heard(pushed: Pushed, node: Node) -> Outcome:
    """A chat node the radio did not add: its NEW_ADVERT carries the record the radio made from
    this advert alone (lat/lon only with the flag), so T is exact and nothing waits. A position
    in that record is evidence on its own; else the log rules of `outcome` (design 6c D27)."""
    if node.position is not None:  # only a flagged advert sets it (BaseChatMesh.cpp:114-117)
        return Outcome(AdvertCheck(node.last_advert, True))
    return outcome(pushed, node, None, pushed.at + WAIT_S + 1)  # no wait: the record is this one
