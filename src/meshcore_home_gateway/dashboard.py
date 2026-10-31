"""The Home Assistant dashboard of one gateway (ADR 0021; the part this version builds:
docs/design.md): the template dashboard.yaml of this package, filled in for the gateway's node,
this version and the contacts on the map, its texts in [homeassistant] language. Text in, text
out: no YAML parser at run time (ADR 0005).

The template is YAML as it stands, both branches of its conditional included:

* `@gw@` (the gateway's part of its entity IDs), `@node@`, `@version@` and `["@trackers@"]` (the
  map's trackers, a JSON list) are filled in.
* The lines between `# @if trackers` and `# @else` stay while the map lists a tracker, those
  between `# @else` and `# @end` while it lists none.
* `_("…")`, a JSON string inside, marks a text (msgctxt "dashboard"; tools/i18n.py extracts it).
  As a whole YAML value it becomes a quoted scalar and has no field; inside a card's content
  (a literal block) it becomes the text, {node} and {version} filled in, and any other field the
  Jinja variable of that name, which the card sets before it.
* A translation is used only when it holds nothing of FORBIDDEN and no character of UNSAFE, the
  rule the catalog check of tools/i18n.py applies too; else the English text.

tests/test_dashboard.py checks the rest of the template's rules on the shipped one: a value read
from Home Assistant reaches a card only through the card's macro m(), on a line that is one HTML
element.
"""

from __future__ import annotations

import json
import re
import string
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from typing import TYPE_CHECKING, Final

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile

if TYPE_CHECKING:
    from pathlib import Path

    from meshcore_home_gateway.config import Config

TEMPLATE: Final = "dashboard.yaml"
MARKER: Final = re.compile(r'_\("((?:[^"\\]|\\.)*)"\)')
VALUE: Final = re.compile(r'^(\s*(?:- )?[A-Za-z_][\w-]*: )_\("((?:[^"\\]|\\.)*)"\)\s*$')
# what a card would read as code: Jinja, HTML, Markdown links and images, the autolinks of web
# and mail addresses (the alerts are Markdown), a marker
FORBIDDEN: Final = ("{{", "{%", "{#", "<", ">", "@", "](", "://", "www.", '_("')
# control and format characters, line and paragraph separators: CR and LF end a card's HTML
# block, U+2028 and U+2029 the YAML block that holds the card
UNSAFE: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
FIXED: Final = ("node", "version")  # filled in here; any other field is a Jinja variable
_PLACEHOLDER: Final = re.compile(r"@\w+@")
_SLUG: Final = re.compile(r"[a-z0-9_]+")  # as the gateway gives them: a part of an entity ID
_NAME: Final = re.compile(r"[A-Za-z_]\w*")  # a field's name: a Jinja variable
# stands for a Jinja variable while a text is checked: private use characters, in no text
_STAND_IN: Final = chr(0xE000) + "{}" + chr(0xE001)


class TemplateError(ValueError):
    """The template breaks one of its rules (tests/test_dashboard.py finds it before a release)."""


def unsafe(text: str) -> str | None:
    """The first thing of FORBIDDEN or UNSAFE the text holds, as a finding names it; else None."""
    for item in FORBIDDEN:
        if item in text:
            return repr(item)
    for char in text:
        if unicodedata.category(char) in UNSAFE:
            return f"U+{ord(char):04X}"
    return None


def trackers(records: ContactDeviceRecords) -> list[str]:
    """The trackers of the contacts announced with a position, sorted. A contact deleted in Home
    Assistant is hidden, not announced; a slug that is no part of an entity ID never came from
    the gateway."""
    return sorted(
        f"device_tracker.mesh_{record.slug}"
        for record in records.announced.values()
        if record.components.get("position") == "device_tracker" and _SLUG.fullmatch(record.slug)
    )


