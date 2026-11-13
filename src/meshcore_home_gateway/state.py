"""Small persistent state in one JSON file, held in memory and written atomically.

It holds the newest accepted command timestamp per sender (replay protection, ADR 0009), the
times of its recent wrong codes and the latest refused commands, the Home Assistant components
of the last discovery document and the contact devices announced (docs/home-assistant.md), the
contacts whose retained documents were published, the documents of the companions whose answers
are current (ADR 0024), the last checked advert of each companion (ADR 0018), the radio profile's
memory (ADR 0020) and the telemetry schedule (ADR 0018). The contacts the household excluded from
telemetry have a file of their own (TelemetryExclusions).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeGuard

from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.commands import (
    CODE_FAILURE_LIMIT,
    REFUSALS_KEPT,
    Refusal,
    codes_locked,
    recent_code_failures,
)
from meshcore_home_gateway.core.contacts import is_key
from meshcore_home_gateway.core.mesh import AdvertCheck
from meshcore_home_gateway.core.profile import Applied, Attempt, Value
from meshcore_home_gateway.core.telemetry import Saved, SavedContact
from meshcore_home_gateway.core.text import clean_name

log = logging.getLogger(__name__)

EXCLUSIONS_FILE = "telemetry-excluded.json"


class JsonStateFile:
    """The state file (`state.json`), the one store of the sections below (ARC-004): read once,
    held in memory and written whole and atomically. The sections read and change their part
    here, never in the file: `put()` changes one, which the next `flush()` writes, with every
    other change; the bridge flushes at its status tick and at its stop. What must survive a
    crash is written at once (`flush()` after `put()`, or `save()` for the command ledger, which
    must be on disk before a command is published). A file replaced while the service runs is
    overwritten with every section by the next write: restore one with the service stopped."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        """`read_only`: for a look at the service's state (probe): an unreadable file reads as
        empty and stays where it is; saving is refused."""
        self.path = path
        self.read_only = read_only
        self.failed = False  # the last write failed (the health section, ARC-012)
        self._data: dict[str, Any] | None = None  # the document, once read
        self._changed = False  # put() since the last write
        self._warned = False  # a flush failed, and the log said so

    def load(self) -> dict[str, Any]:
        """The document (a copy of its top level). The file is read at the first call only; an
        OSError then is raised, and the next call reads again."""
        return dict(self._document())

    def put(self, key: str, value: Any) -> None:
        """Change one section; the next flush() writes it."""
        self._document()[key] = value
        self._changed = True

    def drop(self, key: str) -> None:
        """Remove one section, if there is one; the next flush() writes it."""
        if self._document().pop(key, None) is not None:
            self._changed = True

    def save(self, data: dict[str, Any]) -> None:
        """Make `data` the document and write it at once, with the changes put() before. An
        OSError if it cannot be written: the document stays as it was."""
        self._write(data)
        self._data, self._changed = data, False

    def flush(self) -> bool:
        """Write the changes put() since the last write, and the document again after a write
        that failed, a save() too: `failed` stays only while the file cannot be written (#94).
        Never raises an OSError: False while it cannot be, with a warning once, and an info line
        once it is again."""
        if not (self._changed or self.failed) or self._data is None:
            return True
        try:
            self._write(self._data)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                log.warning(
                    "cannot write the state file %s (%s): its changes are kept in "
                    "memory and written once it can be",
                    self.path,
                    exc.strerror or exc,
                )
            return False
        self._changed = False
        if self._warned:
            self._warned = False
            log.info("state file %s written again", self.path)
        return True

    def _document(self) -> dict[str, Any]:
        if self._data is None:
            self._data = self._read()
        return self._data

    def _read(self) -> dict[str, Any]:
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

    def _write(self, data: dict[str, Any]) -> None:
        """Write to a temporary file in the same directory, fsync, then rename over the old one."""
        if self.read_only:
            msg = f"{self.path} is opened read-only"
            raise PermissionError(msg)
        self.failed = True  # until the write is through
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
        self.failed = False


class CommandLedger:
    """Newest accepted sender timestamp per sender key, and the time step of the newest accepted
    code of a sender with a code secret; both only ever move forward. Also the times of each
    sender's recent wrong codes (#79), so that a restart gives no new guesses, and the latest
    refused commands (#141), so that a restart still shows them."""

    _KEY = "command_timestamps"
    _CODES = "command_code_steps"
    _FAILURES = "command_code_failures"
    _REFUSED = "command_refusals"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store
        data = store.load()
        self._last = _int_map(data.get(self._KEY))
        self._steps = _int_map(data.get(self._CODES))
        self._failures = _times_map(data.get(self._FAILURES))
        self._refused = _refusals(data.get(self._REFUSED))

    def last(self, sender_key: str) -> int | None:
        return self._last.get(sender_key)

    @property
    def store(self) -> JsonStateFile:
        """The state file, which it shares with the other sections."""
        return self._store

    @property
    def view(self) -> Mapping[str, int]:
        return MappingProxyType(self._last)

    @property
    def code_steps(self) -> Mapping[str, int]:
        return MappingProxyType(self._steps)

    @property
    def code_failures(self) -> Mapping[str, tuple[int, ...]]:
        """The wall-clock times of each sender's recent wrong codes, oldest first."""
        return MappingProxyType(self._failures)

    def code_failure(self, sender_key: str, now: float) -> bool:
        """A wrong code of the sender at wall-clock time `now`; True when it reaches the limit,
        which locks the sender's codes (core.commands.codes_locked). Only the recent ones are
        kept, at most the limit per sender. Written at once; a file that cannot be written
        keeps it in memory, with a warning (JsonStateFile.flush)."""
        before = self._failures.get(sender_key, ())
        failures = {}
        for key, times in {**self._failures, sender_key: (*before, int(now))}.items():
            if recent := recent_code_failures(times, now)[-CODE_FAILURE_LIMIT:]:
                failures[key] = tuple(int(t) for t in recent)
        self._failures = failures
        self._store.put(self._FAILURES, {key: list(times) for key, times in failures.items()})
        self._store.flush()
        return not codes_locked(before, now) and codes_locked(failures[sender_key], now)

    @property
    def refused(self) -> tuple[Refusal, ...]:
        """The latest refused commands, the newest first, at most REFUSALS_KEPT."""
        return self._refused

    def refusal(self, refusal: Refusal) -> None:
        """A refused command, the newest. The next flush writes it (the status tick, the stop):
        a crash may lose it, which costs only what a front end shows."""
        self._refused = (refusal, *self._refused)[:REFUSALS_KEPT]
        self._store.put(self._REFUSED, [_refusal_entry(r) for r in self._refused])

    def clean_names(self) -> None:
        """With `[messages] clean_received`, at the start: the senders' names that a run with it
        off kept as they came, cleaned as the inbox cleans a new refusal's (#137). Stored at once
        when one changed, as the observer stores a cleaned document of the last run."""
        cleaned = tuple(
            r if r.sender_name is None else replace(r, sender_name=clean_name(r.sender_name))
            for r in self._refused
        )
        if cleaned != self._refused:
            self._refused = cleaned
            self._store.put(self._REFUSED, [_refusal_entry(r) for r in cleaned])
            self._store.flush()

    def record(self, sender_key: str, timestamp: int, code_step: int | None = None) -> None:
        """Store the timestamp (and the code's time step) durably; memory follows only after a
        successful save."""
        steps = self._steps
        if code_step is not None and code_step > steps.get(sender_key, -1):
            steps = {**steps, sender_key: code_step}
        if timestamp <= self._last.get(sender_key, -1) and steps is self._steps:
            return
        last = {**self._last, sender_key: max(timestamp, self._last.get(sender_key, timestamp))}
        data = self._store.load()
        data[self._KEY] = last
        if steps:
            data[self._CODES] = steps
        self._store.save(data)
        self._last, self._steps = last, steps


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
            key: entry
            for key, entry in removed.items()
            if key not in current and now - entry["since"] < REMOVAL_KEEP_S
        }
        if previous != current or data.get(self._removed, {}) != removed:
            # pending removals also come back from `previous` while the file cannot be written
            self._store.put(self._key, dict(current))
            self._store.put(self._removed, removed)
        return {key: entry["p"] for key, entry in removed.items()}

    def announced(self) -> dict[str, str]:
        """Every component Home Assistant may still have (key -> platform): those of the last
        document and the pending removals; for `retire`, which removes them all."""
        data = self._store.load()
        raw = data.get(self._removed)
        pending = {
            key: entry["p"]
            for key, entry in (raw.items() if isinstance(raw, dict) else ())
            if isinstance(entry, dict) and isinstance(entry.get("p"), str)
        }
        return pending | _string_map(data.get(self._key))


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
    of every contact that had a device, so that its entity IDs stay when it comes back: for
    SLUG_KEEP_S after it left the radio's table, at most SLUGS_LEFT of those (CODE-003)."""

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
        left = data.get("left")  # key -> when the contact left the table (unix time)
        self.left: dict[str, float] = {
            key: float(at)
            for key, at in (left.items() if isinstance(left, dict) else ())
            if key in self.slugs and _finite(at)
        }
        self._saved = self._data()

    def prune(self, present: Collection[str], now: float) -> None:
        """Forget the slugs of contacts that left the table (are not `present`) SLUG_KEEP_S ago,
        and of those beyond the SLUGS_LEFT that left last."""
        present = set(present)
        for key in present & self.left.keys():
            del self.left[key]
        for key in self.slugs.keys() - present - self.left.keys():
            self.left[key] = now
        newest = sorted(self.left, key=lambda key: (-self.left[key], key))
        for rank, key in enumerate(newest):
            if rank >= SLUGS_LEFT or now - self.left[key] >= SLUG_KEEP_S:
                del self.slugs[key], self.left[key]

    def save(self) -> None:
        """Hand the records to the state file if they changed (its next flush writes them)."""
        data = self._data()
        if data != self._saved:
            self._store.put(self._KEY, data)
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
            "left": dict(sorted(self.left.items())),
        }


