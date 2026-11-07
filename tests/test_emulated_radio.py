"""tools/emulated_radio.py: the emulated radio as a process with a real gateway on it, for the
browser check (docs/testing.md), and its round trip of a draft through Home Assistant.

The runner's tests start the gateway as a process on a private broker, as `run` does; the round
trip's tests talk to a stand-in for Home Assistant's REST API. The round trip through a real Home
Assistant is the browser check's (docs/testing.md#the-dashboard-in-a-browser).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.core.airtime import direct_attempt_airtime_s, meshcore_lora
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES
from tests.conftest import Broker, free_port

ROOT = Path(__file__).resolve().parent.parent


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "emulated_radio", ROOT / "tools" / "emulated_radio.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["emulated_radio"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()
ROOT_TOPIC = "meshcore/radio1"
HISTORY = f"{ROOT_TOPIC}/history"
PENDING = f"{ROOT_TOPIC}/pending"
DRAFTS = f"{ROOT_TOPIC}/homeassistant/draft/target"


# --- the household ---------------------------------------------------------------------------


def test_the_configuration_is_the_households() -> None:
    config = parse_config(tool.configuration(5000, "127.0.0.1", 1883, node="radio1", language="en"))
    assert {t.alias: t.channel_index for t in config.targets} == {
        "family": 1,
        "alice": None,
        "bob": None,
    }
    keys = {t.alias: t.public_key for t in config.targets if t.public_key}
    radio = tool.household_radio(1_800_000_000)
    names = {c.public_key.hex(): c.name for c in radio.contacts}
    assert {alias: names[key] for alias, key in keys.items()} == {"alice": "Alice", "bob": "Bob"}
    assert radio.channels[1] == "Family"
    assert "Carol" in names.values()  # a companion that is no target: her chat is her device's
    assert (config.radio.transport, config.radio.port) == ("tcp", 5000)
    assert (config.mqtt.host, config.mqtt.port, config.mqtt.node_id) == (
        "127.0.0.1",
        1883,
        "radio1",
    )


def test_a_hundred_of_the_longest_messages_go_out_without_waiting_for_airtime() -> None:
    """The browser check's trials send about a hundred messages in a few minutes: the burst holds
    a hundred of the longest direct messages, each flooded with its path packet back."""
    config = parse_config(tool.configuration(5000, "127.0.0.1", 1883, node="radio1", language="en"))
    radio = tool.household_radio(1_800_000_000)
    lora = meshcore_lora(62_500, radio.spreading_factor, radio.coding_rate)
    longest = direct_attempt_airtime_s(MAX_TEXT_BYTES, 0, True, lora)
    assert 100 * longest <= config.airtime.burst_s
    assert config.airtime.budget_s_per_hour >= config.airtime.burst_s  # refilled within the hour


def test_every_direct_message_is_acknowledged_as_over_its_path() -> None:
    """Each direct message gets its acknowledgement, after a round trip a genuine one can take
    (the outbox calls a sooner one suspect), so no trial waits for a retry."""
    radio = tool.household_radio(1_800_000_000)
    assert radio.auto_ack
    assert radio.ack_round_trip_ms >= 1500


def test_with_stuck_the_budget_holds_messages_for_long_and_none_expires() -> None:
    config = parse_config(
        tool.configuration(5000, "127.0.0.1", 1883, node="radio1", language="en", stuck=True)
    )
    radio = tool.household_radio(1_800_000_000)
    lora = meshcore_lora(62_500, radio.spreading_factor, radio.coding_rate)
    longest = direct_attempt_airtime_s(MAX_TEXT_BYTES, 0, True, lora)
    wait_s = longest / (config.airtime.budget_s_per_hour / 3600)  # once the burst is spent
    assert wait_s > 30 * 60  # the browser check's run sees them waiting
    assert wait_s < config.messages.queue_ttl_s  # and none expires at once


# --- the runner on a broker -------------------------------------------------------------------


class Watcher:
    """The broker as Home Assistant sees it: every message of the gateway's topics."""

    def __init__(self, broker: Broker) -> None:
        self.messages: list[tuple[str, bytes]] = []
        self._lock = threading.Lock()
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.client.on_message = self._on_message
        self.client.connect(broker.host, broker.port)
        self.client.subscribe(f"{ROOT_TOPIC}/#", 1)
        self.client.loop_start()

    def _on_message(self, _c: Any, _u: Any, m: Any) -> None:
        with self._lock:
            self.messages.append((m.topic, bytes(m.payload)))

    def raw(self, topic: str) -> bytes | None:
        with self._lock:
            payloads = [p for t, p in self.messages if t == topic]
        return payloads[-1] if payloads else None

    def last(self, topic: str) -> Any:
        payload = self.raw(topic)
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return payload.decode()  # availability: online or offline

    def wait(self, topic: str, check: Callable[[Any], bool], timeout: float = 15) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.last(topic)
            if value is not None and check(value):
                return value
            time.sleep(0.05)
        raise AssertionError(f"not on {topic} within {timeout} s: {self.last(topic)!r}")

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.client.publish(topic, json.dumps(payload), qos=0).wait_for_publish(5)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class Runner:
    """`run` in a thread, as Ctrl-C would end it: `stop()` sets its event."""

    def __init__(self, *argv: str) -> None:
        self.lines: list[str] = []
        self.ready = threading.Event()
        self._stop = threading.Event()
        self.result: list[int] = []
        self.args = tool.parser().parse_args(["run", *argv])
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _say(self, line: str) -> None:
        self.lines.append(line)
        if line.startswith("ready"):
            self.ready.set()

    def _run(self) -> None:
        self.result.append(tool.serve(self.args, self._stop, self._say))
        self.ready.set()  # also when it gave up

    def start(self) -> None:
        self._thread.start()
        assert self.ready.wait(90), self.lines
        assert not self.result, self.lines

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(120)
        assert self.result, f"still running: {self.lines}"
        return self.result[0]


