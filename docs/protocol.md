# MeshCore companion protocol: the subset the bridge uses

This is a reference for the part of the MeshCore companion protocol that meshcore-home-gateway
implements, written from the firmware source. The firmware is authoritative; client libraries were
only read to compare behaviour.

* **Firmware basis:** MeshCore `companion-v1.18.0`, commit `81dbae592ffad40b4212bc0b37e82b38da08f4bc`,
  `FIRMWARE_VER_CODE` 14 (`examples/companion_radio/MyMesh.h:7`).
* **Citations:** `path:line` relative to the firmware root. `MyMesh.cpp` means
  `examples/companion_radio/MyMesh.cpp`, `BaseChatMesh.cpp` means `src/helpers/BaseChatMesh.cpp`.
  Client libraries for comparison: meshcore_py 2.3.17, meshcore-cli 1.6.6, meshcore.js 1.15.0.
* **Marks:** statements without a mark were read in the cited code. *(inferred)* marks conclusions
  drawn from the code that were not tested on hardware.
* **Byte order:** every multi-byte integer is little endian (`MyMesh.cpp:1105-1110`).

Golden frames built from these layouts are in [`tests/test_codec.py`](../tests/test_codec.py).

## 1 Framing (USB serial and TCP)

| Direction | Byte 0 | Bytes 1–2 | Then |
| --- | --- | --- | --- |
| client → radio | `<` (0x3C) | payload length, uint16 LE | payload; first byte = command code |
| radio → client | `>` (0x3E) | payload length, uint16 LE | payload; first byte = response or push code |

* Maximum payload 176 bytes in both directions (`src/helpers/BaseSerialInterface.h:5`). The radio
  never sends more (`src/helpers/ArduinoSerialInterface.cpp:25-28`).
* No checksum, no escaping, no request id (`src/helpers/ArduinoSerialInterface.cpp`).
* **Radio-side parser** (`ArduinoSerialInterface.cpp:39-73`): bytes other than `<` are ignored
  while idle; a zero length returns to idle; longer frames are consumed and truncated to 176 bytes,
  not rejected. There is no timeout, and the parser state survives a reconnect of the client
  *(inferred)*: a partial frame from an earlier client would swallow the next bytes.
* **Resynchronisation** *(inferred from the parser)*: the bridge writes 256 zero bytes at session
  start and discards everything that arrives during the next 0.5 s. Zeros are ignored when idle,
  complete a pending header, and complete a pending frame that still expects at most 256 bytes. A
  frame with no payload byte yet executes as unknown command 0 (answered with `ERR 1`); a partly
  received command executes with zero-padded arguments: a cut channel send, for example, transmits
  its truncated text (`MyMesh.cpp:1178-1193`). A corrupted header that announced a longer frame is
  not completed: the first command then times out and the bridge reconnects; every reconnect feeds
  the stale frame another 256 bytes and a command until it is complete, and restarting the radio
  clears it at once. Valid frames are at most 176 bytes, so only a client that wrote garbage leaves
  such a header. This is for USB serial only.
* **The TCP parser is different** (`src/helpers/wifi/SerialWifiInterface.cpp:102-161`): while no
  header is pending it consumes exactly 3 bytes; a header with type 0 or length 0 is not one
  (:46-48), and the next 3 bytes are read as a header again. 256 zeros are 85 such headers plus
  one byte, which shifts every later header by one byte, so the radio never sees `APP_START`.
  A new TCP connection resets the parser (:57-70), so no stale frame can survive one: the bridge
  sends no zeros over TCP. `tests/radio_emulator.py` parses TCP input the same way.
* **DTR on nRF52 boards** (Adafruit TinyUSB core): the USB serial port only sends while DTR is
  asserted; without DTR the radio silently drops every frame but still executes commands. With DTR
  asserted and nobody reading, the firmware's main loop blocks until there is room
  (TinyUSB `Adafruit_USBD_CDC.cpp:240-258`). Opening the port at **1200 baud** and dropping DTR
  reboots the radio into its bootloader (`Adafruit_USBD_CDC.cpp:270-286`). The bridge opens the
  port at 115200 baud, asserts DTR, clears RTS (as meshcore_py and meshcore-cli do) and reads
  continuously.
