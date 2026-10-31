# Testing

The strategy is described in [design §10](design.md#10-testing-strategy). This page says how to run
the automated tests and how to test with real radios before a release.

## Automated tests

```sh
make test        # pytest with branch coverage (service subprocesses included); fails below 90 %
make matrix      # make test with each Python of 3.11 to 3.14 (all installed), like the CI matrix
make unit-check  # systemd-analyze verify and exposure score of the unit
make check       # lint, types, shellcheck, workflow audit, tests, build, unit check: what CI runs
```

`make unit-check` needs `systemd-analyze` (any systemd-based Linux).

## Home Assistant contract tests

```sh
make ha-test     # the gateway's documents in a real Home Assistant (needs Python 3.14.2 or newer)
```

`home-assistant/tests/test_contract.py` feeds the discovery document, the state document and the
event payloads, `test_contact_devices.py` the contacts' devices, `test_manage_contacts.py` the
controls that add and remove contacts, `test_telemetry.py` the companions' telemetry (their
readings, states, switch and expiry, a `person` and its presence triggers, the telemetry examples),
`test_adverts.py` the check for positions in adverts (*Position in adverts*, the gateway's sensor,
the event and its example) and `test_traces.py` the traces on the map (the map card's own request
against the real recorder) into the MQTT integration of the Home Assistant release that
`pytest-homeassistant-custom-component` pins (`home-assistant/requirements.in`; the environment is
installed from the hash-pinned `home-assistant/requirements.txt` into `.venv-homeassistant`, about
900 MB). It checks what [home-assistant.md](home-assistant.md) promises, including the example
automations taken from that page, and that Home Assistant logs no warning. It is a separate CI job
and not part of `make check`, because Home Assistant needs a newer Python than the gateway and
is a large download. Run it after changing discovery, payloads, the state document or
home-assistant.md.

