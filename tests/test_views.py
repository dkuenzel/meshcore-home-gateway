"""The providers' documents (views.py, ADR 0015): shapes, keys always present, sizes bounded."""

import json
from typing import Any

import pytest

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.lpp import Named
from meshcore_home_gateway.core.mesh import (
    AdvertCheck,
    AirtimeMeter,
    Fix,
    Heard,
    Mesh,
    Node,
    Readings,
    Signal,
    Tracked,
)
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.core.telemetry import CURRENT, STATES, TelemetryState
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.views import (
    ATTRIBUTE_LIMIT_BYTES,
    INDEX_LIMIT_BYTES,
    PENDING_LIMIT_BYTES,
    PENDING_STEPS,
    HistoryEntry,
    PendingEntry,
    RadioReadings,
    TargetSetting,
    advert_carries_position,
    bounded,
    config_document,
    contact_document,
    contact_event,
    contacts_document,
    encode,
    flag_names,
    history_document,
    history_entry,
    iso,
    name_taken_nodes,
    nodes_document,
    pending_document,
    pending_entry,
    restated,
    stats_document,
    telemetry_event,
)
from tests.radio_emulator import LPP_BME280

T = 1_800_000_000.0
KEY = "a1" * 32
HOSTILE = '<img src=x onerror=alert(1)>\x1b[2J"\\\x00' + chr(0x2028)  # inflate JSON


def test_a_contact_document_has_every_key_and_iso_times() -> None:
    mesh = Mesh()
    node = Node(
        KEY,
        "Alice",
        1,
        flags=0x01 | 0x10,
        path_hops=2,
        last_advert=int(T) - 60,
        position=Position(53.55, 9.99),
    )
    mesh.contacts_read([node], T)
    mesh.contact_heard(KEY, Heard(T + 5, "message", Signal(2, 6.25, -97)))
    document = contact_document(mesh.contacts[KEY], protected=True)
    assert document == {
        "key": KEY,
        "prefix": "a1a1a1a1a1a1",
        "name": "Alice",
        "type": "chat",
        "flags": ["favourite", "remote_cli"],
        "favourite": True,
        "remote_cli": True,
        "protected": True,
        "removable": False,
        "verified": True,
        "path_known": True,
        "path_hops": 2,
        "last_advert_at": "2027-01-15T07:59:00+00:00",
        "first_heard_at": "2027-01-15T08:00:05+00:00",
        "heard_at": "2027-01-15T08:00:05+00:00",
        "heard_via": "message",
        "hops": 2,
        "snr_db": 6.25,
        "rssi_dbm": -97,
        "latitude": 53.55,
        "longitude": 9.99,
        "altitude_m": None,
        "position_at": "2027-01-15T07:59:00+00:00",
        "position_source": "advert",
        "position_accuracy_m": None,
        "position_received_at": None,
        "advert_position": True,
        "advert_carries_position": True,
        "advert_checked_at": None,
        "telemetry_state": None,
        "telemetry_excluded": False,
        "telemetry_current": None,
        "position_current": None,
        "readings_at": None,
        "battery_v": None,
        "mcu_temperature_c": None,
        "readings": {},
    }


def test_a_telemetry_position_has_its_accuracy_and_when_it_was_received() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Alice", 1)], T)
    mesh.contact_answered(KEY, T + 10, Position(50.12345, 8.56775), 120.5)
    mesh.contact_answered(KEY, T + 100, Position(50.12345, 8.56775), 120.5)
    document = contact_document(mesh.contacts[KEY])
    assert {
        k: document[k]
        for k in (
            "latitude",
            "longitude",
            "altitude_m",
            "position_at",
            "position_source",
            "position_accuracy_m",
            "position_received_at",
            "heard_via",
            "advert_position",
        )
    } == {
        "latitude": 50.12345,
        "longitude": 8.56775,
        "altitude_m": 120.5,
        "position_at": "2027-01-15T08:00:10+00:00",
        "position_source": "telemetry",
        "position_accuracy_m": 15.0,
        "position_received_at": "2027-01-15T08:01:40+00:00",
        "heard_via": "telemetry",
        "advert_position": False,
    }


def test_a_contact_never_heard_has_nulls() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Bob", 2)], T)
    document = contact_document(mesh.contacts[KEY])
    nulls = {k for k, v in document.items() if v is None}
    assert nulls == {
        "path_hops",
        "last_advert_at",
        "first_heard_at",
        "heard_at",
        "heard_via",
        "hops",
        "snr_db",
        "rssi_dbm",
        "latitude",
        "longitude",
        "altitude_m",
        "position_at",
        "position_source",
        "position_accuracy_m",
        "position_received_at",
        "advert_carries_position",
        "advert_checked_at",
        "telemetry_state",
        "telemetry_current",
        "position_current",
        "readings_at",
        "battery_v",
        "mcu_temperature_c",
    }
    assert (document["path_known"], document["flags"], document["type"]) == (False, [], "repeater")
    assert (document["telemetry_excluded"], document["readings"]) == (False, {})


