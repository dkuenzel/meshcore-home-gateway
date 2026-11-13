"""Providers: the documents of the MQTT API (ADR 0015), built from the gateway's state.

Pure functions. Every key is always present (`null` when unknown), times are ISO 8601 in UTC,
units are in the key names, machine values are lower case, and texts from the mesh are plain data.
Lists are bounded by their serialised size and carry `total` and `truncated`.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from meshcore_home_gateway.core import profile
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS, HeardNodes, name_key
from meshcore_home_gateway.core.mesh import AirtimeMeter, Mesh, Node, Tracked, type_name
from meshcore_home_gateway.core.telemetry import CURRENT, TelemetryEvent, TelemetryState
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import AutoAdd, ContactFlag, NodeType, TelemetryMode

INDEX_LIMIT_BYTES = 32 * 1024  # the contact index; Home Assistant reads only its count
ATTRIBUTE_LIMIT_BYTES = 15 * 1024  # documents read into attributes: below the recorder's 16 KiB
PENDING_LIMIT_BYTES = 8 * 1024  # the `pending` document, whole: it goes out with every step
WARNING_KEYS = 16  # keys listed per warning in the contact index; the total is counted
TELEMETRY_ACCURACY_M = 15.0  # of a position from telemetry: cell centring and the GNSS (ADR 0018)
_FLAGS = {
    ContactFlag.FAVOURITE: "favourite",
    ContactFlag.TELEMETRY_BASE: "telemetry_base",
    ContactFlag.TELEMETRY_LOCATION: "telemetry_location",
    ContactFlag.TELEMETRY_ENVIRONMENT: "telemetry_environment",
    ContactFlag.REMOTE_CLI: "remote_cli",
}
_ERRORS = {0x01: "queue_full", 0x02: "cad_timeout", 0x04: "start_rx_timeout"}  # Dispatcher.h
_AUTO_ADD = {
    AutoAdd.CHAT: "chat",
    AutoAdd.REPEATER: "repeater",
    AutoAdd.ROOM_SERVER: "room",
    AutoAdd.SENSOR: "sensor",
}


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
        if size > limit_bytes:  # cut: none after it is kept, so none is encoded
            continue
        size += len(encode(entry)) + 1
        if size <= limit_bytes:
            kept.append(entry)
    return kept, total


# --- contacts -----------------------------------------------------------------------------------


_HEARD_FIELDS = ("heard_at", "heard_via", "hops", "snr_db", "rssi_dbm")


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


def advert_carries_position(tracked: Tracked) -> bool | None:
    """Whether a companion's adverts carry its position, which everyone in range can read: the
    flag of its newest advert, as the gateway checked it. While its newest advert is not checked
    (none yet, or a newer one the gateway missed), the last check's flag, or True when the
    radio's record holds a position from an earlier advert, which it may still send: it errs to
    warn (ADR 0025). None for other node types, which share theirs by design and
    are not checked."""
    node, check = tracked.node, tracked.advert
    if node.node_type != NodeType.CHAT:
        return None
    if check is not None and node.last_advert <= check.at:  # the newest advert, checked
        return check.carries
    return (check is not None and check.carries) or node.position is not None


def contact_document(
    tracked: Tracked,
    *,
    protected: bool = False,
    manage: bool = False,
    telemetry_state: TelemetryState | None = None,
    excluded: bool = False,
) -> dict[str, Any]:
    """`contacts/<key>`: one contact of the radio and what the gateway learned about it.
    `telemetry_state`: None for a contact that is never asked (no companion)."""
    node, fix, readings = tracked.node, tracked.fix, tracked.readings
    check = tracked.advert if node.node_type == NodeType.CHAT else None  # companions only
    from_telemetry = fix is not None and fix.source == "telemetry"
    telemetry_current, position_current = _current(telemetry_state, from_telemetry)
    named = readings.named if readings else None
    return {
        "key": node.public_key,
        "prefix": node.public_key[:PREFIX_HEX_DIGITS],
        "name": node.name,
        "type": type_name(node.node_type),
        "flags": flag_names(node.flags),
        "favourite": bool(node.flags & ContactFlag.FAVOURITE),
        "remote_cli": bool(node.flags & ContactFlag.REMOTE_CLI),
        "protected": protected,
        "removable": manage and not protected,  # a remove request would be accepted
        # the radio holds a signed advert of this key: name and type are its holder's own
        "verified": node.last_advert > 0,
        "path_known": node.path_hops is not None,
        "path_hops": node.path_hops,
        "last_advert_at": iso(node.last_advert) if node.last_advert else None,
        "first_heard_at": iso(tracked.first_heard),
        **_heard(tracked),
        "latitude": fix.position.latitude if fix else None,
        "longitude": fix.position.longitude if fix else None,
        "altitude_m": fix.altitude_m if fix else None,
        "position_at": iso(fix.at) if fix else None,  # when it last changed, never a fix time
        "position_source": fix.source if fix else None,
        "position_accuracy_m": TELEMETRY_ACCURACY_M if from_telemetry else None,
        "position_received_at": iso(tracked.position_received_at),  # the newest answer with one
        # the radio's record holds a position from an advert, kept after sharing stops
        "advert_position": node.position is not None,
        "advert_carries_position": advert_carries_position(tracked),
        "advert_checked_at": iso(check.at) if check and check.at else None,
        "telemetry_state": telemetry_state,
        "telemetry_excluded": excluded and telemetry_state is not None,  # a type that is asked
        "telemetry_current": telemetry_current,
        "position_current": position_current,
        "readings_at": iso(readings.at) if readings else None,  # the gateway's time of the answer
        "battery_v": named.battery_v if named else None,
        "mcu_temperature_c": named.mcu_temperature_c if named else None,
        "readings": dict(named.readings) if named else {},
    }


def _current(state: TelemetryState | None, from_telemetry: bool) -> tuple[bool | None, bool | None]:
    """`telemetry_current` and `position_current` of a contact in this state. Only a position from
    telemetry expires: None for one from an advert."""
    telemetry_current = None if state is None else state in CURRENT
    return telemetry_current, state == "answering" if from_telemetry else None


_FORGOTTEN = ("readings_at", "battery_v", "mcu_temperature_c", "position_received_at")
_POSITION = (
    "latitude",
    "longitude",
    "altitude_m",
    "position_at",
    "position_source",
    "position_accuracy_m",
)


def restated(
    document: Mapping[str, Any], state: TelemetryState | None, *, excluded: bool = False
) -> dict[str, Any]:
    """`contacts/<key>` as last published, in another telemetry state: before the first table
    read of a run the stored document is all there is (ADR 0024). `excluded`: without what an
    exclusion forgets, as far as the document shows it (the position the contact's record carries
    comes back with the table read); not excluded, what an exclusion forgot stays forgotten."""
    again = dict(document)
    # keys added since the document was stored (1.1.0a3): what the gateway would say without a check
    chat = document.get("type") == "chat"
    again.setdefault("advert_carries_position", document.get("advert_position") if chat else None)
    again.setdefault("advert_checked_at", None)
    from_telemetry = document.get("position_source") == "telemetry"
    if excluded:
        again |= {**dict.fromkeys(_FORGOTTEN), "readings": {}}
        if from_telemetry:
            again |= dict.fromkeys(_POSITION)
            from_telemetry = False
    telemetry_current, position_current = _current(state, from_telemetry)
    again |= {
        "telemetry_state": state,
        "telemetry_current": telemetry_current,
        "position_current": position_current,
        "telemetry_excluded": excluded,
    }
    return again


def contacts_document(
    mesh: Mesh,
    *,
    protected: Iterable[str] = (),
    manage: bool = False,
    excluded: Collection[str] = (),
    manage_reason: str | None = None,
) -> dict[str, Any]:
    """`contacts`: the index of the radio's contact table, most recently heard first.
    `excluded`: every key excluded from telemetry, also those not on the radio.
    `manage_reason`: why add and remove requests are refused now (management_off, no_radio,
    untested_firmware), so Home Assistant can say why its controls are unavailable."""
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
    # companions whose record holds a position from their adverts: everyone in range saw it
    shared = sorted(k for k, t in contacts if t.node.position is not None and t.node.node_type == 1)
    stored_set = set(excluded)
    # companions not excluded whose adverts carry their position (ADR 0018): the household's
    carrying = sorted(k for k, t in contacts if k not in stored_set and advert_carries_position(t))
    stored = sorted(stored_set)
    return {
        "total": total,
        "max": mesh.max_contacts,
        "manage": manage,  # add and remove requests are taken
        "manage_reason": manage_reason,  # why one sent now is refused (OPS-007), or None
        "remote_cli": remote_cli[:WARNING_KEYS],
        "remote_cli_total": len(remote_cli),
        "advert_position": shared[:WARNING_KEYS],
        "advert_position_total": len(shared),
        "advert_carries_position": carrying[:WARNING_KEYS],
        "advert_carries_position_total": len(carrying),
        "telemetry_excluded": stored[:WARNING_KEYS],
        "telemetry_excluded_total": len(stored),
        "truncated": len(kept) < total,
        "contacts": kept,
    }


def look_alike_nodes(mesh: Mesh, protected: Iterable[str] = ()) -> set[str]:
    """Heard nodes whose 6-byte prefix another node has: a contact, a protected key or another
    heard node. The radio could not tell them apart (ADR 0009): a look-alike of a contact or a
    protected key is refused; of heard nodes, the one added first keeps the prefix."""
    counts: dict[str, int] = {}
    for key in {*mesh.contacts, *protected, *mesh.nodes}:
        counts[key[:PREFIX_HEX_DIGITS]] = counts.get(key[:PREFIX_HEX_DIGITS], 0) + 1
    return {key for key in mesh.nodes if counts[key[:PREFIX_HEX_DIGITS]] > 1}


def name_taken_nodes(mesh: Mesh, reserved: Iterable[str] = ()) -> set[str]:
    """Heard nodes whose name, as `name_key` compares names, a contact has, a name the gateway
    goes by (`reserved`: its device in Home Assistant, its radio's configured name) or another
    heard node: someone may pass for them (SEC-007). Empty names are no names."""
    counts: dict[str, int] = {}
    names = [t.node.name for t in (*mesh.contacts.values(), *mesh.nodes.values())]
    for compared in (*map(name_key, names), *{name_key(n) for n in reserved}):
        if compared:
            counts[compared] = counts.get(compared, 0) + 1
    return {
        key
        for key, t in mesh.nodes.items()
        if (compared := name_key(t.node.name)) and counts[compared] > 1
    }


def nodes_document(
    mesh: Mesh,
    *,
    protected: Iterable[str] = (),
    heard: HeardNodes = "full",
    reserved: Iterable[str] = (),
) -> dict[str, Any]:
    """`nodes`: nodes the radio heard but did not add, most recently heard first; no positions.
    `heard` (`[contacts] heard_nodes`, SEC-015): with `keys` without their names, times and
    signal; with `off` none of them, only the look-alikes' and the taken names' keys."""
    flagged = look_alike_nodes(mesh, protected)
    taken = name_taken_nodes(mesh, reserved)
    full = heard == "full"
    listed = [] if heard == "off" else list(reversed(mesh.nodes.values()))
    entries = (
        {
            "key": t.node.public_key,
            "prefix": t.node.public_key[:PREFIX_HEX_DIGITS],
            "name": t.node.name if full else None,
            "type": type_name(t.node.node_type),
            "first_heard_at": iso(t.first_heard) if full else None,
            "look_alike": t.node.public_key in flagged,
            "name_taken": t.node.public_key in taken,
            **(_heard(t) if full else dict.fromkeys(_HEARD_FIELDS)),
        }
        for t in listed
    )
    warning, names = sorted(flagged), sorted(taken)
    head = {
        "total": len(listed),
        "truncated": True,
        "look_alike": warning[:WARNING_KEYS],
        "look_alike_total": len(warning),
        "name_taken": names[:WARNING_KEYS],
        "name_taken_total": len(names),
        "nodes": [],
    }
    kept, total = bounded(entries, ATTRIBUTE_LIMIT_BYTES - len(encode(head)))  # all within it
    return {**head, "total": total, "truncated": len(kept) < total, "nodes": kept}


def contact_event(
    kind: str,
    key: str | None,
    node: Node | None = None,
    *,
    name: str | None = None,
    node_type: int | None = None,
    request: str | None = None,
    request_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """A `contact` event: what changed in the table, or a request's result. Name and type from
    the node, else as given (a request's claim)."""
    kind_of = node.node_type if node else node_type
    return {
        "event_type": kind,
        "key": key,
        "prefix": key[:PREFIX_HEX_DIGITS] if key else None,
        "name": node.name if node else name,
        "type": type_name(kind_of) if kind_of is not None else None,
        "request": request,
        "id": request_id,
        "reason": reason,
    }


def telemetry_event(
    kind: TelemetryEvent, key: str, node: Node | None, state: TelemetryState
) -> dict[str, Any]:
    """A `telemetry` event: a companion stopped answering or answers again. Name from the
    radio's record, if it has one; never a position or reading."""
    return {
        "event_type": kind,
        "key": key,
        "prefix": key[:PREFIX_HEX_DIGITS],
        "name": node.name if node else None,
        "state": state,
    }


# `advert` events (ADR 0033): what became of a request to send the radio's advert nearby
SELF_ADVERT_EVENTS = ("sent", "refused", "failed")
# the gateway's reasons of `refused`, in the order it checks them, and of `failed`; the radio's
# refusals of the advert are its error names (codec.Err.name, "disabled") beside these
SELF_ADVERT_REASONS = (
    "too_large",
    "invalid_utf8",
    "invalid_request",
    "retained",
    "empty",
    "no_radio",
    "untested_firmware",
    "advert_location",
    "no_name",
    "busy",
    "too_soon",
    "messages_waiting",
    "airtime_budget",
    "clock_ahead",
    "outcome_unknown",
    "internal_error",
)


def self_advert_event(
    kind: str,
    request_id: str,
    *,
    reason: str | None = None,
    name: str | None = None,
    airtime_s: float | None = None,
    next_at: float | None = None,
) -> dict[str, Any]:
    """An `advert` event: what became of a request to send the radio's advert nearby. `name`
    and `airtime_s` of an advert that was written; `next_at`, when the next one may go."""
    return {
        "event_type": kind,
        "id": request_id,
        "reason": reason,
        "name": name,
        "airtime_ms": round(airtime_s * 1000) if airtime_s is not None else None,
        "next_at": iso(next_at),
    }


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
    profile.MANUAL_ADD: "manual_add",
    profile.AUTOADD: "autoadd",
    profile.TELEMETRY: "telemetry",
    profile.ADVERT_LOCATION: "advert_location",
    profile.AIRTIME_FACTOR: "duty_cycle_percent",
    profile.NAME: "name",
}


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
        *report.not_converging,
        *report.unsupported,
        *report.disabled,
        *report.failed,
        *report.not_taken,
        *report.contacts_failed,
    )
    differs = [
        k
        for k, v in report.desired.items()
        if report.live.get(k) is not None and not profile.same(k, report.live.get(k), v)
    ]
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
        for name, keys in (
            ("unsupported", report.unsupported),
            ("disabled", report.disabled),
            ("failed", report.failed),
            ("not_taken", report.not_taken),
            ("not_converging", report.not_converging),
            ("written", report.written),
            ("drift", report.drift),
        ):
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
    # since 2.1, after the others: the history builds entries from its rows in this order
    target: str | None = None  # the configured alias it came through or went to
    id: str | None = None  # out: the id of its delivery events
    queued_at: float | None = None  # out: when the gateway accepted it (wall clock)
    suspect: bool | None = None  # out, confirmed: its acknowledgement may be forged (SEC-001)
    reason: str | None = None  # out: why it failed or expired


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
        "target": entry.target,
        "id": entry.id,
        "queued_at": iso(entry.queued_at),
        "suspect": entry.suspect,
        "reason": entry.reason,
    }


