"""The contract tests' own helpers against what they model (design 7.3, harness first).

cards() finds a page's cards by its path, a chat page's alias or slug, or a contact's key. The
household user's browser (visibility.py) decides a card's visibility as the frontend does since
Home Assistant 2026.10: each case below states the frontend's rule it follows, so that a wrong
helper fails here before it passes a wrong dashboard.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from test_contract import problems
from visibility import Split, Viewer, household_viewer, translate

from test_contact_devices import ALICE
from test_dashboard import card, cards, load, page, render, shows_nothing

DOOR, SHED, SCRIPT = "binary_sensor.shed_door", "sensor.shed_temperature", "script.mesh_message"
WANTED = "sensor.shed_door_wanted"
PAGES = f"""
title: Pages
views:
  - title: Overview
    path: overview
    type: sections
    sections:
      - type: grid
        cards:
          - type: markdown
            content: overview
          - type: vertical-stack
            cards:
              - type: markdown
                content: nested
  - title: Family
    path: chat-family
    subview: true
    back_path: ./overview
    type: sections
    sections:
      - type: grid
        cards:
          - type: markdown
            content: family thread
    footer:
      card:
        type: entities
        entities:
          - entity: text.meshcore_home_gateway_radio1_write_family
  - title: Alice
    path: contact-alice
    subview: true
    back_path: ./overview
    type: sections
    sections:
      - type: grid
        cards:
          - type: markdown
            content: alice thread
    footer:
      card:
        type: entities
        visibility:
          - condition: template
            value_template: >-
              {{{{ device_attr(device_id('text.mesh_alice_write'), 'serial_number')
                 == '{ALICE}' }}}}
        entities:
          - entity: text.mesh_alice_write
"""


async def test_cards_are_found_by_path_alias_slug_or_key(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A view's cards, nested ones included; a subview's footer card; a page named twice or
    not at all fails."""
    loaded = (await load(hass, hass_ws_client, {"mesh-pages": PAGES}))["mesh-pages"]
    assert [c["type"] for c in cards(loaded, "overview")] == [
        "markdown",
        "vertical-stack",
        "markdown",
    ]
    family = cards(loaded, "chat-family")
    assert [c["type"] for c in family] == ["markdown", "entities"]
    assert cards(loaded, "family") == family
    assert card(family, "thread")["content"] == "family thread"
    alice = cards(loaded, "contact-alice")
    assert cards(loaded, "alice") == cards(loaded, ALICE) == alice
    assert alice[-1]["entities"] == [{"entity": "text.mesh_alice_write"}]
    assert page(loaded, ALICE)["path"] == "contact-alice"
    for needle in ("nobody", "thread", "e5" * 32):
        with pytest.raises(AssertionError):
            page(loaded, needle)
    assert problems(caplog) == []


def test_the_frontends_translation_to_core() -> None:
    """translate.ts lines 148-190 and 281-304: `state_not` as a `not` of a `state`, a Lovelace
    `not` as NOT(AND); split.ts lines 107-139: sibling conditions for core in one subscription,
    `enabled: false` left out, a browser condition kept apart."""
    assert translate({"condition": "state", "entity": SCRIPT, "state_not": ["on", "off"]}) == {
        "condition": "not",
        "conditions": [{"condition": "state", "entity_id": SCRIPT, "state": ["on", "off"]}],
    }
    assert translate({"condition": "not", "conditions": [{"entity": DOOR, "state": "on"}]}) == {
        "condition": "not",
        "conditions": [
            {
                "condition": "and",
                "conditions": [{"condition": "state", "entity_id": DOOR, "state": "on"}],
            }
        ],
    }
    assert translate({"condition": "numeric_state", "entity": SHED}) == {
        "condition": "template",
        "value_template": '{{ is_number(states("sensor.shed_temperature")) }}',
    }
    split = Split(
        [
            {"condition": "state", "entity": DOOR, "state": "on"},
            {"condition": "user", "users": ["someone"]},
            {"condition": "template", "value_template": "{{ true }}"},
            {"condition": "state", "entity": DOOR, "state": "off", "enabled": False},
        ]
    )
    assert split.subtrees == [
        {
            "condition": "and",
            "conditions": [
                {"condition": "state", "entity_id": DOOR, "state": "on"},
                {"condition": "template", "value_template": "{{ true }}"},
            ],
        }
    ]


DOOR_ON = {"condition": "state", "entity": DOOR, "state": "on"}
DOOR_OFF = {"condition": "state", "entity": DOOR, "state": "off"}
MISSING = {"condition": "state", "entity": SCRIPT, "state_not": ["on", "off"]}
NOBODY = {"condition": "user", "users": ["nobody"]}


async def visible(viewer: Viewer, *conditions: Any, **card: Any) -> bool:
    return await viewer.visible({"type": "markdown", "visibility": list(conditions)} | card)