def test_the_index_is_sorted_by_last_heard_and_bounded() -> None:
    mesh = Mesh()
    nodes = [Node(f"{i:064x}", HOSTILE * 3, 1, position=Position(1, 1)) for i in range(510)]
    mesh.contacts_read(nodes, T)
    mesh.max_contacts = 510
    mesh.contact_heard(nodes[7].public_key, Heard(T, "advert"))
    document = contacts_document(mesh, protected=[nodes[7].public_key])
    assert len(encode(document)) < INDEX_LIMIT_BYTES + 4096  # below the broker's 64 KiB
    assert document["total"] == 510
    assert document["truncated"] is True
    assert len(encode({"c": document["contacts"]})) <= INDEX_LIMIT_BYTES + 16
    first = document["contacts"][0]
    assert (first["key"], first["protected"], first["heard_at"]) == (
        nodes[7].public_key,
        True,
        iso(T),
    )
    assert document["max"] == 510
    # companions whose record holds a position from their adverts: counted, the first keys listed
    assert (document["advert_position_total"], len(document["advert_position"])) == (510, 16)
    # none checked yet: the record says their adverts may carry it (ADR 0025)
    assert document["advert_carries_position"] == sorted(n.public_key for n in nodes)[:16]
    assert document["advert_carries_position_total"] == 510


def test_the_index_reports_contacts_with_the_remote_cli_flag() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Admin", 1, flags=0x10), Node("b2" * 32, "Bob", 2)], T)
    document = contacts_document(mesh)
    assert (document["remote_cli"], document["remote_cli_total"]) == ([KEY], 1)
    assert (document["advert_position"], document["advert_position_total"]) == ([], 0)
    assert document["advert_carries_position"] == []
    assert document["advert_carries_position_total"] == 0
    assert document["truncated"] is False


def test_heard_nodes_newest_first_without_positions_and_bounded() -> None:
    mesh = Mesh(max_nodes=60)
    for i in range(60):
        node = Node(f"{i:064x}", HOSTILE * 3, 2, position=Position(1, 1))
        mesh.node_heard(node, Heard(T + i, "advert", Signal(i % 4, 1.0, -100)))
    document = nodes_document(mesh)
    assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES
    assert document["total"] == 60
    assert document["truncated"] is True
    newest = document["nodes"][0]
    assert newest["key"] == f"{59:064x}"
    assert "latitude" not in newest
    assert (newest["hops"], newest["type"], newest["heard_via"]) == (3, "repeater", "advert")


def test_statistics_before_and_after_readings() -> None:
    empty = stats_document(None, AirtimeMeter(), None)
    assert all(v is None for k, v in empty.items() if k != "errors")
    assert empty["errors"] == []
    meter = AirtimeMeter()
    meter.add(T - 600, 30)
    meter.add(T, 41)
    readings = RadioReadings(
        codec.Battery(4012, 12, 976),
        codec.CoreStats(4012, 3600, 0x05, 2),
        codec.RadioStats(-118, -97, 6.5, 41, 300),
        codec.PacketStats(120, 30, 10, 20, 70, 50, None),
        T,
    )
    document = stats_document(readings, meter, codec.TuningParams(0, 9000))
    assert document["errors"] == ["queue_full", "start_rx_timeout"]
    assert (document["tx_airtime_1h_s"], document["tx_airtime_window_s"]) == (11, 600)
    assert (document["airtime_factor"], document["duty_cycle_percent"]) == (9.0, 10.0)
    assert (document["noise_floor_dbm"], document["receive_errors"]) == (-118, None)
    assert (document["storage_used_kb"], document["uptime_s"]) == (12, 3600)


def test_history_newest_first_and_bounded_with_hostile_texts() -> None:
    entries = [
        HistoryEntry(T + i, "in" if i % 2 else "out", "contact", HOSTILE * 20, contact_key=KEY)
        for i in range(50)
    ]
    document = history_document(entries)
    assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES
    assert document["total"] == 50
    assert document["truncated"] is True
    assert document["messages"][0]["at"] == iso(T + 49)
    assert json.loads(encode(document))["messages"][0]["text"] == HOSTILE * 20  # data, unchanged


HISTORY_KEYS = [  # the keys of 2.0, then those of 2.1 at the end
    "at",
    "direction",
    "kind",
    "contact_key",
    "channel_index",
    "name",
    "text",
    "state",
    "hops",
    "snr_db",
    "target",
    "id",
    "queued_at",
    "suspect",
    "reason",
]