@pytest.fixture
def watcher(broker: Broker) -> Iterator[Watcher]:
    watching = Watcher(broker)
    try:
        yield watching
    finally:
        watching.close()


def texts(document: dict[str, Any], text: str) -> list[dict[str, Any]]:
    return [m for m in document["messages"] if m["text"] == text]


@pytest.mark.broker
def test_the_runner_fills_a_real_gateway_and_a_draft_goes_out_once(
    tmp_path: Path, broker: Broker, watcher: Watcher
) -> None:
    work = tmp_path / "work"
    runner = Runner(
        "--mqtt-host", broker.host, "--mqtt-port", str(broker.port), "--work", str(work)
    )
    runner.start()
    try:  # this client may hear a message a moment after the runner did: it waits for each
        history = watcher.wait(HISTORY, lambda d: d["total"] == 5)
        seen = {m["text"]: m for m in history["messages"]}
        assert {(seen[t]["direction"], seen[t]["name"], seen[t]["target"]) for t in seen} == {
            ("in", "Alice", "alice"),
            ("in", "Bob", "bob"),
            ("in", "Carol", "family"),
            ("out", "family", "family"),
            ("out", "Alice", "alice"),
        }
        assert seen["Dinner is ready"]["state"] == "sent"
        assert (seen["Back soon"]["state"], seen["Back soon"]["suspect"]) == ("confirmed", False)
        watcher.wait(f"{DRAFTS}/bob", lambda d: d == {"text": "See you at 7"})
        watcher.wait(PENDING, lambda d: d["total"] == 0)

        # the round trip as Home Assistant's text and Send button make it: one request topic
        watcher.publish(f"{DRAFTS}/family/request", {"text": "Round trip 1"})
        watcher.publish(f"{DRAFTS}/family/request", {"send": True})
        watcher.wait(HISTORY, lambda d: bool(texts(d, "Round trip 1")))
        time.sleep(1)
        assert len(texts(watcher.last(HISTORY), "Round trip 1")) == 1
        assert texts(watcher.last(PENDING), "Round trip 1") == []
        assert watcher.last(f"{DRAFTS}/family") == {"text": ""}
    finally:
        result = runner.stop()
    assert result == 0, runner.lines
    watcher.wait(f"{DRAFTS}/bob", lambda d: d == {"text": ""})  # a clean stop empties drafts
    watcher.wait(f"{ROOT_TOPIC}/availability", lambda a: a == "offline")
    assert (work / "config.toml").is_file()
    assert (work / "state" / "history.sqlite3").is_file()
    log = (work / "gateway.log").read_text(encoding="utf-8")
    assert "Traceback" not in log
    assert any(str(work / "gateway.log") in line for line in runner.lines)