def render(
    template: str,
    *,
    node_id: str,
    version: str,
    trackers: Sequence[str],
    text: Callable[[str, Mapping[str, str]], str],
) -> str:
    """The template filled in; `text(msgid, values)` gives a text in the dashboard's language,
    filled with the values."""
    lines = _kept(template.split("\n"), {"trackers": bool(trackers)})
    filled = (
        "\n".join(lines)
        .replace('["@trackers@"]', json.dumps(sorted(trackers)))
        .replace("@gw@", "meshcore_home_gateway_" + node_id.replace("-", "_"))
        .replace("@node@", node_id)
        .replace("@version@", version)
    )
    for line in filled.split("\n"):  # before any text goes in: no translation can trip it
        if '_("' in MARKER.sub("", line) or _PLACEHOLDER.search(line):
            raise TemplateError(f"left over: {line.strip()}")
    fixed = {"node": node_id, "version": version}
    out = []
    for line in filled.split("\n"):
        if match := VALUE.match(line):
            msgid = _msgid(match.group(2))
            if _fields(msgid):
                raise TemplateError(f"a whole value takes no field: {msgid!r}")
            out.append(match.group(1) + json.dumps(_text(msgid, fixed, text), ensure_ascii=False))
        else:
            out.append(MARKER.sub(lambda m: _text(_msgid(m.group(1)), fixed, text), line))
    return "\n".join(out)


def dashboard(config: Config, state_dir: Path) -> tuple[str, int]:
    """The YAML for this gateway, and how many contacts its map lists, from the service's state
    in state_dir, read only: without it, none."""
    records = ContactDeviceRecords(JsonStateFile(state_dir / "state.json", read_only=True))
    listed = trackers(records)
    translator = i18n.translator(config.homeassistant.language)

    def text(msgid: str, values: Mapping[str, str]) -> str:
        return translator.pgettext(i18n.DASHBOARD, msgid, **values)

    template = (resources.files(__package__ or __name__) / TEMPLATE).read_text(encoding="utf-8")
    yaml = render(
        template, node_id=config.mqtt.node_id, version=__version__, trackers=listed, text=text
    )
    return yaml, len(listed)


def _kept(lines: Sequence[str], flags: Mapping[str, bool]) -> list[str]:
    """The lines the conditional keeps, without its own and without the translators' notes,
    which are YAML comments for the catalog, not for the household's file."""
    kept: list[str] = []
    levels: list[tuple[bool, bool]] = []  # each open @if: its flag, and whether past its @else
    note = False  # in the comment lines that a `# Translators:` line begins
    for line in lines:
        directive = line.strip()
        note = directive.startswith("# Translators:") or (
            note and directive.startswith("#") and not directive.startswith("# @")
        )
        if note:
            continue
        if not directive.startswith("# @"):
            if all(flag != past_else for flag, past_else in levels):
                kept.append(line)
            continue
        word, _, flag = directive.removeprefix("# @").partition(" ")
        if word == "if":
            if flag not in flags:
                raise TemplateError(f"unknown flag in {directive!r}")
            levels.append((flags[flag], False))
        elif word not in {"else", "end"} or flag:
            raise TemplateError(f"unknown line {directive!r}")
        elif not levels:
            raise TemplateError(f"{directive!r} with no @if")
        elif word == "end":
            levels.pop()
        elif levels[-1][1]:
            raise TemplateError("a second @else")
        else:
            levels[-1] = (levels[-1][0], True)
    if levels:
        raise TemplateError("an @if not closed")
    return kept


def _msgid(body: str) -> str:
    try:
        return str(json.loads(f'"{body}"'))
    except ValueError as exc:
        raise TemplateError(f'_("{body}") is no JSON string: {exc}') from exc


def _fields(msgid: str) -> list[str]:
    try:
        names = [name for _, name, _, _ in string.Formatter().parse(msgid) if name is not None]
    except ValueError as exc:
        raise TemplateError(f"{msgid!r} is no format string: {exc}") from exc
    for name in names:
        if not _NAME.fullmatch(name):
            raise TemplateError(f"{msgid!r}: a field is a plain name, such as {{n}}")
    return names


def _text(
    msgid: str, fixed: Mapping[str, str], text: Callable[[str, Mapping[str, str]], str]
) -> str:
    """The text in the dashboard's language, or English if a card would read it as code."""
    names = _fields(msgid)
    values = {name: fixed.get(name, _STAND_IN.format(name)) for name in names}
    chosen = text(msgid, values)
    if _refused(chosen):
        chosen = i18n.ENGLISH.pgettext(i18n.DASHBOARD, msgid, **values)
        if _refused(chosen):
            raise TemplateError(f"{msgid!r}: its English text breaks the rule of the translations")
    for name in names:
        if name not in fixed:
            chosen = chosen.replace(_STAND_IN.format(name), "{{ " + name + " }}")
    return chosen


def _refused(text: str) -> bool:
    """unsafe(); or a '{', which is what a '{{' of the translation leaves once filled in."""
    return unsafe(text) is not None or "{" in text
