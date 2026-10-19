---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Runtime dependencies: the standard library and the distribution's paho-mqtt

## Context and Problem Statement

Every runtime dependency runs with access to the radio and the broker. Packages from PyPI change
without review on our side unless pinned, and pinned packages still need updates. Which libraries
may the running service use, and where do they come from?

## Decision Drivers

* Small attack surface in the radio path ([ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md)).
* Security updates without our involvement.
* Installation without a network connection to PyPI on the target.
* Linux is the only target platform.

## Considered Options

* Standard library plus `paho-mqtt` from the Linux distribution
* Packages from PyPI in a virtual environment (for example `pyserial-asyncio`, `paho-mqtt`,
  `pydantic`), pinned with hashes
* Vendored copies of the needed libraries

## Decision Outcome

Chosen option: "standard library plus the distribution's `paho-mqtt`", because the protocol
subset, the serial port (`termios`), TCP (`asyncio`), configuration (`tomllib`) and JSON need
nothing else, and the one non-trivial component, the MQTT client, is a mature Eclipse project that
Debian packages and maintains (`python3-paho-mqtt`).

### Consequences

* Good, because the deployment target installs exactly two Debian packages, `python3` and
  `python3-paho-mqtt`, which receive Debian security updates.
* Good, because the application itself ships as one reproducible zipapp built with the standard
  library (`tools/build_pyz.py`).
* Bad, because the minimum platform is Debian 13 (trixie: Python 3.13, `python3-paho-mqtt` 2.1.0).
  Debian 12 ships paho-mqtt 1.6.1, which lacks the version-2 callback API; it would need paho from
  PyPI in a virtual environment.
* Bad, because the serial transport uses `termios` and only runs on POSIX systems (Linux tested).
* Neutral, because development tools (pytest, ruff, mypy, coverage, build, flit-core, shellcheck,
  zizmor) come from PyPI, pinned by hash in `requirements/dev.txt` with a 14-day cooldown; they
  never run in production.

### Confirmation

`pyproject.toml` declares only `paho-mqtt>=2.0,<3`; the development lock pins paho-mqtt 2.1.0, the
Debian 13 version, so CI tests against what production runs. A test asserts that the core package
imports no I/O modules (ADR 0007).

## Pros and Cons of the Options

### PyPI packages in a virtual environment

* Good, because any version can be used on any distribution.
* Bad, because the target needs PyPI access at install and update time, and every update is ours
  to review.

### Vendored copies

* Good, because no network is needed.
* Bad, because we would have to track upstream security fixes ourselves.

## More Information

* Debian package versions (Debian QA madison, 2026-10-18): `python3` 3.11.2 (bookworm) and 3.13.5
  (trixie); `python3-paho-mqtt` 1.6.1 (bookworm) and 2.1.0 (trixie).
* paho-mqtt 2.0 introduced `CallbackAPIVersion`; 2.1.0 (2024-04-29) is the latest release and
  ships type information (`py.typed`).
* Revisit if the bridge needs a library that the distribution does not package.