* **TCP** (WiFi companion builds, `src/helpers/wifi/SerialWifiInterface.cpp`): same framing on port
  5000 (`examples/companion_radio/main.cpp:46-48`), **no authentication**, one client at a time (a
  new connection replaces the old one, :57-70), and a 4-frame send queue that drops frames when
  full (`SerialWifiInterface.h:27`, :21-40).
* **Several interfaces at once** (`src/helpers/MultiSerialInterface.h:160-198`): every response and
  push goes to all interfaces, and whichever client sends `SYNC_NEXT_MESSAGE` first takes the
  message from the shared queue. The bridge should be the radio's only client.

## 2 Commands

| Code | Command | Request after the code | Answer |
| --- | --- | --- | --- |
| 1 | `APP_START` | 7 reserved bytes, client name (`MyMesh.cpp:1076-1081`) | `SELF_INFO` |
| 22 | `DEVICE_QUERY` | protocol version; ≥ 3 selects the V3 message frames (`MyMesh.cpp:1056-1057`) | `DEVICE_INFO` |
| 5 | `GET_DEVICE_TIME` | – | `CURR_TIME`: uint32 Unix seconds |
| 6 | `SET_DEVICE_TIME` | uint32 seconds; **forward only**, else `ERR 6` (`MyMesh.cpp:1287-1296`) | `OK` / `ERR` |
| 4 | `GET_CONTACTS` | optional uint32 `since` (`MyMesh.cpp:1238-1247`) | `CONTACTS_START`, `CONTACT` × n, `END_OF_CONTACTS` |
| 31 | `GET_CHANNEL` | channel index (`MyMesh.cpp:1752-1766`) | `CHANNEL_INFO` |
| 20 | `GET_BATT_AND_STORAGE` | – | `BATT_AND_STORAGE`: uint16 mV, uint32 used kB, uint32 total kB |
| 10 | `SYNC_NEXT_MESSAGE` | – | oldest queued message frame, or `NO_MORE_MESSAGES` (`MyMesh.cpp:1415-1423`) |
| 2 | `SEND_TXT_MSG` | text type, attempt, uint32 timestamp, 6-byte key prefix, text (`MyMesh.cpp:1132-1146`) | `SENT` / `ERR` |
| 3 | `SEND_CHANNEL_TXT_MSG` | text type 0, channel index, uint32 timestamp, text (`MyMesh.cpp:1178-1185`) | **`OK`** / `ERR` |
| 13 | `RESET_PATH` | full 32-byte public key (`MyMesh.cpp:1317-1327`) | `OK` / `ERR` |
| 59 | `GET_AUTOADD_CONFIG` | – (`MyMesh.cpp:2030-2035`); 1.11 and older answer `ERR 1` | `AUTOADD_CONFIG` |
| 43 | `GET_TUNING_PARAMS` | – (`MyMesh.cpp:1481-1487`) | `TUNING_PARAMS` |
| 56 | `GET_STATS` | statistics type: 0 core, 1 radio, 2 packets (`MyMesh.cpp:70-73, 1914-1964`); another type gives `ERR 6` | `STATS` |
| 38 | `SET_OTHER_PARAMS` | manual adding, telemetry modes (bits 0-1 base, 2-3 location, 4-5 environment), advert location policy; an optional fifth byte, the extra acknowledgements, is left out (`MyMesh.cpp:1488-1503`) | `OK` |
| 58 | `SET_AUTOADD_CONFIG` | the auto-add flags; the optional hop limit byte is left out (`MyMesh.cpp:2023-2029`) | `OK` |
| 21 | `SET_TUNING_PARAMS` | uint32 receive delay × 1000, uint32 airtime factor × 1000 (`MyMesh.cpp:1470-1480`); the gateway writes the receive delay back as read | `OK` |
| 8 | `SET_ADVERT_NAME` | the name, at most 31 bytes are kept (`MyMesh.cpp:1259-1265`) | `OK` |
| 9 | `ADD_UPDATE_CONTACT` | the contact layout of `CONTACT` without the last-modified time (`MyMesh.cpp:194-215, 1328-1346`) | `OK` / `ERR 3` |

