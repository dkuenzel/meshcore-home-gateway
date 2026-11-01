# 11 Research notes

Facts established during the review that the project may want to keep, each with how it was
verified and where it matters. Firmware citations are at MeshCore commit
`81dbae592ffad40b4212bc0b37e82b38da08f4bc` (tags `companion-v1.18.0`, `repeater-v1.18.0`),
fetched with `tools/fetch_firmware.py` into `build/firmware/src/meshcore`; `platformio.ini` and
`docs/` were fetched separately from the same tag (a sparse checkout, since the project's fetch
excludes them). Items are `RES-nnn`.

### RES-001 The acknowledgement is a clear-text hash of (timestamp, attempt, text, sender key)

**Fact.** A plain direct message is acknowledged with a packet of type `PAYLOAD_TYPE_ACK` whose
payload is `SHA256(timestamp(4) ‖ flags(1) ‖ text ‖ sender's public key)[:4]` followed, since
1.18, by the attempt byte and one random byte; the sender matches the first four bytes against
its table of up to eight expected codes. The packet is neither encrypted nor signed; the
`markSeen` table drops an identical packet a second time only.

**How it was verified.** `src/helpers/BaseChatMesh.cpp:240-257` (hash and `sendAckTo`),
`src/Mesh.cpp:560-571` (`createAck`: `memcpy(packet->payload, ack, len)`), `src/Mesh.cpp:117-128`
(receive: `memcpy(&ack_crc, &pkt->payload[0], 4)`, `wasSeen`, `onAckRecv`),
`examples/companion_radio/MyMesh.cpp:424-434` (`processAck`: `memcmp(data, &expected_ack_table[i].ack, 4)`).
The gateway's `codec.expected_ack` reproduces the hash and `tests/test_codec.py` pins it. Multi-acks
(`BaseChatMesh.cpp:43-58`): with `multi_acks > 0` a `createMultiAck` packet precedes the plain
one by 300 ms; both carry the same code.

**Where it matters.** SEC-001, IMP-001.

### RES-002 A contact's advert is dropped unless its timestamp is newer than the stored one

**Fact.** `onAdvertRecv` for a known contact: `if (timestamp <= from->last_advert_timestamp)
return;` ("check for replay attacks"). The timestamp is the advertising radio's clock at the
moment it built the advert (`Mesh.cpp:404-420`, `emitted_timestamp = _rtc->getCurrentTime()`).

**How it was verified.** `src/helpers/BaseChatMesh.cpp:125-140`, `src/Mesh.cpp:404-420`.

**Where it matters.** SEC-002 (a radio whose clock is in the future poisons its own adverts for
every contact that holds it), ADR 0025 (the record's `last_advert`).

### RES-003 The cost of a 6-byte (48-bit) key prefix collision

**Fact.** The radio names a contact by the first 6 bytes of its 32-byte Ed25519 public key in
message frames and in `SEND_TXT_MSG`. To produce a key whose first 48 bits equal a chosen value,
an attacker generates keys until one matches: each trial succeeds with probability 2⁻⁴⁸, so the
expected number of trials is 2⁴⁸ ≈ 2.8 × 10¹⁴ (there is no birthday shortcut: the target is
fixed). Ed25519 key generation is one scalar multiplication: about 20–50 µs per key on one CPU
core with libsodium (roughly 20,000–50,000 keys per second per core), and 1–10 million per second
on a GPU with a tuned implementation. Expected cost: about 180 years of one core; about 3 years
of a 64-core machine; about 1–10 months of a single GPU; days for a cluster of a hundred GPUs.
Cost in cloud terms at 2026 prices: on the order of a few thousand to a few tens of thousands of
euros for a targeted attack. The design's "considerable but feasible computing effort (about
2^48 key generations)" is right; the figures above give it a scale.

**How it was verified.** Arithmetic on the prefix length; throughput figures from libsodium's
benchmarks and published GPU Ed25519 implementations (not measured by the reviewer).

**Where it matters.** ADR 0009 §7.4, SEC-007 (a name collision costs nothing; a prefix collision
costs this), IMP-002.

### RES-004 Time on air at EU/UK Narrow of the packets that matter

**Fact.** With the preset the hardware checklist and the firmware tests use (869.618 MHz,
62.5 kHz, SF 8, CR 4/8, 32-symbol preamble, CRC, explicit header), computed with the project's
own `core.airtime.time_on_air_s`:

| Packet | Bytes on air | Time on air |
| --- | --- | --- |
| Advert with a 12-byte name, no position | 115 | 1.16 s |
| Advert with name and position | 123 | 1.23 s |
| Acknowledgement (header, path length, 6 bytes) | 8 | 0.28 s |
| Direct text of 20 bytes | 38 | 0.51 s |
| Direct text of 160 bytes | 182 | 1.69 s |

So a single board can send an advert every 1.3 s at most on this preset, and the radio spends
0.28 s per acknowledgement it owes.

**How it was verified.** `python3 -c` against `src/meshcore_home_gateway/core/airtime.py`
(`meshcore_lora(62500.0, 8, 8)`), whose formula the project validated against RadioLib and
published calculators (`tests/test_airtime.py`).

**Where it matters.** SEC-010, SEC-011, SEC-013.

### RES-005 Experiment: an advert flood with invented keys against the observer and the history

**Fact.** Feeding `NEW_ADVERT` records with distinct random keys to an `Observer` with a real
`History` (temporary SQLite file), as the bridge does for pushes:

| Adverts | `node` rows | Database size | Heard nodes in memory | Publishes | Bytes published |
| --- | --- | --- | --- | --- | --- |
| 5,000 | 5,000 | 5.1 MB | 50 | 8,012 | 75 MB |
| 50,000 | 50,000 | 14.4 MB | 50 | 80,019 | 755 MB |

About 290 bytes of database per node and 15 KB on the bus per advert (the `nodes` document at
its bound plus the index). Runtime 43 s for 50,000 on the sandbox's CPU (0.9 ms per advert, the
SQLite upsert included).

