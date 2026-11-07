"""The repository's emulated radio as a process, with a real gateway on it, for the browser check.

`run` serves the emulated radio of tests/radio_emulator.py over TCP, as a WiFi companion does,
starts `meshcore-home-gateway run` on it and on a broker, and gives the gateway a household to
show: the targets family (a channel), alice and bob (companions), the companion Carol, who is no
target, three received and two sent messages in its history, and a draft in Bob's chat. The radio
acknowledges every direct message at once, with a round trip a genuine acknowledgement can take,
and the airtime budget is an hour's worth at once, so that a hundred messages go out within a few
minutes without waiting for airtime or a retry. With --stuck the budget is a second an hour
instead, and the last messages to family stay on their way (`pending`), as behind a radio that
used up its airtime. It runs until Ctrl-C or SIGTERM; then it stops the gateway, which empties its
drafts, and the radio.

    .venv/bin/python tools/emulated_radio.py run --mqtt-port 1883 --work build/emulated-radio

The broker is the one Home Assistant uses; the gateway connects to it without a user name, so
it must allow that (a test broker on the loopback). The work folder, new or empty (a temporary
one without --work, removed at the end), holds the gateway's configuration, its state and its
log, gateway.log. The dashboard comes from them:

    .venv/bin/meshcore-home-gateway dashboard --config build/emulated-radio/config.toml \\
        --state-dir build/emulated-radio/state > mesh-radio1.yaml

`round-trip` writes a text into a chat's draft through Home Assistant's REST API, as the user
given, who need not be an administrator, presses the chat's Send button, and waits until the
text is in the gateway's message history: once, no longer on its way, and the draft empty again.
The password comes from the environment variable HA_PASSWORD or from --password-file, never from
the command line. It prints what it found and exits with 1 when the text did not arrive once:

    .venv/bin/python tools/emulated_radio.py round-trip --url http://127.0.0.1:8123 \\
        --user household --chat family
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _folder in (ROOT, ROOT / "src"):  # the emulated radio lives with the tests
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))

import paho.mqtt.client as paho  # noqa: E402
from paho.mqtt.enums import CallbackAPIVersion  # noqa: E402
from tests.radio_emulator import (  # noqa: E402
    EmulatedContact,
    EmulatedRadio,
    RemoteCompanion,
    TcpRadioServer,
)

from meshcore_home_gateway.core.airtime import (  # noqa: E402
    channel_message_bytes,
    meshcore_lora,
    time_on_air_s,
)
from meshcore_home_gateway.mqtt.topics import Topics  # noqa: E402

Say = Callable[[str], None]

NODE = "radio1"
BASE_TOPIC = "meshcore"  # the gateway's default
RADIO_NAME = "Home"
ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
BOB = EmulatedContact(bytes.fromhex("b2" * 32), "Bob")
CAROL = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")  # no target: her chat is her device's
CHANNELS = {0: "Public", 1: "Family"}
FAMILY_CHANNEL = 1
TARGETS = (
    ("family", f"channel = {FAMILY_CHANNEL}"),
    ("alice", f'contact = "{ALICE.public_key.hex()}"'),
    ("bob", f'contact = "{BOB.public_key.hex()}"'),
)
RECEIVED = (  # who, what, how long ago
    (ALICE, "Are you home?", 300),
    (BOB, "On my way", 240),
)
CHANNEL_RECEIVED = ("Carol", "Dinner at 7?", 180)
SENT = (("family", "Dinner is ready"), ("alice", "Back soon"))
DRAFT = ("bob", "See you at 7")
REMINDER = "Water the plants ({n})"  # --stuck: the messages that stay on their way

# An hour's airtime at once: a hundred of the longest direct messages take about 220 s.
PLENTY_S = 3600.0
# --stuck: a second an hour, from the configuration's least burst. Once the burst is spent, a
# short channel message waits about half an hour for its airtime, within the queue's lifetime.
STUCK_BUDGET_S = 1.0
STUCK_BURST_S = 5.0
STUCK_TTL_S = 86_400.0
ACK_ROUND_TRIP_MS = 1500  # as over the path: the outbox does not call it suspect

TIMEOUT_S = 30.0
STOP_S = 90.0  # the gateway waits up to a minute for an acknowledgement in flight
SETTLE_S = 2.0  # after the last request of the stuck messages: until the queue settles


class GaveUpError(Exception):
    """A wait for the gateway that ran out."""


def household_radio(now: int) -> EmulatedRadio:
    """The emulated radio with the household's contacts and channels; its clock is `now`."""
    contacts = [
        EmulatedContact(c.public_key, c.name, last_advert=now - 3600, last_modified=now - 60)
        for c in (ALICE, BOB, CAROL)
    ]
    return EmulatedRadio(
        name=RADIO_NAME,
        clock=now,
        contacts=contacts,
        channels=dict(CHANNELS),
        remotes={ALICE.public_key: RemoteCompanion()},  # Alice answers telemetry
        auto_ack=True,
        ack_round_trip_ms=ACK_ROUND_TRIP_MS,
        description="the browser check's emulated radio",
    )