def history_document(entries: Iterable[HistoryEntry]) -> dict[str, Any]:
    """`history`: the latest messages in and out, newest first; of two at the same time, the one
    given later first. The bound counts the whole document, its head too (#111)."""
    ordered = sorted(entries, key=lambda e: e.at)[::-1]  # not reverse=True: ties keep order
    # "false" is a byte longer than "true", and bounded() counts a comma more than the list has
    head = {"total": len(ordered), "truncated": True, "messages": []}
    kept, total = bounded(
        (history_entry(e) for e in ordered), ATTRIBUTE_LIMIT_BYTES - len(encode(head))
    )
    return {"total": total, "truncated": len(kept) < total, "messages": kept}


# --- messages on their way ----------------------------------------------------------------------

PENDING_STEPS = ("queued", "sent")  # `step` of a pending message: not sent yet, or sent


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """A message the gateway accepted that is not final yet, as `pending` lists it."""

    id: str  # of its delivery events
    queued_at: float | None  # when the gateway accepted it (wall clock); None if not known
    target: str | None  # the configured alias; None for `contacts/<key>/send`
    kind: str  # "contact" or "channel"
    contact_key: str | None
    channel_index: int | None
    name: str  # the recipient: the contact's name, else the alias
    text: str  # as it is sent
    step: str  # one of PENDING_STEPS: its latest delivery event, `queued` or `sent`
    attempt: int  # transmissions so far
    max_attempts: int


