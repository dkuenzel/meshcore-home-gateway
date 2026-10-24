"""Providers: the documents of the MQTT API (ADR 0015), built from the gateway's state.

Pure functions. Every key is always present (`null` when unknown), times are ISO 8601 in UTC,
units are in the key names, machine values are lower case, and texts from the mesh are plain data.
Lists are bounded by their serialised size and carry `total` and `truncated`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from meshcore_home_gateway.core import profile
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS
from meshcore_home_gateway.core.mesh import AirtimeMeter, Mesh, Tracked, type_name
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import AutoAdd, ContactFlag, TelemetryMode

INDEX_LIMIT_BYTES = 32 * 1024  # the contact index; Home Assistant reads only its count
ATTRIBUTE_LIMIT_BYTES = 15 * 1024  # documents read into attributes: below the recorder's 16 KiB
WARNING_KEYS = 16  # keys listed per warning in the contact index; the total is counted
_FLAGS = {
    ContactFlag.FAVOURITE: "favourite",
    ContactFlag.TELEMETRY_BASE: "telemetry_base",
    ContactFlag.TELEMETRY_LOCATION: "telemetry_location",
    ContactFlag.TELEMETRY_ENVIRONMENT: "telemetry_environment",
    ContactFlag.REMOTE_CLI: "remote_cli",
}
_ERRORS = {0x01: "queue_full", 0x02: "cad_timeout", 0x04: "start_rx_timeout"}  # Dispatcher.h
_AUTO_ADD = {AutoAdd.CHAT: "chat", AutoAdd.REPEATER: "repeater", AutoAdd.ROOM_SERVER: "room",
             AutoAdd.SENSOR: "sensor"}  # fmt: skip


def encode(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


def flag_names(flags: int) -> list[str]:
    """Names of a contact's flags; unknown bits as one hex value."""
    names = [name for flag, name in _FLAGS.items() if flags & flag]
    unknown = flags & ~sum(_FLAGS)
    return [*names, f"0x{unknown:02x}"] if unknown else names


def bounded(
    entries: Iterable[dict[str, Any]], limit_bytes: int
) -> tuple[list[dict[str, Any]], int]:
    """The entries, in order, as long as their encoded size stays within the limit; and the total
    number of entries."""
    kept: list[dict[str, Any]] = []
    size, total = 0, 0
    for entry in entries:
        total += 1
        size += len(encode(entry)) + 1
        if size <= limit_bytes:
            kept.append(entry)
    return kept, total


# --- contacts -----------------------------------------------------------------------------------


def _heard(tracked: Tracked) -> dict[str, Any]:
    heard = tracked.heard
    signal = heard.signal if heard else None
    return {
        "heard_at": iso(heard.at) if heard else None,
        "heard_via": heard.how if heard else None,
        "hops": signal.hops if signal else None,
        "snr_db": signal.snr_db if signal else None,
        "rssi_dbm": signal.rssi_dbm if signal else None,
    }


def contact_document(tracked: Tracked, *, protected: bool = False) -> dict[str, Any]:
    """`contacts/<key>`: one contact of the radio and what the gateway learned about it."""
    node, fix = tracked.node, tracked.fix
    return {
        "key": node.public_key,
        "prefix": node.public_key[:PREFIX_HEX_DIGITS],
        "name": node.name,
        "type": type_name(node.node_type),
        "flags": flag_names(node.flags),
        "favourite": bool(node.flags & ContactFlag.FAVOURITE),
        "remote_cli": bool(node.flags & ContactFlag.REMOTE_CLI),
        "protected": protected,
        "path_known": node.path_hops is not None,
        "path_hops": node.path_hops,
        "last_advert_at": iso(node.last_advert) if node.last_advert else None,
        "first_heard_at": iso(tracked.first_heard),
        **_heard(tracked),
        "latitude": fix.position.latitude if fix else None,
        "longitude": fix.position.longitude if fix else None,
        "altitude_m": fix.altitude_m if fix else None,
        "position_at": iso(fix.at) if fix else None,
        "position_source": fix.source if fix else None,
        "advert_position": node.position is not None,  # its adverts carried a position
    }


