"""The observer's view of the mesh (ADR 0014): the radio's contacts with what the gateway learned
about them, and the nodes it heard but did not add.

Fed with what the radio reports and the wall-clock time; documents are built from it in
`views`. Positions are kept for contacts only: a node the radio merely heard keeps none (privacy,
ADR 0016).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from meshcore_home_gateway.core.model import Position, distance_m

NODE_TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}  # advert types
MAX_NODES = 50  # heard nodes kept; the least recently heard goes first
NODE_TTL_S = 7 * 86_400.0  # a heard node is forgotten this long after it was last heard
FIX_MOVED_M = 20.0  # a telemetry position replaces the fix from this distance on
HEARD_REFRESH_S = 3600.0  # telemetry answers alone move "last heard" at most this often


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


@dataclass(slots=True)
class Tracked:
    node: Node
    first_heard: float | None = None
    heard: Heard | None = None
    fix: Fix | None = None
    position_received_at: float | None = None  # the newest telemetry answer with a position


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
        self._restored: dict[str, tuple[Heard | None, Fix | None]] = {}

    # --- contacts ------------------------------------------------------------------------------

    def restore(self, key: str, heard: Heard | None, fix: Fix | None) -> None:
        """What the history knows about a contact from before this run; it applies when the
        contact shows up in a table read."""
        self._restored[key] = (heard, fix)

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
                heard, fix = self._restored.pop(key, (None, None))
                tracked.heard = _newer(tracked.heard, heard)
                tracked.fix = fix
                tracked.first_heard = tracked.first_heard or (heard.at if heard else None)
                previous = None
            else:
                previous = tracked.node
                if previous != node:
                    changed.append(key)
            tracked.node = node
            self._advert_fix(tracked, previous, at)
            self.contacts[key] = tracked
        for tracked in gone:  # after the heard nodes just added left them: none of them evicted
            self._heard_again(tracked, at)
        return TableChange(tuple(added), removed, tuple(changed))

    def _heard_again(self, tracked: Tracked, at: float) -> None:
        """A contact that left the table is a heard node again while it counts as recently heard
        (so it can be added back), without what only a contact has: flags, path, position. In its
        place by the time it was heard; then the limits apply."""
        if tracked.heard is None or at - tracked.heard.at > self._node_ttl_s:
            return
        node = replace(tracked.node, flags=0, path_hops=None, position=None)
        self.nodes[node.public_key] = Tracked(node, tracked.first_heard, tracked.heard)
        ordered = sorted(self.nodes.items(), key=lambda item: _heard_at(item[1]))  # stable
        self.nodes = dict(ordered)
        self.forget_nodes(at)

    def _advert_fix(self, tracked: Tracked, previous: Node | None, at: float) -> None:
        """A position from the contact's record: new when it differs from the record read before;
        on the first read of this run, kept unless the history already has a fix."""
        position = tracked.node.position
        if position is None:
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
        self, key: str, at: float, position: Position | None, altitude_m: float | None
    ) -> bool:
        """A telemetry answer of the contact (ADR 0018); True when `heard` moved.

        While only its answers are heard, `heard` moves at most every HEARD_REFRESH_S and keeps
        the signal of the last advert or message (an answer reports none). The fix changes when
        the position moved FIX_MOVED_M or came from an advert: its time is when it changed, not
        when it was last confirmed. Answers come in the order the poller took them (by frame,
        not by the wall clock, which may be set back)."""
        tracked = self.contacts.get(key)
        if tracked is None:
            return False
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
        if position is not None:
            tracked.position_received_at = at
            fix = tracked.fix
            if (
                fix is None or fix.source != "telemetry"
                or distance_m(fix.position, position) >= FIX_MOVED_M
            ):  # fmt: skip
                tracked.fix = Fix(position, at, "telemetry", altitude_m)
        return moved

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
