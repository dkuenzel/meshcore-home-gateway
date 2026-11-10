"""The generated dashboard's Overview against the real Home Assistant: the status line, the problem
cards, Latest chats and Where everyone is (docs/home-assistant.md, Dashboard).

The household, the dashboard and the helpers are those of test_dashboard.py: the gateway's own
discovery and its contacts' devices, the cards rendered strictly as the frontend asks for them, a
household user's browser for what shows. Latest chats reads the gateway's message history, a
sensor of version 2.1: its tests say so and fail with a gateway that lacks it.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from test_contract import PREFIX, TARGETS, fire, make_config, problems, state_document
from visibility import household_viewer

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.config_view import shown_settings
from meshcore_home_gateway.core.mesh import Node
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.views import config_document, iso
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, HILL_NODE, Broker
from test_dashboard import (
    BOB,
    CONFIGURATION,
    LANGUAGES,
    PENDING,
    card,
    cards,
    contacts_of,
    deliver,
    documented,
    form_findings,
    gateway,
    household,
    listening,
    made,
    markdown,
    render,
    rendered,
    said,
    shows_nothing,
    speaking,
)

# the cards of Overview, by a part of their content
STATUS, LATEST, WHERE = "macro problem(v)", "./messages", "Why someone is missing"
# a problem card's first line names its class (rule 9 of the template)
CLASS = re.compile(r"\{#- problem: (\w+) -#\}")
FIRST = re.compile(r'<div><ha-alert alert-type="(\w+)"><b>(.*?)</b>(?:<br>| )(.*)</ha-alert></div>')
ORDER = [
    "taken_over",
    "degraded",
    "look_alike",
    "remote_cli",
    "advert_position",
    "profile",
    "telemetry",
    "clock",
    "full",
    "version",
]
LOOKS = "a1a1a1a1a1a1" + "0f" * 26  # a node whose first 6 key bytes are Alice's
STRANGER = "c4" * 32  # a node like no contact: a protected key's, as far as the cards know
MAJOR, MINOR = (int(n) for n in __version__.split(".")[:2])


def problem_cards(loaded: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m.group(1): c for c in markdown(loaded) if (m := CLASS.match(c["content"]))}


def first(output: str) -> tuple[str, str, str]:
    """A card's coloured line: its alert type, its bold verdict and what follows."""
    match = FIRST.fullmatch(output.split("\n", maxsplit=1)[0])
    assert match, output
    return match.group(1), match.group(2), match.group(3)


def index(**more: Any) -> dict[str, Any]:
    """The contacts' index as the gateway publishes it (docs/mqtt-api.md, Contacts)."""
    empty: dict[str, Any] = {"total": 2, "max": 350, "manage": True, "manage_reason": None}
    for key in ("remote_cli", "advert_position", "advert_carries_position", "telemetry_excluded"):
        empty |= {key: [], f"{key}_total": 0}
    return empty | {"truncated": False, "contacts": []} | more


def health(**parts: Any) -> dict[str, Any]:
    """The state document with its health section; degraded when a part says so."""
    section = {
        "degraded": False,
        "history": "ok",
        "state_file": "ok",
        "exclusions": "ok",
        "errors_1h": 0,
        "loop_lag_max_s": 0.0,
        "events_dropped_1h": 0,
        "disk_free_mb": 5120,
    }
    return state_document(known=True) | {"queue": 0, "health": section | parts}


def usb() -> Config:
    """A gateway whose radio is on USB: no other client can take its link."""
    return parse_config(
        '[radio]\ntransport = "serial"\ndevice = "/dev/ttyUSB0"\n'
        f'[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\ninsecure = true\n{TARGETS}'
    )


async def household_of(
    hass: HomeAssistant, hass_ws_client: Any, config: Config, tmp_path: Path, *nodes: Node
) -> tuple[dict[str, Any], Topics]:
    """The dashboard of a household of these contacts, loaded; the gateway online with them."""
    broker, mesh = Broker(), household(*nodes)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    await fire(hass, topics.state, health(), retain=True)
    return loaded, topics