def contacts_document(mesh: Mesh, *, protected: Iterable[str] = ()) -> dict[str, Any]:
    """`contacts`: the index of the radio's contact table, most recently heard first."""
    keep = set(protected)

    def heard_at(tracked: Tracked) -> float:
        return tracked.heard.at if tracked.heard else 0.0

    ordered = sorted(mesh.contacts.values(), key=lambda t: (-heard_at(t), t.node.name))
    entries = (
        {
            "key": t.node.public_key,
            "name": t.node.name,
            "type": type_name(t.node.node_type),
            "heard_at": iso(t.heard.at) if t.heard else None,
            "protected": t.node.public_key in keep,
        }
        for t in ordered
    )
    kept, total = bounded(entries, INDEX_LIMIT_BYTES)
    contacts = mesh.contacts.items()
    # contacts that may run the radio's command line over the air (flag 0x10)
    remote_cli = sorted(k for k, t in contacts if t.node.flags & ContactFlag.REMOTE_CLI)
    # companions whose adverts carried a position: everyone in range can see it
    shared = sorted(k for k, t in contacts if t.node.position is not None and t.node.node_type == 1)
    return {
        "total": total,
        "max": mesh.max_contacts,
        "remote_cli": remote_cli[:WARNING_KEYS],
        "remote_cli_total": len(remote_cli),
        "advert_position": shared[:WARNING_KEYS],
        "advert_position_total": len(shared),
        "truncated": len(kept) < total,
        "contacts": kept,
    }


def nodes_document(mesh: Mesh) -> dict[str, Any]:
    """`nodes`: nodes the radio heard but did not add, most recently heard first; no positions."""
    entries = (
        {
            "key": t.node.public_key,
            "prefix": t.node.public_key[:PREFIX_HEX_DIGITS],
            "name": t.node.name,
            "type": type_name(t.node.node_type),
            "first_heard_at": iso(t.first_heard),
            **_heard(t),
        }
        for t in reversed(mesh.nodes.values())
    )
    kept, total = bounded(entries, ATTRIBUTE_LIMIT_BYTES)
    return {"total": total, "truncated": len(kept) < total, "nodes": kept}


# --- radio --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RadioReadings:
    battery: codec.Battery
    core: codec.CoreStats | None
    radio: codec.RadioStats | None
    packets: codec.PacketStats | None
    read_at: float


def stats_document(
    readings: RadioReadings | None, meter: AirtimeMeter, tuning: codec.TuningParams | None
) -> dict[str, Any]:
    """`stats`: what the radio measures itself (GET_STATS), and its duty-cycle setting."""
    core = readings.core if readings else None
    radio = readings.radio if readings else None
    packets = readings.packets if readings else None
    battery = readings.battery if readings else None
    used, span = meter.used_s(), meter.span_s()
    factor = tuning.airtime_factor_milli / 1000 if tuning else None
    return {
        "read_at": iso(readings.read_at) if readings else None,
        "uptime_s": core.uptime_s if core else None,
        "queue_length": core.queue_length if core else None,
        "errors": [n for bit, n in _ERRORS.items() if core.error_flags & bit] if core else [],
        "noise_floor_dbm": radio.noise_floor_dbm if radio else None,
        "last_rssi_dbm": radio.last_rssi_dbm if radio else None,
        "last_snr_db": radio.last_snr_db if radio else None,
        "tx_airtime_s": radio.tx_airtime_s if radio else None,
        "rx_airtime_s": radio.rx_airtime_s if radio else None,
        # the radio's own count over the last hour (or the time measured so far)
        "tx_airtime_1h_s": used,
        "tx_airtime_window_s": round(span) if used is not None else None,
        "packets_received": packets.received if packets else None,
        "packets_sent": packets.sent if packets else None,
        "packets_sent_flood": packets.sent_flood if packets else None,
        "packets_sent_direct": packets.sent_direct if packets else None,
        "packets_received_flood": packets.received_flood if packets else None,
        "packets_received_direct": packets.received_direct if packets else None,
        "receive_errors": packets.receive_errors if packets else None,
        "storage_used_kb": battery.storage_used_kb if battery else None,
        "storage_total_kb": battery.storage_total_kb if battery else None,
        "airtime_factor": factor,
        "duty_cycle_percent": round(100 / (1 + factor), 2) if factor is not None else None,
    }


# --- radio profile ------------------------------------------------------------------------------

_TELEMETRY_MODES: dict[int, str] = {
    TelemetryMode.DENY: "deny",
    TelemetryMode.BY_CONTACT: "contacts",
    TelemetryMode.ALLOW_ALL: "all",
}
SETTING_NAMES = {
    profile.MANUAL_ADD: "manual_add", profile.AUTOADD: "autoadd", profile.TELEMETRY: "telemetry",
    profile.ADVERT_LOCATION: "advert_location", profile.AIRTIME_FACTOR: "duty_cycle_percent",
    profile.NAME: "name",
}  # fmt: skip


