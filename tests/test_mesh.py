"""The observer's view of the mesh (core/mesh.py) and the radio's own airtime meter."""

from dataclasses import replace

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.mesh import (
    AdvertCheck,
    AirtimeMeter,
    Fix,
    Heard,
    Mesh,
    Node,
    Readings,
    Signal,
    TableChange,
    newer_check,
    type_name,
)
from meshcore_home_gateway.core.model import Position
from tests.radio_emulator import LPP_BME280, LPP_FULL

T = 1_800_000_000.0
ALICE = Node("a1" * 32, "Alice", 1, last_advert=int(T) - 600)
BOB = Node("b2" * 32, "Bob", 2, path_hops=1)
HOME = Position(53.55, 9.99)
FIELD = Position(53.56, 10.01)
BME280 = Readings(T, lpp.named(lpp.decode(LPP_BME280)))


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
    mesh.contact_answered(ALICE.public_key, T + 10, HOME, 3.0)
    assert mesh.contacts[ALICE.public_key].fix == Fix(HOME, T + 10, "telemetry", 3.0)
    mesh.contacts_read([Node(ALICE.public_key, "Alice", 1, position=HOME)], T + 20)
    assert mesh.contacts[ALICE.public_key].fix == Fix(HOME, T + 10, "telemetry", 3.0)
    assert not mesh.contact_answered(BOB.public_key, T + 30, FIELD, None)  # no contact


def test_a_telemetry_answer_moves_its_fix_from_20_m_on() -> None:
    """The fix's time is when the position changed: a resting companion's answers change
    nothing, so Home Assistant writes no state per answer."""
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    key = ALICE.public_key
    near = Position(HOME.latitude + 0.0001, HOME.longitude)  # 11 m
    far = Position(HOME.latitude + 0.0002, HOME.longitude)  # 22 m
    mesh.contact_answered(key, T + 1, HOME, 40.0)
    mesh.contact_answered(key, T + 2, near, 41.0)
    tracked = mesh.contacts[key]
    assert tracked.fix == Fix(HOME, T + 1, "telemetry", 40.0)
    assert tracked.position_received_at == T + 2
    mesh.contact_answered(key, T + 3, far, 42.0)
    assert tracked.fix == Fix(far, T + 3, "telemetry", 42.0)
    mesh.contact_answered(key, T + 4, None, None)  # no position: the last one stays
    assert tracked.fix == Fix(far, T + 3, "telemetry", 42.0)
    assert tracked.position_received_at == T + 3
    mesh.contact_answered(key, T - 50, HOME, 40.0)  # the wall clock was set back: still taken
    assert tracked.fix == Fix(HOME, T - 50, "telemetry", 40.0)


def test_a_telemetry_position_replaces_an_advert_fix_at_any_distance() -> None:
    """Its source and accuracy count, not only the distance (design §6.6)."""
    mesh = Mesh()
    mesh.contacts_read([Node(ALICE.public_key, "Alice", 1, position=HOME)], T)
    tracked = mesh.contacts[ALICE.public_key]
    assert tracked.fix is not None
    assert tracked.fix.source == "advert"
    five_m = Position(HOME.latitude + 0.000045, HOME.longitude)
    mesh.contact_answered(ALICE.public_key, T + 10, five_m, 7.0)
    assert tracked.fix == Fix(five_m, T + 10, "telemetry", 7.0)


def test_telemetry_answers_alone_move_last_heard_hourly_and_keep_the_signal() -> None:
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    key = ALICE.public_key
    advert = Heard(T, "advert", Signal(2, 7.5, -90))
    mesh.contact_heard(key, advert)
    assert mesh.contact_answered(key, T + 60, None, None)  # after an advert: at once
    assert mesh.contacts[key].heard == Heard(T + 60, "telemetry", Signal(2, 7.5, -90))
    assert not mesh.contact_answered(key, T + 60 + 3599, None, None)
    assert mesh.contacts[key].heard == Heard(T + 60, "telemetry", Signal(2, 7.5, -90))
    assert mesh.contact_answered(key, T + 60 + 3600, None, None)
    mesh.contacts_read([BOB], T + 4000)
    bob = mesh.contacts[BOB.public_key]
    assert mesh.contact_answered(BOB.public_key, T + 4001, None, None)  # never heard before
    assert bob.heard == Heard(T + 4001, "telemetry", Signal())


