"""Small persistent state in one JSON file, written atomically.

It holds the newest accepted command timestamp per sender (replay protection, ADR 0009), the
Home Assistant components of the last discovery document and the contact devices announced
(docs/home-assistant.md), the contacts whose retained documents were published, the documents of
the companions whose answers are current (ADR 0024), the radio profile's memory (ADR 0020) and the
telemetry schedule (ADR 0018). The contacts the household excluded from telemetry have a file of
their own (TelemetryExclusions).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeGuard

from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.contacts import is_key
from meshcore_home_gateway.core.profile import Applied, Attempt, Value
from meshcore_home_gateway.core.telemetry import Saved, SavedContact

log = logging.getLogger(__name__)

EXCLUSIONS_FILE = "telemetry-excluded.json"


class JsonStateFile:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        """`read_only`: for a look at the service's state (probe): an unreadable file reads as
        empty and stays where it is; saving is refused."""
        self.path = path
        self.read_only = read_only

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if self.read_only:  # the service's own file, for a look (probe): say so, go on
                log.warning("cannot read %s (%s); taken as empty", self.path, exc.strerror or exc)
                return {}
            raise
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict) and self.read_only:
            return {}
        if not isinstance(data, dict):
            aside = self.path.with_name(self.path.name + ".corrupt")
            self.path.replace(aside)
            log.warning("state file %s unreadable, moved to %s, starting empty", self.path, aside)
            return {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        """Write to a temporary file in the same directory, fsync, then rename over the old one."""
        if self.read_only:
            msg = f"{self.path} is opened read-only"
            raise PermissionError(msg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class CommandLedger:
    """Newest accepted sender timestamp per sender key; only ever moves forward."""

    _KEY = "command_timestamps"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store
        raw = store.load().get(self._KEY, {})
        self._last: dict[str, int] = {}
        if isinstance(raw, dict):
            self._last = {k: v for k, v in raw.items() if isinstance(v, int)}

    def last(self, sender_key: str) -> int | None:
        return self._last.get(sender_key)

    @property
    def view(self) -> Mapping[str, int]:
        return MappingProxyType(self._last)

    def record(self, sender_key: str, timestamp: int) -> None:
        """Store the timestamp durably; memory follows only after a successful save."""
        if timestamp <= self._last.get(sender_key, -1):
            return
        updated = {**self._last, sender_key: timestamp}
        data = self._store.load()
        data[self._KEY] = updated
        self._store.save(data)
        self._last = updated


class AnnouncedComponents:
    """The components of the last discovery document (key -> platform), and the earlier ones
    that a new configuration no longer has (a removed target, command words or rx log switched
    off), which the bridge removes in Home Assistant.

    The removals stay pending for REMOVAL_KEEP_S: the gateway cannot tell when Home Assistant has
    received one (it may be down, or the broker), and sending one again does no harm.
    """

    def __init__(self, store: JsonStateFile, document: str | None = None) -> None:
        """`document`: another discovery document of the gateway's device (its object ID)."""
        self._store = store
        suffix = f"_{document}" if document else ""
        self._key = f"ha_components{suffix}"
        self._removed = f"ha_removed{suffix}"  # key -> {"p": platform, "since": unix time}

    def replace(self, current: Mapping[str, str], now: float) -> dict[str, str]:
        """Store the current components; return the pending removals (key -> platform)."""
        data = self._store.load()
        previous = _string_map(data.get(self._key))
        removed: dict[str, dict[str, Any]] = {}
        raw = data.get(self._removed)
        for key, entry in raw.items() if isinstance(raw, dict) else ():
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("p"), str)
                and isinstance(entry.get("since"), (int, float))
            ):
                removed[key] = {"p": entry["p"], "since": entry["since"]}
        for key, platform in previous.items():
            if key not in current:
                removed.setdefault(key, {"p": platform, "since": now})
        removed = {
            key: entry for key, entry in removed.items()
            if key not in current and now - entry["since"] < REMOVAL_KEEP_S
        }  # fmt: skip
        if previous != current or data.get(self._removed, {}) != removed:
            data[self._key] = dict(current)
            data[self._removed] = removed
            try:
                self._store.save(data)
            except OSError as exc:  # pending removals come back from `previous` next time
                log.warning("cannot store the Home Assistant components: %s", exc)
        return {key: entry["p"] for key, entry in removed.items()}


