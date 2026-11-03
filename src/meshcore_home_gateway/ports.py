"""The ports of the application (ADR 0007, ARC-003): what it asks of the radio and of the bus, the
events and errors the radio gives it, and the requests that come over the bus.

The adapters implement them: `radio/session.py` and `radio/transport.py` the radio,
`mqtt/client.py` the bus. The application's modules import these and no adapter
(`tests/test_architecture.py`), so that a second radio adapter, BLE for one (design §12), changes
none of them. The command line and the wiring of the service (`wiring.py`) put the adapters in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from meshcore_home_gateway.core.airtime import LoRaParams, meshcore_lora
from meshcore_home_gateway.core.contacts import ContactBook
from meshcore_home_gateway.core.model import InboundMessage
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.protocol import codec

# --- the radio ----------------------------------------------------------------------------------


class RadioError(Exception):
    """The radio cannot be used right now (disconnected, timed out, protocol violation)."""


class NotSentError(RadioError):
    """The session ended before the command was sent: the radio never got it."""


class CommandFailedError(RadioError):
    """The radio refused a command: ERR with an error name, or `disabled`."""

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"{command}: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RadioInfo:
    self_info: codec.SelfInfo
    device: codec.DeviceInfo

    @property
    def public_key(self) -> str:
        return self.self_info.public_key.hex()

    @property
    def name(self) -> str:
        return self.self_info.name

    @property
    def lora(self) -> LoRaParams:
        s = self.self_info
        return meshcore_lora(float(s.bandwidth_hz), s.spreading_factor, s.coding_rate)


@dataclass(frozen=True, slots=True)
class MessageReceived:
    message: InboundMessage
    session: RadioPort  # judge the sender on this session's contact table
    fetched_at: int  # the session's command count when the message was fetched
    at_connect: bool  # fetched at connect: it may have arrived while no session listened


@dataclass(frozen=True, slots=True)
class AckReceived:
    ack: bytes
    round_trip_ms: int


@dataclass(frozen=True, slots=True)
class TelemetryReceived:
    """Push 0x8B: the answer to the pending telemetry request, named by the 6-byte prefix of
    the contact whose key decrypted it."""

    prefix: bytes
    data: bytes
    received_at: float
    seq: int  # the frame's number in its session
    session_serial: int  # numbers are compared only within one session


@dataclass(frozen=True, slots=True)
class TelemetrySent:
    """The radio's answer to a telemetry request: it sent it."""

    flood: bool  # the route it took
    timeout_ms: int  # the radio's estimate of the round trip
    seq: int  # the number of the SENT frame


@dataclass(frozen=True, slots=True)
class PathChanged:
    """Push 0x81: the radio stored a path the contact sent (also the same one again)."""

    public_key: bytes


@dataclass(frozen=True, slots=True)
class PathReset:
    """RESET_PATH was answered: the radio floods to the contact until it learns a path."""

    public_key: bytes


@dataclass(frozen=True, slots=True)
class PacketLogged:
    rx: codec.RxLog
    received_at: float
    seq: int = 0  # the frame's number in its session; 0: not from a session (tests)
    session_serial: int = 0  # numbers are compared only within one session


@dataclass(frozen=True, slots=True)
class AdvertHeard:
    """The radio checked an advert's signature and reports it: a contact (key only) or a node it
    did not add (its full record)."""

    advert: codec.Advert
    received_at: float
    seq: int = 0  # the frame's number: with no receive delay, the log just before is its own
    session_serial: int = 0


@dataclass(frozen=True, slots=True)
class ContactsRead:
    records: Mapping[str, codec.ContactRecord]  # full key, hex -> record; not changed later
    read_at: float


@dataclass(frozen=True, slots=True)
class StatsRead:
    """The heartbeat's readings; a part is None if the firmware does not have it."""

    battery: codec.Battery
    core: codec.CoreStats | None
    radio: codec.RadioStats | None
    packets: codec.PacketStats | None
    read_at: float


@dataclass(frozen=True, slots=True)
class Barrier:
    """Not from the radio: put on the event queue by a caller that waits until the events before
    it are handled (`done` is then set)."""

    done: asyncio.Event = field(default_factory=asyncio.Event)