def configuration(
    radio_port: int,
    mqtt_host: str,
    mqtt_port: int,
    *,
    node: str,
    language: str,
    stuck: bool = False,
) -> str:
    """The gateway's configuration file for the household's radio on `radio_port`."""
    budget, burst = (STUCK_BUDGET_S, STUCK_BURST_S) if stuck else (PLENTY_S, PLENTY_S)
    lines = [
        "# made by tools/emulated_radio.py for the emulated radio (docs/testing.md)",
        "[radio]",
        'transport = "tcp"',
        "tcp_unauthenticated = true",
        'host = "127.0.0.1"',
        f"port = {radio_port}",
        "[mqtt]",
        f"host = {json.dumps(mqtt_host)}",
        f"port = {mqtt_port}",
        f"node_id = {json.dumps(node)}",
        "insecure = true  # a test broker on the loopback, without user names",
        "[homeassistant]",
        f"language = {json.dumps(language)}",
        "[airtime]",
        f"budget_s_per_hour = {budget}",
        f"burst_s = {burst}",
    ]
    if stuck:
        lines += ["[messages]", f"queue_ttl_s = {STUCK_TTL_S}"]
    for alias, where in TARGETS:
        lines += [f"[targets.{alias}]", where]
    return "\n".join(lines) + "\n"


