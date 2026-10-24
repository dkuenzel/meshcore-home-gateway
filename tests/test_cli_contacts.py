"""`meshcore-home-gateway contacts` against a broker, with a fake gateway that publishes its
retained documents and answers requests like the bridge (ADR 0017)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway.cli import EX_FAILURE, EX_OK, EX_USAGE, main
from tests.conftest import Broker
from tests.test_cli import broker_config

ROOT = "meshcore/radio1"
DAVE, ERIN = "d4" * 32, "e5" * 32
DAVE_LABEL = f"{DAVE[:12]}…{DAVE[-8:]}"
HOSTILE = "Mal\tlory\n\x1b[2J\u202e"


def contacts_index(**extra: Any) -> dict[str, Any]:
    return {
        "total": 2, "manage": True, "truncated": False,
        "contacts": [
            {"key": "a1" * 32, "name": "Alice", "type": "chat", "heard_at": None,
             "protected": True},
            {"key": ERIN, "name": HOSTILE, "type": "room", "heard_at": "2027-01-15T08:00:00+00:00",
             "protected": False},
        ],
        **extra,
    }  # fmt: skip


def nodes_document(*keys: str, truncated: bool = False) -> dict[str, Any]:
    return {
        "total": len(keys), "truncated": truncated, "look_alike": [], "look_alike_total": 0,
        "nodes": [{"key": k, "name": f"Node {k[:2]}", "type": "chat", "look_alike": False,
                   "heard_at": None} for k in keys],
    }  # fmt: skip


class FakeGateway:
    """Retained documents, and an answer to each contact request: events of other requests
    first, then its own."""

    def __init__(
        self, broker: Broker, *, online: bool = True, answer: str | None = "ok",
        documents: dict[str, Any] | None = None,
    ) -> None:  # fmt: skip
        self.requests: list[tuple[str, bytes]] = []
        self.answer = answer
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        ready = threading.Event()
        self.client.on_subscribe = lambda *_: ready.set()
        self.client.on_message = self._on_message
        self.client.connect(broker.host, broker.port)
        self.client.loop_start()
        retained = {
            "availability": "online" if online else "offline", "nodes": nodes_document(),
            **(documents or {}),
        }  # fmt: skip
        for name, document in retained.items():
            body = document if isinstance(document, str) else json.dumps(document)
            self.client.publish(f"{ROOT}/{name}", body, qos=1, retain=True).wait_for_publish(5)
        self.client.subscribe([(f"{ROOT}/add_contact", 1), (f"{ROOT}/contacts/+/remove", 1)])
        assert ready.wait(5)

    def _on_message(self, _c: Any, _u: Any, message: Any) -> None:
        self.requests.append((message.topic, bytes(message.payload)))
        if self.answer is None:
            return
        request = json.loads(message.payload)
        kind = "add" if message.topic.endswith("add_contact") else "remove"
        key = request.get("contact", DAVE) if kind == "add" else message.topic.split("/")[-2]
        key = key if len(key) == 64 else DAVE
        done = {"add": "added", "remove": "removed"}[kind] if self.answer == "ok" else "refused"
        for event_id, event_type in (("other", "added"), (request["id"], done)):
            body = {"event_type": event_type, "key": key, "prefix": key[:12], "name": "Dave",
                    "type": "chat", "request": kind, "id": event_id,
                    "reason": None if done != "refused" else "look_alike"}  # fmt: skip
            self.client.publish(f"{ROOT}/contact", json.dumps(body), qos=1)

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def run(tmp_path: Path, broker: Broker, *args: str) -> int:
    return main(["contacts", args[0], "--config", str(broker_config(tmp_path, broker)), *args[1:]])


@pytest.mark.broker
def test_add_by_key_names_the_full_key_and_waits_for_its_own_result(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker)
    try:
        code = run(tmp_path, broker, "add", DAVE.upper())
    finally:
        gateway.stop()
    assert code == EX_OK
    ((topic, payload),) = gateway.requests
    request = json.loads(payload)
    assert topic == f"{ROOT}/add_contact"
    assert request["contact"] == DAVE
    assert request["id"].startswith("cli-")
    captured = capsys.readouterr()
    assert json.loads(captured.out)["id"] == request["id"]  # not the other request's
    assert "key d4d4 d4d4 d4d4" in captured.err


@pytest.mark.broker
def test_add_a_heard_node_by_its_label(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, documents={"nodes": nodes_document(DAVE, ERIN)})
    try:
        assert run(tmp_path, broker, "add", DAVE_LABEL) == EX_OK
        assert run(tmp_path, broker, "add", DAVE_LABEL.replace("…", "...")) == EX_OK
    finally:
        gateway.stop()
    assert [json.loads(p)["contact"] for _, p in gateway.requests] == [DAVE, DAVE]
    assert "'Node d4'" in capsys.readouterr().err


@pytest.mark.broker
@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (nodes_document(ERIN), "no node d4d4d4d4d4d4…d4d4d4d4"),
        (nodes_document(ERIN, truncated=True), "the published list is cut: give the full key"),
        (nodes_document(DAVE, "d4" * 6 + "00" * 22 + "d4" * 4), "2 keys match"),
    ],
)
def test_a_label_must_name_one_heard_node(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str],
    nodes: dict[str, Any], message: str,
) -> None:  # fmt: skip
    gateway = FakeGateway(broker, documents={"nodes": nodes})
    try:
        assert run(tmp_path, broker, "add", DAVE_LABEL) == EX_USAGE
    finally:
        gateway.stop()
    assert gateway.requests == []
    assert message in capsys.readouterr().err


@pytest.mark.broker
@pytest.mark.parametrize("text", [DAVE[:12], DAVE[:63], "hello", "meshcore://channel/add?x=1"])
def test_a_prefix_or_bad_text_publishes_nothing(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    """A 12-digit prefix is what an attacker's key can match (ADR 0009): never resolved."""
    gateway = FakeGateway(broker)
    try:
        assert run(tmp_path, broker, "add", text) == EX_USAGE
    finally:
        gateway.stop()
    assert gateway.requests == []