Answers `OK` (0) and `ERR` (1, followed by the error code) are generic (`MyMesh.cpp:153-163`).
Error codes (`MyMesh.cpp:135-140`): 1 unsupported command or frame too short, 2 not found (contact,
channel; also no free packet buffer for channel sends), 3 table full (direct message: text too
long **or** no free packet buffer), 4 bad state, 5 file I/O, 6 illegal argument.

Details that matter:

* Every setter rewrites the radio's preferences file (`savePrefs()`), so the managed profile writes
  only what differs, one frame per command ([ADR 0020](decisions/0020-managed-radio-profile-for-the-observer.md)).
  The airtime factor is clamped to 0–9 only when the radio starts (`MyMesh.cpp:988`), not when it
  is set. The gateway has no encoder for radio parameters (11), the private key (23, 24), raw data
  and packets (25, 65), signing (33–35), the BLE PIN (37), factory reset (51) and the radio's
  command line (66); `test_the_codec_has_no_command_that_could_harm_the_radio` fails if one
  appears.

* `SEND_TXT_MSG` looks up the first contact whose key starts with the 6-byte prefix
  (`BaseChatMesh.cpp:874-880`); no match gives `ERR 2`. The text limit is 160 bytes, 158 if
  `attempt > 3` (`BaseChatMesh.cpp:451-454`). An all-zero prefix matches an unused anonymous slot
  *(inferred)*; the bridge never sends one.
* `SEND_CHANNEL_TXT_MSG` has **no minimum-length check**; a frame shorter than 7 bytes leads to a
  `memcpy` with a negative length *(inferred: crash)*. The bridge always sends a non-empty text.
  An index inside the range but never configured is not an error: the radio sends with an all-zero
  key *(inferred)*. The bridge only sends to channels whose `CHANNEL_INFO` has a name.
* `SENT` means "queued for transmission" (`src/Mesh.cpp:637-715`).

## 3 Responses

**`SELF_INFO`** (5), 58 bytes plus the name (`MyMesh.cpp:1084-1117`):

| Offset | Size | Field |
| --- | --- | --- |
| 1 | 1 | advert type |
| 2 | 1 | transmit power, int8 dBm |
| 3 | 1 | maximum transmit power |
| 4 | 32 | own public key |
| 36, 40 | 4 + 4 | latitude, longitude, int32 × 10⁻⁶ degrees; 0, 0 is none. A board with GPS updates it from its fix (`src/helpers/sensors/EnvironmentSensorManager.cpp:939-947`), otherwise it is the position set with `SET_ADVERT_LATLON` (`MyMesh.cpp:981, 1266-1280`) |
| 44 | 1 | extra acknowledgements (`multi_acks`) |
| 45 | 1 | advert location policy: 0 adverts carry no position, 1 they carry this one (`MyMesh.cpp:1299-1303, 2436-2442`; `NodePrefs.h:11-12`) |
| 46 | 1 | telemetry modes: bits 0–1 base (battery), 2–3 location, 4–5 environment; each 0 deny, 1 by the requesting contact's flags, 2 allow everyone (`MyMesh.cpp:1101-1102`, `NodePrefs.h:7-9`) |
| 47 | 1 | bit 0: contacts are added manually (then only the types enabled in `AUTOADD_CONFIG`) |
| 48 | 4 | frequency, uint32 kHz |
| 52 | 4 | bandwidth, uint32 Hz |
| 56 | 1 | spreading factor |
| 57 | 1 | coding rate 5..8 (4/5..4/8) |
| 58 | n | node name, UTF-8, at most 31 bytes |