SLUG_KEEP_S = 90 * 86_400.0
SLUGS_LEFT = 1000


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
        """Hand the published keys to the state file if they changed."""
        if keys != self._keys:
            self._store.put(self._name, sorted(keys))
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
            key: document
            for key, document in (raw.items() if isinstance(raw, dict) else ())
            if isinstance(key, str) and is_key(key) and isinstance(document, dict)
        }

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return dict(self._documents)

    def replace(self, documents: Mapping[str, dict[str, Any]]) -> None:
        """Store the documents at once if they changed (the observer stores them when a crash
        must not lose the change, else at its tick); a failed write is the state file's to
        log and to try again."""
        if documents != self._documents:
            self._store.put(self._KEY, dict(documents))
            self._documents = dict(documents)
            self._store.flush()


class AdvertChecks:
    """The newest advert of each companion whose content the gateway checked (ADR 0018), so that
    a restart neither raises nor clears the check: `advert_checks` = {key: {"advert_at": int,
    "carries": bool}}. No position."""

    _KEY = "advert_checks"

    def __init__(self, store: JsonStateFile) -> None:
        """Loads at once. Invalid entries (a key the gateway does not write, `advert_at` not an
        int ≥ 0, `carries` not a bool, an entry not an object) are skipped with one warning."""
        self._store = store
        raw = store.load().get(self._KEY, {})
        entries = raw.items() if isinstance(raw, dict) else ()
        self._checks = {
            key: check for key, entry in entries if (check := _advert_check(key, entry)) is not None
        }
        if not isinstance(raw, dict) or len(self._checks) < len(raw):
            log.warning(
                "state file %s: invalid advert checks skipped, %d kept",
                store.path,
                len(self._checks),
            )

    @property
    def checks(self) -> dict[str, AdvertCheck]:
        return dict(self._checks)

    def replace(self, checks: Mapping[str, AdvertCheck]) -> None:
        """Store them at once if they changed: a check that no longer carries a position must
        not come back after a crash (ADR 0025); a failed write is the state file's to log and to
        try again."""
        if checks != self._checks:
            self._store.put(
                self._KEY,
                {
                    key: {"advert_at": check.at, "carries": check.carries}
                    for key, check in checks.items()
                },
            )
            self._checks = dict(checks)
            self._store.flush()