# --- the status line ----------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_status_line_says_how_the_gateway_is(  # noqa: PLR0915 - situation by situation
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    language: str,
) -> None:
    """Red, blue, yellow or green, the verdict on a line of its own and an action after it: not
    found, offline, the radio disconnected, taken over, no report yet, the problems by name,
    all fine. Every card renders strictly in each situation and listens to no domain."""
    config = speaking(language, monkeypatch)
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    status = card(loaded, STATUS)
    seen: dict[str, dict[str, str]] = {}
    tell = said(config, "If this lasts more than a few minutes, tell your administrator.")

    async def situation(name: str, kind: str, msgid: str, **values: str) -> str:
        seen[name] = rendered(hass, loaded)
        assert listening(hass, loaded) == [], name
        output = render(hass, status)
        assert first(output)[:2] == (kind, said(config, msgid, **values)), name
        return first(output)[2]

    after = await situation("no gateway", "error", "Gateway not found.")
    assert after.startswith(
        said(config, "This dashboard cannot reach the gateway. Tell your administrator.")
    )
    admin = (
        "For your administrator: Home Assistant has no entity of node {node}. Check MQTT "
        "discovery, or make this dashboard for the right node."
    )
    assert after.endswith(f"<small>{said(config, admin, node='radio1')}</small>")
    topics = await gateway(hass, config, online=False)
    assert (await situation("not online yet", "error", "Gateway offline.")).endswith(tell)
    await fire(hass, topics.availability, "offline", retain=True)
    assert (await situation("offline", "error", "Gateway offline.")).endswith(tell)
    await fire(hass, topics.availability, "online")
    assert (await situation("no report yet", "info", "No report yet.")).endswith(tell)
    await fire(hass, topics.state, state_document(known=False), retain=True)
    assert (await situation("radio disconnected", "error", "Radio disconnected.")).endswith(tell)
    await fire(hass, topics.state, health())
    await situation("all fine", "success", "All fine.")
    gateway_, settings, help_ = (said(config, text) for text in ("Gateway", "Settings", "Help"))
    lines = render(hass, status).split("\n")
    assert len(lines) == 3
    assert lines[1] == (  # the way to the pages without a tab, outside the fold
        f'<div><a href="./gateway">{gateway_}</a> · <a href="./settings">{settings}</a> · '
        f'<a href="./help">{help_}</a></div>'
    )
    assert lines[2].startswith("<details><summary>")
    # the queue sensor counts the messages behind the one being sent: never the status (M22)
    await fire(hass, topics.state, health() | {"queue": 3})
    await situation("queue sensor at 3", "success", "All fine.")
    await deliver(hass, config, broker, mesh)
    await fire(hass, topics.nodes, {"total": 1, "look_alike": [STRANGER], "look_alike_total": 1})
    one = said(config, "a node imitates a contact")
    await situation("one problem", "warning", "Needs a look: {first}.", first=one)
    await fire(hass, topics.contacts, index(remote_cli=[BOB], remote_cli_total=1), retain=True)
    await fire(hass, topics.state, health(degraded=True, errors_1h=2))
    fault = said(config, "a fault in the gateway")
    await situation(
        "three problems", "warning", "Needs a look: {first} and {more} more.", first=fault, more="2"
    )
    # red before yellow (M7): another client took the radio's link
    await fire(hass, topics.state, health(degraded=True) | {"radio_link_contested": True})
    after = await situation("taken over", "error", "Radio taken over by another program.")
    assert after == said(config, "Act now: see the red card below.")
    await fire(hass, topics.state, health())
    await fire(hass, topics.nodes, {"total": 0, "look_alike": [], "look_alike_total": 0})
    await fire(hass, topics.contacts, index())
    starts = health() | {"telemetry": "paused", "telemetry_reason": "first_start"}
    await fire(hass, topics.state, starts)
    after = await situation("a blue note", "success", "All fine.")
    assert after.endswith(said(config, "One note below."))
    for name, outputs in seen.items():
        assert form_findings(outputs) == [], name
    assert problems(caplog) == []


