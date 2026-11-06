"""Hardware checklist steps 11-13, 55 and 56 on the simulated air: commands from the mesh. Bob's
companion B is a command sender; C, another companion on A's table, is not. The broker of the
tests takes anyone, so `insecure = true` accepts that (SEC-004)."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.core.commands import CODE_STEP_S, code_at
from meshcore_home_gateway.core.model import MessageKind
from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import Watcher, running
from tests.firmware.household import ADVERT_S, SNR, household_ready, start

SECRET = bytes(range(1, 21))  # B's code secret: 160 bits, as `commands enrol` makes them
CHANNEL = 0  # Public, a channel target with a prefix (step 56)
NEWER_S = 1.1  # a sender's commands carry its clock in seconds: each newer than the last


def commands(bob_key: str, more: str = "") -> str:
    return (
        "[telemetry]\nenabled = false\n"
        f'[commands]\nsenders = ["{bob_key}"]\nwords = ["status"]\n{more}'
    )


def text(wanted: str) -> Callable[[Any], bool]:
    return lambda payload: payload is not None and payload["text"] == wanted


def confirmation(wanted: str) -> Callable[[Any], bool]:
    return lambda payload: text(wanted)(payload) and payload["event_type"] == "confirmed"


def judged(watcher: Watcher, since: int, wanted: str) -> list[Any]:
    """The `command` parts of the `received` events of a text, after `since`."""
    return [m.json()["command"] for m in watcher.on("received", since) if text(wanted)(m.json())]


@pytest.mark.broker
def test_commands_from_the_mesh(scene: Scene) -> None:
    """Step 11: B, a sender, sends `status`: a `command` event, `received` shows it accepted.
    Step 12: C, not a sender, sends `status`: no `command` event, `sender_not_allowed`.
    Step 13: B sends `status` while the service is stopped: after the start `received` shows
    `sender_unverified`; sent again, it is a command."""
    household = start(scene.nodes, scene.air)
    carol = scene.nodes.companion("C")
    scene.air.link(household.alice.node_id, carol.node_id, snr=SNR)
    watcher = scene.watcher
    insecure = "insecure = true\n"

    async def scenario() -> None:
        async with App(household.bob) as bob, App(carol) as carol_app:
            async with App(household.alice) as alice_app:
                await household_ready(household, alice_app, bob)
                carol_key = (await carol_app.start()).public_key
                await carol_app.advert(flood=False)
                await alice_app.wait_for_contact(carol_key, timeout_s=ADVERT_S)
                await alice_app.advert(flood=False)
                await carol_app.wait_for_contact(household.alice_key, timeout_s=ADVERT_S)
            bob_key = household.bob_key.hex()
            config = scene.configuration(household.alice, commands(bob_key), mqtt=insecure)
            async with running(config, scene.state_dir):
                await watcher.wait(f"contacts/{bob_key}")
                since = watcher.mark()
                await bob.send_text(household.alice_key, "status")
                command = (await watcher.wait("command", since=since)).json()
                assert (command["event_type"], command["args"]) == ("status", "")
                assert command["sender_key"] == bob_key
                await watcher.wait("received", text("status"), since=since)
                assert judged(watcher, since, "status") == [
                    {"word": "status", "accepted": True, "reason": None}
                ]

                since = watcher.mark()
                await carol_app.send_text(household.alice_key, "status")
                received = (await watcher.wait("received", text("status"), since=since)).json()
                assert received["command"] == {
                    "word": "status",
                    "accepted": False,
                    "reason": "sender_not_allowed",
                }
                await asyncio.sleep(2)  # a command event would have come with the received one
                assert not watcher.on("command", since)

            await asyncio.sleep(NEWER_S)
            await bob.send_text(household.alice_key, "status")  # waits in A for the gateway
            since = watcher.mark()
            async with running(config, scene.state_dir):
                await watcher.wait("received", text("status"), since=since)
                assert judged(watcher, since, "status") == [
                    {"word": "status", "accepted": False, "reason": "sender_unverified"}
                ]
                assert not watcher.on("command", since)
                await asyncio.sleep(NEWER_S)
                await bob.send_text(household.alice_key, "status")
                await watcher.wait("command", since=since)

    asyncio.run(scenario())


@pytest.mark.broker
def test_codes_and_replies(scene: Scene, tmp_path: Path) -> None:
    """Step 55: B has a code secret; `status <code>` is a command with empty `args`, the same
    text anew `code_reused`, `status` alone `code_missing`. Step 56: with `reply = "all"` B gets
    `ok: status` and `rejected: code_missing`, each confirmed with B's key as `target`; a channel
    target with `prefix = "Home: "` sends `Home: Gate open`."""
    household = start(scene.nodes, scene.air)
    watcher = scene.watcher
    codes = tmp_path / "codes.toml"

    async def scenario() -> None:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice_app:
                await household_ready(household, alice_app, bob)
            bob_key = household.bob_key.hex()
            codes.write_text(f'"{bob_key}" = "{base64.b32encode(SECRET).decode()}"\n')
            codes.chmod(0o600)
            more = (
                f'codes_file = "{codes}"\nreply = "all"\n'
                f'[targets.home]\nchannel = {CHANNEL}\nprefix = "Home: "\n'
            )
            config = scene.configuration(
                household.alice, commands(bob_key, more), mqtt="insecure = true\n"
            )
            async with running(config, scene.state_dir):
                await watcher.wait(f"contacts/{bob_key}")
                code = code_at(SECRET, int(time.time()) // CODE_STEP_S)
                since = watcher.mark()
                await bob.send_text(household.alice_key, f"status {code}")
                command = (await watcher.wait("command", since=since)).json()
                assert (command["event_type"], command["args"]) == ("status", "")
                await bob.wait_for_message(
                    lambda m: m.kind is MessageKind.CONTACT and m.text == "ok: status",
                    "the reply to the command",
                )

                await asyncio.sleep(NEWER_S)
                since = watcher.mark()
                await bob.send_text(household.alice_key, f"status {code}")
                await watcher.wait("received", text(f"status {code}"), since=since)
                assert judged(watcher, since, f"status {code}") == [
                    {"word": "status", "accepted": False, "reason": "code_reused"}
                ]

                await asyncio.sleep(NEWER_S)
                since = watcher.mark()
                await bob.send_text(household.alice_key, "status")
                await watcher.wait("received", text("status"), since=since)
                assert judged(watcher, since, "status") == [
                    {"word": "status", "accepted": False, "reason": "code_missing"}
                ]
                await bob.wait_for_message(
                    lambda m: m.kind is MessageKind.CONTACT and m.text == "rejected: code_missing",
                    "the reply to the rejected command",
                )
                for reply in ("ok: status", "rejected: code_missing"):
                    confirmed = await watcher.wait("delivery", confirmation(reply))
                    assert confirmed.json()["target"] == bob_key
                assert not watcher.on("command", since)

                watcher.publish("send/home", "Gate open")
                await bob.wait_for_message(
                    lambda m: (
                        m.kind is MessageKind.CHANNEL and m.text.endswith(": Home: Gate open")
                    ),
                    "the channel message with the target's prefix",
                )

    asyncio.run(scenario())
