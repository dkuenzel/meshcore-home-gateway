"""The generated dashboard's pages for whoever looks after the gateway, against the real Home
Assistant: Gateway (health, the radio, its settings, the newest events), Settings and the map's
hints (docs/home-assistant.md, Dashboard).

The household and the helpers are those of test_dashboard.py. Contract items 7 (the radio's own
permissions as they act, D-58), 8 (the Health row "Messages on their way" counts as the status
line does) and 14 (Settings from the configuration the gateway announces, and without it).
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from ha_plugin import async_fire_time_changed
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import TrackTemplate, async_track_template_result
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util
from test_contract import RADIO, fire, make_config, problems, state_document
from test_overview import health
from visibility import household_viewer

from meshcore_home_gateway import dashboard
from meshcore_home_gateway.config_view import shown_settings, target_settings
from meshcore_home_gateway.core.commands import CommandDecision, Rejection
from meshcore_home_gateway.core.contacts import Contact
from meshcore_home_gateway.core.mesh import Node
from meshcore_home_gateway.core.model import InboundMessage, MessageKind, Position
from meshcore_home_gateway.mqtt.payloads import received_payload
from meshcore_home_gateway.views import config_document, contact_event, iso
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, Broker
from test_dashboard import (
    BOB,
    CONFIGURATION,
    NONE,
    PENDING,
    ZED,
    card,
    cards,
    contacts_of,
    deliver,
    documented,
    gateway,
    household,
    load,
    made,
    page,
    render,
    shown_by_content,
    shows_nothing,
)

HEALTH = "set ns = namespace(waiting=0, look=[])"
FACTS = "set transport = cfg.get('transport')"
PROFILE = "set rank = {'deny': 0, 'contacts': 1, 'all': 2}"
LATEST = "macro setting(v)"
WHO = "Why can't I change this here?"  # the first card of Settings, by its help
HINT = "set listed"
WHERE = "Why someone is missing or not current"  # Where everyone is, by its help
HOME = Position(52.37, 9.73)
# what a card that shows the gateway's reports says while it is offline: Home Assistant drops
# the attributes of an unavailable entity, so its values would read as never reported
GONE = "<div>The gateway is offline: this shows again when it is back.</div>"


def row(output: str, label: str) -> str:
    """The cell after a label of a table."""
    return output.split(f"<td>{label}</td><td>", 1)[1].split("</td>", 1)[0]


def queued(seconds: float, step: str = "queued") -> dict[str, Any]:
    """A message on its way, accepted `seconds` ago."""
    return {
        "id": f"p{seconds:g}",
        "queued_at": iso(time.time() - seconds),
        "target": "alerts",
        "kind": "channel",
        "contact_key": None,
        "channel_index": 1,
        "name": "alerts",
        "text": "Gate open",
        "step": step,
        "attempt": int(step == "sent"),
        "max_attempts": 3,
    }


# --- Health ------------------------------------------------------------------------------------


async def test_health_names_the_parts_that_need_a_look_and_counts_as_the_status_line(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G-1, contract item 8 (HC 13): every part with its state; "All parts working." or the
    parts that need a look, by their names; "Messages on their way" counts every pending
    message and those queued over a minute ago, as the status line does, none with a gateway
    before 2.1; airtime left of the budget; offline, a line says so."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    health_card = card(cards(loaded, "gateway"), HEALTH)
    await fire(hass, topics.state, health() | {"radio_clock_offset_s": 0})
    documented(hass, PENDING, [])
    said = render(hass, health_card)
    assert said.startswith("<div>All parts working.</div>\n<table><tr><th>Part</th>")
    assert row(said, "Radio link") == "connected"
    assert row(said, "Message history") == "working"
    assert row(said, "Free disk") == "5120 MB"
    assert row(said, "Radio clock") == "on time"
    assert row(said, "Messages on their way") == "0 (0 waiting over a minute)"
    assert row(said, "Airtime left this hour") == "9 of 36 s"
    documented(hass, PENDING, [queued(90), queued(10), queued(120, "sent")])
    assert row(render(hass, health_card), "Messages on their way") == "3 (1 waiting over a minute)"
    status = render(hass, card(cards(loaded, "overview"), "macro problem(v)"))
    assert "Messages waiting over a minute: 1." in status
    await fire(hass, topics.state, health(errors_1h=2, disk_free_mb=50) | {"version": "x"})
    said = render(hass, health_card)
    assert said.startswith("<div>2 parts need a look: Program errors, last hour, Free disk.</div>")
    assert row(said, "Program errors, last hour") == "2"
    await fire(hass, topics.state, health(history="broken"))
    assert render(hass, health_card).startswith("<div>One part needs a look: Message history.")
    hass.states.async_remove(PENDING)
    assert row(render(hass, health_card), "Messages on their way") == NONE
    await fire(hass, topics.availability, "offline")
    said = render(hass, health_card)
    assert said.startswith("<div>The gateway is offline: its parts report again when it is back.")
    assert row(said, "Radio link") == "gateway offline"
    assert problems(caplog) == []


# --- Radio and Radio settings -------------------------------------------------------------------


async def test_the_radio_shows_its_facts_and_how_it_is_connected(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G-2: the radio's name, model, firmware and frequency from the state document, how the
    gateway reaches it from the configuration (G6); no tile of the airtime (D-65)."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    facts = card(cards(loaded, "gateway"), FACTS)
    said = render(hass, facts)
    assert row(said, "Name") == "Gateway"
    assert row(said, "Model") == "Heltec V3"
    assert row(said, "Frequency") == "869.618 MHz"
    assert row(said, "Coding rate") == "4/8"
    assert row(said, "Connected by") == NONE  # no configuration known yet
    document = config_document(time.time(), [], shown_settings(config))
    await fire(hass, topics.config, document, retain=True)
    assert row(render(hass, facts), "Connected by") == "network"
    gateway_page = page(loaded, "gateway")
    assert "tile" not in [c["type"] for s in gateway_page["sections"] for c in s["cards"]]
    await fire(hass, topics.availability, "offline")
    assert render(hass, facts).startswith(f"{GONE}\n<details>")  # no row of dashes
    assert problems(caplog) == []


def profile(live: dict[str, str] | None, desired: dict[str, str] | None) -> dict[str, Any]:
    """The settings document of the managed profile (docs/mqtt-api.md, Radio profile)."""
    settings = {
        "manual_add": {"live": True, "desired": True, "state": "written"},
        "autoadd": {"live": [], "desired": [], "state": "ok"},
        "telemetry": {"live": live, "desired": desired, "state": "differs"},
        "advert_location": {"live": "none", "desired": "none", "state": "ok"},
        "duty_cycle_percent": {"live": 10.0, "desired": None, "state": "unmanaged"},
        "name": {"live": "Kitchen radio", "desired": "Gateway", "state": "drift"},
    }
    return {
        "checked_at": iso(time.time()),
        "status": "drift",
        "mode": "auto",
        "observing": False,
        "firmware_code": 9,
        "firmware_tested": True,
        "settings": settings,
        "own_position": False,
        "protected_contacts": {
            "added": [],
            "favoured": [],
            "missing": [],
            "failed": [],
            "not_favourite": [],
        },
    }


async def test_who_may_ask_the_radio_is_shown_as_the_radio_acts(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 7, D-58: the battery row is the base permission, the position and readings
    rows are the base cut by location and environment: base deny with location all reads
    "never" for the position; base all with location contacts "contacts you allowed". A code
    added later stays a code, a value not reported none; each setting's state in words."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    settings = card(cards(loaded, "gateway"), PROFILE)
    assert render(hass, settings).startswith(
        "<div>The gateway has not reported the radio's settings yet.</div>"
    )
    live = {"base": "deny", "location": "all", "environment": "contacts"}
    desired = {"base": "all", "location": "contacts", "environment": "unknown"}
    await fire(hass, topics.settings, profile(live, desired), retain=True)
    said = render(hass, settings)
    assert said.startswith("<div>Mode: managed. State: changed by someone.</div>")
    battery = "<td>Who may ask this radio for its battery</td>"
    assert f"{battery}<td>never</td><td>every contact</td><td>differs: only watched</td>" in said
    position = "<td>Who may ask this radio for its position</td>"
    assert f"{position}<td>never</td><td>contacts you allowed</td>" in said
    readings = "<td>Who may ask this radio for its readings</td>"
    assert f"{readings}<td>never</td><td><code>unknown</code></td>" in said
    assert (
        "<td>Radio name</td><td>Kitchen radio</td><td>Gateway</td><td>changed on the radio</td>"
        in said
    )
    assert f"<td>Duty cycle</td><td>10</td><td>{NONE}</td><td>left as found</td>" in said  # #145
    assert "<td>Contacts added only by hand</td><td>yes</td><td>yes</td><td>written</td>" in said
    await fire(hass, topics.settings, profile(None, None))
    assert f"{position}<td>{NONE}</td><td>{NONE}</td>" in render(hass, settings)
    document = profile(live, desired)  # a duty cycle of 1 % is a number, never "yes"
    document["settings"]["duty_cycle_percent"] = {"live": 1.0, "desired": 0, "state": "drift"}
    advert = {"live": "share", "desired": "none", "state": "drift"}
    document["settings"]["advert_location"] = advert
    await fire(hass, topics.settings, document)
    said = render(hass, settings)
    assert "<td>Duty cycle</td><td>1</td><td>0</td><td>changed on the radio</td>" in said
    adverts = "<td>Position in the radio's adverts</td>"
    assert f"{adverts}<td>yes</td><td>no</td><td>changed on the radio</td>" in said
    document["settings"]["advert_location"] = advert | {"live": "later"}  # a code added later
    await fire(hass, topics.settings, document)
    assert f"{adverts}<td><code>later</code></td><td>no</td>" in render(hass, settings)
    await fire(hass, topics.availability, "offline")  # reported before, not "not yet"
    assert render(hass, settings).startswith(f"{GONE}\n<details>")
    assert problems(caplog) == []


# --- Latest of each kind ------------------------------------------------------------------------


async def test_latest_of_each_kind_shows_the_newest_event_of_each(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G-4, owner Q3: the newest contact, telemetry, setting, node and command event, each in
    words with its time; "none yet" before one."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    latest = card(cards(loaded, "gateway"), LATEST)
    said = render(hass, latest)
    for kind in ("Contact", "Position and battery", "Radio settings", "New node", "Command"):
        assert row(said, kind) == "none yet", kind
    await fire(hass, topics.contact_event, contact_event("added", ALICE, ALICE_NODE))
    unanswered = {"event_type": "unanswered", "key": BOB, "prefix": BOB[:12], "name": "Bob"}
    await fire(hass, topics.telemetry_event, unanswered | {"state": "away"})
    setting = {"event_type": "drift", "setting": "name", "reason": None}
    await fire(hass, topics.setting_event, setting | {"live": "x", "desired": "y"})
    node = {"event_type": "new", "key": ZED, "prefix": ZED[:12], "name": None, "type": "chat"}
    await fire(hass, topics.node_event, node)
    command = {"event_type": "arm", "args": "now", "sender_key": BOB, "sender_name": "Bob"}
    await fire(hass, topics.command, command | {"sent_at": iso(time.time())})
    said = render(hass, latest)
    assert row(said, "Contact").startswith("<b>Alice</b> added to the radio · ")
    assert row(said, "Position and battery").startswith("<b>Bob</b> stopped answering · ")
    assert row(said, "Radio settings").startswith("Radio name: changed on the radio · ")
    assert row(said, "New node").startswith(f"<b>{ZED[:8]}</b> heard for the first time · ")
    assert row(said, "Command").startswith("Bob: arm · ")
    await fire(hass, topics.availability, "offline")  # no "none yet" for what happened
    assert render(hass, latest).startswith(f"{GONE}\n<details>")
    assert problems(caplog) == []


def refused(word: str, reason: str, sender: Contact | None = None) -> dict[str, Any]:
    """A command the gateway refused, as `received` carries it (docs/mqtt-api.md, Commands)."""
    message = InboundMessage(
        MessageKind.CONTACT, f"{word} now", 1_760_000_000, time.time(), sender_prefix=BOB[:12]
    )
    decision = CommandDecision(word, rejection=Rejection(reason) if reason in REASONS else None)
    payload = received_payload(message, sender, None, decision)
    return payload | {"command": payload["command"] | {"reason": reason}}


REASONS = {reason.value for reason in Rejection}


async def test_latest_of_each_kind_shows_a_refused_command_with_its_reason_in_words(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The newest command event of either kind: one the gateway refused, which only `received`
    carries (command.accepted false), while it came after the newest accepted one, with the
    reason in words, also code_locked (#79); a reason added later stays a code. A message that
    is no command, and a command accepted afterwards, leave the row to the accepted one."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    latest = card(cards(loaded, "gateway"), LATEST)
    bob = Contact(BOB, "Bob")
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.received, refused("arm", "code_locked", bob))
    assert row(render(hass, latest), "Command").startswith(
        "Bob: arm, refused: too many wrong codes · "
    )
    command = {"event_type": "arm", "args": "now", "sender_key": BOB, "sender_name": "Bob"}
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.command, command | {"sent_at": iso(time.time())})
    assert row(render(hass, latest), "Command").startswith("Bob: arm · ")
    for reason, words in (
        ("code_invalid", "wrong code"),
        ("code_missing", "no code"),
        ("code_reused", "code used already"),
        ("replay", "an old command again"),
        ("sender_not_allowed", "sender not allowed"),
        ("code_unheard_of", "<code>code_unheard_of</code>"),
    ):
        freezer.tick(timedelta(seconds=1))
        await fire(hass, topics.received, refused("status", reason, bob))
        assert row(render(hass, latest), "Command").startswith(
            f"Bob: status, refused: {words} · "
        ), reason
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.received, refused("status", "sender_unknown"))  # no contact
    assert row(render(hass, latest), "Command").startswith(
        f"{BOB[:8]}: status, refused: sender not a contact · "
    )
    message = InboundMessage(MessageKind.CONTACT, "Hello", 1_760_000_000, time.time())
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.received, received_payload(message, bob, None))
    assert row(render(hass, latest), "Command").startswith("Bob: arm · ")
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.received, refused("status", "too_old", bob))
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.command, command | {"sent_at": iso(time.time())})
    assert row(render(hass, latest), "Command").startswith("Bob: arm · ")
    accepted = refused("arm", "", bob)  # `received` of an accepted one: no refusal
    freezer.tick(timedelta(seconds=1))
    await fire(hass, topics.received, accepted | {"command": {"word": "arm", "accepted": True}})
    assert row(render(hass, latest), "Command").startswith("Bob: arm · ")
    assert problems(caplog) == []


# --- Settings (contract item 14) -----------------------------------------------------------------


async def test_settings_read_the_configuration_the_gateway_announces(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 14, G6, owner Q2: the configuration document through the gateway's own
    discovery fills each card, the key of each setting in code: the chats with their kind and
    priority, the delivery code, who is asked, the contacts' controls, how the radio is reached,
    the managed profile in words (radio.profile.manage), whether commands need codes; the
    versions always."""
    more = '[messages]\nalert_targets = ["operator"]\nconfirm_nonce = true\n'
    more += '[radio.profile]\nmanage = "observe"\n'
    config = make_config(
        '[targets.alerts]\nchannel = 1\n[targets.operator]\ncontact = "' + ALICE + '"\n' + more
    )
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    document = config_document(time.time(), target_settings(config, RADIO), shown_settings(config))
    await fire(hass, topics.config, document, retain=True)
    view = cards(loaded, "settings")
    said = "\n".join(render(hass, c) for c in view if c["type"] == "markdown")
    assert '<div><ha-alert alert-type="info"><b>Set by your administrator.</b>' in said
    chats = row(said, "Chats")  # by their titles, as Chats names them (D-7), the alias in code
    assert chats == (
        "<b>Alerts</b> <code>alerts</code> (channel 1)<br>"
        "<b>Operator</b> <code>operator</code> (person) · sent first"
    )
    assert row(said, "Delivery code") == "on"
    assert row(said, "Position and battery asked from") == "only people turned on<br>persons"
    assert row(said, "Contact controls in Home Assistant") == "on"
    assert row(said, "Advert button in Home Assistant") == "on"
    assert "<code>[homeassistant]</code> <code>radio_controls</code>" in said
    assert row(said, "Connected by") == "network"
    open_link = (
        '<div><ha-alert alert-type="warning">The radio\'s network link has no password: anyone '
        "who reaches its port can control the radio."
    )
    assert open_link in said
    assert row(said, "Radio settings managed") == "only watched"
    assert row(said, "Command senders") == "1"
    assert row(said, "Command codes") == "off"
    assert "<code>[radio.profile]</code> <code>manage</code>" in said
    assert "<code>[commands]</code> <code>codes_file</code>" in said
    assert row(said, "This dashboard") == dashboard.__version__
    codes = document | {"commands": {"senders": 1, "codes": True, "reply": "none"}}
    await fire(
        hass, topics.config, codes | {"radio": document["radio"] | {"profile": {"manage": "auto"}}}
    )
    said = "\n".join(render(hass, c) for c in view if c["type"] == "markdown")
    assert row(said, "Command codes") == "on"
    assert row(said, "Radio settings managed") == (
        "managed on tested firmware, only watched on other firmware"
    )
    assert problems(caplog) == []


