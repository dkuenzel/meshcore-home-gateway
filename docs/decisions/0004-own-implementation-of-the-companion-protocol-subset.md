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
* Note of 2026-11-02 (security and architecture review, ARC-007): the observer
  ([ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md)) grew the subset; the protocol
  layer (`src/meshcore_home_gateway/protocol/`) is 1,175 lines, no longer "a few hundred". It is
  still pure encode and decode, with a golden frame from the firmware source for every message
  (`tests/test_codec.py`) and the real firmware in the tests
  ([ADR 0026](0026-gateway-tested-against-the-real-firmware-built-natively.md)).
* Amended on 2026-11-15 (#174): when a community library in the radio path is reviewed. The
  analysis of configuration from the user interface weighed MeshCore's Python library again, and
  the maintainer agreed to take none into the radio path. At its tag `v2.3.19` (2026-11-11) the
  library's one CI workflow, `.github/workflows/python-import.yml`, installs and imports the
  package on Python 3.10 and 3.11 and runs none of its 200 test functions; PyPI holds no
  provenance for its files (its integrity endpoint answers 404, checked 2026-11-15). Debian
  packages it as `python3-meshcore` and runs its tests at the build and as autopkgtest
  (`debian/tests/control`), in unstable only, not in Debian 13
  (<https://sources.debian.org/src/python-meshcore/>). The decision is reviewed once all three
  hold: `python3-meshcore` is in a Debian stable release with its tests run as autopkgtest, so
  that [ADR 0005](0005-runtime-dependencies-from-the-distribution.md) can take it from the
  distribution; upstream runs its tests in CI and its releases carry provenance; and the
  gateway's scope needs more of the protocol than its subset. This makes the revisit above
  concrete for the library. Until then the firmware's source and the firmware itself stay the
  references for every frame
  ([ADR 0026](0026-gateway-tested-against-the-real-firmware-built-natively.md)).
