"""Whether the frontend shows a card: its visibility conditions decided as the frontend does.

Since Home Assistant 2026.10 the frontend evaluates only five kinds of condition in the browser
(`screen`, `user`, `view_columns`, `location`, `time`) and a Lovelace `state` or `numeric_state`
that core would read differently (with an `attribute`, an entity ID among the values or an entity
ID as a bound). It translates every other condition to core's form and sends it to core's
`subscribe_condition` websocket command; an error from core hides the card, also under a `not`.
So a `state` condition on an entity that does not exist hides its card under `state` and under
`state_not` alike (docs/platform-notes.md; FF 0.1 of the slice 9 facts).

`Viewer.visible` does the same: the browser's kinds in Python, the rest sent as a household user
(group `system-users`, no administrator) and the first result taken. It reads the frontend's
version from the pinned Home Assistant (its frontend manifest) and refuses one whose sources it
was not checked against, because these semantics moved in a minor release before.

Sources, the same file and lines in both frontends unless two are given (20260930.4 for Home
Assistant 2026.10.2, 20261028.4 for 2026.11.0; https://github.com/home-assistant/frontend at
those tags):

* which conditions stay in the browser: `src/common/condition/translate.ts:21-28` (the five
  kinds), `:52-67` (legacy semantics), `:74-95` (`isServerCondition`);
* the translation to core's form: `translate.ts:117-139`, `:141-146` (always false), `:148-190`
  (`state`, `state_not` as `not`), `:192-279` (`numeric_state`), `:281-304` (`and`, `or`,
  `not` as NOT(AND));
* the split into subscriptions, sibling server conditions grouped, `enabled: false` skipped:
  `src/common/condition/split.ts:42-84, 93-170`;
* one `subscribe_condition` per subtree; an error, or a failed subscription, hides the card:
  `src/common/controllers/condition-evaluator-controller.ts:238-274, 286-314`; the message
  `{type: "subscribe_condition", condition}` (`src/data/automation.ts`, `subscribeCondition`);
* the card's own entity for a condition without one, conditions that are no mapping dropped
  (20261028.4 only; an array is an object to JavaScript and stays), no conditions shown:
  `src/mixins/conditional-listener-mixin.ts:124-142, 148-184` / `:125-143, 149-186`,
  `src/panels/lovelace/common/validate-condition.ts:520-546` / `:733-759`,
  `src/common/condition/sanitize.ts:4-34` (20261028.4),
  `src/panels/lovelace/cards/hui-card.ts:173-176` / `:193-196` (`entity`);
* a card or section with `disabled` hidden before its conditions: `hui-card.ts:259-262` /
  `:268-271`, `src/panels/lovelace/sections/hui-section.ts:280-283` / `:367-370`;
* the browser's checks: `validate-condition.ts:151-208` / `:177-234` (`state`), `:210-253` /
  `:236-279` (`numeric_state`), `:255-264` / `:281-290` (`view_columns`), `:266-270` /
  `:292-296` (`screen`), `:272-277` / `:298-303` and `src/common/datetime/check_time.ts:60-131`
  (`time`), `:279-288` / `:305-345` (`location`; 20261028.4 adds `away` and zone targets),
  `:290-294` / `:469-473` (`user`), `:296-364` / `:475-543` (`and`, `or`, `not`);
* a conditional card or row evaluates its `conditions` the same way:
  `src/panels/lovelace/components/hui-conditional-base.ts:68-74, 90-110`;
* what is inside a hidden card, stack, section or conditional row is not rendered, and a section
  hides when all its content hides: its cards (`hui-section.ts:292-296`), and since 20261028.4
  also its badges and sections (`:379-384`); a Markdown card with `show_empty: false` hides
  itself while its template renders nothing (`hui-markdown-card.ts:184-191` / `:232-239`), and
  `hui-card.ts:249-251` passes that on. `Viewer.shown` walks that path (D-53 of the slice 9
  design).

Core's side: `homeassistant/components/websocket_api/commands.py:1118-1196` (2026.10.2) and
`:1225-1303` (2026.11.0), the same handler: no administrator needed, a result, then one event
`{"result": bool}` or `{"error": str}` at once, again when it changes.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import homeassistant.components
from ha_plugin import CLIENT_ID
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

# the frontends whose sources this module follows; another one needs them read again
FRONTENDS = frozenset({"20260930.4", "20261028.4"})
CLIENT_KINDS = frozenset({"screen", "user", "view_columns", "location", "time"})
LOGICAL = frozenset({"and", "or", "not"})
ROW_KEYS = ("alias", "note", "enabled")
ALWAYS_FALSE = {"condition": "not", "conditions": [{"condition": "and", "conditions": []}]}
VALID_ENTITY_ID = re.compile(r"(\w+)\.(\w+)", re.ASCII)
# JavaScript's Number() of a string, after trimming: a decimal, Infinity or a 0x, 0o, 0b integer
JS_NUMBER = re.compile(
    r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)|0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+"
)
# Python's weekday() order, with the frontend's names (src/common/datetime/weekday.ts:35-43)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MEDIA_FEATURE = re.compile(r"\(\s*([a-z-]+)\s*:\s*([^)]*?)\s*\)")
UNKNOWN = "unknown"
MISSING: Any = object()  # JavaScript's undefined, where it differs from null

Condition = dict[str, Any]
Check = Callable[[Condition], bool]
Node = Callable[[Check, list[bool]], bool | None]


def frontend_version() -> str:
    """The frontend that the installed Home Assistant pins (its frontend manifest)."""
    manifest = Path(homeassistant.components.__path__[0], "frontend", "manifest.json")
    (requirement,) = json.loads(manifest.read_text(encoding="utf-8"))["requirements"]
    return str(requirement).removeprefix("home-assistant-frontend==")


# --- the translation to core's form (translate.ts) ------------------------------------------------


def ensure_list(value: Any) -> list[Any]:
    """ensureArray(value) ?? []: a list as it is, null and undefined as none."""
    if value is None or value is MISSING:
        return []
    return value if isinstance(value, list) else [value]


def is_logical(condition: Condition) -> bool:
    return condition.get("condition") in LOGICAL


def children(condition: Condition) -> list[Any]:
    return ensure_list(condition.get("conditions"))


def is_entity_reference(value: Any) -> bool:
    """A string that names an entity and is no number."""
    return (
        isinstance(value, str)
        and math.isnan(js_number(value))
        and VALID_ENTITY_ID.fullmatch(value) is not None
    )


def legacy_only(condition: Condition) -> bool:
    """A Lovelace `state` or `numeric_state` that core would read otherwise: kept in the
    browser."""
    if condition.get("condition") == "numeric_state":
        return any(is_entity_reference(condition.get(bound)) for bound in ("above", "below"))
    if "attribute" in condition:
        return True
    values = [*ensure_list(condition.get("state")), *ensure_list(condition.get("state_not"))]
    return any(is_entity_reference(value) for value in values)


def is_server(condition: Condition) -> bool:
    """Whether the frontend sends the condition to core."""
    if is_logical(condition):
        return all(is_server(child) for child in children(condition))
    if "condition" not in condition:
        return not legacy_only(condition)
    if condition["condition"] in CLIENT_KINDS:
        return False
    if condition["condition"] in ("state", "numeric_state") and "entity_id" not in condition:
        return not legacy_only(condition)
    return True


def is_disabled(condition: Condition) -> bool:
    return condition.get("enabled", MISSING) is False


def row(condition: Condition) -> Condition:
    return {key: condition[key] for key in ROW_KEYS if key in condition}


def translate(condition: Condition) -> Condition:
    """The condition in core's form, as the frontend sends it."""
    if "condition" not in condition:
        return translate_state({"condition": "state", **condition})
    if is_logical(condition):
        return translate_logical(condition)
    if condition["condition"] == "state":
        return translate_state(condition)
    if condition["condition"] == "numeric_state":
        return translate_numeric(condition)
    return condition