def setting_value(key: str, value: object) -> object:
    """A managed setting as the documents show it."""
    if value is None or isinstance(value, bool):
        return value
    if key == profile.AUTOADD and isinstance(value, int):
        overwrite = ["overwrite_oldest"] if value & AutoAdd.OVERWRITE_OLDEST else []
        types = [name for flag, name in _AUTO_ADD.items() if value & flag]
        return [*overwrite, *types]
    if key == profile.TELEMETRY and isinstance(value, int):
        return {
            kind: _TELEMETRY_MODES.get((value >> shift) & 3, "unknown")
            for kind, shift in (("base", 0), ("location", 2), ("environment", 4))
        }
    if key == profile.ADVERT_LOCATION and isinstance(value, int):
        return "share" if value else "none"
    if key == profile.AIRTIME_FACTOR and isinstance(value, int):
        return round(100_000 / (1000 + value), 2)
    return value


def settings_document(report: profile.ProfileReport, at: float) -> dict[str, Any]:
    """`settings`: the radio's managed settings as found at the last connect, what the profile
    wants, and what happened to each (ADR 0020)."""
    problems = (
        *report.not_converging, *report.unsupported, *report.failed, *report.not_taken,
        *report.contacts_failed,
    )  # fmt: skip
    differs = [
        k for k, v in report.desired.items()
        if report.live.get(k) is not None and not profile.same(k, report.live.get(k), v)
    ]  # fmt: skip
    if problems:
        status = "problem"
    elif report.observing and (differs or report.contacts_missing or report.contacts_not_favourite):
        status = "differs"
    elif report.drift:
        status = "drift"
    elif report.written:
        status = "written"
    else:
        status = "ok"

    def state(key: str) -> str:
        for name, keys in (("unsupported", report.unsupported), ("failed", report.failed),
                           ("not_taken", report.not_taken),
                           ("not_converging", report.not_converging),
                           ("written", report.written), ("drift", report.drift)):  # fmt: skip
            if key in keys:
                return name
        if key not in report.desired:
            return "unmanaged"
        if report.live.get(key) is None:
            return "unknown"  # the firmware does not report it
        return "differs" if key in differs else "ok"

    return {
        "checked_at": iso(at),
        "status": status,
        "mode": report.mode,
        "observing": report.observing,
        "firmware_code": report.firmware_code,
        "firmware_tested": report.tested,
        "settings": {
            name: {
                "live": setting_value(key, report.live.get(key)),
                "desired": setting_value(key, report.desired.get(key)),
                "state": state(key),
            }
            for key, name in SETTING_NAMES.items()
        },
        "own_position": report.own_position,
        "protected_contacts": {
            "added": list(report.contacts_added),
            "favoured": list(report.contacts_favoured),
            "missing": list(report.contacts_missing),
            "failed": list(report.contacts_failed),
            "not_favourite": list(report.contacts_not_favourite),
        },
    }


# --- history ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One message in or out, as the recent history shows it."""

    at: float
    direction: str  # "in" or "out"
    kind: str  # "contact" or "channel"
    text: str
    contact_key: str | None = None
    channel_index: int | None = None
    name: str | None = None  # in: the sender's (for channels: claimed) name; out: the recipient
    state: str | None = None  # out: the final delivery state
    hops: int | None = None
    snr_db: float | None = None


def history_entry(entry: HistoryEntry) -> dict[str, Any]:
    return {
        "at": iso(entry.at),
        "direction": entry.direction,
        "kind": entry.kind,
        "contact_key": entry.contact_key,
        "channel_index": entry.channel_index,
        "name": entry.name,
        "text": entry.text,
        "state": entry.state,
        "hops": entry.hops,
        "snr_db": entry.snr_db,
    }


def history_document(entries: Iterable[HistoryEntry]) -> dict[str, Any]:
    """`history`: the latest messages in and out, newest first; of two at the same time, the one
    given later first."""
    ordered = sorted(entries, key=lambda e: e.at)[::-1]  # not reverse=True: ties keep order
    kept, total = bounded((history_entry(e) for e in ordered), ATTRIBUTE_LIMIT_BYTES)
    return {"total": total, "truncated": len(kept) < total, "messages": kept}