class Watcher:
    """The broker as the runner sees it: the latest message of each of the gateway's topics."""

    def __init__(self, host: str, port: int, topics: Topics) -> None:
        self._latest: dict[str, bytes] = {}
        self._changed = threading.Condition()
        self._client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(host, port)
        self._client.subscribe(f"{topics.root}/#", 1)
        self._client.loop_start()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        with self._changed:
            self._latest[message.topic] = bytes(message.payload)
            self._changed.notify_all()

    def latest(self, topic: str) -> Any:
        """The latest payload on `topic`, from JSON where it is JSON; None before the first."""
        with self._changed:
            payload = self._latest.get(topic)
        return None if payload is None else _decoded(payload)

    def wait(
        self,
        topic: str,
        check: Callable[[Any], bool],
        what: str,
        *,
        timeout: float,
        alive: Callable[[], bool],
    ) -> Any:
        """The first payload on `topic` that passes `check`; GaveUpError after `timeout` or when
        the gateway is no longer `alive`."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                payload = self._latest.get(topic)
                if payload is not None and check(value := _decoded(payload)):
                    return value
                left = deadline - time.monotonic()
                if left <= 0 or not alive():
                    raise GaveUpError(f"gave up waiting for {what}")
                self._changed.wait(min(left, 0.5))

    def publish(self, topic: str, payload: str, *, qos: int = 1) -> None:
        self._client.publish(topic, payload, qos=qos).wait_for_publish(5)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _decoded(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except ValueError:
        return payload.decode("utf-8", errors="replace")


def _has(*texts: str) -> Callable[[Any], bool]:
    """A check of the history document: every text is in it."""

    def check(document: Any) -> bool:
        found = {m.get("text") for m in document.get("messages", [])}
        return set(texts) <= found

    return check


@dataclass(frozen=True, slots=True)
class Run:
    """One `run`: the radio served, the broker watched, the work folder to keep the gateway in."""

    args: argparse.Namespace
    work: Path
    watcher: Watcher
    server: TcpRadioServer
    radio: EmulatedRadio
    say: Say

    @property
    def topics(self) -> Topics:
        return Topics(BASE_TOPIC, self.args.node)

    def gateway(self, stop: threading.Event) -> int:
        """Start the gateway, fill it, and run until `stop` is set; then stop it."""
        args, config, log_path = self.args, self.work / "config.toml", self.work / "gateway.log"
        config.write_text(
            configuration(
                self.server.port,
                args.mqtt_host,
                args.mqtt_port,
                node=args.node,
                language=args.language,
                stuck=args.stuck,
            ),
            encoding="utf-8",
        )
        self.say(
            f"emulated radio {RADIO_NAME!r} on 127.0.0.1:{self.server.port}, "
            f"the gateway's log {log_path}"
        )
        paths = [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]
        with log_path.open("ab") as log:
            gateway = subprocess.Popen(  # noqa: S603 - this checkout's gateway, fixed arguments
                [
                    sys.executable,
                    "-m",
                    "meshcore_home_gateway",
                    "run",
                    "--config",
                    str(config),
                    "--state-dir",
                    str(self.work / "state"),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, paths))},
                start_new_session=True,  # Ctrl-C reaches the runner, which stops it with SIGTERM
            )
            result = 1
            try:
                result = self._supervise(gateway, stop, log_path)
            finally:
                code = _stop(gateway)
                if code != 0:
                    self.say(f"the gateway ended with {code}; see {log_path}")
                    result = 1
        if result == 0:
            self.say("stopped: the gateway emptied its drafts and went offline")
        return result

    def _supervise(
        self, gateway: subprocess.Popen[bytes], stop: threading.Event, log_path: Path
    ) -> int:
        def alive() -> bool:
            return gateway.poll() is None

        args = self.args
        try:
            held = self.fill(alive)
        except GaveUpError as exc:
            self.say(f"{exc}; see {log_path}")
            return 1
        self.say(f"the gateway {args.node} on {args.mqtt_host}:{args.mqtt_port} holds {held}")
        self.say(
            "ready: the chats family, alice, bob and Carol's; "
            f"{chat_entities(args.node, 'family').text} is family's draft. Ctrl-C stops."
        )
        while not stop.wait(0.5):
            if not alive():
                self.say(f"the gateway stopped by itself; see {log_path}")
                return 1
        return 0

    def fill(self, alive: Callable[[], bool]) -> str:
        """The household's messages and draft, once the gateway is up; what it holds, in words."""
        topics, radio, watcher = self.topics, self.radio, self.watcher

        def wait(topic: str, check: Callable[[Any], bool], what: str) -> Any:
            return watcher.wait(topic, check, what, timeout=self.args.timeout, alive=alive)

        wait(topics.availability, lambda v: v == "online", "the gateway online")
        wait(topics.state, _connected, "the radio connected")
        wait(topics.target_draft(DRAFT[0]), lambda d: d == {"text": ""}, "the drafts")
        now = int(time.time())
        for contact, text, ago in RECEIVED:
            prefix = contact.public_key[:6]
            self.server.call(partial(radio.queue_direct_message, prefix, text, now - ago))
        sender, text, ago = CHANNEL_RECEIVED
        channel_text = f"{sender}: {text}"
        self.server.call(
            partial(radio.queue_channel_message, FAMILY_CHANNEL, channel_text, now - ago)
        )
        received = [text for _, text, _ in RECEIVED] + [CHANNEL_RECEIVED[1]]
        wait(topics.history, _has(*received), "the received messages in the history")
        for alias, text in SENT:
            watcher.publish(topics.send(alias), text)
        wait(topics.history, _has(*(t for _, t in SENT)), "the sent messages in the history")
        alias, text = DRAFT
        watcher.publish(topics.target_draft_request(alias), json.dumps({"text": text}), qos=0)
        wait(topics.target_draft(alias), lambda d: d == {"text": text}, f"{alias}'s draft")
        held = f"{len(received)} received and {len(SENT)} sent messages, a draft in {alias}'s chat"
        if not self.args.stuck:
            return held
        # a burst's worth of reminders and two more: the burst is spent, the last ones wait
        lora = meshcore_lora(62_500, radio.spreading_factor, radio.coding_rate)
        longest = len(REMINDER.format(n=99).encode())
        each = time_on_air_s(channel_message_bytes(longest, len(RADIO_NAME.encode())), lora)
        for n in range(1, math.floor(STUCK_BURST_S / each) + 3):
            watcher.publish(topics.send("family"), REMINDER.format(n=n))
        time.sleep(SETTLE_S)
        pending = wait(topics.pending, lambda d: d.get("total", 0) >= 2, "messages on their way")
        return f"{held}, {pending['total']} messages on their way"


def _connected(state: Any) -> bool:
    return isinstance(state, dict) and state.get("radio") == "connected"


def work_folder(named: Path | None) -> tuple[Path, bool]:
    """The folder to work in, and whether it is a temporary one to remove at the end."""
    if named is None:
        return Path(tempfile.mkdtemp(prefix="emulated-radio-")), True
    named.mkdir(parents=True, exist_ok=True)
    return named, False