def translate_state(condition: Condition) -> Condition:
    if "entity_id" in condition:
        return condition
    if not condition.get("entity") or ("state" not in condition and "state_not" not in condition):
        return row(condition) | ALWAYS_FALSE
    base = {"condition": "state", "entity_id": condition["entity"]}
    if "attribute" in condition:
        base["attribute"] = condition["attribute"]
    if "state" in condition:
        return row(condition) | base | {"state": condition["state"]}
    not_state = base | {"state": condition["state_not"]}
    return row(condition) | {"condition": "not", "conditions": [not_state]}


NEVER: Any = object()  # a bound no value passes


def bound(value: Any, side: str) -> Any:
    """A Lovelace bound in core's form: a number, an entity ID, NEVER, or None (no bound)."""
    if value is None:
        return None
    number = js_number(value)
    if math.isnan(number):
        return value if isinstance(value, str) and VALID_ENTITY_ID.fullmatch(value) else None
    if math.isinf(number):
        return NEVER if (side == "above") == (number > 0) else None
    return int(number) if number.is_integer() else number  # as JSON.stringify writes it


def translate_numeric(condition: Condition) -> Condition:
    if "entity_id" in condition:
        return condition
    if not condition.get("entity"):
        return row(condition) | ALWAYS_FALSE
    above, below = bound(condition.get("above"), "above"), bound(condition.get("below"), "below")
    if NEVER in (above, below):
        return row(condition) | ALWAYS_FALSE
    entity = json.dumps(condition["entity"], ensure_ascii=False)
    if above is None and below is None:
        if "attribute" in condition:
            attribute = json.dumps(condition["attribute"], ensure_ascii=False)
            value = f"{{{{ is_number(state_attr({entity}, {attribute})) }}}}"
        else:
            value = f"{{{{ is_number(states({entity})) }}}}"
        return row(condition) | {"condition": "template", "value_template": value}
    core = row(condition) | {"condition": "numeric_state", "entity_id": condition["entity"]}
    if "attribute" in condition:
        core["attribute"] = condition["attribute"]
    for name, value in (("above", above), ("below", below)):
        if value is not None:
            core[name] = value
    return core


