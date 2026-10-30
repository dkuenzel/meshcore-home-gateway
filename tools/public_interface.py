"""Compare two snapshots of the public interface (tests/public_interface.json).

docs/development.md#compatibility is the policy. tests/public_interface.py computes a snapshot
from the code; tools/release_plan.py compares the one of the last release with the one of HEAD.
Standard library only, and it never imports the package: release_plan runs under `python3 -I`
and loads this file by its path, as the tests do.

A snapshot is JSON. Below the paths in RECORDS each child is a *record*, whose keys are attribute
names that ATTRIBUTES gives a rule and a shape of values; an attribute not there, or a value of
another shape, is a FormatError, so tracking a new one is a deliberate change here. An attribute
absent from a record means what its rule says: no unit, no class, any string, exactly one value,
and, for the attributes that mean nothing when absent, not recorded yet, which is growth.
Everywhere else a dict is a collection (a key that disappears is breaking), a list is a set of
values (a value that disappears is breaking), and a scalar stays equal; only the option groups
of a command have a comparison of their own (`_groups`), and a new option that has the name of
an old one is an alias of it, also when it is required (`_alias`). Sets are compared value by
value, so that each finding states one fact: a waiver names one finding as `str(finding)` shows
it. A finding is "breaking", "changed" (compatible, but users notice it: a default, a wider
range) or "added".
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

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
COMMANDS = ("cli", "commands", "*")  # their `groups`: argparse's mutually exclusive groups

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
        """The finding as the refusal shows it; a waiver names it so."""
        return f"{self.path}: {self.what}"


class FormatError(ValueError):
    """The snapshots cannot be compared: another format, or a record that ATTRIBUTES does not
    cover."""


# (old, new, old record, new record) -> kind or None
Rule = Callable[[Any, Any, dict[str, Any], dict[str, Any]], str | None]


def _equal(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    # absent before: not recorded yet, which is growth. Only for the attributes whose absence
    # means nothing else: retained, qos, payload, takes_value, name, output, id, topic,
    # platform, entity_id
    return ADDED if old is None else BREAKING


def _kept_flag(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    return BREAKING if old and not new else ADDED


def _required(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    return BREAKING if new and not old else CHANGED  # relaxed: what was accepted still is


def _minimum(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    before, after = (-math.inf if v is None else v for v in (old, new))
    return BREAKING if after > before else CHANGED


def _maximum(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    before, after = (math.inf if v is None else v for v in (old, new))
    return BREAKING if after < before else CHANGED


def _type(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    # a setting always has one; an option without one takes any string
    if new is None:
        return CHANGED
    if old is None:
        return BREAKING
    return CHANGED if (old, new) in _WIDER_TYPES else BREAKING


def _answer(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    if old is None:
        return ADDED  # the answers file grows
    return CHANGED if (old, new) in _WIDER_ANSWERS else BREAKING


def _nargs(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    before, after = _count(old), _count(new)
    if before is None or after is None:  # argparse.REMAINDER and the like
        return BREAKING
    return CHANGED if after[0] <= before[0] and before[1] <= after[1] else BREAKING


def _count(nargs: Any) -> tuple[float, float] | None:
    """The fewest and the most values that `nargs` takes, or None for another kind."""
    if isinstance(nargs, int):
        return (nargs, nargs)
    return _COUNTS.get(nargs)


def _unit(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    # also to or from none: Home Assistant's statistics keep the unit they began with
    return BREAKING


def _class(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    # a device or state class added where there was none, also one recorded for the first time,
    # is growth; removed or changed, Home Assistant's statistics and unit conversions end
    return ADDED if old is None else BREAKING


def _choices(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    if new is None:
        return CHANGED  # any value now
    if old is None:
        return BREAKING
    return EACH


def _default(old: Any, new: Any, record: dict[str, Any], _: dict[str, Any]) -> str | None:
    # brokers, ACLs and Home Assistant refer to the names that these defaults make up; a script
    # relies on the file that a path option means when it is not given
    return BREAKING if record.get("identity") or record.get("type") == "path" else CHANGED


def _conditional(old: Any, new: Any, _: dict[str, Any], record: dict[str, Any]) -> str | None:
    # read for every file from now on: if it is required, every file needs it; else its default
    # now applies to every file. Read only when present from now on: what loaded still loads
    return BREAKING if old and not new and record.get("required") else CHANGED


def _values(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    return EACH  # absent: none


def _ignored(old: Any, new: Any, *_: dict[str, Any]) -> str | None:
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Shape(NamedTuple):
    what: str  # what a value is, for the error
    fits: Callable[[Any], bool]


def _listed(value: Any, item: Callable[[Any], bool]) -> bool:
    return isinstance(value, list) and all(item(v) for v in value)


_FLAG = Shape("true or false", lambda v: isinstance(v, bool))
_INTEGER = Shape("an integer", lambda v: isinstance(v, int) and not isinstance(v, bool))
_NUMBER = Shape("a number", lambda v: isinstance(v, float) or _INTEGER.fits(v))
_TEXT = Shape("a string", lambda v: isinstance(v, str))
_TEXTS = Shape("a list of strings", lambda v: _listed(v, _TEXT.fits))
_TEXT_OR_INTEGER = Shape("a string or an integer", lambda v: _TEXT.fits(v) or _INTEGER.fits(v))
_DEFAULT = Shape("a JSON scalar or list", lambda v: isinstance(v, str | int | float | list))
_CHOICES = Shape("a list of strings and integers", lambda v: _listed(v, _TEXT_OR_INTEGER.fits))
_ATTRIBUTES = Shape("a string or a list of strings", lambda v: _TEXT.fits(v) or _TEXTS.fits(v))


class Attribute(NamedTuple):
    rule: Rule  # how a difference counts
    shape: Shape  # what its values are


# every attribute a record may have
ATTRIBUTES: dict[str, Attribute] = {
    # mqtt.publish
    "retained": Attribute(_equal, _FLAG), "qos": Attribute(_equal, _INTEGER),
    "payload": Attribute(_equal, _TEXT), "cleared": Attribute(_kept_flag, _FLAG),
    # config and cli
    "type": Attribute(_type, _TEXT), "default": Attribute(_default, _DEFAULT),
    "required": Attribute(_required, _FLAG), "min": Attribute(_minimum, _NUMBER),
    "max": Attribute(_maximum, _NUMBER), "choices": Attribute(_choices, _CHOICES),
    "answer": Attribute(_answer, _TEXT), "takes_value": Attribute(_equal, _FLAG),
    "nargs": Attribute(_nargs, _TEXT_OR_INTEGER), "name": Attribute(_equal, _TEXT),
    "output": Attribute(_equal, _TEXT), "identity": Attribute(_ignored, _FLAG),
    "conditional": Attribute(_conditional, _FLAG), "names": Attribute(_values, _TEXTS),
    # homeassistant
    "id": Attribute(_equal, _TEXT), "topic": Attribute(_equal, _TEXT),
    "platform": Attribute(_equal, _TEXT), "entity_id": Attribute(_equal, _TEXT),
    "unit": Attribute(_unit, _TEXT), "device_class": Attribute(_class, _TEXT),
    "state_class": Attribute(_class, _TEXT), "event_types": Attribute(_values, _TEXTS),
    "options": Attribute(_values, _TEXTS), "attributes": Attribute(_values, _ATTRIBUTES),
    "node_types": Attribute(_values, _TEXTS),
}  # fmt: skip


def check(doc: dict[str, Any]) -> None:
    """Refuse a snapshot that this tool cannot compare (FormatError)."""
    if doc.get("format") != FORMAT:
        msg = f"snapshot format {doc.get('format')!r}, this tool reads {FORMAT}"
        raise FormatError(msg)
    _check_records(doc, ())


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[Finding]:
    """What `new` removes, narrows, changes or adds against `old`, sorted."""
    check(old)
    check(new)
    found: list[Finding] = []
    for key in sorted((old.keys() | new.keys()) - {"format"}):
        _walk(old.get(key), new.get(key), (key,), found)
    return sorted(found)


def unwaived(old: dict[str, Any], new: dict[str, Any], waived: dict[str, str]) -> list[Finding]:
    """The findings that no waiver names (by `str(finding)`: one fact each)."""
    return [f for f in compare(old, new) if str(f) not in waived]


def render(doc: dict[str, Any]) -> str:
    """The canonical text of a snapshot: what `make interface` writes and the test compares."""
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    return len(path) == len(pattern) and all(
        a in ("*", b) for a, b in zip(pattern, path, strict=True)
    )


def _is_record_parent(path: tuple[str, ...]) -> bool:
    return any(_matches(path, p) for p in RECORDS)


def _name(path: tuple[str, ...]) -> str:
    return " > ".join(path)


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _check_records(node: Any, path: tuple[str, ...]) -> None:
    if not isinstance(node, dict):
        return
    if _matches(path, COMMANDS):
        _check_groups(node.get("groups", []), (*path, "groups"))
    parent = _is_record_parent(path)
    for key, value in node.items():
        here = (*path, key)
        if not parent:
            _check_records(value, here)
        elif not isinstance(value, dict):
            msg = f"{_name(here)}: a record must be an object, got {_show(value)}"
            raise FormatError(msg)
        elif unknown := sorted(value.keys() - ATTRIBUTES.keys()):
            msg = f"{_name(here)}: no rule for the attribute {unknown[0]!r}"
            raise FormatError(msg)
        else:
            for attribute, item in sorted(value.items()):
                shape = ATTRIBUTES[attribute].shape
                if not shape.fits(item):
                    msg = f"{_name((*here, attribute))}: expected {shape.what}, got {_show(item)}"
                    raise FormatError(msg)


def _check_groups(groups: Any, path: tuple[str, ...]) -> None:
    def fits(group: Any) -> bool:
        return (
            isinstance(group, dict)
            and group.keys() == {"options", "required"}
            and bool(group["options"])
            and _listed(group["options"], lambda names: bool(names) and _TEXTS.fits(names))
            and _FLAG.fits(group["required"])
        )

    if not isinstance(groups, list) or not all(fits(group) for group in groups):
        msg = (
            f'{_name(path)}: expected a list of {{"options": [[the names of a member]],'
            f' "required": true or false}}, got {_show(groups)}'
        )
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
        if key == "groups" and _matches(path, COMMANDS):
            _groups(old, new, here, out)
        elif key not in new:
            out.append(Finding(BREAKING, _name(here), "removed"))
        elif key not in old:
            required = records and new[key].get("required") is True
            if required and not _new_table(key, old, new) and not _alias(old, new[key]):
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


def _alias(old: dict[str, Any], record: dict[str, Any]) -> bool:
    """A new option that has the name of one that existed: the same option, by another name, so
    every call that gave it still does."""
    return any(name in old for name in record.get("names", []))


def _record(
    old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...], out: list[Finding]
) -> None:
    for attribute in sorted(old.keys() | new.keys()):
        before, after = old.get(attribute), new.get(attribute)
        if before == after:
            continue
        here = (*path, attribute)
        kind = ATTRIBUTES[attribute].rule(before, after, old, new)  # _check_records: each has one
        if kind == EACH:
            _each(_as_list(before), _as_list(after), here, out)
        elif kind is not None:
            out.append(Finding(kind, _name(here), f"{_show(before)} -> {_show(after)}"))


Member = frozenset[str]  # an option by all its names (its aliases), or a positional by its name


class Group(NamedTuple):
    """A mutually exclusive group of a command."""

    members: frozenset[Member]
    required: bool

    @property
    def names(self) -> frozenset[str]:
        return frozenset().union(*self.members)


def _groups(
    old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...], out: list[Finding]
) -> None:
    """The mutually exclusive groups of a command, old and new: a call that worked must still
    work. A new exclusion between two options that both existed breaks one; so does a required
    group that holds no member of an old required group (an option required on its own counts
    as a group of one), or that such a member left while it still exists. The names of one
    member are one option: an alias excludes none of them."""
    before, after = _read_groups(old), _read_groups(new)
    existed, exists = _members(old), _members(new)
    excluded_before, excluded = _exclusions(before), _exclusions(after)
    for pair in sorted(excluded.keys() - excluded_before.keys(), key=sorted):
        kind = BREAKING if pair <= existed else ADDED
        out.append(Finding(kind, _group(path, excluded[pair]), f"{_pair(pair)} exclude each other"))
    for pair in sorted(excluded_before.keys() - excluded.keys(), key=sorted):
        if pair <= exists:  # else the option itself is gone, which is a finding of its own
            what = f"{_pair(pair)} no longer exclude each other"
            out.append(Finding(CHANGED, _group(path, excluded_before[pair]), what))
    required = {group.names: group for group in before if group.required}
    alone = [
        frozenset([name]) for name, entry in old.get("options", {}).items() if entry.get("required")
    ]
    required_now = {group.names: group for group in after if group.required}
    for names in sorted(required_now.keys() - required.keys(), key=sorted):
        here = _group(path, required_now[names].members)
        bases = [kept for base in (*required, *alone) if (kept := base & exists) & names]
        if not bases:
            loose = any(group.names == names for group in before)
            out.append(Finding(BREAKING, here, "now required" if loose else "new and required"))
            continue
        base = frozenset().union(*bases)
        out.extend(  # a call with it alone no longer satisfies the group
            Finding(BREAKING, here, f"no longer satisfied by {_show(name)}")
            for name in sorted(base - names)
        )
        for name in sorted(names - base):
            kind = CHANGED if name in existed else ADDED
            out.append(Finding(kind, here, f"also satisfied by {_show(name)}"))
    for names, group in required.items():
        kept = names & exists
        if kept and not any(kept & now for now in required_now):
            out.append(Finding(CHANGED, _group(path, group.members), "no longer required"))


def _read_groups(command: dict[str, Any]) -> list[Group]:
    return [
        Group(frozenset(frozenset(names) for names in group["options"]), group["required"])
        for group in command.get("groups", [])
    ]


def _members(command: dict[str, Any]) -> frozenset[str]:
    """What a group of the command can hold: its options and its positionals, by name."""
    positionals = command.get("positionals", {}).values()
    return frozenset(command.get("options", {})) | {p["name"] for p in positionals if "name" in p}


def _exclusions(groups: list[Group]) -> dict[frozenset[str], frozenset[Member]]:
    """Each pair of names of two members that exclude each other, with the group that makes it
    so."""
    return {
        frozenset([a, b]): group.members
        for group in groups
        for one in group.members
        for other in group.members - {one}
        for a in one
        for b in other
        if a < b
    }


def _group(path: tuple[str, ...], members: frozenset[Member]) -> str:
    """A group as a finding names it: its members, each by its names joined by "/"."""
    return _name((*path, " | ".join(sorted("/".join(sorted(names)) for names in members))))


def _pair(pair: frozenset[str]) -> str:
    first, second = sorted(pair)
    return f"{_show(first)} and {_show(second)}"