def serve(args: argparse.Namespace, stop: threading.Event, say: Say) -> int:
    """`run`: until `stop` is set. 0 when the gateway ran and stopped cleanly."""
    if args.work is not None and args.work.is_dir() and any(args.work.iterdir()):
        say(f"the work folder {args.work} is not empty: name a new one")
        return 2
    try:
        watcher = Watcher(args.mqtt_host, args.mqtt_port, Topics(BASE_TOPIC, args.node))
    except OSError as exc:
        say(f"no broker on {args.mqtt_host}:{args.mqtt_port}: {exc}")
        return 1
    work, temporary = work_folder(args.work)
    try:
        radio = household_radio(int(time.time()))
        with TcpRadioServer(radio) as server:
            return Run(args, work, watcher, server, radio, say).gateway(stop)
    finally:
        watcher.close()
        if temporary:
            shutil.rmtree(work, ignore_errors=True)


def _stop(gateway: subprocess.Popen[bytes]) -> int:
    """SIGTERM, as systemd stops it; its exit code (killed after STOP_S: -9)."""
    if gateway.poll() is None:
        gateway.send_signal(signal.SIGTERM)
        try:
            gateway.wait(STOP_S)
        except subprocess.TimeoutExpired:
            gateway.kill()
            gateway.wait()
    return gateway.returncode


# --- the round trip through Home Assistant ------------------------------------------------------


class RoundTripError(Exception):
    """The round trip could not be made: no login, no entity, no answer."""


@dataclass(frozen=True, slots=True)
class Chat:
    """The entities of a target's chat in Home Assistant."""

    text: str
    button: str
    history: str
    pending: str


def chat_entities(node: str, alias: str) -> Chat:
    """The entity IDs Home Assistant gives the gateway's components: a hyphen of the object ID
    becomes an underscore."""
    device = "meshcore_home_gateway_" + node.replace("-", "_")
    write = "write_" + alias.replace("-", "_")
    return Chat(
        text=f"text.{device}_{write}",
        button=f"button.{device}_{write}_send",
        history=f"sensor.{device}_message_history",
        pending=f"sensor.{device}_pending_messages",
    )


