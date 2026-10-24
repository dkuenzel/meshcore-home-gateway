"""The observer's view of the mesh (core/mesh.py) and the radio's own airtime meter."""

from meshcore_home_gateway.core.mesh import (
    AirtimeMeter,
    Fix,
    Heard,
    Mesh,
    Node,
    Signal,
    TableChange,
    type_name,
)
from meshcore_home_gateway.core.model import Position

T = 1_800_000_000.0
ALICE = Node("a1" * 32, "Alice", 1, last_advert=int(T) - 600)
BOB = Node("b2" * 32, "Bob", 2, path_hops=1)
HOME = Position(53.55, 9.99)
FIELD = Position(53.56, 10.01)


def test_table_reads_report_what_was_added_removed_and_changed() -> None:
    mesh = Mesh()
    assert mesh.contacts_read([ALICE, BOB], T) == TableChange((ALICE.public_key, BOB.public_key))
    renamed = Node(ALICE.public_key, "Alice B", 1)
    change = mesh.contacts_read([renamed], T + 1)
    assert change == TableChange((), (BOB.public_key,), (ALICE.public_key,))
    assert mesh.contacts_read([renamed], T + 2) == TableChange()
    assert not mesh.contacts_read([renamed], T + 2)
    assert list(mesh.contacts) == [ALICE.public_key]
    assert mesh.contacts[ALICE.public_key].node.name == "Alice B"


def test_a_position_in_the_record_becomes_a_fix_when_it_changes() -> None:
    mesh = Mesh()
    first = Node(ALICE.public_key, "Alice", 1, last_advert=int(T) - 600, position=HOME)
    mesh.contacts_read([first], T)
    fix = mesh.contacts[ALICE.public_key].fix
    # the first read cannot know when the advert with it came: at most as old as the newest one
    assert fix == Fix(HOME, T - 600, "advert")
    mesh.contacts_read([first], T + 60)
    assert mesh.contacts[ALICE.public_key].fix == fix  # unchanged
    moved = Node(ALICE.public_key, "Alice", 1, last_advert=int(T) + 100, position=FIELD)
    mesh.contacts_read([moved], T + 120)
    assert mesh.contacts[ALICE.public_key].fix == Fix(FIELD, T + 120, "advert")


def test_a_first_read_takes_the_time_of_the_read_when_the_advert_time_is_implausible() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(BOB.public_key, "Bob", 2, last_advert=int(T) + 999, position=HOME)], T)
    assert mesh.contacts[BOB.public_key].fix == Fix(HOME, T, "advert")


def test_history_from_earlier_runs_is_restored_when_the_contact_shows_up() -> None:
    mesh = Mesh()
    heard = Heard(T - 300, "message", Signal(2, 5.5))
    stored = Fix(HOME, T - 3000, "advert")
    mesh.restore(ALICE.public_key, heard, stored)
    mesh.contacts_read([Node(ALICE.public_key, "Alice", 1, position=HOME)], T)
    tracked = mesh.contacts[ALICE.public_key]
    assert tracked.heard == heard
    assert tracked.fix == stored  # the same position keeps the time it was first seen
    assert tracked.first_heard == T - 300


def test_a_telemetry_fix_is_not_replaced_by_an_unchanged_advert_position() -> None:
    mesh = Mesh()
    gps = Fix(FIELD, T - 60, "telemetry", altitude_m=12.0)
    mesh.restore(ALICE.public_key, None, gps)
    mesh.contacts_read([Node(ALICE.public_key, "Alice", 1, position=HOME)], T)
    assert mesh.contacts[ALICE.public_key].fix == gps
    assert mesh.contact_fix(ALICE.public_key, Fix(HOME, T + 10, "telemetry"))
    assert mesh.contacts[ALICE.public_key].fix == Fix(HOME, T + 10, "telemetry")
    assert mesh.contact_fix(ALICE.public_key, Fix(FIELD, T, "telemetry"))  # older: kept out
    assert mesh.contacts[ALICE.public_key].fix == Fix(HOME, T + 10, "telemetry")
    assert not mesh.contact_fix(BOB.public_key, gps)


def test_contacts_heard_keep_the_newest_report() -> None:
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    assert mesh.contact_heard(ALICE.public_key, Heard(T + 10, "advert"))
    assert mesh.contact_heard(ALICE.public_key, Heard(T + 5, "message"))
    tracked = mesh.contacts[ALICE.public_key]
    assert tracked.heard == Heard(T + 10, "advert")
    assert tracked.first_heard == T + 10
    assert not mesh.contact_heard(BOB.public_key, Heard(T, "advert"))


def test_heard_nodes_are_bounded_and_keep_no_position() -> None:
    mesh = Mesh(max_nodes=2, node_ttl_s=100)
    carol = Node("c3" * 32, "Carol", 1, position=HOME)
    assert mesh.node_heard(carol, Heard(T, "advert"))
    assert mesh.nodes[carol.public_key].node.position is None  # privacy: no positions of strangers
    assert not mesh.node_heard(carol, Heard(T + 1, "advert"))  # heard before
    assert mesh.nodes[carol.public_key].first_heard == T
    dave, erin = Node("d4" * 32, "Dave", 2), Node("e5" * 32, "Erin", 3)
    mesh.node_heard(dave, Heard(T + 2, "advert"))
    mesh.node_heard(erin, Heard(T + 3, "advert"))
    assert list(mesh.nodes) == [dave.public_key, erin.public_key]  # the least recent went
    assert mesh.forget_nodes(T + 102.5) == [dave.public_key]
    assert list(mesh.nodes) == [erin.public_key]


def test_a_heard_node_that_is_added_keeps_its_sighting() -> None:
    mesh = Mesh()
    carol = Node("c3" * 32, "Carol", 1)
    mesh.node_heard(carol, Heard(T, "advert", Signal(3, -2.5, -110)))
    mesh.contacts_read([carol], T + 30)
    assert carol.public_key not in mesh.nodes
    tracked = mesh.contacts[carol.public_key]
    assert tracked.heard == Heard(T, "advert", Signal(3, -2.5, -110))
    assert tracked.first_heard == T


def test_a_new_advert_of_a_contact_counts_as_heard_contact() -> None:
    """Firmware 1.11 also sends NEW_ADVERT for a contact it has just added."""
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    assert not mesh.node_heard(ALICE, Heard(T + 1, "advert"))
    assert not mesh.nodes
    assert mesh.contacts[ALICE.public_key].heard == Heard(T + 1, "advert")


def test_type_names() -> None:
    assert [type_name(t) for t in (1, 2, 3, 4, 9)] == [
        "chat", "repeater", "room", "sensor", "type_9",
    ]  # fmt: skip


def test_the_airtime_meter_counts_the_last_hour_across_restarts() -> None:
    meter = AirtimeMeter(window_s=3600)
    assert meter.used_s() is None
    assert meter.span_s() == 0.0
    meter.add(T, 100)
    assert meter.used_s() is None  # one sample: nothing to compare
    meter.add(T + 60, 103)
    assert (meter.used_s(), meter.span_s()) == (3, 60)
    meter.add(T + 120, 1)  # the radio restarted: its counter began again
    assert meter.used_s() == 4
    for minute in range(3, 70):
        meter.add(T + minute * 60, 1 + (minute - 2))
    # the oldest sample kept is the one at or just before the start of the window
    assert (meter.used_s(), meter.span_s()) == (60, 3600)
    meter.add(T + 70 * 60 + 30, 69)
    assert (meter.used_s(), meter.span_s()) == (60, 3630)