def test_a_path_reset_forgets_the_path_until_the_next_read() -> None:
    mesh = Mesh()
    mesh.contacts_read([BOB], T)
    assert mesh.path_reset(BOB.public_key)
    assert mesh.contacts[BOB.public_key].node.path_hops is None
    assert not mesh.path_reset(BOB.public_key)  # none left
    assert not mesh.path_reset(ALICE.public_key)  # no contact
    change = mesh.contacts_read([Node(BOB.public_key, "Bob", 2)], T + 1)
    assert not change  # the radio's record says the same now


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


def test_a_contact_that_leaves_the_table_is_a_heard_node_again_in_its_place() -> None:
    """It can be added back; without what only a contact has; in the order it was heard."""
    mesh = Mesh()
    alice = Node(ALICE.public_key, "Alice", 1, flags=0x11, path_hops=2, position=HOME)
    mesh.contacts_read([alice, BOB], T)
    mesh.contact_heard(ALICE.public_key, Heard(T - 100, "message"))
    carol, dave = Node("c3" * 32, "Carol", 1), Node("d4" * 32, "Dave", 1)
    mesh.node_heard(carol, Heard(T - 200, "advert"))
    mesh.node_heard(dave, Heard(T - 50, "advert"))
    change = mesh.contacts_read([], T)
    assert set(change.removed) == {ALICE.public_key, BOB.public_key}
    assert list(mesh.nodes) == ["c3" * 32, ALICE.public_key, "d4" * 32]  # Bob was never heard
    back = mesh.nodes[ALICE.public_key]
    assert (back.node.flags, back.node.path_hops, back.node.position, back.fix) == (
        0, None, None, None,
    )  # fmt: skip
    small = Mesh(max_nodes=2)
    small.node_heard(carol, Heard(T - 200, "advert"))
    small.node_heard(dave, Heard(T - 50, "advert"))
    small.contacts_read([ALICE], T)
    small.contact_heard(ALICE.public_key, Heard(T - 100, "message"))
    small.contacts_read([], T)
    assert list(small.nodes) == [ALICE.public_key, "d4" * 32]  # the least recently heard goes


def test_a_contact_not_heard_lately_is_forgotten_when_it_leaves() -> None:
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    mesh.contact_heard(ALICE.public_key, Heard(T - 8 * 86_400, "message"))
    mesh.contacts_read([], T)
    assert mesh.nodes == {}


def test_a_replayed_older_advert_does_not_replace_the_heard_record() -> None:
    mesh = Mesh()
    newer = Node("c3" * 32, "Carol", 1, last_advert=int(T) - 10)
    older = Node("c3" * 32, "Mallory", 3, last_advert=int(T) - 900)
    mesh.node_heard(newer, Heard(T - 5, "advert"))
    assert not mesh.node_heard(older, Heard(T, "advert"))
    tracked = mesh.nodes["c3" * 32]
    assert (tracked.node.name, tracked.node.node_type) == ("Carol", 1)
    assert tracked.heard is not None
    assert tracked.heard.at == T  # but heard


def test_a_contact_back_among_the_heard_nodes_evicts_none_added_in_the_same_read() -> None:
    """One read shows Alice removed and Dave, the least recently heard node, added: Dave keeps
    what the gateway heard of him."""
    mesh = Mesh(max_nodes=2)
    carol, dave = Node("c3" * 32, "Carol", 1), Node("d4" * 32, "Dave", 1)
    mesh.node_heard(dave, Heard(T - 300, "advert"))
    mesh.node_heard(carol, Heard(T - 200, "advert"))
    mesh.contacts_read([ALICE], T)
    mesh.contact_heard(ALICE.public_key, Heard(T - 100, "message"))
    mesh.contacts_read([dave], T)
    assert mesh.contacts["d4" * 32].heard == Heard(T - 300, "advert")
    assert list(mesh.nodes) == ["c3" * 32, ALICE.public_key]