def _advert_check(key: object, entry: object) -> AdvertCheck | None:
    if not (isinstance(key, str) and is_key(key) and isinstance(entry, dict)):
        return None
    at, carries = entry.get("advert_at"), entry.get("carries")
    # the sender's clock, 32 bits as the record's `last_advert`
    if _small(at, 0xFFFFFFFF) and isinstance(carries, bool):
        return AdvertCheck(at, carries)
    return None


class TelemetryExclusions:
    """The contacts the household excluded from telemetry (ADR 0018) and, for `[telemetry] ask =
    "household"`, those it included (SEC-016), in a file of their own: kept until changed, also
    while they are not in the radio's table and while telemetry is off. Written atomically, only
    when a set changed. `key in exclusions`: the contact is not asked."""

    LIMIT = 1024  # of each set
    _KEY = "excluded"
    _INCLUDED = "included"

    def __init__(
        self,
        path: Path | None,
        *,
        household: bool = False,
        protected: frozenset[str] = frozenset(),
    ) -> None:
        """Loads at once. A missing file: none excluded. An OSError stops the service (as
        state.json). A file that cannot be parsed, or holds no list under "excluded" (or something
        else than a list under "included"), is left in place: `unreadable` is set, an error is
        logged once, nothing is ever written to it, and telemetry stays paused until it is fixed
        or deleted and the service restarted. Entries that are not 64 lower-case hex digits are
        skipped (a warning); at most LIMIT of each kept. None: memory only (tests, and the
        observer's default). `household`: only the included contacts and the `protected` keys
        are asked, unless excluded; inclusions are kept only then."""
        self._path = path
        self._household = household
        self._protected = protected
        self._keys: set[str] = set()
        self._included: set[str] = set()
        # what the file holds: excluded, included
        self._stored: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
        self._warned = False  # a change is not stored, and the log said so
        self.unreadable = False
        if path is not None:
            self._load(path)

    def __contains__(self, key: object) -> bool:
        """Not asked: excluded, or with `household` neither included nor protected."""
        if key in self._keys:
            return True
        return self._household and key not in self._included and key not in self._protected

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def household(self) -> bool:
        return self._household

    @property
    def keys(self) -> frozenset[str]:
        """The keys excluded."""
        return frozenset(self._keys)

    @property
    def included(self) -> frozenset[str]:
        return frozenset(self._included)

    @property
    def pending(self) -> bool:
        """A change is not stored yet."""
        return (frozenset(self._keys), frozenset(self._included)) != self._stored

    @property
    def health(self) -> str:
        """For the health section: `unreadable` (telemetry paused), `unsaved` (a change waits
        to be stored) or `ok`."""
        return "unreadable" if self.unreadable else "unsaved" if self.pending else "ok"

    def exclude(self, key: str) -> bool:
        """Excluded from now on, also if the file cannot be written. True when the sets are
        stored (also unchanged with nothing pending); False if not: a warning, once while
        unsaved, and flush() tries again."""
        if key in self._keys and key not in self._included and not self.pending:
            return True
        self._keys.add(key)
        self._included.discard(key)
        return self._store()

    def include(self, key: str) -> bool:
        """Asked again, only once stored: False and nothing changed if the file cannot be
        written or is unreadable. With `household` it is kept as included."""
        if self.unreadable:
            return False
        rest = self._keys - {key}
        included = self._included | {key} if self._household else self._included
        if rest == self._keys and included == self._included:
            return True  # nothing to store
        why = self._write(rest, included)
        if why is not None:
            log.warning(
                "cannot write the telemetry exclusions (%s): %s stays excluded", why, key[:12]
            )
            return False
        self._keys, self._included = rest, included
        self._stored_again()
        return True

    def flush(self) -> None:
        """Store a pending change; never raises; an info line once it is stored. Called at
        every status tick (bridge)."""
        if self.pending:
            self._store()

    def _store(self) -> bool:
        """The sets as they are in memory: a warning once while they are not stored."""
        why = self._write(self._keys, self._included)
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

    def _write(self, keys: set[str], included: set[str]) -> str | None:
        """None once the sets are stored, else why not. Without inclusions the file is as an
        earlier version wrote it, which ignores them."""
        if self._path is not None:
            if self.unreadable:
                return f"{self._path} is unreadable"
            document: dict[str, list[str]] = {self._KEY: sorted(keys)}
            if included:
                document[self._INCLUDED] = sorted(included)
            try:
                JsonStateFile(self._path).save(document)
            except OSError as exc:
                return str(exc.strerror or exc)
        self._stored = (frozenset(keys), frozenset(included))
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
        included = data.get(self._INCLUDED, []) if isinstance(data, dict) else None
        if not isinstance(entries, list) or not isinstance(included, list):
            self.unreadable = True
            log.error(
                "telemetry exclusions %s cannot be read: telemetry paused, nothing "
                "written to the file; fix or delete it and restart the service",
                path,
            )
            return
        self._keys = self._valid(path, entries, "excluded")
        self._included = self._valid(path, included, "included") - self._keys
        self._stored = (frozenset(self._keys), frozenset(self._included))

    def _valid(self, path: Path, entries: list[object], what: str) -> set[str]:
        keys = sorted({e for e in entries if isinstance(e, str) and is_key(e)})
        skipped = sum(not (isinstance(e, str) and is_key(e)) for e in entries)
        if skipped:
            log.warning(
                "telemetry exclusions %s: %d entries that are no contact keys skipped",
                path,
                skipped,
            )
        if len(keys) > self.LIMIT:
            log.warning(
                "telemetry exclusions %s: %d %s contacts, %d kept",
                path,
                len(keys),
                what,
                self.LIMIT,
            )
        return set(keys[: self.LIMIT])


