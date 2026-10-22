"""MQTT topics and JSON payloads (docs/mqtt-api.md)."""

import json

import pytest

from meshcore_home_gateway.core.commands import Command, CommandDecision, Rejection
from meshcore_home_gateway.core.contacts import Contact
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    TextType,
    split_sender_name,
)
from meshcore_home_gateway.mqtt.payloads import (
    command_payload,
    delivery_payload,
    encode,
    received_payload,
    rx_payload,
)
from meshcore_home_gateway.mqtt.topics import Topics

KEY = "a1" * 32
T0 = 1_800_000_000


def test_topics_hang_below_base_and_node() -> None:
    topics = Topics("mesh/home", "radio1")
    assert topics.availability == "mesh/home/radio1/availability"
    assert topics.state == "mesh/home/radio1/state"
    assert topics.send("alerts") == "mesh/home/radio1/send/alerts"
    assert topics.send_subscription == "mesh/home/radio1/send/+"
    assert topics.received == "mesh/home/radio1/received"
    assert topics.command == "mesh/home/radio1/command"
    assert topics.delivery == "mesh/home/radio1/delivery"
    assert topics.rx == "mesh/home/radio1/rx"


@pytest.mark.parametrize(
    ("topic", "alias"),
    [
        ("mesh/home/radio1/send/alerts", "alerts"),
        ("mesh/home/radio1/send/", None),
        ("mesh/home/radio1/send/a/b", None),
        ("mesh/home/radio2/send/alerts", None),
    ],
)
def test_alias_from_send_topic(topic: str, alias: str | None) -> None:
    assert Topics("mesh/home", "radio1").alias_from_send_topic(topic) == alias


def test_channel_sender_name_is_split_off() -> None:
    assert split_sender_name("Alice: gate open") == ("Alice", "gate open")
    assert split_sender_name("no name here") == (None, "no name here")
    assert split_sender_name(": empty name") == (None, ": empty name")
    assert split_sender_name("x" * 33 + ": too long") == (None, "x" * 33 + ": too long")


def test_received_direct_message_payload() -> None:
    msg = InboundMessage(
        kind=MessageKind.CONTACT,
        text="hello",
        sender_timestamp=T0,
        received_at=T0 + 2.5,
        sender_prefix=KEY[:12],
        snr_db=7.25,
        hops=2,
    )
    payload = received_payload(msg, sender=Contact(KEY, "Alice"), target="alice")
    assert payload == {
        "event_type": "contact",
        "text": "hello",
        "text_type": "plain",
        "sender_key": KEY,
        "sender_prefix": KEY[:12],
        "sender_name": "Alice",
        "target": "alice",
        "channel_index": None,
        "sent_at": "2027-01-15T08:00:00+00:00",
        "received_at": "2027-01-15T08:00:02+00:00",
        "snr_db": 7.25,
        "hops": 2,
        "command": None,
    }


def test_received_channel_message_payload_marks_the_name_as_claimed() -> None:
    msg = InboundMessage(
        kind=MessageKind.CHANNEL,
        text="Bob: rain",
        sender_timestamp=T0,
        received_at=T0,
        channel_index=1,
        text_type=TextType.PLAIN,
    )
    payload = received_payload(msg, sender=None, target="alerts")
    assert payload["event_type"] == "channel"
    assert payload["sender_name"] == "Bob"
    assert payload["text"] == "rain"
    assert payload["sender_key"] is None
    assert payload["channel_index"] == 1


def test_received_payload_reports_a_rejected_command() -> None:
    msg = InboundMessage(
        kind=MessageKind.CONTACT,
        text="arm",
        sender_timestamp=T0,
        received_at=T0,
        sender_prefix=KEY[:12],
    )
    decision = CommandDecision("arm", rejection=Rejection.REPLAY)
    payload = received_payload(msg, sender=None, target=None, decision=decision)
    assert payload["command"] == {"word": "arm", "accepted": False, "reason": "replay"}


def test_command_payload_carries_the_word_as_event_type() -> None:
    command = Command(word="arm", args="now", sender_key=KEY, sender_timestamp=T0)
    payload = command_payload(command, sender_name="Alice", received_at=T0 + 1)
    assert payload == {
        "event_type": "arm",
        "args": "now",
        "sender_key": KEY,
        "sender_name": "Alice",
        "sent_at": "2027-01-15T08:00:00+00:00",
        "received_at": "2027-01-15T08:00:01+00:00",
    }


def test_delivery_payload() -> None:
    event = DeliveryEvent(
        id="7f3a",
        target="alice",
        state=DeliveryState.CONFIRMED,
        text="hi",
        attempt=2,
        rtt_ms=2300,
        airtime_ms=640,
    )
    assert delivery_payload(event) == {
        "event_type": "confirmed", "id": "7f3a", "target": "alice", "text": "hi",
        "attempt": 2, "reason": None, "rtt_ms": 2300, "airtime_ms": 640,
    }  # fmt: skip


def test_rx_payload() -> None:
    assert rx_payload(snr_db=-3.5, rssi_dbm=-112, raw=bytes.fromhex("1122"), received_at=T0) == {
        "snr_db": -3.5, "rssi_dbm": -112, "length": 2, "raw": "1122",
        "received_at": "2027-01-15T08:00:00+00:00",
    }  # fmt: skip


def test_encode_is_compact_json_without_ascii_escaping() -> None:
    assert encode({"text": "Tür"}) == '{"text":"Tür"}'.encode()
    assert json.loads(encode({"a": None})) == {"a": None}