@pytest.mark.parametrize(
    ("homeassistant", "shown"),
    [
        ({"controls": True, "radio_controls": True}, "on"),
        ({"controls": True, "radio_controls": False}, "off"),
        ({"controls": False, "radio_controls": True}, "off"),
        ({"controls": True}, NONE),  # a gateway before 2.2 has no such key
    ],
)
async def test_settings_say_whether_the_advert_button_can_be_pressed(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    homeassistant: dict[str, bool],
    shown: str,
) -> None:
    """ADR 0033: off with `radio_controls` or `controls` off, as the button is unavailable
    then; on with both; a dash for a gateway whose document has no `radio_controls`."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    document = config_document(time.time(), target_settings(config, RADIO), shown_settings(config))
    document["homeassistant"] = {
        k: v
        for k, v in document["homeassistant"].items()
        if k not in ("controls", "radio_controls")
    } | homeassistant
    await fire(hass, topics.config, document, retain=True)
    said = "\n".join(render(hass, c) for c in cards(loaded, "settings") if c["type"] == "markdown")
    assert row(said, "Advert button in Home Assistant") == shown
    assert problems(caplog) == []


@pytest.mark.parametrize(
    ("types", "kinds"),
    [
        ('["chat"]', "persons"),
        ('["sensor"]', "sensors"),
        ('["chat", "sensor"]', "persons and sensors"),
    ],
)
async def test_settings_say_which_kinds_of_node_are_asked(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    types: str,
    kinds: str,
) -> None:
    """Settings, Privacy: which kinds of node are asked for their position and battery, as the
    configuration names them ([telemetry] ask_types)."""
    config = make_config(f"[telemetry]\nask_types = {types}\n")
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    document = config_document(time.time(), target_settings(config, RADIO), shown_settings(config))
    await fire(hass, topics.config, document, retain=True)
    said = "\n".join(render(hass, c) for c in cards(loaded, "settings") if c["type"] == "markdown")
    assert row(said, "Position and battery asked from") == f"only people turned on<br>{kinds}"
    assert problems(caplog) == []


async def test_settings_without_the_configuration_say_what_is_missing(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 14, HC 8: without the gateway, its first card says so; a gateway before 2.1
    reports no settings: the card says so and what the dashboard can tell (the contacts'
    controls, the chats); the tables are empty; offline, a red line; the versions always."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    view = cards(loaded, "settings")
    first = card(view, WHO)
    tables = [c for c in view if shown_by_content(c)]
    assert len(tables) == 4
    assert render(hass, first).startswith(
        "<div>The gateway is not found: see the first line of Overview.</div>"
    )
    topics = await gateway(hass, config)
    viewer = await household_viewer(hass, hass_ws_client)
    settings = page(loaded, "settings")
    sections = [s for s in settings["sections"] if any(c in tables for c in s["cards"])]
    hass.states.async_remove(CONFIGURATION)
    said = render(hass, first)
    assert said.split("\n")[:2] == [
        (
            '<div><ha-alert alert-type="warning">This gateway does not report its settings: '
            "that needs version 2.1 or later. Ask your administrator to update the gateway."
            "</ha-alert></div>"
        ),
        "<div>Contact controls in Home Assistant: on. Chats: Alerts, My target, Operator.</div>",
    ]
    for table in tables:
        shows_nothing(hass, table)
    # no heading without its table: the section hides (D-53's section rule)
    assert [await viewer.shown(settings, s) for s in sections] == [False] * 4
    versions = render(hass, card(view, "Why versions matter"))
    assert row(versions, "Gateway") == state_document(known=True)["version"]
    await fire(hass, topics.config, config_document(time.time(), [], shown_settings(config)))
    assert [await viewer.shown(settings, s) for s in sections] == [True] * 4
    await fire(hass, topics.availability, "offline")
    assert render(hass, first).startswith(
        '<div><ha-alert alert-type="error">The gateway is offline. Its settings show again'
    )
    versions = render(hass, card(view, "Why versions matter"))
    assert versions.startswith(f"{GONE}\n<table>")  # not a dash for the gateway's versions
    assert row(versions, "This dashboard") == dashboard.__version__
    assert "<td>Gateway</td>" not in versions
    assert [await viewer.shown(settings, s) for s in sections] == [False] * 4
    assert problems(caplog) == []


# --- the map's hints -----------------------------------------------------------------------------


async def test_the_map_names_another_gateways_node_under_this_gateways_name(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M2, D-33: radio-2's Zed is another node than radio1's Zed, which got the tracker's ID
    first: radio-2's map lists radio1's tracker, and a yellow hint says so and links to the
    guide; radio1's map shows its own Zed and no hint."""
    config, other = make_config(), make_config(node_id="radio-2")
    first, second = Broker(), Broker()
    zed = household(Node(ZED, "Zed", COMPANION, position=HOME))
    impostor = household(Node(BOB, "Zed", COMPANION, position=HOME))
    contacts_of(config, tmp_path / "radio1", first).update(zed)
    contacts_of(other, tmp_path / "radio-2", second).update(impostor)
    files = {
        "mesh-radio1": dashboard.dashboard(config, tmp_path / "radio1")[0],
        "mesh-radio-2": dashboard.dashboard(other, tmp_path / "radio-2")[0],
    }
    loaded = await load(hass, hass_ws_client, files)
    await gateway(hass, config)
    await deliver(hass, config, first, zed)
    await gateway(hass, other)
    await deliver(hass, other, second, impostor)
    assert hass.states.get("device_tracker.mesh_zed_2") is not None
    hint = render(hass, card(cards(loaded["mesh-radio-2"], "map"), HINT)).split("\n")
    assert hint[0] == (  # radio-2's own Zed has no place on its map
        '<div><ha-alert alert-type="info">Not on this map: Zed. Ask your administrator to make '
        "the dashboard again.</ha-alert></div>"
    )
    assert hint[1].startswith(  # for the household, then for whoever can fix it (D-30)
        '<div><ha-alert alert-type="warning"><b>This map shows Zed of another gateway under this '
        "gateway's names.</b><br>For your administrator: give each gateway a contact prefix of "
        'its own, then make this dashboard again. <a href="https://github.com/'
    )
    assert "#a-second-gateway" in hint[1]
    shows_nothing(hass, card(cards(loaded["mesh-radio1"], "map"), HINT))
    assert problems(caplog) == []