class ProfileRecords:
    """What the managed radio profile last wrote (ADR 0020), per radio key: a replaced radio
    starts afresh. Kept for PROFILE_RADIOS radios, the current one and those last saved before
    (CODE-003)."""

    _KEY = "radio_profile"

    def __init__(
        self, store: JsonStateFile, *, wall_clock: Callable[[], float] = time.time
    ) -> None:
        self._store = store
        self._wall = wall_clock

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
            values={k: v for k, v in values.items() if _setting(v)}
            if isinstance(values, dict)
            else {},
            attempts=tried,
            unsupported=_int_map(entry.get("unsupported")),
            disabled=_int_map(entry.get("disabled")),
        )

    def save(self, radio_key: str, applied: Applied) -> None:
        """Store the result at once (the attempts before the setters go out), with the time
        (`at`) that ranks the radios; a file that cannot be read or written is logged (the
        profile keeps its memory of this run)."""
        entry: dict[str, Any] = {
            "values": dict(applied.values),
            "attempts": {
                key: {"value": a.value, "connects": a.connects, "firmware": a.firmware_code}
                for key, a in applied.attempts.items()
            },
            "unsupported": dict(applied.unsupported),
            "disabled": dict(applied.disabled),
        }
        try:
            raw = self._store.load().get(self._KEY)
        except OSError as exc:
            log.warning("cannot store the radio profile: %s", exc)
            return
        others = {
            key: other
            for key, other in (raw.items() if isinstance(raw, dict) else ())
            if key != radio_key and isinstance(other, dict)
        }
        stored = raw.get(radio_key) if isinstance(raw, dict) else None
        unchanged = (
            isinstance(stored, dict)
            and {key: value for key, value in stored.items() if key != "at"} == entry
        )
        if unchanged and len(others) < PROFILE_RADIOS:
            return
        kept = sorted(others, key=lambda key: (-_time(others[key].get("at")), key))
        self._store.put(
            self._KEY,
            {
                **{key: others[key] for key in kept[: PROFILE_RADIOS - 1]},
                radio_key: stored if unchanged else {**entry, "at": self._wall()},
            },
        )
        self._store.flush()