def test_last_heard_follows_answers_after_the_clock_was_set_back() -> None:
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    key = ALICE.public_key
    mesh.contact_answered(key, T, None, None)
    assert mesh.contact_answered(key, T - 7200 + 900, None, None)  # the clock went back 2 h
    assert mesh.contacts[key].heard == Heard(T - 6300, "telemetry", Signal())
    mesh.contact_heard(key, Heard(T - 6000, "advert", Signal(1, 5.0, -80)))
    assert not mesh.contact_answered(key, T - 6100, None, None)  # older than the advert: kept
    assert mesh.contacts[key].heard == Heard(T - 6000, "advert", Signal(1, 5.0, -80))
    assert mesh.contact_answered(key, T - 5900, None, None)  # newer: it moves


# --- readings of telemetry answers ----------------------------------------------------------------


def test_an_answer_with_readings_keeps_the_rules_of_last_heard_and_the_fix() -> None:
    mesh = Mesh()
    mesh.contacts_read([ALICE], T)
    key = ALICE.public_key
    assert mesh.contact_answered(key, T, HOME, 40.0, BME280)
    tracked = mesh.contacts[key]
    assert tracked.readings == BME280
    assert (tracked.heard, tracked.fix) == (
        Heard(T, "telemetry"), Fix(HOME, T, "telemetry", 40.0),
    )  # fmt: skip
    later = Readings(T + 900, lpp.named(lpp.decode(LPP_FULL)))
    assert not mesh.contact_answered(key, T + 900, HOME, 40.0, later)  # within the hour
    assert tracked.readings == later  # the newest answer's, also when it has fewer
    assert tracked.readings.named.readings == {}
    assert tracked.heard == Heard(T, "telemetry")
    mesh.contact_answered(key, T + 1000, None, None)  # none given: the last ones stay
    assert tracked.readings == later
    assert not mesh.contact_answered(BOB.public_key, T, None, None, BME280)  # no contact


def test_readings_from_the_history_apply_at_the_first_table_read() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    gps = Fix(FIELD, T - 60, "telemetry")
    mesh.restore(key, Heard(T - 60, "telemetry"), gps, BME280)
    assert key not in mesh.contacts
    mesh.contacts_read([ALICE], T)
    tracked = mesh.contacts[key]
    assert (tracked.readings, tracked.fix, tracked.heard) == (
        BME280, gps, Heard(T - 60, "telemetry"),
    )  # fmt: skip
    mesh.restore(BOB.public_key, None, None)
    mesh.contacts_read([ALICE, BOB], T + 1)
    assert mesh.contacts[BOB.public_key].readings is None


def test_when_the_newest_position_came_is_restored_before_and_after_the_first_read() -> None:
    """From the saved schedule; before the first table read it is kept with the history's."""
    mesh = Mesh()
    alice, bob = ALICE.public_key, BOB.public_key
    mesh.restore_received(alice, T - 120)
    mesh.restore(alice, Heard(T - 60, "telemetry"), Fix(FIELD, T - 600, "telemetry"), BME280)
    mesh.contacts_read([ALICE, BOB], T)
    tracked = mesh.contacts[alice]
    assert (tracked.position_received_at, tracked.readings) == (T - 120, BME280)
    mesh.restore_received(bob, T - 30)  # a contact without one: at once
    assert mesh.contacts[bob].position_received_at == T - 30
    mesh.contact_answered(alice, T + 10, FIELD, None)
    mesh.restore_received(alice, T - 120)  # one it has stays
    assert tracked.position_received_at == T + 10
    mesh.contacts_read([ALICE], T + 20)
    mesh.contacts_read([ALICE, BOB], T + 30)  # back: what the last run knew is used up
    assert mesh.contacts[bob].position_received_at is None


