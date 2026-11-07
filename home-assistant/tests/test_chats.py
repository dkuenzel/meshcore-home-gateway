"""The generated dashboard's chats against the real Home Assistant: a chat's page (its head, its
thread, why writing is not possible, its About and its footer's refused line), All chats and Latest
messages on Chats, and Latest chats on Overview (docs/home-assistant.md, Dashboard).

The household and the helpers are those of test_dashboard.py. The cards read the gateway's message
history and its messages on their way, sensors of version 2.1 (G1, G3): most tests here set their
states as the gateway announces them (documented()), so they test the cards' rules on their own;
the tests marked "stage 2" feed the documents through the gateway's own discovery and need it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_contract import PREFIX, RADIO, fire, make_config, problems, state_document
from visibility import household_viewer

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.mesh import Node
from meshcore_home_gateway.core.model import DeliveryEvent, DeliveryState
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.payloads import delivery_payload
from meshcore_home_gateway.views import iso
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, Broker
from test_dashboard import (
    BOB,
    CONFIGURATION,
    HISTORY,
    PENDING,
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
    }
    return entry | more


def to_alice(**more: Any) -> dict[str, Any]:
    """The keys of a direct message with Alice."""
    return {"kind": "contact", "contact_key": ALICE, "channel_index": None, "name": "Alice"} | more


def final(seconds: float, text: str = "Gate open", **more: Any) -> dict[str, Any]:
    """A message sent from this home, final `seconds` ago, accepted a second before."""
    entry = received(seconds, text) | {
        "direction": "out",
        "name": "alerts",
        "state": "sent",
        "hops": None,
        "snr_db": None,
        "id": f"m{seconds:g}",
        "queued_at": ago(seconds + 1),
        "suspect": False,
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
    hass: HomeAssistant, hass_ws_client: Any, tmp_path: Path, config: Config | None = None
) -> dict[str, Any]:
    """The dashboard of a household of Alice (also the target operator) and Bob, loaded; the
    gateway online with them."""
    config = config or make_config()
    broker, mesh = Broker(), household(ALICE_NODE, Node(BOB, "Bob", COMPANION))
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
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
    (a code the dashboard does not know stays a code); the delivery code of a direct message is
    hidden, sent or still on its way (PC 16, M55)."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alice = thread(loaded, "alice")
    documented(
        hass,
        HISTORY,
        [
            final(10, "Lights" + CODE, **to_alice(state="confirmed", suspect=True)),
            final(20, "Dinner" + CODE, **to_alice(state="failed", reason="no_acknowledgement")),
            final(30, "Late" + CODE, **to_alice(state="expired", reason="queue_ttl")),
            final(40, "Odd" + CODE, **to_alice(state="failed", reason="error_7")),
            received(60, "Ends in #1a2b3c4d", **to_alice()),  # hers: as received
        ],
    )
    documented(hass, PENDING, [on_its_way(5, "Soon" + CODE, **to_alice(target=None))])
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
    assert "✗ not sent: waited too long (waited too long)" in yellow[1]
    assert "✗ not delivered (no acknowledgement)" in yellow[2]
    assert "⚠ delivered? maybe forged" in yellow[3]
    assert "… waiting to send" in shown[-1][1]
    assert "<i>Soon</i>" in shown[-1][1]
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
    refused = page(loaded, "family")["footer"]["card"]["cards"][0]
    topics = topics_of(config)
    for alias in ("family", "alerts"):
        event = DeliveryEvent(f"r{alias}", alias, DeliveryState.REJECTED, reason="too_long")
        await fire(hass, topics.delivery, delivery_payload(event))
        assert "Not sent: text too long." in render(hass, refused), alias
    assert problems(caplog) == []


# --- the footer's refused line -------------------------------------------------------------------


async def test_the_refused_line_says_why_a_send_of_this_chat_was_refused(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§3.4, D-20: in the footer above the field, a refusal of this chat's Send in words: too
    long, the same text again, no text at all (HC 1); not another chat's refusal, not one
    followed by a message of this chat, and nothing at all otherwise."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    alice = page(loaded, "alice")
    refused = alice["footer"]["card"]["cards"][0]
    topics = topics_of(config)
    shows_nothing(hass, refused)

    async def refusal(target: str, state: DeliveryState, **more: Any) -> str:
        event = DeliveryEvent(f"r{time.time()}", target, state, **more)
        await fire(hass, topics.delivery, delivery_payload(event))
        return render(hass, refused)

    line = await refusal(ALICE, DeliveryState.REJECTED, reason="too_long")
    assert line == (
        '<div><ha-alert alert-type="warning"><b>Not sent: text too long.</b></ha-alert></div>'
    )
    line = await refusal("operator", DeliveryState.DUPLICATE, text="Back <b>soon</b>")
    assert line == (
        '<div><ha-alert alert-type="warning"><b>Not sent again: the same text went to this chat '
        "a moment ago.</b><br>Back &lt;b&gt;soon&lt;/b&gt;</ha-alert></div>"
    )
    line = await refusal("operator", DeliveryState.REJECTED, reason="empty")
    assert "<b>Nothing was sent: the gateway had no text. Tap Return (↵) in the field" in line
    await refusal("alerts", DeliveryState.REJECTED, reason="too_long")
    shows_nothing(hass, refused)  # another chat's
    await refusal(ALICE, DeliveryState.REJECTED, reason="too_long")
    documented(hass, PENDING, [on_its_way(-5, "Shorter", **to_alice(target=None))])
    shows_nothing(hass, refused)  # a message of this chat after it: the line is done
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
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    viewer = await household_viewer(hass, hass_ws_client)
    bob = page(loaded, "bob")
    must_wait = card(cards(loaded, "bob"), "Why you cannot write now")
    cannot = card(cards(loaded, "bob"), "has another name on this gateway")
    topics = topics_of(config)
    documented(hass, HISTORY, [])
    registry = er.async_get(hass)
    for field in ("text.mesh_bob_write", "button.mesh_bob_write_send"):  # a gateway of 2.1 has
        if registry.async_get(field) is not None:  # them: the page starts without its field
            registry.async_remove(field)
    await hass.async_block_till_done()
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
    assert await viewer.shown(bob, bob["footer"]["card"])  # a message waits at the gateway
    await fire(hass, topics.availability, "offline")
    assert render(hass, must_wait).startswith(
        '<div><ha-alert alert-type="error"><b>You cannot write now: the gateway is offline.</b>'
    )
    shows_nothing(hass, cannot)  # the red line says it
    assert problems(caplog) == []


async def test_about_a_person_and_the_target_lines_of_a_chat(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S4: where the person is, the battery and whether the gateway asks, with the way to
    Contacts; on a page that writes through a target, its text before and after messages and
    its priority from the gateway's configuration (HC 30), left out without it."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    about = card(cards(loaded, "alice"), "Change on Contacts")
    said = render(hass, about)
    assert said.startswith('<table role="presentation" width="100%"><tr><td>Where</td><td>')
    assert "<td>Ask for position and battery</td><td>on</td>" in said
    assert '<div><a href="./contacts">Change on Contacts</a></div>' in said
    assert "Text before or after messages:" not in said.split("<details>")[0]
    targets = {
        "total": 1,
        "truncated": False,
        "items": [{"alias": "operator", "prefix": "[Home] ", "suffix": " -Ops", "alert": True}],
    }
    hass.states.async_set(CONFIGURATION, ago(0), {"targets": targets})
    said = render(hass, about)
    assert "<div>Text before or after messages: [Home]  …  -Ops</div>" in said
    assert "<div>Sent before other messages.</div>" in said
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
    room, the window of the same text, whether a long text is cut; a person no longer on the
    radio says so instead (S1, HC 3)."""
    config = make_config()
    loaded = await chats_of(hass, hass_ws_client, tmp_path, config)
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
    alerts = render(hass, card(cards(loaded, "alerts"), "How to write"))
    assert alerts.startswith(
        "<div><b>Alerts</b> · channel · everyone with the channel's key reads it</div>"
    )
    registry = dr.async_get(hass)  # Bob's device deleted
    (device,) = [d for d in registry.devices if d.serial_number == BOB]
    registry.async_remove_device(device.id)
    said = render(hass, bob)
    assert "<b>Bob is no longer a contact of this radio.</b>" in said
    assert "<summary>Why you cannot write here</summary>" in said
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
    the radio and the minute; its refused line on the delivery event, the two documents and
    the minute: no domain, no state of every entity."""
    from homeassistant.helpers.template import Template  # noqa: PLC0415

    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    documented(hass, HISTORY, [received(10, **to_alice())])
    radio = f"binary_sensor.{PREFIX}_radio"
    delivery = f"event.{PREFIX}_delivery"
    for found, expected in (
        (thread(loaded, "alice"), {HISTORY, PENDING, radio}),  # the time of each message
        (card(loaded, "macro entry(e)"), {HISTORY, PENDING, radio}),
        (page(loaded, "alice")["footer"]["card"]["cards"][0], {HISTORY, PENDING, delivery}),
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
    """Each chat with its newest message, without the delivery code (PC 16), and "new" for a
    message received less than 15 minutes ago, never for one only sent (D-19, HC 22, M54); the
    chats with a message first; Latest chats the three newest, All chats every page; and the
    contacts and channels without a page."""
    loaded = await chats_of(hass, hass_ws_client, tmp_path)
    everything = card(loaded, "Who reads what")
    latest = card(loaded, "./messages")
    documented(
        hass,
        HISTORY,
        [
            final(60, "Reply" + CODE, **to_alice(state="confirmed")),
            received(300, "Hello", **to_alice()),
            received(1200, "Old news", channel_index=2, target="my-target"),
            received(1300, "Stranger", channel_index=5, target=None),
        ],
    )
    documented(hass, PENDING, [on_its_way(30, "Soon" + CODE, **to_alice(target=None))])
    for found in (everything, latest):
        said = render(hass, found)
        assert CODE not in said, said
        first = said.split("</tr>")[0]
        assert '<a href="./contact-alice"><b>Alice</b></a>' in first
        assert "Sent: Soon" in first
        assert " · new</small>" in first  # Hello, 5 minutes ago
        assert " · new" not in said.split("</tr>")[1]  # My target: 20 minutes ago
    documented(hass, HISTORY, [final(60, "Only mine", **to_alice(state="confirmed"))])
    documented(hass, PENDING, [])
    assert " · new" not in render(hass, latest)
    said = render(hass, everything)
    titles = re.findall(r'<a href="\./([\w-]+)"><b>', said)
    assert titles == ["contact-alice", "chat-alerts", "chat-my-target", "contact-bob"]
    assert "<div>Channels heard without a chat page:" not in said
    documented(hass, HISTORY, [received(1300, "Stranger", channel_index=5, target=None)])
    assert "<div>Channels heard without a chat page: 5</div>" in render(hass, everything)
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


# --- long words --------------------------------------------------------------------------------

KEY = "0123456789abcdef" * 4
LINK = "https://example.org/" + "a" * 40 + "?q=1&r=2"


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


# --- the sensors from the gateway's own discovery (stage 2) ----------------------------------


async def test_the_thread_reads_the_history_the_gateway_announces(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The history document on the gateway's history topic, through its own discovery (G1),
    reaches a chat's thread; the messages on their way likewise (G3). Red until the gateway
    announces the two sensors (stage 2)."""
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
    gateway's echo is the field's state, and the footer shows. Red until the gateway announces
    the drafts (stage 2's G4)."""
    from homeassistant.auth.const import GROUP_ID_USER  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import CLIENT_ID  # noqa: PLC0415

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
