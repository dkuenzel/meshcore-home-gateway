# 03 Security findings

Every item follows the template of `00-instructions-and-plan.md` §5: no rating, no priority, full
impact and preconditions. Items are grouped by the boundary of `02-threat-model.md` §6 where the
attacker stands. Findings that are mainly operational (installer, unit) or supply chain are in
`07-…` and `08-…`; code defects without an attacker are in `05-…`.

Summary of the items in this document:

| ID | Title | Boundary |
| --- | --- | --- |
| SEC-001 | Acknowledgements are forgeable from a guessed text: a false `confirmed` and suppressed retries | B1 air |
| SEC-002 | `set_clock` moves the radio's clock forward irreversibly from a wrong host clock | B2 / operator |
| SEC-003 | Over TCP the radio's private key is exportable and importable by anyone on the network (default build), and the gateway cannot tell a second client | B2 link |
| SEC-004 | An insecure broker configuration (no credentials, no TLS) is accepted without a word | B3 broker |
| SEC-005 | A forged or stale retained document is neither detected nor healed; a presence automation follows it | B3 broker |
| SEC-006 | The MQTT client id is predictable; birth messages trigger unbounded re-announcements | B3 broker |
| SEC-007 | Names from the mesh reach the Home Assistant UI unflagged: a stranger can look like a contact or like the gateway | B4/B5 Home Assistant |
| SEC-008 | Every Home Assistant user can send, add and remove contacts, toggle anyone's telemetry and read every position | B5 Home Assistant users |
| SEC-009 | The broker password is a group-readable file, printed by `config answers`, never a systemd credential | B6 host |
| SEC-010 | An advert flood with invented keys grows the history's `node` table without a cap | B1 air |
| SEC-011 | An advert flood republishes a 15 KiB document per advert, evicts real heard nodes and keeps the gateway re-reading the contact table | B1 air |
| SEC-012 | Messages on the public channel are published by default: every neighbour's chatter lands in Home Assistant's recorder and the history | B1 air / privacy |
| SEC-013 | A contact can starve the radio's own duty cycle with direct messages the radio must acknowledge | B1 air |
| SEC-014 | Malformed frames over TCP produce one warning each, without a limit | B2 link |
| SEC-015 | Bystanders' data (names, keys, signal, raw packets) is collected, retained and recorded without their knowledge | privacy |
| SEC-016 | Telemetry is opt-out per contact: a newly added contact is asked within a minute, and consent is inferred from the companion's settings | privacy / design |
| SEC-017 | The telemetry answer, the message and the add-contact paths accept names and texts without length or character limits beyond the protocol's | B1 / B3 |

---

## B1 The air

### SEC-001 Acknowledgements are forgeable from a guessed text: a false `confirmed` and suppressed retries

| | |
| --- | --- |
| Category | security |
| Component | `src/meshcore_home_gateway/bridge.py:653-698` (`_attempt`), `bridge.py:161-199` (`AckWaiter`), `protocol/codec.py:207-210` (`expected_ack`); firmware `src/Mesh.cpp:560-571` (`createAck`), `src/Mesh.cpp:117-128` (ACK receive), `src/helpers/BaseChatMesh.cpp:240-257` (ACK hash), `examples/companion_radio/MyMesh.cpp:424-434` (`processAck`) |
| Related | design §6.2 step 7, §7.4; RES-001; IMP-001; SEC-004 (the `delivery` topic reveals texts to broker readers) |

**Description.** When the gateway sends a direct message it reports `delivery: confirmed` as soon
as the radio pushes `SEND_CONFIRMED` with the 4-byte acknowledgement code of one of its attempts.
That code is `SHA256(timestamp ‖ attempt & 3 ‖ text ‖ sender's public key)[:4]`
(`BaseChatMesh.cpp:244-245`, `MyMesh.cpp:1148-1163`, the gateway's `codec.expected_ack`). The
recipient sends it back **in the clear** as a `PAYLOAD_TYPE_ACK` packet (`Mesh.cpp:560-571` copies
the hash into the payload without encryption; `Mesh.cpp:117-128` matches the first four bytes
against the sender's table of expected codes and calls `onAckRecv`). Nothing in the packet proves
that the recipient produced it: anyone who knows the four inputs can produce the same code.

Three of the four inputs are public or observable from the air: the sender's public key is in
every advert of the radio and in its contact link; the attempt number is 0 for the first
transmission; the timestamp is the gateway's wall clock in seconds at the moment it handed the
message to the radio (`bridge.py:602`), which an observer sees within a second or two from the
packet's time on air (and can cover by trying a few values). The fourth input, the text, is
encrypted on the air, but the texts a home automation sends are **fixed strings** by their
nature: "Smoke in the kitchen", "Gate open", "Water pump stopped", "Back online" (the project's own
examples). An attacker who has read the documentation of a household's automations, or has watched
the `delivery` topic once (it carries `text`), or simply tries the usual phrases, computes a
handful of candidate codes per observed packet and transmits them. The radio takes the first
match, pushes `SEND_CONFIRMED`, and the gateway publishes `confirmed` with a round-trip time.

The firmware adds two bytes in 1.18 (an attempt byte and a random byte, `BaseChatMesh.cpp:246-248`)
but matches only the first four (`MyMesh.cpp:425`), so they change nothing.

**Evidence.** Firmware source at `companion-v1.18.0` (commit `81dbae5`), fetched with the
project's `tools/fetch_firmware.py`:

* `src/helpers/BaseChatMesh.cpp:244-245`: `mesh::Utils::sha256(ack_hash, 4, data, 5 + text_len, from.id.pub_key, PUB_KEY_SIZE);` where `data` is `timestamp(4) ‖ flags(1) ‖ text`.
* `src/Mesh.cpp:560-571`: `createAck` builds a packet of type `PAYLOAD_TYPE_ACK` whose payload is the hash bytes; no encryption, no signature.
* `src/Mesh.cpp:117-128`: on receive, `memcpy(&ack_crc, &pkt->payload[0], 4)` and, unless the packet was seen before, `onAckRecv(pkt, ack_crc)`.
* `examples/companion_radio/MyMesh.cpp:424-434`: `processAck` compares four bytes with each entry of `expected_ack_table`, pushes `SEND_CONFIRMED` with the round trip, and clears the entry.
* Gateway: `codec.expected_ack` reproduces the formula; `tests/test_codec.py` pins it with a golden vector; `bridge._attempt` ends the delivery on the first matching push.

The reviewer did not transmit a forged acknowledgement on real hardware; the chain above is
complete in the source, and the firmware tests of the project (`tests/firmware/`) would allow a
scripted stand-in on the simulated air to do it (see the suggested test).

**Preconditions and attacker model.** T1: a stranger in radio range of the gateway's radio (or of
a repeater that forwards to it) with any board that can transmit raw MeshCore packets (the
project's own test harness contains a `StandIn` node that does exactly this on the simulated
air). The attacker needs the text, or a short list of candidates. They do not need to be a
contact, to decrypt anything, or to know any secret.

**Impact.** The alert path (A2) loses its only end-to-end signal:

1. The household's automation reports a safety alert as delivered (`confirmed`, with a plausible
   round-trip time) although nobody received it. An automation that escalates only on `failed`
   never escalates. A person who checks the dashboard sees "confirmed".
