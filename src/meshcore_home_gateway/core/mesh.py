"""The observer's view of the mesh (ADR 0014): the radio's contacts with what the gateway learned
about them, and the nodes it heard but did not add.

Fed with what the radio reports and the wall-clock time; documents are built from it in
`views`. Positions and telemetry readings are kept for contacts only: a node the radio merely
heard keeps none (privacy, ADR 0016).

Each companion keeps the last check of its adverts (ADR 0018, `core.adverts`): whether the newest
advert the gateway checked carried its position. A position from adverts is shown only while the
radio's record holds one and that check, if any, carried one: the record keeps a position after
the companion stops sharing it (design 6c D12). A heard node keeps the time and flag of its
checked advert, never a position.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from meshcore_home_gateway.core.lpp import Named
from meshcore_home_gateway.core.model import Position, distance_m

NODE_TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}  # advert types
COMPANION = 1  # the advert type of a chat contact (NODE_TYPES): the only one checked
MAX_NODES = 50  # heard nodes kept; the least recently heard goes first
NODE_TTL_S = 7 * 86_400.0  # a heard node is forgotten this long after it was last heard
FIX_MOVED_M = 20.0  # a telemetry position replaces the fix from this distance on
HEARD_REFRESH_S = 3600.0  # telemetry answers alone move "last heard" at most this often
ANSWERED_SLACK_S = 60.0  # a saved answer's time, after the round trip between the clocks


def type_name(node_type: int) -> str:
    return NODE_TYPES.get(node_type, f"type_{node_type}")


@dataclass(frozen=True, slots=True)
class Node:
    """A node as the radio describes it: a contact record, or the record of an advert from a node
    it did not add."""

    public_key: str  # 64 lower-case hex digits
    name: str
    node_type: int
    flags: int = 0
    path_hops: int | None = None  # repeaters on the stored path; None: no path, the radio floods
    last_advert: int = 0  # the node's clock at its newest advert
    position: Position | None = None  # from its newest advert that carried one


@dataclass(frozen=True, slots=True)
class Signal:
    """How a packet arrived."""

    hops: int | None = None  # repeaters it passed; None: by direct route or not known
    snr_db: float | None = None
    rssi_dbm: int | None = None


@dataclass(frozen=True, slots=True)
class Heard:
    at: float  # the gateway's clock
    how: str  # "advert", "message", "telemetry"
    signal: Signal = field(default_factory=Signal)


@dataclass(frozen=True, slots=True)
class Fix:
    """A position and when the gateway learned it."""

    position: Position
    at: float
    source: str  # "advert" or "telemetry"
    altitude_m: float | None = None


@dataclass(frozen=True, slots=True)
class AdvertCheck:
    """A companion's newest advert whose content the gateway checked (ADR 0018)."""

    at: int  # its timestamp, the contact's clock: the record's `last_advert` then
    carries: bool  # flag 0x10: a position everyone in range can read (also 0, 0: never had one)


def newer_check(one: AdvertCheck | None, other: AdvertCheck | None) -> AdvertCheck | None:
    """The check of the newer advert; `one` on a tie."""
    if one is None or (other is not None and other.at > one.at):
        return other
    return one


@dataclass(frozen=True, slots=True)
class Readings:
    """The readings of a contact's newest telemetry answer (ADR 0018)."""

    at: float  # the gateway's wall clock of the answer
    named: Named


@dataclass(slots=True)
class Tracked:
    node: Node
    first_heard: float | None = None
    heard: Heard | None = None
    fix: Fix | None = None
    position_received_at: float | None = None  # the newest telemetry answer with a position
    restored_heard: Heard | None = None  # last heard as the history knew it at the start
    readings: Readings | None = None  # of the newest telemetry answer
    # the last check of its adverts; on a heard node only its time and flag, never a position
    advert: AdvertCheck | None = None


@dataclass(frozen=True, slots=True)
class _Kept:
    """What an earlier run knew about a contact, for its first table read in this one."""

    heard: Heard | None = None
    fix: Fix | None = None
    readings: Readings | None = None
    position_received_at: float | None = None
    advert: AdvertCheck | None = None


