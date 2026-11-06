---
status: accepted
date: 2026-11-05
decision-makers: maintainers
---

# A release checked by CI, the installer's self-test and the maintainer's use, not by a manual checklist

## Context and Problem Statement

Before every release, the hardware checklist of [testing.md](../testing.md#hardware-checklist)
was to be run with the release candidate ([ADR 0022](0022-releases-tagged-by-ci-on-main.md),
[design §10](../design.md#10-testing-strategy)): 58 steps on two radios, some of them hours of
real time, with a TCP radio, a sensor node, an authenticator app and openHAB. The project has one
maintainer, one installation and an agent that writes most of the code. After 1.0.0 the checklist
was not run for any of the pre-releases, and on 2026-11-05 the maintainer stated that it is not
something he can run: too many steps, and components he does not use. He uses the gateway with
Home Assistant, tests the features he uses and reports their bugs, and does not invest in other
front ends; the bus stays open for them
([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)), and whoever uses them can maintain
them. How is a release checked without the checklist, and without losing what it checked?

## Decision Drivers

* No step for the maintainer beyond installing the release candidate and using it.
* Whatever can run without a person runs on every pull request, against the real firmware and a
  real service manager where an emulation would only prove our own reading.
* What only a real installation can show is checked by the installation itself, for every user,
  silently when it passes.
* Home Assistant is the front end a release is checked with.
* Honest about what is no longer checked.

## Considered Options

* CI for everything that can be played, the installer's self-test on every installation, and the
  maintainer's use of Home Assistant with the release candidate
* The checklist cut to its core steps (1–4, 10–12, 15, 16, 18), run by hand before a release
* A hardware rig: two radios on a self-hosted runner that plays the steps on real air
* CI alone, no release candidate

## Decision Outcome

Chosen option: "CI, the installer's self-test and the maintainer's use", because it asks nothing
of the maintainer that he does not do anyway, moves most of the checklist into tests that run on
every change, and checks the one real installation, and every user's, where only a real
installation can.

* **CI** (every pull request and every push to `main`): the unit, property and Home Assistant
  contract tests, as before; the firmware tests on the simulated air
  ([ADR 0026](0026-gateway-tested-against-the-real-firmware-built-natively.md)) also play the
  checklist's messages, commands, codes, replies, unreachable contacts and the radio profile
  (steps 3–6, 10–14, 55 and 56) besides the telemetry and advert scenarios; the system test
  restarts the broker (step 16); and the job *unit check* installs the zipapp and the unit as they
  are on its runner, a machine thrown away after the job, and runs the service under systemd: its
  readiness, its sandbox, `doctor`, a broker restart and the watchdog (steps 2, 16 and 17).
* **The installer's self-test**: after an installation, a reconfiguration and an update, the
  installer runs `meshcore-home-gateway doctor` in the container until it passes, for about a
  minute while the radio connects: the configuration, the broker login, the radio connected as
  the gateway reports it, its clock and firmware, the gateway's health and the container. The
  final message says `passed`, or shows the problems; nothing is undone. Every installation
  checks itself this way, the maintainer's with each release candidate.
* **The release candidate**: the maintainer updates his installation with the candidate's
  installer (the self-test runs) and uses Home Assistant as he does: the gateway's device online,
  a companion's messages shown, its *Telemetry* switch, the companions on the map. He reports
  what he finds as issues. The pull request of the release records the Home Assistant version; the
  release ships the candidate's program, as before.
* **Not checked before a release**: real radio propagation and timing, real GPS fixes, a USB
  cable pulled out of a real host, a host switched off, the hours of the telemetry intervals in
  real time, radios over TCP, MeshCore's sensor firmware, the phone app scanning a QR code, and
  the Homie convention in openHAB. They rest on the unit tests and on reports from use. Homie is
  maintained by whoever uses it: its documents keep their unit tests; the maintainer does not check
  them in openHAB.
* **The checklist** stays in testing.md as the description of what the gateway does on real
  radios, step by step; a table there says where each step is checked now. A contributor who
  changes a feature and has the hardware runs its steps and says so in the pull request.

### Consequences

* Good, because no release waits for a manual procedure that nobody runs.
* Good, because steps that were only ever checked by hand (messages, commands, codes, replies, the
  radio profile, the broker restart, the watchdog) now run on every pull request.
* Good, because every user's installation checks itself, not only the maintainer's.
* Bad, because a regression that shows only on real air, a real GPS or a real USB host is found
  in use, after a release; so is one in a Home Assistant feature the maintainer does not use.
* Bad, because a Homie user gets no check of openHAB's side before a release.
* Neutral, because pre-releases go out as before, without a release check.

### Trust boundaries

B6 the container: the self-test runs as root in it, as `check-config --broker` already does; it
reads the configuration and the broker password and logs in to the broker, and `doctor` also tries
a login without a user name to say whether the broker takes anonymous clients. Nothing leaves
the container that the service does not send. B7 the project: the release gate changes from a
procedure to tests and the maintainer's use; a release candidate still waits a day after its merge
and needs CI to pass on it (SUP-001). The threat model is unchanged.

### Confirmation

`tests/test_installer.py` (the self-test after an installation and an update, its repetitions, its
report, an older version), the firmware tests `test_messages.py`, `test_commands.py` and
`test_radio_profile.py`, `tests/test_system.py` (the broker restart) and `tests/test_unit_run.py`
in the CI job *unit check*. `tools/release_plan.py` still refuses a release without a release
candidate of the same program. The table in testing.md names the test of each checklist step.

## Pros and Cons of the Options

### CI, the installer's self-test and the maintainer's use

* Good, because it costs the maintainer nothing he does not do already.
* Good, because it grows with the tests: a step that becomes a test leaves the list of what is
  not checked.
* Bad, because real radio behaviour is checked only in use.

### The checklist cut to its core steps

* Good, because a person still looks at real radios before a release.
* Bad, because it is still a manual procedure for one maintainer; the shorter list was not run
  either.

### A hardware rig on a self-hosted runner

* Good, because it would play steps on real air on every change.
* Bad, because it needs hardware, a host and a runner that someone keeps up, and a self-hosted
  runner is a new trust boundary for a public repository; not with one maintainer.

### CI alone, no release candidate

* Good, because it is the fastest.
* Bad, because nothing would check a real installation before the release that every
  installation then takes.

## More Information

This record supersedes the part of ADR 0022 that a release follows the hardware checklist, and the
part of ADR 0026 that what the simulation cannot show stays on the checklist. Revisit when a second
maintainer with hardware joins, when a hardware rig becomes possible, or when a regression reaches
a release that a checklist step would have caught.
