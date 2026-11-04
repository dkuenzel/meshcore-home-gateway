"""`probe`: what the radio tells about itself, its privacy and its airtime, and what the managed
radio profile would write (ADR 0020)."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshcore_home_gateway.cli.common import (
    DEFAULT_STATE_DIR,
    EX_FAILURE,
    EX_OK,
    PREFIX,
    _json_text,
    _safe_text,
    tr,
    trn,
)
from meshcore_home_gateway.config import ProfileConfig, load_config
from meshcore_home_gateway.i18n import N_
from meshcore_home_gateway.protocol.codes import ContactFlag, TelemetryMode

if TYPE_CHECKING:
    from meshcore_home_gateway.core.profile import Plan
    from meshcore_home_gateway.state import ProfileRecords


def _probe(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.config_checks import protected_names  # noqa: PLC0415
    from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport  # noqa: PLC0415
    from meshcore_home_gateway.state import JsonStateFile, ProfileRecords  # noqa: PLC0415

    if args.serial:
        transport: Any = SerialTransport(args.serial)
    else:
        transport = TcpTransport(*args.tcp)
    config = load_config(args.config) if args.config else None
    profile = config.radio.profile if config else ProfileConfig()
    # the contacts the service keeps on the radio (targets, command senders), as it does
    protected = protected_names(config) if config and profile.protect_contacts else {}
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    records = ProfileRecords(JsonStateFile(state_dir / "state.json", read_only=True))
    try:
        report = asyncio.run(
            _probe_radio(transport, profile, records, protected, configured=config is not None)
        )
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        print(PREFIX + tr("probe failed: {detail}", detail=exc), file=sys.stderr)
        return EX_FAILURE
    if args.list == "channels":
        for channel in report["channels"]:
            print(f"{channel['index']}\t{_safe_text(channel['name'])}")
    elif args.list == "contacts":
        for contact in report["contacts"]:
            print(f"{contact['public_key']}\t{_safe_text(contact['name'])}\t{contact['type']}")
    elif args.list == "profile":  # for the installer: the name, and what the service changes
        profile = report["profile"]
        print(f"name\t{_safe_text(report['name'])}")
        print(f"tested\t{'yes' if profile['firmware_tested'] else 'no'}")
        print(f"writes\t{'yes' if profile['writes'] else 'no'}")
        for name, change in profile["changes"].items():
            live, wanted = _json_text(change["live"]), _json_text(change["profile"])
            print(f"change\t{name}\t{live}\t{wanted}\t{change['action']}")
        for key, contact in profile["contacts"].items():
            # the name last: it may be empty, and a shell's read joins tabs in a row
            print(f"contact\t{key}\t{contact['action']}\t{_safe_text(contact['name'])}")
    elif args.json:
        print(_json_text(report, indent=2))
    else:
        _print_report(report)
    return EX_OK


NODE_TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}


ADVERT_LOCATION = {0: "none", 1: "share"}  # adverts without or with the radio's position


_FLAG_NAMES = {
    ContactFlag.FAVOURITE: "favourite",
    ContactFlag.TELEMETRY_BASE: "telemetry_base",
    ContactFlag.TELEMETRY_LOCATION: "telemetry_location",
    ContactFlag.TELEMETRY_ENVIRONMENT: "telemetry_environment",
    ContactFlag.REMOTE_CLI: "remote_cli",
}


def _mode_name(mode: TelemetryMode | int) -> str:
    return mode.name.lower() if isinstance(mode, TelemetryMode) else str(mode)


def _flag_names(flags: int) -> list[str]:
    """Names of a contact's flags; unknown bits as one hex value."""
    names = [name for flag, name in _FLAG_NAMES.items() if flags & flag]
    unknown = flags & ~sum(_FLAG_NAMES)
    return [*names, f"0x{unknown:02x}"] if unknown else names