def test_a_history_entry_has_every_key_with_null_where_unknown() -> None:
    """ADR 0015: every key always present. A received entry names the target it came through;
    a sent one also its delivery id, when it was accepted, whether its acknowledgement was
    suspect and why it failed."""
    received = HistoryEntry(
        T, "in", "channel", "On my way", None, 1, "Carol", None, 1, 6.25, target="family"
    )
    assert list(history_entry(received)) == HISTORY_KEYS
    assert history_entry(received) == {
        "at": iso(T),
        "direction": "in",
        "kind": "channel",
        "contact_key": None,
        "channel_index": 1,
        "name": "Carol",
        "text": "On my way",
        "state": None,
        "hops": 1,
        "snr_db": 6.25,
        "target": "family",
        "id": None,
        "queued_at": None,
        "suspect": None,
        "reason": None,
    }
    sent = HistoryEntry(
        T + 30,
        "out",
        "contact",
        "Back soon",
        contact_key=KEY,
        name="Alice",
        state="confirmed",
        target=None,  # sent through contacts/<key>/send: contact_key holds the key
        id="1a2b3c4d",
        queued_at=T + 2.75,
        suspect=True,
    )
    assert history_entry(sent) | {"at": None} == dict.fromkeys(HISTORY_KEYS) | {
        "direction": "out",
        "kind": "contact",
        "contact_key": KEY,
        "name": "Alice",
        "text": "Back soon",
        "state": "confirmed",
        "id": "1a2b3c4d",
        "queued_at": "2027-01-15T08:00:02+00:00",  # as `at`: UTC, whole seconds
        "suspect": True,
    }
    failed = HistoryEntry(
        T,
        "out",
        "channel",
        "x",
        channel_index=0,
        state="failed",
        target="alerts",
        id="9f8e7d6c",
        reason="channel_not_configured",
    )
    assert history_entry(failed)["reason"] == "channel_not_configured"
    assert history_entry(failed)["suspect"] is None
    assert history_document([])["messages"] == []


def _worst_entries(text_bytes: int, *, received: bool) -> list[HistoryEntry]:
    """The worst cases for the bound: a received direct message from a 31-byte name of control
    characters with a text of control characters (6 bytes each in JSON), or a sent one with
    every key filled, the longest reason and a text of quotes and backslashes (2 bytes each)."""
    if received:
        text, name = "\x01" * text_bytes, "\x02" * 31
        return [
            HistoryEntry(
                T + i,
                "in",
                "contact",
                text,
                KEY,
                name=name,
                hops=63,
                snr_db=-12.25,
                target="a" * 32,
            )
            for i in range(50)
        ]
    text = ('"\\' * text_bytes)[:text_bytes]
    return [
        HistoryEntry(
            T + i,
            "out",
            "contact",
            text,
            KEY,
            name="n" * 32,
            state="failed",
            target="t" * 32,
            id=f"{i:08x}",
            queued_at=T + i - 600,
            suspect=None,
            reason="exceeds_airtime_burst",
        )
        for i in range(50)
    ]


@pytest.mark.parametrize("received", [True, False], ids=["received", "sent"])
def test_the_whole_history_document_stays_within_its_bound(received: bool) -> None:
    """#111: the bound counts the whole document, its head too, as `nodes_document` does: from
    texts that fill it short of a whole entry to texts that leave it with room for one."""
    for text_bytes in range(0, 161, 3):
        document = history_document(_worst_entries(text_bytes, received=received))
        assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES, text_bytes
        assert document["total"] == 50
        kept = document["messages"]
        assert document["truncated"] is (len(kept) < 50)
        assert [m["at"] for m in kept] == [iso(T + 49 - i) for i in range(len(kept))]


def test_a_history_near_its_bound_stays_within_it_whole_or_cut() -> None:
    """Entries that fill the bound to the byte without the head: the document keeps them all
    only if the head fits too (2.0 went 43 bytes over then, and 42 when cut)."""
    whole = set()
    for size in range(40, 160):
        entries = [HistoryEntry(T + i, "in", "channel", "x" * size) for i in range(50)]
        document = history_document(entries)
        assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES, size
        whole.add(document["truncated"] is False)
    assert whole == {True, False}


PENDING_KEYS = [
    "id",
    "queued_at",
    "target",
    "kind",
    "contact_key",
    "channel_index",
    "name",
    "text",
    "step",
    "attempt",
    "max_attempts",
]


def test_a_pending_entry_has_every_key_with_null_where_unknown() -> None:
    """A message accepted and not final yet (`pending`). A direct message sent through
    `contacts/<key>/send` has no target (its key is `contact_key`); one kept by 2.0 has no
    `queued_at`."""
    direct = PendingEntry(
        "1a2b3c4d",
        T + 2.75,
        None,
        "contact",
        KEY,
        None,
        "Alice",
        "Back soon #0f1e2d3c",
        "sent",
        1,
        3,
    )
    assert list(pending_entry(direct)) == PENDING_KEYS
    assert pending_entry(direct) == {
        "id": "1a2b3c4d",
        "queued_at": "2027-01-15T08:00:02+00:00",
        "target": None,
        "kind": "contact",
        "contact_key": KEY,
        "channel_index": None,
        "name": "Alice",
        "text": "Back soon #0f1e2d3c",
        "step": "sent",
        "attempt": 1,
        "max_attempts": 3,
    }
    channel = PendingEntry(
        "9f8e7d6c", None, "family", "channel", None, 1, "family", "Hi", "queued", 0, 1
    )
    assert pending_entry(channel) | {"id": None} == dict.fromkeys(PENDING_KEYS) | {
        "target": "family",
        "kind": "channel",
        "channel_index": 1,
        "name": "family",
        "text": "Hi",
        "step": "queued",
        "attempt": 0,
        "max_attempts": 1,
    }
    assert PENDING_STEPS == ("queued", "sent")