def test_forgetting_telemetry_puts_the_records_position_back() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    record = Node(key, "Alice", 1, last_advert=int(T) - 600, position=HOME)
    mesh.contacts_read([record], T)
    mesh.contact_answered(key, T + 10, FIELD, 12.0, BME280)
    assert mesh.forget_telemetry(key, T + 20)
    tracked = mesh.contacts[key]
    assert (tracked.readings, tracked.position_received_at) == (None, None)
    assert tracked.fix == Fix(HOME, T - 600, "advert")  # as at a first table read
    assert tracked.heard == Heard(T + 10, "telemetry")  # when it last answered stays
    assert not mesh.forget_telemetry(key, T + 30)  # nothing left to forget
    assert tracked.fix == Fix(HOME, T - 600, "advert")
    mesh.contact_answered(key, T + 40, None, None, BME280)
    assert mesh.forget_telemetry(key, T + 50)  # its readings alone
    assert tracked.fix == Fix(HOME, T - 600, "advert")  # an advert fix stays
    assert not mesh.forget_telemetry(BOB.public_key, T)  # no contact


def test_forgetting_telemetry_without_a_record_position_leaves_no_fix() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([ALICE], T)
    mesh.contact_answered(key, T + 10, FIELD, None)
    assert mesh.forget_telemetry(key, T + 20)
    assert mesh.contacts[key].fix is None


def test_forgetting_telemetry_before_the_first_table_read() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    heard = Heard(T - 60, "telemetry")
    mesh.restore(key, heard, Fix(FIELD, T - 60, "telemetry"), BME280)
    mesh.restore_received(key, T - 60)
    assert mesh.forget_telemetry(key, T)
    assert not mesh.forget_telemetry(key, T)
    mesh.contacts_read([Node(key, "Alice", 1, last_advert=int(T) - 600, position=HOME)], T)
    tracked = mesh.contacts[key]
    assert (tracked.readings, tracked.position_received_at, tracked.heard) == (None, None, heard)
    assert tracked.fix == Fix(HOME, T - 600, "advert")
    mesh.restore(BOB.public_key, None, Fix(HOME, T - 60, "advert"))
    assert not mesh.forget_telemetry(BOB.public_key, T)  # an advert fix is kept
    mesh.contacts_read([ALICE, BOB], T)
    assert mesh.contacts[BOB.public_key].fix is None  # but Bob's record holds no position now


def test_a_contact_that_leaves_the_table_loses_its_readings() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([ALICE], T)
    mesh.contact_answered(key, T, HOME, None, BME280)
    mesh.contacts_read([], T + 1)
    assert mesh.nodes[key].readings is None
    mesh.contacts_read([ALICE], T + 2)
    tracked = mesh.contacts[key]
    assert (tracked.readings, tracked.position_received_at, tracked.fix) == (None, None, None)


# --- the check for positions in adverts (design 6c) -----------------------------------------------

SHARED = Node(ALICE.public_key, "Alice", 1, last_advert=int(T) - 600, position=HOME)


def test_the_newer_check_is_the_one_of_the_newer_advert() -> None:
    older, newer = AdvertCheck(int(T) - 600, True), AdvertCheck(int(T) - 60, False)
    assert newer_check(older, newer) == newer
    assert newer_check(newer, older) == newer
    assert newer_check(None, older) == older
    assert newer_check(older, None) == older
    assert newer_check(None, None) is None
    same = AdvertCheck(int(T) - 600, False)
    assert newer_check(older, same) is older  # a tie: the first


def test_a_check_without_a_position_drops_an_advert_fix_and_keeps_a_telemetry_fix() -> None:
    """The radio's record keeps a position after sharing stops (BaseChatMesh.cpp:193-196): it is
    shown only while the last checked advert carried one (design D12)."""
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    tracked = mesh.contacts[key]
    assert mesh.contacts[key].fix == Fix(HOME, T - 600, "advert")
    assert mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T + 2)
    assert (tracked.advert, tracked.fix) == (AdvertCheck(int(T) - 600, False), None)
    mesh.contacts_read([SHARED], T + 60)
    assert mesh.contacts[key].fix is None  # the record holds it, the companion no longer shares it
    mesh.contact_answered(key, T + 70, FIELD, None)
    newer = replace(SHARED, last_advert=int(T) + 100)
    mesh.contacts_read([newer], T + 120)
    assert not mesh.advert_checked(key, AdvertCheck(int(T) + 100, False), T + 122)
    assert (tracked.advert, tracked.fix) == (
        AdvertCheck(int(T) + 100, False), Fix(FIELD, T + 70, "telemetry"),
    )  # fmt: skip