async def _probe_radio(
    transport: Any,
    profile: ProfileConfig,
    records: ProfileRecords,
    protected: Mapping[str, str],
    *,
    configured: bool,
) -> dict[str, Any]:
    from meshcore_home_gateway.core import profile as policy  # noqa: PLC0415
    from meshcore_home_gateway.ports import RadioEvent  # noqa: PLC0415
    from meshcore_home_gateway.protocol.codec import added_types, type_names  # noqa: PLC0415
    from meshcore_home_gateway.radio.profile import live_values  # noqa: PLC0415
    from meshcore_home_gateway.radio.session import CompanionSession  # noqa: PLC0415
    from meshcore_home_gateway.views import SETTING_NAMES, setting_value  # noqa: PLC0415

    events: asyncio.Queue[RadioEvent] = asyncio.Queue()
    session = CompanionSession(transport, events, set_clock=False)
    try:
        info = await session.start(fetch_messages=False)
    finally:
        await session.close()
    me, device, autoadd, tuning = info.self_info, info.device, session.autoadd, session.tuning
    live = live_values(session)
    want = policy.desired(
        me.frequency_khz, name=profile.name, duty_cycle_percent=profile.duty_cycle_percent
    )
    mode = policy.Mode(profile.manage)
    tested = device.firmware_code in policy.TESTED_FIRMWARE
    # the service's own reconcile, with what it stored for this radio: the exact plan
    plan = policy.plan(
        want, records.load(info.public_key), live, mode=mode, firmware_code=device.firmware_code
    )
    changes = {
        SETTING_NAMES[key]: {
            "live": setting_value(key, live[key]),
            "profile": setting_value(key, value),
            "action": _profile_action(key, plan),
            "reason": plan.reasons.get(key),
        }
        for key, value in want.values.items()
        if live.get(key) is not None and not policy.same(key, live[key], value)
    }
    contacts = {}  # the protected contacts the service adds or makes favourites
    for key, name in sorted(protected.items()):
        record = session.contact_records.get(key)
        if record is not None and record.flags & ContactFlag.FAVOURITE:
            continue
        if plan.observing:
            action = "missing" if record is None else "not_favourite"
        else:
            action = "add" if record is None else "favourite"
        contacts[key] = {"name": name if record is None else record.name, "action": action}
    return {
        "name": me.name,
        "public_key": info.public_key,
        "firmware": device.version,
        "firmware_code": device.firmware_code,
        "model": device.model,
        "build_date": device.build_date,
        "frequency_mhz": me.frequency_khz / 1000,
        "bandwidth_khz": me.bandwidth_hz / 1000,
        "spreading_factor": me.spreading_factor,
        "coding_rate": me.coding_rate,
        "tx_power_dbm": me.tx_power_dbm,
        "manual_add_contacts": me.manual_add_contacts,
        "auto_add": {  # overwrite_oldest null: firmware 1.11 or older, no such setting
            "manual": me.manual_add_contacts,
            "types": type_names(added_types(me, autoadd)),
            "overwrite_oldest": None if autoadd is None else autoadd.overwrites_oldest,
        },
        "position": None if me.position is None else dataclasses.asdict(me.position),
        "advert_location": ADVERT_LOCATION.get(me.advert_location, str(me.advert_location)),
        "telemetry": {
            "base": _mode_name(me.telemetry_base),
            "location": _mode_name(me.telemetry_location),
            "environment": _mode_name(me.telemetry_environment),
        },
        "multi_acks": me.multi_acks,
        # null: firmware without GET_TUNING_PARAMS; duty cycle 1 / (1 + factor)
        "airtime_factor": None if tuning is None else tuning.airtime_factor_milli / 1000,
        "battery_mv": session.battery.millivolts if session.battery else None,
        "contacts": [
            {
                "public_key": key,
                "name": rec.name,
                "type": NODE_TYPES.get(rec.node_type, str(rec.node_type)),
                "flags": _flag_names(rec.flags),
            }
            for key, rec in sorted(
                session.contact_records.items(), key=lambda kv: kv[1].name.lower()
            )
        ],
        "channels": [{"index": i, "name": n} for i, n in sorted(session.channels.items())],
        # what the managed profile changes at the service's first connect (ADR 0020)
        "profile": {
            "manage": mode.value,
            "configured": configured,  # false: the default observer profile
            "firmware_tested": tested,
            "writes": not plan.observing,
            "changes": changes,
            "contacts": contacts,
        },
    }


def _profile_action(key: str, plan: Plan) -> str:
    """What the service does about a setting that differs from the profile."""
    if key in plan.writes:
        return "set"
    if key in plan.drift:
        return "kept"  # changed on the radio after the service set it
    if key in plan.not_converging:
        return "given_up"  # written at the last connects without effect
    if key in plan.unsupported:
        return "unsupported"
    if key in plan.disabled:
        return "disabled"  # compiled out of the firmware (IMP-050)
    return "reported"  # observing


# Translators: a line of probe's report on how the radio adds the nodes it hears; its value
# starts in the column of the other lines; {types} lists node types (chat, repeater, room,
# sensor), which stay as they are
_ADDING = {  # manually; some types still automatically; a full table overwrites the oldest
    (True, False, False): N_("contacts   added manually"),
    (True, True, False): N_("contacts   added manually, but {types} automatically"),
    (True, True, True): N_(
        "contacts   added manually, but {types} automatically; a full table overwrites the oldest"
    ),
    (False, False, False): N_("contacts   all added automatically"),
    (False, False, True): N_(
        "contacts   all added automatically; a full table overwrites the oldest"
    ),
}


# Translators: a line of probe's report, its value in the column of the other lines: the radio's
# position, and whether its adverts carry it
_POSITION = {  # known; shared in adverts
    (False, False): N_("position   none, not in adverts"),
    (False, True): N_("position   none, shared in adverts"),
    (True, False): N_("position   {latitude}, {longitude}, not in adverts"),
    (True, True): N_("position   {latitude}, {longitude}, shared in adverts"),
}