async def shed(hass: HomeAssistant, hass_ws_client: Any, **window: Any) -> Viewer:
    """The shed's door open, its sensor at 21.5 with a version attribute; a household user's
    browser."""
    hass.states.async_set(DOOR, "on")
    hass.states.async_set(SHED, "21.5", {"bridge_version": "2.0.0", "battery": 3.9})
    return await household_viewer(hass, hass_ws_client, **window)


async def test_core_decides_state_numeric_and_template_conditions(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """translate.ts lines 74-95: a Lovelace `state` or `numeric_state` without an attribute or
    an entity among its values goes to core, a template always."""
    viewer = await shed(hass, hass_ws_client)
    assert await visible(viewer)  # no conditions (conditional-listener-mixin.ts)
    assert await visible(viewer, DOOR_ON)
    assert not await visible(viewer, DOOR_OFF)
    assert await visible(viewer, {"condition": "state", "entity": DOOR, "state_not": "off"})
    assert viewer.errors == []
    # a missing entity: core answers an error, which hides the card under `state_not` too, where
    # the browser before 2026.10 read `unknown` and showed it (HA-09)
    assert not await visible(viewer, MISSING)
    (error,) = viewer.errors
    assert f"unknown entity {SCRIPT}" in error
    # ... also where a `not` in the browser would invert it (condition-evaluator-controller.ts
    # lines 302-307)
    viewer.errors.clear()
    assert not await visible(viewer, {"condition": "not", "conditions": [NOBODY, MISSING]})
    assert len(viewer.errors) == 1
    # sibling conditions go to core as one `and` (split.ts lines 123-132), and core's `and`
    # answers false without an error when one of them is false
    viewer.errors.clear()
    assert not await visible(viewer, MISSING, DOOR_OFF)
    assert viewer.errors == []
    # the same check as a template condition, allowed to a household user
    hint = {"condition": "template", "value_template": f"{{{{ not has_value('{SCRIPT}') }}}}"}
    assert await visible(viewer, hint)
    # numeric_state; without a bound, a template that the value is a number (:240-250)
    assert await visible(viewer, {"condition": "numeric_state", "entity": SHED, "above": 20})
    assert not await visible(viewer, {"condition": "numeric_state", "entity": SHED, "below": 20})
    assert await visible(viewer, {"condition": "numeric_state", "entity": SHED})
    # a condition without an entity reads the card's own entity (hui-card.ts), one with an
    # entity keeps it (validate-condition.ts, addEntityToCondition: `entity || entityId`)
    assert await visible(viewer, {"condition": "state", "state": "on"}, type="tile", entity=DOOR)
    assert await visible(viewer, DOOR_ON, type="tile", entity=SHED)
    # `enabled: false` is left out (split.ts lines 114-117)
    assert await visible(viewer, DOOR_OFF | {"enabled": False})
    assert viewer.errors == []
    assert viewer.template_errors == []
    assert problems(caplog) == []


async def test_the_viewer_is_a_household_user(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The viewer's connection is Bob's and he is no administrator: core refuses him
    `test_condition`, which needs one (websocket_api/commands.py lines 1061-1063 at 2026.10.2,
    1168-1170 at 2026.11.0), and answers his `subscribe_condition`, which does not."""
    viewer = await shed(hass, hass_ws_client)
    await viewer.client.send_json_auto_id(
        {
            "type": "test_condition",
            "condition": {"condition": "state", "entity_id": DOOR, "state": "on"},
        }
    )
    refused = await viewer.client.receive_json()
    assert (refused["success"], refused["error"]["code"]) == (False, "unauthorized")
    assert await visible(viewer, DOOR_ON)
    (logged,) = problems(caplog)
    assert "Unauthorized" in logged
    assert "Bob" in logged


async def test_a_condition_core_refuses_hides_its_card(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A subscription that core refuses is false with an error (condition-evaluator-controller.ts
    lines 264-272); one refused sibling takes the whole group with it (translate.ts lines
    141-142)."""
    viewer = await shed(hass, hass_ws_client)
    assert not await visible(viewer, DOOR_ON, {"condition": "template"})
    (error,) = viewer.errors
    assert "value_template" in error
    (logged,) = problems(caplog)
    assert "value_template" in logged


async def test_the_browser_decides_its_own_kinds(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """translate.ts lines 21-28 and 52-67: screen, user, view_columns, location, time, and a
    `state` with an attribute, by validate-condition.ts; `or` and `not` around both kinds in the
    browser over core's answers (split.ts)."""
    viewer = await shed(hass, hass_ws_client)
    # with an attribute, the old rule: a missing entity or attribute reads `unknown`
    version = {"condition": "state", "entity": SHED, "attribute": "bridge_version"}
    assert not await visible(viewer, version | {"state_not": ["2.0.0", "unknown"]})
    assert await visible(viewer, version | {"state_not": ["2.1.0", "unknown"]})
    gone = version | {"entity": "sensor.gone", "state_not": ["2.1.0", "unknown"]}
    assert not await visible(viewer, gone)
    # a value that names an entity adds that entity's state, in the browser only (translate.ts
    # lines 63-66, validate-condition.ts lines 191-203); core would compare the ID itself
    hass.states.async_set(WANTED, "on")
    assert await visible(viewer, {"condition": "state", "entity": DOOR, "state": WANTED})
    assert await visible(viewer, {"condition": "or", "conditions": [NOBODY, DOOR_ON]})
    assert not await visible(viewer, {"condition": "or", "conditions": [NOBODY, DOOR_OFF]})
    # the siblings for core go as one `or` here (split.ts lines 123-132), and core's `or` stops
    # at its first true child, so the missing entity after it raises no error
    assert await visible(viewer, {"condition": "or", "conditions": [NOBODY, DOOR_ON, MISSING]})
    assert await visible(viewer, {"condition": "not", "conditions": [NOBODY, DOOR_ON]})
    # a card or section with `disabled` is hidden before its conditions (hui-card.ts,
    # hui-section.ts)
    assert not await viewer.visible({"type": "markdown", "disabled": True})
    assert not await viewer.visible({"type": "grid", "disabled": True, "cards": []})
    assert await visible(viewer, {"condition": "user", "users": [viewer.user.id]})
    assert not await visible(viewer, NOBODY)
    wide = {"condition": "screen", "media_query": "(min-width: 768px)"}
    assert not await visible(viewer, wide)  # 390 px
    assert await visible(await household_viewer(hass, hass_ws_client, width=1024), wide)
    assert await visible(viewer, {"condition": "view_columns", "min": 2})  # columns unknown
    narrow = await household_viewer(hass, hass_ws_client, columns=1)
    assert not await visible(narrow, {"condition": "view_columns", "min": 2})
    now = dt_util.now(dt_util.get_time_zone(hass.config.time_zone))
    clock = [(now + timedelta(hours=h)).strftime("%H:%M:%S") for h in (-1, 1, 2, -2)]
    assert await visible(viewer, {"condition": "time", "after": clock[0], "before": clock[1]})
    assert not await visible(viewer, {"condition": "time", "after": clock[1], "before": clock[2]})
    # from an hour ago across midnight to two hours ago (check_time.ts lines 114-118)
    assert await visible(viewer, {"condition": "time", "after": clock[0], "before": clock[3]})
    hass.states.async_set("person.bob", "home", {"user_id": viewer.user.id})
    assert await visible(viewer, {"condition": "location", "locations": ["home"]})
    assert not await visible(viewer, {"condition": "location", "locations": ["not_home"]})
    assert viewer.errors == []
    assert problems(caplog) == []


async def test_a_conditional_card_and_an_entry_that_is_no_mapping(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A conditional card also needs its `conditions` (hui-conditional-base.ts lines 68-74); an
    entry that is no mapping is dropped since 20261028.4 (sanitize.ts) and throws before; an
    array is an object to JavaScript, kept by both, and always false (translate.ts lines
    159-166)."""
    viewer = await shed(hass, hass_ws_client)
    conditional = {"type": "conditional", "conditions": [DOOR_OFF], "card": {"type": "markdown"}}
    assert not await viewer.visible(conditional)
    assert await viewer.visible(conditional | {"conditions": [DOOR_ON]})
    assert not await visible(viewer, ["x"], DOOR_ON)
    assert not await visible(viewer, {"condition": "or", "conditions": [[DOOR_ON]]}, DOOR_ON)
    assert viewer.errors == []
    if viewer.frontend == "20261028.4":
        assert await visible(viewer, None, DOOR_ON)
    else:
        with pytest.raises(AssertionError, match="no mapping"):
            await visible(viewer, None, DOOR_ON)
    assert problems(caplog) == []


async def test_an_empty_card_renders_to_nothing_at_all(hass: HomeAssistant) -> None:
    """Core strips the output, so blank lines are nothing; markup without text is something;
    a card without `show_empty: false` shows even nothing (hui-markdown-card.ts)."""
    for content in (
        "{%- if false %}x{% endif -%}",
        "\n",
        "{{ ' ' }}",
        "{% if false %}x{% endif %}\n",
    ):
        shows_nothing(hass, {"type": "markdown", "show_empty": False, "content": content})
    for content in ("<div></div>", "&nbsp;", "{{ '\u200b' }}"):
        with pytest.raises(AssertionError, match="shows"):
            shows_nothing(hass, {"type": "markdown", "show_empty": False, "content": content})
        assert render(hass, {"type": "markdown", "content": content}) != ""
    with pytest.raises(AssertionError, match="show_empty"):
        shows_nothing(hass, {"type": "markdown", "content": "\n"})