@dataclass(frozen=True, slots=True)
class TableChange:
    """The difference between two reads of the contact table (full keys)."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()  # the record differs: name, type, flags, path, position

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


class Mesh:
    def __init__(self, *, max_nodes: int = MAX_NODES, node_ttl_s: float = NODE_TTL_S) -> None:
        self.contacts: dict[str, Tracked] = {}
        self.nodes: dict[str, Tracked] = {}  # heard, not contacts; least recently heard first
        self.max_contacts: int | None = None
        self._max_nodes = max_nodes
        self._node_ttl_s = node_ttl_s
        self._restored: dict[str, _Kept] = {}

    # --- contacts ------------------------------------------------------------------------------

    def restore(
        self, key: str, heard: Heard | None, fix: Fix | None, readings: Readings | None = None
    ) -> None:
        """What the history knows about a contact from before this run; it applies when the
        contact shows up in a table read."""
        kept = self._restored.get(key, _Kept())
        self._restored[key] = replace(kept, heard=heard, fix=fix, readings=readings)

    def restore_received(self, key: str, at: float) -> None:
        """When the contact's newest telemetry answer with a position came, from the saved
        schedule: kept for its first table read, or set at once if it is a contact without one."""
        tracked = self.contacts.get(key)
        if tracked is None:
            kept = self._restored.get(key, _Kept())
            self._restored[key] = replace(kept, position_received_at=at)
        elif tracked.position_received_at is None:
            tracked.position_received_at = at

    def restore_answered(self, key: str, at: float) -> None:
        """When the contact's newest telemetry answer came, from the saved schedule, before its
        first table read: readings the history kept of an older answer are not restored (it
        missed the later ones), so they cannot read as current."""
        kept = self._restored.get(key)
        if kept is None or kept.readings is None or kept.readings.at >= at - ANSWERED_SLACK_S:
            return
        self._restored[key] = replace(kept, readings=None)

    def restore_advert(self, key: str, check: AdvertCheck) -> None:
        """The check an earlier run stored; it applies at the contact's first table read."""
        kept = self._restored.get(key, _Kept())
        self._restored[key] = replace(kept, advert=check)

    def contacts_read(self, nodes: Iterable[Node], at: float) -> TableChange:
        """A complete read of the radio's contact table."""
        current = {node.public_key: node for node in nodes}
        removed = tuple(key for key in self.contacts if key not in current)
        added, changed = [], []
        gone = [self.contacts.pop(key) for key in removed]
        for key, node in current.items():
            tracked = self.contacts.get(key)
            if tracked is None:
                added.append(key)
                tracked = self.nodes.pop(key, None) or Tracked(node)  # a heard node now added
                kept = self._restored.pop(key, _Kept())
                heard = kept.heard
                tracked.heard = _newer(tracked.heard, heard)
                tracked.restored_heard = heard
                tracked.fix = kept.fix
                tracked.readings = kept.readings
                tracked.position_received_at = kept.position_received_at
                tracked.first_heard = tracked.first_heard or (heard.at if heard else None)
                tracked.advert = newer_check(tracked.advert, kept.advert)  # heard, stored
                previous = None
            else:
                previous = tracked.node
                if previous != node:
                    changed.append(key)
            tracked.node = node
            if node.node_type != COMPANION:
                tracked.advert = None
            self._advert_fix(tracked, previous, at)
            self.contacts[key] = tracked
        for tracked in gone:  # after the heard nodes just added left them: none of them evicted
            self._heard_again(tracked, at)
        return TableChange(tuple(added), removed, tuple(changed))

    def _heard_again(self, tracked: Tracked, at: float) -> None:
        """A contact that left the table is a heard node again while it counts as recently heard
        (so it can be added back), without what only a contact has: flags, path, position. It
        keeps the time and flag of its checked advert, so that it is checked when added back
        (design 6c D27). In its place by the time it was heard; then the limits apply."""
        if tracked.heard is None or at - tracked.heard.at > self._node_ttl_s:
            return
        node = replace(tracked.node, flags=0, path_hops=None, position=None)
        self.nodes[node.public_key] = Tracked(
            node, tracked.first_heard, tracked.heard, advert=tracked.advert
        )
        ordered = sorted(self.nodes.items(), key=lambda item: _heard_at(item[1]))  # stable
        self.nodes = dict(ordered)
        self.forget_nodes(at)

    def _advert_fix(self, tracked: Tracked, previous: Node | None, at: float) -> None:
        """A position from the contact's record: new when it differs from the record read before;
        on the first read of this run, kept unless the history already has a fix. None from
        adverts while the record holds none or the contact no longer shares it (`_shared`): an
        advert fix goes then, a telemetry fix stays."""
        position = tracked.node.position
        if position is None or not _shared(tracked):  # design 6c D12: no position from adverts
            if _from_advert(tracked.fix):
                tracked.fix = None
            return
        if previous is not None:
            if previous.position != position:
                tracked.fix = Fix(position, at, "advert")
            return
        if tracked.fix is None or (
            tracked.fix.source == "advert" and tracked.fix.position != position
        ):
            # the newest advert is at most this old; the one with the position may be older
            advert = tracked.node.last_advert
            tracked.fix = Fix(position, advert if 0 < advert <= at else at, "advert")

    def advert_checked(self, key: str, check: AdvertCheck, at: float) -> bool:
        """A companion's advert was checked (`core.adverts`): the check replaces the one held,
        unless it is older, and the advert fix follows `_shared` (design 6c D12): dropped when the
        check carries none, the record's position when it carries one and the contact has no fix
        or an advert fix elsewhere. A telemetry fix is never touched. True when the fix changed;
        nothing for a key that is no companion."""
        tracked = self.contacts.get(key)
        if tracked is None or tracked.node.node_type != COMPANION:
            return False
        if tracked.advert is not None and check.at < tracked.advert.at:
            return False
        tracked.advert = check
        before, position = tracked.fix, tracked.node.position
        if position is None or not _shared(tracked):
            if _from_advert(before):
                tracked.fix = None
        elif before is None or (_from_advert(before) and before.position != position):
            advert = tracked.node.last_advert  # the newest advert carried it: its own time
            tracked.fix = Fix(position, advert if 0 < advert <= at else at, "advert")
        return tracked.fix != before

    def contact_heard(self, key: str, heard: Heard) -> bool:
        """A contact was heard (advert, message, telemetry); False if it is no contact."""
        tracked = self.contacts.get(key)
        if tracked is None:
            return False
        tracked.heard = _newer(tracked.heard, heard)
        if tracked.first_heard is None:
            tracked.first_heard = heard.at
        return True

    def contact_answered(
        self, key: str, at: float, position: Position | None, altitude_m: float | None,
        readings: Readings | None = None,
    ) -> bool:  # fmt: skip
        """A telemetry answer of the contact (ADR 0018); True when `heard` moved. Its readings,
        when given, replace those of the answer before.

        While only its answers are heard, `heard` moves at most every HEARD_REFRESH_S and keeps
        the signal of the last advert or message (an answer reports none). The fix changes when
        the position moved FIX_MOVED_M or came from an advert: its time is when it changed, not
        when it was last confirmed. Answers come in the order the poller took them (by frame,
        not by the wall clock, which may be set back)."""
        tracked = self.contacts.get(key)
        if tracked is None:
            return False
        if readings is not None:
            tracked.readings = readings
        heard = tracked.heard
        moved = (
            heard is None or heard.how != "telemetry" or not 0 <= at - heard.at < HEARD_REFRESH_S
        )
        if moved:
            answer = Heard(at, "telemetry", heard.signal if heard else Signal())
            if heard is not None and heard.how == "telemetry":  # also after the clock went back
                tracked.heard = answer
            else:  # an advert or message newer than the answer stays
                self.contact_heard(key, answer)
                moved = tracked.heard is answer
        if position is not None:
            tracked.position_received_at = at
            fix = tracked.fix
            if (
                fix is None or fix.source != "telemetry"
                or distance_m(fix.position, position) >= FIX_MOVED_M
            ):  # fmt: skip
                tracked.fix = Fix(position, at, "telemetry", altitude_m)
        return moved

    def forget_telemetry(self, key: str, at: float) -> bool:
        """The contact was excluded from telemetry: its readings, `position_received_at` and a
        position from telemetry are forgotten, on the contact or in what is kept for its first
        table read. The position its record carries takes the place, as at a first table read,
        while the contact still shares it (design 6c D12). `heard` stays. True if anything
        changed."""
        changed = False
        if (kept := self._restored.get(key)) is not None:
            fix = None if _from_telemetry(kept.fix) else kept.fix
            forgotten = replace(kept, fix=fix, readings=None, position_received_at=None)
            changed = forgotten != kept
            self._restored[key] = forgotten
        tracked = self.contacts.get(key)
        if tracked is None:
            return changed
        changed |= tracked.readings is not None or tracked.position_received_at is not None
        tracked.readings = tracked.position_received_at = None
        if _from_telemetry(tracked.fix):
            tracked.fix = None
            self._advert_fix(tracked, None, at)
            changed = True
        return changed

    def path_reset(self, key: str) -> bool:
        """The radio forgot its path to the contact; True if the record had one."""
        tracked = self.contacts.get(key)
        if tracked is None or tracked.node.path_hops is None:
            return False
        tracked.node = replace(tracked.node, path_hops=None)
        return True

    # --- heard nodes ---------------------------------------------------------------------------

    def node_heard(self, node: Node, heard: Heard) -> bool:
        """An advert from a node the radio did not add; True the first time it is heard (within
        the memory of the heard nodes). Its position is dropped."""
        key = node.public_key
        if key in self.contacts:  # firmware 1.11 also reports a contact it has just added this way
            self.contact_heard(key, heard)
            return False
        tracked = self.nodes.pop(key, None)
        new = tracked is None
        if tracked is None:
            tracked = Tracked(replace(node, position=None), first_heard=heard.at)
        if new or node.last_advert >= tracked.node.last_advert:  # a replayed older advert: kept
            tracked.node = replace(node, position=None)
        tracked.heard = heard
        self.nodes[key] = tracked  # to the end: most recently heard
        self.forget_nodes(heard.at)
        return new

    def advert_heard(self, key: str, check: AdvertCheck) -> None:
        """A heard chat node's advert was checked from its NEW_ADVERT (`core.adverts.heard`):
        kept with the heard node, unless older, for the table read that adds it (design 6c
        D27)."""
        tracked = self.nodes.get(key)
        if tracked is not None and tracked.node.node_type == COMPANION:
            tracked.advert = newer_check(tracked.advert, check)

    def forget_nodes(self, now: float) -> list[str]:
        """Drop heard nodes beyond the limits; returns their keys."""
        dropped = []
        for key, tracked in list(self.nodes.items()):
            if now - _heard_at(tracked) > self._node_ttl_s or len(self.nodes) > self._max_nodes:
                del self.nodes[key]
                dropped.append(key)
        return dropped