def test_the_pending_document_lists_the_messages_oldest_first() -> None:
    """In the order the outbox gives them (accepted first, first), every key there when empty."""
    assert pending_document([]) == {"total": 0, "truncated": False, "messages": []}
    entries = [
        PendingEntry(
            f"{i:08x}", T + i, "alerts", "channel", None, 0, "alerts", f"m{i}", "queued", 0, 1
        )
        for i in range(3)
    ]
    document = pending_document(entries)
    assert (document["total"], document["truncated"]) == (3, False)
    assert [m["id"] for m in document["messages"]] == ["00000000", "00000001", "00000002"]


def _worst_pending(text_bytes: int, count: int) -> list[PendingEntry]:
    """Every key filled at its longest: a 32-character alias, a 31-byte name of control
    characters (6 bytes each in JSON), a text of quotes and backslashes (2 bytes each)."""
    text = ('"\\' * text_bytes)[:text_bytes]
    return [
        PendingEntry(
            f"{i:08x}", T + i, "t" * 32, "contact", KEY, None, "\x02" * 31, text, "sent", 2, 3
        )
        for i in range(count)
    ]


def test_the_whole_pending_document_stays_within_8_kib() -> None:
    """Bounded by bytes, as ADR 0015 asks of every list: `total` counts every message and
    `truncated` is true exactly when some were left out; the oldest stay."""
    cut = set()
    for text_bytes in range(0, 161, 4):
        for count in (1, 10, 21, 1001):  # the largest queue and the message in flight
            document = pending_document(_worst_pending(text_bytes, count))
            assert len(encode(document)) <= PENDING_LIMIT_BYTES, (text_bytes, count)
            assert document["total"] == count
            kept = document["messages"]
            assert document["truncated"] is (len(kept) < count)
            assert [m["id"] for m in kept] == [f"{i:08x}" for i in range(len(kept))]
            cut.add(document["truncated"])
    assert cut == {True, False}
    assert PENDING_LIMIT_BYTES == 8 * 1024


def test_a_pending_document_near_its_bound_stays_within_it_whole_or_cut() -> None:
    """Entries that fill the bound to the byte without the head: whole only if the head fits."""
    whole = set()
    for size in range(100, 300):
        entries = [
            PendingEntry(f"{i:08x}", T, "a", "channel", None, 0, "a", "x" * size, "queued", 0, 1)
            for i in range(20)
        ]
        document = pending_document(entries)
        assert len(encode(document)) <= PENDING_LIMIT_BYTES, size
        whole.add(document["truncated"] is False)
    assert whole == {True, False}


def _settings() -> dict[str, dict[str, Any]]:
    """Sections as config_view gives them, with lists of their largest length."""
    return {
        "messages": {"ignore_channels": list(range(256)), "publish_channels": "targets"},
        "homeassistant": {"contact_entity_prefix": "x" * 20, "hidden_targets": 1000},
    }


def test_a_config_document_near_its_bound_stays_within_it_whole_or_cut() -> None:
    """Targets that fill the bound to the byte without the head: whole only if the head fits;
    `total` counts every target, `truncated` is true exactly when the last were left out."""
    whole = set()
    for size in range(150, 260):
        targets = [
            TargetSetting(f"t{i:02d}", "contact", None, KEY, "p" * size, "", False, True, 150)
            for i in range(40)
        ]
        document = config_document(T, targets, _settings())
        assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES, size
        listed = document["targets"]
        assert listed["total"] == 40
        assert listed["truncated"] is (len(listed["items"]) < 40)
        assert [t["alias"] for t in listed["items"]] == [t.alias for t in targets][
            : len(listed["items"])
        ]
        whole.add(listed["truncated"] is False)
    assert whole == {True, False}


def test_a_target_shows_the_start_of_its_key_only() -> None:
    target = TargetSetting("alice", "contact", None, KEY, "", "", True, False, 150)
    shown = config_document(T, [target], {})
    assert shown == {
        "started_at": iso(T),
        "targets": {
            "total": 1,
            "truncated": False,
            "items": [
                {
                    "alias": "alice",
                    "kind": "contact",
                    "channel_index": None,
                    "key_start": KEY[:8],
                    "prefix": "",
                    "suffix": "",
                    "alert": True,
                    "hidden": False,
                    "room": 150,
                }
            ],
        },
    }
    assert KEY[:9] not in encode(shown).decode()


