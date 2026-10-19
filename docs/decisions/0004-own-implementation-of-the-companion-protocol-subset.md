---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Own implementation of the companion protocol subset

## Context and Problem Statement

The bridge must speak the MeshCore companion protocol. The MeshCore project publishes a Python
client library, `meshcore` (meshcore_py). Should the bridge use it or implement the messages it
needs itself?

## Decision Drivers

* Supply chain: code in the radio path should be reviewable and pinned
  ([ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md)).
* The bridge needs a small subset: handshake, time, contacts, channels, sending, message sync,
  acknowledgements, battery and receive log.
* Testability with golden vectors and an emulated radio.
* Following firmware changes.

## Considered Options

* Use `meshcore` (meshcore_py) from PyPI
* Vendor a pinned copy of `meshcore`
* Implement the subset from the firmware source

## Decision Outcome

Chosen option: "implement the subset from the firmware source", because the subset is a few
hundred lines of pure encode/decode functions that can be reviewed completely and tested against
byte layouts cited from a named firmware release, without adding a dependency tree.

### Consequences

* Good, because the protocol layer has no dependencies and no I/O, and every field is documented
  with its source line in [`docs/protocol.md`](../protocol.md).
* Good, because the bridge controls timeouts, retries and error handling.
* Bad, because firmware changes must be tracked by us. The protocol carries a version
  (`DEVICE_QUERY`); the bridge requests version 3 frames and logs the firmware version at connect.
* Bad, because features outside the subset are not available.

### Confirmation

[`docs/protocol.md`](../protocol.md) names the firmware release the layouts were taken from. Unit
tests encode and decode golden frames derived from that source. The hardware checklist in
[`docs/testing.md`](../testing.md) is run against each new firmware release before the supported
version is raised.

## Pros and Cons of the Options

### Use `meshcore` from PyPI

* Good, because it covers the whole protocol and is maintained by the MeshCore community.
* Bad, because release 2.3.17 is about 5,300 lines and has no provenance attestations on PyPI;
  versions change underneath unless pinned and reviewed.

### Vendor a pinned copy

* Good, because the code no longer changes underneath.
* Bad, because the review effort stays and updates become manual merges.

### Own implementation

* Good, because small and fully tested.
* Bad, because it duplicates community work for this subset.

## More Information

* Firmware source: <https://github.com/meshcore-dev/MeshCore>, `examples/companion_radio/`,
  licensed MIT.
* Revisit when the scope grows beyond messaging, or when an official, minimal and signed client
  library exists.
