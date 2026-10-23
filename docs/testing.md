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
event payloads into the MQTT integration of the Home Assistant release that
`pytest-homeassistant-custom-component` pins (`home-assistant/requirements.in`; the environment is
installed from the hash-pinned `home-assistant/requirements.txt` into `.venv-homeassistant`, about
900 MB). It checks what [home-assistant.md](home-assistant.md) promises, including the example
automations taken from that page, and that Home Assistant logs no warning. It is a separate CI job
and not part of `make check`, because Home Assistant needs a newer Python than the gateway and
is a large download. Run it after changing discovery, payloads, the state document or
home-assistant.md.

| Suite | Files | Needs |
| --- | --- | --- |
| Core rules | `tests/test_text.py`, `test_budget.py`, `test_commands.py`, `test_contacts.py`, `test_airtime.py` | nothing |
| Architecture | `tests/test_architecture.py` (the core and the codec import no I/O modules, ADR 0007) | nothing |
| Protocol | `tests/test_framing.py`, `test_codec.py` (golden frames from the firmware source) | nothing |
| Radio session and bridge | `tests/test_session.py`, `test_bridge.py` (emulated radio) | nothing |
| MQTT | `tests/test_mqtt_client.py` | `mosquitto` for the tests marked `broker` |
| Home Assistant discovery, payloads | `tests/test_discovery.py`, `test_payloads.py` | nothing |
| Transports | `tests/test_transport.py` (pseudo terminal, local TCP) | Linux |
| Configuration, state, packaging | `tests/test_config.py`, `test_state.py`, `test_build_pyz.py`, `test_release_notes.py` | nothing |
| CLI | `tests/test_cli.py` | `mosquitto` for the tests marked `broker` |
| systemd | `tests/test_systemd.py` (notification protocol on a local socket) | Linux |
| System | `tests/test_system.py` (the service as a process with an emulated radio and a broker) | `mosquitto` |
| Documentation | `tests/test_docs.py` (relative links and headings in every Markdown file) | nothing |
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

It types the answers of a default setup and prints the resulting targets and broker settings. It
sends keys on timing, so it is a manual check, not part of `make check`. It caught the two bugs
the stub could not show: dialogs drawn into captured output, and names starting with `-` taken
for options.

## Hardware checklist

The automated tests emulate the radio. This checklist covers what an emulator cannot: real radio
links, acknowledgements over the air, a USB cable pulled out, the watchdog of a real systemd. It is
the same for every installation and needs no particular hardware: any two MeshCore radios with
companion firmware will do. Run it before every release and after raising the supported firmware
version, and record the firmware versions, radio models and results in the pull request. Steps 15
(the `pct` part), 19 and 20 apply to the Proxmox installer only.

Setup: radio **A** on the gateway (USB), radio **B** as a handheld or phone companion in range,
both on the same channel settings; B knows A as a contact and the reverse.

| # | Step | Expected |
| --- | --- | --- |
| 1 | `meshcore-home-gateway probe --serial /dev/serial/by-id/…` with the service stopped | firmware, radio settings, how the radio adds contacts, contacts and channels listed |
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
| 14 | `journalctl -u meshcore-home-gateway` after the start, with automatic adding switched on | a warning says that the radio adds contacts on its own, in manual mode which types (ADR 0009); none with the recommended settings |
| 15 | Unplug A's USB cable for 30 s, plug it in; in a Proxmox container also `pct reboot <id>` | `radio` = `disconnected`, then `connected`; queued messages go out (deployment.md: the container keeps the old device until it restarts) |
| 16 | Stop and start the broker | bridge reconnects; `availability` back to `online` |
| 17 | `kill -STOP` the service process | systemd watchdog restarts it within the watchdog interval |
| 18 | Home Assistant | device with all entities; notify entity sends; event entities fire |
| 19 | Proxmox installer, default setup, then `--reconfigure` and `--update` | container runs the service; reconfigure keeps unasked settings; update keeps the configuration |
| 20 | Switch the host off and on with the radio unplugged | the container does not start (expected); after plugging in, `pct start <id>` |