def translate_logical(condition: Condition) -> Condition:
    if "conditions" not in condition:
        return row(condition) | {"condition": "and", "conditions": []}
    translated = [translate(child) for child in children(condition)]
    if condition["condition"] == "not":
        inner = {"condition": "and", "conditions": translated}
        return row(condition) | {"condition": "not", "conditions": [inner]}
    return row(condition) | {"condition": condition["condition"], "conditions": translated}


def with_entity(condition: Condition, entity_id: str) -> Condition:
    """addEntityToCondition: a Lovelace `state` or `numeric_state` without an entity reads the
    card's own entity."""
    if condition.get("conditions"):
        return condition | {"conditions": [with_entity(c, entity_id) for c in children(condition)]}
    kind = condition.get("condition")
    if (kind is None or kind in ("state", "numeric_state")) and "entity_id" not in condition:
        return condition | {"entity": condition.get("entity") or entity_id}
    return condition


# --- the split into subscriptions (split.ts) ------------------------------------------------------


def both(nodes: list[Node]) -> Node:
    """false wins AND, even if a sibling is still unknown."""

    def node(check: Check, results: list[bool]) -> bool | None:
        values = [each(check, results) for each in nodes]
        return False if False in values else None if None in values else True

    return node


def either(nodes: list[Node]) -> Node:
    def node(check: Check, results: list[bool]) -> bool | None:
        values = [each(check, results) for each in nodes]
        return True if True in values else None if None in values else False

    return node