2. The gateway stops retrying: a forged acknowledgement of attempt 0 ends the delivery
   (`bridge.py:695-698`), so the message is transmitted once, during whatever the attacker is
   doing to make the real recipient miss it (jamming at the moment, or simply the recipient being
   out of range). The forged acknowledgement turns a message that would have been retried three
   times and flooded into one that is sent once and declared delivered.
3. The `history` document and the `message` table record the message as `confirmed`.
4. For a message with `attempt` ≥ 1 after a path reset, the same holds.

The attack cannot make the gateway send anything, read anything, or run anything; it falsifies
the delivery state of outgoing messages. For a bridge whose stated purpose is alerts that must
arrive when other networks are down, that is the state the household relies on most.

**Existing mitigations.** None against this. The radio's seen-packet table drops an identical
acknowledgement packet a second time, which does not help (a forged one is new). Design §7.4
discusses the confidentiality of messages ("only as confidential as MeshCore's encryption") but
not the authenticity of acknowledgements. The protocol reference (`docs/protocol.md` §5, "ACK
code") documents the formula without drawing the conclusion.

**Suggested mitigation or implementation.** The weakness is the protocol's; the gateway can only
reduce the attacker's knowledge of the text or stop treating a code match as proof. Options, all
compatible with each other:

1. **Document it** (`docs/design.md` §7.2 and §7.4, `docs/mqtt-api.md` under `delivery`,
   `docs/home-assistant.md` where `confirmed` is explained): `confirmed` means "a packet carrying
   the expected code arrived", which proves receipt only if the text was unguessable. Advise
   automations that need proof to use an application-level reply (the recipient sends a command
   word back, which is authenticated by ADR 0009) and to escalate on the absence of that reply,
   not on `failed`.
2. **Make texts unguessable on request.** A setting `[messages] confirm_nonce = true` (or per
   target) appends a short random suffix to each direct message, for example ` #a7f3` (5 bytes,
   `secrets.token_hex(2)`), before the acknowledgement code is computed. The suffix is in the text
   the recipient sees. With 16 bits of entropy the attacker needs 65,536 guesses per message,
   each a transmitted packet; with 24 bits (` #a7f3c1`, 7 bytes) 16 million. The cost is 5–7 of
   the 160 bytes and a visible tag. The duplicate check must use the text before the suffix
   (`bridge._accept` keys the deduplicator on `(alias, text)` before `_deliver` adds it), and the
   `delivery` and `history` payloads should carry the text as sent (with the suffix), so a reader
   can match what the recipient shows. Retries keep the same suffix (the acknowledgement code
   depends on it). Sketch in `bridge._deliver`:

   ```python
   text = item.text
   if self.config.messages.confirm_nonce and direct:
       text = f"{item.text} #{secrets.token_hex(2)}"  # fits: the policy limit leaves room
   ```

   and `_text_policy` reserves `len(" #") + 4` bytes when the setting is on, so the cleaned text
   plus suffix never exceeds 160 bytes.
3. **Do not reveal texts on the bus more than needed.** The `delivery` event carries `text` in
   every step. A broker client with read access learns the texts the automations send (and so can
   anyone who reads Home Assistant's recorder, where the *Delivery* event entity keeps them). This
   is not the air, but it is where an attacker who is also a guest on the broker or in Home
   Assistant would learn the exact phrasing. Consider a setting that omits `text` from `delivery`
   events after `queued` (the `id` correlates them), or documenting the exposure.
4. **Report the evidence of a forgery when it exists.** Two acknowledgements for one attempt, an
   acknowledgement whose round trip is implausibly short for the path (below the time on air of
   the message plus the acknowledgement), or an acknowledgement that arrives before the radio could
   have finished transmitting, are signs. The gateway sees `rtt_ms` from the radio
   (`MyMesh.cpp:428`): a round trip shorter than `2 × time_on_air(message)` is impossible for a
   genuine acknowledgement of a direct message to a neighbour. Logging a warning and adding
   `"suspect": true` to the `confirmed` event would cost little; it would not stop the attack but
   would make it visible in the journal and to an automation. (The multi-ack feature of the
   firmware sends several acknowledgements per message, so "two acknowledgements" alone is not a
   sign.)
5. **Upstream.** Raise it with the MeshCore project: an acknowledgement that includes a MAC over
   the hash with the pair's shared secret, or that is sent encrypted like a path return, would
   close it for everyone. The gateway cannot do this alone; ADR 0004's "revisit" clause already
   names "signed commands" as a trigger.

A test for option 2: `tests/test_bridge.py` — a message sent with the setting on has a text
ending in ` #` and four hex digits, the deduplicator treats the request as a duplicate of the
same text without the suffix, and the retry keeps the suffix. For option 4: an acknowledgement
pushed with `round_trip_ms` below the message's time on air gives a `confirmed` event with
`suspect: true` and a warning. A firmware-test scenario (option 1's documentation claim): the
`StandIn` of `tests/firmware/air.py` transmits a `PAYLOAD_TYPE_ACK` packet with the computed code
after B is switched off; the gateway reports `confirmed`.

**References.** MeshCore `docs/packet_format.md` and `docs/payloads.md` at the pinned commit
(the ACK payload: "4 byte CRC/hash"); RFC 2104 (HMAC) for what an authenticated acknowledgement
would need; the project's `docs/protocol.md` §5.

---

### SEC-010 An advert flood with invented keys grows the history's `node` table without a cap

| | |
| --- | --- |
| Category | security (denial of service, disk) |
| Component | `src/meshcore_home_gateway/history.py:170-184` (`node_heard`), `history.py:186-212` (`purge`), `observer.py:605-656` (`_advert`), `observer.py:688-690` (`_store_heard`) |
| Related | SEC-011; ADR 0016; DOC-004 (7 vs 30 days); CODE-00x |

**Description.** Every `NEW_ADVERT` push (an advert the radio heard from a node it does not hold)
makes the observer write a row into the history's `node` table with the node's key, name, type,
signal and times (`observer._advert` → `_store_heard` → `history.node_heard`, an upsert keyed by
the public key). The `message`, `position` and `reading` tables have row caps (10,000; 100,000;
200,000) besides their age limits; the `node` table has only an age limit, `nodes_days` (30 days
in `Retention`, never set by the CLI). The in-memory list of heard nodes is capped at 50
(`core/mesh.MAX_NODES`), so memory is safe, but every distinct key heard within 30 days is a row
on disk.

A MeshCore advert is signed by the key it announces, and the radio accepts an advert from any key
it has never seen as a new node (`BaseChatMesh.cpp:156-182`). Generating a fresh Ed25519 key is
microseconds; an advert packet at the EU/UK Narrow preset is about 1.3 s of airtime (RES-004).
A single board can therefore announce a new identity every 2–3 s indefinitely, and a few boards
or a faster preset proportionally more.

**Evidence.** Experiment (`/tmp/advert_flood_experiment.py`, reproduced in `11-research-notes.md`
RES-005): 50,000 `NEW_ADVERT` records with distinct keys fed to an `Observer` with a real
`History` on a temporary file gave 50,000 rows in `node` and 14.4 MB of database (about 290 bytes
per node); the in-memory heard nodes stayed at 50. At one advert every 3 s that is 28,800 rows and
about 8 MB a day, 250 MB a month until the purge by age catches up; the container's disk is 2 GB
by default and shared with the journal and the other tables.

`history.purge` (`history.py:186-212`) deletes `node` rows only `WHERE heard_at < now − nodes_days`.

**Preconditions and attacker model.** T1, any board in range, transmitting continuously (which is
itself a violation of the duty cycle the attacker does not care about). No contact status, no
secret. The attack is cheap and leaves the attacker's board as the only evidence.

**Impact.** Disk of the container fills at a rate the attacker chooses (within the channel's
capacity); each row also costs an `INSERT … ON CONFLICT` with a `fsync`-free WAL write on the event
loop. A full disk breaks the history (`History._broke`: "running without it until restart"), then
`state.json` writes fail (`state_unwritable`: commands are refused, exclusions cannot be stored,
the telemetry schedule is not saved), then the journal. The gateway keeps running until the state
directory cannot be written; the alert path survives, the command path does not (A1 availability,
A11). The `node` rows also carry the attacker's chosen names: 30 days of them in the history.

**Existing mitigations.** The age limit (30 days); the memory bound of 50; the rate limit on
`node` *events* (0.2/s, burst 10, `observer.py:94`), which protects the bus but not the disk; the
hourly purge.

**Suggested mitigation or implementation.**

1. **A row cap for `node`, like the other tables.** In `history.purge`, after the age delete:

   ```python
   self._write(
       "DELETE FROM node WHERE key IN (SELECT key FROM node ORDER BY heard_at DESC "
       "LIMIT -1 OFFSET ?)", (MAX_NODES_ROWS,),
   )
   ```

   with `MAX_NODES_ROWS` of a few thousand (the radio's table holds 350 at most; the heard nodes
   the gateway shows are 50; 5,000 keeps three months of a lively mesh and bounds the attack at
   about 1.5 MB). The restore at start (`observer._restore` → `history.nodes()`) reads all rows:
   with the cap it stays small.
2. **Purge on write, not hourly.** The purge runs every `PURGE_INTERVAL_S` (3600 s). Within an
   hour the attacker writes 1,200 rows (at one every 3 s), which is fine with a cap, but a cap
   applied hourly still lets the table grow by that much between purges; a cheap guard in
   `node_heard` (count rows every N inserts, or keep a counter in the observer) would bound it
   tighter. The hourly purge with a cap is probably enough.
3. **Do not store what the gateway does not keep.** The in-memory `Mesh.nodes` evicts beyond 50;
   the history could mirror that (delete the evicted key's row when `node_heard` evicts, keep
   rows only for nodes still "heard" or contacts). Then the table equals the memory bound plus
   contacts. This changes what comes back after a restart (`_take_heard_nodes` restores from the
   table), which the documents say is "the last 50 of the last 7 days" anyway.
4. **Align the retention**: `nodes_days` 7 (ADR 0016, `docs/mqtt-api.md` "the last 50 of the last
   7 days") or document 30 (DOC-004).

Test: `tests/test_history.py` — after inserting `MAX_NODES_ROWS + 100` nodes and a purge, the
table holds `MAX_NODES_ROWS` rows and the most recently heard survive; `tests/test_observer.py` —
a flood of `NEW_ADVERT`s beyond the cap leaves the history bounded.

**References.** `docs/decisions/0016-history-in-a-local-sqlite-database.md` (retention "sightings
7 days"); SQLite `DELETE … LIMIT` needs `SQLITE_ENABLE_UPDATE_DELETE_LIMIT`, which Debian's build
does not enable, hence the subselect form above.

---

### SEC-011 An advert flood republishes a 15 KiB document per advert, evicts real heard nodes and keeps the gateway re-reading the contact table

| | |
| --- | --- |
| Category | security (denial of service: bus, Home Assistant, the radio link; usability) |
| Component | `observer.py:605-656` (`_advert` → `_publish_nodes` on every advert), `views.py:246-267` (`nodes_document`, up to 15 KiB), `core/mesh.py:26,357-395` (`MAX_NODES = 50`, eviction), `radio/session.py:678-681` (`_push`: every `Advert` marks the table dirty), `session.py:599-626` (`_maintenance`: re-read 2 s later), `mqtt/manage_entities.py:200-221` (the select's options) |
| Related | SEC-010, IMP-003 (incremental contact reads), RES-005 |

**Description.** Three effects of the same flood:

1. **Amplification on the bus.** Every advert from a node the radio does not hold changes the
   heard-node list, and `_publish_nodes` publishes the `nodes` document again, retained. The
   document is bounded at 15 KiB and holds up to 50 nodes with names, keys, prefixes, times and
   signal: with 50 nodes it is at its bound. The experiment measured 15.1 KB per advert on the
   bus (755 MB for 50,000 adverts). Home Assistant subscribes to `nodes` for the *Nodes heard*
   sensor and the *Look-alike node heard* binary sensor (with `json_attributes_template`): each
   publish is parsed, the attributes (`nodes`, `total`) change, so the recorder writes a row.
   The `node` *events* are rate-limited (`NODE_EVENTS_PER_S`), the document is not.
2. **Eviction.** `Mesh.nodes` keeps 50 entries, the least recently heard evicted. A flood of 50
   invented keys removes every real heard node from the list, from the `nodes` document and from
   the *Add heard node* select (which offers the 20 most recently heard). A household that wants
   to add Bob's new handheld cannot find it while the flood lasts; the select shows 20 of the
   attacker's names.
3. **Continuous table reads.** `CompanionSession._push` calls `_contacts_changed()` for every
   `codec.Advert`, also when `decoded.new` is true (a node the radio did not add), because
   firmware 1.11 and older announce a contact they have just added with `NEW_ADVERT`
   (`docs/protocol.md` §4). The maintenance task then re-reads the whole contact table
   (`GET_CONTACTS` without `since`) two seconds later. A read costs about 150 bytes per contact:
   52 KB and about 4.6 s over a USB-UART bridge at 115200 baud for 350 contacts
   (`docs/protocol.md` §5). An advert every 3 s keeps the radio listing contacts most of the
   time: every command of the gateway (sends, telemetry requests, path resets) waits behind the
   session lock for the listing to end; over TCP the firmware's 4-frame push queue drops pushes
   during a listing (`protocol.md` §5, "Over TCP pushes can be lost"), so acknowledgements,
   telemetry answers and `MSG_WAITING` are lost while the flood lasts.

**Evidence.** The experiment of SEC-010 (`publishes: 80019 (755427752 B)` for 50,000 adverts,
heard nodes 50). Code: `observer._advert` ends with `self._publish_nodes()` unconditionally for a
`NEW_ADVERT`; `session._push`, lines 678–681. The listing cost is the project's own figure.

**Preconditions and attacker model.** T1, as SEC-010. Effect 3 also follows from a benign busy
mesh: a city-centre node hears dozens of adverts an hour from strangers, each of which re-reads the
table.

**Impact.**

* Bus and Home Assistant: 15 KiB per advert of retained traffic; the broker's retained store is
  rewritten; Home Assistant parses and records each; the recorder grows (one row per change of
  the `nodes` sensor's attributes). At one advert every 3 s that is 430 MB/day of MQTT traffic
  and about 29,000 recorder rows a day.
* The household cannot add real nodes while the flood lasts (A6 usability); the *Look-alike*
  sensor flaps if the attacker chooses prefixes that collide with contacts (the attacker can
  choose any 6-byte prefix that is *not* a key's prefix; colliding with a real contact's prefix
  costs 2^47 work, see RES-003, so the look-alike flag is not the easy target).
* The radio link is saturated by listings: outgoing alerts wait up to a listing's duration for
  the lock on every attempt; telemetry answers over TCP are dropped; the gateway's own watchdog
  is unaffected (the reads are awaited), but `delivery` latency rises by seconds per message.

**Existing mitigations.** The rate limit on `node` events; the 2 s coalescing of re-reads; the
`manage_contacts` discovery document's budget (70 changes an hour), which protects Home Assistant
from the select's option churn; the 15 KiB bound of the document.

**Suggested mitigation or implementation.**

1. **Rate-limit the `nodes` document** like the events: publish it at most every 10–30 s when
   only heard nodes changed (a token bucket, or a timer that coalesces), at once when a contact
   joined or left (the `read=True` case already distinguishes that). The document is a retained
   snapshot; a 30 s delay changes nothing for a user. `_publish_nodes(read=False)` would schedule
   instead of publish.
2. **Do not mark the table dirty on `NEW_ADVERT`** when the firmware is 1.12 or newer (the
   firmware code is known at connect: `info.device.firmware_code`; 1.11 and older are the ones
   that announce a just-added contact this way). A node the radio did not add changes nothing in
   the table. For firmware ≤ 1.11 keep the current behaviour. This removes effect 3 for strangers'
   adverts entirely; a contact's `ADVERT` (which does change the record) still triggers the read.
3. **Incremental reads** (IMP-003): `GET_CONTACTS` takes a `since` timestamp and lists only
   contacts with `lastmod > since` (`MyMesh.cpp:1238-1247, 2397`); the codec already encodes it
   (`codec.get_contacts(since)`), the session never uses it. A re-read after a push could ask for
   records changed since the newest `last_modified` of the last read, costing a few frames instead
   of the table. Care: a removal is not listed (the record is gone), so a full read is still
   needed when a `CONTACT_DELETED` or `CONTACTS_FULL` push came, and ADR 0009's reasoning
   ("every table read lists the keys") relies on full reads for the look-alike memory; incremental
   reads would be for the maintenance re-reads after adverts and paths, with a full read at a
   lower rate (for example every 10 minutes and before any command decision, as now).
4. **Protect the real nodes from eviction**: keep the 50 most recently heard *distinct prefixes*
   is no defence (the attacker chooses prefixes); instead keep two lists, nodes seen more than
   once (a second advert, a message) and nodes seen once, and evict the once-seen first. An
   attacker who re-adverts each invented key twice defeats it at double cost; it still raises the
   bar and keeps a household's real handheld (which adverts repeatedly) in the list.
5. **Say it in the documents**: `docs/design.md` §8 (failure modes) has no row for an advert
   flood; §7.2 has none for floods at all (see `02-threat-model.md` §7).

Tests: `tests/test_observer.py` — 200 `NEW_ADVERT`s within a second publish the `nodes` document
at most N times; `tests/test_session.py` — a `NEW_ADVERT` on firmware code 14 does not set
`contacts_dirty`, on code 9 it does; `tests/test_mesh.py` — a node heard twice survives 50
once-heard strangers.

**References.** `docs/protocol.md` §4–5; MeshCore `examples/companion_radio/MyMesh.cpp:2388-2410`
(one contact per main-loop pass during a listing); `src/helpers/wifi/SerialWifiInterface.h:27`
(the 4-frame queue).

---

### SEC-012 Messages on the public channel are published by default: every neighbour's chatter lands in Home Assistant's recorder and the history

| | |
| --- | --- |
| Category | security (privacy of third parties; denial of service of the recorder) |
| Component | `config.py:110,531` (`ignore_channels` default empty), `bridge.py:808-813,815-860` (`_ignored`, `_on_message`), `observer.py:359-378` (`message` → history), `mqtt/discovery.py:137-142` (the *Message* event entity on `received`); firmware `MyMesh.cpp:1002` (`addChannel("Public", PUBLIC_GROUP_PSK)`) |
| Related | SEC-015, DOC-00x, IMP-004, ADR 0017 (every contact a recipient) |

**Description.** Every companion radio has the channel "Public" in slot 0 with a key that is
public (`PUBLIC_GROUP_PSK`, in the firmware source). Whatever anyone within the mesh's reach
posts there, the radio decrypts and queues; the gateway fetches it, publishes it on `received`
(QoS 1) with the claimed sender name and the text, stores it in the history (`message` table,
up to 10,000 rows for 30 days) and in the retained `history` document (the last 50 messages),
and Home Assistant turns each into a state change of the *Message* event entity with the text as
an attribute, which the recorder keeps for 10 days by default and `core.restore_state` keeps for
the last one. Nothing in the default configuration restricts this: `ignore_channels` is empty,
the installer does not ask, and the guide's first-steps card shows "the last mesh message",
which in a lively mesh is a stranger's.

The household did not ask for its neighbours' conversations, and the neighbours did not consent
to being recorded in someone's home automation database. Channels with a hashtag name are the
same (their key is derived from the name).

**Evidence.** `config.py:531`: `ignore_channels=frozenset(s.integers("ignore_channels", 0, 255))`
defaults to empty; `bridge._on_message` publishes every message that is not ignored and not an
exact repeat; `observer.message` records every published message; the discovery document's
*Message* event has `event_types: ["contact", "channel"]`. Firmware: `MyMesh.cpp:1002`.

**Preconditions and attacker model.** T1 for the denial-of-service side (post on the public
channel at the channel's capacity, about one message every 1–2 s at the default preset: each
becomes a recorder row and a history row). No attacker is needed for the privacy side: a busy
public channel does it by itself.

**Impact.**

* Privacy (A4, third parties): texts and names of people outside the household are stored in
  three places in the home for 10–30 days and are visible to every Home Assistant user.
* Recorder growth: at one message every 2 s, 43,000 state changes a day of the *Message* event,
  each with the text and the claimed name as attributes. Home Assistant's recorder handles it,
  but the database grows by tens of MB a day, and the *Last mesh message* card shows the spam.
* The `history` retained document (up to 15 KiB) is republished after every message: the same
  amplification as SEC-011, smaller.
* Nothing acts on channel messages (commands come only from direct messages), so A1 is not
  affected.

**Existing mitigations.** `ignore_channels` exists and is documented ("busy public channels");
the *Privacy* section of `docs/home-assistant.md` shows how to exclude the message entities from
the recorder; the exact-repeat memory drops retries.

**Suggested mitigation or implementation.**

1. **Change the default to publish only configured channels.** A setting
   `[messages] publish_channels = "targets"` (default) | `"all"`: with `targets`, only channels
   that are a `[targets.*]` entry are published (the household chose them); with `all`, the current
   behaviour. `ignore_channels` stays as a further filter. This is a changed default, which the
   compatibility policy allows as a *Changed* entry (a message that used to be published is not:
   the policy's "a document no longer published" is about documents, but the team should decide
   whether a changed filter default is breaking for automations that trigger on `channel` events
   from the public channel; a one-release warning in the log when the public channel is heard and
   not configured would ease it).
2. **Or ask in the installer**: the installer already lists the radio's channels to pick
   targets; a checklist "publish messages of these channels" would set `ignore_channels` to the
   rest.
3. **Rate-limit `received` for channel messages** per channel (a token bucket of, say, 1/s with a
   burst of 20): a spammer cannot fill the recorder faster than that; direct messages are not
   limited (a contact is already a trusted party, and SEC-013 covers that case).
4. **Keep the history's `message` table free of ignored channels** (it already is: ignored
   messages are neither published nor recorded) and consider a separate cap for channel messages
   in the history, so that public chatter cannot push the household's own messages out of the
   10,000 rows.
5. **Documentation**: state in `docs/home-assistant.md` *Privacy* and in `docs/design.md` §7.4
   that the default records third parties' public-channel texts, and how to stop it; mention the
   public channel by name in `docs/configuration.md` under `ignore_channels`.

Tests: `tests/test_bridge.py` — with `publish_channels = "targets"` a message on channel 0 that
is no target is neither published nor recorded; a message on a target channel is; the warning
line appears once.

**References.** MeshCore `examples/companion_radio/MyMesh.cpp:1002`; Home Assistant recorder
documentation (`recorder.exclude`, `purge_keep_days` default 10).

---

### SEC-013 A contact can starve the radio's own duty cycle with direct messages the radio must acknowledge

| | |
| --- | --- |
| Category | security (denial of service of the alert path) |
| Component | firmware `src/helpers/BaseChatMesh.cpp:240-257` (every plain direct message from a contact is acknowledged), `src/Dispatcher.cpp:19-53` (the airtime factor delays the next transmission); gateway `core/budget.py` (counts only the gateway's own sends), `radio/profile.py` (sets the factor to 9 in the 10 % band) |
| Related | SEC-012, ADR 0008 (the budget does not see the radio's own transmissions), ADR 0020 |

**Description.** The companion firmware acknowledges every plain direct message from a contact
with a transmitted packet (`sendAckTo`, or a path return if the message was flooded). The radio's
own transmit limiter, the airtime factor, makes it wait `factor × time on air` after each
transmission before it sends the next (`Dispatcher.cpp:19-53`; the managed profile sets the factor
to 9 in the 869.4–869.65 MHz band, a 10 % duty cycle). A contact of the radio (any type: the
firmware decrypts direct messages of repeaters, room servers and sensors too, `protocol.md` §5)
that sends direct messages back to back makes the radio spend its budget on acknowledgements: at
the EU/UK Narrow preset an acknowledgement packet takes about 0.3 s, so the radio then waits
2.7 s; a contact sending a message every 3 s keeps the radio at its duty cycle with
acknowledgements alone, and every message the gateway wants to send queues behind them in the
radio's dispatcher. The gateway's own budget (`[airtime]`) counts none of this (ADR 0008 says so),
so its `airtime_available_s` says there is room while the radio has none. Each message also
becomes a `received` event, a history row, a recorder row, and (if the contact has a device) a
*Message* event on its device.

**Evidence.** Firmware source: `BaseChatMesh.cpp:222-257` (`onPeerDataRecv`: for `TXT_TYPE_PLAIN`
it calls `onMessageRecv` and then `sendAckTo` or `createPathReturn`); `Dispatcher.cpp:19-53`
(`getNextTransmission` honours `airtime_factor`); the project's `docs/protocol.md` §7 ("The
firmware has its own transmit budget, `airtime_factor`"). Not measured on hardware by the
reviewer.

**Preconditions and attacker model.** T3: a contact of the radio. Being a contact is cheap in the
default configuration: `[contacts] manage = true` and any Home Assistant user or broker client
with the ACL can add any heard node (SEC-008); a neighbour once added as a courtesy, or a lost
handheld, suffices. A stranger (T1) cannot: direct messages from non-contacts are dropped
undecrypted and not acknowledged.

**Impact.** Outgoing alerts are delayed by the radio's limiter for as long as the contact keeps
sending; the gateway reports `sent` when the radio accepted the message (`SENT` means queued,
`protocol.md` §2), so `delivery` does not show the delay, and the acknowledgement wait
(5–60 s) may expire before the radio even transmits, which the gateway counts as a failed attempt
and retries, spending its own budget. Direct messages from the household compete with the
attacker's in the radio's 16-slot inbound queue when the gateway is disconnected. The recorder and
the history fill with the contact's messages (10,000 rows cap).

**Existing mitigations.** A contact can be removed (any user, SEC-008 again) and the radio then
drops its messages; the `received` rate is not limited for direct messages; the radio's queue
limit protects the firmware.

**Suggested mitigation or implementation.**

1. **See the radio's state.** The gateway reads `GET_STATS` core type 0 every 60 s, which carries
   `queue_length` (packets waiting to be sent) and the error flag `queue_full` (`protocol.md` §3);
   `stats.queue_length` is published. The outbox could consult it: when the radio reports a
   backlog, the gateway's acknowledgement wait should start when the radio has transmitted, not
   when it accepted the frame. The firmware does not push "transmitted", but the heartbeat could
   be read more often while a message is in flight, or the wait extended by
   `queue_length × (time on air × (1 + factor))`.
2. **Rate-limit `received` per sender** (a token bucket per key, for example 1 message per 2 s,
   burst 10): beyond it the message is still handled (a command may be in it) but the event and
   the history row are dropped with a counter in the log. This protects the bus and the
   recorder; it cannot protect the radio's duty cycle, which the firmware spends before the
   gateway sees the message.
3. **Document** it as a residual risk of ADR 0017 ("every contact is a recipient" also means
   "every contact is a sender the radio answers") and in design §7.4; recommend keeping the
   table to the household and removing a contact that floods.
4. **Upstream**: a per-contact "do not acknowledge" or a rate limit on acknowledgements in the
   firmware would close it; out of the gateway's hands.

Tests: `tests/test_bridge.py` with the emulator — a contact sending 50 messages in 10 s produces
at most N `received` events and a warning with the count.

**References.** MeshCore `src/Dispatcher.cpp:19-53`; ERC Recommendation 70-03 Annex 1 (the 10 %
sub-band the profile assumes).

---

### SEC-015 Bystanders' data (names, keys, signal, raw packets) is collected, retained and recorded without their knowledge

| | |
| --- | --- |
| Category | security (privacy of third parties; data protection) |
| Component | `views.nodes_document` (`nodes`, retained), `observer._advert` (`node` events, the `node` table), `bridge._event_loop` (`rx` when `publish_rx_log`), `mqtt/discovery.py` (*Nodes heard*, *Look-alike node heard*, *New node* in Home Assistant), `history.py` (the `node` table, 30 days) |
| Related | SEC-010, SEC-012, SEC-016, ARC-00x, RES-006 |

**Description.** The observer's purpose (ADR 0014) includes "nodes heard but not added". Every
advert in range yields the node's public key, its self-chosen name, its type, when and how often it
was heard, and the signal strength and path with which it reached the gateway. This is published
retained on `nodes` (50 nodes), as `node` events ("a node heard for the first time"), as entities
in Home Assistant (count, look-alike list with keys, the *New node* event with name and key, all
recorded), and stored for 30 days in the history. With `publish_rx_log` the gateway additionally
publishes every packet it hears, including other people's ciphertext and adverts with positions.
ADR 0016 deliberately stores no *positions* of strangers ("privacy"), but it stores their names,
keys and radio fingerprint, and the `rx` log stores their positions when it is on.

For the household this is the feature. For the people around the house it is a log of their
presence near it, kept in a database they do not know exists, by a device that advertises
nothing about itself (the observer profile). Under data-protection regimes (GDPR, where the
household exception for purely personal activity usually applies but not if the data is shared,
for example by posting a dashboard), keys are identifiers and names may be personal data.

**Evidence.** `views.nodes_document` (keys, prefixes, names, first and last heard, hops, SNR,
RSSI); `history._SCHEMA` (`node` table with `rssi_dbm`, `snr_db`, `hops`); `discovery.py`
`_mesh_components` (`nodes`, `look_alike`, `node`); `rx_payload` (raw hex).

**Preconditions and attacker model.** None required; the collection is the default. An attacker
model applies to the readers: T6 and T7 see the neighbourhood's radios.

**Impact.** Reputational and legal exposure for the household and the project if the data is
shared or leaked (a dashboard screenshot with a neighbour's name and key; the `history.sqlite3` in a
backup that leaks). No impact on the gateway's own security.

**Existing mitigations.** Positions of non-contacts are never stored (ADR 0016); `rx` is
opt-in and documented as diagnostics; the heard-node list is bounded; the retention is 30 days;
the *New node* event carries no position.

**Suggested mitigation or implementation.**

1. **Make the heard-node features opt-in or minimise them**: a setting
   `[contacts] heard_nodes = "full" | "keys" | "off"` where `keys` publishes the `nodes` document
   with keys and types but without names and signal (the select then shows digits only, which is
   what the household compares anyway), and `off` keeps nothing of strangers. The default could
   stay `full` with a documented privacy note, or move to `keys`.
2. **Shorter retention for strangers**: `nodes_days = 7` as ADR 0016 intended (DOC-004), and
   delete a node's row when it is evicted from the in-memory list (SEC-010 option 3).
3. **Hash or truncate the signal data** in the history for non-contacts: nothing in the gateway
   uses a stranger's RSSI after the fact.
4. **A privacy section in the README and `docs/design.md`** that names what is collected about
   third parties, where it is kept, for how long, and how to turn each part off; and the advice
   not to share dashboards or the history file. The `docs/home-assistant.md` *Privacy* section
   covers the household's own data well; third parties are not mentioned.
5. **`rx` log**: document that it records bystanders' positions (adverts with the position flag)
   and ciphertext; consider redacting the advert's position bytes from `raw` unless a second
   setting allows it.

**References.** ADR 0014, ADR 0016; GDPR Art. 2(2)(c) (household exemption) and Recital 18; the
EDPB's guidance on "purely personal or household activity" (data shared beyond the household is
not covered).

---

### SEC-016 Telemetry is opt-out per contact: a newly added contact is asked within a minute, and consent is inferred from the companion's settings

| | |
| --- | --- |
| Category | security (privacy; design) |
| Component | `telemetry_poller.py` (candidates = every chat contact not excluded), `observer.on_contact_added` → `poller.contact_added` (due at once), `config.py:140` (`telemetry.enabled = True`), `state.TelemetryExclusions` (the opt-out list), ADR 0018, CHANGELOG 1.1.0a2 ("on by default") |
| Related | SEC-008, SEC-015, ARC-00x, IMP-005 |

**Description.** The gateway asks every chat contact of the radio for its telemetry unless the
household excluded it. A companion answers only if its owner allowed telemetry requests from the
gateway (or from everyone), so the project's position (design §7.4, `docs/companions.md`
*Privacy*) is that the companion's settings are the consent, and the exclusion list exists for
companions that "allow all". Two things weaken that:

1. **Timing.** After the first-start hour, a contact added through any of the paths (the select,
   the text, `add_contact`, the CLI) is due at once and asked within a minute
   (`poller.contact_added`; `docs/companions.md` step 3 says "best do it before you add it"). The
   household member who adds a neighbour's node so that they can exchange messages has, without
   knowing, started polling that neighbour's battery, sensors and position every 15 minutes, if
   the neighbour's companion answers everyone. The documentation tells the operator to exclude
   first; the Home Assistant select has no way to.
2. **Consent inferred from a device setting of another person.** A companion set to "Allow All"
   by its owner for convenience in a club or a family does not mean its owner consents to being
   tracked by every gateway whose radio they add as a contact. The gateway cannot tell (it only
   sees that answers come) and the design says so; but the default makes the gateway the party
   that has to be stopped rather than the party that has to be enabled.

**Evidence.** `core/telemetry.py` candidate selection (every chat contact not excluded with a
unique prefix); `telemetry_poller.contact_added` marks the contact due; `tests/test_telemetry_poller.py::test_the_advert_of_a_node_added_later_counts`
and related tests; CHANGELOG 1.1.0a2 *Changed*: "`[telemetry] enabled` is `true` by default: the
gateway asks every companion of its radio that is not excluded".

**Preconditions and attacker model.** None for the privacy side. An attacker model for the
reverse: T6/T7 can *include* an excluded contact again (any Home Assistant user who may switch
entities; the broker's ACL on `contacts/+/telemetry`), which restarts the tracking of a person who
was deliberately excluded.

**Impact.** The household tracks people it did not mean to (their positions go to the broker,
Home Assistant's map and recorder, the history), for as long as nobody notices the new tracker
on the map. The gateway's own transmissions grow with every contact asked (within the lanes).

**Existing mitigations.** The first-start hour; the exclusion list that survives removals and
restarts; the `telemetry` events and *Telemetry state* that make polling visible; the warnings
in the log; `docs/companions.md`.

**Suggested mitigation or implementation.** Make telemetry **opt-in per contact** rather than
opt-out, while keeping the current behaviour available:

1. A setting `[telemetry] ask = "household" | "all"` (default `household`): with `household`,
   only contacts the household marked are asked. The mark would be the natural inverse of the
   exclusion: `contacts/<key>/telemetry` `on` marks a contact as household (persisted in a
   `telemetry-included.json`, or the existing file with a mode field), the switch *Telemetry* is
   off by default for a new contact, and the first-start hour becomes unnecessary (nobody is
   asked until somebody says so). With `all`, today's behaviour. The compatibility policy allows
   a changed default with a *Changed* entry; the team may prefer to keep `all` the default for
   installations that already run and switch new installations to `household` through the
   installer (which could ask "which of these contacts are the household's?" where it already
   offers the contacts as targets).
2. **Protected contacts and contact targets could be household by default** (the household
   configured them), which gives a sensible out-of-the-box set.
3. **The add paths could carry the intent**: `add_contact` with `{"contact": "…", "household": true}`
   and a second select or a confirmation in Home Assistant are more work; the switch on the new
   device (off by default) is the simplest UI.
4. **Document the current default honestly** in the README's feature list ("only from those who
   allow the gateway" is true of the companion's side; "every contact is asked unless excluded"
   is the gateway's side).

Tests: `tests/test_telemetry_poller.py` — with `ask = "household"` a contact added is not a
candidate until included; a protected contact is.

**References.** ADR 0018 and ADR 0024; `docs/companions.md`; GDPR Art. 5(1)(c) (data
minimisation) and Art. 25 (data protection by default) for the principle, whether or not it
applies to a household.

---

### SEC-017 The telemetry answer, the message and the add-contact paths accept names and texts without limits beyond the protocol's

| | |
| --- | --- |
| Category | security (input validation; low) |
| Component | `protocol/codec._cstring` (names: 32 bytes), `codec.decode_message` (texts ≤ 176 bytes), `core/lpp.decode` (total), `protocol/links.parse_contact_text` (links ≤ 1 KiB), `core/text.contact_name` (31 bytes, cleaned), `core/contacts.node_label` (cleaned) |
| Related | SEC-007, CODE-00x |

**Description.** The reviewer looked for places where mesh- or broker-controlled strings reach a
parser, a template, a file name, a shell, SQL, a log line or a terminal without a bound or an
escape. The result is **mostly reassuring** and is recorded here so that the team knows what was
checked:

* Names from the radio are NUL-terminated within 32 bytes, decoded with replacement; names
  written to the radio go through `contact_name` (control and direction characters dropped, 31
  bytes, cut between graphemes). Labels for the select drop control, format and separator
  characters (`node_label`).
* Message texts are at most 176 − header bytes, decoded with replacement; they are never cleaned
  on the way in (by design: data), but every consumer the project ships escapes them: JSON
  encodes control characters, the CLI's `_safe_text` and `_json_text` replace terminal-steering
  characters, log lines use `%r`, the dashboard's macro `m()` escapes and joins lines, the guide's
  card does the same.
* SQL uses parameters throughout (`history.py`); there is no string-built SQL.
* No shell is ever invoked by the service; the installer passes values as arguments and
  validates them with regular expressions before they reach a command line, and the only
  interpolations into `bash -c` strings are constants (`$STAGE`, `$LIB`, `$CONFIG`, `$ANSWERS_FILE`)
  and the validated `$SOURCE`.
* Jinja templates in discovery documents are written by the gateway from constants; data from
  the mesh is only ever the *value* a template reads, never the template.
* JSON envelopes are parsed with `json.loads` on at most 1 KiB (`RecursionError` caught), the
  `id` is restricted by a regular expression, and only known keys are read.
* CayenneLPP decoding is total and bounded by the frame.
* The TOML configuration is root's; the installer renders it through `_toml_string`, which
  escapes quotes, backslashes and control characters (a `\u2028` or a non-BMP character is
  written raw, which TOML allows).

What remains:

1. **Message texts are stored and published with control characters intact.** A text of
   `"\x1b[2J"` reaches `received`, the history and Home Assistant's attributes as is. Every known
   consumer escapes it, but a future consumer (a terminal dashboard, an export to CSV) may not.
   An `[messages] clean_received = true` option (apply `core.text._clean` to inbound texts, as to
   outbound ones) would be a defensive default at the cost of altering data; the documents promise
   "texts from the mesh are plain data", so the team may prefer to keep them raw and document
   the duty of consumers, which `docs/mqtt-api.md` does.
2. **Names in the `nodes` and `contacts` documents are raw** (the 32-byte NUL-terminated field as
   decoded). A name of 31 zero-width joiners or a bidirectional override reaches the broker and
   Home Assistant's device name. Home Assistant renders names as text, so the effect is visual
   only (SEC-007 treats the impersonation angle).
3. **The `delivery` event's `target` for a rejected request is the alias from the topic**, which
   can be any string without `/` (`alias_from_send_topic`): it is logged with `%r` and published
   as JSON. A 1 KiB topic level is possible; the event carries it. Harmless, but the event's size
   is then controlled by the sender; the refusal rate limit bounds the count.

**Evidence.** Reading of the modules named; `tests/test_cli.py` has tests for `_safe_text`;
`home-assistant/tests/test_dashboard.py` renders hostile names and texts; `tests/test_text.py`
covers cleaning.

**Preconditions and attacker model.** T1 (names and texts from the air), T6 (topic levels and
payloads on the bus).

**Impact.** None found that crosses a boundary. Listed for completeness and as the baseline the
team can keep when adding consumers.

**Existing mitigations.** As listed.

**Suggested mitigation or implementation.** A short "handling mesh text" section in
`docs/development.md` (the rules the code follows: `%r` in logs, `_safe_text` on terminals,
`m()` in cards, JSON only on the bus) so that new code keeps them; optionally the `clean_received`
setting; a bound on the alias echoed in a rejected `delivery` event (truncate to 64 characters).

**References.** none.
