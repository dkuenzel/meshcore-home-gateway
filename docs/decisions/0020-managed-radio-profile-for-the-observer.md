---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# A managed radio profile for the observer, reconciled at every connect

## Context and Problem Statement

The gateway's radio is dedicated to it. Its settings decide whether the observer
([ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md)) leaks anything (telemetry,
its position), whether strangers end up in its contact table (look-alikes block command senders,
[ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md)), and how much it may transmit.
Today the gateway only warns about automatic adding and leaves every setting to the phone app,
which cannot reach the radio while the gateway uses it. A replaced or reset radio comes with
factory settings: automatic adding on and a 50 % duty cycle. Who sets these, and how, without
wearing out the radio's flash or fighting deliberate changes?

## Decision Drivers

* The observer reveals nothing: no telemetry to anyone, no position in adverts.
* Only the gateway adds contacts ([ADR 0017](0017-contacts-on-the-radio-each-with-its-own-device.md)).
* The radio's own transmit limit should match the region's duty cycle.
* Every setter rewrites the radio's preferences file: write only differences.
* A radio swap or factory reset heals itself; deliberate changes of identity (name) survive.
* Firmware differs between versions: unknown firmware must not be written blindly.
* Network-wide parameters (frequency, bandwidth, spreading factor, coding rate, path hash mode)
  belong to the local mesh, not to the gateway.

## Considered Options

* Set once by the installer
* Enforce everything at every connect
* Three-way reconcile at every connect: desired (configuration), last applied (state), live (radio)
* Observe and warn only

## Decision Outcome

Chosen option: "three-way reconcile at every connect", because it heals drift of the policy,
keeps deliberate identity changes, and writes nothing in the steady state.

**Profile** (`[radio.profile]` in `config.toml`; defaults are the observer profile):

| Setting | Observer default | Kind | Command |
| --- | --- | --- | --- |
| Manual adding | on | enforced | `SET_OTHER_PARAMS` (38), shortest frame |
| Auto-add types, overwrite oldest | none, off | enforced | `SET_AUTOADD_CONFIG` (58), flags only |
| Telemetry: base, location, environment | deny, deny, deny | enforced | 38, after reading `SELF_INFO` |
| Position in adverts | none | enforced | 38 |
| Radio duty cycle | 10 % on 869.40–869.65 MHz (airtime factor 9), else as found unless set | enforced when set or derived | `SET_TUNING_PARAMS` (21), receive delay written back as read |
| Name | as found, or as configured | seed: written when the configured value changes | `SET_ADVERT_NAME` (8) |
| Protected contacts present, as favourites | on | enforced | `ADD_UPDATE_CONTACT` (9) |

* **Reconcile** per key: desired ≠ last applied → the operator changed the configuration: write
  it. Desired = last applied but live differs → someone changed the radio: an *enforced* key is
  written back with a warning, a *seed* key is accepted and reported as drift. After writing, the
  session reads the settings again and compares; values that go through the firmware's float32
  arithmetic are compared with a tolerance of one thousandth.
* **When**: at connect, after the auto-add settings are read and before the contact table is read
  for command decisions, so the policy holds before any message is judged.
* **Firmware gate**: `manage = "auto"` (default) writes only on tested firmware version codes
  (initially 14, companion 1.18); other firmware is observed and reported. `observe` never writes,
  `always` writes on any firmware at the operator's risk. A command answered with `ERR 1` marks
  its setting `unsupported`.
* **Convergence guard**: a setting written at three connects in a row is no longer written and is
  reported as `not_converging` (a firmware that clamps or ignores a value must not wear the flash).
* **Visibility**: a retained `settings` document (live, desired, drift, unsupported, mode), one
  log line per change, a settings event and a diagnostic entity in Home Assistant.
* **Display only, never written**: frequency, bandwidth, spreading factor, coding rate, path hash
  mode, transmit power, client repeat, the stored own position (reported, cleared only on request).
* **Deny list**: the codec has no encoder for setting radio parameters (11), exporting or importing
  the private key (23, 24), raw data and raw packets (25, 65), signing (33–35), setting the BLE PIN
  (37), factory reset (51) and the radio's command line (66); a test fails if one appears.
* **Installer**: reads the radio (`probe`), asks for the name (default: the current one), shows the
  plan, and writes only `config.toml`. The service is the radio's only writer.

### Consequences

* Good, because a fresh, replaced or reset radio becomes an observer at the first connect.
* Good, because nothing is written while the radio matches the profile.
* Good, because the radio's own limiter then counts everything it sends (acknowledgements,
  adverts, repeats), which the gateway's budget cannot see.
* Bad, because the firmware clamps the airtime factor to 0–9 at boot (`MyMesh.cpp:988`), so the
  radio cannot be limited below 10 %; stricter limits (1 % and 0.1 % sub-bands) remain the
  gateway's budget ([ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md)).
* Bad, because the gateway now writes to the radio; the firmware gate, the diff and the guard bound
  that, and the emulator tests prove the second connect writes nothing.
* Bad, because household companions cannot request the observer's battery any more (telemetry
  denied); the gateway publishes its own battery anyway.

### Confirmation

Emulator tests: the first connect writes exactly the differences, the second connect writes
nothing; a clamped value stops after three connects; `ERR 1` yields `unsupported`; unknown
firmware only observes; an operator change of the name is written once, a radio change of the
name is kept and reported. The hardware checklist covers the frames on the reference radio.

## Pros and Cons of the Options

### Set once by the installer

* Good, because minimal writes.
* Bad, because drift (factory reset, a new radio, the phone app) goes unnoticed, and the installer
  would become a second writer of the radio.

### Enforce everything at every connect

* Good, because simple.
* Bad, because without a diff it rewrites the preferences at every reconnect, and it reverts
  deliberate changes of the name.

### Observe and warn only

* Good, because it never writes.
* Bad, because every user would need the phone app and a cable to fix what the gateway can see is
  wrong.

## More Information

* Command layouts and firmware citations: [protocol.md](../protocol.md); duty cycle and the airtime
  factor: protocol.md, section 7.
* meshcore-ha sets manual adding at every connect, one preferences write per connect
  (`custom_components/meshcore/coordinator.py:2034-2049` at v3.0.3).
* Revisit when further firmware versions are tested (extend the gate) or when the firmware offers a
  per-key read-back for every setting.