REMOVAL_KEEP_S = 30 * 86_400.0


@dataclass(slots=True)
class DeviceRecord:
    """A contact device announced to Home Assistant: the slug of its entity IDs, chosen when it
    first appeared, and every component it was announced with (key -> platform)."""

    slug: str
    components: dict[str, str]


@dataclass(slots=True)
class RemovedDevice:
    components: dict[str, str]
    since: float


class ContactDeviceRecords:
    """The contact devices announced to Home Assistant, the removals still pending (until Home
    Assistant's next start), the contacts the user deleted there, which stay hidden, and the slug
    of every contact that had a device, so that its entity IDs stay when it comes back."""

    _KEY = "ha_contact_devices"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store
        raw = store.load().get(self._KEY)
        data = raw if isinstance(raw, dict) else {}
        self.announced: dict[str, DeviceRecord] = {}
        self.removed: dict[str, RemovedDevice] = {}
        self.hidden: set[str] = set()
        self.slugs: dict[str, str] = _string_map(data.get("slugs"))
        announced, removed, hidden = data.get("announced"), data.get("removed"), data.get("hidden")
        for key, entry in announced.items() if isinstance(announced, dict) else ():
            if isinstance(entry, dict) and isinstance(slug := entry.get("slug"), str):
                self.announced[key] = DeviceRecord(slug, _string_map(entry.get("cmps")))
                self.slugs.setdefault(key, slug)
        for key, entry in removed.items() if isinstance(removed, dict) else ():
            if isinstance(entry, dict) and isinstance(since := entry.get("since"), (int, float)):
                self.removed[key] = RemovedDevice(_string_map(entry.get("cmps")), float(since))
        self.hidden = (
            {k for k in hidden if isinstance(k, str)} if isinstance(hidden, list) else set()
        )
        self._saved = self._data()

    def save(self) -> None:
        """Store the records if they changed; a failed write is logged (the next change tries
        again, and pending removals are sent again at the next start)."""
        data = self._data()
        if data == self._saved:
            return
        stored = self._store.load()
        stored[self._KEY] = data
        try:
            self._store.save(stored)
        except OSError as exc:
            log.warning("cannot store the Home Assistant contact devices: %s", exc)
            return
        self._saved = data

    def _data(self) -> dict[str, Any]:
        return {
            "announced": {
                key: {"slug": r.slug, "cmps": dict(r.components)}
                for key, r in self.announced.items()
            },
            "removed": {
                key: {"cmps": dict(r.components), "since": r.since}
                for key, r in self.removed.items()
            },
            "hidden": sorted(self.hidden),
            "slugs": dict(sorted(self.slugs.items())),
        }


class RetainedKeys:
    """Keys of the per-object retained topics the gateway published (the contacts), so that it can
    clear the topics of objects that went away while it was stopped."""

    def __init__(self, store: JsonStateFile, name: str) -> None:
        self._store = store
        self._name = f"retained_{name}"
        raw = store.load().get(self._name)
        self._keys = {k for k in raw if isinstance(k, str)} if isinstance(raw, list) else set()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._keys)

    def replace(self, keys: set[str]) -> None:
        """Store the published keys; a failed write is logged (the next change tries again)."""
        if keys == self._keys:
            return
        data = self._store.load()
        data[self._name] = sorted(keys)
        try:
            self._store.save(data)
        except OSError as exc:
            log.warning("cannot store the published %s: %s", self._name, exc)
            return
        self._keys = set(keys)