def test_flags_and_bounds_helpers() -> None:
    assert flag_names(0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0xC0) == [
        "favourite",
        "telemetry_base",
        "telemetry_location",
        "telemetry_environment",
        "remote_cli",
        "0xc0",
    ]
    kept, total = bounded(iter([{"a": "x" * 10}, {"a": "y" * 100}, {"a": "z"}]), 30)
    assert (kept, total) == ([{"a": "x" * 10}], 3)  # the order is kept: nothing after a cut
    assert iso(None) is None
    assert Fix(Position(1, 2), T, "advert").altitude_m is None


def test_contact_management_in_the_documents() -> None:
    mesh = Mesh()
    mesh.contacts_read(
        [Node(KEY, "Alice", 1, last_advert=0), Node("b2" * 32, "Bob", 1, last_advert=5)], T
    )
    alice = contact_document(mesh.contacts[KEY], manage=True)
    assert (alice["removable"], alice["verified"]) == (True, False)  # added from a link
    assert contact_document(mesh.contacts[KEY], protected=True, manage=True)["removable"] is False
    assert contact_document(mesh.contacts["b2" * 32])["removable"] is False  # not managed
    assert contacts_document(mesh, manage=True)["manage"] is True
    assert contacts_document(mesh, manage=True)["manage_reason"] is None
    off = contacts_document(mesh, manage=False, manage_reason="management_off")
    assert (off["manage"], off["manage_reason"]) == (False, "management_off")


def test_look_alike_nodes_are_flagged() -> None:
    """A heard node with the prefix of a contact, a protected key or another heard node."""
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Alice", 1)], T)
    twin, other, third = "a1" * 6 + "ee" * 26, "b2" * 6 + "00" * 26, "b2" * 6 + "11" * 26
    for key in (twin, other, third, "c3" * 6 + "00" * 26, "d4" * 32):
        mesh.node_heard(Node(key, "x", 1), Heard(T, "advert"))
    document = nodes_document(mesh, protected=["c3" * 6 + "ff" * 26])
    flagged = {n["key"] for n in document["nodes"] if n["look_alike"]}
    assert flagged == {twin, other, third, "c3" * 6 + "00" * 26}
    assert document["look_alike"] == sorted(flagged)
    assert document["look_alike_total"] == 4


def test_a_heard_node_with_a_taken_name_is_flagged() -> None:
    """SEC-007: a heard node whose name, as a person reads it, a contact has, the gateway goes by
    or another heard node has: in each entry, and the keys at the top, also with
    `heard_nodes = off`."""
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Alice", 1)], T)
    impostor, gateway, twin_a, twin_b, plain = (f"{n:064x}" for n in range(1, 6))
    for key, name in (
        (impostor, "\u0430lice"),
        (gateway, "MESH radio"),
        (twin_a, "Hill"),
        (twin_b, "hill"),
        (plain, "Dave"),
    ):
        mesh.node_heard(Node(key, name, 1), Heard(T, "advert"))
    document = nodes_document(mesh, reserved={"Mesh radio"})
    taken = {impostor, gateway, twin_a, twin_b}
    assert {n["key"] for n in document["nodes"] if n["name_taken"]} == taken
    assert (document["name_taken"], document["name_taken_total"]) == (sorted(taken), 4)
    off = nodes_document(mesh, reserved={"Mesh radio"}, heard="off")
    assert (off["nodes"], off["name_taken"]) == ([], sorted(taken))
    assert name_taken_nodes(mesh) == {impostor, twin_a, twin_b}  # nothing reserved


def test_a_name_with_an_invisible_character_of_any_category_is_taken() -> None:
    """#89: a variation selector, a Hangul filler or a braille blank does not make a name
    another one: the node is marked as one whose name is taken."""
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Bob", 1)], T)
    imitations = [f"{n:064x}" for n in range(1, 4)]
    for key, name in zip(imitations, ("Bob\ufe0f", "B\u3164ob", "Bob\u2800"), strict=True):
        mesh.node_heard(Node(key, name, 1), Heard(T, "advert"))
    assert name_taken_nodes(mesh) == set(imitations)


def test_heard_nodes_keys_or_off_publish_less_of_strangers() -> None:
    """`[contacts] heard_nodes` (SEC-015): `keys` keeps a stranger's key, type and look-alike
    mark, not its name, times or signal; `off` lists none, only the look-alikes' keys."""
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Alice", 1)], T)
    twin = KEY[:12] + "ee" * 26
    mesh.node_heard(Node("d4" * 32, "Dave", 2), Heard(T, "advert", Signal(1, 5.5, -90)))
    mesh.node_heard(Node(twin, "Alice", 1), Heard(T + 1, "advert", Signal(0, 9.0, -60)))
    full = nodes_document(mesh)
    keys = nodes_document(mesh, heard="keys")
    assert [n["key"] for n in keys["nodes"]] == [n["key"] for n in full["nodes"]]
    newest = keys["nodes"][0]
    assert (newest["key"], newest["type"], newest["look_alike"]) == (twin, "chat", True)
    assert [k for k, v in newest.items() if v is None] == [
        "name",
        "first_heard_at",
        "heard_at",
        "heard_via",
        "hops",
        "snr_db",
        "rssi_dbm",
    ]
    assert "Dave" not in json.dumps(keys)
    off = nodes_document(mesh, heard="off")
    assert (off["total"], off["truncated"], off["nodes"]) == (0, False, [])
    assert (off["look_alike"], off["look_alike_total"]) == ([twin], 1)