async def test_the_cards_that_read_the_registry_show_a_new_tracker_within_a_minute(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M2, M3, O3: the map's hint and Where everyone is, on the map and on Overview, find the
    trackers in the registry, which tells no listener. On a dashboard made without a tracker,
    each card, as the frontend subscribes to it, renders again within a minute after Alice and
    Bob got theirs, and names them."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)  # no tracker on its map
    await gateway(hass, config)
    watched = {
        "hint": card(cards(loaded, "map"), HINT),
        "map": card(cards(loaded, "map"), WHERE),
        "overview": card(cards(loaded, "overview"), WHERE),
    }
    fired = dict.fromkeys(watched, 0)

    def counting(name: str) -> Any:
        @callback
        def changed(event: Any, updates: Any) -> None:
            fired[name] += 1

        return changed

    subscriptions = [
        async_track_template_result(
            hass,
            [TrackTemplate(Template(found["content"], hass), {"config": found, "user": "Bob"})],
            counting(name),
        )
        for name, found in watched.items()
    ]
    await hass.async_block_till_done()
    assert render(hass, watched["map"]).startswith("<div>Nobody shares a position yet.")
    broker, mesh = Broker(), household(ALICE_NODE, Node(BOB, "Bob", COMPANION, position=HOME))
    contacts_of(config, tmp_path, broker).update(mesh)
    await deliver(hass, config, broker, mesh)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1, seconds=1))
    await hass.async_block_till_done()
    for subscription in subscriptions:
        subscription.async_remove()
    assert all(fired.values()), fired
    assert render(hass, watched["hint"]).startswith(
        '<div><ha-alert alert-type="info">Not on this map: Alice, Bob.'
    )
    for name in ("map", "overview"):
        assert ">Bob</b>" in render(hass, watched[name]), name
    assert problems(caplog) == []


async def test_without_a_tracker_the_map_page_says_why_in_blue(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M1: no tracker when the dashboard was made: a blue note says why and who makes it again,
    no GitHub alert (rule 11); Where everyone is below lists everyone with a position now."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    note = page(loaded, "map")["sections"][0]["cards"][0]
    assert note["type"] == "markdown"
    assert render(hass, note).startswith(
        '<div><ha-alert alert-type="info">Nobody had a position when this dashboard was made. '
        "People show here once your administrator makes the dashboard again.</ha-alert></div>"
    )
    broker, mesh = Broker(), household(ALICE_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    where = render(hass, card(cards(loaded, "map"), WHERE))
    assert where.startswith('<table role="presentation" width="100%"><tr><td><b>Alice</b>')
    assert "./map" not in where  # the map's own copy has no link to itself
    assert problems(caplog) == []
