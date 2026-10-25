"""Compare two snapshots of the public interface (tests/public_interface.json).

docs/development.md#compatibility is the policy. tests/public_interface.py computes a snapshot
from the code; tools/release_plan.py compares the one of the last release with the one of HEAD.
Standard library only, and it never imports the package: release_plan runs under `python3 -I`
and loads this file by its path, as the tests do.

A snapshot is JSON. Below the paths in RECORDS each child is a *record*, whose keys are attribute
names compared by RULES, with values of the shape SHAPES give; an attribute without a rule, or a
value of another shape, is a FormatError, so tracking a new one is a deliberate change here. An
attribute absent from a record means what its rule says: no unit, any string, exactly one value,
and, for the attributes that mean nothing when absent, not recorded yet, which is growth.
Everywhere else a dict is a collection (a key that disappears is breaking), a list is a set of
values (a value that disappears is breaking), and a scalar stays equal. Sets are compared value
by value, so that each finding states one fact: a waiver names one finding as `str(finding)`
shows it. A finding is "breaking", "changed" (compatible, but users notice it: a default, a
wider range) or "added".
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
EACH = "each"  # a rule's answer for two sets of values: one finding per value
_WIDER_TYPES = {("integer", "number")}  # a TOML file with an integer still loads
_WIDER_ANSWERS = {("int", "float")}  # an answer of an older installation still reads
# how many values an option or a positional takes, by nargs (absent: exactly one)
_COUNTS: dict[str | None, tuple[float, float]] = {
    None: (1, 1), "?": (0, 1), "*": (0, math.inf), "+": (1, math.inf),
}  # fmt: skip


@dataclass(frozen=True, order=True)
class Finding:
    kind: str  # BREAKING, CHANGED or ADDED
    path: str  # "mqtt > publish > <base_topic>/<node_id>/rx"
    what: str  # "removed", 'no longer has "battery_v"', "10 -> 11"

    def __str__(self) -> str:
        """The finding as the refusal and `make interface` show it; a waiver names it so."""
        return f"{self.path}: {self.what}"


class FormatError(ValueError):
    """The snapshots cannot be compared: another format, or a record that RULES or SHAPES do
    not cover."""


Rule = Callable[[Any, Any, dict[str, Any]], str | None]  # (old, new, old record) -> kind or None


def _equal(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    # absent before: not recorded yet, which is growth. Only for the attributes whose absence
    # means nothing else: retained, qos, payload, takes_value, name, output, id, topic,
    # platform, entity_id
    return ADDED if old is None else BREAKING


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
    # a setting always has one; an option without one takes any string
    if new is None:
        return CHANGED
    if old is None:
        return BREAKING
    return CHANGED if (old, new) in _WIDER_TYPES else BREAKING


def _answer(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    if old is None:
        return ADDED  # the answers file grows
    return CHANGED if (old, new) in _WIDER_ANSWERS else BREAKING


def _nargs(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    before, after = _count(old), _count(new)
    if before is None or after is None:  # argparse.REMAINDER and the like
        return BREAKING
    return CHANGED if after[0] <= before[0] and before[1] <= after[1] else BREAKING


def _count(nargs: Any) -> tuple[float, float] | None:
    """The fewest and the most values that `nargs` takes, or None for another kind."""
    if isinstance(nargs, int):
        return (nargs, nargs)
    return _COUNTS.get(nargs)


def _unit(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    # also to or from none: Home Assistant's statistics keep the unit they began with
    return BREAKING


def _choices(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    if new is None:
        return CHANGED  # any value now
    if old is None:
        return BREAKING
    return EACH


def _default(old: Any, new: Any, record: dict[str, Any]) -> str | None:
    # brokers, ACLs and Home Assistant refer to the names that these defaults make up; a script
    # relies on the file that a path option means when it is not given
    return BREAKING if record.get("identity") or record.get("type") == "path" else CHANGED


def _values(old: Any, new: Any, _: dict[str, Any]) -> str | None:
    return EACH  # absent: none


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
    "choices": _choices, "answer": _answer, "takes_value": _equal, "nargs": _nargs,
    "name": _equal, "output": _equal, "identity": _ignored, "conditional": _ignored,
    # homeassistant
    "id": _equal, "topic": _equal, "platform": _equal, "entity_id": _equal, "unit": _unit,
    "event_types": _values, "options": _values, "attributes": _values, "node_types": _values,
}  # fmt: skip

Shape = tuple[str, Callable[[Any], bool]]  # what a value is, for the error; whether it is


def _listed(value: Any, item: Callable[[Any], bool]) -> bool:
    return isinstance(value, list) and all(item(v) for v in value)


_FLAG: Shape = ("true or false", lambda v: isinstance(v, bool))
_INTEGER: Shape = ("an integer", lambda v: isinstance(v, int) and not isinstance(v, bool))
_NUMBER: Shape = ("a number", lambda v: isinstance(v, float) or _INTEGER[1](v))
_TEXT: Shape = ("a string", lambda v: isinstance(v, str))
_TEXTS: Shape = ("a list of strings", lambda v: _listed(v, _TEXT[1]))
_TEXT_OR_INTEGER: Shape = ("a string or an integer", lambda v: _TEXT[1](v) or _INTEGER[1](v))

# the attributes of RULES, each with the shape of its values
SHAPES: dict[str, Shape] = {
    "retained": _FLAG, "qos": _INTEGER, "payload": _TEXT, "cleared": _FLAG,
    "type": _TEXT, "required": _FLAG, "min": _NUMBER, "max": _NUMBER, "answer": _TEXT,
    "default": ("a JSON scalar or list", lambda v: isinstance(v, str | int | float | list)),
    "choices": ("a list of strings and integers", lambda v: _listed(v, _TEXT_OR_INTEGER[1])),
    "takes_value": _FLAG, "nargs": _TEXT_OR_INTEGER, "name": _TEXT, "output": _TEXT,
    "identity": _FLAG, "conditional": _FLAG,
    "id": _TEXT, "topic": _TEXT, "platform": _TEXT, "entity_id": _TEXT, "unit": _TEXT,
    "event_types": _TEXTS, "options": _TEXTS, "node_types": _TEXTS,
    "attributes": ("a string or a list of strings", lambda v: _TEXT[1](v) or _TEXTS[1](v)),
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
    """The breaking findings that no waiver names (by `str(finding)`: one fact each)."""
    return [f for f in compare(old, new) if f.kind == BREAKING and str(f) not in waived]


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
        else:
            for attribute, item in sorted(value.items()):
                what, fits = SHAPES[attribute]
                if not fits(item):
                    msg = f"{_name((*here, attribute))}: expected {what}, got {_show(item)}"
                    raise FormatError(msg)


def _walk(old: Any, new: Any, path: tuple[str, ...], out: list[Finding]) -> None:
    if old is None and new is not None:
        out.append(Finding(ADDED, _name(path), "new"))
    elif new is None and old is not None:
        out.append(Finding(BREAKING, _name(path), "removed"))
    elif isinstance(old, dict) and isinstance(new, dict):
        _collection(old, new, path, out)
    elif isinstance(old, list) and isinstance(new, list):
        _each(old, new, path, out)
    elif old != new:
        out.append(Finding(BREAKING, _name(path), f"{_show(old)} -> {_show(new)}"))


def _each(old: list[Any], new: list[Any], path: tuple[str, ...], out: list[Finding]) -> None:
    """Two sets of values, one finding per value: a waiver of one removal covers no other."""
    name = _name(path)
    out.extend(Finding(BREAKING, name, f"no longer has {_show(v)}") for v in old if v not in new)
    out.extend(Finding(ADDED, name, f"also has {_show(v)}") for v in new if v not in old)


def _collection(
    old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...], out: list[Finding]
) -> None:
    records = _is_record_parent(path)
    for key in sorted(old.keys() | new.keys()):
        here = (*path, key)
        if key not in new:
            out.append(Finding(BREAKING, _name(here), "removed"))
        elif key not in old:
            if records and new[key].get("required") is True and not _new_table(key, old, new):
                out.append(Finding(BREAKING, _name(here), "new and required"))
            else:
                out.append(Finding(ADDED, _name(here), "new"))
        elif records:
            _record(old[key], new[key], here, out)
        else:
            _walk(old[key], new[key], here, out)


def _new_table(key: str, old: dict[str, Any], new: dict[str, Any]) -> bool:
    """A setting read only when present, in a table that is new too: no file has it yet. In a
    table that files have (`targets.<alias>`), or read for every file, every file needs it."""
    table = key.rpartition(".")[0]
    return bool(new[key].get("conditional")) and table in new and table not in old


def _record(
    old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...], out: list[Finding]
) -> None:
    for attribute in sorted(old.keys() | new.keys()):
        before, after = old.get(attribute), new.get(attribute)
        if before == after:
            continue
        here = (*path, attribute)
        kind = RULES[attribute](before, after, old)  # _check_records: every attribute has one
        if kind == EACH:
            _each(_as_list(before), _as_list(after), here, out)
        elif kind is not None:
            out.append(Finding(kind, _name(here), f"{_show(before)} -> {_show(after)}"))
