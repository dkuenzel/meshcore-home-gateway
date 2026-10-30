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
controls that add and remove contacts and `test_telemetry.py` the companions' telemetry (their
readings, states, switch and expiry, a `person` and its presence triggers, the telemetry examples)
into the MQTT integration of the Home Assistant release that
`pytest-homeassistant-custom-component` pins (`home-assistant/requirements.in`; the environment is
installed from the hash-pinned `home-assistant/requirements.txt` into `.venv-homeassistant`, about
900 MB). It checks what [home-assistant.md](home-assistant.md) promises, including the example
automations taken from that page, and that Home Assistant logs no warning. It is a separate CI job
and not part of `make check`, because Home Assistant needs a newer Python than the gateway and
is a large download. Run it after changing discovery, payloads, the state document or
home-assistant.md.

| Suite | Files | Needs |
| --- | --- | --- |
| Core rules | `tests/test_text.py`, `test_budget.py`, `test_commands.py`, `test_contacts.py`, `test_airtime.py`, `test_mesh.py`, `test_lpp.py` (CayenneLPP frames and the names of their readings), `test_telemetry.py` (the telemetry schedule and each companion's state on a set clock) | nothing |
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
| Documentation, tooling | `tests/test_docs.py` (relative links and headings in every Markdown file), `test_tooling.py`, `test_public_interface.py` (the snapshot of the public interfaces, [development.md](development.md#compatibility)), `test_release_plan.py` | nothing |
| Home Assistant contract | `home-assistant/tests/test_contract.py`, `test_contact_devices.py`, `test_manage_contacts.py`, `test_telemetry.py` (above) | `make ha-test`: Python 3.14.2 or newer |
| Installer | `tests/test_installer.py` (stub `pct`, `pveam`, `pvesm`, `pvesh`, `pveversion`, `dpkg-query`, `whiptail`) | bash |

Tests marked `broker` start a private `mosquitto` on a free local port. When `mosquitto` is not
installed they **fail** with a hint; set `MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1` to skip them on
purpose. One transport test is skipped when running as root, because root bypasses the exclusive
lock it checks; CI runs it as a normal user.

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

The automated tests emulate the radio. This checklist covers what an emulator cannot: real radio
links, acknowledgements over the air, a USB cable pulled out, the watchdog of a real systemd. It is
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

Steps 29–40 need B to be a companion **with GPS**, set up as [companions.md](companions.md)
describes and in A's table, A adding contacts by hand only; a repeater for the steps with hops;
for step 30 B2, a companion with an environment sensor (a BME280 or similar); optionally C, a
tracker board that keeps a stored position, best a T1000-E (its light and temperature sensors).
Telemetry on (the default); `log_level = "debug"` where the log is read.

| # | Step | Expected |
| --- | --- | --- |
| 29 | Start the service with B beside A, once on a state directory where telemetry never ran and once again; then carry B behind one repeater and send the gateway a message from B (the radio learns the path through the repeater, as in step 33) | where telemetry never ran: `state.telemetry` `paused` with `telemetry_reason` `first_start` from `connected` for an hour, one warning with the number of companions, then the first request; on the later start the first request about 76 s after `connected` (EU/UK Narrow), direct; `contacts/<B>`: the position, `position_source` `telemetry`, `position_accuracy_m` 15, `telemetry_state` `answering`, `battery_v` close to B's battery in the app; *Telemetry* `answered` once; *Battery*, *MCU temperature* (nRF52, ESP32) and *Telemetry state* on B's device, no warning in Home Assistant's log; then about every 15 min at rest; behind the repeater about every 31 min at rest (47 min over two repeaters; started behind one, the first request after about 82 s); `state.telemetry_airtime_1h_s` plausible against `stats.tx_airtime_1h_s` |
| 30 | On B: telemetry requests "No"; "From Specific Contacts" with the gateway allowed nothing, base only, base and location, everything; remove the gateway from B's contacts. Then B2 with everything allowed, C, and B2 with the environment permission taken away | silence; silence; battery only; with position; with environment; silence. Requests on B's path only, never a flood or a path reset. "No" and nothing allowed: `no_answer` and **no** `unanswered` (B never answered); base only: `no_position`, battery and MCU temperature only; base and location: `answering`. B2: its channel 2 readings with their units; C: `ch1_temperature`, `ch1_luminosity` (0–100, no unit), *MCU temperature*; B2 without environment: its readings `unknown` from the next answer |
| 31 | B: GPS off; on without a fix; C with GPS off; B at rest indoors for an hour | without position: the tracker keeps the last one for an hour, `position_received_at` stops, then `no_position` and the tracker `unavailable` (the sensors stay); indoors without a fix for less than an hour the tracker stays; C keeps sending its stored position; at rest no history rows and no tracker states |
| 32 | Walk B 200 m away and back, within range | moving: requests about every 90 s; resting again after two answers under 50 m; a history row and a trace point per move of 20 m or more |
| 33 | Carry B out of direct range but within the mesh (a repeater hears both); after two unanswered requests, send the gateway a message from B | no path reset by the gateway; B's app floods the message after its direct attempts fail, A stores the path B sends (0x81); the next request goes direct over the repeater and is answered |
| 34 | Debug log of answers, direct and after a flood; force a timeout (B two hops away) | "answer of N bytes, 19 of them readings": N = 28 direct (9 zero bytes), 26 after a flood to a neighbour (7), one less per repeater; no "bytes not decoded"; a late answer after the next request appears as an ignored 0x8C (`push 0x8c ignored`) |
| 35 | Send messages to B while telemetry runs, also during a large table read and right after a flooded request | no request between a message's send and its confirmation, nor within 10 s after; how long a message sent right after a flooded request waits (log times of the send and of the radio's transmission) |
| 36 | Stop and start the service five times within 10 minutes; then exclude B with its switch *Telemetry* and restart twice; then `meshcore-home-gateway contacts telemetry <B> on` | no request within 75 s of any start; B's next request at its saved due time; `state.json` `telemetry` holds no position; across the restarts no tracker or sensor of B turns `unavailable` beyond the restart's seconds. Excluded: `contact` `excluded` at once, B's values gone, no request to B over the restarts, `telemetry-excluded.json` holds B's key; `on`: `included`, B asked again within a minute, `answered` |
| 37 | Radio over TCP (if available): repeat 29 during a large table read; switch automatic adding on, then only repeater adding. Unplug A's USB cable for more than an hour, plug it back | a silence during the read is retried without back-off; with automatic adding of any type `state.telemetry` is `paused`, `telemetry_reason` `auto_add`. Unplugged: `paused`, `no_radio` at the next state publication; an hour after its last answer B `away`, its tracker `unavailable`, no `unanswered`; plugged back: `on`, B `answering` within its interval |
| 38 | Give a `person` in Home Assistant B's tracker only. Carry B out of all range silently (no message, no advert) for an hour, bring it back | no path reset, no flood; direct requests at most 30 min apart while it is away; about 16 min after it left (at rest; about 2 min while moving) `away`, *Telemetry* `unanswered`, tracker and telemetry sensors `unavailable`, the person `unknown`; B's position updates within 30 min of its return: `answering`, `answered`, all available |
| 39 | B two hops away, with its path to the gateway reset on B (it floods its answer), `publish_rx_log` on | a flooded RESPONSE in the packet log after a direct request; `telemetry_mesh_airtime_1h_s` rises by the direct request plus about 12.6 s; A sends its path back 3 s later; the next answer comes direct |
| 40 | As 38, and send B a message 10 min after it left | the message: two direct attempts, a path reset, a flood, `failed`; `contacts/<B>.path_known` `false` at once; no request to B until it advertises after its return; `away` within `max(1 h, 3 × its interval)` after its last answer, without `unanswered`; a day after its last answer `no_path` until it advertises; then one flood, the path, direct requests again |