def test_the_nodes_document_stays_within_its_bound_with_its_warnings() -> None:
    mesh = Mesh(max_nodes=200)
    for i in range(200):  # every one a look-alike of the next
        key = f"{i // 2:012x}" + f"{i:052x}"
        mesh.node_heard(Node(key, HOSTILE * 3, 1), Heard(T, "advert"))
    document = nodes_document(mesh)
    assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES
    assert document["truncated"] is True
    assert len(document["look_alike"]) == 16
    assert document["look_alike_total"] == 200


def test_every_contact_event_has_every_key() -> None:
    event = contact_event("refused", None, request="add", request_id="x1", reason="invalid_link")
    assert event == {
        "event_type": "refused",
        "key": None,
        "prefix": None,
        "name": None,
        "type": None,
        "request": "add",
        "id": "x1",
        "reason": "invalid_link",
    }
    claimed = contact_event("added", KEY, name="Hill", node_type=3)
    assert (claimed["prefix"], claimed["type"]) == (KEY[:12], "room")


# --- telemetry ----------------------------------------------------------------------------------


def test_a_contact_document_carries_the_readings_of_the_newest_answer() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Alice", 1)], T)
    named = lpp.named(lpp.decode(LPP_BME280))
    mesh.contact_answered(KEY, T + 5, Position(50.12345, 8.56775), 120.5, Readings(T + 5, named))
    document = contact_document(mesh.contacts[KEY], telemetry_state="answering")
    assert {
        k: document[k]
        for k in (
            "telemetry_state",
            "telemetry_excluded",
            "telemetry_current",
            "position_current",
            "readings_at",
            "battery_v",
            "mcu_temperature_c",
        )
    } == {
        "telemetry_state": "answering",
        "telemetry_excluded": False,
        "telemetry_current": True,
        "position_current": True,
        "readings_at": "2027-01-15T08:00:05+00:00",
        "battery_v": 4.12,
        "mcu_temperature_c": 23.4,
    }
    assert json.loads(encode(document))["readings"] == {
        "ch2_temperature_c": 21.3,
        "ch2_humidity_percent": 55.5,
        "ch2_barometric_pressure_hpa": 1013.2,
        "ch2_altitude_m": 120.0,
    }
    assert list(document["readings"]) == list(named.readings)  # in the answer's order
    document["readings"]["ch2_altitude_m"] = 0.0
    assert named.readings["ch2_altitude_m"] == 120.0  # a copy: the answer stays as it came


@pytest.mark.parametrize("state", [*STATES, None])
@pytest.mark.parametrize("source", ["telemetry", "advert", None])
def test_whether_answers_and_position_are_current(
    state: TelemetryState | None, source: str | None
) -> None:
    """`position_current` speaks of a position from telemetry only; `null` for an advert's."""
    fix = Fix(Position(1, 2), T, source) if source else None
    tracked = Tracked(Node(KEY, "Alice", 1), fix=fix)
    document = contact_document(tracked, telemetry_state=state)
    assert document["telemetry_state"] == state
    assert document["telemetry_current"] == (None if state is None else state in CURRENT)
    expected = state == "answering" if source == "telemetry" else None
    assert document["position_current"] == expected


@pytest.mark.parametrize("before", [*STATES, None])
@pytest.mark.parametrize("source", ["telemetry", "advert", None])
def test_a_document_restated_in_another_state_is_the_one_built_in_it(
    before: TelemetryState | None, source: str | None
) -> None:
    """Before the first table read of a run only the stored document is there (ADR 0024)."""
    fix = Fix(Position(1, 2), T, source) if source else None
    readings = Readings(T, lpp.named(lpp.decode(LPP_BME280)))
    heard = Heard(T, "telemetry", Signal(1, 6.25, -97))
    tracked = Tracked(Node(KEY, "Alice", 1, path_hops=1), T, heard, fix, T, readings=readings)
    stored = contact_document(tracked, protected=True, manage=True, telemetry_state=before)
    for state in (*STATES, None):
        built = contact_document(tracked, protected=True, manage=True, telemetry_state=state)
        assert restated(stored, state) == built


@pytest.mark.parametrize("advert", [False, True])
def test_a_document_restated_as_excluded_forgets_what_an_exclusion_forgets(advert: bool) -> None:
    """As far as the stored document shows it: the position the contact's record carries comes
    back with the first table read, so here the answer carried one only without it."""
    mesh = Mesh()
    advertised = Position(53.55, 9.99) if advert else None
    mesh.contacts_read([Node(KEY, "Alice", 1, position=advertised)], T)
    named = lpp.named(lpp.decode(LPP_BME280))
    position = None if advert else Position(50.12345, 8.56775)
    mesh.contact_answered(KEY, T + 5, position, 120.5, Readings(T + 5, named))
    mesh.restore_received(KEY, T + 5)  # an earlier answer carried one
    stored = contact_document(mesh.contacts[KEY], telemetry_state="answering")
    mesh.forget_telemetry(KEY, T + 10)
    excluded = contact_document(mesh.contacts[KEY], telemetry_state="excluded", excluded=True)
    assert restated(stored, "excluded", excluded=True) == excluded
    assert excluded["position_source"] == ("advert" if advert else None)
    included = contact_document(mesh.contacts[KEY], telemetry_state="waiting")
    assert restated(excluded, "waiting") == included  # what it forgot stays forgotten