def _heard_at(tracked: Tracked) -> float:
    return tracked.heard.at if tracked.heard else 0.0


def _from_telemetry(fix: Fix | None) -> bool:
    return fix is not None and fix.source == "telemetry"


def _from_advert(fix: Fix | None) -> bool:
    return fix is not None and fix.source == "advert"


def _shared(tracked: Tracked) -> bool:
    """The record's position is one the contact still shares in its adverts: the record holds
    one, and the last advert the gateway checked, if any, carried one. A newer advert that could
    not be checked changes nothing (design 6c D4)."""
    check = tracked.advert
    return tracked.node.position is not None and (check is None or check.carries)


def _newer(current: Heard | None, other: Heard | None) -> Heard | None:
    """The newer report; of two with the same time the later one, `other`."""
    if current is None:
        return other
    if other is None or current.at > other.at:
        return current
    return other


class AirtimeMeter:
    """The radio's own time on air over a sliding window, from its cumulative counter
    (`GET_STATS`, whole seconds). A counter that goes back means the radio restarted: the time
    before counts on.
    """

    def __init__(self, window_s: float = 3600.0) -> None:
        self._window_s = window_s
        self._samples: deque[tuple[float, int]] = deque()  # (time, total including restarts)
        self._last_counter: int | None = None
        self._offset = 0

    def add(self, at: float, counter_s: int) -> None:
        if self._last_counter is not None and counter_s < self._last_counter:
            self._offset += self._last_counter
        self._last_counter = counter_s
        self._samples.append((at, self._offset + counter_s))
        while len(self._samples) > 2 and at - self._samples[1][0] >= self._window_s:
            self._samples.popleft()  # keep one sample at or before the window's start

    def used_s(self) -> int | None:
        """Seconds on air within the window (or since the first sample, if that is younger);
        None before two samples."""
        if len(self._samples) < 2:
            return None
        return self._samples[-1][1] - self._samples[0][1]

    def span_s(self) -> float:
        return self._samples[-1][0] - self._samples[0][0] if self._samples else 0.0
