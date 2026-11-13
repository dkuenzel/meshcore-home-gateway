"""The generated dashboard's chats against the real Home Assistant: a chat's page (its head, the
refused line, its thread, why writing is not possible, and its About), All chats and Latest
messages on Chats, and Latest chats on Overview (docs/home-assistant.md, Dashboard).

The household and the helpers are those of test_dashboard.py. The cards read the gateway's message
history and its messages on their way, sensors of version 2.1 (G1, G3): most tests here set their
states as the gateway announces them (documented()), so they test the cards' rules on their own;
the last tests feed the documents and the drafts through the gateway's own discovery.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from ha_plugin import CLIENT_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_contract import (
    CLEARS_PER_ENTITY,
    PREFIX,
    RADIO,
    TARGETS,
    fire,
    make_config,
    problems,
    state_document,
)
from visibility import household_viewer

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.config_view import shown_settings, target_settings
from meshcore_home_gateway.core.mesh import Node
from meshcore_home_gateway.core.model import DeliveryEvent, DeliveryState
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.payloads import delivery_payload
from meshcore_home_gateway.views import config_document, iso
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, Broker
from test_dashboard import (
    BOB,
    CONFIGURATION,
    HISTORY,
    NONE,
    PENDING,
    SHOWS_NOTHING,
    ZED,
    card,
    cards,
    contacts_of,
    deliver,
    documented,
    drafts,
    form_findings,
    gateway,
    household,
    made,
    page,
    refused_line,
    render,
    shows_nothing,
    topics_of,
)

ROW = re.compile(r'<table role="presentation" width="100%">(.*?)</table>')
LEFT = '<td width="85%" align="left">'
TWO = (
    '[targets.family]\nchannel = 1\n[targets.alerts]\nchannel = 1\n[targets.operator]\ncontact = "'
    + ALICE
    + '"\n[targets.my-target]\nchannel = 2\n'
)
CODE = " #1a2b3c4d"  # what confirm_nonce adds to a direct message


def ago(seconds: float) -> str:
    return iso(time.time() - seconds)


def clock(at: str) -> str:
    """The local clock time of a time of the documents, as the cards show it (D-9)."""
    parsed = dt_util.parse_datetime(at)
    assert parsed is not None, at
    return dt_util.as_local(parsed).strftime("%H:%M")


def received(seconds: float, text: str = "Hi", **more: Any) -> dict[str, Any]:
    """A message received `seconds` ago, on channel 1 unless `more` says otherwise
    (docs/mqtt-api.md, History: every key, null where it does not apply)."""
    entry: dict[str, Any] = {
        "at": ago(seconds),
        "direction": "in",
        "kind": "channel",
        "contact_key": None,
        "channel_index": 1,
        "name": "Bob",
        "text": text,
        "state": None,
        "hops": 1,
        "snr_db": 6.25,
        "target": "alerts",
        "id": None,
        "queued_at": None,
        "suspect": None,
        "reason": None,
        "nonce": None,
    }
    return entry | more


def to_alice(**more: Any) -> dict[str, Any]:
    """The keys of a direct message with Alice."""
    return {"kind": "contact", "contact_key": ALICE, "channel_index": None, "name": "Alice"} | more


def final(seconds: float, text: str = "Gate open", **more: Any) -> dict[str, Any]:
    """A message sent from this home, final `seconds` ago, accepted a second before; sent
    without confirm_nonce's code unless `nonce=True` (#142)."""
    entry = received(seconds, text) | {
        "direction": "out",
        "name": "alerts",
        "state": "sent",
        "hops": None,
        "snr_db": None,
        "id": f"m{seconds:g}",
        "queued_at": ago(seconds + 1),
        "suspect": False,
        "nonce": False,
    }
    return entry | more


def on_its_way(seconds: float, text: str = "Back soon", **more: Any) -> dict[str, Any]:
    """A message accepted `seconds` ago and not final (docs/mqtt-api.md, Messages on their way)."""
    entry: dict[str, Any] = {
        "id": f"p{seconds:g}",
        "queued_at": ago(seconds),
        "target": "alerts",
        "kind": "channel",
        "contact_key": None,
        "channel_index": 1,
        "name": "alerts",
        "text": text,
        "step": "queued",
        "attempt": 0,
        "max_attempts": 1,
    }
    return entry | more


def rows(output: str) -> list[tuple[str, str]]:
    """The open messages of a thread: each its side and its cell, oldest first."""
    shown = output.split("\n")
    line = next(line for line in shown if line.startswith("<div><table"))
    return [("left" if LEFT in r else "right", r) for r in ROW.findall(line)]


def texts(output: str) -> list[str]:
    """The texts of the open messages, as shown, without their tags."""
    return [re.sub(r"<[^>]+>", "", cell.split("<br>", 1)[1]) for _, cell in rows(output)]


def earlier(output: str) -> str | None:
    """The summary of the fold of earlier messages, if there is one."""
    match = re.search(r"<details><summary>(Earlier messages[^<]*)</summary>", output)
    return match.group(1) if match else None


async def chats_of(
    hass: HomeAssistant,
    hass_ws_client: Any,
    tmp_path: Path,
    config: Config | None = None,
    *,
    drafts: bool = True,
) -> dict[str, Any]:
    """The dashboard of a household of Alice (also the target operator) and Bob, loaded; the
    gateway online with them; without `drafts`, without the chats' fields of a gateway of 2.1,
    for a test that gives them itself."""
    config = config or make_config()
    broker, mesh = Broker(), household(ALICE_NODE, Node(BOB, "Bob", COMPANION))
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config, drafts=drafts)
    await deliver(hass, config, broker, mesh, drafts=drafts)
    return loaded


def thread(loaded: dict[str, Any], chat: str) -> dict[str, Any]:
    return card(cards(loaded, chat), "macro row(e)")