class HomeAssistant:
    """Home Assistant's REST API as one of its users, logged in as its login page does."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise RoundTripError(f"not an http or https address: {url!r}")
        self.url = url.rstrip("/")
        self.client_id = self.url + "/"
        self.timeout = timeout
        self._token = ""

    def _send(
        self,
        method: str,
        path: str,
        body: object = None,
        form: dict[str, str] | None = None,
    ) -> Any:
        headers = {}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(  # noqa: S310 - http or https, checked above
            self.url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as answer:  # noqa: S310
                content = answer.read()
        except urllib.error.HTTPError as exc:
            exc.close()  # it holds the answer, and one left open warns when collected (3.14)
            raise
        return json.loads(content) if content else None

    def log_in(self, user: str, secret: str) -> None:
        flow = self._send(
            "POST",
            "/auth/login_flow",
            {
                "client_id": self.client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": self.client_id,
            },
        )
        step = self._send(
            "POST",
            f"/auth/login_flow/{urllib.parse.quote(str(flow['flow_id']), safe='')}",
            {"client_id": self.client_id, "username": user, "password": secret},
        )
        if step.get("type") != "create_entry":
            errors = ", ".join(str(e) for e in (step.get("errors") or {}).values()) or step
            raise RoundTripError(f"login as {user} failed: {errors}")
        grant = {"grant_type": "authorization_code", "code": step["result"]}
        token = self._send("POST", "/auth/token", form={**grant, "client_id": self.client_id})
        self._token = str(token["access_token"])

    def state(self, entity_id: str) -> dict[str, Any] | None:
        """The entity's state and attributes; None when Home Assistant has no such entity."""
        try:
            found = self._send("GET", f"/api/states/{entity_id}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return dict(found)

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self._send("POST", f"/api/services/{domain}/{service}", data)


def _entries(ha: HomeAssistant, entity_id: str, text: str) -> list[dict[str, Any]]:
    """The messages of a history or pending sensor whose text holds `text`."""
    state = ha.state(entity_id) or {}
    messages = (state.get("attributes") or {}).get("messages") or []
    return [m for m in messages if isinstance(m, dict) and text in str(m.get("text"))]


def check_round_trip(
    ha: HomeAssistant, chat: Chat, text: str, *, timeout: float, settle: float, say: Say
) -> bool:
    """Write `text` into the chat's draft, press Send, and check that it arrives once."""
    field = ha.state(chat.text)
    if field is None:
        raise RoundTripError(f"no {chat.text} in Home Assistant")
    if field.get("state") == "unavailable":
        raise RoundTripError(f"{chat.text} is unavailable: is the gateway running?")
    ha.call("text", "set_value", {"entity_id": chat.text, "value": text})
    ha.call("button", "press", {"entity_id": chat.button})
    started = time.monotonic()
    while not _entries(ha, chat.history, text):
        if time.monotonic() - started > timeout:
            say(f"round trip: {text!r} not in the history within {timeout:g} s")
            return False
        time.sleep(0.2)
    arrived = time.monotonic() - started
    time.sleep(settle)  # a second copy would come by now
    in_history = _entries(ha, chat.history, text)
    on_its_way = _entries(ha, chat.pending, text)
    draft = (ha.state(chat.text) or {}).get("state")
    say(
        f"round trip: {text!r} in the history after {arrived:.1f} s; "
        f"{len(in_history)} time(s) in the history, {len(on_its_way)} on its way, "
        f"the draft {draft!r}"
    )
    out = [m for m in in_history if m.get("direction") == "out"]
    return len(in_history) == len(out) == 1 and not on_its_way and draft == ""


def password(args: argparse.Namespace) -> str:
    if args.password_file is not None:
        return str(args.password_file.read_text(encoding="utf-8").strip())
    secret = os.environ.get(args.password_env, "")
    if not secret:
        raise SystemExit(f"no password: set {args.password_env} or give --password-file")
    return secret


def round_trip(args: argparse.Namespace, say: Say) -> int:
    """`round-trip`: 0 when the text arrived once."""
    secret = password(args)
    text = f"Round trip {secrets.token_hex(4)}"
    try:
        ha = HomeAssistant(args.url)
        ha.log_in(args.user, secret)
        passed = check_round_trip(
            ha,
            chat_entities(args.node, args.chat),
            text,
            timeout=args.timeout,
            settle=args.settle,
            say=say,
        )
    except (RoundTripError, OSError, ValueError, KeyError) as exc:
        say(f"round trip: {exc}")
        return 1
    say(f"round trip: {'passed' if passed else 'failed'}")
    return 0 if passed else 1


# --- the command line ---------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="emulated_radio.py",
        description="The emulated radio with a real gateway on it, for the browser check.",
    )
    commands = p.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="serve the emulated radio and run a gateway on it")
    run.add_argument("--mqtt-host", default="127.0.0.1", help="the broker (default: 127.0.0.1)")
    run.add_argument("--mqtt-port", type=int, default=1883, help="its port (default: 1883)")
    run.add_argument("--node", default=NODE, help=f"the gateway's node_id (default: {NODE})")
    run.add_argument(
        "--language", default="en", help="homeassistant.language, of the names (default: en)"
    )
    run.add_argument("--work", type=Path, help="a new or empty folder to keep (default: temporary)")
    run.add_argument(
        "--stuck",
        action="store_true",
        help="a second of airtime an hour: the last messages stay on their way",
    )
    run.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT_S,
        metavar="SECONDS",
        help=f"how long each step of the start may take (default: {TIMEOUT_S:g})",
    )
    trip = commands.add_parser(
        "round-trip", help="send a draft through Home Assistant and find it once in the history"
    )
    trip.add_argument("--url", default="http://127.0.0.1:8123", help="Home Assistant's address")
    trip.add_argument("--user", required=True, help="the user name to log in with")
    trip.add_argument(
        "--password-env",
        default="HA_PASSWORD",
        metavar="NAME",
        help="the environment variable that holds the password (default: HA_PASSWORD)",
    )
    trip.add_argument("--password-file", type=Path, help="a file that holds the password instead")
    trip.add_argument("--node", default=NODE, help=f"the gateway's node_id (default: {NODE})")
    trip.add_argument("--chat", default="family", help="the target's alias (default: family)")
    trip.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT_S,
        metavar="SECONDS",
        help=f"how long the text may take to reach the history (default: {TIMEOUT_S:g})",
    )
    trip.add_argument(
        "--settle",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="how long to look for a second copy after the first (default: 3)",
    )
    return p


def _say(line: str) -> None:
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "round-trip":
        return round_trip(args, _say)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    return serve(args, stop, _say)


if __name__ == "__main__":
    sys.exit(main())