def negated(inner: Node) -> Node:
    def node(check: Check, results: list[bool]) -> bool | None:
        value = inner(check, results)
        return None if value is None else not value

    return node


class Split:
    """The conditions as the frontend splits them: `subtrees` go to core one subscription each;
    `evaluate` combines their results with the browser's checks."""

    def __init__(self, conditions: list[Condition]) -> None:
        self.subtrees: list[Condition] = []
        self.root = both(self.siblings(conditions, "and"))

    def subtree(self, condition: Condition) -> Node:
        index = len(self.subtrees)
        self.subtrees.append(condition)
        return lambda _check, results: results[index]

    def siblings(self, conditions: list[Condition], operator: str) -> list[Node]:
        kept = [c for c in conditions if not is_disabled(c)]
        server = [c for c in kept if is_server(c)]
        nodes = []
        if len(server) == 1:
            nodes.append(self.subtree(translate(server[0])))
        elif server:
            grouped = {"condition": operator, "conditions": [translate(c) for c in server]}
            nodes.append(self.subtree(grouped))
        nodes += [self.build(c) for c in kept if not is_server(c)]
        return nodes

    def build(self, condition: Condition) -> Node:
        if "enabled" in condition and not isinstance(condition["enabled"], bool):
            return lambda _check, _results: None  # a template `enabled`: only core knows
        if is_logical(condition):
            kind = condition["condition"]
            if kind == "or":
                return either(self.siblings(children(condition), "or"))
            inner = both(self.siblings(children(condition), "and"))
            return negated(inner) if kind == "not" else inner
        if is_server(condition):
            return self.subtree(translate(condition))
        return lambda check, _results: check(condition)

    def evaluate(self, check: Check, results: list[bool]) -> bool | None:
        return self.root(check, results)


# --- JavaScript's conversions, where a check relies on them ---------------------------------------


def js_number(value: Any) -> float:
    """Number(value); MISSING (undefined) is NaN, None (null) is 0."""
    if isinstance(value, str | list):
        return js_parse(js_string(value).strip())
    if value is None:
        return 0.0
    return float(value) if isinstance(value, bool | int | float) else math.nan


def js_truthy(value: Any) -> bool:
    """Boolean(value): a list or mapping is true even when empty; NaN and undefined are false."""
    if isinstance(value, list | dict):
        return True
    if value is MISSING or (isinstance(value, float) and math.isnan(value)):
        return False
    return bool(value)


def js_parse(text: str) -> float:
    """Number() of a trimmed string."""
    if not text:
        return 0.0
    if not JS_NUMBER.fullmatch(text):
        return math.nan
    if text[:2].lower() in ("0x", "0o", "0b"):
        return float(int(text, 0))
    return float(text.replace("Infinity", "inf"))


