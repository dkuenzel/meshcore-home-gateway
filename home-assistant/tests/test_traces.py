"""Traces on the map against the real Home Assistant recorder (design 6c D18-D21;
docs/home-assistant.md "Traces on the map").

A trace is the tracker's rows in Home Assistant's recorder. A map card with `hours_to_show` asks
for them with the frontend's `history/stream` subscription, `minimal_response`,
`significant_changes_only` and `no_attributes` all false (hui-map-card.ts, frontend 20260930.2),
and draws a point for each row whose latitude and longitude are set. The gateway's own code builds
the documents and the devices. The tests check what the guide says: a point per move and none per
answer at rest, a point for an advert's move and for a switch of the source, an expiry as a row
without coordinates, the guide's card exactly as written, and the guide's recorder exclusion.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from test_contract import TOPICS, fire, guide_block, problems

from meshcore_home_gateway.core.mesh import AdvertCheck, Mesh
from meshcore_home_gateway.core.model import Position
from test_contact_devices import ALICE, ALICE_NODE, NOW, contact_entities, guide_recorder
from test_telemetry import (
    BATTERY_4_12,
    MCU,
    TRACKER,
    answer,
    document,
    gps,
    home,
    with_alice,
    with_person,
)

PERSON = "person.alice"
MAP_CARD = {"minimal_response": False, "significant_changes_only": False, "no_attributes": False}
Point = tuple[float, float, str]  # latitude, longitude, position_source


async def subscribe(hass: HomeAssistant, hass_ws_client: Any, card: dict[str, Any]) -> Any:
    """The subscription a map card opens for its entities and `hours_to_show`."""
    await async_wait_recording_done(hass)
    client = await hass_ws_client(hass)
    start = dt_util.utcnow() - timedelta(hours=card["hours_to_show"])
    await client.send_json({
        "id": 1, "type": "history/stream", "entity_ids": card["entities"],
        "start_time": start.isoformat(), **MAP_CARD,
    })  # fmt: skip
    return client


async def rows(client: Any) -> dict[str, list[dict[str, Any]]]:
    """The rows the subscription sent until it went quiet: first those in the recorder, then
    each new state while it stays open."""
    found: dict[str, list[dict[str, Any]]] = {}
    while True:
        try:
            message = await asyncio.wait_for(client.receive_json(), 0.5)
        except TimeoutError:
            return found
        if message.get("type") == "event":
            for entity_id, states in message["event"]["states"].items():
                found.setdefault(entity_id, []).extend(states)


def points(entity_rows: list[dict[str, Any]]) -> list[Point]:
    """The card's points: the rows with a latitude and a longitude (hui-map-card.ts)."""
    return [
        (a["latitude"], a["longitude"], a["position_source"])
        for row in entity_rows
        if (a := row.get("a") or {}).get("latitude") and a.get("longitude")
    ]


def point(mesh: Mesh) -> Point:
    """Alice's position as her document has it."""
    found = document(mesh)
    return found["latitude"], found["longitude"], found["position_source"]


def card_of(*entities: str) -> dict[str, Any]:
    return {"type": "map", "entities": list(entities), "hours_to_show": 24}


