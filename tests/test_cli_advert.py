"""`meshcore-home-gateway advert` against a broker, with a fake gateway that answers each request
on `advert/send` like the advertiser (ADR 0033); and `listen --topic advert`."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway.cli import EX_CONFIG, EX_FAILURE, EX_OK, main
from tests.conftest import Broker
from tests.test_cli import broker_config

ROOT = "meshcore/radio1"
HOSTILE = "Home\tgate\n\x1b[2J\u202e"


class FakeGateway:
    """Its availability, and an answer to each advert request: an event of another request
    first, then its own (`answer` None: none)."""

    def __init__(
        self,
        broker: Broker,
        *,
        online: bool = True,
        answer: str | None = "sent",
        reason: str | None = None,
        name: str = "Gateway",
    ) -> None:
        self.requests: list[tuple[str, bytes, bool, int]] = []
        self.answer, self.reason, self.name = answer, reason, name
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        ready = threading.Event()
        self.client.on_subscribe = lambda *_: ready.set()
        self.client.on_message = self._on_message
        self.client.connect(broker.host, broker.port)
        self.client.loop_start()
        availability = "online" if online else "offline"
        self.client.publish(
            f"{ROOT}/availability", availability, qos=1, retain=True
        ).wait_for_publish(5)
        self.client.subscribe(f"{ROOT}/advert/send", qos=1)
        assert ready.wait(5)

    def _on_message(self, _c: Any, _u: Any, message: Any) -> None:
        self.requests.append((message.topic, bytes(message.payload), message.retain, message.qos))
        if self.answer is None:
            return
        request = json.loads(message.payload)
        written = self.answer != "refused"
        for event_id, event_type, reason in (
            ("other", "refused", "busy"),
            (request["id"], self.answer, self.reason),
        ):
            body = {
                "event_type": event_type,
                "id": event_id,
                "reason": reason,
                "name": self.name if written and event_id != "other" else None,
                "airtime_ms": 1164 if written and event_id != "other" else None,
                "next_at": "2026-11-13T10:10:00+00:00",
            }
            self.client.publish(f"{ROOT}/advert", json.dumps(body), qos=1)

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def run(tmp_path: Path, broker: Broker, *args: str) -> int:
    return main(["advert", "--config", str(broker_config(tmp_path, broker)), *args])


def retained_requests(broker: Broker) -> list[bytes]:
    """What the broker keeps on `advert/send` for a client that subscribes now: a retained
    request reaches the gateway's live subscription without the retain flag, and again, flagged,
    at each of its reconnects."""
    kept: list[bytes] = []
    subscribed = threading.Event()
    client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    client.on_subscribe = lambda *_: subscribed.set()
    client.on_message = lambda _c, _u, m: kept.append(bytes(m.payload)) if m.retain else None
    client.connect(broker.host, broker.port)
    client.loop_start()
    try:
        client.subscribe(f"{ROOT}/advert/send", qos=1)
        assert subscribed.wait(5)
        time.sleep(0.5)  # a retained message comes right after the subscription
    finally:
        client.disconnect()
        client.loop_stop()
    return kept


@pytest.mark.broker
def test_a_sent_advert_prints_its_event_and_names_the_name(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, name=HOSTILE)
    try:
        code = run(tmp_path, broker)
    finally:
        gateway.stop()
    assert code == EX_OK
    ((topic, payload, retained, qos),) = gateway.requests
    request = json.loads(payload)
    assert (topic, retained, qos) == (f"{ROOT}/advert/send", False, 1)
    assert retained_requests(broker) == []  # not left on the broker
    assert set(request) == {"id"}
    assert re.fullmatch(r"cli-[0-9a-f]{8}", request["id"])
    captured = capsys.readouterr()
    event = json.loads(captured.out)
    assert (event["id"], event["event_type"]) == (request["id"], "sent")  # not the other's
    assert "advert sent with the name" in captured.err
    assert not {"\x1b", "\u202e", "\n"} & set(captured.err.rstrip("\n"))  # shown, not obeyed


@pytest.mark.broker
@pytest.mark.parametrize(
    "case",
    [
        ("refused", "too_soon", "no advert sent (too_soon); docs/mqtt-api.md says why"),
        ("failed", "outcome_unknown", "the radio did not confirm the advert (outcome_unknown)"),
        ("failed", "table_full", "the radio refused the advert (table_full); nothing was sent"),
        ("failed", "disabled", "the radio refused the advert (disabled); nothing was sent"),
    ],
)
def test_a_refused_or_failed_advert_fails_with_its_reason(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str], case: tuple[str, ...]
) -> None:
    answer, reason, said = case
    gateway = FakeGateway(broker, answer=answer, reason=reason)
    try:
        assert run(tmp_path, broker) == EX_FAILURE
    finally:
        gateway.stop()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["event_type"] == answer
    assert said in captured.err


@pytest.mark.broker
def test_nothing_is_sent_to_a_gateway_that_is_offline(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, online=False)
    try:
        assert run(tmp_path, broker) == EX_FAILURE
    finally:
        gateway.stop()
    assert gateway.requests == []
    assert "the gateway is offline" in capsys.readouterr().err


@pytest.mark.broker
def test_without_waiting_or_without_a_result(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, answer=None)
    try:
        assert run(tmp_path, broker, "--wait", "0") == EX_OK
        assert run(tmp_path, broker, "--wait", "0.5") == EX_FAILURE
    finally:
        gateway.stop()
    assert len(gateway.requests) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no result in time" in captured.err
    assert "meshcore-home-gateway listen --topic advert" in captured.err


def test_a_configuration_error_is_78(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[radio]\n", encoding="utf-8")
    assert main(["advert", "--config", str(path)]) == EX_CONFIG
    assert "configuration error" in capsys.readouterr().err


@pytest.mark.parametrize("wait", ["-1", "inf", "nan", "1e9"])
def test_an_impossible_wait_is_a_usage_error(wait: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["advert", "--wait", wait])
    assert exit_info.value.code == 2
    assert "argument --wait" in capsys.readouterr().err


@pytest.mark.broker
@pytest.mark.parametrize("topic", ["advert", "all"])
def test_listen_follows_advert_events(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str], topic: str
) -> None:
    publisher = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect(broker.host, broker.port)
    publisher.loop_start()
    event = {
        "event_type": "refused",
        "id": "x1",
        "reason": "too_soon",
        "name": None,
        "airtime_ms": None,
        "next_at": "2026-11-13T10:10:00+00:00",
    }
    done = threading.Event()

    def publish() -> None:  # once the listener has subscribed: events are not retained
        while not done.wait(0.1):
            publisher.publish(f"{ROOT}/advert", json.dumps(event), qos=1)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        config = str(broker_config(tmp_path, broker))
        assert main(["listen", "--config", config, "--topic", topic, "--count", "1"]) == EX_OK
    finally:
        done.set()
        thread.join()
        publisher.loop_stop()
        publisher.disconnect()
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"topic": f"{ROOT}/advert", "payload": event}
