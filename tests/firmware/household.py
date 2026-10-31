"""The setup of the hardware checklist's steps 29-40 on the simulated air: radio A on the gateway,
Bob's companion B with a GPS, set up by his phone app as docs/companions.md says, and repeaters
between them where a step needs hops.

Positions are public landmarks.
"""

from __future__ import annotations

from dataclasses import dataclass

from meshcore_home_gateway.protocol.codes import Push
from tests.firmware.air import AirThread
from tests.firmware.app import BY_CONTACT, GATEWAY_FLAGS, App, AppError
from tests.firmware.conftest import Nodes
from tests.firmware.node import Node

# the Brandenburg Gate in Berlin, a public landmark: latitude, longitude, altitude in m
LANDMARK = (52.516275, 13.377704, 34.5)
SNR = 8.0  # dB on every link: a clear signal
ADVERT_S = 30.0  # an advert crosses two repeaters and their random delays
ROUTE_S = 15.0  # a message floods over two repeaters and the paths come back


@dataclass
class Household:
    alice: Node  # A: the gateway's radio
    bob: Node  # B: Bob's companion with a GPS
    repeaters: list[Node]
    alice_key: bytes = b""
    bob_key: bytes = b""

    @property
    def chain(self) -> list[Node]:
        return [self.alice, *self.repeaters, self.bob]


def start(nodes: Nodes, air: AirThread, hops: int = 0) -> Household:
    """A, `hops` repeaters and B, linked in a chain A - R1 - ... - B (both ways), all booting at
    once. B has a GPS with a fix at the landmark."""
    alice = nodes.companion("A", wait=False)
    repeaters = [nodes.repeater(f"R{n}", wait=False) for n in range(1, hops + 1)]
    gps = ",".join(str(value) for value in LANDMARK)
    bob = nodes.companion("B", gps=gps, wait=False)
    nodes.ready()
    household = Household(alice, bob, repeaters)
    for near, far in zip(household.chain, household.chain[1:], strict=False):
        air.link(near.node_id, far.node_id, snr=SNR)
    return household


async def introduce(household: Household, alice: App, bob: App) -> None:
    """A and B hear each other's advert and add each other, as a fresh radio adds every node it
    hears. Over repeaters the adverts flood."""
    flood = bool(household.repeaters)
    household.alice_key = (await alice.start()).public_key
    household.bob_key = (await bob.start()).public_key
    await alice.advert(flood=flood)
    await bob.wait_for_contact(household.alice_key, timeout_s=ADVERT_S)
    await bob.advert(flood=flood)
    await alice.wait_for_contact(household.bob_key, timeout_s=ADVERT_S)


async def set_up_bob(household: Household, bob: App) -> None:
    """docs/companions.md steps 5, 6 and 8: telemetry from specific contacts, the gateway
    allowed everything and a favourite, the GPS on."""
    await bob.settings(BY_CONTACT)
    await bob.grant(household.alice_key, GATEWAY_FLAGS)
    await bob.set_var("gps", "1")


async def teach_route(household: Household, alice: App, bob: App) -> None:
    """docs/companions.md step 9: B sends the gateway one message. It floods; A answers with its
    path and B sends its own back, so both radios hold a route. The message waits in A for the
    gateway. When a collision on the air lost B's path on its way to A, Bob sends a second
    message."""
    for text in ("Hello from Bob", "Hello again"):
        since = alice.mark()
        await bob.send_text(household.alice_key, text)
        try:
            await alice.push(Push.PATH_UPDATED, since, household.bob_key, timeout_s=ROUTE_S)
        except AppError:
            continue
        return
    raise AppError("A learned no route to B")


async def household_ready(
    household: Household, alice: App, bob: App, *, route: bool = True
) -> None:
    """Everything above: then the gateway can start on A."""
    await introduce(household, alice, bob)
    await set_up_bob(household, bob)
    if route:
        await teach_route(household, alice, bob)