def test_only_a_contact_that_is_asked_is_excluded() -> None:
    """A contact of a type that is asked has a telemetry state (companions, with `ask_types`
    also sensors, EXT-005); one that is never asked has none and is not excluded."""
    alice = contact_document(
        Tracked(Node(KEY, "Alice", 1)), telemetry_state="excluded", excluded=True
    )
    assert alice["telemetry_excluded"] is True
    sensor = contact_document(
        Tracked(Node(KEY, "Shed", 4)), telemetry_state="excluded", excluded=True
    )
    assert sensor["telemetry_excluded"] is True
    repeater = contact_document(Tracked(Node(KEY, "Hill", 2)), excluded=True)
    assert repeater["telemetry_excluded"] is False


def test_the_index_lists_the_excluded_keys_also_those_not_on_the_radio() -> None:
    mesh = Mesh()
    stored = [f"{i:064x}" for i in range(20)]
    mesh.contacts_read([Node(stored[19], "Alice", 1), Node("b2" * 32, "Bob", 1)], T)
    document = contacts_document(mesh, excluded=frozenset(stored))
    assert document["telemetry_excluded"] == sorted(stored)[:16]
    assert document["telemetry_excluded_total"] == 20
    empty = contacts_document(mesh)
    assert (empty["telemetry_excluded"], empty["telemetry_excluded_total"]) == ([], 0)
    most = [f"{i:064x}" for i in range(1024)]  # the most the gateway stores
    grown = len(encode(contacts_document(mesh, excluded=most))) - len(encode(empty))
    assert grown <= 1.1 * 1024  # outside the bound of the entries, and small


def test_a_telemetry_event_has_every_key() -> None:
    assert telemetry_event("unanswered", KEY, Node(KEY, "Alice", 1), "away") == {
        "event_type": "unanswered",
        "key": KEY,
        "prefix": "a1a1a1a1a1a1",
        "name": "Alice",
        "state": "away",
    }
    assert telemetry_event("answered", KEY, None, "no_position")["name"] is None


def test_the_largest_contact_document_stays_within_the_attribute_bound() -> None:
    """52 readings of the longest keys (156 bytes of LPP at 3 bytes each), the widest values and
    a name of 32 bytes in 4-byte characters."""
    readings = {f"ch255_barometric_pressure_{n}_hpa": -4294967.295 for n in range(2, 54)}
    node = Node(
        KEY,
        "\U0001f600" * 8,
        1,
        flags=0xFF,
        path_hops=64,
        last_advert=int(T),
        position=Position(-89.99999, -179.99999),
    )
    tracked = Tracked(
        node,
        T,
        Heard(T, "telemetry", Signal(64, -32.25, -128)),
        Fix(Position(-89.99999, -179.99999), T, "telemetry", -8388.608),
        T,
        readings=Readings(T, Named(-327.68, -3276.8, readings)),
        advert=AdvertCheck(int(T), True),
    )
    document = contact_document(tracked, protected=True, manage=True, telemetry_state="no_answer")
    assert 2048 < len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES


# --- positions in adverts (ADR 0018, ADR 0025) -------------------------------------------------

LAST = int(T) - 60  # the contact's clock at its newest advert, as the radio's record has it
HERE = Position(53.55, 9.99)
CHECK_KEYS = ("advert_position", "advert_carries_position", "advert_checked_at")


def _check_keys(
    check: AdvertCheck | None,
    position: Position | None = HERE,
    *,
    last_advert: int = LAST,
    node_type: int = 1,
) -> tuple[Any, ...]:
    """`advert_position`, `advert_carries_position` and `advert_checked_at` of a contact whose
    record has this position and time, with this check."""
    node = Node(KEY, "Alice", node_type, last_advert=last_advert, position=position)
    document = contact_document(Tracked(node, advert=check))
    assert advert_carries_position(Tracked(node, advert=check)) == document[CHECK_KEYS[1]]
    return tuple(document[k] for k in CHECK_KEYS)


def test_a_checked_newest_advert_says_whether_it_carries_the_position() -> None:
    """`advert_position` stays the record's: it keeps a position after sharing stops, and 0, 0
    clears it (ADR 0025)."""
    assert _check_keys(AdvertCheck(LAST, True)) == (True, True, iso(LAST))
    assert _check_keys(AdvertCheck(LAST, False)) == (True, False, iso(LAST))  # an older one kept
    assert _check_keys(AdvertCheck(LAST, True), None) == (False, True, iso(LAST))  # 0, 0
    assert _check_keys(AdvertCheck(LAST, False), None) == (False, False, iso(LAST))
    # a clock at 0 gives no time, as for `last_advert_at`
    assert _check_keys(AdvertCheck(0, True), None, last_advert=0) == (False, True, None)