**`DEVICE_INFO`** (13), 82 bytes (`MyMesh.cpp:1059-1075`): firmware version code (1), maximum
contacts / 2 (2), maximum channels (3), stored BLE PIN (4–7), build date (8–19), manufacturer and
model (20–59), version string (60–79), client repeat (80, firmware code 9 and later), path hash
mode (81, code 10 and later). Strings are NUL-padded. Older firmware sends 80 or 81 bytes; the
bridge accepts 80 and more.

**`CONTACT`** (3), 148 bytes (`MyMesh.cpp:171-192`); the push `NEW_ADVERT` (0x8A) uses the same
layout: public key (1–32), type (33: 1 chat, 2 repeater, 3 room server, 4 sensor), flags (34),
`out_path_len` (35: `0xFF` = no path, sends flood; otherwise bits 0–5 hops, bits 6–7 hash size − 1),
path (36–99), name (100–131), last advert (132), latitude (136), longitude (140), last modified
(144). `CONTACTS_START` carries the contact count, `END_OF_CONTACTS` the newest modification time.

* **Flags** (`src/helpers/ContactInfo.h:29-33`): 0x01 favourite (kept when a full table overwrites
  the oldest contact), 0x02, 0x04, 0x08 this contact may request base, location, environment
  telemetry where the mode says "by contact" (`MyMesh.cpp:641-663`), **0x10 this contact may run
  the radio's CLI over the air** (`BaseChatMesh.cpp:262-268`, `MyMesh.cpp:538-548`). `probe`
  lists the flags and marks 0x10.
* **Path:** of the 64 path bytes only hops × hash size are in use; the rest is stale buffer.
* **Position:** an advert updates it only when it carries one (`BaseChatMesh.cpp:114-116,
  193-196`), so a contact keeps the position of its last advert with one; 0, 0 is none. A record
  in `NEW_ADVERT` has the position of that advert, if any (`BaseChatMesh.cpp:108-120`).
* **Last advert** is the node's clock at its newest advert; the radio drops an advert of a contact
  that is not newer (replay protection, `BaseChatMesh.cpp:131-136`). **Last modified** is the
  radio's clock.

**`AUTOADD_CONFIG`** (25), 3 bytes (`MyMesh.cpp:2030-2035`): flags (1) and hop limit (2). Flags
(`MyMesh.cpp:147-151`): 0x01 a full table overwrites the oldest non-favourite contact, 0x02 chat,
0x04 repeater, 0x08 room server, 0x10 sensor. The type flags only apply with manual adding
(`SELF_INFO` byte 47); without it every type is added (`MyMesh.cpp:308-332`). Hop limit 0 means
none; otherwise only adverts that passed fewer repeaters than the limit add a contact
(`BaseChatMesh.cpp:165-172`). Firmware 1.12 and 1.13 send 2 bytes, without the hop limit
(`MyMesh.cpp:1733-1737` of `companion-v1.12.0`); 1.11 and older do not know the command
(`MyMesh.cpp:1624-1627` of `companion-v1.11.0`) and add every contact unless manual adding is on.

**`TUNING_PARAMS`** (23), 9 bytes (`MyMesh.cpp:1481-1487`): receive delay base (1) and airtime
factor (5), each a float × 1000 as uint32. The factor sets the radio's own duty cycle,
`1 / (1 + factor)` (section 7); the radio clamps it to 0–9 when it loads its settings
(`MyMesh.cpp:988`). Firmware without the command answers `ERR 1`.

**`STATS`** (24), the statistics type in byte 1 (`MyMesh.cpp:1914-1964`), all since the radio
booted:

| Type | Bytes after the type | Fields |
| --- | --- | --- |
| 0 core | 9 | battery uint16 mV, uptime uint32 s, error flags uint16 (0x01 send queue full, 0x02 channel activity detection timed out, 0x04 receive start timed out; `src/Dispatcher.h:110-112`), packets waiting to be sent uint8 |
| 1 radio | 12 | noise floor int16 dBm, last RSSI int8 dBm, last SNR int8 × 4, time on air sent and received uint32 s each (everything the radio sent: messages, acknowledgements, adverts, repeats) |
| 2 packets | 28 | packets received, sent, sent flood, sent direct, received flood, received direct, receive errors, uint32 each; the last one from 1.18 on (24 bytes before) |

**`CHANNEL_INFO`** (18), 50 bytes: index (1), name (2–33, NUL-terminated; **the bytes after the NUL
are stale buffer contents**), secret (34–49) (`MyMesh.cpp:1752-1766`). The bridge does not keep
channel secrets.

**Received messages**, returned by `SYNC_NEXT_MESSAGE` (`MyMesh.cpp:440-467, 558-583`). The layout
is fixed when the message is queued, so messages that arrived before `DEVICE_QUERY 3` come in the
legacy layout; the bridge decodes both.

| Direct, V3 (16) | Legacy (7) | Size | Field |
| --- | --- | --- | --- |
| 1 | – | 1 | SNR × 4, int8 |
| 4 | 1 | 6 | sender key prefix |
| 10 | 7 | 1 | path length: `0xFF` arrived direct, else bits 0–5 hops |
| 11 | 8 | 1 | text type: 0 plain, 1 CLI data, 2 signed (room server), 3 CLI command |
| 12 | 9 | 4 | sender timestamp, uint32 (sender's clock, unchecked) |
| 16 | 13 | n | text; for type 2 a 4-byte author prefix comes first |

| Channel, V3 (17) | Legacy (8) | Size | Field |
| --- | --- | --- | --- |
| 1 | – | 1 | SNR × 4, int8 |
| 4 | 1 | 1 | channel index |
| 5 | 2 | 1 | path length, as above |
| 6 | 3 | 1 | text type, always 0 |
| 7 | 4 | 4 | sender timestamp |
| 11 | 8 | n | `"<sender name>: <text>"`; the name is not authenticated |

`SYNC_NEXT_MESSAGE` can also return `CHANNEL_DATA_RECV` (27, binary channel data), which the bridge
skips.

## 4 Push frames

Codes 0x80 and above can arrive at any time, also between the frames of `GET_CONTACTS`
(`MyMesh.cpp:2388-2421`). Those the bridge acts on:

| Code | Name | Content | Bridge |
| --- | --- | --- | --- |
| 0x80 | `ADVERT` | public key of a contact in the table that advertised (`MyMesh.cpp:358-367`) | remember the key if it shares a command sender's prefix (ADR 0009); refresh contacts; report the advert |
| 0x81 | `PATH_UPDATED` | public key | remember the key, as above; refresh contacts |
| 0x82 | `SEND_CONFIRMED` | 4-byte ACK code, uint32 round trip in ms | match to a waiting message |
| 0x83 | `MSG_WAITING` | – | sync messages |
| 0x88 | `LOG_RX_DATA` | SNR × 4 (int8), RSSI (int8 dBm), raw packet: every packet the radio receives, before it handles it (`MyMesh.cpp:291-302`, `src/Dispatcher.cpp:199`); see below | optional `rx` topic |
| 0x8A | `NEW_ADVERT` | contact layout of a node that advertised but was not added; 1.11 and older also send it for a contact they have just added (`BaseChatMesh.cpp:88-134` of `companion-v1.11.0`) | renew the key if it is already remembered; refresh contacts; report the advert with its record |
| 0x8F | `CONTACT_DELETED` | public key of a contact overwritten to make room (`MyMesh.cpp:342-349`) | remember the key, as above; refresh contacts |
| 0x90 | `CONTACTS_FULL` | – | refresh contacts |

Further pushes (0x84–0x87, 0x89, 0x8B–0x8E: raw data, login, status, trace, telemetry, binary and
path-discovery responses, control data) are ignored (`MyMesh.cpp:117-133`).

**Raw packets in `LOG_RX_DATA`** (wire format, `src/Packet.cpp:65-85`): header (1: bits 0–1 route
0 flood with transport codes, 1 flood, 2 direct, 3 direct with transport codes; bits 2–5 payload
type; bits 6–7 version, `src/Packet.h:8-37`), two uint16 transport codes (only routes 0 and 3),
path length (1, as `out_path_len`; hash size 4 is reserved and invalid, `src/Packet.cpp:13-18`),
path, payload (at least one byte). Pushes longer than a frame are not sent (`MyMesh.cpp:292`).
An advert (payload type 4, `src/Mesh.cpp:252-290, 410-440`) is public key (32), timestamp (uint32,
the sender's clock), signature (64), then app data of at most 32 bytes
(`src/MeshCore.h:12`, `src/Mesh.cpp:269`; `src/helpers/AdvertDataHelpers.cpp:4-68`): flags (bits
0–3 type; 0x10 position follows, int32 latitude and longitude × 10⁻⁶; 0x20 and 0x40 a uint16
feature field each; 0x80 the rest is the name). The radio drops an advert without a name
(`BaseChatMesh.cpp:122-127`). **A logged advert is unverified**: its signature is checked by the
radio only after the log (`src/Mesh.cpp:271-290`), and the bridge cannot check Ed25519 signatures
with the standard library. Only the `ADVERT` or `NEW_ADVERT` that follows for the same key and
timestamp shows that the radio accepted it.

## 5 Semantics

* **One command at a time.** The radio reads one frame per main-loop pass and answers it before the
  next (`MyMesh.cpp:2388-2391`). The bridge sends one command, waits up to 5 s for an answer with
  one of the expected codes, routes push frames separately and drops anything else.
* **Offline queue** (`MyMesh.h:61-63`, `MyMesh.cpp:224-260`): 16 frames by default (some builds
  more), RAM only. When full, the oldest channel message is evicted; without one, the new message
  is dropped. `MSG_WAITING` is pushed once per queued message; nothing is pushed for messages that
  were already waiting, so the bridge drains the queue after connecting.
* **Text limits:** direct 160 bytes; channel `160 − (name length + 2)` bytes, because the radio
  prepends `"<name>: "` and silently truncates the rest (`BaseChatMesh.cpp:518-531`).
* **ACK code** (`BaseChatMesh.cpp:451-471`):
  `SHA-256(timestamp uint32 ‖ attempt & 3 ‖ text ‖ sender's own public key)[0:4]`. Attempts 0–3
  give four different codes; attempt 4 repeats attempt 0.
* **Retries:** the radio does not retry and sends no timeout push (`BaseChatMesh.cpp:989-993`). A
  retry with the same timestamp and a higher attempt number is a new packet, and the receiving
  radio does not deduplicate it, so it can show up twice *(inferred)*; a retry with the same attempt
  number is dropped by the 160-entry seen-packet table and gets no acknowledgement
  (`src/helpers/SimpleMeshTables.h:9-57`). The bridge keeps the timestamp, increments the attempt,
  accepts late acknowledgements of earlier attempts, and deduplicates received messages by sender,
  timestamp and text.
* **Expected ACKs:** 8 entries, circular (`MyMesh.h:280-287`).
* **Suggested timeout** in `SENT` (`MyMesh.cpp:888-896`): flood `500 + 16 · t`, direct
  `500 + (6 · t + 250) · (hops + 1)` ms, with `t` the estimated airtime of the packet. The bridge
  waits 1.2 times that, between 5 and 60 s, like meshcore_py.
* **Path reset:** after unanswered direct attempts, `RESET_PATH` makes the next send flood
  (meshcore_py and meshcore-cli do this before the third attempt).
* **No replay protection for text messages** in the firmware beyond the seen-packet table
  (`BaseChatMesh.cpp:133-136` checks timestamps only for adverts). The bridge adds its own for
  commands (ADR 0009).
* **Unknown senders:** direct messages are decrypted only for senders in the contact table
  (`BaseChatMesh.cpp:203-211`); messages from others are dropped by the radio.
* **Every contact type can send.** `onPeerDataRecv` checks no type (`BaseChatMesh.cpp:222-245`):
  a repeater, room server or sensor contact delivers plain direct messages like a chat contact.
* **Contact table** (`BaseChatMesh.cpp:72-106, 122-200`): an advert from an unknown node adds it
  if its type may be added (`AUTOADD_CONFIG`) and the hop limit allows. A full table refuses the
  new contact (push `CONTACTS_FULL`) unless overwriting is on; then the non-favourite contact with
  the oldest modification time is replaced and its key pushed as `CONTACT_DELETED`. A plain direct
  message renews its sender's modification time (`BaseChatMesh.cpp:240`).
* **A contact can leave the table without `CONTACT_DELETED`.** Every advert stores the type it
  claims, also type 0 (`BaseChatMesh.cpp:192`). The next advert from a type-0 contact clears its
  slot and runs the add flow again (`BaseChatMesh.cpp:151-154`), which refuses it when its type is
  not added automatically, the hop limit is exceeded, or the table is full without overwriting
  (`BaseChatMesh.cpp:158-182`). The key holder can do this with two adverts.
* **Every advert is pushed with the full key:** `ADVERT` for a contact that is (now) in the table,
  `NEW_ADVERT` for a node that was not added (`MyMesh.cpp:358-367`; `BaseChatMesh.cpp:158-182`
  passes the refusals on). Firmware 1.11 and older also announce a contact they have just added
  with `NEW_ADVERT`, but they have no way to drop a contact without a client: neither overwriting
  (from 1.12) nor the type-0 path (from 1.17). A push goes out once, to whichever client is
  connected: over USB the firmware always assumes one (`ArduinoSerialInterface.cpp:16-18`), over
  TCP only a connected client gets it (`MyMesh.cpp:342-349`). A bridge that was not connected
  never learns of it.
* **Over TCP pushes can be lost.** The WiFi interface queues at most 4 frames and drops new ones
  while the queue is full (`SerialWifiInterface.cpp:27-31`); it sends one queued frame per
  main-loop pass (`SerialWifiInterface.cpp:85-100`), and a contact listing adds one frame per pass
  because `isWriteBusy` is always false (`SerialWifiInterface.cpp:42-44`, `MyMesh.cpp:2392-2402`).
  Pushes that arrive during a listing fill the queue for the rest of it.
* **Contact list:** `GET_CONTACTS` sends one contact per main-loop pass (`MyMesh.cpp:2388-2410`),
  so the list is not a snapshot; a contact overwritten during the read can be missing, while its
  push arrives (over USB). The eight slots for temporary contacts (type 0, which a client creates
  for anonymous requests, `BaseChatMesh.cpp:72-86, 883`) are never listed
  (`BaseChatMesh.cpp:976`), yet the radio decrypts messages with them too
  (`BaseChatMesh.cpp:203-211`); only a client of the radio can create them.
* **Listing cost** *(calculated)*: about 150 bytes per contact with framing, so a table of 350
  contacts takes about 4.6 s over a USB-UART bridge at 115200 baud; native USB is faster.

## 6 On-air sizes

Packet: header (1), transport codes (4, only on floods with an active flood scope), path length
(1), path (hops × hash size, at most 64), payload (`src/Packet.cpp:52-63`, `src/Dispatcher.cpp:309-325`).
Encryption: AES-128-ECB in 16-byte blocks, zero-padded, with a 2-byte MAC in front
(`src/Utils.cpp:85-145`).

| Packet | Payload bytes |
| --- | --- |
| direct text, `L` text bytes | `4 + 16 · ⌈(5 + L) / 16⌉` (hashes 2, MAC 2, timestamp 4, flags 1) |
| channel text, name `N` bytes | `3 + 16 · ⌈(7 + N + L) / 16⌉` (channel hash 1, MAC 2, timestamp, flags, `"name: "`) |
| acknowledgement | 6 |
| path packet returned after a flooded acknowledgement | `4 + 16 · ⌈(6 + P) / 16⌉` for a way back of `P` path bytes (hashes 2, MAC 2, path length 1, path, type 1, random 4); sent direct along the way there (`src/Mesh.cpp:173-178, 448-486`) |

A flood leaves the originating radio with an empty path; each repeater adds its hash
(`src/Mesh.cpp:344-357, 647-678`). The bridge counts the transport codes on every flood and,
after a flooded direct message, the returned path packet for a recipient 4 path bytes away (4 hops
with one-byte hashes, either way): 26 bytes on air. Up to that distance the estimate errs on the
high side. A more distant recipient makes the path packet larger than counted: at EU/UK Narrow a
flooded attempt then takes about 0.2 s more airtime than estimated at 16 path bytes, and about
1 s more at the maximum of 64.

## 7 Radio parameters and time on air

* Preamble 32 symbols for SF ≤ 8, 16 above (`src/helpers/radiolib/RadioLibWrappers.h:56-57`, since
  firmware 1.16); CRC on (`src/helpers/radiolib/CustomSX1262.h:69`); explicit header; low data rate
  optimisation automatic in RadioLib (symbol time ≥ 16 ms).
* Time on air (Semtech SX126x; for SF 7–12 identical to the SX127x formula in AN1200.13):

  ```text
  Tsym = 2^SF / BW
  N    = Npre + 4.25 + 8 + ceil(max(8·PL + 16·CRC − 4·SF + 8 + 20·explicit, 0) / (4·(SF − 2·LDRO))) · CR'
  SF 5/6: Npre + 6.25 + 8 + ceil(max(8·PL + 16·CRC − 4·SF + 20·explicit, 0) / (4·SF)) · CR'
  ToA  = N · Tsym          (CR' = 5..8 for 4/5..4/8)
  ```

* The implementation in `core/airtime.py` reproduces the published values of the TTN airtime
  calculator test suite, the mobilefish tutorial, `tanupoo/lorawan_toa`, Semtech AN1200.13 Table 3
  and an SDR measurement on an SX1276, and RadioLib's `getTimeOnAir` for the EU/UK Narrow preset
  (869.618 MHz, 62.5 kHz, SF 8, CR 4/8): 20 bytes 377.856 ms, 100 bytes 1033.216 ms, 200 bytes
  1852.416 ms. Sources are listed in `tests/test_airtime.py`.
* The firmware has its own transmit budget, `airtime_factor`: duty cycle `1 / (1 + af)` over an
  hour, default 50 % (`src/Dispatcher.cpp:19-53`, `MyMesh.cpp:917`). For a 10 % limit it has to be
  set on the radio (af = 9); the bridge's budget only covers what the bridge asks the radio to send.

## 8 Where the firmware's own documentation differs from the code

`docs/companion_protocol.md` in the firmware repository warns that some information may be
inaccurate. Differences relevant here:

| Documentation | Code (1.18.0) |
| --- | --- |
| channel send answers `SENT` | answers `OK` (`MyMesh.cpp:1192-1193`) |
| `SEND_CONFIRMED` carries a 6-byte code | 4-byte code plus uint32 round trip (`MyMesh.cpp:426-430`) |
| text type 2 has a 4-byte signature | 4-byte author key prefix (`BaseChatMesh.cpp:295`) |
| messages are limited to 133 characters | 160 bytes direct; channel 160 minus the name prefix |
| `APP_START` must come first | not enforced |