PROFILE_RADIOS = 3


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
        )
        own, mesh = raw.get("own_debt_s", 0.0), raw.get("mesh_debt_s", 0.0)
        hold = raw.get("hold_left_s")
        return Saved(
            contacts,
            floods[: telemetry.FLOODS_PER_HOUR],
            min(own, telemetry.HOUR_S) if _age(own) else 0.0,
            min(mesh, telemetry.HOUR_S) if _age(mesh) else 0.0,
            min(hold, telemetry.MAX_DUE_S) if _age(hold) else None,
            raw.get("hold_pending") is True,
        ), elapsed

    def ran_before(self) -> bool:
        """The state file has a telemetry record (any version): telemetry ran on this
        directory. False also when the file cannot be read (then the hold applies: fewer
        requests, never more)."""
        try:
            return isinstance(self._store.load().get(self._KEY), dict)
        except Exception:  # noqa: BLE001 - load() logs nothing here; the hold is the safe side
            return False

    def save(self, saved: Saved, wall_now: float, *, at_once: bool = False) -> None:
        """Hand this key to the state file, which writes it at its next flush or, `at_once`,
        now (after a flood, so that a crash cannot repeat it); a file that cannot be read is
        logged and the schedule stays in memory."""
        entry = {
            "version": self.VERSION,
            "saved_at": wall_now,
            "contacts": {
                key: {
                    "due_in_s": c.due_in_s,
                    "failures": c.failures,
                    "answered_age_s": c.answered_age_s,
                    "polled_age_s": c.polled_age_s,
                    "flood_age_s": c.flood_age_s,
                    "floods": c.floods,
                    "located": c.located,
                    "still": c.still,
                    "lpp_bytes": c.lpp_bytes,
                    "parked_age_s": c.parked_age_s,
                    "current_s": c.current_s,
                    "located_age_s": c.located_age_s,
                }
                for key, c in saved.contacts.items()
            },
            "floods_age_s": list(saved.floods_age_s),
            "own_debt_s": saved.own_debt_s,
            "mesh_debt_s": saved.mesh_debt_s,
            "hold_left_s": saved.hold_left_s,
            "hold_pending": saved.hold_pending,
        }
        try:
            self._store.put(self._KEY, entry)
        except OSError as exc:
            log.warning("telemetry: cannot store the schedule: %s", exc)
            return
        if at_once:
            self._store.flush()

    def forget(self, key: str) -> None:
        """The contact was excluded, included or added again while telemetry was off
        (design §6.11): as Scheduler.forget, its saved answers are gone and it is due at once;
        when it was asked and flooded stays. Never raises: a file that cannot be read is logged."""
        try:
            raw = self._store.load().get(self._KEY)
        except OSError as exc:
            log.warning("telemetry: cannot forget the answers of %s: %s", key[:12], exc)
            return
        entries = raw.get("contacts") if isinstance(raw, dict) else None
        entry = entries.get(key) if isinstance(entries, dict) else None
        if isinstance(raw, dict) and isinstance(entries, dict) and isinstance(entry, dict):
            forgotten = {
                **entry,
                "due_in_s": 0.0,
                "failures": 0,
                "answered_age_s": None,
                "located": False,
                "still": telemetry.STILL_POLLS,
                "current_s": telemetry.CURRENT_MIN_S,
                "located_age_s": None,
            }
            self._store.put(self._KEY, {**raw, "contacts": {**entries, key: forgotten}})