# Translators: a line of probe's report, its value in the column of the other lines: whether
# the radio's settings differ from the managed profile, which the setting radio.profile
# configures or leaves to its default
_PROFILE = {  # differs; configured
    (False, False): N_("profile    the radio matches the default observer profile"),
    (False, True): N_("profile    the radio matches the configured profile"),
    (True, False): N_("profile    differs from the default observer profile:"),
    (True, True): N_("profile    differs from the configured profile:"),
}


def _print_report(r: dict[str, Any]) -> None:
    auto, spot = r["auto_add"], r["position"]
    types = ", ".join(auto["types"])
    adding = (bool(auto["manual"]), bool(auto["manual"] and types))
    overwrites = bool(types and auto["overwrite_oldest"])
    modes = ", ".join(f"{kind} {mode}" for kind, mode in r["telemetry"].items())
    shared = r["advert_location"] == "share"
    factor = r["airtime_factor"]
    # Translators: a line of probe's report. Its value starts in the column where the other
    # lines start theirs (12 in English): pad the label with spaces to keep one column.
    lines = [
        _safe_text(tr("radio      {name}  {key}", name=r["name"], key=r["public_key"])),
        _safe_text(
            tr(
                "firmware   {version} (code {code}), {model}, built {date}",
                version=r["firmware"],
                code=r["firmware_code"],
                model=r["model"],
                date=r["build_date"],
            )
        ),
        tr(
            "lora       {frequency:.3f} MHz, {bandwidth:g} kHz, SF {sf}, CR 4/{cr}, {power} dBm",
            frequency=r["frequency_mhz"],
            bandwidth=r["bandwidth_khz"],
            sf=r["spreading_factor"],
            cr=r["coding_rate"],
            power=r["tx_power_dbm"],
        ),
        tr(_ADDING[(*adding, overwrites)], types=types),
        tr("telemetry  {modes}", modes=modes),
        tr(_POSITION[spot is not None, shared], **(spot or {})),
        *(
            [
                tr(
                    "airtime    factor {factor:g}: the radio sends at most {duty:.3g} %",
                    factor=factor,
                    duty=100 / (1 + factor),
                )
            ]
            if factor is not None
            else []
        ),
    ]
    for line in lines:
        print(line)
    for c in r["contacts"]:
        # Translators: after a contact's name in probe's report: it may use the radio's
        # command line over the air
        remote = "  " + tr("REMOTE CLI") if "remote_cli" in c["flags"] else ""
        print(_safe_text(f"  {c['public_key']}  {c['type']:<8} {c['name']}{remote}"))
    # Translators: a heading in probe's report, above the radio's channels
    print(tr("channels"))
    for ch in r["channels"]:
        print(_safe_text(f"  {ch['index']:>3}  {ch['name']}"))
    _print_plan(r["profile"])


def _print_plan(profile: dict[str, Any]) -> None:
    """What the managed profile changes at the service's next connect (ADR 0020)."""
    from meshcore_home_gateway.core.profile import NOT_CONVERGING_AFTER  # noqa: PLC0415

    differs = bool(profile["changes"] or profile["contacts"])
    print(tr(_PROFILE[differs, bool(profile["configured"])]))
    if not differs:
        return
    # Translators: why the gateway does not change a setting or contact; it follows a
    # setting's or contact's line, for example "not on the radio; only reported: …"
    reported = (
        tr("only reported: radio.profile.manage = observe")
        if profile["manage"] == "observe"
        else tr("only reported: the firmware is untested; radio.profile.manage = always sets it")
    )
    # Translators: what the gateway does about a setting of the radio that differs from the
    # managed profile, in brackets after the setting's line
    what = {
        "set": N_("set at the next connect"),
        "kept": N_("changed on the radio; kept"),
        "unsupported": N_("the firmware does not support it"),
        "disabled": N_("compiled out of the radio's firmware"),
    }
    for name, change in profile["changes"].items():
        live, wanted = _json_text(change["live"]), _json_text(change["profile"])
        action = change["action"]
        if action == "given_up":
            done = trn(
                "did not take effect at {count} connect; no longer set",
                "did not take effect at {count} connects; no longer set",
                NOT_CONVERGING_AFTER,
            )
        else:
            done = reported if action == "reported" else tr(what[action])
        print(f"  {name}: {live} -> {wanted}  ({done})")
    # Translators: what the gateway does about a contact it keeps on the radio, in brackets after
    # the contact's line; {reported} says why it only reports it ("only reported: …")
    doing = {
        "add": N_("added as a favourite at the next connect"),
        "favourite": N_("made a favourite at the next connect"),
        "missing": N_("not on the radio; {reported}"),
        "not_favourite": N_("no favourite; {reported}"),
    }
    for key, contact in profile["contacts"].items():
        action = tr(doing[contact["action"]], reported=reported)
        # Translators: a contact the managed profile adds or makes a favourite: its first 12 key
        # digits, its name, what the gateway does
        line = tr(
            "  contact {key} {name}  ({action})", key=key[:12], name=contact["name"], action=action
        )
        print(_safe_text(line))