async def test_a_trace_has_a_point_per_move_and_none_per_resting_answer(
    recorder_mock: Any, hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *,
    caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:  # fmt: skip
    """Telemetry: the first fix and each move of 20 m or more, nothing for an answer at rest or
    a move of 10 m. Then an advert that carries a position 5 m on, and an answer at that place
    (to the 11 m of a GPS entry): a point each, the second for the switch of the source."""
    assert await async_setup_component(hass, "history", {})
    mesh, _devices, _broker = await with_alice(
        hass, tmp_path, BATTERY_4_12, gps(home(hass, 300)), MCU
    )
    expected = [point(mesh)]
    for at, north_m, moved in ((900, 300, False), (1800, 330, True), (2700, 340, False),
                               (3600, 400, True)):  # fmt: skip
        answer(mesh, NOW + at, BATTERY_4_12, gps(home(hass, north_m)), MCU)
        await fire(hass, TOPICS.contact(ALICE), document(mesh))
        if moved:
            expected.append(point(mesh))
    latitude, longitude, _source = expected[-1]
    at = int(NOW) + 3700  # her advert 5 m on: the record takes its position (flag 0x10)
    advertised = Position(latitude + 5 / 111_195, longitude)
    mesh.contacts_read([replace(ALICE_NODE, last_advert=at, position=advertised)], at)
    mesh.advert_checked(ALICE, AdvertCheck(at, True), at)
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    answer(mesh, NOW + 4500, BATTERY_4_12, gps(advertised), MCU)
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    trace = points((await rows(await subscribe(hass, hass_ws_client, card_of(TRACKER))))[TRACKER])
    assert trace[: len(expected)] == expected
    assert [source for _, _, source in trace[len(expected) :]] == ["advert", "telemetry"]
    assert trace[-2][:2] == (advertised.latitude, advertised.longitude)
    assert problems(caplog) == []


async def test_an_expiry_is_a_point_without_coordinates(
    recorder_mock: Any, hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *,
    caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:  # fmt: skip
    """Her answers expire, then she answers again at the same place: a row without a position,
    which the card skips, and the same point again."""
    assert await async_setup_component(hass, "history", {})
    mesh, _devices, _broker = await with_alice(
        hass, tmp_path, BATTERY_4_12, gps(home(hass, 300)), MCU
    )
    located = point(mesh)
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "answering"))
    found = (await rows(await subscribe(hass, hass_ws_client, card_of(TRACKER))))[TRACKER]
    shown = [(row["s"], (row.get("a") or {}).get("latitude")) for row in found]
    first = shown.index(("not_home", located[0]))
    assert shown[first:] == [("not_home", located[0]), ("unavailable", None),
                             ("not_home", located[0])]  # fmt: skip
    assert points(found) == [located, located]  # the card joins them with a straight line
    assert problems(caplog) == []


async def test_the_documented_map_card_reads_its_trace(
    recorder_mock: Any, hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *,
    caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:  # fmt: skip
    """The guide's card exactly as written: a map of the mesh trackers, whose `hours_to_show`
    (24) is the window the card asks for."""
    card = guide_block("type: map")
    assert card == card_of(TRACKER)
    assert await async_setup_component(hass, "history", {})
    mesh, _devices, _broker = await with_alice(
        hass, tmp_path, BATTERY_4_12, gps(home(hass, 300)), MCU
    )
    assert set(card["entities"]) <= set(contact_entities(hass, ALICE))
    assert all(entity.startswith("device_tracker.mesh_") for entity in card["entities"])
    expected = [point(mesh)]
    answer(mesh, NOW + 900, BATTERY_4_12, gps(home(hass, 400)), MCU)
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    expected.append(point(mesh))
    assert points((await rows(await subscribe(hass, hass_ws_client, card)))[TRACKER]) == expected
    assert problems(caplog) == []


@pytest.mark.parametrize(
    "recorder_config", [{"exclude": {"entity_globs": guide_recorder("device_tracker.mesh_*")}}]
)
async def test_without_the_recorder_there_is_no_trace_but_a_person_keeps_one(
    recorder_mock: Any, hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *,
    caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:  # fmt: skip
    """The guide's recorder exclusion: the tracker leaves no row in the database, a card open at
    that moment still gets the next point, and a `person` with the tracker records its own."""
    assert await async_setup_component(hass, "history", {})
    await with_person(hass)
    mesh, _devices, _broker = await with_alice(
        hass, tmp_path, BATTERY_4_12, gps(home(hass, 300)), MCU
    )
    client = await subscribe(hass, hass_ws_client, card_of(TRACKER, PERSON))
    stored = await rows(client)
    assert not stored.get(TRACKER)
    first = point(mesh)[:2]
    assert [(a["latitude"], a["longitude"]) for a in person_positions(stored)] == [first]
    answer(mesh, NOW + 900, BATTERY_4_12, gps(home(hass, 400)), MCU)
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    assert points((await rows(client))[TRACKER]) == [point(mesh)]  # live, while it stays open
    assert problems(caplog) == []


def person_positions(found: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [a for row in found[PERSON] if (a := row.get("a") or {}).get("latitude")]