class AirtimeLevel:
    """The level of the message budget (ADR 0008), kept across restarts so that a restart adds
    no burst (ARC-005), and the airtime of a long direct message at the radio's settings, which
    a start gets at least, so that the first alert after it goes out at once: `airtime` =
    {"level_s", "longest_s", "saved_at"}. Written at once: a crash must not give the next start
    more than the last one left."""

    _KEY = "airtime"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store
        self.longest_s: float | None = None  # unknown until the radio is known

    def load(self, wall_now: float) -> tuple[float, float | None] | None:
        """The level and the seconds since it was saved (None if the clock went back); None
        if none was saved, or the file cannot be read (a full budget, as before). Sets
        `longest_s` from the file."""
        try:
            raw = self._store.load().get(self._KEY)
        except OSError as exc:
            log.warning("cannot read the airtime budget's level: %s", exc)
            return None
        if not isinstance(raw, dict):
            return None
        level, longest, saved_at = raw.get("level_s"), raw.get("longest_s"), raw.get("saved_at")
        if _age(longest):
            self.longest_s = float(longest)
        if not (_finite(level) and _finite(saved_at)):
            return None
        return float(level), wall_now - saved_at if wall_now >= saved_at else None

    def save(self, level_s: float, wall_now: float) -> None:
        """At once; a failed write is the state file's to log and to try again."""
        try:
            self._store.put(
                self._KEY, {"level_s": level_s, "longest_s": self.longest_s, "saved_at": wall_now}
            )
        except OSError as exc:
            log.warning("cannot store the airtime budget's level: %s", exc)
            return
        self._store.flush()