# --- a chat's thread ---------------------------------------------------------------------------


async def test_a_chat_shows_its_own_messages_oldest_first_on_two_sides(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A channel's page holds the messages of its channel, a person's those with the person's
    key; received ones on the left with the sender on a channel, sent ones on the right with
    their time and ✓ or ✓✓. The newest-first history comes out oldest first: a batch fetched
    at once keeps its order (#113), and a confirmation that came after a reply sorts by when
    the message was accepted (CP F-09). The thread stays while the gateway is offline."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alerts, alice = thread(loaded, "alerts"), thread(loaded, "alice")
    batch = ago(600)
    documented(
        hass,
        HISTORY,
        [
            received(25, "New phone", **to_alice(name="Alice (new phone)")),  # her key: hers
            received(28, "Not her", **to_alice(contact_key="b7" * 32)),  # another Alice
            received(30, "Got it", **to_alice()),
            final(20, "On my way", **to_alice(state="confirmed"), queued_at=ago(120)),
            received(300, "third", at=batch),
            received(300, "second", at=batch),
            received(300, "first", at=batch),
            final(400, "Elsewhere", channel_index=2, target="my-target", name="my-target"),
        ],
    )
    assert texts(render(hass, alerts)) == ["first", "second", "third"]
    assert [side for side, _ in rows(render(hass, alerts))] == ["left"] * 3
    assert "<b>Bob</b> · " in rows(render(hass, alerts))[0][1]  # a channel: who wrote it
    shown = texts(render(hass, alice))
    assert shown == ["On my way", "Got it", "New phone"]  # accepted before the reply (M3)
    (mine, _), (hers, cell), _ = rows(render(hass, alice))  # by her key, not her name (M2)
    assert (mine, hers) == ("right", "left")
    assert "<b>" not in cell  # a person's chat: no sender's name
    assert "✓✓ " in rows(render(hass, alice))[0][1]
    assert texts(render(hass, thread(loaded, "my-target"))) == ["Elsewhere"]
    assert "✓ sent" in rows(render(hass, thread(loaded, "my-target")))[0][1]
    shows = render(hass, alerts)
    await fire(hass, topics_of(make_config()).availability, "offline")
    assert render(hass, alerts) == shows  # the history stays readable
    assert form_findings({"alerts": shows}) == []
    assert problems(caplog) == []


@pytest.mark.parametrize(
    ("sizes", "opened"),
    [
        ([20] * 6, 6),
        ([20] * 8, 6),
        ([150] * 6, 2),
        ([400, 20, 20, 20, 20, 20], 2),
        ([100, 100, 100, 100, 10, 10], 3),  # from the newest: the fourth does not fit, and stops
    ],
)
async def test_the_newest_messages_stay_open_at_most_six_fewer_when_long(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    sizes: list[int],
    opened: int,
) -> None:
    """D-17: from the newest, at most 6 open, past the second only while their texts stay
    within 360 characters; the others in a fold that counts them and the problems among them
    (M47)."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alerts = thread(loaded, "alerts")
    history = [received(10 * i + 10, chr(ord("a") + i) * size) for i, size in enumerate(sizes)]
    history.append(final(500, "x", state="failed", reason="no_acknowledgement"))
    documented(hass, HISTORY, history)
    output = render(hass, alerts)
    assert len(rows(output)) == opened
    assert texts(output)[-1] == "a" * sizes[0] or "\u200b" in texts(output)[-1]
    folded = len(history) - opened
    assert earlier(output) == f"Earlier messages ({folded}) · with problems: 1"
    assert problems(caplog) == []


async def test_a_message_not_delivered_not_sent_or_maybe_forged_is_yellow_with_the_reason(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§3.4: failed, expired and a suspect confirmation in a yellow box with the reason in words
    (a code the dashboard does not know stays a code), but none that repeats "waited too long";
    the delivery code of a direct message the history holds is hidden (PC 16, M55); one on its
    way has none (#87)."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alice = thread(loaded, "alice")
    documented(
        hass,
        HISTORY,
        [
            final(10, "Lights" + CODE, **to_alice(state="confirmed", suspect=True, nonce=True)),
            final(
                20,
                "Dinner" + CODE,
                **to_alice(state="failed", reason="no_acknowledgement"),
                nonce=True,
            ),
            final(30, "Late" + CODE, **to_alice(state="expired", reason="queue_ttl", nonce=True)),
            final(40, "Odd" + CODE, **to_alice(state="failed", reason="error_7", nonce=True)),
            received(60, "Ends in #1a2b3c4d", **to_alice()),  # hers: as received
        ],
    )
    documented(hass, PENDING, [on_its_way(5, "Soon", **to_alice(target=None))])
    output = render(hass, alice)
    shown = rows(output)
    assert texts(output) == [
        "Ends in #1a2b3c4d",
        "Odd",
        "Late",
        "Dinner",
        "Lights",
        "Soon",
    ]
    assert CODE not in "".join(cell for _, cell in shown if "Ends in" not in cell)
    yellow = [cell for _, cell in shown if '<ha-alert alert-type="warning">' in cell]
    assert len(yellow) == 4
    assert "✗ not delivered (<code>error_7</code>)" in yellow[0]
    assert "✗ not sent: waited too long</small>" in yellow[1]  # the reason says it again
    assert "✗ not delivered (no acknowledgement)" in yellow[2]
    assert "⚠ delivered? maybe forged" in yellow[3]
    assert "… waiting to send" in shown[-1][1]
    assert "<i>Soon</i>" in shown[-1][1]
    assert problems(caplog) == []


async def test_the_delivery_code_is_cut_where_the_history_says_the_gateway_added_it(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#142: the gateway adds the code of `[messages] confirm_nonce` only to what the history and
    the final delivery event carry (#87), and since 2.2 each sent entry of the history says with
    `nonce` whether it did. So the cards cut ' #' and 8 hex digits from a sent direct message
    where it says true, whatever the setting is now; never where it says false, so that a
    household's own text that ends like a code shows whole; and only where it is null, a row of
    2.1, by the setting as before: while the configuration says confirm_nonce is on or does not
    say (the gateway offline: Home Assistant drops the configuration's attributes). A mixed
    history in a chat and Latest messages, each of its rows as the newest in Latest chats and
    All chats; one on its way has no code and shows whole, also with the setting on. The
    refused line quotes no text (#143)."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
    alice = thread(loaded, "alice")
    everything = card(loaded, "macro entry(e)")
    latest = card(cards(loaded, "overview"), "./messages")  # Latest chats
    chats = card(loaded, "Who reads what")  # All chats
    own = "Gate code ends #0a1b2c3d"  # the household's own words, as typed
    for setting, on in (
        ({"confirm_nonce": True}, True),
        ({"confirm_nonce": False}, False),
        ({"truncate": True}, True),  # a section without the setting
        (None, True),  # no section: the gateway offline
    ):
        hass.states.async_set(
            CONFIGURATION, ago(0), {} if setting is None else {"messages": setting}
        )
        mixed = [  # oldest first, as a thread shows them; each with what the cards show
            (final(30, own + CODE, **to_alice(state="confirmed", nonce=True)), own),
            (final(20, own, **to_alice(state="confirmed", nonce=False)), own),
            # sent by 2.1 with the code: the setting of now decides, as in 2.1
            (
                final(10, own + CODE, **to_alice(state="confirmed", nonce=None)),
                own if on else own + CODE,
            ),
        ]
        documented(hass, HISTORY, [entry for entry, _ in mixed])
        documented(hass, PENDING, [])
        shown = [text for _, text in mixed]
        assert texts(render(hass, alice)) == shown, setting
        assert texts(render(hass, everything)) == shown, setting
        for entry, text in mixed:
            documented(hass, HISTORY, [entry])
            for found in (latest, chats):
                said = render(hass, found)
                assert f"Sent: {text}</small>" in said, (setting, entry["nonce"], said[:200])
        documented(hass, PENDING, [on_its_way(5, own, **to_alice(target=None))])
        assert texts(render(hass, alice))[-1] == own, setting
        assert texts(render(hass, everything))[-1] == own, setting
        for found in (latest, chats):
            assert f"On its way: {own}</small>" in render(hass, found), setting
    refused = refused_line(loaded, "alice")
    event = DeliveryEvent("r1", "operator", DeliveryState.DUPLICATE, text=own)
    await fire(hass, topics_of(config).delivery, delivery_payload(event))
    line = render(hass, refused)
    assert "Not sent again" in line
    assert "Gate code" not in line  # #143: the line quotes no text
    assert problems(caplog) == []


async def test_messages_on_their_way_show_once_until_they_are_final(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A message waiting, one sent and waiting for its confirmation (try n of max), in italics
    after the final ones; one that the history holds already, between the gateway's two
    publishes, shows once (PC 20, M56); a pending message of another chat not at all."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alice = thread(loaded, "alice")
    done = final(10, "Done", **to_alice(state="confirmed"), id="d1")
    documented(hass, HISTORY, [done])
    documented(
        hass,
        PENDING,
        [
            on_its_way(30, "Done", **to_alice(target=None), id="d1", step="sent", attempt=1),
            on_its_way(
                20, "Trying", **to_alice(target=None), step="sent", attempt=2, max_attempts=3
            ),
            on_its_way(5, "Waiting", **to_alice(target=None)),
            on_its_way(4, "Family's"),
        ],
    )
    output = render(hass, alice)
    assert texts(output) == ["Trying", "Done", "Waiting"]  # by when the gateway took them
    cells = [cell for _, cell in rows(output)]
    assert "… sent, waiting for confirmation (try 2 of 3)" in cells[0]
    assert "… waiting to send" in cells[2]
    assert "<i>" not in cells[1]
    assert problems(caplog) == []


async def test_a_channel_page_holds_every_alias_on_its_channel(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two targets on channel 1 share the page chat-family (the first of the configuration):
    it shows what went through either alias and what came in on the channel (PC 3, M41), and
    the refusals of both."""
    config = make_config(TWO)
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
    assert "chat-alerts" not in [v["path"] for v in loaded["views"]]
    alerts = thread(loaded, "family")
    documented(
        hass,
        HISTORY,
        [
            final(10, "Via family", target="family", name="family"),
            final(20, "Via alerts"),
            received(30, "Came in", target="alerts"),
        ],
    )
    assert texts(render(hass, alerts)) == ["Came in", "Via alerts", "Via family"]
    refused = refused_line(loaded, "family")
    topics = topics_of(config)
    for alias in ("family", "alerts"):
        event = DeliveryEvent(f"r{alias}", alias, DeliveryState.REJECTED, reason="too_long")
        await fire(hass, topics.delivery, delivery_payload(event))
        assert "Not sent: text too long." in render(hass, refused), alias
    assert problems(caplog) == []


# --- the refused line above the thread ------------------------------------------------------------


async def test_the_refused_line_says_why_a_send_of_this_chat_was_refused(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§3.4: above the thread, not in the footer (H-27) and not under the thread, where the footer
    covered it (#143), a refusal of this chat's Send in words, which a household user sees: too
    long, the same text again, no text at all (HC 1); never the refused text, which the field
    keeps and, sent twice, the thread shows; not another chat's refusal, not one followed by a
    message of this chat, and nothing at all otherwise."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    viewer = await household_viewer(hass, hass_ws_client)
    alice = page(loaded, "alice")
    refused = refused_line(loaded, "alice")
    topics = topics_of(config)
    shows_nothing(hass, refused)
    assert not await viewer.shown(alice, refused)

    async def refusal(target: str, state: DeliveryState, **more: Any) -> str:
        event = DeliveryEvent(f"r{time.time()}", target, state, **more)
        await fire(hass, topics.delivery, delivery_payload(event))
        return render(hass, refused)

    line = await refusal(ALICE, DeliveryState.REJECTED, reason="too_long")
    assert line == (
        '<div><ha-alert alert-type="warning"><b>Not sent: text too long.</b></ha-alert></div>'
    )
    assert await viewer.shown(alice, refused), viewer.errors
    line = await refusal("operator", DeliveryState.DUPLICATE, text="Back <b>soon</b>")
    assert line == (
        '<div><ha-alert alert-type="warning"><b>Not sent again: the same text went to this chat '
        "a moment ago.</b></ha-alert></div>"
    )
    line = await refusal(ALICE, DeliveryState.REJECTED, reason="queue_full", text="Back soon")
    assert line == (
        '<div><ha-alert alert-type="warning"><b>Not sent: queue full.</b></ha-alert></div>'
    )
    line = await refusal("operator", DeliveryState.REJECTED, reason="empty")
    assert "<b>Nothing was sent: the gateway had no text. Tap Return (↵) in the field" in line
    await refusal("alerts", DeliveryState.REJECTED, reason="too_long")
    shows_nothing(hass, refused)  # another chat's
    await refusal(ALICE, DeliveryState.REJECTED, reason="too_long")
    documented(hass, PENDING, [on_its_way(-5, "Shorter", **to_alice(target=None))])
    shows_nothing(hass, refused)  # a message of this chat after it: the line is done
    assert not await viewer.shown(alice, refused)
    assert problems(caplog) == []


# --- why writing is not possible, and About ------------------------------------------------------


async def test_a_chat_page_says_why_its_field_is_missing(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S3, for a household user: red while the gateway is offline, red while the radio is
    disconnected, and the footer stays then (HC 11); yellow for a field that has another name on
    this gateway, with that name for the administrator and the guide's section (D-30); yellow
    for a field that is missing; nothing once the field is there."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, drafts=False)  # without its field
    viewer = await household_viewer(hass, hass_ws_client)
    bob = page(loaded, "bob")
    must_wait = card(cards(loaded, "bob"), "Why you cannot write now")
    cannot = card(cards(loaded, "bob"), "has another name on this gateway")
    topics = topics_of(config)
    documented(hass, HISTORY, [])
    assert await viewer.shown(bob, cannot)
    said = render(hass, cannot)
    assert said.startswith('<div><ha-alert alert-type="warning"><b>You cannot write to Bob from')
    assert "Ask your administrator to make the dashboard again." in said
    own = discovery_payload(config, topics, RADIO)["dev"]["ids"][0]
    renamed = f"{own}_contact_{BOB}"
    await fire(  # Bob's field under another ID: a second gateway's contact prefix, say
        hass,
        f"homeassistant/device/{renamed}_drafts/config",
        drafts(renamed, "mesh_bob_write_2", via=own, key=BOB),
    )
    said = render(hass, cannot)
    assert "has another name on this gateway: text.mesh_bob_write_2. " in said
    guide = "docs/home-assistant.md#a-second-gateway"
    assert re.search(rf'<a href="https://[^"]+/blob/v[^/]+/{guide}">How, in the guide</a>', said)
    assert not await viewer.shown(bob, bob["footer"]["card"])
    await fire(
        hass,
        f"homeassistant/device/{renamed}_drafts/config",
        drafts(renamed, "mesh_bob_write", via=own, key=BOB),
    )
    assert await viewer.shown(bob, bob["footer"]["card"])
    assert not await viewer.shown(bob, cannot)
    shows_nothing(hass, must_wait)
    await fire(hass, topics.state, state_document(known=False))
    assert render(hass, must_wait).startswith(
        '<div><ha-alert alert-type="error"><b>The radio is disconnected.</b> Messages wait'
    )
    assert "<summary>Why messages wait</summary>" in render(hass, must_wait)  # writing works
    assert await viewer.shown(bob, bob["footer"]["card"])  # a message waits at the gateway
    await fire(hass, topics.availability, "offline")
    assert render(hass, must_wait).startswith(
        '<div><ha-alert alert-type="error"><b>You cannot write now: the gateway is offline.</b>'
    )
    assert "<summary>Why you cannot write now</summary>" in render(hass, must_wait)
    shows_nothing(hass, cannot)  # the red line says it
    assert problems(caplog) == []


async def test_a_chat_page_says_when_its_field_or_its_send_button_cannot_be_used(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S3 for a field under the page's own ID whose footer hides all the same: Bob's Send button
    unavailable or turned off, the field of Alerts turned off (Home Assistant keeps a disabled
    entity without a state, and its device does not list it). The card names that cause: not
    another name, which is for a field found under another ID (D-30), and not making the
    dashboard again, which is for a field that is missing."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config, drafts=False)
    viewer = await household_viewer(hass, hass_ws_client)
    documented(hass, HISTORY, [])
    own = discovery_payload(config, topics_of(config), RADIO)["dev"]["ids"][0]
    bob = f"{own}_contact_{BOB}"
    never = drafts(bob, "mesh_bob_write", via=own, key=BOB)
    never["cmps"]["mesh_bob_write_send"]["availability_topic"] = "test/never"
    await fire(hass, f"homeassistant/device/{bob}_drafts/config", never)
    await fire(
        hass, f"homeassistant/device/{own}_alerts/config", drafts(own, f"{own}_write_alerts")
    )
    registry = er.async_get(hass)

    async def says(chat: str) -> str:
        view = page(loaded, chat)
        cannot = card(cards(loaded, chat), "has another name on this gateway")
        assert not await viewer.shown(view, view["footer"]["card"])
        assert await viewer.shown(view, cannot)
        said = render(hass, cannot)
        assert "another name" not in said
        assert "make the dashboard again" not in said
        return said

    assert hass.states.get("button.mesh_bob_write_send").state == "unavailable"
    turned = "this chat's field or its Send button is disabled, unavailable or on another device"
    assert turned in await says("bob")
    view = page(loaded, "alerts")
    assert await viewer.shown(view, view["footer"]["card"])
    for entity in ("button.mesh_bob_write_send", f"text.{own}_write_alerts"):
        registry.async_update_entity(entity, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert hass.states.get(f"text.{own}_write_alerts") is None
    assert turned in await says("bob")
    assert turned in await says("alerts")
    assert problems(caplog) == []


async def test_about_a_person_and_the_target_lines_of_a_chat(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S4: where the person is, the battery in volts once it is current, and whether the
    gateway asks, with the device's answer in words (D-8) and the way to Contacts; the hops and
    the signal once both are known. On a page that writes through a target, its text before and
    after messages and its priority from the gateway's configuration (HC 30), left out without
    it; the help explains a target's lines only on such a page."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    about = card(cards(loaded, "alice"), "Change on Contacts")
    said = render(hass, about)
    assert said.startswith('<table role="presentation" width="100%"><tr><td>Where</td><td>')
    assert "<td>Ask for position and battery</td><td>on · answering</td>" in said
    assert '<div><a href="./contacts">Change on Contacts</a></div>' in said
    assert "Text before or after messages:" not in said.split("<details>")[0]
    alice = "sensor.mesh_alice_telemetry_state"
    for state, word in (("no_answer", "no answer for a day"), ("later", "<code>later</code>")):
        hass.states.async_set(alice, state)
        assert f"<td>Ask for position and battery</td><td>on · {word}</td>" in render(hass, about)
    registry = er.async_get(hass)
    (device,) = [d for d in dr.async_get(hass).devices if d.serial_number == ALICE]
    battery = registry.async_get_or_create(
        "sensor",
        "mqtt",
        "contract_alice_battery",
        device_id=device.id,
        suggested_object_id="mesh_alice_battery",
    ).entity_id
    volts = {"device_class": "voltage", "unit_of_measurement": "V"}
    for state, shown in (("3.912", "3.9 V"), ("unknown", NONE), ("unavailable", NONE)):
        hass.states.async_set(battery, state, volts)  # unavailable while answers are not current
        assert f"<td>Battery</td><td>{shown}</td>" in render(hass, about), state
    hass.states.async_set("sensor.mesh_alice_hops", "2")
    hass.states.async_set("sensor.mesh_alice_snr", "unknown")
    assert "hops away" not in render(hass, about)
    hass.states.async_set("sensor.mesh_alice_snr", "6.25")
    assert "it was 2 hops away, at a signal of 6.25 dB." in render(hass, about)
    assert "Sent before other messages:" in render(hass, about)  # Alice's page writes to operator
    bob = render(hass, card(cards(loaded, "bob"), "Change on Contacts"))
    assert "Sent before other messages" not in bob  # his own field: no target
    targets = {
        "total": 1,
        "truncated": False,
        "items": [{"alias": "operator", "prefix": "[Home] ", "suffix": " -Ops", "alert": True}],
    }
    hass.states.async_set(CONFIGURATION, ago(0), {"targets": targets})
    said = render(hass, about)
    assert "<div>Text before or after messages: [Home]  …  -Ops</div>" in said
    assert "<div>Sent before other messages.</div>" in said
    targets["items"][0] |= {"suffix": ""}
    hass.states.async_set(CONFIGURATION, ago(0), {"targets": targets})
    assert "<div>Text before messages: [Home] </div>" in render(hass, about)
    targets["items"][0] |= {"prefix": "", "suffix": " -Ops"}
    hass.states.async_set(CONFIGURATION, ago(0), {"targets": targets})
    assert "<div>Text after messages:  -Ops</div>" in render(hass, about)
    channel = render(hass, card(cards(loaded, "alerts"), "Change on Contacts"))
    assert channel.startswith("<div>About 158 letters per message.</div>")
    assert problems(caplog) == []


async def test_a_chats_head_says_who_and_how_to_write(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S1: the name and when the person was last heard, or the channel and who reads it; "How
    to write" with the field's label and the gateway's numbers when it publishes them (G6): the
    room, the window of the same text in minutes or seconds, none when it is off, whether a
    long text is cut. A person whose device is gone, removed from the radio or hidden in Home
    Assistant, says so instead and how to get it back (S1, HC 3), and the card below the thread
    stays empty; a page that writes through a contact target keeps its field and says that
    messages fail while the radio does not have the contact."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
    viewer = await household_viewer(hass, hass_ws_client)
    bob = card(cards(loaded, "bob"), "How to write")
    said = render(hass, bob)
    assert said.startswith("<div><b>Bob</b></div>\n<details><summary>How to write<")  # unheard
    alice = render(hass, card(cards(loaded, "alice"), "How to write"))
    assert re.match(r"<div><b>Alice</b> · heard \d\d:\d\d</div>\n<details>", alice)
    assert "Type in the field “Write to Bob” at the bottom, tap Return (↵), then tap Send." in said
    assert "cleared after 15 minutes" in said
    assert "About 160 letters; a longer text is cut and ends in ..." in said
    assert "within a few minutes is not sent again" in said
    messages = {"truncate": False, "dedup_window_s": 600}
    hass.states.async_set(CONFIGURATION, ago(0), {"messages": messages})
    said = render(hass, bob)
    assert "About 160 letters; a longer text is not sent and stays in the field" in said
    assert "within 10 minutes is not sent again" in said
    for window, line in ((20, "within 20 seconds"), (90, "within 90 seconds"), (0, None)):
        messages = {"truncate": True, "dedup_window_s": window}
        hass.states.async_set(CONFIGURATION, ago(0), {"messages": messages})
        sames = re.findall(r"<li>The same text to the same chat ([^<]*)</li>", render(hass, bob))
        assert sames == (
            [f"{line} is not sent again, unless it was not delivered."] if line else []
        )
    alerts = render(hass, card(cards(loaded, "alerts"), "How to write"))
    assert alerts.startswith(
        "<div><b>Alerts</b> · channel · everyone with the channel's key reads it</div>"
    )
    cannot = card(cards(loaded, "bob"), "has another name on this gateway")
    documented(hass, HISTORY, [])  # a gateway of 2.1, whose S3 would speak
    registry = dr.async_get(hass)  # Bob's device deleted: hidden, or gone from the radio
    for key in (BOB, ALICE):
        (device,) = [d for d in registry.devices if d.serial_number == key]
        registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    # since 2026.11 Home Assistant clears a deleted device's topic once for each of its entities
    # and finds nothing left to clean up at a copy of its own (#115)
    leftovers = [
        "WARNING homeassistant.components.mqtt.discovery: No device components to cleanup for "
        f"contact_{key}, node_id '{PREFIX}'"
        for key in (BOB, ALICE)
    ]
    assert problems(caplog) == (leftovers if CLEARS_PER_ENTITY else [])
    caplog.clear()
    said = render(hass, bob)
    assert "<b>Bob is no longer a contact of this radio, or was hidden.</b>" in said
    assert "<summary>Why you cannot write here</summary>" in said
    assert "To write again, add the contact again, or show hidden contacts again" in said
    shows_nothing(hass, cannot)  # the head says why: no second answer
    assert not await viewer.shown(page(loaded, "bob"), cannot)
    alice = render(hass, card(cards(loaded, "alice"), "How to write"))  # through operator
    assert "<b>Alice is no longer a contact of this radio, or was hidden.</b> If the radio" in alice
    assert "<summary>How to write</summary>" in alice
    assert problems(caplog) == []


async def test_a_chat_listens_to_its_documents_and_the_minute_only(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 10: a chat's thread renders again on the history, the pending messages,
    the radio, the configuration (whether the gateway adds confirm_nonce's code, #87) and the
    minute; its refused line on the delivery event, the two documents and the minute: no
    domain, no state of every entity."""
    from homeassistant.helpers.template import Template  # noqa: PLC0415

    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    documented(hass, HISTORY, [received(10, **to_alice())])
    radio = f"binary_sensor.{PREFIX}_radio"
    delivery = f"event.{PREFIX}_delivery"
    for found, expected in (
        # the radio for the time of each message
        (thread(loaded, "alice"), {HISTORY, PENDING, radio, CONFIGURATION}),
        (card(loaded, "macro entry(e)"), {HISTORY, PENDING, radio, CONFIGURATION}),
        (refused_line(loaded, "alice"), {HISTORY, PENDING, delivery}),
    ):
        info = Template(found["content"], hass).async_render_to_info(
            {"config": found, "user": "Bob"}, strict=True
        )
        assert not info.domains
        assert not info.domains_lifecycle
        assert not info.all_states
        assert {HISTORY, PENDING} <= set(info.entities) <= expected
        assert info.has_time
    assert problems(caplog) == []


# --- All chats, Latest messages, Latest chats ---------------------------------------------


async def test_all_chats_and_latest_chats_show_each_chats_newest_message(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each chat with its newest message, without the delivery code (PC 16), one on its way
    called so (D-7), and "new" for a message received less than 15 minutes ago, never for one
    only sent (D-19, HC 22, M54); the chats with a message first, each with the time it is
    sorted by: when the gateway took a message from this home, as in its thread; Latest chats
    the three newest, All chats every page; and the contacts and channels without a page."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    everything = card(loaded, "Who reads what")
    latest = card(cards(loaded, "overview"), "./messages")  # Latest chats
    documented(
        hass,
        HISTORY,
        [
            final(60, "Reply" + CODE, **to_alice(state="confirmed", nonce=True)),
            received(300, "Hello", **to_alice()),
            received(1200, "Old news", channel_index=2, target="my-target"),
            received(1300, "Stranger", channel_index=5, target=None),
        ],
    )
    documented(hass, PENDING, [on_its_way(30, "Soon", **to_alice(target=None))])
    for found in (everything, latest):
        said = render(hass, found)
        assert CODE not in said, said
        first = said.split("</tr>")[0]
        assert '<a href="./contact-alice"><b>Alice</b></a>' in first
        assert "On its way: Soon" in first
        assert " · new</small>" in first  # Hello, 5 minutes ago
        assert " · new" not in said.split("</tr>")[1]  # My target: 20 minutes ago
    documented(hass, HISTORY, [final(60, "Only mine", **to_alice(state="confirmed"))])
    documented(hass, PENDING, [])
    assert " · new" not in render(hass, latest)
    assert " · new" not in render(hass, everything)
    said = render(hass, everything)
    titles = re.findall(r'<a href="\./([\w-]+)"><b>', said)
    assert titles == ["contact-alice", "chat-alerts", "chat-my-target", "contact-bob"]
    assert "<div>Channels heard without a chat page:" not in said
    documented(hass, HISTORY, [received(1300, "Stranger", channel_index=5, target=None)])
    assert "<div>Channels heard without a chat page: channel 5</div>" in render(hass, everything)
    taken = ago(600)
    documented(  # confirmed a minute ago, taken ten minutes ago: sorted and shown by the ten
        hass,
        HISTORY,
        [
            final(60, "Reply", **to_alice(state="confirmed"), queued_at=taken),
            received(300, "Dinner"),
        ],
    )
    for found in (everything, latest):
        listed = re.findall(
            r'<b>([^<]*)</b></a>.*?valign="top"><small>([^<]*)</small>', render(hass, found)
        )
        assert [title for title, _ in listed][:2] == ["Alerts", "Alice"]
        assert clock(taken) in listed[1][1]
    assert problems(caplog) == []


async def test_latest_messages_name_each_messages_chat(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C2: every chat's messages in one thread; a received one says who wrote it and where, a
    sent one to whom."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    everything = card(loaded, "macro entry(e)")
    documented(hass, HISTORY, [])
    assert render(hass, everything).startswith("<div>No message yet.</div>")
    documented(
        hass,
        HISTORY,
        [
            final(10, "Gate open"),
            received(20, "Hi", **to_alice()),
            received(30, "Psst", kind="contact", contact_key=None, channel_index=None, name="Eve"),
            received(40, "Hello all", channel_index=5, target=None),
            received(50, "Dinner"),
        ],
    )
    cells = [cell for _, cell in rows(render(hass, everything))]
    assert "<b>Bob in Alerts</b> · " in cells[0]
    assert "<b>Bob in channel 5</b> · " in cells[1]
    assert "<b>Eve (not a contact)</b> · " in cells[2]
    assert "<b>Alice</b> · " in cells[3]
    assert "<small>to Alerts · " in cells[4]
    assert problems(caplog) == []


@pytest.mark.parametrize("name", [*SHOWS_NOTHING, "Carol"])
async def test_a_sender_whose_name_shows_nothing_is_a_question_mark(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    """A name that shows nothing, made only of invisible characters such as a zero-width space
    or drawn as nothing such as a Hangul filler, is no name: the sender a channel message claims
    and a contact without a chat page are "?" in Latest messages, Latest chats, All chats and a
    channel's thread, as without a name, never an empty bold name. A name that shows stays."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    everything = card(loaded, "macro entry(e)")
    alerts = thread(loaded, "alerts")
    latest = card(cards(loaded, "overview"), "./messages")  # Latest chats
    chats = card(loaded, "Who reads what")  # All chats
    shown = name if name == "Carol" else "?"
    zed = {"kind": "contact", "contact_key": ZED, "channel_index": None, "name": name}
    documented(
        hass,
        HISTORY,
        [
            final(10, "Gate open", **zed, target=None),
            received(20, "Psst", **zed, target=None),
            received(30, "Psst", kind="contact", contact_key=None, channel_index=None, name=name),
            received(40, "Dinner", name=name),
        ],
    )
    cells = [cell for _, cell in rows(render(hass, everything))]
    assert f"<b>{shown} in Alerts</b> · " in cells[0], cells[0]
    assert f"<b>{shown} (not a contact)</b> · " in cells[1], cells[1]
    assert f"<b>{shown}</b> · " in cells[2], cells[2]
    assert f"<small>to {shown} · " in cells[3], cells[3]
    assert f"<b>{shown}</b> · " in render(hass, alerts)
    documented(hass, HISTORY, [received(40, "Dinner", name=name)])
    for found in (latest, chats):
        assert f"{shown}: Dinner</small>" in render(hass, found)
    assert problems(caplog) == []


async def test_latest_messages_show_each_message_once_as_its_chat_does(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C2 with the fixtures of the thread's tests: by when the gateway took a message (M3), the
    delivery code hidden on every direct message from this home, sent or on its way (PC 16,
    M55), a message on its way that the history holds already shown once (PC 20, M56), and the
    open rows fewer when long (D-17, M47)."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    everything = card(loaded, "macro entry(e)")
    documented(
        hass,
        HISTORY,
        [
            final(20, "On my way", **to_alice(state="confirmed"), queued_at=ago(120)),
            received(30, "Got it", **to_alice()),
        ],
    )
    assert texts(render(hass, everything)) == ["On my way", "Got it"]
    documented(
        hass,
        HISTORY,
        [
            final(10, "Lights" + CODE, **to_alice(state="confirmed", suspect=True, nonce=True)),
            final(
                20,
                "Dinner" + CODE,
                **to_alice(state="failed", reason="no_acknowledgement"),
                nonce=True,
            ),
            final(30, "Stuck" + CODE, **to_alice(state="expired", reason="no_radio", nonce=True)),
            received(60, "Ends in #1a2b3c4d", **to_alice()),
        ],
    )
    documented(hass, PENDING, [on_its_way(5, "Soon", **to_alice(target=None))])
    output = render(hass, everything)
    assert texts(output) == ["Ends in #1a2b3c4d", "Stuck", "Dinner", "Lights", "Soon"]
    assert CODE not in "".join(cell for _, cell in rows(output) if "Ends in" not in cell)
    assert "✗ not sent: waited too long (no radio)" in rows(output)[1][1]
    done = final(10, "Done" + CODE, **to_alice(state="confirmed", nonce=True), id="d1")
    documented(hass, HISTORY, [done])
    documented(
        hass,
        PENDING,
        [
            on_its_way(30, "Done", **to_alice(target=None), id="d1", step="sent"),
            on_its_way(5, "Waiting", **to_alice(target=None)),
            on_its_way(4, "To all"),
        ],
    )
    assert texts(render(hass, everything)) == ["Done", "Waiting", "To all"]
    documented(hass, PENDING, [])
    documented(hass, HISTORY, [received(10 * i + 10, chr(ord("a") + i) * 150) for i in range(6)])
    output = render(hass, everything)
    assert len(rows(output)) == 2
    assert earlier(output) == "Earlier messages (4)"
    assert problems(caplog) == []


# --- long words --------------------------------------------------------------------------------

KEY = "0123456789abcdef" * 4
LINK = "https://example.org/" + "a" * 34 + "?q=1&r=2"  # & the 59th letter: a break falls on it


async def test_long_words_break_every_20_letters_and_never_inside_an_entity(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-52: mt() puts U+200B after every 20 base letters of a run: a 64-hex key in three
    places, a link with `&amp;` counted as one letter and never split (M39); not before a
    combining mark, a joiner or a variation selector (M43), and runs joined by non-breaking
    spaces break too; m() keeps U+200C and U+200D (M42)."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alerts = thread(loaded, "alerts")
    nbsp = "\u00a0".join(["word"] * 10)
    marks = "e\u0301" * 25
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467" * 8
    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    documented(
        hass,
        HISTORY,
        [final(5, KEY)]
        + [
            received(10 * i + 10, text)
            for i, text in enumerate([KEY, LINK, nbsp, marks, family, persian])
        ],
    )
    shown = texts(render(hass, alerts))
    broken = "\u200b".join([KEY[:20], KEY[20:40], KEY[40:60], KEY[60:]])
    assert shown[-1] == broken  # sent from this home (M32)
    shown = shown[:-1]
    assert shown[-1] == broken
    assert shown[-2].count("\u200b") >= 2  # the link
    raw = render(hass, alerts)
    assert not re.search(r"&[#0-9a-zA-Z]*\u200b[#0-9a-zA-Z]*;", raw)
    assert shown[-3].count("\u200b") >= 2  # the words joined by non-breaking spaces
    assert "\u200b\u0301" not in raw  # never before a combining mark
    assert "\u200d\u200b" not in raw
    assert "\u200b\u200d" not in raw
    assert family in raw  # the joiners stay
    assert persian in raw
    assert problems(caplog) == []


# --- the sensors and drafts from the gateway's own discovery ----------------------------------


async def test_the_thread_reads_the_history_the_gateway_announces(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The history document on the gateway's history topic, through its own discovery (G1),
    reaches a chat's thread; the messages on their way likewise (G3)."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
    assert hass.states.get(HISTORY) is not None, "no history sensor: the gateway's G1"
    assert hass.states.get(PENDING) is not None, "no pending messages' sensor: the gateway's G3"
    topics = topics_of(config)
    history = {"total": 1, "truncated": False, "messages": [received(10, "From the topic")]}
    await fire(hass, topics.history, history, retain=True)
    pending = {"total": 1, "truncated": False, "messages": [on_its_way(5, "On its way")]}
    await fire(hass, f"{topics.root}/pending", pending, retain=True)
    assert texts(render(hass, thread(loaded, "alerts"))) == ["From the topic", "On its way"]
    assert problems(caplog) == []


async def test_a_chat_page_reads_the_configuration_the_gateway_announces(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The configuration document on the gateway's config topic, through its own discovery
    (G6), reaches the chat pages: the room of a channel's text, which only the gateway knows
    once the radio told it its name, whether a long text is cut, the window of the same text,
    and a target's text before messages and its priority."""
    targets = TARGETS.replace("[targets.operator]\n", '[targets.operator]\nprefix = "[Home] "\n')
    assert targets != TARGETS
    more = '[messages]\nalert_targets = ["operator"]\ntruncate = false\ndedup_window_s = 600\n'
    config = make_config(targets + more)
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
    assert hass.states.get(CONFIGURATION) is not None, "no configuration sensor: the gateway's G6"
    alerts = card(cards(loaded, "alerts"), "How to write")
    assert "About 158 letters; a longer text is cut" in render(hass, alerts)  # the page's own
    settings = target_settings(config, RADIO)
    document = config_document(time.time(), settings, shown_settings(config))
    await fire(hass, topics_of(config).config, document, retain=True)
    room = 160 - len(f"{RADIO.name}: ")  # the firmware puts the radio's name before a channel text
    assert [s.room for s in settings if s.alias == "alerts"] == [room]
    said = render(hass, alerts)
    assert f"About {room} letters; a longer text is not sent and stays in the field" in said
    assert "within 10 minutes is not sent again" in said
    channel = render(hass, card(cards(loaded, "alerts"), "Change on Contacts"))
    assert channel.startswith(f"<div>About {room} letters per message.</div>")
    about = render(hass, card(cards(loaded, "alice"), "Change on Contacts"))  # through operator
    assert "<div>Text before messages: [Home] </div>" in about
    assert "<div>Sent before other messages.</div>" in about
    assert problems(caplog) == []


async def test_a_household_user_writes_and_sends_through_the_gateways_drafts(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 3: a household user's text.set_value publishes {"text": …} and button.press
    {"send": true} on the chat's request topic at QoS 0, for a target and for a person; the
    gateway's echo is the field's state, and the footer shows (G4)."""
    from homeassistant.auth.const import GROUP_ID_USER  # noqa: PLC0415

    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
    topics = topics_of(config)
    viewer = await household_viewer(hass, hass_ws_client)
    user = await hass.auth.async_create_user("Carol", group_ids=[GROUP_ID_USER])
    token = hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    )
    client = await hass_ws_client(hass, token)
    draft = f"{topics.root}/homeassistant/draft"
    for chat, field, request in (
        ("alerts", f"text.{PREFIX}_write_alerts", f"{draft}/target/alerts/request"),
        ("bob", "text.mesh_bob_write", f"{draft}/contact/{BOB}/request"),
    ):
        assert hass.states.get(field) is not None, field
        view = page(loaded, chat)
        assert await viewer.shown(view, view["footer"]["card"]), viewer.errors
        for service, data, payload in (
            ("text", {"entity_id": field, "value": "Hi"}, {"text": "Hi"}),
            ("button", {"entity_id": f"button.{field[5:]}_send"}, {"send": True}),
        ):
            mqtt.async_publish.reset_mock()
            action = "set_value" if service == "text" else "press"
            await client.send_json_auto_id(
                {"type": "call_service", "domain": service, "service": action, "service_data": data}
            )
            answer = await client.receive_json()
            assert answer["success"], answer
            await hass.async_block_till_done()
            ((topic, sent, qos, _),) = [c.args[:4] for c in mqtt.async_publish.call_args_list]
            assert (topic, json.loads(sent), qos) == (request, payload, 0)
    assert problems(caplog) == []