| Suite | Files | Needs |
| --- | --- | --- |
| Core rules | `tests/test_text.py`, `test_budget.py`, `test_commands.py`, `test_contacts.py`, `test_airtime.py`, `test_mesh.py`, `test_lpp.py` (CayenneLPP frames and the names of their readings), `test_telemetry.py` (the telemetry schedule and each companion's state on a set clock), `test_adverts.py` (the check for positions in adverts: logged adverts, the radio's push and the record) | nothing |
| Architecture | `tests/test_architecture.py` (the core and the codec import no I/O modules, ADR 0007) | nothing |
| Protocol | `tests/test_framing.py`, `test_codec.py` (golden frames from the firmware source), `test_packet.py`, `test_links.py` | nothing |
| Radio session and bridge | `tests/test_session.py`, `test_bridge.py`, `test_observer.py`, `test_contact_manager.py`, `test_profile.py`, `test_telemetry_poller.py` (emulated radio, with remote companions that answer telemetry) | nothing |
| MQTT | `tests/test_mqtt_client.py` | `mosquitto` for the tests marked `broker` |
| Documents, Home Assistant discovery, payloads | `tests/test_views.py`, `test_discovery.py`, `test_payloads.py`, `test_contact_devices.py`, `test_manage_entities.py` | nothing |
| Transports | `tests/test_transport.py` (pseudo terminal, local TCP) | Linux |
| Configuration, state, history, packaging | `tests/test_config.py`, `test_state.py`, `test_history.py`, `test_build_pyz.py`, `test_release_notes.py` | nothing |
| CLI | `tests/test_cli.py`, `test_cli_contacts.py` | `mosquitto` for the tests marked `broker` |
| systemd | `tests/test_systemd.py` (notification protocol on a local socket) | Linux |
| System | `tests/test_system.py` (the service as a process with an emulated radio and a broker) | `mosquitto` |
| Documentation, tooling | `tests/test_docs.py` (relative links and headings in every Markdown file), `test_tooling.py`, `test_public_interface.py` (the snapshot of the public interfaces, [development.md](development.md#compatibility)), `test_release_plan.py`, `test_fetch_firmware.py` (the pinned fetch of the firmware, from a local repository) | git |
| Home Assistant contract | `home-assistant/tests/test_contract.py`, `test_contact_devices.py`, `test_manage_contacts.py`, `test_telemetry.py`, `test_adverts.py`, `test_traces.py` (above) | `make ha-test`: Python 3.14.2 or newer |
| Firmware | `tests/firmware/test_smoke.py` (the companion and the repeater start on the simulated air, all on EU/UK Narrow with CR 4/8; a channel message crosses the air; the gateway's probe reads the companion through its serial transport, twice; the port is exclusive and opens again right after a close), `test_air.py` (the air with stand-in nodes: time on air, collisions, half duplex, one-way links, loss, delay, radio settings), the scenarios of checklist steps 29, 30, 33, 34, 35 and 39 (`test_first_start.py`, `test_permissions.py`, `test_paths.py`, `test_answer_sizes.py`, `test_message_order.py`), the parts of steps 41 and 43 on the check for positions in adverts (`test_advert_check.py`), and `test_adverts_and_positions.py`, the firmware's part of step 31 ([below](#scenarios-of-the-hardware-checklist)) | `make firmware-test`: git, `g++`, `libssl-dev`, `mosquitto` |
| Installer | `tests/test_installer.py` (stub `pct`, `pveam`, `pvesm`, `pvesh`, `pveversion`, `dpkg-query`, `whiptail`) | bash |

Tests marked `broker` start a private `mosquitto` on a free local port. When `mosquitto` is not
installed they **fail** with a hint; set `MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1` to skip them on
purpose. One transport test and one firmware test are skipped when running as root, because root
bypasses the exclusive lock they check; CI runs them as a normal user.

## Tests against the real firmware

```sh
make firmware-test   # fetch and build MeshCore's firmware, then run the gateway against it
```

The tests above talk to an emulated radio, which answers as the firmware's source says. These
tests run the firmware itself: MeshCore's companion and repeater firmware of the release that
`tests/firmware/sources.toml` pins, compiled for Linux, each node a process. A simulated LoRa air
connects them, and the gateway talks to a companion through its own serial transport on a pseudo
terminal ([ADR 0026](decisions/0026-gateway-tested-against-the-real-firmware-built-natively.md)).
They need git, `g++` and OpenSSL's headers, and `mosquitto` for the scenarios with the gateway's
service (`apt install g++ libssl-dev mosquitto`). `make test` and `make check` leave them out, so
those need neither a compiler nor network; CI runs them in a job of their own.

- `make firmware-fetch` puts the pinned sources into `build/firmware/src`: the firmware by its
  tags `companion-vX.Y.Z` and `repeater-vX.Y.Z`, and CayenneLPP in the version the firmware's
  `platformio.ini` names. `tools/fetch_firmware.py` refuses a tag that points to another commit
  than the pinned one. A second run needs no network. `MESHCORE_SOURCE` and `CAYENNELPP_SOURCE`
  name local git repositories to fetch from instead of GitHub, for example a clone of MeshCore.
- `make firmware` builds `build/firmware/bin/companion` and `build/firmware/bin/repeater`. The
  firmware is compiled unchanged: `tests/firmware/shim` stands in for the Arduino core and the
  libraries, `tests/firmware/board` for the board (the radio on the simulated air, the USB serial
  on a pseudo terminal, the flash in a directory). `make firmware V=1` shows the compiler's
  command lines.
- `make firmware-test` runs `tests/firmware` with pytest; `FIRMWARE_TESTS` narrows it, for
  example `make firmware-test FIRMWARE_TESTS=tests/firmware/test_paths.py`. Once the firmware is
  built, `.venv/bin/python -m pytest tests/firmware/test_paths.py` works too.
  `tests/firmware/air.py` is the air, in the test's process; `tests/firmware/node.py` starts a
  node; the fixtures in `tests/firmware/conftest.py` give a test the air and its nodes.

Every node runs the EU/UK Narrow preset with CR 4/8 (869.618 MHz, 62.5 kHz, SF 8), the settings
the hardware checklist assumes. Each node writes its packet log, debug output and, on the
repeater, its command line's replies to `node.log` in its directory under the test's temporary
directory; the air writes `air.log` there. A failed test shows the last lines of each. A node
without GPS takes about 5 s to start, because the firmware looks that long for a GPS module.

The simulation does not show radio propagation, the noise floor and the capture of a stronger
packet, the time on air of a real chip, GPS, USB, the code paths of ESP32 and nRF52 boards (it
builds the RP2040 ones), the phone app or Home Assistant. Those stay on the hardware checklist.
The nodes run in real time, so a scenario of an hour takes an hour.

### Scenarios of the hardware checklist

The scenarios play parts of steps 29–43 of the [hardware checklist](#hardware-checklist) with
the gateway's service and the real firmware; [below the checklist](#steps-2945-on-the-simulated-air)
it says which. `tests/firmware/household.py` starts radio A, Bob's companion B with a GPS (its fix
at a public landmark) and, for the steps with hops, repeaters in a chain.
`tests/firmware/app.py` plays Bob's phone app on B's serial port, on the gateway's own transport,
framing and codec: it sets B up as [companions.md](companions.md) says, and sends adverts,
messages and path resets when a scenario says so. The app's own policies, such as when it gives up
a direct attempt and floods, are scripted. `tests/firmware/gateway.py` runs the service in the
test's process, as `meshcore-home-gateway run` runs it, on A's pseudo terminal and a private
`mosquitto`, and keeps everything it publishes.

The nodes run in real time. So that a scenario takes a minute, not hours, the fixture
`fast_schedule` shortens the telemetry schedule: the airtime lanes start full, every interval is
15 s, and a retry comes after 10 s. The rules the checklist checks stay as they are: the 10 s
quiet after a message, the 10 s gap after every outcome, the answer waits, the flood limits and the
routes. The first-start test runs with the real lanes and shortens only the hour of its hold.
The air has knobs for a worse mesh: links one way only, `loss` (the share of packets a receiver
misses) and `delay_s` (a packet reaches the receiver that much later, as if the mesh held it).
Collisions are real, so the scenarios link only the nodes a step needs. A node hears nothing
while it transmits (half duplex: the air records such a reception as `deaf`).

`tests/firmware/test_adverts_and_positions.py` runs no gateway. It pins down what the firmware
does with adverts and positions, which the gateway's check for positions in adverts
([ADR 0025](decisions/0025-positions-in-adverts-checked-from-the-packet-log.md)) relies on: at
receive delay 0 an accepted advert's packet log (0x88) is the frame right before the
radio's push (0x80); a node the radio does not add arrives as 0x8A right after its log; an advert
flagged with a position (0x10) sets the contact's position, one without the flag leaves it, and
one flagged with 0, 0 zeroes it; the device's Advert button sends zero hop, which a repeater does
not forward, while the app's flood advert is forwarded; with the GPS on and no fix, telemetry
carries the last fix, and after a reboot the position saved with the settings, at altitude 0.

`tests/firmware/test_advert_check.py` runs that check in the gateway's service, with telemetry
off, so that a position on B's document can only come from B's adverts. The board's control line
`advert` presses B's Advert button; the request on `add_contact`, which `contacts add` sends,
stands in for Home Assistant's *Add heard node*. The restart test stops and starts the service on
one state directory. A stand-in on the air (`StandIn` in `air.py`) plays a stranger's radio that
sends A copies of B's adverts with a path of its own.

The whole suite takes about 14 minutes on four cores, the longest test about 2 minutes, so none is
marked slow. A failed scenario's report ends with the last lines of each node's log
and of the air's; `--log-file=gateway.log --log-file-level=DEBUG` keeps the gateway's own log.

### Testing another firmware release

1. Put the release's tags and the commit they point to into `tests/firmware/sources.toml`.
   `git ls-remote https://github.com/meshcore-dev/MeshCore.git 'refs/tags/*-vX.Y.Z*'` lists them;
   for an annotated tag the line ending in `^{}` names the commit. Keep CayenneLPP at the version
   the release's `platformio.ini` names.
2. Run `make firmware-test`. A release that needs more of the Arduino core or of a library fails
   to build or to link: extend the stand-in in `tests/firmware/shim` or `tests/firmware/board`.
3. The smoke test expects the gateway to count the release as tested. Adding its firmware code to
   `TESTED_FIRMWARE` in `src/meshcore_home_gateway/core/profile.py` follows the hardware checklist
   with the release and is an addition to the public interface
   ([development.md](development.md#compatibility)).

## Installer dialogs with the real whiptail

The installer tests use a whiptail stub. After changing dialogs, run them once with the real
program (Debian package `whiptail`) in a pseudo terminal against the same stub Proxmox commands:

```sh
.venv/bin/python -I tests/manual/installer_dialogs.py
```

It types the answers of a default setup, with a password of non-ASCII characters and under a
locale the system lacks (as ssh may pass one on), prints the resulting targets and broker settings,
and exits non-zero when they differ from what was typed. It sends keys on timing, so it is a manual
check, not part of `make check`. It caught three bugs the stub could not show: dialogs drawn into
captured output, names starting with `-` taken for options, and non-ASCII characters dropped in a
locale the host lacks ([platform-notes.md](platform-notes.md#whiptail)).

## Hardware checklist

The automated tests emulate the radio or run its firmware on a simulated air. This checklist covers
what they cannot: real radio links, acknowledgements over the air, a USB cable pulled out, the watchdog of a real systemd. It is
the same for every installation and needs no particular hardware: any two MeshCore radios with
companion firmware will do. Run it before every release and after raising the supported firmware
version, and record the firmware versions, radio models and results in the pull request. The
core steps 1–4, 10–12, 15, 16 and 18 test the bridge end to end and are run for every release
(docs/development.md); the others where their feature changed. Steps 15 (the `pct` part), 19 and
20 apply to the Proxmox installer only.

Setup: radio **A** on the gateway (USB), radio **B** as a handheld or phone companion in range,
both on the same channel settings; B knows A as a contact and the reverse. Steps 21–28 add and
remove B: for them take B out of `[targets]` and `commands.senders` and restart the service,
because the gateway never removes a contact target or command sender (and the managed profile
adds it back).

| # | Step | Expected |
| --- | --- | --- |
| 1 | `meshcore-home-gateway probe --serial /dev/serial/by-id/…` with the service stopped | firmware, radio settings, how the radio adds contacts, contacts and channels listed, and what the profile would change |
| 2 | Start the service | `availability` = `online`; `state.radio` = `connected`; battery plausible |
| 3 | Publish to `send/<channel alias>` | B shows the message; `delivery`: `queued`, `sent` |
| 4 | Publish to `send/<contact alias>` | B shows it; `delivery`: `confirmed` with a round trip time |
| 5 | Switch B off, send to its alias | attempts 1..`max_attempts`, path reset after attempt `flood_after`, then `failed` |
| 6 | Send a text longer than 160 bytes | truncated with `...` (or `rejected` if `truncate = false`) |
| 7 | With `ascii_only = true`, send umlauts | B shows the transliteration |
| 8 | Send the same text twice within the window | second is `duplicate` |
| 9 | Set `budget_s_per_hour = 1` and `burst_s = 5` (`burst_s = 20` at SF 11 or 12 with 62.5 kHz, where one message takes longer than 5 s), send messages of about 100 characters at once until `airtime_available_s` in the state is below the `airtime_ms` of one (more than ten at fast presets) | those sent while airtime lasts, the rest `expired` (`airtime_budget`) without waiting |
| 10 | B sends a direct message and a channel message | two `received` events with sender, SNR, path length |
| 11 | B (allowlisted) sends `status` | `command` event `status`; `received` shows `accepted` |
| 12 | A not allowlisted contact sends `status` | no `command` event; `received` shows `sender_not_allowed` |
| 13 | `systemctl stop meshcore-home-gateway`; B sends `status`; start the service | `received` shows `sender_unverified`, the log says why; B sends again and gets a `command` event |
| 14 | Switch on automatic adding, telemetry for all and the position in adverts in the app, start the service, read `journalctl -u meshcore-home-gateway` and the `settings` document | the log names each setting set back; `settings.status` is `written`; `probe` afterwards shows manual adding, telemetry denied, no position in adverts, and 10 % in 869.40–869.65 MHz; a restart writes nothing (`status` `ok`). With `radio.profile.manage = "observe"`: nothing written, a warning that the radio adds contacts on its own (ADR 0009, ADR 0020) |
| 15 | Unplug A's USB cable for 30 s, plug it in; in a Proxmox container also `pct reboot <id>` | `radio` = `disconnected`, then `connected`; queued messages go out (deployment.md: the container keeps the old device until it restarts) |
| 16 | Stop and start the broker | bridge reconnects; `availability` back to `online` |
| 17 | `kill -STOP` the service process | systemd watchdog restarts it within the watchdog interval |
| 18 | Home Assistant | device with all entities; notify entity sends; event entities fire |
| 19 | Proxmox installer from its address (`bash -c "$(wget -qO- URL)"`), default setup, then `--reconfigure` and `--update` | container runs the service; nothing new on the host but the template; reconfigure keeps unasked settings; update keeps the configuration |
| 20 | Switch the host off and on with the radio unplugged | the container does not start (expected); after plugging in, `pct start <id>` |
| 21 | B advertises (not yet a contact of A); `meshcore-home-gateway contacts add <B's key from contacts list, or KEY12…KEY8>` | `added`; `probe` lists B with its name and type, no path; `contacts/<key>.verified` is `true`; a direct message to B is `confirmed` after B's next advert |
| 22 | With a radio C that A has never heard (no advert, no message): `contacts add` with C's contact link (from the app's "Share contact"); `contacts remove` C; `contacts add` with C's exported contact card | each parses to C's key, type and claimed name; the radio lists C; `verified` `false` until C's first advert replaces name and type. (A link or card of a node A heard in the last 7 days, such as B after a removal, is added with the heard record: `verified` `true` at once, the claimed name ignored.) |
| 23 | `contacts remove <B's key>`, then pull A's power within 5 s of the answer; start again | the change can be lost (the radio writes its contacts file 5 s later); the next table read shows the truth |
| 24 | Fill the table (or use a radio with a small table) with overwrite off; add one more | `refused` `table_full` before anything is written; with the profile only observing and overwrite on, the same |
| 25 | Scan the QR code of the gateway's `contact_link` with the MeshCore app | the app adds the gateway as a companion; nothing is written to A |
| 26 | After the add of step 21, replay one of B's older adverts (a second radio with a captured packet, test radio only) | the radio drops it; after a removal, B's next advert arrives as a new node |
| 27 | In Home Assistant, with B not a contact of A (`contacts remove` it first, check with `contacts list`): B advertises; pick B in *Add heard node* on the gateway's device; then *Remove from radio* on B's device; then paste B's contact link into *Add contact (key or link)* | B is offered within 30 s with the first 12 and last 8 digits of its key; `added`, B's device appears; `removed`, the device goes and B is offered again; `added` once more. With `[contacts] manage = false` all three are `unavailable` |
| 28 | Add the QR card and the dashboard row of home-assistant.md with B's entity ID; press the row's button; add B again, set `[contacts] manage = false` and restart | the card shows a QR code (step 25 scans it); the row asks before it removes, and after the removal it says that the entity is missing; with management off the row's button is disabled |

Steps 29–45 need B to be a companion **with GPS**, set up as [companions.md](companions.md)
describes and in A's table, A adding contacts by hand only; a repeater for the steps with hops;
for step 30 B2, a companion with an environment sensor (a BME280 or similar); optionally C, a
tracker board that keeps a stored position, best a T1000-E (its light and temperature sensors).
For step 42 a companion that never had a position (no GPS fix ever, none set in the app), if at
hand; for step 43 a copy of a state directory of 1.1.0a2, taken before the update; for step 44 a
radio over TCP; for step 45 a browser on Home Assistant. Telemetry on (the default);
`log_level = "debug"` where the log is read. The session logs no command it sends: that the
gateway never sends `EXPORT_CONTACT` (17) is shown by the emulator's record of the commands in the
automated tests.

| # | Step | Expected |
| --- | --- | --- |
| 29 | Start the service with B beside A, once on a state directory where telemetry never ran and once again; then carry B behind one repeater and send the gateway a message from B (the radio learns the path through the repeater, as in step 33) | where telemetry never ran: `state.telemetry` `paused` with `telemetry_reason` `first_start` from `connected` for an hour, one warning with the number of companions, then the first request; on the later start the first request about 76 s after `connected` (EU/UK Narrow), direct; `contacts/<B>`: the position, `position_source` `telemetry`, `position_accuracy_m` 15, `telemetry_state` `answering`, `battery_v` close to B's battery in the app; *Telemetry* `answered` once; *Battery*, *MCU temperature* (nRF52, ESP32) and *Telemetry state* on B's device, no warning in Home Assistant's log; then about every 15 min at rest; behind the repeater about every 31 min at rest (47 min over two repeaters; started behind one, the first request after about 82 s); `state.telemetry_airtime_1h_s` plausible against `stats.tx_airtime_1h_s` |
| 30 | On B: telemetry requests "No"; "From Specific Contacts" with the gateway allowed nothing, base only, base and location, everything; remove the gateway from B's contacts. Then B2 with everything allowed, C, and B2 with the environment permission taken away | silence; silence; battery only; with position; with environment; silence. Requests on B's path only, never a flood or a path reset. "No" and nothing allowed: `no_answer` and **no** `unanswered` (B never answered); base only: `no_position`, battery and MCU temperature only; base and location: `answering`. B2: its channel 2 readings with their units; C: `ch1_temperature`, `ch1_luminosity` (0–100, no unit), *MCU temperature*; B2 without environment: its readings `unknown` from the next answer |
| 31 | B: GPS off; on without a fix; C with GPS off; B at rest indoors for an hour | GPS off, without position: the tracker keeps the last one for an hour, `position_received_at` stops, then `no_position` and the tracker `unavailable` (the sensors stay); on without a fix: B keeps answering with its last valid position (after a reboot, the saved one): the tracker stays, `position_current` stays `true`; note whether the position repeats bit for bit; C keeps sending its stored position; at rest no history rows and no tracker states |
| 32 | Walk B 200 m away and back, within range | moving: requests about every 90 s; resting again after two answers under 50 m; a history row and a trace point per move of 20 m or more |
| 33 | Carry B out of direct range but within the mesh (a repeater hears both); after two unanswered requests, send the gateway a message from B | no path reset by the gateway; B's app floods the message after its direct attempts fail, A stores the path B sends (0x81); the next request goes direct over the repeater and is answered |
| 34 | Debug log of answers, direct and after a flood; force a timeout (B two hops away) | "answer of N bytes, 19 of them readings": N = 28 direct (9 zero bytes), 26 after a flood to a neighbour (7), one less per repeater; no "bytes not decoded"; a late answer after the next request appears as an ignored 0x8C (`push 0x8c ignored`) |
| 35 | Send messages to B while telemetry runs, also during a large table read and right after a flooded request | no request between a message's send and its confirmation, nor within 10 s after; how long a message sent right after a flooded request waits (log times of the send and of the radio's transmission) |
| 36 | Stop and start the service five times within 10 minutes; then exclude B with its switch *Telemetry* and restart twice; then `meshcore-home-gateway contacts telemetry <B> on` | no request within 75 s of any start; B's next request at its saved due time; `state.json` `telemetry` holds no position; across the restarts no tracker or sensor of B turns `unavailable` beyond the restart's seconds. Excluded: `contact` `excluded` at once, B's values gone, no request to B over the restarts, `telemetry-excluded.json` holds B's key; `on`: `included`, B asked again within a minute, `answered` |
| 37 | Radio over TCP (if available): repeat 29 during a large table read; switch automatic adding on, then only repeater adding. Unplug A's USB cable for more than an hour, plug it back | a silence during the read is retried without back-off; with automatic adding of any type `state.telemetry` is `paused`, `telemetry_reason` `auto_add`. Unplugged: `paused`, `no_radio` at the next state publication; an hour after its last answer B `away`, its tracker `unavailable`, no `unanswered`; plugged back: `on`, B `answering` within its interval |
| 38 | Give a `person` in Home Assistant B's tracker only. Carry B out of all range silently (no message, no advert) for an hour, bring it back | no path reset, no flood; direct requests at most 30 min apart while it is away; about 16 min after it left (at rest; about 2 min while moving) `away`, *Telemetry* `unanswered`, tracker and telemetry sensors `unavailable`, the person `unknown`; B's position updates within 30 min of its return: `answering`, `answered`, all available |
| 39 | B two hops away, with its path to the gateway reset on B (it floods its answer), `publish_rx_log` on | a flooded RESPONSE in the packet log after a direct request; `telemetry_mesh_airtime_1h_s` rises by the direct request plus about 12.6 s; A sends its path back 3 s later; the next answer comes direct |
| 40 | As 38, and send B a message 10 min after it left | the message: two direct attempts, a path reset, a flood, `failed`; `contacts/<B>.path_known` `false` at once; no request to B until it advertises after its return; `away` within `max(1 h, 3 × its interval)` after its last answer, without `unanswered`; a day after its last answer `no_path` until it advertises; then one flood, the path, direct requests again |
| 41 | On B, with a GPS fix: "Share Position in Advert" on; press *Advert* beside A. Behind the repeater (out of A's direct range): press *Advert*, then send a flood advert from the app. Then off, and *Advert* beside A again. Repeat with B excluded from telemetry (its switch *Telemetry* off); end the repeat with sharing on and an *Advert*, then include B again. Last: *Remove from radio* on B's device; with sharing on, press *Advert* beside A; add B with *Add heard node* | on: within about 2 s `contacts/<B>.advert_carries_position` `true` and `advert_checked_at`; *Position in adverts* on B's device and the gateway's sensor on; one `contact` `position_in_advert`, no coordinate in it; the info line names the check. Behind the repeater: the button's advert does not arrive (zero hop: `last_advert_at` unchanged); the app's flood advert is checked (`advert_checked_at` moves), no second event. off: `false`, both sensors off, no event; `advert_position` still `true` and the info line "no position (the radio's record keeps an older one)". Excluded: B's own sensor as above, the gateway's sensor off, no event; B's tracker shows the advert position while it shares and turns `unavailable` at the advert without it. Included again: one `position_in_advert`, the gateway's sensor on. Added again: `advert_carries_position` `true` at once with `advert_checked_at` the time of the advert the gateway heard, one event; no position from adverts until B's next advert (B is asked within a minute, and its telemetry answer gives a position) |
| 42 | (a) After step 41 (B saved its fix), reboot B indoors with GPS on and no fix, sharing on, *Advert* beside A. (b) If at hand, the same with a companion that never had a position; then outdoors with a fix, *Advert* | (a) B sends its saved position: `true`, `advert_position` `true`, B's tracker shows the saved place with `position_at` the time of the table read, not the advert's (the position changed while the check already carried); note in [platform-notes.md](platform-notes.md#meshcore-companion-firmware) whether the position repeats bit for bit across adverts. (b) `true` with `advert_position` `false` (0, 0), the event once; an earlier advert position gone from its tracker; with the fix: `advert_position` `true`, no second event |
| 43 | Restart the service between the two adverts of 41. With sharing off on B (checked `false`): stop the service, switch sharing on B, press *Advert* beside A, start it; then *Advert* again. Separately, start 1.1.0a3 on a state directory of 1.1.0a2 whose radio holds an old advert position of B (sharing off since) | the restart raises and clears nothing; `state.json` `advert_checks` holds B with no position. After the stop: both sensors on (B's record took its position; the advert is unchecked), `advert_checked_at` older than `last_advert_at`, no event, no old position back on the tracker, one debug line "not checked"; the next *Advert* checks it: `advert_checked_at` moves, one `position_in_advert`. First start: B's sensor on (the record), the gateway's sensor keeps its 1.1.0a2 state until the radio connects, then on; `advert_checked_at` `null`, no event; B's *Advert* turns both off |
| 44 | Radio over TCP (if available): B adverts ten times during a large table read; then an hour behind a busy repeater with B sending a flood advert from the app every 5 minutes; count B's adverts | the debug lines "not checked" (with their reason) and the info lines of checks counted, and compared with B's adverts and with the changes of `advert_checked_at`; an advert whose push was dropped never turns `advert_carries_position` to `false` while B shares; the next advert checks it. Write the counts into [platform-notes.md](platform-notes.md#meshcore-companion-firmware): they decide whether a later release needs the radio's stored copy of the advert, and whether 40 s and 64 logged adverts are enough in a busy mesh |
| 45 | Browser: the guide's map card ([Traces on the map](home-assistant.md#traces-on-the-map)) with B's tracker. Walk B 200 m away and back; let it expire (step 38) and come back; restart the gateway; then exclude `device_tracker.mesh_*` from the recorder, restart Home Assistant, open the card. For slice 8: a `show_all` card with `conditions` on an attribute only mesh trackers have (`position_source`), `hours_to_show: 24`, with a phone tracker in Home Assistant, and B's tracker `unavailable` when the card loads | a dot per move of 20 m or more, a line between, older ones fainter; none while B rests; the expiry as a straight segment; the restart as a second dot on one spot; a trail of exactly two points drawn with sensible opacity; with the exclusion no trail after the reload, a `person` of B still has one. `show_all`: whether the phone's trail shows although its marker is filtered, and whether a tracker `unavailable` at load gets a trail. Both into [platform-notes.md](platform-notes.md#home-assistant-dashboards); slice 8 decides its map and the tracker attributes on them |

### Steps 29–45 on the simulated air

The [firmware tests](#scenarios-of-the-hardware-checklist) play parts of steps 29–43 against the
real firmware, with the gateway's service: the telemetry steps on a shortened schedule, the steps
on adverts with telemetry off. They do not replace a step: what they cannot show stays here.

| # | Shown by the firmware tests | Stays on the hardware checklist |
| --- | --- | --- |
| 29 | **partly**, `test_first_start.py`: the first-start hold (`paused`, `first_start` from connected, one warning with the number of companions, no request; the hour shortened to 15 s); on the later start the first request 75.6 s after connected, direct; B's document with the position from telemetry, `position_accuracy_m` 15, `answering`, `battery_v` as B's app reads it; *Telemetry* `answered` once; an answer of 28 bytes | the hour itself; the intervals (15, 31 and 47 minutes, 82 s behind a repeater); Home Assistant's entities and log; a real board's battery and MCU temperature; `telemetry_airtime_1h_s` against `stats.tx_airtime_1h_s` (the simulation measures its own estimate) |
| 30 | **partly**, B's part, `test_permissions.py`: "No", nothing allowed, base only, base and location, everything, the gateway removed give silence, silence, battery and MCU temperature only, the position, the same (B has no environment sensor), silence; every request direct; `no_answer` without `unanswered`, then `no_position`, then `answering` | B2 and C: their sensors; the app's labels and counts |
| 31 | **partly**, the firmware's part, `test_adverts_and_positions.py`: with the GPS on and no fix a companion still sends a position, its last fix, after a reboot the saved one at altitude 0. The expected "without position" for "on without a fix" does not hold for this firmware | the whole step: check "on without a fix" with a real GPS |
| 33 | **partly** (most), `test_paths.py`: the requests on the stale direct path go unanswered, direct, without a path reset; B's flooded message brings A the path over the repeater (0x81); the next request goes direct over the repeater and is answered | the app's own retries (scripted: one direct attempt, then the path reset and the flood) |
| 34 | **fully**, `test_answer_sizes.py`: 28 bytes with 9 zero bytes direct, over no, one and two repeaters; after a flood 26 with 7 to a neighbour, 25 with 6 over one repeater, 24 with 5 over two; no "bytes not decoded"; an answer held back past the wait is taken as late until the next request, and after it the radio pushes it as 0x8C, which the gateway ignores | a check with each firmware release; here the late answer comes from the air's delay knob on a neighbour, not from distance |
| 35 | **partly** (most), the ordering rules, `test_message_order.py`: no request between a message's queueing and its confirmation, nor within 10 s after, for a message right after a flooded request and for messages while B is asked; the wait of the message after the flood is recorded, not judged | a large table read; the waits on a real radio over USB |
| 39 | **partly** (most), `test_paths.py`: over two repeaters, the flooded RESPONSE after a direct request in the packet log (`rx`), `telemetry_mesh_airtime_1h_s` up by the direct request plus 12.6 s, A's path back 3.0 s later, the next answer direct | whether 12.6 s matches real air; the SNR and RSSI in `rx` are those the test gives the links |
| 41 | **partly**, `test_advert_check.py`. Beside A, sharing on with a GPS fix: within 3 s of the button's advert reaching A (2.0 s measured), `advert_carries_position` `true` with `advert_checked_at` the advert's time, B in the index's `advert_carries_position` (the gateway's sensor), the advert's position on B's document, one `position_in_advert` without a coordinate, the info line; sharing off: `false`, B out of the index, the advert position gone, `advert_position` still `true`, the info line "no position (the radio's record keeps an older one)", no event. Behind the repeater: the button's zero-hop advert reaches only the repeater, `last_advert_at` stays; the app's flood advert is checked, one hop, with one event, and the next one moves `advert_checked_at` without a second. Added after it was heard: A, which never held B, gets B's advert as a node it does not add; B added with `add_contact` starts `true` with `advert_checked_at` that advert's time, one `position_in_advert` after `added`, no position until B's next advert, which raises no second event. The advert's hops, SNR and RSSI are those of the direct reception the radio took, not of the repeater's copy logged after it (`rx` shows both). Without its own log: a stand-in sends A a late copy of B's older advert, then B's next advert over 63 hops, too long for the radio's packet log; the radio takes it, and the gateway gives it no hops, SNR or RSSI (not those of the late copy, the frame before the push) and no check (`advert_checked_at` stays, one debug line "not checked (not logged)", no second event). Forged copies and a receive delay above 0 are shown only by the unit tests (`tests/test_observer.py`, `tests/test_adverts.py`) | Home Assistant: *Position in adverts*, the gateway's sensor, the tracker; the repeat with B excluded from telemetry and included again; *Remove from radio* and *Add heard node* (here B was never on A); B asked after the add, and its telemetry position; the time on a real radio over USB |
| 42 | **not yet**: `test_adverts_and_positions.py` shows the saved position in telemetry after a reboot, not in adverts | all |
| 43 | **partly**, `test_advert_check.py`: a restart after the check `true` keeps it (`advert_checked_at`, the advert position) and raises no event; after each run `state.json` `advert_checks` holds B's advert time and flag, no position. Checked `false`, stopped, B shares and advertises beside A, started: `advert_checked_at` older than `last_advert_at`, `advert_carries_position` `true` and B in the index, no position on B's document, no event, one debug line "not checked"; the next Advert checks it, `advert_checked_at` moves, one `position_in_advert`; B's document is never cleared | Home Assistant's sensors and the tracker; the first start on a state directory of 1.1.0a2 |
| 32, 36, 37, 38, 40, 44, 45 | **not yet**: they take hours, Home Assistant, a browser, a TCP interface or a USB cable | all |