@dataclass(frozen=True, slots=True)
class SavedMessage:
    """A message the outgoing queue held when the service stopped (ARC-013)."""

    id: str
    target: str  # the alias, or the key of a contact
    text: str
    raw: str | None  # a channel text accepted before the radio's name was known
    age_s: float  # how long it had waited
    timestamp: int | None  # the radio timestamp of its attempts, once one was sent
    sent: int  # attempts sent
    queued_at: float | None = None  # wall clock at its acceptance; None: kept by 2.0
    nonce: bool | None = None  # the gateway added confirm_nonce's suffix; None: kept by 2.0


class OutboxRecords:
    """The messages a stop left in the outgoing queue, until the next start takes them:
    `outbox` = {"saved_at": time, "messages": [...]}. The texts are the household's, as in the
    history, and stay only until that start."""

    _KEY = "outbox"
    LIMIT = 1001  # the largest queue and the message in flight
    MAX_TEXT_BYTES = 1024  # a request's limit

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store

    def save(self, messages: list[SavedMessage], wall_now: float) -> None:
        """Hand them to the state file (the stop writes it); none: the section goes."""
        if not messages:
            self._store.drop(self._KEY)
            return
        self._store.put(
            self._KEY,
            {
                "saved_at": wall_now,
                "messages": [
                    {
                        "id": m.id,
                        "target": m.target,
                        "text": m.text,
                        "raw": m.raw,
                        "age_s": m.age_s,
                        "timestamp": m.timestamp,
                        "sent": m.sent,
                        "queued_at": m.queued_at,
                        "nonce": m.nonce,
                    }
                    for m in messages
                ],
            },
        )

    def take(self, wall_now: float) -> tuple[list[SavedMessage], float | None]:
        """The messages kept and the seconds since (None if the clock went back); the file
        then holds none, at once, so that a crash after the start does not bring them back a
        second time. Invalid entries are skipped with a warning; never raises."""
        try:
            raw = self._store.load().get(self._KEY)
        except OSError as exc:
            log.warning("cannot read the messages kept at the last stop: %s", exc)
            return [], None
        if raw is None:
            return [], None
        self._store.drop(self._KEY)
        self._store.flush()
        entries = raw.get("messages") if isinstance(raw, dict) else None
        saved_at = raw.get("saved_at") if isinstance(raw, dict) else None
        messages = [
            m
            for m in map(_saved_message, entries if isinstance(entries, list) else [])
            if m is not None
        ][: self.LIMIT]
        if not isinstance(entries, list) or len(messages) < len(entries):
            log.warning(
                "state file %s: invalid messages of the last stop skipped, %d kept",
                self._store.path,
                len(messages),
            )
        elapsed = wall_now - saved_at if _finite(saved_at) and wall_now >= saved_at else None
        return messages, elapsed