def test_a_check_with_a_position_brings_the_record_position_back_at_the_adverts_time() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    mesh.restore_advert(key, AdvertCheck(int(T) - 900, False))
    mesh.contacts_read([SHARED], T)  # an advert since that the gateway did not check
    tracked = mesh.contacts[key]
    assert mesh.contacts[key].fix is None
    assert mesh.advert_checked(key, AdvertCheck(int(T) - 600, True), T + 2)
    assert tracked.fix == Fix(HOME, T - 600, "advert")  # the advert's own time
    assert not mesh.advert_checked(key, AdvertCheck(int(T) - 600, True), T + 3)  # nothing new
    tracked.fix = Fix(FIELD, T - 900, "advert")  # an advert fix elsewhere
    assert mesh.advert_checked(key, AdvertCheck(int(T) - 600, True), T + 4)
    assert tracked.fix == Fix(HOME, T - 600, "advert")
    gps = Fix(FIELD, T + 5, "telemetry")
    tracked.fix = gps
    assert not mesh.advert_checked(key, AdvertCheck(int(T) - 600, True), T + 6)
    assert tracked.fix == gps  # a telemetry fix stays
    future = replace(SHARED, last_advert=int(T) + 999)  # the companion's clock is ahead
    mesh.contacts_read([future], T + 10)
    tracked.fix = None
    assert mesh.advert_checked(key, AdvertCheck(int(T) + 999, True), T + 12)
    assert tracked.fix == Fix(HOME, T + 12, "advert")


def test_a_check_that_carries_a_changed_position_replaces_a_telemetry_fix() -> None:
    """The table read takes the record before its advert is checked: under the check before
    (none carried) the changed position was not shared. The check that carries it replaces a
    telemetry fix as a changed advert position does (design 6c G26); an unchanged one does not."""
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T)
    mesh.contact_answered(key, T + 10, FIELD, None)
    elsewhere = Position(53.57, 10.02)
    moved = replace(SHARED, last_advert=int(T) + 60, position=elsewhere)
    mesh.contacts_read([moved], T + 120)
    tracked = mesh.contacts[key]
    assert tracked.fix == Fix(FIELD, T + 10, "telemetry")  # the check before carried none
    assert mesh.advert_checked(key, AdvertCheck(int(T) + 60, True), T + 120, SHARED)
    assert tracked.fix == Fix(elsewhere, T + 120, "advert")
    mesh.contact_answered(key, T + 130, FIELD, None)
    stopped = replace(moved, last_advert=int(T) + 90)
    mesh.contacts_read([stopped], T + 140)
    mesh.advert_checked(key, AdvertCheck(int(T) + 90, False), T + 140)
    again = replace(moved, last_advert=int(T) + 150)
    mesh.contacts_read([again], T + 160)
    assert not mesh.advert_checked(key, AdvertCheck(int(T) + 150, True), T + 160, stopped)
    assert tracked.fix == Fix(FIELD, T + 130, "telemetry")


def test_a_check_that_carries_a_changed_position_without_a_fix_gives_the_adverts_time() -> None:
    """With no fix there is nothing to replace: a changed position comes back at the time of
    the advert that carried it, as the same position does (docs/mqtt-api.md, `position_at`)."""
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T)
    elsewhere = Position(53.57, 10.02)
    moved = replace(SHARED, last_advert=int(T) + 60, position=elsewhere)
    mesh.contacts_read([moved], T + 120)
    tracked = mesh.contacts[key]
    assert tracked.fix is None  # the check before carried none
    assert mesh.advert_checked(key, AdvertCheck(int(T) + 60, True), T + 120, SHARED)
    assert tracked.fix == Fix(elsewhere, T + 60, "advert")