RadioEvent = (
    MessageReceived
    | AckReceived
    | PacketLogged
    | AdvertHeard
    | ContactsRead
    | StatsRead
    | TelemetryReceived
    | PathChanged
    | PathReset
    | Barrier
)


class Transport(Protocol):
    """A byte stream to the radio: serial or TCP (radio/transport.py)."""

    description: str
    needs_flush: bool  # the radio's parser may hold a partial frame from an earlier client

    async def open(self) -> None: ...
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class RadioProfileHook(Protocol):
    """The managed radio profile (radio/profile.py, ADR 0020): the session runs it before and
    after its first table read; the application takes what it did."""

    async def before_contacts(self, session: RadioPort) -> None: ...
    async def after_contacts(self, session: RadioPort) -> bool: ...
    def take_reports(self) -> list[ProfileReport]: ...


class RadioPort(Protocol):
    """One connection to a companion radio, as the application uses it (radio/session.py
    implements it). Its events go to the queue it was made with."""

    info: RadioInfo | None
    autoadd: codec.AutoAddConfig | None
    tuning: codec.TuningParams | None  # None: not known to the firmware
    battery: codec.Battery | None
    stats: StatsRead | None
    channels: dict[int, str]
    contact_records: dict[str, codec.ContactRecord]
    contacts_read_at: int  # command count of the read behind contact_records
    contacts_read_started_at: int  # command count of the newest read, also one running
    clock_offset_s: int | None  # the radio's clock minus the host's, last read
    set_clock_max_days: int
    serial: int  # tells this session's frame numbers from another's
    closed_by_radio: bool
    on_contact_key: Callable[[str, bool], None] | None  # key, held as contact
    profile: RadioProfileHook | None

    @property
    def closed(self) -> bool: ...
    @property
    def contacts(self) -> ContactBook: ...
    @property
    def commands_sent(self) -> int: ...
    @property
    def contacts_changed_at(self) -> int: ...
    @property
    def contacts_outdated(self) -> bool: ...

    async def start(self, *, fetch_messages: bool = True) -> RadioInfo: ...
    async def close(self) -> None: ...
    async def wait_closed(self) -> None: ...
    async def refresh_contacts(self, *, newer_than: int | None = None) -> None: ...
    async def write_setting(self, what: str, request: bytes) -> None: ...
    async def reread_settings(self) -> None: ...
    async def write_contact(self, request: bytes) -> None: ...
    async def remove_contact(self, public_key: bytes) -> None: ...
    async def send_text(
        self, prefix: bytes, text: str, timestamp: int, attempt: int
    ) -> codec.Sent: ...
    async def send_channel_text(self, index: int, text: str, timestamp: int) -> None: ...
    async def reset_path(self, public_key: bytes) -> None: ...
    async def request_telemetry(
        self, public_key: bytes, *, unless: Callable[[], bool] | None = None
    ) -> TelemetrySent | None: ...


# made by the wiring (wiring.py): a session on this transport that reports to this queue, and
# whether it sets the radio's clock
SessionFactory = Callable[[Transport, "asyncio.Queue[RadioEvent]", bool], RadioPort]


# --- the bus ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SendRequest:
    alias: str  # a configured target, or the full key of a contact (`contact` is then true)
    payload: bytes
    retained: bool
    contact: bool = False


@dataclass(frozen=True, slots=True)
class ContactRequest:
    """A request to change the radio's contact table (ADR 0017), or whether a companion is asked
    for its telemetry (ADR 0018)."""

    # "add" (add_contact, homeassistant/add_node), "remove" (contacts/<key>/remove) or
    # "telemetry" (contacts/<key>/telemetry)
    kind: str
    payload: bytes
    retained: bool
    key: str | None = None  # remove, telemetry: the topic's <key> level, as it came
    node_label: bool = False  # add: the text is the label of a heard node (add_node)


class Bus(Protocol):
    """What the application needs from the message bus (mqtt/client.py's MqttBus declares it)."""

    requests: asyncio.Queue[SendRequest]
    contact_requests: asyncio.Queue[ContactRequest]
    connected: bool
    on_connected: list[Callable[[], None]]
    on_ha_online: list[Callable[[], None]]
    on_ha_offline: list[Callable[[], None]]
    on_ha_device: list[Callable[[str, bytes, bool], None]]
    on_show_hidden: list[Callable[[], None]]

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None: ...
    def dropped_1h(self) -> int: ...