def js_string(value: Any) -> str:
    """String(value)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return js_float(value)
    if isinstance(value, list):
        return ",".join("" if each is None else js_string(each) for each in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def js_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    mantissa, _, exponent = repr(value).partition("e")
    if not exponent:
        return mantissa
    power = int(exponent)
    if -7 < power < 21:
        return format(Decimal(repr(value)), "f")
    return f"{mantissa}e{'+' if power > 0 else '-'}{abs(power)}"


# --- the household user's browser ---------------------------------------------------------------


@dataclasses.dataclass
class Viewer:
    """A household user's browser tab: the user, the window and the time zone the browser's
    checks read, and a websocket client of that user for what core decides. `errors` collects
    what core answered instead of a result (each hid its card), `template_errors` what a template
    logged while it still gave a result (they hide nothing)."""

    hass: HomeAssistant
    client: Any
    user: User
    width: int = 390
    height: int = 844
    dark: bool = False
    columns: int | None = None  # the sections view's columns now; None: not known, any passes
    time_zone: str | None = None  # the browser's; None: the server's
    frontend: str = dataclasses.field(default_factory=frontend_version)
    errors: list[str] = dataclasses.field(default_factory=list)
    template_errors: list[str] = dataclasses.field(default_factory=list)

    def __post_init__(self) -> None:
        assert not self.user.is_admin, "a household user is no administrator"
        assert self.frontend in FRONTENDS, (
            f"frontend {self.frontend}: read its condition sources (this module's docstring) "
            "and add it to FRONTENDS"
        )

    async def visible(self, found: dict[str, Any]) -> bool:
        """Whether the frontend shows `found`, a card, section or conditional row: not while a
        card or section is `disabled`; else its `visibility`, with its `entity` for a condition
        without one, and for `type: conditional` also its `conditions`. Only `found` itself:
        the stack, section or row around it is the caller's to check."""
        if js_truthy(found.get("disabled")):
            return False
        entity = found.get("entity")
        shown = await self.met(found.get("visibility"), entity if isinstance(entity, str) else "")
        if found.get("type") == "conditional":
            shown = shown and await self.met(found["conditions"], "")
        return shown

    async def shown(self, view: dict[str, Any], node: dict[str, Any]) -> bool:
        """Whether the frontend shows `node`, a section, card or row of `view` (its sections and
        its footer's card): `node` and every section, card, stack and conditional row on the
        path from the view to it visible(), none of them a card that hides itself, and no
        section on it whose content all hides."""
        path = self.path(view, node)
        assert path is not None, f"{json.dumps(node)[:80]} is not in the view"
        for each in path:
            if not await self.visible(each) or self.empty(each):
                return False
            sections = view.get("sections") or []
            if any(each is section for section in sections) and await self.all_hidden(each):
                return False
        return True

    def path(self, above: Any, node: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The mappings from below `above` down to `node` (found by identity), node included."""
        if above is node:
            return [node]
        values = above.values() if isinstance(above, dict) else above
        for value in values if isinstance(above, dict | list) else ():
            if isinstance(value, dict | list) and (below := self.path(value, node)) is not None:
                return [value, *below] if isinstance(value, dict) else below
        return None

    async def all_hidden(self, section: dict[str, Any]) -> bool:
        """The section rule: content, and every part of it hidden. 20260930.4 counts the cards,
        20261028.4 also the badges and the sections."""
        content = list(section.get("cards") or [])
        if self.frontend != "20260930.4":
            content += [*(section.get("badges") or []), *(section.get("sections") or [])]
        for each in content:
            if await self.visible(each) and not self.empty(each):
                return False
        return bool(content)

    def empty(self, found: dict[str, Any]) -> bool:
        """A Markdown card with `show_empty: false` whose template renders nothing, as the
        frontend asks for it (strict, the variables `config` and `user`); core strips it."""
        if found.get("type") != "markdown" or found.get("show_empty") is not False:
            return False
        variables = {"config": found, "user": self.user.name}
        template = Template(str(found.get("content", "")), self.hass)
        return str(template.async_render(variables, parse_result=False, strict=True)) == ""

    async def met(self, conditions: list[Any] | None, entity: str) -> bool:
        """The conditions' verdict once core has answered every subtree."""
        conditions = self.valid(conditions or [])
        if not conditions:
            return True
        if entity:
            conditions = [with_entity(c, entity) for c in conditions]
        split = Split(conditions)
        results, errors = [], []
        for subtree in split.subtrees:
            result, error = await self.ask(subtree)
            results.append(result)
            if error is not None:
                errors.append(f"{json.dumps(subtree)}: {error}")
        self.errors += errors
        if errors:  # "don't treat a server error as false that a client not would invert"
            return False
        return split.evaluate(lambda c: self.check(c, entity), results) is True

    def valid(self, conditions: list[Any]) -> list[Condition]:
        """The entries the frontend evaluates, at any depth. An array is an object to JavaScript:
        both frontends keep it and read it as a `state` condition without an entity or a state,
        so it is always false (translate.ts lines 159-166). Any other entry that is no mapping
        20261028.4 drops; 20260930.4 throws on it, so a template with one is wrong there."""
        kept = []
        for each in conditions:
            entry = {} if isinstance(each, list) else each
            if not isinstance(entry, dict):
                assert self.frontend != "20260930.4", (
                    f"frontend 20260930.4 throws on a condition that is no mapping: {conditions}"
                )
                continue
            nested = entry.get("conditions") is not None
            kept.append(entry | {"conditions": self.valid(children(entry))} if nested else entry)
        return kept

    async def ask(self, condition: Condition) -> tuple[bool, str | None]:
        """Core's first answer to `subscribe_condition`, then the subscription ended."""
        await self.client.send_json_auto_id({"type": "subscribe_condition", "condition": condition})
        answer = await self.client.receive_json()
        if not answer["success"]:  # the subscription failed: hidden, with its error
            return False, str(answer["error"]["message"])
        first = await self.client.receive_json()
        assert (first["id"], first["type"]) == (answer["id"], "event"), first
        await self.client.send_json_auto_id(
            {"type": "unsubscribe_events", "subscription": answer["id"]}
        )
        while (reply := await self.client.receive_json())["type"] == "event":
            assert reply["id"] == answer["id"], reply  # a later answer of the same subscription
        assert reply["success"], reply
        event = first["event"]
        self.template_errors += [str(e) for e in event.get("template_errors", [])]
        if "error" in event:
            error = event["error"]
            return False, str(error["message"] if isinstance(error, dict) else error)
        return bool(event["result"]), None

    # the browser's checks (checkConditionsMet for one condition)

    def check(self, condition: Condition, host: str) -> bool:
        checks: dict[Any, Callable[[Condition, str], bool]] = {
            "view_columns": self.check_columns,
            "time": self.check_time,
            "screen": self.check_screen,
            "user": self.check_user,
            "location": self.check_location,
            "numeric_state": self.check_numeric,
            "and": self.check_logical,
            "or": self.check_logical,
            "not": self.check_logical,
        }
        return checks.get(condition.get("condition"), self.check_state)(condition, host)

    def check_logical(self, condition: Condition, host: str) -> bool:
        listed = condition.get("conditions")
        if not listed:
            return True
        if not isinstance(listed, list):
            return False  # the browser's .every on a mapping throws: false
        if condition["condition"] == "or":
            return any(self.check(c, host) for c in listed)
        met = all(self.check(c, host) for c in listed)
        return not met if condition["condition"] == "not" else met

    def entity_of(self, condition: Condition, host: str) -> Any:
        entity_id = condition.get("entity_id") or condition.get("entity") or host
        return self.hass.states.get(entity_id) if entity_id else None

    def state_of(self, entity_id: Any) -> str | None:
        """getValueFromEntityId: the state of a value that names an existing entity."""
        valid = isinstance(entity_id, str) and VALID_ENTITY_ID.fullmatch(entity_id)
        found = self.hass.states.get(entity_id) if valid else None
        return found.state if found else None

    def check_state(self, condition: Condition, host: str) -> bool:
        found = self.entity_of(condition, host)
        attribute = condition.get("attribute")
        if found is None:
            state = UNKNOWN
        elif attribute:
            value = found.attributes.get(attribute)
            state = UNKNOWN if value is None else js_string(value)
        else:
            state = found.state
        positive = condition.get("state") is not None
        value = condition["state"] if positive else condition.get("state_not")
        if value is None:
            return False
        if isinstance(value, list):
            values = [*value, *(s for v in value if (s := self.state_of(v)) is not None)]
        elif isinstance(value, str):
            values = [value, *(s for s in (self.state_of(value),) if s)]
        else:
            values = [value]
        return (state in values) == positive

    def check_numeric(self, condition: Condition, host: str) -> bool:
        found = self.entity_of(condition, host)
        attribute = condition.get("attribute")
        if found is None:
            state = MISSING
        elif attribute:
            state = found.attributes.get(attribute, MISSING)
        else:
            state = found.state
        number = js_number(state)
        if math.isnan(number):
            return False
        limits = []
        for name in ("above", "below"):
            raw = condition.get(name)
            if raw is None:
                continue
            state_of_raw = self.state_of(raw)
            given = raw if state_of_raw is None else state_of_raw
            limit = js_number(given)
            if not math.isnan(limit):
                limits.append(limit < number if name == "above" else limit > number)
        return all(limits)

    def check_columns(self, condition: Condition, _host: str) -> bool:
        if not self.columns:
            return True
        low, high = condition.get("min"), condition.get("max")
        return (low is None or self.columns >= low) and (high is None or self.columns <= high)

    def check_user(self, condition: Condition, _host: str) -> bool:
        users = condition.get("users")
        return bool(users) and self.user.id in users

    def check_location(self, condition: Condition, _host: str) -> bool:
        person = next(
            (
                s
                for s in self.hass.states.async_all("person")
                if s.attributes.get("user_id") == self.user.id
            ),
            None,
        )
        if person is None:
            return False
        locations = condition.get("locations")
        if self.frontend == "20260930.4":
            return bool(locations) and person.state in locations
        if locations and person.state in locations:
            return True
        if condition.get("away") is True and person.state == "not_home":
            return True
        assert not isinstance(condition.get("target"), dict), "zone targets are not modelled"
        return False

    def check_screen(self, condition: Condition, _host: str) -> bool:
        query = condition.get("media_query")
        return bool(query) and self.media(str(query))

    def check_time(self, condition: Condition, _host: str) -> bool:
        zone = dt_util.get_time_zone(self.time_zone or self.hass.config.time_zone)
        now = dt_util.now(zone)
        weekdays = condition.get("weekdays")
        if weekdays and WEEKDAYS[now.weekday()] not in weekdays:
            return False
        after, before = condition.get("after"), condition.get("before")

        def today(text: str) -> datetime:
            hour, minute, *second = (int(part) for part in text.split(":"))
            return now.replace(hour=hour, minute=minute, second=sum(second), microsecond=0)

        start, end = (today(after) if after else None), (today(before) if before else None)
        if start and end:
            if end < start:  # across midnight, such as 22:00 to 06:00
                return not now < start or not now > end
            return start <= now <= end
        if start:
            return not now < start
        if end:
            return not now > end
        return True

    def media(self, query: str) -> bool:
        """matchMedia(query).matches for the window, in the forms the screen condition writes;
        any other form fails the test rather than being guessed."""
        return any(self.media_part(part.strip()) for part in query.split(","))

    def media_part(self, part: str) -> bool:
        met = True
        for token in (t.strip() for t in re.split(r"\band\b", part)):
            if token in ("all", "screen", "only screen"):
                continue
            match = MEDIA_FEATURE.fullmatch(token)
            assert match, f"no rule for the media query {part!r}"
            name, value = match.groups()
            if name in ("min-width", "max-width", "min-height", "max-height"):
                assert value.endswith("px"), f"no rule for the media query {part!r}"
                size = float(value.removesuffix("px"))
                measured = self.width if name.endswith("width") else self.height
                met = met and (measured >= size if name.startswith("min") else measured <= size)
            elif name == "orientation":
                met = met and value == ("portrait" if self.height >= self.width else "landscape")
            elif name == "prefers-color-scheme":
                met = met and value == ("dark" if self.dark else "light")
            else:
                raise AssertionError(f"no rule for the media query {part!r}")
        return met


async def household_viewer(hass: HomeAssistant, hass_ws_client: Any, **window: Any) -> Viewer:
    """The browser of Bob, a household user: in the group `system-users`, no administrator."""
    user = await hass.auth.async_create_user("Bob", group_ids=[GROUP_ID_USER])
    refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    client = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    return Viewer(hass, client, user, **window)