def test_an_older_check_is_ignored() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    tracked = mesh.contacts[key]
    assert mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T)
    assert not mesh.advert_checked(key, AdvertCheck(int(T) - 900, True), T + 1)
    assert (tracked.advert, tracked.fix) == (AdvertCheck(int(T) - 600, False), None)
    assert mesh.advert_checked(key, AdvertCheck(int(T) - 600, True), T + 2)  # the same advert
    assert tracked.advert == AdvertCheck(int(T) - 600, True)
    assert tracked.fix == Fix(HOME, T - 600, "advert")


def test_only_companions_are_checked() -> None:
    mesh = Mesh()
    repeater = Node(BOB.public_key, "Bob", 2, last_advert=int(T) - 600, position=HOME)
    mesh.contacts_read([repeater], T)
    assert not mesh.advert_checked(BOB.public_key, AdvertCheck(int(T) - 600, False), T)
    tracked = mesh.contacts[BOB.public_key]
    assert (tracked.advert, tracked.fix) == (None, Fix(HOME, T - 600, "advert"))
    assert not mesh.advert_checked(ALICE.public_key, AdvertCheck(int(T) - 600, False), T)
    assert ALICE.public_key not in mesh.contacts


def test_an_unchecked_newer_advert_does_not_bring_an_old_position_back() -> None:
    """The last check decides, whatever advert it was of: a newer advert the gateway could not
    check (its report dropped, the gateway stopped) says nothing (design D4, D12)."""
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T)
    newer = replace(SHARED, last_advert=int(T) + 60)
    mesh.contacts_read([newer], T + 120)
    assert mesh.contacts[key].fix is None
    restarted = Mesh()  # the check stored, the advert fix in the history
    restarted.restore(key, None, Fix(HOME, T - 600, "advert"))
    restarted.restore_advert(key, AdvertCheck(int(T) - 600, False))
    restarted.contacts_read([newer], T + 300)
    tracked = restarted.contacts[key]
    assert (tracked.advert, tracked.fix) == (AdvertCheck(int(T) - 600, False), None)
    unknown = Mesh()  # never checked: today's rule
    unknown.restore(key, None, Fix(HOME, T - 600, "advert"))
    unknown.contacts_read([newer], T + 300)
    assert unknown.contacts[key].fix == Fix(HOME, T - 600, "advert")


def test_a_record_without_a_position_drops_an_advert_fix() -> None:
    """After an advert that shares 0, 0 (a companion that never had a position) or the gateway's
    own add, the record holds none: the old place is not known to be current, also for a contact
    never checked (design D12)."""
    mesh = Mesh()
    key = ALICE.public_key
    repeater = Node(BOB.public_key, "Bob", 2, last_advert=int(T) - 600, position=FIELD)
    mesh.contacts_read([SHARED, repeater], T)
    zeroed = [
        replace(SHARED, last_advert=int(T) + 60, position=None), replace(repeater, position=None),
    ]  # fmt: skip
    mesh.contacts_read(zeroed, T + 120)
    assert [t.fix for t in mesh.contacts.values()] == [None, None]
    mesh.contact_answered(key, T + 130, FIELD, None)
    mesh.contacts_read(zeroed, T + 180)
    assert mesh.contacts[key].fix == Fix(FIELD, T + 130, "telemetry")


def test_an_exclusion_falls_back_to_the_record_only_while_it_is_shared() -> None:
    """A contact excluded from telemetry gets its record's position back only while its adverts
    carry it (6b's open point n5)."""
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T)
    mesh.contact_answered(key, T + 10, FIELD, None)
    assert mesh.forget_telemetry(key, T + 20)
    tracked = mesh.contacts[key]
    assert tracked.fix is None
    mesh.advert_checked(key, AdvertCheck(int(T) - 600, True), T + 30)
    mesh.contact_answered(key, T + 40, FIELD, None)
    assert mesh.forget_telemetry(key, T + 50)
    assert tracked.fix == Fix(HOME, T - 600, "advert")


