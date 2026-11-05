# The firmware on a simulated air

MeshCore's own companion and repeater firmware, at the release `sources.toml` pins, built for
Linux unchanged and run as processes on a simulated LoRa air
([ADR 0026](../../docs/decisions/0026-gateway-tested-against-the-real-firmware-built-natively.md),
[testing.md](../../docs/testing.md#tests-against-the-real-firmware)). `make firmware-test` from the
top of the repository fetches the sources, builds them with the `Makefile` here and runs the
tests.

The firmware's own files are compiled as they are. What it expects from an Arduino board and
from its libraries is replaced by the stand-ins below; each is the smallest part of the original
API the pinned firmware uses. A firmware release that needs more fails to build or to link, and
the stand-in grows ([testing.md](../../docs/testing.md#testing-another-firmware-release)).

## `shim/`: libraries and the Arduino core

| File | Replaces | Why |
| --- | --- | --- |
| `Arduino.h` | the Arduino core's types, time, random numbers and pins | the firmware is written against it; time, randomness and the serial ports live in `board/runtime.cpp` |
| `Stream.h` | the core's `Print` and `Stream` | the serial interfaces and the CLI print through them; derived from MeshCore's `test/mocks/Stream.h` |
| `FS.h`, `LittleFS.h` | the RP2040 core's LittleFS | the stores keep contacts, identity and preferences in a directory of the host (`SIM_FS_DIR`) |
| `AES.h` | rweather/Crypto's AES-128 | on OpenSSL's AES |
| `SHA256.h` | rweather/Crypto's SHA-256 and HMAC | on OpenSSL's digests (`board/crypto.cpp`) |
| `Ed25519.h` | rweather/Crypto's signature check | on OpenSSL; signing stays with the firmware's own `lib/ed25519` |
| `RTClib.h` | Adafruit RTClib | the firmware includes it; the repeater's command line formats times with `DateTime` |
| `Wire.h` | the I2C bus | no device answers, so no environment sensor is found |
| `ArduinoJson.h` | ArduinoJson and Arduino's `String` | enough for CayenneLPP 1.6.1 to compile; its JSON decoder is never called |
| `base64.hpp` | densaugeo/base64 | `encode_base64` and `decode_base64` with explicit lengths |

## `board/`: the simulated board

| File | Replaces | Why |
| --- | --- | --- |
| `target.h` | `variants/<board>/target.h` and `target.cpp` | the board objects the firmware's `main` uses |
| `SimRadio.h`, `SimRadio.cpp` | `RadioLibWrapper` and `CustomSX1262Wrapper` (`src/helpers/radiolib/`) | transmissions go to the simulated air (`air.py`) as datagrams, which delivers them after their time on air, drops collisions and half-duplex overlaps, and reports SNR and RSSI per link |
| `runtime.cpp` | the Arduino runtime | time, random numbers, the USB serial on a pseudo terminal, LittleFS in a host directory, control lines on standard input, and a `main()` that calls the firmware's `setup()` and `loop()` |
| `crypto.cpp` | rweather/Crypto's hash classes | SHA-256 and HMAC-SHA256 on OpenSSL's EVP digests |
| `companion.cpp` | the companion's user interface | its Advert button: the control line `advert` calls `MyMesh::advert()` |

## The Python side

`air.py` is the air, `node.py` runs a node as a process (its flash, its serial port, its log),
`gateway.py` runs the gateway's service on a node's serial port and the test's broker,
`household.py` sets up the hardware checklist's households (the gateway's radio, a companion and
repeaters), `app.py` plays a companion's phone app on the gateway's own codec, and `conftest.py`
has the fixtures (the firmware `make firmware` built, the air and its nodes). `play.py` keeps a
household, a broker and the gateway running for exploration (`make firmware-play`). The tests are the
`test_*.py` files; [testing.md](../../docs/testing.md#where-each-step-is-checked) lists
which step of the hardware checklist each one covers.