@pytest.mark.broker
def test_with_stuck_the_last_messages_stay_on_their_way(
    tmp_path: Path, broker: Broker, watcher: Watcher
) -> None:
    """As behind a radio that used up its airtime: queued, not sent, and they stay."""
    runner = Runner("--mqtt-host", broker.host, "--mqtt-port", str(broker.port), "--stuck")
    runner.start()
    try:
        pending = watcher.wait(PENDING, lambda d: d["total"] >= 2)
        time.sleep(1)
        pending = watcher.last(PENDING)  # settled
        assert {m["step"] for m in pending["messages"]} == {"queued"}
        assert {m["target"] for m in pending["messages"]} == {"family"}
        time.sleep(1)
        assert watcher.last(PENDING) == pending  # they stay
        assert {"Dinner is ready", "Back soon"} <= {
            m["text"] for m in watcher.last(HISTORY)["messages"]
        }
    finally:
        assert runner.stop() == 0, runner.lines


@pytest.mark.broker
def test_a_work_folder_that_holds_something_is_refused(tmp_path: Path, broker: Broker) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "config.toml").write_text("", encoding="utf-8")
    args = tool.parser().parse_args(
        ["run", "--mqtt-port", str(broker.port), "--work", str(tmp_path / "work")]
    )
    lines: list[str] = []
    stop = threading.Event()
    stop.set()  # were the folder taken, the run would end at once
    assert tool.serve(args, stop, lines.append) == 2
    assert lines == [f"the work folder {tmp_path / 'work'} is not empty: name a new one"]


def test_without_a_broker_it_says_so_and_starts_nothing(tmp_path: Path) -> None:
    args = tool.parser().parse_args(["run", "--mqtt-port", str(free_port())])
    lines: list[str] = []
    assert tool.serve(args, threading.Event(), lines.append) == 1
    assert len(lines) == 1
    assert lines[0].startswith("no broker on 127.0.0.1:")


