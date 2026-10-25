"""Compare two snapshots of the public interface (tests/public_interface.json).

docs/development.md#compatibility is the policy. tests/public_interface.py computes a snapshot
from the code; tools/release_plan.py compares the one of the last release with the one of HEAD.
Standard library only, and it never imports the package: release_plan runs under `python3 -I`
and loads this file by its path, as the tests do.

A snapshot is JSON. Below the paths in RECORDS each child is a *record*, whose keys are attribute
names compared by RULES; an attribute without a rule is a FormatError, so tracking a new one is a
deliberate change here. Everywhere else a dict is a collection (a key that disappears is
breaking), a list is a set of values (a value that disappears is breaking), and a scalar stays
equal. A finding is "breaking", "changed" (compatible, but users notice it: a default, a wider
range) or "added".
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

FORMAT = 1  # a new format comes with a converter of the older ones, so releases stay comparable

# the children of these paths are records ("*" matches any one key)
RECORDS: tuple[tuple[str, ...], ...] = (
    ("mqtt", "publish"),
    ("config",),
    ("cli", "commands", "*", "options"),
    ("cli", "commands", "*", "positionals"),
    ("homeassistant", "devices"),
    ("homeassistant", "components", "*"),
)

BREAKING, CHANGED, ADDED = "breaking", "changed", "added"
_WIDER_TYPES = {("integer", "number")}  # a TOML file with an integer still loads


@dataclass(frozen=True, order=True)
class Finding:
    kind: str  # BREAKING, CHANGED or ADDED
    path: str  # "mqtt > publish > <base_topic>/<node_id>/rx"; waivers name findings by it
    what: str

    def __str__(self) -> str:
        return f"{self.path}: {self.what}"


class FormatError(ValueError):
    """The snapshots cannot be compared: another format, or a record that RULES do not cover."""


Rule = Callable[[Any, Any, dict[str, Any]], str | None]  # (old, new, old record) -> kind or None


def _equal(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    return ADDED if old is None else BREAKING  # None: tracked since, which is growth


def _kept_flag(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    return BREAKING if old and not new else ADDED


def _required(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    return BREAKING if new and not old else CHANGED  # relaxed: what was accepted still is


def _minimum(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    before, after = (-math.inf if v is None else v for v in (old, new))
    return BREAKING if after > before else CHANGED


def _maximum(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    before, after = (math.inf if v is None else v for v in (old, new))
    return BREAKING if after < before else CHANGED


def _type(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    if old is None:
        return ADDED
    return CHANGED if (old, new) in _WIDER_TYPES else BREAKING


def _choices(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    if new is None:
        return CHANGED  # any value now
    if old is None:
        return BREAKING
    return _values(old, new, {})


def _default(old: Any, new: Any, record: dict[str, Any]) -> str | None:
    # brokers, ACLs and Home Assistant refer to the names that these defaults make up
    return BREAKING if record.get("identity") else CHANGED


def _values(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    before, after = set(_as_list(old)), set(_as_list(new))
    if before - after:
        return BREAKING
    return ADDED if after - before else None  # None: in another order


def _ignored(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


RULES: dict[str, Rule] = {
    # mqtt.publish
    "retained": _equal, "qos": _equal, "payload": _equal, "cleared": _kept_flag,
    # config and cli
    "type": _type, "default": _default, "required": _required, "min": _minimum, "max": _maximum,
    "choices": _choices, "answer": _equal, "takes_value": _equal, "nargs": _equal,
    "output": _equal, "identity": _ignored, "conditional": _ignored,
    # homeassistant
    "id": _equal, "topic": _equal, "platform": _equal, "entity_id": _equal, "unit": _equal,
    "event_types": _values, "options": _values, "attributes": _values, "node_types": _values,
}  # fmt: skip


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[Finding]:
    """What `new` removes, narrows, changes or adds against `old`, sorted."""
    for doc in (old, new):
        if doc.get("format") != FORMAT:
            msg = f"snapshot format {doc.get('format')!r}, this tool reads {FORMAT}"
            raise FormatError(msg)
        _check_records(doc, ())
    found: list[Finding] = []
    for key in sorted((old.keys() | new.keys()) - {"format"}):
        _walk(old.get(key), new.get(key), (key,), found)
    return sorted(found)


def breaking(old: dict[str, Any], new: dict[str, Any], waived: dict[str, str]) -> list[Finding]:
    """The breaking findings whose path no waiver names."""
    return [f for f in compare(old, new) if f.kind == BREAKING and f.path not in waived]


def render(doc: dict[str, Any]) -> str:
    """The canonical text of a snapshot: what `make interface` writes and the test compares."""
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _is_record_parent(path: tuple[str, ...]) -> bool:
    return any(
        len(path) == len(p) and all(a in ("*", b) for a, b in zip(p, path, strict=True))
        for p in RECORDS
    )


def _name(path: tuple[str, ...]) -> str:
    return " > ".join(path)


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _check_records(node: Any, path: tuple[str, ...]) -> None:
    if not isinstance(node, dict):
        return
    parent = _is_record_parent(path)
    for key, value in node.items():
        here = (*path, key)
        if not parent:
            _check_records(value, here)
        elif not isinstance(value, dict):
            msg = f"{_name(here)}: a record must be an object, got {_show(value)}"
            raise FormatError(msg)
        elif unknown := sorted(value.keys() - RULES.keys()):
            msg = f"{_name(here)}: no rule for the attribute {unknown[0]!r}"
            raise FormatError(msg)


def _walk(old: Any, new: Any, path: tuple[str, ...], out: list[Finding]) -> None:
    if old is None and new is not None:
        out.append(Finding(ADDED, _name(path), "new"))
    elif new is None and old is not None:
        out.append(Finding(BREAKING, _name(path), "removed"))
    elif isinstance(old, dict) and isinstance(new, dict):
        _collection(old, new, path, out)
    elif isinstance(old, list) and isinstance(new, list):
        if gone := [v for v in old if v not in new]:
            out.append(Finding(BREAKING, _name(path), f"no longer has {_show(gone)}"))
        if more := [v for v in new if v not in old]:
            out.append(Finding(ADDED, _name(path), f"also has {_show(more)}"))
    elif old != new:
        out.append(Finding(BREAKING, _name(path), f"{_show(old)} -> {_show(new)}"))


def _collection(
    old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...], out: list[Finding]
) -> None:
    records = _is_record_parent(path)
    for key in sorted(old.keys() | new.keys()):
        here = (*path, key)
        if key not in new:
            out.append(Finding(BREAKING, _name(here), "removed"))
        elif key not in old:
            if records and new[key].get("required") is True:
                out.append(Finding(BREAKING, _name(here), "new and required"))
            else:
                out.append(Finding(ADDED, _name(here), "new"))
        elif records:
            _record(old[key], new[key], here, out)
        else:
            _walk(old[key], new[key], here, out)


def _record(
    old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...], out: list[Finding]
) -> None:
    for attribute in sorted(old.keys() | new.keys()):
        before, after = old.get(attribute), new.get(attribute)
        if before == after:
            continue
        kind = RULES[attribute](before, after, old)  # _check_records: every attribute has one
        if kind is not None:
            here = _name((*path, attribute))
            out.append(Finding(kind, here, f"{_show(before)} -> {_show(after)}"))