def pending_entry(entry: PendingEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "queued_at": iso(entry.queued_at),
        "target": entry.target,
        "kind": entry.kind,
        "contact_key": entry.contact_key,
        "channel_index": entry.channel_index,
        "name": entry.name,
        "text": entry.text,
        "step": entry.step,
        "attempt": entry.attempt,
        "max_attempts": entry.max_attempts,
    }


def pending_document(entries: Collection[PendingEntry]) -> dict[str, Any]:
    """`pending`: the messages accepted and not final yet, the one being sent included, in the
    order given (the oldest first). The bound counts the whole document, its head too; a cut
    leaves out the newest."""
    head = {"total": len(entries), "truncated": True, "messages": []}
    kept, total = bounded(
        (pending_entry(e) for e in entries), PENDING_LIMIT_BYTES - len(encode(head))
    )
    return {"total": total, "truncated": len(kept) < total, "messages": kept}


# --- the configuration --------------------------------------------------------------------------

KEY_START_DIGITS = 8  # of a contact target's key, as the documents show it: never the whole key


@dataclass(frozen=True, slots=True)
class TargetSetting:
    """A target of the configuration, as `config` shows it."""

    alias: str
    kind: str  # "channel" or "contact"
    channel_index: int | None
    key: str | None  # a contact target's full key, of which `config` shows the start only
    prefix: str
    suffix: str
    alert: bool  # `[messages] alert_targets`: sent before the other messages
    hidden: bool  # `[homeassistant] hidden_targets`: no entities in Home Assistant
    room: int  # bytes a text has beside the prefix and suffix: the `max` of the chat's draft


def target_setting(target: TargetSetting) -> dict[str, Any]:
    return {
        "alias": target.alias,
        "kind": target.kind,
        "channel_index": target.channel_index,
        "key_start": target.key[:KEY_START_DIGITS] if target.key else None,
        "prefix": target.prefix,
        "suffix": target.suffix,
        "alert": target.alert,
        "hidden": target.hidden,
        "room": target.room,
    }


def config_document(
    started_at: float,
    targets: Sequence[TargetSetting],
    settings: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """`config`: the settings in effect since the gateway started (`started_at`), by section as
    the configuration names them (`settings`, an allowlist: config_view.shown_settings), and the
    targets in their order. The configuration allows any number of targets: a cut leaves out the
    last, so that the whole document stays within ATTRIBUTE_LIMIT_BYTES, its head counted."""
    head = {
        "started_at": iso(started_at),
        "targets": {"total": len(targets), "truncated": True, "items": []},
        **settings,
    }
    kept, total = bounded(
        (target_setting(t) for t in targets), ATTRIBUTE_LIMIT_BYTES - len(encode(head))
    )
    return {**head, "targets": {"total": total, "truncated": len(kept) < total, "items": kept}}
