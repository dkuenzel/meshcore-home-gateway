"""Small persistent state in one JSON file, written atomically.

It holds the newest accepted command timestamp per sender (replay protection, ADR 0009) and the
Home Assistant components of the last discovery document (docs/home-assistant.md).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

log = logging.getLogger(__name__)


class JsonStateFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict):
            aside = self.path.with_name(self.path.name + ".corrupt")
            self.path.replace(aside)
            log.warning("state file %s unreadable, moved to %s, starting empty", self.path, aside)
            return {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        """Write to a temporary file in the same directory, fsync, then rename over the old one."""
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

    _KEY = "ha_components"
    _REMOVED = "ha_removed"  # key -> {"p": platform, "since": unix time}

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store

    def replace(self, current: Mapping[str, str], now: float) -> dict[str, str]:
        """Store the current components; return the pending removals (key -> platform)."""
        data = self._store.load()
        previous = _string_map(data.get(self._KEY))
        removed: dict[str, dict[str, Any]] = {}
        raw = data.get(self._REMOVED)
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
        if previous != current or data.get(self._REMOVED, {}) != removed:
            data[self._KEY] = dict(current)
            data[self._REMOVED] = removed
            try:
                self._store.save(data)
            except OSError as exc:  # pending removals come back from `previous` next time
                log.warning("cannot store the Home Assistant components: %s", exc)
        return {key: entry["p"] for key, entry in removed.items()}


REMOVAL_KEEP_S = 30 * 86_400.0


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


def _string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, str)}
