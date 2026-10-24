"""The managed radio profile (ADR 0020): what the observer's radio is set to, and the three-way
reconcile of desired (configuration), last applied (state file) and live (the radio).

Pure: the radio adapter reads the live values, writes what the plan names, and stores the result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

# firmware version codes (FIRMWARE_VER_CODE, MyMesh.h) whose setters were checked
TESTED_FIRMWARE = frozenset({14})  # companion 1.18
NOT_CONVERGING_AFTER = 3  # connects in a row that wrote a setting
TOLERANCE_MILLI = 1  # values stored as float32 come back within a thousandth
# the sub-band with a 10 % duty cycle (ERC Recommendation 70-03, annex 1, band h1.6)
TEN_PERCENT_BAND_KHZ = (869_400, 869_650)
MAX_AIRTIME_FACTOR_MILLI = 9000  # the firmware clamps the factor to 0..9 at boot


class Mode(Enum):
    AUTO = "auto"  # write on tested firmware only
    OBSERVE = "observe"  # never write
    ALWAYS = "always"  # write on any firmware


class Kind(Enum):
    ENFORCED = "enforced"  # a change on the radio is set back
    SEED = "seed"  # written when the configuration changes; a change on the radio is kept


# the observer's policy (ADR 0020): it adds no contact by itself and reveals nothing
MANUAL_ADD = "manual_add"
AUTOADD = "autoadd"
TELEMETRY = "telemetry"
ADVERT_LOCATION = "advert_location"
AIRTIME_FACTOR = "airtime_factor_milli"
NAME = "name"
KINDS = {
    MANUAL_ADD: Kind.ENFORCED, AUTOADD: Kind.ENFORCED, TELEMETRY: Kind.ENFORCED,
    ADVERT_LOCATION: Kind.ENFORCED, AIRTIME_FACTOR: Kind.ENFORCED, NAME: Kind.SEED,
}  # fmt: skip

Value = bool | int | str


@dataclass(frozen=True, slots=True)
class Desired:
    """What the configuration asks for; a key that is absent is left as found."""

    values: Mapping[str, Value]


def desired(
    frequency_khz: int, *, name: str | None = None, duty_cycle_percent: float | None = None
) -> Desired:
    """The observer profile for a radio on `frequency_khz`: manual adding with no type added
    automatically, all telemetry denied, no position in adverts, the duty cycle as configured or
    10 % in the 10 % sub-band, the name if configured."""
    values: dict[str, Value] = {
        MANUAL_ADD: True, AUTOADD: 0, TELEMETRY: 0, ADVERT_LOCATION: 0,
    }  # fmt: skip
    low, high = TEN_PERCENT_BAND_KHZ
    if duty_cycle_percent is None and low <= frequency_khz <= high:
        duty_cycle_percent = 10.0
    if duty_cycle_percent is not None:
        values[AIRTIME_FACTOR] = airtime_factor_milli(duty_cycle_percent)
    if name is not None:
        values[NAME] = name
    return Desired(values)


def airtime_factor_milli(duty_cycle_percent: float) -> int:
    """The firmware waits `factor` times a packet's airtime after sending it: duty cycle
    1 / (1 + factor)."""
    factor = round((100.0 / duty_cycle_percent - 1.0) * 1000)
    return max(0, min(factor, MAX_AIRTIME_FACTOR_MILLI))


def same(key: str, a: Value | None, b: Value | None) -> bool:
    if key == AIRTIME_FACTOR and isinstance(a, int) and isinstance(b, int):
        return abs(a - b) <= TOLERANCE_MILLI
    return a == b


@dataclass(frozen=True, slots=True)
class Attempt:
    """A value written at connects in a row without taking effect (refused, read back otherwise,
    or the radio went away), on one firmware: another firmware is tried afresh."""

    value: Value
    connects: int
    firmware_code: int


@dataclass(slots=True)
class Applied:
    """The state file's memory, per radio: the configured value the radio last took per key
    (written and read back, or found there), the value being tried without effect, and the keys
    a firmware answered with 'unsupported' (key -> firmware code)."""

    values: dict[str, Value] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    unsupported: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """Nothing stored: the first connect of this radio (or of the profile)."""
        return not (self.values or self.attempts or self.unsupported)


@dataclass(frozen=True, slots=True)
class Plan:
    writes: dict[str, Value]  # key -> value to write now
    # key -> why: "initial" (first managed on this radio), "configured" (the configuration
    # changed) or "drift" (changed on the radio)
    reasons: dict[str, str]
    drift: dict[str, Value]  # seed keys changed on the radio: kept, reported (key -> live value)
    not_converging: tuple[str, ...]  # written at the last connects without effect: given up
    unsupported: tuple[str, ...]
    observing: bool  # nothing may be written (mode, untested firmware)


def plan(
    want: Desired,
    applied: Applied,
    live: Mapping[str, Value | None],
    *,
    mode: Mode,
    firmware_code: int,
) -> Plan:
    """What to write at this connect. A key the radio does not report (None) is left alone."""
    writes: dict[str, Value] = {}
    reasons: dict[str, str] = {}
    drift: dict[str, Value] = {}
    stuck, unsupported = [], []
    observing = mode is Mode.OBSERVE or (mode is Mode.AUTO and firmware_code not in TESTED_FIRMWARE)
    for key, value in want.values.items():
        current = live.get(key)
        if current is None or same(key, current, value):
            continue
        if applied.unsupported.get(key) == firmware_code:
            unsupported.append(key)
            continue
        changed = key not in applied.values or not same(key, applied.values[key], value)
        if not changed and KINDS[key] is Kind.SEED:
            drift[key] = current  # renamed on the radio: kept
            continue
        if _given_up(applied.attempts.get(key), key, value, firmware_code):
            stuck.append(key)
            continue
        writes[key] = value
        reasons[key] = "initial" if applied.empty else "configured" if changed else "drift"
    return Plan(
        writes={} if observing else writes,
        reasons={} if observing else reasons,
        drift=drift,
        not_converging=tuple(stuck),
        unsupported=tuple(unsupported),
        observing=observing,
    )


def _given_up(attempt: Attempt | None, key: str, value: Value, firmware_code: int) -> bool:
    return (
        attempt is not None and attempt.firmware_code == firmware_code
        and same(key, attempt.value, value) and attempt.connects >= NOT_CONVERGING_AFTER
    )  # fmt: skip


def _tried(attempts: Mapping[str, Attempt], key: str, value: Value, firmware_code: int) -> Attempt:
    """The attempt after one more connect that wrote `value` without effect."""
    earlier = attempts.get(key)
    again = (
        earlier is not None and earlier.firmware_code == firmware_code
        and same(key, earlier.value, value)
    )  # fmt: skip
    return Attempt(value, earlier.connects + 1 if again and earlier else 1, firmware_code)


def pending(applied: Applied, sent: Mapping[str, Value], *, firmware_code: int) -> Applied:
    """The state to store before a setter goes out, with the values of it and the setters before
    it (`sent`): each counts as not taken until it is read back, so a write after which the radio
    goes away still counts towards the guard."""
    attempts = dict(applied.attempts)
    for key, value in sent.items():
        attempts[key] = _tried(applied.attempts, key, value, firmware_code)
    return Applied(dict(applied.values), attempts, dict(applied.unsupported))


def record(
    applied: Applied, want: Desired, done: Plan, live_after: Mapping[str, Value | None],
    failed: Mapping[str, bool], *, firmware_code: int,
) -> Applied:  # fmt: skip
    """The state after the writes: `live_after` is read back, `failed` names the writes the
    radio refused (True: as unsupported)."""
    values = dict(applied.values)
    attempts = dict(applied.attempts)
    unsupported = dict(applied.unsupported)
    for key, value in want.values.items():
        took = key not in failed and same(key, live_after.get(key), value)
        if took:  # written and read back, or found on the radio
            values[key] = value
            attempts.pop(key, None)
        elif key in done.writes:
            if failed.get(key):  # the firmware does not know the command
                unsupported[key] = firmware_code
                continue
            # refused or not kept: counted per value, so a new configured value starts afresh
            attempts[key] = _tried(applied.attempts, key, value, firmware_code)
    for key in [k for k in (*values, *attempts) if k not in want.values]:
        values.pop(key, None)  # no longer managed: a later configuration starts afresh
        attempts.pop(key, None)
    return Applied(values, attempts, unsupported)


@dataclass(frozen=True, slots=True)
class ProfileReport:
    """What the reconcile found and did at one connect."""

    mode: str
    firmware_code: int
    tested: bool
    observing: bool
    live: Mapping[str, Value | None]
    desired: Mapping[str, Value]
    written: Mapping[str, Value] = field(default_factory=dict)  # and read back
    reasons: Mapping[str, str] = field(default_factory=dict)  # of the writes: initial, ...
    failed: tuple[str, ...] = ()  # refused by the radio at this connect (unsupported aside)
    not_taken: tuple[str, ...] = ()  # accepted, but read back otherwise
    drift: Mapping[str, Value] = field(default_factory=dict)  # kept as the radio has it
    not_converging: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    contacts_added: tuple[str, ...] = ()
    contacts_favoured: tuple[str, ...] = ()
    contacts_missing: tuple[str, ...] = ()  # protected, but not on the radio (observing)
    contacts_failed: tuple[str, ...] = ()  # protected, refused by the radio (a full table)
    # protected, on the radio, but no favourite (observing)
    contacts_not_favourite: tuple[str, ...] = ()
    radio: str = ""  # the radio's key
    own_position: bool = False  # the radio keeps a position of its own (reported only)