**How it was verified.** The script (`experiments/advert_flood.py`, kept here too so the team
can rerun it; it uses only the project's modules):

```python
import os, sqlite3, sys, tempfile, time
sys.path.insert(0, "src")
from pathlib import Path
from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import AdvertHeard

class Bus:
    def __init__(self): self.n = 0; self.bytes = 0
    def publish(self, topic, payload, retain=False, qos=1): self.n += 1; self.bytes += len(payload)

tmp = Path(tempfile.mkdtemp())
history = History.open(tmp / "history.sqlite3", Retention())
bus = Bus()
ob = Observer(bus, Topics("meshcore", "radio1"), history=history, wall_clock=time.time)
ob.radio_connected(350, None)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
for i in range(N):
    key = i.to_bytes(4, "big") + os.urandom(28)
    name = f"Node{i}".encode().ljust(32, b"\0")
    raw = (key + bytes([1, 0, 0xFF]) + bytes(64) + name
           + (1_800_000_000 + i).to_bytes(4, "little") + bytes(8) + bytes(4))
    rec = codec._contact(bytes([0x8A]) + raw)
    ob.radio_event(AdvertHeard(codec.Advert(key, True, rec), time.time() + i * 3, i + 1, 1))
db = sqlite3.connect(tmp / "history.sqlite3")
rows = db.execute("select count(*) from node").fetchone()[0]
size = sum(os.path.getsize(p) for p in tmp.glob("history.sqlite3*"))
print(N, rows, size, len(ob.mesh.nodes), bus.n, bus.bytes)
```

**Where it matters.** SEC-010, SEC-011, IMP-019.

### RES-006 Data about bystanders and the household exemption

**Fact.** The GDPR does not apply to processing "by a natural person in the course of a purely
personal or household activity" (Art. 2(2)(c)); Recital 18 adds that this covers "social
networking and online activity undertaken within the context of such activities", and the
Court of Justice has held (Lindqvist, C-101/01; Ryneš, C-212/13: a home camera filming public
space) that publication to an indefinite audience or systematic recording of public space falls
outside the exemption. A gateway that records names, keys and signal of every radio that passes
the house, for 30 days, in a database, is closer to Ryneš than to a phone's contact list; sharing
the data (a dashboard, a posted history) takes it out of the exemption in any reading. Public keys
and self-chosen names are personal data when they can be linked to a person, which a mesh
community's public map does.

**How it was verified.** The regulation and the two judgments (public texts); not legal advice.

**Where it matters.** SEC-015, SEC-016, DOC-010.

### RES-007 The radio's clock: forward-only over the protocol, bootstrapped from contacts at boot, persistent on RTC boards

**Fact.** `CMD_SET_DEVICE_TIME` writes the clock only if the new time is not earlier than the
current (`ERR 6` otherwise). At boot the companion calls `bootstrapRTCfromContacts()`, which sets
the clock to the newest `lastmod` of any contact plus one second; `lastmod` is written with the
radio's clock whenever a contact's message, advert or path arrives and is saved in the contacts
file. Boards with a DS3231, RV3028, PCF8563 or RX8130 keep the time in the chip. Repeaters' CLI
has a `clock` command; the companion has no backward path but a factory reset.

**How it was verified.** `examples/companion_radio/MyMesh.cpp` (`CMD_SET_DEVICE_TIME` handler;
`bootstrapRTCfromContacts()` at line 1001), `src/helpers/BaseChatMesh.cpp:60-70,119,198,240,294,357`,
`src/helpers/AutoDiscoverRTCClock.cpp:85-101`, `src/helpers/ESP32Board.h:225-245` (the ESP32's
RTC backup registers across a soft reset), `src/helpers/CommonCLI.cpp:200-213`.

**Where it matters.** SEC-002.

### RES-008 Private key export and import are enabled in the release firmware's common build flags

**Fact.** `platformio.ini` at `companion-v1.18.0`, `[env]` `build_flags`, lines 33–34:
`-D ENABLE_PRIVATE_KEY_IMPORT=1   ; NOTE: comment these out for more secure firmware` and
`-D ENABLE_PRIVATE_KEY_EXPORT=1`. `CMD_EXPORT_PRIVATE_KEY` (23) answers with the 64-byte key
under `#if ENABLE_PRIVATE_KEY_EXPORT`, else with the `DISABLED` frame. No variant file in the
tree removes the flag (two set it again). The TCP interface (`SerialWifiInterface`) has no
authentication and replaces an existing connection with a new one.

**How it was verified.** Sparse checkout of the tag; `grep -n ENABLE_PRIVATE_KEY platformio.ini
variants/*/platformio.ini`; `examples/companion_radio/MyMesh.cpp:1528-1556`;
`src/helpers/wifi/SerialWifiInterface.cpp:57-70`.

**Where it matters.** SEC-003, DOC-009.

### RES-009 Experiment: an event-loop stall longer than the command timeout ends the session

**Fact.** With the real `CompanionSession` over a socket pair to a thread that answers each
command 1 s after receiving it, a `time.sleep` of 6 s scheduled on the loop 0.2 s after
`get_battery()` was sent ends the session with "no answer from the radio"; a sleep of 4 s does
not. The answer had arrived during the stall. The cause is asyncio's ordering when the loop
resumes: the I/O callback and the due timer run in one iteration; the reader task and then the
queue's getter task need two more iterations; the waiter resumes in between and finds the getter
not done.

**How it was verified.** `experiments/loop_stall_session.py` (CODE-001; it uses `tests/radio_emulator.py`'s
`EmulatedRadio` in a thread and a `SocketTransport` over `socket.socketpair()`); Python 3.11.17.
A pure-asyncio variant with `asyncio.wait({reader.read()}, timeout=5)` and the same stall
*does* see the data, because the read task completes in the iteration before the waiter resumes;
the session's extra hop through `pending.frames.get()` is what loses it.

**Where it matters.** CODE-001, CODE-002, IMP-020.

### RES-010 MQTT 3.1.1: a second connection with the same client id disconnects the first

**Fact.** "If the ClientId represents a Client already connected to the Server then the Server
MUST disconnect the existing Client" (MQTT 3.1.1 §3.1.4, [MQTT-3.1.4-2]). With
`clean_session=True` (the gateway's setting) the broker keeps no session state across
connections, so a stable client id serves only as a label in logs and `$SYS`. paho-mqtt 2.1
reconnects after `reconnect_delay_set(1, 60)` with exponential back-off.

**How it was verified.** The OASIS standard text; `mqtt/client.py:77-83,164`.

**Where it matters.** SEC-006, IMP-009.

### RES-011 Home Assistant's user model has no entity-level permissions in the UI

**Fact.** Home Assistant has administrators and non-administrators. Non-administrators cannot
open the configuration panel (devices, entities, automations) but can call any service on any
entity through dashboards, the entity search, and the REST and WebSocket APIs with their token;
the `event.received` trigger, notify entities, buttons, switches, selects and text entities are
all callable. A policy system (`homeassistant/auth/permissions`, groups with `entities` policies)
exists in the auth storage and is used for the `system-users` and `system-admin` groups, but
there is no supported UI to create a group with a narrower policy.

**How it was verified.** Home Assistant developer documentation ("Permissions"); the project's
own statements in `docs/home-assistant.md` ("every user can press a button and set a select or
text").

**Where it matters.** SEC-008, IMP-011.

### RES-012 GitHub release assets can be replaced after publication

**Fact.** A user with write access can edit a published release: upload, delete and replace
assets, and change the notes, without touching the tag; the REST API offers
`DELETE /repos/{owner}/{repo}/releases/assets/{asset_id}` and asset upload on an existing
release. Tag rulesets protect the tag (its ref), not the release's assets. GitHub announced
"immutable releases" (assets locked after publication) as a public preview in 2025; its general
availability and defaults should be checked at the time the team reads this.

**How it was verified.** GitHub REST API documentation (releases and release assets); the
GitHub changelog for immutable releases (the reviewer could not confirm its current status from
the sandbox).

**Where it matters.** OPS-002, SUP-003.

### RES-013 systemd features available on Debian 13

**Fact.** Debian 13 (trixie) ships systemd 257. Available: `LoadCredential=` (since 247),
`SetCredentialEncrypted=` (250; needs the host's key or TPM, not usable in an unprivileged
container), `RestartSteps=` and `RestartMaxDelaySec=` (254), `OnFailure=`, `MemoryMax=`,
`IPAddressAllow=`/`IPAddressDeny=`, `ProtectProc=`, `ProcSubset=` (247), `time-sync.target`.
`systemd-analyze security` scores the project's unit 1.6 (the project's own figure; the
reviewer's sandbox has no systemd).

**How it was verified.** Debian package versions (`systemd` 257 in trixie); `systemd.exec(5)`,
`systemd.service(5)`, `systemd.special(7)`.

**Where it matters.** SEC-002, SEC-009, OPS-004, OPS-005.

### RES-014 MQTT 5 support in the project's platforms

**Fact.** paho-mqtt 2.x supports MQTT 5 (`MQTTProtocolVersion.MQTTv5`, properties, reason
codes); Mosquitto 2.0 supports MQTT 5 and bridges 3.1.1 and 5 clients on one broker (properties
are dropped for 3.1.1 subscribers); Home Assistant's MQTT integration offers the protocol version
(3.1, 3.1.1, 5) in its broker options and defaults to 3.1.1 (to confirm against the pinned
release). Message expiry interval, response topic, correlation data and user properties are
MQTT 5 publish properties.

**How it was verified.** paho-mqtt 2.1 documentation; mosquitto.conf(5) and the Mosquitto
changelog; Home Assistant MQTT integration documentation.

**Where it matters.** ARC-014, IMP-015.

### RES-015 `GET_CONTACTS` takes an optional `since` and lists contacts with `lastmod > since`

**Fact.** With a 5-byte frame the companion stores the `since` value and, during the listing,
sends only contacts whose `lastmod` is greater; `CONTACTS_START` carries the total count, not the
filtered one; `END_OF_CONTACTS` carries the newest `lastmod` seen. A listing in progress refuses
another `GET_CONTACTS` with `ERR 4` (bad state). `lastmod` is the radio's clock (RES-007), so the
filter is monotonic only while the radio's clock is.

**How it was verified.** `examples/companion_radio/MyMesh.cpp` (`CMD_GET_CONTACTS` handler;
line 2397 `if (contact.lastmod > _iter_filter_since)`; `_most_recent_lastmod`). The gateway's
`codec.get_contacts(since)` encodes it and nothing calls it with a value.

**Where it matters.** SEC-011, IMP-003.

### RES-016 The telemetry request's tag is the radio's clock, made unique

**Fact.** `BaseChatMesh::sendRequest` sets `tag = getRTCClock()->getCurrentTimeUnique()` and
puts it into the encrypted request; the companion echoes it in its response; `MyMesh` keeps one
`pending_telemetry` tag and turns a response with that tag into push `0x8B`, any other into
`0x8C`. A tag is therefore predictable from the radio's clock (ADR 0018's "another contact that
guesses the clock-based tag") and the gateway correctly binds the answer to the contact it asked
rather than to the tag.

**How it was verified.** `src/helpers/BaseChatMesh.cpp:663-700`, `examples/companion_radio/MyMesh.cpp:1680-1699,739-749`.

**Where it matters.** DOC-022, EXT-010.

### RES-017 No forward secrecy in MeshCore direct messages

**Fact.** The shared secret of a pair is `ed25519_key_exchange(other_pub, own_prv)` (the Ed25519
keys transposed to Curve25519, a static ECDH), cached per contact; every direct message, request
and response between two nodes is encrypted with keys derived from it. A party that obtains a
node's private key decrypts all past traffic of that node recorded from the air.

**How it was verified.** `src/Identity.cpp:155-157`, `src/Identity.h:70-81`,
`src/helpers/ContactInfo.h` (`getSharedSecret`, `shared_secret_valid`).

**Where it matters.** SEC-003.

### RES-018 `uptime_s` in the core statistics counts milliseconds since boot

**Fact.** `GET_STATS` type 0 carries `uptime_secs = _ms->getMillis() / 1000`; it resets only when
the radio restarts. A TCP connection that drops while `uptime_s` keeps counting was dropped by
something other than a radio restart (another client, the network).

**How it was verified.** `examples/companion_radio/MyMesh.cpp:1921-1924`.

**Where it matters.** SEC-003 option 3, IMP-041.

### RES-019 The Dependabot-regenerated lock files differ in form from `make lock`'s

**Fact.** The two open Dependabot branches (`dependabot/pip/requirements/dev-tools-601fa647f8`,
`dependabot/pip/home-assistant/pytest-homeassistant-custom-component-0.13.372`) drop the
platform-marker lines (`colorama … ; os_name == 'nt'`, `pyobjc-*`, `winrt-*`,
`librt … ; platform_python_implementation != 'PyPy'`) that `uv pip compile --universal` writes,
and write `coverage[toml]` and `pyjwt[crypto]` where the project's lock has the bare names. The
hashes are kept.

**How it was verified.** `git diff main...origin/dependabot/…` in the clone.

**Where it matters.** SUP-004.

### RES-020 `journald` rate limits

**Fact.** `RateLimitIntervalSec=30s` and `RateLimitBurst=10000` by default: a service that logs
more than 10,000 lines in 30 s has the excess dropped, with one line saying how many; the limit
scales with free disk space on recent versions. `SystemMaxUse` defaults to 10 % of the file system
(capped at 4 GB).

**How it was verified.** `journald.conf(5)`.

**Where it matters.** SEC-014, OPS-009.

### RES-021 Mosquitto facts the project relies on (confirmed where the reviewer could)

**Fact.** `allow_anonymous false` refuses an anonymous CONNECT with CONNACK 5 (the project's
platform notes verified it on 2.0.18); `max_packet_size` (bytes) makes the broker refuse larger
PUBLISH packets (the project recommends 65536); an ACL file with `user` and `pattern` lines, and
`%c` for the client id in patterns; `listener … protocol websockets` for EXT-012.

**How it was verified.** `mosquitto.conf(5)`; the project's `docs/platform-notes.md`, which goes
further than the reviewer could here.

**Where it matters.** SEC-004, SEC-006, EXT-012.

### RES-022 Python version floor and the installer's `tarfile` filter

**Fact.** `tarfile.extractall(filter="data")` exists since Python 3.12 and was backported to
3.8–3.11 in their security releases (3.11.4 and later); Debian 13 has 3.13. The installer's
`FETCH_PY` runs inside the new Debian 13 container, so the filter is always available there.

**How it was verified.** CPython documentation (`tarfile` "Extraction filters"); the installer's
`FETCH_PY`.

**Where it matters.** OPS-002 (the `--ref` path's safety against hostile archives: good).

### RES-023 Related projects, as the records name them, with the dates checked

**Fact.** ADR 0002 and ADR 0012 evaluate `meshcore-dev/meshcore-ha` (3.0.3), `ipnet-mesh/meshcore-mqtt`
(commit `bbc8584`), `Cisien/meshcoretomqtt` and the Unraid `meshcore-mqtt-bridge`, and cite
meshcore-ha issue 214 on polling load. The reviewer did not re-evaluate them; the records' dates
(2026-10-18, 2026-10-22, 2026-10-23) are recent enough. One addition worth tracking: the MeshCore
project's own `docs/companion_protocol.md` at the pinned commit, which the project's
`docs/protocol.md` §8 corrects in five places; a future firmware release may fix the
documentation or the code, and the project's firmware tests will show which.

**How it was verified.** The records; the fetched `docs/` of the firmware tag.

**Where it matters.** ADR 0002, ADR 0004, EXT-020.

### RES-024 The review's own method, for reproducibility

**Fact.** Static reading of every file under `src/`, `deploy/`, `tools/`, `.github/`, `docs/`,
the test layout and the tests cited; the unit, component and emulator suites run on Python
3.11.17 with `paho-mqtt` 2.1.0 (about 900 tests, `MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1`,
`MESHCORE_HOME_GATEWAY_SKIP_GETTEXT_TESTS=1`); `ruff check` and `ruff format --check`; the
firmware fetched with the project's tool; three experiments (RES-005, RES-009, and the pure
asyncio variant); the GitHub API for issues, pull requests, releases and branches. Not run:
the broker tests, the gettext tests, the firmware tests, the Home Assistant contract tests, the
installer against a host, anything on hardware.

**Where it matters.** `00-instructions-and-plan.md` §1 and §6.