async def test_messages_waiting_over_a_minute_are_counted_from_the_messages_on_their_way(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Blue for a message queued over a minute ago, also while the queue sensor reads 0 (the
    stuck message is the one being sent, #112); not for one sent and waiting for its
    confirmation as long (M22)."""
    config = make_config()
    loaded, _ = await household_of(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    assert hass.states.get(f"sensor.{PREFIX}_queue").state == "0"
    status = card(loaded, STATUS)
    old = iso(time.time() - 90)
    entry = {
        "id": "1a2b3c4d",
        "queued_at": old,
        "target": "alerts",
        "kind": "channel",
        "contact_key": None,
        "channel_index": 1,
        "name": "alerts",
        "text": "Gate open",
        "step": "sent",
        "attempt": 1,
        "max_attempts": 1,
    }
    documented(hass, PENDING, [entry])
    assert first(render(hass, status))[:2] == ("success", "All fine.")
    documented(hass, PENDING, [entry | {"step": "queued"}])
    waiting = "Messages waiting over a minute: {waiting}."
    assert first(render(hass, status))[:2] == ("info", said(config, waiting, waiting="1"))
    young = entry | {"step": "queued", "queued_at": iso(time.time() - 10)}
    documented(hass, PENDING, [young])
    assert first(render(hass, status))[:2] == ("success", "All fine.")
    documented(hass, PENDING, [entry | {"step": "queued"}, entry | {"id": "2b", "step": "queued"}])
    assert first(render(hass, status))[:2] == ("info", said(config, waiting, waiting="2"))
    assert problems(caplog) == []


# --- the problem cards --------------------------------------------------------------------------


async def test_each_problem_card_shows_while_its_problem_exists(  # noqa: PLR0915 - card by card
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One card per problem, in the status line's order, each empty (and hidden) while its
    problem does not exist and coloured while it does; a red card for a takeover (M8)."""
    config = make_config()
    loaded, topics = await household_of(
        hass, hass_ws_client, config, tmp_path, ALICE_NODE, Node(BOB, "Bob", COMPANION)
    )
    found = problem_cards(loaded)
    assert list(found) == ORDER
    viewer = await household_viewer(hass, hass_ws_client)
    overview = loaded["views"][0]
    for each in found.values():
        shows_nothing(hass, each)
        assert not await viewer.shown(overview, each)

    async def shown(name: str) -> tuple[str, str, str]:
        """The card's line, the only one of the cards that shows."""
        assert [n for n, c in found.items() if render(hass, c)] == [name]
        assert await viewer.shown(overview, found[name])
        lines = render(hass, found[name]).split("\n")
        assert lines[-1].startswith("<details><summary>")
        return first(render(hass, found[name]))

    await fire(hass, topics.state, health() | {"radio_link_contested": True})
    kind, verdict, _ = await shown("taken_over")
    assert kind == "error"
    assert verdict.startswith("Another program took over the radio's link, since ")
    await fire(hass, topics.state, health(degraded=True, history="broken", disk_free_mb=50))
    kind, verdict, after = await shown("degraded")
    parts = "message history broken, disk almost full"
    assert (kind, verdict) == ("warning", f"The gateway runs with a fault: {parts}.")
    assert after.endswith('<br><a href="./gateway">Gateway: Health</a>')
    await fire(hass, topics.state, health())
    await fire(hass, topics.nodes, {"total": 1, "look_alike": [STRANGER], "look_alike_total": 1})
    assert await shown("look_alike") == (
        "warning",
        "A node nearby imitates a contact: c4c4c4c4.",
        "Do not trust a message only because its name looks known.",
    )
    await fire(hass, topics.nodes, {"total": 0, "look_alike": [], "look_alike_total": 0})
    await fire(hass, topics.contacts, index(remote_cli=[BOB], remote_cli_total=1), retain=True)
    assert (await shown("remote_cli"))[1] == "Bob may control this home's radio over the air."
    many = [BOB, ALICE, STRANGER, "d6" * 32]
    await fire(hass, topics.contacts, index(advert_carries_position=many))
    await fire(
        hass, topics.contacts, index(advert_carries_position=many, advert_carries_position_total=5)
    )
    assert (await shown("advert_position"))[1] == (
        "Bob, Alice, c4c4c4c4 and 2 more broadcast their position to everyone in range."
    )
    await fire(hass, topics.contacts, index())
    await fire(hass, topics.settings, {"status": "drift"}, retain=True)
    kind, verdict, after = await shown("profile")
    assert (kind, verdict) == ("warning", "Someone changed the radio's settings.")
    assert after.endswith('<a href="./gateway">Gateway: Radio settings</a>')
    await fire(hass, topics.settings, {"status": "ok"})
    for reason, kind, verdict in (
        (
            "auto_add",
            "warning",
            "Asking for position and battery is paused: the radio adds contacts by itself.",
        ),
        ("first_start", "info", "Asking for position and battery starts within the hour."),
        (
            "radio_airtime",
            "info",
            "Asking for position and battery is paused: the radio sent a lot in the last hour.",
        ),
        ("a_reason_of_later", "warning", "Asking for position and battery is paused."),
    ):
        await fire(
            hass, topics.state, health() | {"telemetry": "paused", "telemetry_reason": reason}
        )
        assert (await shown("telemetry"))[:2] == (kind, verdict), reason
    assert "<code>a_reason_of_later</code>" in render(hass, found["telemetry"])
    await fire(
        hass, topics.state, health() | {"telemetry": "paused", "telemetry_reason": "no_radio"}
    )
    shows_nothing(hass, found["telemetry"])  # the status line says the radio is away
    for offset, verdict in (
        (900, "The radio's clock is 15 min ahead."),
        (-3 * 3600 - 60, "The radio's clock is 3 h behind."),
        (4 * 86400, "The radio's clock is 4 days ahead."),
    ):
        await fire(hass, topics.state, health() | {"radio_clock_offset_s": offset})
        assert (await shown("clock"))[1] == verdict, offset
    await fire(hass, topics.state, health() | {"radio_clock_offset_s": 299})
    shows_nothing(hass, found["clock"])
    await fire(hass, topics.contacts, index(total=350))
    kind, verdict, after = await shown("full")
    assert verdict == "The contact table is full: 350 of 350."
    assert after.endswith('<a href="./contacts">Contacts</a>')
    await fire(hass, topics.contacts, index())
    assert [n for n, c in found.items() if render(hass, c)] == []
    assert problems(caplog) == []


async def test_the_clock_card_says_the_gateway_leaves_the_clock_while_set_clock_is_off(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway reports the radio's clock offset whatever `[radio] set_clock` says, and with
    it off neither moves the clock nor restarts the radio (radio/session.py): the card says so
    from the configuration document, naming the key; with it on, and while the gateway does not
    say, how the gateway keeps the clock."""
    config = make_config()
    loaded, topics = await household_of(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    clock = problem_cards(loaded)["clock"]
    await fire(hass, topics.state, health() | {"radio_clock_offset_s": 900})
    keeps = render(hass, clock)
    assert first(keeps)[1] == "The radio's clock is 15 min ahead."
    assert "moves the clock forward" in keeps
    settings = shown_settings(config)
    for set_clock in (False, True):
        radio = settings["radio"] | {"set_clock": set_clock}
        document = config_document(time.time(), [], settings | {"radio": radio})
        await fire(hass, topics.config, document, retain=not set_clock)  # only the first (fire)
        said = render(hass, clock)
        assert first(said)[1] == "The radio's clock is 15 min ahead.", set_clock
        leaves = "The gateway does not set this radio's clock" in said
        assert leaves == (not set_clock), said
        assert ("<code>[radio]</code> <code>set_clock</code>" in said) == leaves
        assert ("moves the clock forward" in said) == set_clock
    assert problems(caplog) == []


async def test_a_look_alike_is_named_after_the_contact_it_imitates(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A heard node whose first 12 key digits are Alice's imitates Alice: the card says her name,
    which the household knows, and its first 8 digits for a node that resembles no contact of
    this gateway, such as a protected key (M60)."""
    config = make_config()
    loaded, topics = await household_of(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    look = problem_cards(loaded)["look_alike"]
    await fire(
        hass, topics.nodes, {"total": 2, "look_alike": [LOOKS, STRANGER], "look_alike_total": 2}
    )
    verdict = first(render(hass, look))[1]
    assert verdict == "A node nearby imitates a contact: Alice, c4c4c4c4."
    assert ALICE[:8] not in verdict
    assert problems(caplog) == []


@pytest.mark.parametrize(
    ("running", "says"),
    [
        (f"{MAJOR}.{MINOR}.{9}", None),  # a patch release of the same minor version: nothing
        (f"{MAJOR}.{MINOR}.0rc1", None),
        (f"{MAJOR - 1}.9.9", "Ask your administrator to update the gateway to {version}."),
        (f"{MAJOR}.{MINOR + 1}.0", "Ask your administrator to make the dashboard again."),
        (f"{MAJOR + 8}.0.0", "Ask your administrator to make the dashboard again."),  # 10 > 2
        ("dev", "Ask your administrator to make the dashboard again."),
    ],
)
async def test_the_version_card_shows_for_another_major_or_minor_version(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    running: str,
    says: str | None,
) -> None:
    """A gateway of another major or minor version than the dashboard's: what to update, by
    which is older, its numbers compared as numbers; a patch release says nothing (M48); a
    version that does not start with two numbers asks for the dashboard again."""
    config = make_config()
    loaded, topics = await household_of(hass, hass_ws_client, config, tmp_path)
    version = problem_cards(loaded)["version"]
    await fire(hass, topics.state, health() | {"version": running})
    status = first(render(hass, card(loaded, STATUS)))
    if says is None:
        shows_nothing(hass, version)
        assert status[:2] == ("success", "All fine.")
        return
    kind, verdict, after = first(render(hass, version))
    made_for = f"This dashboard was made for version {__version__}; the gateway runs {running}."
    assert (kind, verdict, after) == ("warning", made_for, says.format(version=__version__))
    other = "the dashboard and the gateway are of different versions"
    assert status[:2] == ("warning", f"Needs a look: {other}.")
    again = "<code>meshcore-home-gateway dashboard</code>" in render(hass, version)
    assert again == ("make the dashboard again" in says)
    assert problems(caplog) == []


@pytest.mark.parametrize("radio", ["tcp", "usb"])
async def test_a_takeover_turns_the_status_red_over_tcp_and_never_over_usb(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    radio: str,
) -> None:
    """HA-03: over TCP the takeover is red in the status line and in its card; over USB no
    other client can take the link, and its card shows nothing."""
    config = make_config() if radio == "tcp" else usb()
    loaded, topics = await household_of(hass, hass_ws_client, config, tmp_path)
    await fire(hass, topics.state, health() | {"radio_link_contested": True})
    taken = problem_cards(loaded)["taken_over"]
    status = first(render(hass, card(loaded, STATUS)))
    if radio == "tcp":
        assert status[:2] == ("error", "Radio taken over by another program.")
        assert first(render(hass, taken))[0] == "error"
    else:
        assert status[:2] == ("success", "All fine.")
        shows_nothing(hass, taken)
    assert problems(caplog) == []


# --- Latest chats and Where everyone is ---------------------------------------------------------


async def test_where_everyone_is_lists_the_contacts_with_a_position(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each contact device of this gateway with a tracker, in words: at home first, then by the
    latest change; without one, what to do; and the way to the map."""
    config = make_config()
    empty, _ = await household_of(hass, hass_ws_client, config, tmp_path / "none")
    assert render(hass, card(cards(empty, "overview"), WHERE)).split("\n")[:2] == [
        (
            "<div>Nobody shares a position yet. Turn on Ask for position and battery for a "
            "person who agrees (Contacts).</div>"
        ),
        '<div><a href="./map">Open the map</a></div>',
    ]
    loaded, _ = await household_of(hass, hass_ws_client, config, tmp_path, ALICE_NODE, HILL_NODE)

    def rows() -> list[tuple[str, str]]:
        where = render(hass, card(cards(loaded, "overview"), WHERE))
        return re.findall(r"<tr><td><b>(.*?)</b></td><td>(.*?)</td><td", where)

    hass.states.async_set("device_tracker.mesh_hilltop", "not_home")
    hass.states.async_set("device_tracker.mesh_alice", "Work")  # another zone: its name
    assert rows() == [("Alice", "at Work"), ("Hilltop", "away")]  # the latest change first
    hass.states.async_set("device_tracker.mesh_hilltop", "home")
    assert rows() == [("Hilltop", "at home"), ("Alice", "at Work")]  # at home first
    hass.states.async_set("device_tracker.mesh_alice", "unavailable")
    assert rows()[1] == ("Alice", "position not current")
    hass.states.async_set("device_tracker.mesh_alice", "unknown")
    assert rows()[1] == ("Alice", "no position yet")
    assert re.search(
        r"<small>since \d\d:\d\d</small>", render(hass, card(cards(loaded, "overview"), WHERE))
    )
    assert problems(caplog) == []


async def test_where_everyone_is_names_the_administrator_when_asking_is_off(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With telemetry off in the gateway's configuration, no switch helps: the line names the
    administrator (HC 25). The configuration sensor's state as the gateway of 2.1 announces it,
    its document as the attributes (design G6)."""
    config = make_config()
    loaded, _ = await household_of(hass, hass_ws_client, config, tmp_path)
    hass.states.async_set(CONFIGURATION, iso(time.time()), {"telemetry": {"enabled": True}})
    assert render(hass, card(cards(loaded, "overview"), WHERE)).startswith(
        "<div>Nobody shares a position yet. Turn"
    )
    hass.states.async_set(CONFIGURATION, iso(time.time()), {"telemetry": {"enabled": False}})
    assert render(hass, card(cards(loaded, "overview"), WHERE)).startswith(
        "<div>Nobody shares a position yet. Asking for position and battery is off in this "
        "home's settings (your administrator).</div>"
    )
    assert problems(caplog) == []


async def test_latest_chats_need_a_gateway_of_2_1_and_name_whom_to_ask(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without the gateway the card points to the status line, and so does Where everyone is,
    since no switch helps then; with a gateway that has no message history (before 2.1) it says
    what the administrator does (HC 8)."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    latest = card(cards(loaded, "overview"), LATEST)
    for found in (latest, card(cards(loaded, "overview"), WHERE)):
        assert render(hass, found).startswith(
            "<div>The gateway is not found: see the first line of Overview.</div>"
        )
    await gateway(hass, config)
    hass.states.async_remove(f"sensor.{PREFIX}_message_history")  # a gateway of 2.0 has none
    assert render(hass, latest).startswith(
        "<div>Chats need gateway version 2.1 or later. Ask your administrator to update the "
        "gateway.</div>"
    )
    assert problems(caplog) == []