def test_a_stored_check_applies_at_the_first_read() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    check = AdvertCheck(int(T) - 600, True)
    heard = Heard(T - 300, "message")
    mesh.restore(key, heard, None)
    mesh.restore_advert(key, check)
    mesh.restore(BOB.public_key, None, None)
    mesh.restore_advert(BOB.public_key, check)
    mesh.restore(BOB.public_key, heard, None)  # either order
    mesh.contacts_read([SHARED, Node(BOB.public_key, "Bob", 1)], T)
    tracked, bob = mesh.contacts[key], mesh.contacts[BOB.public_key]
    assert (tracked.advert, tracked.heard, tracked.fix) == (
        check, heard, Fix(HOME, T - 600, "advert"),
    )  # fmt: skip
    assert (bob.advert, bob.heard) == (check, heard)
    mesh.contacts_read([], T + 1)
    mesh.contacts_read([SHARED], T + 2)  # back: what the last run knew is used up
    assert mesh.contacts[key].advert == check  # kept by its heard node


def test_a_contact_that_leaves_keeps_its_check_as_a_heard_node() -> None:
    """A time and a flag, never a position: a contact removed and added again through the
    gateway keeps its check (design D27)."""
    mesh = Mesh()
    key = ALICE.public_key
    check = AdvertCheck(int(T) - 600, True)
    mesh.contacts_read([SHARED], T)
    mesh.contact_heard(key, Heard(T, "advert"))
    mesh.advert_checked(key, check, T)
    mesh.contacts_read([], T + 10)
    node = mesh.nodes[key]
    assert (node.advert, node.node.position, node.fix) == (check, None, None)
    mesh.contacts_read([replace(SHARED, position=None)], T + 20)  # the gateway's add: no position
    assert mesh.contacts[key].advert == check


def test_a_contact_that_is_no_longer_a_companion_drops_it() -> None:
    mesh = Mesh()
    key = ALICE.public_key
    mesh.contacts_read([SHARED], T)
    mesh.advert_checked(key, AdvertCheck(int(T) - 600, False), T)
    mesh.contacts_read([replace(SHARED, node_type=3)], T + 10)  # a room server now
    tracked = mesh.contacts[key]
    assert tracked.advert is None
    assert tracked.fix is None  # its record's position comes back with the next change
    restored = Mesh()
    restored.restore_advert(BOB.public_key, AdvertCheck(int(T) - 600, True))
    restored.contacts_read([BOB], T)
    assert restored.contacts[BOB.public_key].advert is None


def test_a_heard_check_is_kept_without_a_position_and_taken_at_the_add() -> None:
    """A chat node's NEW_ADVERT is checked at once; the table read that adds it takes the newer
    of that check and the one an earlier run stored (design D27)."""
    mesh = Mesh()
    carol = Node("c3" * 32, "Carol", 1, last_advert=int(T) - 60, position=HOME)
    heard = AdvertCheck(int(T) - 60, True)
    mesh.node_heard(carol, Heard(T, "advert"))
    mesh.advert_heard(carol.public_key, heard)
    mesh.advert_heard(carol.public_key, AdvertCheck(int(T) - 120, False))  # older: not taken
    tracked = mesh.nodes[carol.public_key]
    assert (tracked.advert, tracked.node.position, tracked.fix) == (heard, None, None)
    dave = Node("d4" * 32, "Dave", 2, last_advert=int(T) - 60)
    mesh.node_heard(dave, Heard(T, "advert"))
    mesh.advert_heard(dave.public_key, heard)  # a repeater shares its position by design
    assert mesh.nodes[dave.public_key].advert is None
    mesh.advert_heard("e5" * 32, heard)  # not heard (forgotten): nothing
    assert "e5" * 32 not in mesh.nodes
    mesh.restore_advert(carol.public_key, AdvertCheck(int(T) - 600, False))
    added = replace(carol, position=None)  # the gateway's add writes no position
    mesh.contacts_read([added], T + 30)
    assert mesh.contacts[carol.public_key].advert == heard
    erin = Node("e5" * 32, "Erin", 1, last_advert=int(T) - 600)
    mesh.node_heard(erin, Heard(T, "advert"))
    mesh.advert_heard(erin.public_key, AdvertCheck(int(T) - 600, False))
    stored = AdvertCheck(int(T) - 60, True)  # from a later advert, before a restart
    mesh.restore_advert(erin.public_key, stored)
    mesh.contacts_read([added, erin], T + 40)
    assert mesh.contacts[erin.public_key].advert == stored
