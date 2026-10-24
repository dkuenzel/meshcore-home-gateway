"""The providers' documents (views.py, ADR 0015): shapes, keys always present, sizes bounded."""

import json

from meshcore_home_gateway.core.mesh import AirtimeMeter, Fix, Heard, Mesh, Node, Signal
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.views import (
    ATTRIBUTE_LIMIT_BYTES,
    INDEX_LIMIT_BYTES,
    HistoryEntry,
    RadioReadings,
    bounded,
    contact_document,
    contacts_document,
    encode,
    flag_names,
    history_document,
    iso,
    nodes_document,
    stats_document,
)

T = 1_800_000_000.0
KEY = "a1" * 32
HOSTILE = '<img src=x onerror=alert(1)>\x1b[2J"\\\x00' + chr(0x2028)  # inflate JSON


def test_a_contact_document_has_every_key_and_iso_times() -> None:
    mesh = Mesh()
    node = Node(KEY, "Alice", 1, flags=0x01 | 0x10, path_hops=2, last_advert=int(T) - 60,
                position=Position(53.55, 9.99))  # fmt: skip
    mesh.contacts_read([node], T)
    mesh.contact_heard(KEY, Heard(T + 5, "message", Signal(2, 6.25, -97)))
    document = contact_document(mesh.contacts[KEY], protected=True)
    assert document == {
        "key": KEY, "prefix": "a1a1a1a1a1a1", "name": "Alice", "type": "chat",
        "flags": ["favourite", "remote_cli"], "favourite": True, "remote_cli": True,
        "protected": True, "path_known": True, "path_hops": 2,
        "last_advert_at": "2027-01-15T07:59:00+00:00",
        "first_heard_at": "2027-01-15T08:00:05+00:00",
        "heard_at": "2027-01-15T08:00:05+00:00", "heard_via": "message", "hops": 2,
        "snr_db": 6.25, "rssi_dbm": -97, "latitude": 53.55, "longitude": 9.99,
        "altitude_m": None, "position_at": "2027-01-15T07:59:00+00:00",
        "position_source": "advert", "advert_position": True,
    }  # fmt: skip


def test_a_contact_never_heard_has_nulls() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Bob", 2)], T)
    document = contact_document(mesh.contacts[KEY])
    nulls = {k for k, v in document.items() if v is None}
    assert nulls == {
        "path_hops", "last_advert_at", "first_heard_at", "heard_at", "heard_via", "hops",
        "snr_db", "rssi_dbm", "latitude", "longitude", "altitude_m", "position_at",
        "position_source",
    }  # fmt: skip
    assert (document["path_known"], document["flags"], document["type"]) == (False, [], "repeater")


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
        nodes[7].public_key, True, iso(T),
    )  # fmt: skip
    assert document["max"] == 510
    # companions whose adverts carry a position: counted, the first keys listed
    assert (document["advert_position_total"], len(document["advert_position"])) == (510, 16)


def test_the_index_reports_contacts_with_the_remote_cli_flag() -> None:
    mesh = Mesh()
    mesh.contacts_read([Node(KEY, "Admin", 1, flags=0x10), Node("b2" * 32, "Bob", 2)], T)
    document = contacts_document(mesh)
    assert (document["remote_cli"], document["remote_cli_total"]) == ([KEY], 1)
    assert (document["advert_position"], document["advert_position_total"]) == ([], 0)
    assert document["truncated"] is False


def test_heard_nodes_newest_first_without_positions_and_bounded() -> None:
    mesh = Mesh(max_nodes=60)
    for i in range(60):
        node = Node(f"{i:064x}", HOSTILE * 3, 2, position=Position(1, 1))
        mesh.node_heard(node, Heard(T + i, "advert", Signal(i % 4, 1.0, -100)))
    document = nodes_document(mesh)
    assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES + 64
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
        codec.Battery(4012, 12, 976), codec.CoreStats(4012, 3600, 0x05, 2),
        codec.RadioStats(-118, -97, 6.5, 41, 300),
        codec.PacketStats(120, 30, 10, 20, 70, 50, None), T,
    )  # fmt: skip
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
    assert len(encode(document)) <= ATTRIBUTE_LIMIT_BYTES + 64
    assert document["total"] == 50
    assert document["truncated"] is True
    assert document["messages"][0]["at"] == iso(T + 49)
    assert json.loads(encode(document))["messages"][0]["text"] == HOSTILE * 20  # data, unchanged


def test_flags_and_bounds_helpers() -> None:
    assert flag_names(0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0xC0) == [
        "favourite", "telemetry_base", "telemetry_location", "telemetry_environment",
        "remote_cli", "0xc0",
    ]  # fmt: skip
    kept, total = bounded(iter([{"a": "x" * 10}, {"a": "y" * 100}, {"a": "z"}]), 30)
    assert (kept, total) == ([{"a": "x" * 10}], 3)  # the order is kept: nothing after a cut
    assert iso(None) is None
    assert Fix(Position(1, 2), T, "advert").altitude_m is None
