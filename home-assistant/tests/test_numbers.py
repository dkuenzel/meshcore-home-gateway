"""Numbers in the dashboard's language against the real Home Assistant (#145;
docs/home-assistant.md, Dashboard).

Every card that prints a number writes it as the dashboard's language does: the language's decimal
mark (i18n.DECIMAL_MARKS) in place of the point, and a whole number without ".0", in every
language the gateway ships and in the pseudo-language, whose mark no shipped language writes.
Texts and names from the mesh keep their points. Home Assistant's own template engine renders the
cards, so its `float`, `round` and `is_number` decide what num() prints: only this test proves it.
A Markdown card's template knows neither the viewer's language nor the number format of the
viewer's profile (docs/platform-notes.md, Home Assistant dashboards).

The household and the helpers are those of test_dashboard.py, test_chats.py and test_gateway.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from test_chats import received
from test_contract import RADIO, fire, problems
from test_gateway import FACTS, HEALTH, PROFILE, profile
from test_overview import health

from meshcore_home_gateway import i18n
from meshcore_home_gateway.config_view import shown_settings, target_settings
from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.mesh import Node, Readings
from meshcore_home_gateway.views import config_document, contact_document
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, NOW, Broker
from test_contacts import PEOPLE, SENSORS
from test_dashboard import (
    BOB,
    HISTORY,
    LANGUAGES,
    PENDING,
    card,
    cards,
    contacts_of,
    deliver,
    documented,
    gateway,
    household,
    made,
    render,
    said,
    speaking,
    topics_of,
)

# Alice's answer: her battery at 3.9 V, a temperature of 21.5 °C on channel 2
ANSWER = "0174" + f"{390:04x}" + "0267" + f"{215:04x}"
# a message and a name from the mesh: their points are no decimal marks
TEXT, NAME = "Update 2.1.0 at 3.5 V", "Bob 2.0"


@pytest.mark.parametrize("language", LANGUAGES)
async def test_numbers_follow_the_dashboards_language(  # noqa: PLR0915 - card by card
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    """#145: German writes 0,4 s, 869,618 MHz, 62,5 kHz, 3,9 V, 6,25 dB and 21,5; every language
    writes 10 for 10.0; a message's text and a contact's name keep their points."""
    config = speaking(language, monkeypatch)
    mark = i18n.DECIMAL_MARKS[language]

    def written(number: str) -> str:
        """A number as the dashboard's language writes it."""
        return number.replace(".", mark)

    broker, mesh = Broker(), household(ALICE_NODE, Node(BOB, NAME, COMPANION))
    mesh.contact_answered(
        ALICE, NOW, None, None, Readings(NOW, lpp.named(lpp.decode(bytes.fromhex(ANSWER))))
    )
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    await deliver(hass, config, broker, mesh)

    # Gateway: the slowest response, the airtime left (whole seconds), the radio's facts
    await fire(hass, topics.state, health(loop_lag_max_s=0.4) | {"radio_clock_offset_s": 0})
    documented(hass, PENDING, [])
    shown = render(hass, card(cards(loaded, "gateway"), HEALTH))
    assert said(config, "{slow} s", slow=written("0.4")) in shown
    assert said(config, "{spare} of {hour} s", spare="9", hour="36") in shown
    shown = render(hass, card(cards(loaded, "gateway"), FACTS))
    assert said(config, "{mhz} MHz", mhz=written("869.618")) in shown
    assert said(config, "{khz} kHz", khz=written("62.5")) in shown
    assert said(config, "{dbm} dBm", dbm="22") in shown
    assert said(config, "4/{cr}", cr="8") in shown

    # Gateway: a setting's number on the radio and the one wanted; the radio's name as it is
    document = profile(None, None)
    duty = {"live": 2.5, "desired": 10.0, "state": "drift"}
    document["settings"] |= {"duty_cycle_percent": duty}
    document["settings"]["name"]["live"] = "Radio 2.0"
    await fire(hass, topics.settings, document, retain=True)
    shown = render(hass, card(cards(loaded, "gateway"), PROFILE))
    assert f"<td>{written('2.5')}</td><td>10</td>" in shown
    assert "<td>Radio 2.0</td><td>Gateway</td>" in shown

    # Settings: the airtime budget and the burst with a fraction (a whole number: the duty
    # cycle's 10 above)
    settings = shown_settings(config)
    settings["airtime"] = {"budget_s_per_hour": 7.2, "burst_s": 2.5}
    await fire(
        hass,
        topics.config,
        config_document(time.time(), target_settings(config, RADIO), settings),
        retain=True,
    )
    shown = render(hass, card(cards(loaded, "settings"), "set air = state_attr(c, 'airtime')"))
    assert (
        said(
            config,
            "{budget} s an hour, bursts of {burst} s",
            budget=written("7.2"),
            burst=written("2.5"),
        )
        in shown
    )

    # Contacts: Alice's battery to a tenth of a volt, her reading as it came; Bob's name
    for key in (ALICE, BOB):
        document = contact_document(mesh.contacts[key], telemetry_state="answering")
        await fire(hass, topics_of(config).contact(key), document)
    shown = render(hass, card(cards(loaded, "contacts"), PEOPLE))
    assert said(config, "{volts} V", volts=written("3.9")) in shown
    assert f">{NAME}<" in shown
    shown = render(hass, card(cards(loaded, "contacts"), SENSORS))
    assert f"{written('3.9')} V" in shown
    assert f"{written('21.5')} °C" in shown

    # Alice's chat page: her battery, the hops and the signal
    about = card(cards(loaded, "alice"), "set measured = ")
    hass.states.async_set("sensor.mesh_alice_hops", "2")
    hass.states.async_set("sensor.mesh_alice_snr", "6.25")
    shown = render(hass, about)
    assert said(config, "{volts} V", volts=written("3.9")) in shown
    message = "At its last message it was {hops} hops away, at a signal of {snr} dB."
    assert said(config, message, hops="2", snr=written("6.25")) in shown

    # a message from the mesh and its sender's name keep their points
    documented(hass, HISTORY, [received(60, TEXT, name=NAME)])
    shown = render(hass, card(cards(loaded, "alerts"), "macro row(e)"))
    assert TEXT in shown
    assert NAME in shown
    if mark != ".":
        assert "0.4" not in render(hass, card(cards(loaded, "gateway"), HEALTH))
    assert problems(caplog) == []