class CurrentDocuments:
    """The last published document (`contacts/<key>`) of each companion whose answers count as
    current (ADR 0024): after a restart without the radio, the observer publishes them again
    once they expire, and keeps them in their latest state until a table read."""

    _KEY = "current_contacts"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store
        raw = store.load().get(self._KEY)
        self._documents = {
            key: document for key, document in (raw.items() if isinstance(raw, dict) else ())
            if isinstance(key, str) and is_key(key) and isinstance(document, dict)
        }  # fmt: skip

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return dict(self._documents)

    def replace(self, documents: Mapping[str, dict[str, Any]]) -> None:
        """Store the documents if they changed; a failed write is logged (the next call tries
        again)."""
        if documents == self._documents:
            return
        try:
            data = self._store.load()
            data[self._KEY] = dict(documents)
            self._store.save(data)
        except OSError as exc:
            log.warning("cannot store the current contact documents: %s", exc)
            return
        self._documents = dict(documents)


class TelemetryExclusions:
    """The contacts the household excluded from telemetry (ADR 0018), in a file of their
    own: kept until included again, also while they are not in the radio's table and while
    telemetry is off. Written atomically, only when the set changed."""

    LIMIT = 1024
    _KEY = "excluded"

    def __init__(self, path: Path | None) -> None:
        """Loads at once. A missing file: none excluded. An OSError stops the service (as
        state.json). A file that cannot be parsed, or holds no list under "excluded", is left in
        place: `unreadable` is set, an error is logged once, nothing is ever written to it, and
        telemetry stays paused until it is fixed or deleted and the service restarted. Entries
        that are not 64 lower-case hex digits are skipped (a warning); at most LIMIT kept.
        None: memory only (tests, and the observer's default)."""
        self._path = path
        self._keys: set[str] = set()
        self._stored: frozenset[str] = frozenset()  # what the file holds
        self._warned = False  # a change is not stored, and the log said so
        self.unreadable = False
        if path is not None:
            self._load(path)

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._keys)

    @property
    def pending(self) -> bool:
        """A change is not stored yet."""
        return self._keys != self._stored

    def exclude(self, key: str) -> bool:
        """Excluded from now on, also if the file cannot be written. True when the set is stored
        (also unchanged with nothing pending); False if not: a warning, once while unsaved, and
        flush() tries again."""
        if key in self._keys and not self.pending:
            return True
        self._keys.add(key)
        return self._store()

    def include(self, key: str) -> bool:
        """Asked again, only once stored: False and nothing changed if the file cannot be
        written or is unreadable."""
        if self.unreadable:
            return False
        if key not in self._keys:
            return True  # not excluded: nothing to store
        rest = self._keys - {key}
        why = self._write(rest)
        if why is not None:
            log.warning("cannot write the telemetry exclusions (%s): %s stays excluded", why,
                        key[:12])  # fmt: skip
            return False
        self._keys = rest
        self._stored_again()
        return True

    def flush(self) -> None:
        """Store a pending change; never raises; an info line once it is stored. Called at
        every status tick (bridge)."""
        if self.pending:
            self._store()

    def _store(self) -> bool:
        """The set as it is in memory: a warning once while it is not stored."""
        why = self._write(self._keys)
        if why is None:
            self._stored_again()
            return True
        if not self._warned:
            self._warned = True
            until = "until the service stops" if self.unreadable else "stored once it can be"
            log.warning("cannot store the telemetry exclusions (%s): in effect, %s", why, until)
        return False

    def _stored_again(self) -> None:
        if self._warned:
            self._warned = False
            log.info("telemetry exclusions stored")

    def _write(self, keys: set[str]) -> str | None:
        """None once `keys` are stored, else why not."""
        if self._path is not None:
            if self.unreadable:
                return f"{self._path} is unreadable"
            try:
                JsonStateFile(self._path).save({self._KEY: sorted(keys)})
            except OSError as exc:
                return str(exc.strerror or exc)
        self._stored = frozenset(keys)
        return None

    def _load(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:  # none excluded; another OSError stops the service
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:  # not UTF-8, not JSON
            data = None
        entries = data.get(self._KEY) if isinstance(data, dict) else None
        if not isinstance(entries, list):
            self.unreadable = True
            log.error("telemetry exclusions %s cannot be read: telemetry paused, nothing "
                      "written to the file; fix or delete it and restart the service",
                      path)  # fmt: skip
            return
        keys = sorted({e for e in entries if isinstance(e, str) and is_key(e)})
        skipped = sum(not (isinstance(e, str) and is_key(e)) for e in entries)
        if skipped:
            log.warning("telemetry exclusions %s: %d entries that are no contact keys skipped",
                        path, skipped)  # fmt: skip
        if len(keys) > self.LIMIT:
            log.warning("telemetry exclusions %s: %d excluded contacts, %d kept", path,
                        len(keys), self.LIMIT)  # fmt: skip
        self._keys = set(keys[: self.LIMIT])
        self._stored = frozenset(self._keys)


class ProfileRecords:
    """What the managed radio profile last wrote (ADR 0020), per radio key: a replaced radio
    starts afresh."""

    _KEY = "radio_profile"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store

    def load(self, radio_key: str) -> Applied:
        raw = self._store.load().get(self._KEY)
        entry = raw.get(radio_key) if isinstance(raw, dict) else None
        if not isinstance(entry, dict):
            return Applied()
        values, attempts = entry.get("values"), entry.get("attempts")
        tried: dict[str, Attempt] = {}
        for key, item in attempts.items() if isinstance(attempts, dict) else ():
            if not isinstance(item, dict):
                continue
            value, connects, firmware = (
                item.get("value"),
                item.get("connects"),
                item.get("firmware"),
            )
            if _setting(value) and _count(connects) and _count(firmware):
                tried[key] = Attempt(value, connects, firmware)
        return Applied(
            values={
                k: v for k, v in values.items() if _setting(v)
            } if isinstance(values, dict) else {},
            attempts=tried,
            unsupported=_int_map(entry.get("unsupported")),
        )  # fmt: skip

    def save(self, radio_key: str, applied: Applied) -> None:
        """Store the result; a failed write is logged (the profile keeps its memory of this run,
        and the next change tries again)."""
        entry = {
            "values": dict(applied.values),
            "attempts": {
                key: {"value": a.value, "connects": a.connects, "firmware": a.firmware_code}
                for key, a in applied.attempts.items()
            },
            "unsupported": dict(applied.unsupported),
        }
        try:
            data = self._store.load()
            raw = data.get(self._KEY)
            radios = raw if isinstance(raw, dict) else {}
            if radios.get(radio_key) == entry:
                return
            data[self._KEY] = {**radios, radio_key: entry}
            self._store.save(data)
        except OSError as exc:
            log.warning("cannot store the radio profile: %s", exc)


class TelemetryRecords:
    """The telemetry schedule (ADR 0018): times relative to when it was saved, no position."""

    _KEY = "telemetry"
    VERSION = 1

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store

    def load(self, wall_now: float) -> tuple[Saved | None, float | None]:
        """The schedule and the seconds since it was saved (None if unknown: no time saved, or
        the clock went back); invalid entries are skipped. Never raises: an unreadable file is
        logged."""
        try:
            raw = self._store.load().get(self._KEY)
        except Exception as exc:  # noqa: BLE001 - the schedule starts empty, the gateway goes on
            log.warning("telemetry: cannot read the saved schedule (%s); starting empty", exc)
            return None, 0.0
        if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
            return None, 0.0
        saved_at = raw.get("saved_at")
        elapsed = wall_now - saved_at if _finite(saved_at) and wall_now >= saved_at else None
        entries = raw.get("contacts")
        contacts: dict[str, SavedContact] = {}
        for key, entry in entries.items() if isinstance(entries, dict) else ():
            contact = _saved_contact(entry) if isinstance(key, str) and is_key(key) else None
            if contact is not None:
                contacts[key] = contact
        ages = raw.get("floods_age_s")
        floods = (
            tuple(sorted(a for a in ages if _age(a) and a < telemetry.HOUR_S))
            if isinstance(ages, list)
            else ()
        )  # fmt: skip
        own, mesh = raw.get("own_debt_s", 0.0), raw.get("mesh_debt_s", 0.0)
        hold = raw.get("hold_left_s")
        return Saved(
            contacts, floods[: telemetry.FLOODS_PER_HOUR],
            min(own, telemetry.HOUR_S) if _age(own) else 0.0,
            min(mesh, telemetry.HOUR_S) if _age(mesh) else 0.0,
            min(hold, telemetry.MAX_DUE_S) if _age(hold) else None,
            raw.get("hold_pending") is True,
        ), elapsed  # fmt: skip

    def ran_before(self) -> bool:
        """The state file has a telemetry record (any version): telemetry ran on this
        directory. False also when the file cannot be read (then the hold applies: fewer
        requests, never more)."""
        try:
            return isinstance(self._store.load().get(self._KEY), dict)
        except Exception:  # noqa: BLE001 - load() logs nothing here; the hold is the safe side
            return False

    def save(self, saved: Saved, wall_now: float) -> None:
        """Replace this key only; a failed write is logged and the schedule stays in memory."""
        entry = {
            "version": self.VERSION,
            "saved_at": wall_now,
            "contacts": {
                key: {
                    "due_in_s": c.due_in_s, "failures": c.failures,
                    "answered_age_s": c.answered_age_s, "polled_age_s": c.polled_age_s,
                    "flood_age_s": c.flood_age_s, "floods": c.floods, "located": c.located,
                    "still": c.still, "lpp_bytes": c.lpp_bytes, "parked_age_s": c.parked_age_s,
                    "current_s": c.current_s, "located_age_s": c.located_age_s,
                }
                for key, c in saved.contacts.items()
            },
            "floods_age_s": list(saved.floods_age_s),
            "own_debt_s": saved.own_debt_s,
            "mesh_debt_s": saved.mesh_debt_s,
            "hold_left_s": saved.hold_left_s,
            "hold_pending": saved.hold_pending,
        }  # fmt: skip
        try:
            data = self._store.load()
            data[self._KEY] = entry
            self._store.save(data)
        except OSError as exc:
            log.warning("telemetry: cannot store the schedule: %s", exc)


def _saved_contact(entry: object) -> SavedContact | None:
    if not isinstance(entry, dict):
        return None
    due, failures, floods = entry.get("due_in_s"), entry.get("failures", 0), entry.get("floods", 0)
    answered, polled = entry.get("answered_age_s"), entry.get("polled_age_s")
    flood, parked = entry.get("flood_age_s"), entry.get("parked_age_s")
    located, still = entry.get("located", False), entry.get("still", telemetry.STILL_POLLS)
    lpp_bytes = entry.get("lpp_bytes", telemetry.DEFAULT_LPP_BYTES)
    current = entry.get("current_s", telemetry.CURRENT_MIN_S)
    located_age = entry.get("located_age_s")
    if (
        _age(due) and _small(failures, telemetry.MAX_FAILURES)
        and _small(floods, telemetry.MAX_FAILURES) and _age_or_none(answered)
        and _age_or_none(polled) and _age_or_none(flood) and _age_or_none(parked)
        and isinstance(located, bool)
        and _small(still, telemetry.STILL_POLLS) and _small(lpp_bytes, telemetry.MAX_LPP_BYTES)
        and lpp_bytes >= 1 and _age(current) and _age_or_none(located_age)
    ):  # fmt: skip
        return SavedContact(
            min(due, telemetry.MAX_DUE_S), failures, answered, polled, flood, floods, located,
            still, lpp_bytes, parked, min(current, telemetry.MAX_DUE_S), located_age,
        )  # fmt: skip
    return None


def _finite(value: object) -> TypeGuard[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _age(value: object) -> TypeGuard[float]:
    return _finite(value) and value >= 0


def _age_or_none(value: object) -> TypeGuard[float | None]:
    return value is None or _age(value)


def _small(value: object, limit: int) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit


def _setting(value: object) -> TypeGuard[Value]:
    return isinstance(value, (bool, int, str))


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _int_map(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: value for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }  # fmt: skip


def _string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, str)}
