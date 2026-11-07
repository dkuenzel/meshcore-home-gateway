"""The bridge service: connects the radio session and the MQTT bus (design §5 and §6, ARC-009).

It supervises the radio, hands out the radio's events and runs the tasks of all parts on one event
loop until the stop; wiring.py builds it with its parts and connects them.
* radio supervisor (here): connect, run the session, reconnect with exponential back-off;
* outbox (outbox.py): send requests -> validation, deduplication, queue (or rejection); one
  message at a time: wait for radio and airtime, send, wait for the acknowledgement, retry,
  flood, report every step on the delivery topic;
* radio events (here): acknowledgements and packet log at once, received messages in order to the
  inbox (inbox.py: deduplicated; command policy with a fresh contact table and a memory of
  look-alikes);
* status (here, the document in status.py): retained state document, systemd watchdog and status;
  an exclusion from telemetry that could not be stored is written again; the `config` document
  at the start and at each radio connect (config_view.py);
* announcements to Home Assistant (announcer.py), contact requests (contact_manager.py);
* telemetry, if enabled: asks the radio's companions one at a time, after the messages
  (telemetry_poller.py, ADR 0018); it gives the observer each companion's state and hears of
  every exclusion;
* the observer gets every report of the radio, every received message and every delivery, for
  the documents of the providers and the history (observer.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable

from meshcore_home_gateway.announcer import Announcer
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.config_checks import start_warnings
from meshcore_home_gateway.config_view import shown_settings, target_settings
from meshcore_home_gateway.contact_manager import ContactManager
from meshcore_home_gateway.health import FAILURES
from meshcore_home_gateway.inbox import Inbox, target_channels
from meshcore_home_gateway.mqtt.payloads import encode, rx_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer, observe
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.ports import (
    AckReceived,
    AdvertHeard,
    Barrier,
    Bus,
    MessageReceived,
    PacketLogged,
    PathChanged,
    RadioError,
    RadioEvent,
    RadioInfo,
    RadioPort,
    RadioProfileHook,
    RadioRestartedError,
    SessionFactory,
    TelemetryReceived,
    Transport,
)
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.state import CommandLedger
from meshcore_home_gateway.status import Status
from meshcore_home_gateway.systemd import watchdog_interval_s
from meshcore_home_gateway.telemetry_poller import TelemetryPoller

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out

RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 60.0
CLOCK_RESTART_INTERVAL_S = 86_400.0  # the radio is restarted for its clock at most this often
RESTART_WAIT_S = 5.0  # a restarted radio is back after a few seconds
STATUS_INTERVAL_S = 60.0
STOP_DRAIN_S = 2.0  # on stop, received messages already out of the radio are handled this long


class Bridge:
    def __init__(
        self,
        config: Config,
        bus: Bus,
        topics: Topics,
        ledger: CommandLedger,
        *,
        transport_factory: Callable[[], Transport],
        session_factory: SessionFactory,
        radio: RadioLink,
        events: asyncio.Queue[RadioEvent],
        observer: Observer,
        outbox: Delivery,
        inbox: Inbox,
        announcer: Announcer,
        status: Status,
        contacts: ContactManager,
        telemetry: TelemetryPoller | None,
        profile: RadioProfileHook | None,
        notifier: Callable[[str], bool],
        monotonic: Callable[[], float],
    ) -> None:
        """The parts, as wiring.gateway builds and connects them."""
        self.config = config
        self._bus, self.topics, self._ledger = bus, topics, ledger
        self._transport_factory, self._session_factory = transport_factory, session_factory
        self.radio, self._events, self.observer = radio, events, observer
        self.outbox, self.inbox, self.announcer, self.status = outbox, inbox, announcer, status
        self._contacts, self._telemetry, self._profile = contacts, telemetry, profile
        self._notify, self._mono = notifier, monotonic
        self._clock_restarted_at: float | None = None  # the monotonic clock's

    # --- life cycle -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        if self._telemetry is not None:  # the states of the first documents come from it
            self._telemetry.restore()
        for warning in start_warnings(self.config):
            log.warning("%s", warning)
        if self.config.contacts.manage and self.config.radio.transport == "tcp":
            log.warning(
                "contact management over TCP: heard nodes are best effort, the radio "
                "drops its pushes while no client is connected"
            )
        if self.config.messages.publish_channels == "targets":
            channels = ", ".join(map(str, sorted(target_channels(self.config)))) or "none"
            log.info("channel messages published only from the channels of targets: %s", channels)
        observe(self._configuration, None)  # the broker's connect publishes it
        await self._bus.start()
        self.outbox.restore()
        self._notify("READY=1")
        tasks: dict[str, asyncio.Task[None]] = {}
        try:
            async with asyncio.TaskGroup() as group:
                for name, coroutine in (
                    ("radio", self._radio_loop()),
                    ("requests", self.outbox.take_requests()),
                    ("contact requests", self._contact_request_loop()),
                    ("contacts", self._contacts.run()),
                    ("outbox", self.outbox.run()),
                    ("events", self._event_loop()),
                    ("messages", self.inbox.run()),
                    ("status", self._status_loop()),
                    ("lag", self.status.measure_lag()),
                ):
                    tasks[name] = group.create_task(coroutine, name=name)
                if self._telemetry is not None:
                    tasks["telemetry"] = group.create_task(self._telemetry.run(), name="telemetry")
                await stop.wait()
                self._notify("STOPPING=1")
                if (telemetry := tasks.get("telemetry")) is not None:
                    telemetry.cancel()  # no request after this; it saves its schedule
                    await asyncio.wait([telemetry])
                await self.outbox.finish(tasks["outbox"])  # before the radio: it may await an ack
                await self._finish_inbound(tasks["radio"])
                for task in tasks.values():
                    task.cancel()
        finally:
            if (session := self.radio.session) is not None:
                await session.close()
            self.radio.ready.clear()
            observe(self.observer.flush)  # what changed since its last tick
            self.outbox.save_level()
            self._ledger.store.flush()
            self.status.publish()
            await self._bus.stop()

    async def _finish_inbound(self, radio: asyncio.Task[None]) -> None:
        """Stop the radio first, then handle the messages it has already handed over, for at most
        STOP_DRAIN_S: once fetched, they are no longer in the radio. A command waiting for its
        table read is rejected, as the closed session cannot read it."""
        radio.cancel()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(STOP_DRAIN_S):
                await asyncio.wait([radio])
                await self._events.join()
                await self.inbox.drained()

    # --- radio ----------------------------------------------------------------------------------

    async def _radio_loop(self) -> None:
        delay, radio = RECONNECT_MIN_S, self.radio
        while True:
            session = self._session_factory(
                self._transport_factory(), self._events, self.config.radio.set_clock
            )
            session.set_clock_max_days = self.config.radio.set_clock_max_days
            session.may_restart = self._may_restart
            try:
                info = radio.info = await session.start()
            except RadioRestartedError as exc:
                delay = self._restarted(exc)
            except (RadioError, OSError) as exc:
                log.warning("radio: cannot connect (%s); retry in %.0f s", exc, delay)
            except Exception:  # a bug while connecting: keep the service up, retry later
                log.exception("radio: cannot connect; retry in %.0f s", delay)
            else:
                delay = await self._serve(session, info)
            finally:
                await session.close()
            await asyncio.sleep(delay * random.uniform(1.0, 1.2))  # noqa: S311 - jitter only
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _serve(self, session: RadioPort, info: RadioInfo) -> float:
        """The connected session until it ends; the delay before connecting again."""
        radio, delay = self.radio, RECONNECT_MIN_S
        radio.session = session
        radio.ready.set()
        if self.config.radio.transport == "tcp":
            self._link_started(session)
        if self._telemetry is not None:
            observe(self._telemetry.radio_connected)
        # untested firmware takes no request: the documents and the controls say so, and
        # why (OPS-007); before the index the connect publishes
        takes = self._contacts.takes_requests(info)
        observe(self.observer.set_manage, takes, self._contacts.refusal(info))
        observe(self.observer.radio_connected, info.device.max_contacts, session.tuning)
        observe(self._configuration, info)  # a channel text's room follows the radio's name
        for report in self._profile.take_reports() if self._profile else []:
            observe(self.observer.profile_applied, report)
        self._check_radio(session)
        if self.config.homeassistant.discovery and self.announcer.radio_changed():
            self.announcer.announce()  # the device page shows the radio
        else:
            self.status.publish()
        try:
            await session.wait_closed()
        except RadioRestartedError as exc:
            delay = self._restarted(exc)
        except RadioError as exc:
            log.warning("radio: connection lost (%s)", exc)
        finally:
            radio.ready.clear()
            radio.session = None
            radio.watch.ended(*_uptime(session), by_radio=session.closed_by_radio)
            # requests are refused until the radio is back: the index says why
            observe(self.observer.set_manage, self.observer.manage, self._contacts.refusal(None))
            self.status.publish()
        return delay

    def _configuration(self, info: RadioInfo | None) -> None:
        """The `config` document, each target's room as the radio's name leaves it (None: not
        connected yet)."""
        self.observer.configuration(target_settings(self.config, info), shown_settings(self.config))

    def _may_restart(self) -> bool:
        """Whether the session may restart the radio now to set its clock back: no message is
        queued or being delivered, and it did not do so in the last day."""
        last = self._clock_restarted_at
        if last is not None and self._mono() - last < CLOCK_RESTART_INTERVAL_S:
            return False
        return not self.outbox.activity.busy

    def _restarted(self, exc: RadioRestartedError) -> float:
        """The session restarted the radio: the delay before connecting again."""
        self._clock_restarted_at = self._mono()
        log.info("radio: %s; connecting again in %.0f s", exc, RESTART_WAIT_S)
        return RESTART_WAIT_S

    def _link_started(self, session: RadioPort) -> None:
        """Over TCP: whether another client took the link before this one (core.link)."""
        uptime_s, read_at = _uptime(session)
        if self.radio.watch.started(uptime_s, read_at):
            log.warning(
                "radio: the radio closed the TCP connection and kept running (uptime %s "
                "s): another client connected to it, which can control the radio and "
                "read its private key; the gateway took the link back "
                "(docs/deployment.md, Troubleshooting)",
                uptime_s,
            )

    def _check_radio(self, session: RadioPort) -> None:
        """Warn about commands over TCP; the outbox warns about its targets and its burst."""
        if self.config.radio.transport == "tcp" and self.config.commands.senders:
            log.warning(
                "commands over TCP: the radio drops push frames when its send queue is full, so "
                "the bridge can miss a look-alike of a command sender; prefer USB (ADR 0009)"
            )
        self.outbox.check_radio(session)

    async def _contact_request_loop(self) -> None:
        while True:
            self._contacts.accept(await self._bus.contact_requests.get())

    # --- radio events ---------------------------------------------------------------------------

    async def _event_loop(self) -> None:
        while True:
            event = await self._events.get()
            telemetry = self._telemetry
            if isinstance(event, MessageReceived):
                self.inbox.put(event)  # in order, without delaying acks
            elif isinstance(event, AckReceived):
                self.outbox.acks.resolve(event.ack, event.round_trip_ms)
            elif isinstance(event, Barrier):
                event.done.set()
            elif isinstance(event, TelemetryReceived):  # only the poller knows whom it asked
                if telemetry is not None:
                    observe(telemetry.answer, event)
            elif isinstance(event, PathChanged):
                if telemetry is not None:
                    observe(telemetry.path_changed, event.public_key)
            else:
                if isinstance(event, PacketLogged) and self.config.diagnostics.publish_rx_log:
                    rx = event.rx
                    payload = rx_payload(rx.snr_db, rx.rssi_dbm, rx.raw, event.received_at)
                    self._bus.publish(self.topics.rx, encode(payload))
                if telemetry is not None:
                    self._telemetry_sees(telemetry, event)
                observe(self.observer.radio_event, event)
            self._events.task_done()  # for the stop (_finish_inbound)

    def _telemetry_sees(self, telemetry: TelemetryPoller, event: RadioEvent) -> None:
        if isinstance(event, PacketLogged):
            observe(telemetry.packet_logged, event.rx)
        elif isinstance(event, AdvertHeard) and _may_be_asked(event.advert):
            observe(telemetry.heard, event.advert.public_key.hex())

    # --- status ---------------------------------------------------------------------------------

    async def _status_loop(self) -> None:
        watchdog = watchdog_interval_s()
        tick = min(STATUS_INTERVAL_S, watchdog / 2) if watchdog else STATUS_INTERVAL_S
        next_state = self._mono() + STATUS_INTERVAL_S
        while True:
            if watchdog:
                self._notify("WATCHDOG=1")
            self.outbox.report_unpublished()  # the end of a flood of refused requests
            self.inbox.report()  # the end of a flood of received messages
            self._contacts.report_unpublished()
            observe(self.observer.exclusions.flush)  # an exclusion not stored yet
            observe(self.observer.tick)
            self._ledger.store.flush()  # what the sections changed since the last tick (ARC-004)
            if self._mono() >= next_state:
                self.status.publish()
                next_state = self._mono() + STATUS_INTERVAL_S
            await asyncio.sleep(tick)


def _uptime(session: RadioPort) -> tuple[int | None, float | None]:
    """The radio's uptime in the session's last statistics, and when they were read."""
    stats = session.stats
    if stats is None or stats.core is None:
        return None, None
    return stats.core.uptime_s, stats.read_at


def _may_be_asked(advert: codec.Advert) -> bool:
    """A checked advert of a contact, or of a companion the radio did not add: added later,
    it is asked for telemetry while its advert is fresh."""
    record = advert.record
    return not advert.new or (record is not None and record.node_type == NodeType.CHAT)
