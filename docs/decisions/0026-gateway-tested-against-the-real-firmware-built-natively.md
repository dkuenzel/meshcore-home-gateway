---
status: accepted
date: 2026-10-31
decision-makers: maintainers
---

# The gateway is tested against the real firmware, built natively

## Context and Problem Statement

The automated tests talk to an emulated radio (`tests/radio_emulator.py`), a re-implementation of
the companion firmware's answers in Python, and decode golden frames written from the firmware's
source. What emerges from the firmware itself is checked only on the hardware checklist, by hand,
once per release: its queue and timeouts, how it learns and resets paths, how it floods, what its
telemetry answers hold byte by byte. The telemetry slices made that gap large: most steps of the
checklist that run for hours (29–40) depend on firmware behaviour. A spike showed that MeshCore's
companion and repeater firmware build for Linux unchanged on about a thousand lines of stand-ins,
run as processes on a simulated LoRa air, and that the gateway talks to them through its own
serial transport. Should the project test against that firmware, and how?

## Decision Drivers

* Fidelity: the firmware's own code, at the release the gateway supports, not a re-implementation.
* `make check` stays hermetic and fast: no compiler, no network, the same coverage gate.
* No third-party sources in the repository: their licences, their review and their size stay out.
* Reproducible: the same sources on every machine, checked against what was pinned.
* Maintainable: a new firmware release is tried in an hour, not rebuilt from scratch.
* Honest: what the simulation cannot show stays on the hardware checklist.

## Considered Options

* Only the emulated radio and the hardware checklist, as before
* The emulated radio extended by hand from the firmware's source
* The real firmware built natively on stand-ins, on a simulated air
* The firmware of a real board in an instruction set emulator (QEMU, Renode) with a simulated
  SX1262
* Real radios on a self-hosted CI runner
* The firmware's sources copied into the repository

## Decision Outcome

Chosen option: "the real firmware built natively on stand-ins, on a simulated air", because it is
the only one that runs the firmware's own code on every change without hardware: it builds in
seconds, and the suite plays the checklist's scenarios in about 12 minutes.

* **Pinned sources, never committed.** `tests/firmware/sources.toml` names each source by its
  tags and the full commit they point to: MeshCore (`companion-vX.Y.Z` and `repeater-vX.Y.Z` of
  one release) and CayenneLPP in the version MeshCore's `platformio.ini` names.
  `tools/fetch_firmware.py` fetches the tags, refuses one that points to another commit, and
  checks out the pinned paths into the ignored `build/firmware/src`. `MESHCORE_SOURCE` and
  `CAYENNELPP_SOURCE` name a local repository instead of GitHub; the same checks run.
* **Unchanged firmware on small stand-ins.** `tests/firmware/shim` stands in for the Arduino core
  and the libraries the firmware includes (rweather Crypto and base64 on OpenSSL; CayenneLPP is
  the real library with a stand-in of its JSON types). `tests/firmware/board` replaces a board's
  `target.h`: the radio on the simulated air, the USB serial on a pseudo terminal, the flash in a
  directory. The build takes the RP2040 code paths (LittleFS).
* **A simulated air in Python.** `tests/firmware/air.py` (asyncio, in the test's process) delivers
  each transmission after its time on air to the nodes linked to the sender, with the link's SNR
  and RSSI, and loses overlapping receptions. Every node runs the EU/UK Narrow preset with
  CR 4/8 (869.618 MHz, 62.5 kHz, SF 8), the settings of the hardware checklist.
* **The gateway unchanged.** It talks to a companion through its `SerialTransport` on the pseudo
  terminal, as to a radio on USB.
* **Separate from `make check`.** `make firmware-test` fetches, builds and runs `tests/firmware`;
  `make test` does not collect them. CI runs them in a job of their own, which a release needs.

### Consequences

* Good, because firmware behaviour (paths, floods, retries, timeouts, the bytes of an answer) is
  tested on every change, and a scenario of the checklist can become a test.
* Good, because trying a new firmware release is two lines in `sources.toml` and a run of
  `make firmware-test`.
* Bad, because the stand-ins can differ from a board: the RP2040 code paths instead of those of
  ESP32 and nRF52, a 64-bit `millis()` that never wraps, the time on air from RadioLib's formula
  instead of the chip, no capture effect, no noise floor, a flash that never fills.
* Bad, because the nodes run in real time: a scenario of an hour takes an hour until the firmware,
  the air and the gateway share a virtual clock, and timing assertions need tolerances.
* Bad, because a firmware release can break a stand-in, and the CI job needs GitHub to fetch.
* Bad, because a green run proves nothing about radio propagation, GPS, USB, the phone app or
  Home Assistant on a real setup: those steps stay on the hardware checklist.

### Confirmation

`make firmware-test` and the CI job *real firmware on a simulated air*. Its smoke test checks
that the gateway's probe reads the pinned release's version and the preset from a companion,
and that the gateway counts that firmware as tested. `tests/test_fetch_firmware.py` checks the
fetch (a moved tag is refused, a second run needs no network); `tests/test_tooling.py` checks
that `make check` and `pytest` alone leave the firmware tests out.

## Pros and Cons of the Options

### Only the emulated radio and the hardware checklist

* Good, because nothing new to maintain.
* Bad, because firmware behaviour is checked by hand, once per release, and the long steps of the
  checklist cost a day each time.

### The emulated radio extended by hand

* Good, because fast and hermetic, in `make test`.
* Bad, because it tests the gateway against our reading of the firmware: a misreading passes in
  both places.

### The real firmware built natively on stand-ins, on a simulated air

* Good, because the firmware's own code runs, two firmwares and any number of nodes at once.
* Bad, because the stand-ins and the board are ours to maintain, per firmware release.

### An instruction set emulator with a simulated SX1262

* Good, because the board's own code paths run.
* Bad, because a full board model (radio chip, USB, flash) is far more work than the stand-ins,
  and slower.

### Real radios on a self-hosted runner

* Good, because real radio links.
* Bad, because hardware in a CI loop is costly, fragile and a security risk for a public
  repository, and the steps that run for hours stay slow.

### The firmware's sources in the repository

* Good, because no fetch.
* Bad, because third-party code of another licence and history in the repository, reviewed by
  nobody, and every bump a large diff.

## More Information

MeshCore: <https://github.com/meshcore-dev/MeshCore> (MIT); CayenneLPP:
<https://github.com/ElectronicCats/CayenneLPP> (MIT). How to run the tests and to move to another
firmware release: [testing.md](../testing.md#tests-against-the-real-firmware). Revisit when
MeshCore offers a native build target of its own, or when the stand-ins of a release cost more
than a day.
