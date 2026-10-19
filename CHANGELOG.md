# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The MQTT API, the configuration
format and the command line are public interfaces.

## [Unreleased]

### Added

- Design document, eleven architecture decision records, protocol reference (MeshCore companion
  firmware 1.18.0), MQTT API, Home Assistant, configuration, deployment, testing and development
  documentation.
- Companion protocol subset: framing with resynchronisation, command encoders, decoders for
  responses, pushes and messages in the legacy and the version 3 layout; device information of
  older firmware (80 and 81 bytes) and the auto-add answer of firmware 1.12 and 1.13 (without the
  hop limit) are accepted.
- Radio session over USB serial (termios, DTR asserted, exclusive) or TCP: handshake, clock
  synchronisation, contact-adding settings, contacts and channels, message sync, acknowledgement
  events, heartbeat. A malformed or unexpected answer ends the session, so the bridge reconnects
  and resynchronises.
- Bridge service: configured targets, text rules with optional ASCII transliteration, duplicate
  suppression, bounded queue with time to live, airtime budget from the LoRa time-on-air formula,
  acknowledgement tracking with retries and path reset, delivery events with reasons (`expired`
  only for messages that never left), inbound deduplication, retained state document.
- Command policy with allowlisted full public keys and replay protection. The sender must be the
  only contact with its 6-byte prefix in the table read after the message arrived, and no other
  key with that prefix may have been seen on the radio in the last 24 hours (table reads, advert,
  path and overwrite pushes), so a look-alike that came and went makes the sender ambiguous.
  Rejections by the cached table need no read, one read serves a batch, and a read in progress is
  reused. Commands that waited in the radio before the bridge connected are refused. The bridge
  warns at connect about contact types the radio adds on its own and about commands over TCP.
- MQTT adapter on paho-mqtt with last will, Home Assistant device discovery with sensor, event and
  notify entities.
- Command line: `run`, `check-config`, `probe` (also shows how the radio adds contacts, and leaves
  waiting messages in the radio), `send` (with `--wait` following its own request), `listen`,
  `config render`, `config answers`. Texts from the mesh reach the terminal without control or
  format characters.
- systemd unit with notify, watchdog and sandboxing; reproducible zipapp build.
- Proxmox VE installer: default and advanced setup in dialogs or from an answers file (unknown
  installer answers refused, every configuration setting passed on, the radio never guessed),
  settings checked before anything is created, unprivileged Debian 13 container with nesting, radio
  passed through by its stable path, update that switches only to a version accepting the
  configuration (also with an unchanged version string), reconfigure that keeps the current
  settings and the targets the radio no longer offers.
- Continuous integration that runs the same `make` targets as a local check (`make check`,
  `make matrix`), with a pre-push hook, hash-pinned tools with a 14-day cooldown and SHA-pinned
  actions.