def _saved_message(entry: object) -> SavedMessage | None:
    if not isinstance(entry, dict):
        return None
    ident, target, text = entry.get("id"), entry.get("target"), entry.get("text")
    raw, age, timestamp, sent = (
        entry.get("raw"),
        entry.get("age_s"),
        entry.get("timestamp"),
        entry.get("sent", 0),
    )
    if (
        _short(ident)
        and _short(target)
        and _text(text)
        and (raw is None or _text(raw))
        and _age(age)
        and (timestamp is None or _small(timestamp, 0xFFFFFFFF))
        and _small(sent, 4)
    ):
        queued_at = entry.get("queued_at")  # since 2.1; only a time, never a reason to skip it
        when = float(queued_at) if _finite(queued_at) else None
        nonce = entry.get("nonce")  # since 2.1, as queued_at
        added = nonce if isinstance(nonce, bool) else None
        return SavedMessage(ident, target, text, raw, float(age), timestamp, sent, when, added)
    return None


def _short(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= 64


def _text(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value.encode()) <= OutboxRecords.MAX_TEXT_BYTES


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
        _age(due)
        and _small(failures, telemetry.MAX_FAILURES)
        and _small(floods, telemetry.MAX_FAILURES)
        and _age_or_none(answered)
        and _age_or_none(polled)
        and _age_or_none(flood)
        and _age_or_none(parked)
        and isinstance(located, bool)
        and _small(still, telemetry.STILL_POLLS)
        and _small(lpp_bytes, telemetry.MAX_LPP_BYTES)
        and lpp_bytes >= 1
        and _age(current)
        and _age_or_none(located_age)
    ):
        return SavedContact(
            min(due, telemetry.MAX_DUE_S),
            failures,
            answered,
            polled,
            flood,
            floods,
            located,
            still,
            lpp_bytes,
            parked,
            min(current, telemetry.MAX_DUE_S),
            located_age,
        )
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


def _time(value: object) -> float:
    """A stored time; one missing or invalid counts as the oldest."""
    return float(value) if _finite(value) else 0.0


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
        key: value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _times_map(raw: object) -> dict[str, tuple[int, ...]]:
    """Lists of whole seconds by key; what is not a finite number is left out."""
    if not isinstance(raw, dict):
        return {}
    times = {
        key: tuple(int(t) for t in value if _finite(t))
        for key, value in raw.items()
        if isinstance(value, list)
    }
    return {key: value for key, value in times.items() if value}


def _refusal_entry(refusal: Refusal) -> dict[str, Any]:
    return {
        "at": refusal.at,
        "reason": refusal.reason,
        "sender_prefix": refusal.sender_prefix,
        "sender_key": refusal.sender_key,
        "sender_name": refusal.sender_name,
    }


def _refusals(raw: object) -> tuple[Refusal, ...]:
    """The refused commands of the state file, the newest first; an entry of another shape is
    left out."""
    refusals = []
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, dict):
            continue
        at, reason, prefix = entry.get("at"), entry.get("reason"), entry.get("sender_prefix")
        key, name = entry.get("sender_key"), entry.get("sender_name")
        if (
            _finite(at)
            and isinstance(reason, str)
            and isinstance(prefix, str | None)
            and isinstance(key, str | None)
            and isinstance(name, str | None)
        ):
            refusals.append(Refusal(float(at), reason, prefix, key, name))
    return tuple(refusals[:REFUSALS_KEPT])


def _string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, str)}
