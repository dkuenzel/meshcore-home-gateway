---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# A standalone bridge service instead of a Home Assistant integration

## Context and Problem Statement

A MeshCore companion radio should send and receive messages on behalf of a home automation
system: alerts out, commands and status in. The radio talks a binary protocol over USB serial or
TCP. Where does the code that speaks this protocol run, and how does the automation reach it?

## Decision Drivers

* **Supply chain:** code that holds the radio and talks to the automation system should be small,
  reviewable and pinned.
* **Isolation:** a fault, hang or update in the radio code must not take the automation system
  down, and the reverse.
* **Reuse:** scripts and other systems should use the same path, not only Home Assistant.
* **Deployment:** Home Assistant often runs as an appliance or a virtual machine where extra
  Python packages and USB devices are awkward.
* **Safety functions** for a shared radio channel: airtime budget, allowlisted commands, replay
  protection.

## Considered Options

* Home Assistant custom integration `meshcore-ha` (installed through HACS)
* An existing bridge to MQTT, `ipnet-mesh/meshcore-mqtt`
* A fork of one of these with pinned dependencies
* A new standalone bridge service that publishes to MQTT (this project)

## Decision Outcome

Chosen option: "a new standalone bridge service", because it is the only option that keeps the
code in the radio path small and pinned, runs isolated from the automation system, and makes the
safety functions part of the design rather than an addition.

### Consequences

* Good, because the bridge can run in its own container with the USB device, independent of the
  automation system's update cycle.
* Good, because any MQTT client can use it; Home Assistant uses its built-in MQTT integration
  ([ADR 0003](0003-mqtt-as-the-message-bus.md), [ADR 0010](0010-home-assistant-through-mqtt-device-discovery.md)).
* Bad, because we implement and maintain the protocol subset ourselves
  ([ADR 0004](0004-own-implementation-of-the-companion-protocol-subset.md)) and follow firmware
  changes.
* Bad, because features of the existing integration (maps, repeater administration, telemetry
  dashboards) are out of scope.

### Confirmation

Runtime dependencies stay limited as recorded in
[ADR 0005](0005-runtime-dependencies-from-the-distribution.md); the CI checks the lock file and the
dependency declaration. The design document lists the scope.

## Pros and Cons of the Options

### Home Assistant custom integration `meshcore-ha`

Evaluated at release 3.0.3 (tag dated 2026-10-16).

* Good, because it is feature-rich and maintained by the MeshCore community.
* Bad, because its manifest requires `meshcore>=2.3.11`, `meshcore-cli>=0.0.0` (no effective
  bound), `pycryptodome`, `cachetools`, `paho-mqtt` and `pynacl`; together with `meshcore` 2.3.17
  and `meshcore-cli` 1.6.6 this is about 29,000 lines of Python with access to the radio and to the
  automation system. PyPI holds no provenance attestations for these two library releases.
* Bad, because Home Assistant installs missing integration requirements from PyPI at runtime
  (`uv pip install`, `homeassistant/util/package.py`); a new container image starts without them,
  so the code that runs can change after every update without a change on our side.
* Bad, because the radio code runs inside the automation process; the USB device must reach the
  Home Assistant host or virtual machine.

### Existing bridge `ipnet-mesh/meshcore-mqtt`

Evaluated at commit `bbc8584` (2025-11-16).

* Good, because it already bridges MeshCore and MQTT and runs as a separate service.
* Bad, because it depends on `meshcore` (the same library as above), `pydantic`, `click` and
  `PyYAML`.
* Bad, because its rate limit counts commands; it does not budget airtime, and we found no
  allowlist with replay protection for commands that reach the automation system.

### Fork with pinned dependencies

* Good, because pinning removes the moving target.
* Bad, because the volume of third-party code stays the same and every upstream release must be
  merged and reviewed.

### New standalone bridge

* Good, because the protocol subset needed for messaging is small (see
  [`docs/protocol.md`](../protocol.md)) and can be reviewed completely.
* Good, because safety functions are designed in.
* Bad, because it is new code without a track record; the test strategy and hardware tests in
  [`docs/testing.md`](../testing.md) have to make up for that.

## More Information

* `meshcore-ha`: <https://github.com/meshcore-dev/meshcore-ha>, `custom_components/meshcore/manifest.json` at tag `v3.0.3`.
* `meshcore-mqtt`: <https://github.com/ipnet-mesh/meshcore-mqtt>, `pyproject.toml` at commit `bbc8584`.
* Line counts were measured with `wc -l` over the Python sources of the released packages
  (`meshcore` 2.3.17, `meshcore-cli` 1.6.6, `meshcore-ha` 3.0.3) on 2026-10-18.
* Provenance: `https://pypi.org/integrity/<project>/<version>/<file>/provenance` answered 404 for
  every file of `meshcore` 2.3.17 and `meshcore-cli` 1.6.6 on 2026-10-18.
* Revisit when the MeshCore project publishes a signed, minimal client library, or when the scope
  grows to repeater administration.