@pytest.mark.broker
def test_a_gateway_whose_radio_never_connects_ends_the_run(
    tmp_path: Path, broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The radio answers nothing: the gateway never reports it connected, and the runner gives
    up after --timeout, stopping the gateway it started."""
    args = tool.parser().parse_args(
        ["run", "--mqtt-port", str(broker.port), "--timeout", "3", "--work", str(tmp_path / "w")]
    )
    lines: list[str] = []
    original = tool.household_radio

    def deaf(now: int) -> Any:
        radio = original(now)
        radio.silent = set(range(256))
        return radio

    monkeypatch.setattr(tool, "household_radio", deaf)
    started = time.monotonic()
    assert tool.serve(args, threading.Event(), lines.append) == 1
    assert time.monotonic() - started < 60
    assert any(line.startswith("gave up waiting for the radio connected") for line in lines), lines


@pytest.mark.broker
def test_a_gateway_that_cannot_start_ends_the_run_at_once(tmp_path: Path, broker: Broker) -> None:
    """A configuration the gateway refuses (a node ID with a slash): it exits, and the runner
    says so without waiting out --timeout."""
    args = tool.parser().parse_args(
        ["run", "--mqtt-port", str(broker.port), "--node", "radio/1", "--work", str(tmp_path / "w")]
    )
    lines: list[str] = []
    started = time.monotonic()
    assert tool.serve(args, threading.Event(), lines.append) == 1
    assert time.monotonic() - started < 15  # --timeout is 30
    assert any(line.startswith("gave up waiting for the gateway online") for line in lines), lines
    assert "radio/1" in (tmp_path / "w" / "gateway.log").read_text(encoding="utf-8")


@pytest.mark.broker
def test_a_gateway_that_ends_with_an_error_fails_the_run(
    tmp_path: Path, broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run passes only when the gateway stopped cleanly, as SIGTERM asks it to."""
    stopping = tool._stop

    def failing(gateway: Any) -> int:
        stopping(gateway)
        return 3

    monkeypatch.setattr(tool, "_stop", failing)
    runner = Runner("--mqtt-port", str(broker.port), "--work", str(tmp_path / "w"))
    runner.start()
    assert runner.stop() == 1
    assert f"the gateway ended with 3; see {tmp_path / 'w' / 'gateway.log'}" in runner.lines
    assert not any(line.startswith("stopped") for line in runner.lines)


# --- the round trip through Home Assistant ------------------------------------------------------

USER, SECRET, TOKEN = "household", "correct horse", "household-token"
TEXT = "text.meshcore_home_gateway_radio1_write_family"
BUTTON = "button.meshcore_home_gateway_radio1_write_family_send"
HISTORY_SENSOR = "sensor.meshcore_home_gateway_radio1_message_history"
PENDING_SENSOR = "sensor.meshcore_home_gateway_radio1_pending_messages"


class FakeHomeAssistant(ThreadingHTTPServer):
    """Home Assistant's login flow and REST API, with the gateway behind its entities: a press
    sends the draft (`copies` times into the history) and empties it."""

    def __init__(self, copies: int = 1) -> None:
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.copies = copies
        self.draft = ""
        self.history: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.stays_pending = False  # the gateway publishes the pending without it: never
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.entities = {TEXT, BUTTON, HISTORY_SENSOR, PENDING_SENSOR}
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def state(self, entity_id: str) -> dict[str, Any]:
        if entity_id == TEXT:
            return {"entity_id": TEXT, "state": self.draft, "attributes": {"max": 158}}
        if entity_id == HISTORY_SENSOR:
            messages = self.history[::-1]
            return {"state": "x", "attributes": {"total": len(messages), "messages": messages}}
        if entity_id == PENDING_SENSOR:
            pending = list(self.pending)
            return {"state": str(len(pending)), "attributes": {"messages": pending}}
        return {"entity_id": entity_id, "state": "unknown", "attributes": {}}

    def call(self, service: str, data: dict[str, Any], authorization: str) -> None:
        self.calls.append((service, data, authorization))
        if service == "text/set_value":
            self.draft = data["value"]
        elif service == "button/press" and self.draft:
            entry = {"direction": "out", "target": "family", "text": self.draft, "state": "sent"}
            self.history += [dict(entry) for _ in range(self.copies)]
            if self.stays_pending:
                self.pending.append({"text": self.draft, "step": "sent"})
            self.draft = ""


class FakeHandler(BaseHTTPRequestHandler):
    server: FakeHomeAssistant

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
        pass

    def _answer(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._answer(401, {"message": "Unauthorized"})
        return False

    def do_GET(self) -> None:
        entity = self.path.removeprefix("/api/states/")
        if not self._authorized():
            return
        if entity not in self.server.entities:
            self._answer(404, {"message": "Entity not found."})
            return
        self._answer(200, self.server.state(entity))

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/auth/login_flow":
            request = json.loads(body)
            assert request["handler"] == ["homeassistant", None]
            self._answer(200, {"type": "form", "flow_id": "flow1", "step_id": "init"})
        elif self.path == "/auth/login_flow/flow1":
            request = json.loads(body)
            if (request["username"], request["password"]) == (USER, SECRET):
                self._answer(200, {"type": "create_entry", "result": "code1"})
            else:
                self._answer(200, {"type": "form", "errors": {"base": "invalid_auth"}})
        elif self.path == "/auth/token":
            form = parse_qs(body.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == ["code1"]
            self._answer(200, {"access_token": TOKEN, "token_type": "Bearer"})
        elif self.path.startswith("/api/services/"):
            if self._authorized():
                service = self.path.removeprefix("/api/services/")
                self.server.call(service, json.loads(body), self.headers["Authorization"])
                self._answer(200, [])
        else:
            self._answer(404, {"message": "not found"})


@pytest.fixture
def home_assistant() -> Iterator[FakeHomeAssistant]:
    server = FakeHomeAssistant()
    server.thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def round_trip(
    url: str, monkeypatch: pytest.MonkeyPatch, *extra: str, secret: str = SECRET
) -> tuple[int, list[str]]:
    monkeypatch.setenv("HA_PASSWORD", secret)
    args = tool.parser().parse_args(
        ["round-trip", "--url", url, "--user", USER, "--settle", "0.3", "--timeout", "2", *extra]
    )
    lines: list[str] = []
    return tool.round_trip(args, lines.append), lines


def test_a_draft_sent_from_home_assistant_ends_once_in_the_history(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, lines = round_trip(home_assistant.url, monkeypatch)
    assert result == 0, lines
    (set_value, set_data, auth), (press, press_data, _) = home_assistant.calls
    assert (set_value, press) == ("text/set_value", "button/press")  # the text first, then Send
    assert set_data["entity_id"] == TEXT
    assert press_data == {"entity_id": BUTTON}
    assert auth == f"Bearer {TOKEN}"  # as the user given
    assert home_assistant.history[0]["text"] == set_data["value"]
    assert lines[-1] == "round trip: passed"
    assert all(SECRET not in line for line in lines)


def test_each_round_trip_writes_a_text_of_its_own(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(2):
        assert round_trip(home_assistant.url, monkeypatch)[0] == 0
    first, second = (
        data["value"] for service, data, _ in home_assistant.calls if "text" in service
    )
    assert first != second


def test_a_text_that_arrives_twice_fails_the_round_trip(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_assistant.copies = 2
    result, lines = round_trip(home_assistant.url, monkeypatch)
    assert result == 1
    assert "2 time(s) in the history" in "\n".join(lines)
    assert lines[-1] == "round trip: failed"


def test_a_text_that_never_arrives_fails_the_round_trip(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_assistant.copies = 0
    started = time.monotonic()
    result, lines = round_trip(home_assistant.url, monkeypatch)
    assert result == 1
    assert time.monotonic() - started < 10
    assert "not in the history within 2 s" in "\n".join(lines)


def test_a_text_still_on_its_way_fails_the_round_trip(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final in the history and yet on its way: the pending without it never came."""
    home_assistant.stays_pending = True
    result, lines = round_trip(home_assistant.url, monkeypatch)
    assert result == 1
    assert "1 time(s) in the history, 1 on its way" in lines[0]
    assert lines[-1] == "round trip: failed"


def test_a_draft_that_stays_in_the_field_fails_the_round_trip(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press that sent the text empties the draft, which the field shows."""
    original = home_assistant.call

    def keeping(service: str, data: dict[str, Any], authorization: str) -> None:
        draft = home_assistant.draft
        original(service, data, authorization)
        if service == "button/press":
            home_assistant.draft = draft

    monkeypatch.setattr(home_assistant, "call", keeping)
    result, lines = round_trip(home_assistant.url, monkeypatch)
    assert result == 1
    assert lines[-1] == "round trip: failed"


def test_a_chat_without_its_draft_ends_the_round_trip(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, lines = round_trip(home_assistant.url, monkeypatch, "--chat", "carol")
    assert result == 1
    assert lines == [
        "round trip: no text.meshcore_home_gateway_radio1_write_carol in Home Assistant"
    ]
    assert home_assistant.calls == []


def test_a_refused_login_ends_the_round_trip(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, lines = round_trip(home_assistant.url, monkeypatch, secret="wrong")
    assert result == 1
    assert lines == ["round trip: login as household failed: invalid_auth"]
    assert home_assistant.calls == []


def test_the_password_comes_from_the_environment_or_a_file(
    home_assistant: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "password"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    result, lines = round_trip(
        home_assistant.url, monkeypatch, "--password-file", str(secret_file), secret="wrong"
    )
    assert result == 0, lines
    monkeypatch.delenv("HA_PASSWORD")
    args = tool.parser().parse_args(["round-trip", "--user", USER])
    with pytest.raises(SystemExit, match="no password: set HA_PASSWORD or give --password-file"):
        tool.round_trip(args, print)
    with pytest.raises(SystemExit):  # never on the command line, where `ps` shows it
        tool.parser().parse_args(["round-trip", "--user", USER, "--password", SECRET])


def test_only_a_web_address_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    result, lines = round_trip("file:///etc/passwd", monkeypatch)
    assert result == 1
    assert lines == ["round trip: not an http or https address: 'file:///etc/passwd'"]


def test_a_node_and_an_alias_with_a_hyphen_name_the_entities_with_an_underscore() -> None:
    """Home Assistant makes a hyphen of an object ID an underscore in the entity ID."""
    chat = tool.chat_entities("radio-1", "dog-walk")
    assert chat.text == "text.meshcore_home_gateway_radio_1_write_dog_walk"
    assert chat.button == "button.meshcore_home_gateway_radio_1_write_dog_walk_send"
    assert chat.history == "sensor.meshcore_home_gateway_radio_1_message_history"
    assert chat.pending == "sensor.meshcore_home_gateway_radio_1_pending_messages"


def test_the_help_names_both_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        tool.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "{run,round-trip}" in out