@pytest.mark.broker
def test_a_link_is_passed_on_and_a_refusal_fails(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    link = f"meshcore://contact/add?name=Dave&public_key={DAVE}&type=1"
    gateway = FakeGateway(broker, answer="refused")
    try:
        assert run(tmp_path, broker, "add", link) == EX_FAILURE
    finally:
        gateway.stop()
    assert json.loads(gateway.requests[0][1])["contact"] == link
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason"] == "look_alike"
    assert "'Dave' (claimed)" in captured.err


@pytest.mark.broker
def test_nothing_is_sent_to_a_gateway_that_is_offline(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, online=False)
    try:
        assert run(tmp_path, broker, "add", DAVE) == EX_FAILURE
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
        assert run(tmp_path, broker, "add", "--wait", "0", DAVE) == EX_OK
        assert run(tmp_path, broker, "add", "--wait", "0.5", DAVE) == EX_FAILURE
    finally:
        gateway.stop()
    assert len(gateway.requests) == 2
    assert "no result in time" in capsys.readouterr().err


@pytest.mark.broker
def test_remove_by_key_or_label(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, documents={"contacts": contacts_index()})
    try:
        assert run(tmp_path, broker, "remove", ERIN.upper()) == EX_OK
        assert run(tmp_path, broker, "remove", f"{ERIN[:12]}...{ERIN[-8:]}") == EX_OK
        assert run(tmp_path, broker, "remove", ERIN[:12]) == EX_USAGE
    finally:
        gateway.stop()
    assert [t for t, _ in gateway.requests] == [f"{ROOT}/contacts/{ERIN}/remove"] * 2
    assert set(json.loads(gateway.requests[0][1])) == {"id"}
    err = capsys.readouterr().err
    assert "the radio drops its direct messages" in err
    assert "\x1b" not in err  # a hostile name, made harmless


@pytest.mark.broker
def test_list_shows_contacts_and_nodes_with_harmless_names(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, documents={
        "contacts": contacts_index(), "nodes": nodes_document(DAVE),
    })  # fmt: skip
    try:
        assert run(tmp_path, broker, "list") == EX_OK
        lines = capsys.readouterr().out.splitlines()
        assert run(tmp_path, broker, "list", "--json") == EX_OK
        documents = json.loads(capsys.readouterr().out)
    finally:
        gateway.stop()
    assert lines[0] == "contact\t" + "a1" * 32 + "\tchat\tprotected\t-\tAlice"
    assert lines[1].split("\t")[:5] == [
        "contact", ERIN, "room", "removable", "2027-01-15T08:00:00+00:00",
    ]  # fmt: skip
    assert len(lines[1].split("\t")) == 6  # the name's tab and line break are spaces
    assert "\x1b" not in lines[1]
    assert "\u202e" not in lines[1]
    assert lines[2] == f"node\t{DAVE}\tchat\t-\t-\tNode d4"
    assert set(documents) == {"contacts", "nodes"}


@pytest.mark.broker
def test_link_prints_the_gateways_contact_link(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    link = f"meshcore://contact/add?name=Gateway&public_key={DAVE}&type=1"
    gateway = FakeGateway(broker, documents={"state": {"radio": "connected", "contact_link": link}})
    try:
        assert run(tmp_path, broker, "link") == EX_OK
    finally:
        gateway.stop()
    assert capsys.readouterr().out.strip() == link


@pytest.mark.broker
def test_link_fails_before_the_radio_is_known(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, documents={"state": {"radio": "disconnected"}})
    try:
        assert run(tmp_path, broker, "link") == EX_FAILURE
    finally:
        gateway.stop()
    assert "no contact link" in capsys.readouterr().err


@pytest.mark.broker
def test_listen_follows_contact_events(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    publisher = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect(broker.host, broker.port)
    publisher.loop_start()
    event = {"event_type": "refused", "key": None, "reason": "invalid_link"}
    done = threading.Event()

    def publish() -> None:  # once the listener has subscribed: events are not retained
        while not done.wait(0.1):
            publisher.publish(f"{ROOT}/contact", json.dumps(event), qos=1)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        config = str(broker_config(tmp_path, broker))
        assert main(["listen", "--config", config, "--topic", "contact", "--count", "1"]) == EX_OK
    finally:
        done.set()
        thread.join()
        publisher.loop_stop()
        publisher.disconnect()
    assert json.loads(capsys.readouterr().out) == {"topic": f"{ROOT}/contact", "payload": event}


def test_check_config_names_contact_management_and_protected_look_alikes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        f'[targets.one]\ncontact = "{"b2" * 32}"\n'
        f'[commands]\nsenders = ["{"b2" * 6 + "00" * 26}"]\nwords = ["status"]\n'
        "[contacts]\nmanage = false\n"
    )
    assert main(["check-config", "--config", str(config)]) == EX_OK  # a warning, no error
    captured = capsys.readouterr()
    assert "  contacts   not managed\n" in captured.out
    assert ("b2b2b2b2b2b2…00000000 (command sender) and b2b2b2b2b2b2…b2b2b2b2 (target one) "
            "have the same first 6 bytes") in captured.err  # fmt: skip
    assert "adds b2b2b2b2b2b2…00000000 unless the other is on the radio" in captured.err
    config.write_text(config.read_text() + '[radio.profile]\nmanage = "observe"\n')
    assert main(["check-config", "--config", str(config)]) == EX_OK
    assert "the gateway adds neither" in capsys.readouterr().err


def test_a_contact_that_is_not_valid_text_is_refused_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bytes that are not UTF-8 reach argv as lone surrogates (PEP 383)."""
    config = tmp_path / "config.toml"
    config.write_text('[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n')
    link = "meshcore://contact/add?name=B\udce4r&public_key=" + DAVE
    assert main(["contacts", "add", "--config", str(config), link]) == EX_USAGE
    assert "not valid Unicode" in capsys.readouterr().err


@pytest.mark.broker
@pytest.mark.parametrize("case", ["flagged", "cut from the list", "a heard twin"])
def test_adding_a_look_alike_warns(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    nodes = nodes_document(DAVE)
    if case == "flagged":
        nodes["nodes"][0]["look_alike"] = True
    elif case == "cut from the list":
        nodes = {**nodes_document(), "truncated": True, "look_alike": [DAVE]}
    else:
        nodes = nodes_document("d4" * 6 + "00" * 26)  # not Dave: another key with his prefix
    gateway = FakeGateway(broker, documents={"nodes": nodes})
    try:
        assert run(tmp_path, broker, "add", DAVE) == EX_OK
    finally:
        gateway.stop()
    assert "another node has the same first 12 key digits" in capsys.readouterr().err


@pytest.mark.broker
def test_list_says_when_a_document_is_cut(
    tmp_path: Path, broker: Broker, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(broker, documents={
        "contacts": contacts_index(truncated=True, total=300), "nodes": nodes_document(DAVE),
    })  # fmt: skip
    try:
        assert run(tmp_path, broker, "list") == EX_OK
    finally:
        gateway.stop()
    assert "the gateway lists 2 of 300 contacts" in capsys.readouterr().err
