---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Ports and adapters around a pure domain core, one asyncio event loop

## Context and Problem Statement

The bridge combines a byte protocol, timing (acknowledgements, retries, budgets), policies
(authorisation, replay protection, text rules) and two I/O systems (radio, broker). How is the code
structured so that the policies are testable without hardware or a broker, and so that the
concurrency stays simple?

## Decision Drivers

* Test-driven development: most logic must be testable fast and deterministically.
* Simplicity (KISS): no locks in application code, no hidden threads.
* Replaceable edges: serial or TCP to the radio; MQTT today, other buses later.
* Single responsibility per module (SOLID).

## Considered Options

* Ports and adapters (hexagonal) with a synchronous domain core and one asyncio event loop
* A layered design with threads per I/O system
* One module per feature without explicit boundaries

## Decision Outcome

Chosen option: "ports and adapters with one asyncio event loop", because it isolates every policy
in pure functions and small classes with an injected clock, while all I/O runs on one loop.

* **Core** (`meshcore_bus.core`): text rules, airtime estimate, airtime budget, deduplication,
  command policy, data model. No I/O, no asyncio, time passed in.
* **Protocol** (`meshcore_bus.protocol`): frame codec, command encoders and response decoders.
  Pure functions on bytes.
* **Application** (`meshcore_bus.bridge`): the service that connects radio and bus, delivery with
  acknowledgement and retries. Async, depends only on the ports.
* **Ports**: `Radio`, `Bus`, `StateStore`, `Clock` as `typing.Protocol` classes.
* **Adapters**: companion session over a serial or TCP transport, MQTT client, JSON state file,
  systemd notification.

The MQTT library runs its network loop in its own thread; its callbacks only hand messages to the
event loop with `call_soon_threadsafe`. Everything else runs on the event loop.

### Consequences

* Good, because the core and protocol layers are covered by fast unit tests, and the application
  layer is tested against an in-memory radio that emulates the firmware.
* Good, because a new transport or bus is a new adapter, not a change to the core.
* Bad, because there are more, smaller modules than in a single-file script.

### Confirmation

`mypy --strict` and `ruff` run in CI; the core package must not import `asyncio`, `socket`,
`termios` or `paho` (checked by a test).

## Pros and Cons of the Options

### Layered design with threads

* Good, because blocking libraries can be used directly.
* Bad, because shared state needs locks, and timing tests become flaky.

### One module per feature

* Good, because it is quick to start.
* Bad, because policies end up next to I/O and can only be tested with mocks of everything.

## More Information

* Alistair Cockburn, "Hexagonal architecture", <https://alistair.cockburn.us/hexagonal-architecture/>
* Python `asyncio` event loop, `loop.call_soon_threadsafe`:
  <https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe>