def test_before_its_first_check_the_record_decides() -> None:
    """A position in the record came from an earlier advert, which may still carry it: it errs
    to warn (ADR 0025)."""
    assert _check_keys(None) == (True, True, None)
    assert _check_keys(None, None) == (False, False, None)


def test_while_its_newest_advert_is_not_checked_the_value_errs_to_warn() -> None:
    """A newer advert than the check (one the gateway missed, or made while it was stopped): the
    last check's flag, or True when the record holds a position (ADR 0025: she
    started sharing, or her push was dropped)."""
    older = LAST - 600
    assert _check_keys(AdvertCheck(older, False)) == (True, True, iso(older))
    assert _check_keys(AdvertCheck(older, False), None) == (False, False, iso(older))
    assert _check_keys(AdvertCheck(older, True), None) == (False, True, iso(older))
    assert _check_keys(AdvertCheck(older, True)) == (True, True, iso(older))


def test_a_check_newer_than_the_record_decides() -> None:
    """A companion the gateway heard and then added: the record the add wrote has no position and
    a protected contact's the time 0 (ADR 0025); the heard check decides."""
    assert _check_keys(AdvertCheck(LAST, False), last_advert=0) == (True, False, iso(LAST))
    mesh = Mesh()
    mesh.node_heard(Node(KEY, "Bob", 1, last_advert=LAST, position=HERE), Heard(T, "advert"))
    mesh.advert_heard(KEY, AdvertCheck(LAST, True))
    mesh.contacts_read([Node(KEY, "Bob", 1, last_advert=0)], T + 5)
    document = contact_document(mesh.contacts[KEY])
    assert tuple(document[k] for k in CHECK_KEYS) == (False, True, iso(LAST))


@pytest.mark.parametrize("node_type", [2, 3, 4])
def test_other_node_types_are_not_checked(node_type: int) -> None:
    """Repeaters, room servers and sensors share their position by design (ADR 0025)."""
    expected = (True, None, None)
    assert _check_keys(AdvertCheck(LAST, True), node_type=node_type) == expected
    assert _check_keys(None, node_type=node_type) == expected


def test_the_index_lists_the_households_companions_whose_adverts_carry_the_position() -> None:
    """Companions not excluded from telemetry (ADR 0025): a neighbour that shares on purpose
    does not count. `advert_position` stays the records' (ADR 0025)."""
    mesh = Mesh()
    carrying = [f"{i:064x}" for i in range(20)]
    excluded, stopped, unchecked = "e1" * 32, "d1" * 32, "c1" * 32
    nodes = [Node(key, "Alice", 1, last_advert=LAST, position=HERE) for key in carrying]
    nodes += [
        Node(key, "Bob", 1, last_advert=LAST, position=HERE)
        for key in (excluded, stopped, unchecked)
    ]
    nodes += [Node("b2" * 32, "Hill", 2, last_advert=LAST, position=HERE)]
    mesh.contacts_read(nodes, T)
    for key in [*carrying, excluded]:
        mesh.advert_checked(key, AdvertCheck(LAST, True), T)
    mesh.advert_checked(stopped, AdvertCheck(LAST, False), T)  # its record keeps the position
    document = contacts_document(mesh, excluded={excluded, "f1" * 32})
    expected = sorted([*carrying, unchecked])
    assert document["advert_carries_position"] == expected[:16]
    assert document["advert_carries_position_total"] == 21
    assert document["advert_position_total"] == 23  # every companion's record holds one
    assert document["telemetry_excluded"] == [excluded, "f1" * 32]  # unchanged


@pytest.mark.parametrize(("node_type", "position"), [(1, HERE), (1, None), (2, HERE)])
def test_a_document_stored_by_1_1_0a2_is_restated_with_the_keys_of_the_check(
    node_type: int, position: Position | None
) -> None:
    """Before the first table read the stored document is all there is, and 1.1.0a2 stored it
    without these keys: what the gateway says without a check (ADR 0025)."""
    state: TelemetryState | None = "answering" if node_type == 1 else None
    tracked = Tracked(Node(KEY, "Alice", node_type, last_advert=LAST, position=position))
    built = contact_document(tracked, telemetry_state=state)
    stored = {k: v for k, v in built.items() if k not in CHECK_KEYS[1:]}
    assert restated(stored, state) == built
    assert restated(stored, state, excluded=True).keys() == built.keys()


def test_a_restated_document_keeps_its_check() -> None:
    node = Node(KEY, "Alice", 1, last_advert=LAST, position=HERE)
    stored = contact_document(Tracked(node, advert=AdvertCheck(LAST, False)))
    again = restated(stored, "waiting")
    assert tuple(again[k] for k in CHECK_KEYS) == (True, False, iso(LAST))
