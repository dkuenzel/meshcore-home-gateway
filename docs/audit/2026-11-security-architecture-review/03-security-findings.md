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

---

## B2 The radio link and the operator's clock

### SEC-002 `set_clock` moves the radio's clock forward irreversibly from a wrong host clock

| | |
| --- | --- |
| Category | security (integrity and availability of the radio's identity; operator safety) |
| Component | `src/meshcore_home_gateway/radio/session.py:587-597` (`_sync_clock`), `config.py:67` (`set_clock = True` default); firmware `examples/companion_radio/MyMesh.cpp` (`CMD_SET_DEVICE_TIME`: `if (secs >= curr) setCurrentTime(secs) else ERR 6`), `src/helpers/BaseChatMesh.cpp:60-70` (`bootstrapRTCfromContacts`), `src/helpers/AutoDiscoverRTCClock.cpp:85-101` (hardware RTC chips), `src/Mesh.cpp:404-420` (`createAdvert` stamps the radio's clock), `BaseChatMesh.cpp:131-136` (adverts not newer than the stored timestamp are dropped) |
| Related | design §6.1 "Clock", `docs/deployment.md` troubleshooting rows `too_old` / `from_the_future`, DOC-002, IMP-006, RES-007 |

**Description.** At every connect, with the default `set_clock = true`, the session reads the
radio's clock and, if the host is more than 2 s ahead, writes the host's time to the radio
(`_sync_clock`). The firmware accepts only a time that is not earlier than its current time
(`CMD_SET_DEVICE_TIME`: `secs >= curr`, else `ERR_CODE_ILLEGAL_ARG`); nothing in the companion
protocol moves the clock back. The gateway applies no plausibility bound: whatever the host says,
if it is later than the radio's time, is written.

A wrong-forward time on the radio is **sticky**:

* On every board the radio writes `lastmod` of a contact with its clock whenever a message or
  advert from that contact arrives (`BaseChatMesh.cpp:119,198,240,294,357`), the contacts file is
  saved, and at the next boot `bootstrapRTCfromContacts()` sets the clock to the newest `lastmod`
  it finds (`BaseChatMesh.cpp:60-70`, called from `MyMesh.cpp:1001` at start). So the wrong time
  survives a reboot even on a board whose clock is a RAM counter.
* On boards with a hardware RTC (DS3231, RV3028, PCF8563, RX8130: `AutoDiscoverRTCClock.cpp`)
  the time is written to the chip and survives power loss.
* The only ways back are a factory reset (which gives the radio a new identity), re-flashing with
  the file system erased, or waiting for real time to catch up.

What a wrong-forward radio clock does:

1. **Adverts.** Every advert the radio sends is stamped with its clock (`Mesh.cpp:404-420`). Every
   node that holds the gateway's radio as a contact stores that timestamp and **drops every later
   advert of the gateway whose timestamp is not newer** (`BaseChatMesh.cpp:131-136`, the replay
   protection). The gateway never sends adverts itself, but the radio's button, the app on it
   before the gateway took over, or a future version sends one; once one advert with a far-future
   time has gone out, the gateway's radio cannot update its name, position or path hints in any
   contact's table until the real clock passes that time. For a radio set to 2099 that is for ever.
2. **Contact ordering.** `lastmod` decides which contact a full table overwrites (the oldest
   non-favourite, `protocol.md` §5). Contacts touched under the wrong clock are "newest" for
   years.
3. **Message timestamps on the radio side are not affected** (the gateway stamps outgoing
   messages with its own clock, `bridge.py:602`) but the gateway's own clock is the one that is
   wrong in this scenario, so every message it sends carries the wrong time, which the recipient's
   app shows, and every incoming command is judged against the wrong clock (`too_old` or
   `from_the_future`, depending on the direction): the troubleshooting table attributes this to
   the radio's clock, which is not involved (DOC-002).

How a host clock goes wrong-forward: a manual `date` with a typo (2062 for 2026); an NTP server
that answers wrongly or is spoofed on the LAN (NTP is unauthenticated; `chrony`'s default
`makestep 1 3` steps the clock in the first three updates after boot by any amount); a Proxmox
host whose CMOS battery died and which boots without network starts at its BIOS default, which on
some boards is in the future. A restored VM or container snapshot and a Raspberry-Pi-class host
without RTC go the other way (past), which is harmless here. None of these is likely on a
well-kept Proxmox host; the point is that one occurrence is unrecoverable and silent: the log says
"radio clock moved forward by N s" at info level and nothing else.

**Evidence.** `session.py:587-597`:

```python
async def _sync_clock(self) -> None:
    radio_time = await self.get_time()
    now = int(self._wall())
    if now - radio_time > CLOCK_SET_THRESHOLD_S:
        await self.set_time(now)
        log.info("radio clock moved forward by %d s", now - radio_time)
    elif radio_time - now > CLOCK_AHEAD_WARNING_S:
        log.warning("radio clock is %d s ahead and cannot be set back; command age checks may fail", ...)
```

Firmware citations above, read at commit `81dbae5`.

**Preconditions and attacker model.** T12 (the operator, by mistake) for the typical case; T5-like
(a host on the LAN that spoofs NTP answers to the Proxmox host at boot) for an adversarial one.
No access to the gateway or the radio is needed; the gateway does the write.

**Impact.** The radio's identity becomes partly unusable on the mesh (its adverts are rejected by
everyone who holds it) for as long as the wrong time is ahead of real time; recovery destroys the
identity (factory reset) or needs a re-flash. Until the host clock is fixed, every command is
rejected and every outgoing message carries a wrong time. The failure is silent at the default log
level and invisible in Home Assistant (no entity shows the radio's clock or the offset).

**Existing mitigations.** The 2 s threshold; the warning when the radio is *ahead*; the
`set_clock = false` setting; the forward-only rule of the firmware (which prevents a wrong-*past*
host from harming the radio).

**Suggested mitigation or implementation.**

1. **Plausibility bounds before writing.** Write only if (a) the host's time is at or after the
   gateway's own release date (a constant written at release, for example `BUILT_AFTER`; the
   release date is in the changelog) and before that date plus, say, 10 years; and (b) the jump
   the write would cause is either from an obviously unset radio clock (the radio's time is before
   the release date, as after a cold boot of a RAM-clock board) or smaller than a bound (an hour).
   A jump larger than the bound from a set clock is logged as a warning and **not written**; the
   operator can force it with `set_clock = "always"` (a new choice; `true` keeps the bounded
   behaviour, `false` as now). Sketch:

   ```python
   if now < BUILT_AFTER or now > BUILT_AFTER + 10 * 365 * 86_400:
       log.warning("host clock %s is implausible: the radio's clock is left alone", iso(now))
       return
   unset = radio_time < BUILT_AFTER
   ahead = now - radio_time
   if ahead > CLOCK_SET_THRESHOLD_S and (unset or ahead <= MAX_JUMP_S or self._set_clock == "always"):
       await self.set_time(now)
   elif ahead > MAX_JUMP_S:
       log.warning("host clock is %d s ahead of the radio's: not written "
                   "(set_clock = \"always\" forces it)", ahead)
   ```

2. **Show the clocks.** Add `radio_clock_offset_s` (radio minus host, at the last read) to the
   `state` document and a diagnostic sensor; read the radio's time at every heartbeat or every
   few minutes, not only at connect. An offset that grows or a large one is then visible.
3. **Document the recovery** in `docs/deployment.md` troubleshooting: how to tell (the offset,
   contacts not seeing the gateway's adverts), that only a factory reset or waiting helps, and
   that the host's clock must be right before the service starts (`After=time-sync.target` in the
   unit is cheap; with `systemd-timesyncd` or `chrony` the target is reached after the first
   synchronisation. In an LXC container the clock is the host's, so the unit's dependency helps
   only on other systems; the documentation should name the host's NTP).
4. **Correct DOC-002**: the troubleshooting rows and design §6.1 that attribute command
   rejections to the radio's clock.

Tests: `tests/test_session.py` — a host time of 2062 does not write the radio's clock and logs a
warning; a radio at 1970 with a host at 2026 is written; a radio 2 days behind a host is written
only with `always`; `tests/test_bridge.py` — `state.radio_clock_offset_s` follows the emulator.

**References.** MeshCore `examples/companion_radio/MyMesh.cpp` (`CMD_SET_DEVICE_TIME`),
`src/helpers/BaseChatMesh.cpp:60-70`; `systemd.special(7)` `time-sync.target`; `chrony.conf(5)`
`makestep`.

---

### SEC-003 Over TCP the radio's private key is exportable and importable by anyone on the network (default build), and the gateway cannot tell a second client

| | |
| --- | --- |
| Category | security (confidentiality of the identity, integrity of the radio, availability) |
| Component | `src/meshcore_home_gateway/radio/transport.py:139-181` (`TcpTransport`), `config.py:63-66`, `bridge.py:419-426` (the warning for commands over TCP); firmware `examples/companion_radio/MyMesh.cpp:1528-1556` (`CMD_EXPORT_PRIVATE_KEY`, `CMD_IMPORT_PRIVATE_KEY`), `platformio.ini:33-34` at `companion-v1.18.0` (`ENABLE_PRIVATE_KEY_IMPORT=1`, `ENABLE_PRIVATE_KEY_EXPORT=1` in the common build flags, "NOTE: comment these out for more secure firmware"), `src/helpers/wifi/SerialWifiInterface.cpp:57-70` (a new connection replaces the old) |
| Related | design §7.2 "Unauthenticated TCP companion interface", §7.4; SEC-001; RES-008; OPS-00x; DOC-00x |

**Description.** The project documents the TCP companion interface as unauthenticated and says
"any client controls the radio, and can for example create temporary contacts that the contact
list never shows". The reviewer verified the stronger consequences in the pinned firmware and its
build configuration:

1. **The private key is exportable.** `CMD_EXPORT_PRIVATE_KEY` (23) returns the 64-byte secret
   when the firmware was built with `ENABLE_PRIVATE_KEY_EXPORT`; the common `build_flags` of
   `platformio.ini` at the pinned release set it for every environment unless a variant removes
   it (the reviewer found none that does; two variants set it again explicitly). The official
   release binaries are built from this file. So any host that can open TCP port 5000 of a WiFi
   companion obtains the radio's Ed25519 private key with one 1-byte frame.
2. **The private key is importable** (`CMD_IMPORT_PRIVATE_KEY`, 24, same flag family): the
   attacker can replace the radio's identity, or install an identity they hold elsewhere.
3. **Destructive and dangerous commands** need no more: factory reset (51), radio parameters (11:
   frequency, bandwidth, spreading factor, coding rate — move the radio off the mesh's channel),
   raw packet transmission (25, 65: use the radio to transmit anything, including SEC-001's
   forged acknowledgements and adverts under the exported key), `ADD_UPDATE_CONTACT` with the
   remote-CLI flag (then control the radio over the air later), `SYNC_NEXT_MESSAGE` (steal
   inbound messages, commands included, before the gateway sees them: the firmware hands a message
   to whichever client asks first).
4. **No forward secrecy.** MeshCore direct messages are encrypted with a static shared secret
   derived from the two parties' long-term keys (X25519 of the identity keys, `src/Identity.cpp`,
   `ContactInfo::getSharedSecret`). With the exported key, every direct message ever recorded from
   the air between the gateway's radio and any contact can be decrypted afterwards, and every
   future one. The commands the household sends (A1), the alerts the gateway sends (A2), the
   telemetry answers (A3: positions) are all direct messages.
5. **The gateway cannot see a second client.** Over TCP the firmware serves one connection and
   replaces it with a new one (`SerialWifiInterface.cpp:57-70`); the gateway's connection drops
   with "closed by the radio" and it reconnects after a second, replacing the attacker, who
   reconnects in turn. The gateway logs reconnects as link trouble. The firmware's `uptime_s`
   (in `stats`) does not reset, which could distinguish a reconnect caused by another client
   from a radio reboot, but the gateway does not look.

**Evidence.** Firmware source at commit `81dbae5`: `MyMesh.cpp:1528-1536` (export under
`#if ENABLE_PRIVATE_KEY_EXPORT`, else `writeDisabledFrame()`); `platformio.ini:33-34` fetched from
the tag `companion-v1.18.0` (sparse checkout; `grep -n ENABLE_PRIVATE_KEY platformio.ini` →
lines 33–34 in `[env]`'s `build_flags`). Gateway: `TcpTransport` opens a plain TCP stream
(`asyncio.open_connection`), no TLS, no shared secret; `docs/protocol.md` §1 "TCP … no
authentication".

**Preconditions and attacker model.** T5: any host on the network the radio joins (the home
WiFi, typically, where phones, TVs and IoT devices live), or any host that can route to it. No
credential, no physical access, one TCP connection. The gateway's own `radio.host` setting tells
the operator which address that is; an attacker finds it with a port scan (5000/tcp, a plain
protocol with a distinctive `>` framing).

**Impact.** Complete compromise of the radio's identity (A5) and settings (A6), decryption of all
past and future direct traffic of the gateway (A1–A4), the ability to transmit anything under the
gateway's identity, and theft of inbound messages (commands never reach the gateway). The project
recommends USB and documents the risk; the specific facts (key export in the default build, no
forward secrecy) make the difference between "an attacker on the WiFi can confuse the gateway"
and "an attacker on the WiFi owns the gateway's identity for good and reads its past".

**Existing mitigations.** `docs/design.md` §7.2 and §7.4, `SECURITY.md`, `docs/configuration.md`
(`host`: "the TCP interface has no authentication"), the warning at connect when commands are
configured over TCP (`bridge._check_radio`), the recommendation of an isolated network, the
telemetry poller's TCP allowances. The gateway itself never sends commands 23, 24, 51, 11, 25, 65
(a codec test fails if an encoder appears, ADR 0020).

**Suggested mitigation or implementation.**

1. **Say it exactly** (`docs/design.md` §7.2 row "Unauthenticated TCP companion interface",
   `SECURITY.md`, `docs/configuration.md` `[radio] host`): the release firmware exports and
   imports the private key over this interface; there is no forward secrecy; recommend a custom
   build without `ENABLE_PRIVATE_KEY_EXPORT`/`IMPORT` for a TCP radio (the firmware's own
   comment suggests it), a dedicated VLAN or a point-to-point WiFi that only the gateway's host
   reaches, and never the household's WiFi.
2. **Refuse the risky combination unless acknowledged**: with `transport = "tcp"` and
   `commands.senders` non-empty (or telemetry on, which sends the household's positions over the
   same link), require `[radio] tcp_insecure_acknowledged = true` in the configuration, with the
   error text explaining why (a configuration error, exit 78). A changed rule between settings is
   breaking under the compatibility policy, so this would be a 2.0 item or a warning until then;
   the warning already exists for commands and could be extended to telemetry and made louder
   (also in `check-config`'s summary, which today prints `radio tcp host:port` without a word).
3. **Detect the second client.** Over TCP, after a reconnect, compare the radio's `uptime_s` with
   the value before the disconnect plus the elapsed time: a continuous uptime with a dropped
   connection means another client (or a network blip, so count repeats). Log a warning naming the
   possibility, and publish it in `state` (a `radio_link_contested` boolean) so that Home
   Assistant can alert. Cheap and useful.
4. **Make the attack visible after the fact**: the radio's `GET_STATS` and its contact table
   show nothing about key export. There is nothing the gateway can do about an exported key; the
   documentation should tell the operator what to do on suspicion (factory reset, re-add the
   gateway on every companion).
5. **Longer term**: an encrypted and authenticated companion transport (a pre-shared key over
   TCP, or TLS with the radio's identity) is a firmware feature to request upstream; until it
   exists the gateway's recommendation of USB is the right one, and the installer's TCP option
   could carry the same warning box as the documentation.

Tests: `tests/test_config.py` — the acknowledged-flag rule; `tests/test_bridge.py` — a reconnect
with continuous uptime sets the contested flag and warns.

**References.** MeshCore `platformio.ini` at `companion-v1.18.0`; `docs/companion_protocol.md` of
the firmware (commands 23 and 24); `src/helpers/ContactInfo.h` (`getSharedSecret`);
`src/Identity.cpp` (the ECDH); the project's `docs/protocol.md` §1.

---

### SEC-014 Malformed frames over TCP produce one warning each, without a limit

| | |
| --- | --- |
| Category | security (denial of service of the journal; low) |
| Component | `radio/session.py:666-671` (`_push`: `log.warning("radio: malformed push ignored: %s", exc)`), `session.py:663-664` (`unexpected frame … dropped` at debug), `session.py:759-768` (`_decode`: a malformed *answer* ends the session) |
| Related | SEC-003, CODE-00x, `bridge._report_unpublished` (the pattern to reuse) |

**Description.** A push frame that fails to decode is logged with a warning and ignored; the
session continues. Over TCP an impostor (SEC-003) or a misbehaving radio can send such frames at
the link's rate: thousands per second, each a journal line. `journald` rate-limits a unit to
10,000 lines per 30 s by default (`RateLimitBurst`), after which it drops lines and logs that it
did, so the journal does not grow without bound, but 10,000 lines per 30 s is about 1 MB per 30 s
of disk and CPU for `journald`, and the gateway's own useful lines are among the dropped ones.
Over USB the serial rate bounds it to a few hundred frames per second at most.

**Evidence.** `session._push`, lines 666–671.

**Preconditions and attacker model.** T5 (an impostor over TCP), or a faulty radio.

**Impact.** Journal noise and disk use; loss of the gateway's real log lines during the flood.
Nothing else: the frames are dropped.

**Existing mitigations.** `journald`'s own limit; the `debug` level of the "unexpected frame"
line.

**Suggested mitigation or implementation.** Count malformed pushes and log at most one warning a
minute with the count, as `bridge._report_unpublished` and `contact_manager.report_unpublished`
already do for refusal events; the first occurrence can stay immediate. Also consider ending the
session after N malformed pushes in a row (the radio is out of step or not a radio), which the
reconnect then resynchronises, as `_decode` does for answers.

Test: `tests/test_session.py` — 100 malformed pushes in a second log one warning with the count.

**References.** `journald.conf(5)` `RateLimitIntervalSec`, `RateLimitBurst`.

---

## B3 The broker

### SEC-004 An insecure broker configuration (no credentials, no TLS) is accepted without a word

| | |
| --- | --- |
| Category | security (authentication of the bus; confidentiality on the wire) |
| Component | `config.py:450-488` (`_mqtt`: `username`, `password`, `tls` all optional), `cli.py:447-486` (`_check_config`, `_login`: "no user name" printed as a fact), `mqtt/client.py:156-174` (`start`), `deploy/proxmox/meshcore-home-gateway-lxc.sh` `ask_broker` (user and password optional; TLS only in the advanced setup) |
| Related | design §7.2 rows "Abuse of the send topic", "Forged events"; `docs/mqtt-api.md` "Securing the broker"; SEC-005, SEC-006, SEC-009; IMP-007 |

**Description.** The gateway's security model places the authentication of everything on the
bus with the broker: "Topic ACLs … Required when commands are configured" (design §7.2), "When
commands are configured, the ACL is not optional: any client that can write `command` can
trigger the automations" (`docs/mqtt-api.md`). The gateway itself does not check, warn or refuse
when the configuration implies that no such protection exists:

* `[mqtt]` with neither `username` nor `password` connects anonymously. `check-config` prints
  "no user name" in its summary, as a fact among others.
* `tls = false` (the default) sends the password, every message text, every position and every
  command event in the clear on the network between the gateway and the broker.
* Nothing verifies that the broker enforces an ACL, or even that it refuses anonymous clients.
  A broker that accepts the gateway's credentials may accept anyone's.

The installer's default setup asks for a user name "none (suggested: meshcore-home-gateway)" and
asks for a password only when a user name was given; TLS is in the advanced setup. A user who
presses Enter twice has an anonymous, plain-text gateway, and the final summary says "login
accepted".

**Evidence.** `config._mqtt` (both optional; the only cross-rule is "password needs username");
`cli._summary` line `broker {host}:{port}, {login}`; the installer's `ask_broker` (lines
847–876).

**Preconditions and attacker model.** T6 without any credential: any device on the network that
reaches the broker (a guest's phone on the WiFi, a compromised IoT device, a laptop on a shared
LAN). With a broker that allows anonymous clients and has no ACL (Mosquitto's `allow_anonymous
true`, which was the default before 2.0 and is what many "quick" setups use), such a device can
publish a `command` event with any word and any `sender_key` (A1), publish `contacts/<key>`
documents with any position (SEC-005, A3), send to every target and contact (A7), read every
message and position (A3, A4), and disconnect the gateway (SEC-006). Without TLS, a device that
can see the traffic (a shared WiFi, a mirrored port) reads it all and learns the password.

**Impact.** The whole trust model of the bus collapses to "whoever is on the network". The gateway
is the component that knows that its topics are dangerous and is the only one that knows which
settings it was given; today it says nothing.

**Existing mitigations.** The documentation, in several places, and an ACL example;
`check-config --broker` tests the configured login; the password-needs-username rule.

**Suggested mitigation or implementation.**

1. **Warn at start and in `check-config`** when (a) no credentials are configured, (b) `tls` is
   off, and especially (c) commands are configured with either. One line each, at warning level,
   naming the consequence ("any client of the broker can publish command events"). This costs
   nothing and is not a changed interface.
2. **An explicit acknowledgement for the dangerous combinations**: `[mqtt] insecure = true`
   required when `commands.senders` is set without credentials, or without TLS when the broker is
   not on the loopback or a link-local address. As with SEC-003 option 2, a new rule between
   settings is breaking for the configuration interface; the team can introduce it as a warning
   now and a rule in the next major version.
3. **Probe the broker for anonymous access** in `check-config --broker` (and once at start, at
   info level): open a second connection without credentials; if the broker accepts it, warn
   "the broker accepts anonymous clients". Mosquitto refuses with CONNACK 5 when
   `allow_anonymous false` (platform notes); an accepted anonymous connection is a certain sign.
   Optionally subscribe anonymously to `<root>/command` and publish a retained empty message to a
   scratch topic under `<root>/` to see whether an anonymous client can read and write the
   gateway's tree; then say so. The gateway needs no new dependency for this.
4. **The installer**: in the default setup, ask for TLS (yes/no with a one-line explanation) and
   make an empty user name a deliberate choice ("no credentials: anyone on the network can control
   the gateway; continue?"). When the broker is Home Assistant's Mosquitto add-on, credentials are
   always required and the add-on's default ACL is permissive (`docs/platform-notes.md` describes
   it); the installer could say which ACL lines to add.
5. **Documentation**: the README's *Security* paragraph says "Secure the broker (authentication,
   topic ACLs)"; it could say what happens otherwise in one sentence, and `docs/deployment.md`
   §1 could make the ACL a numbered step with the example file to install.

Tests: `tests/test_cli.py` — `check-config` prints the warnings for the three cases;
`tests/test_mqtt_client.py` (broker) — the anonymous probe reports an accepting and a refusing
broker correctly.

**References.** Mosquitto `allow_anonymous` (mosquitto.conf(5)); Home Assistant Mosquitto add-on
documentation (ACL file, `customize`); OWASP IoT Top 10 (I1 weak or no authentication).

---

### SEC-005 A forged or stale retained document is neither detected nor healed; a presence automation follows it

| | |
| --- | --- |
| Category | security (integrity of the documents; the presence path) |
| Component | `observer.py:795-800` (`_publish`: compares with the last payload *sent*, never with the broker), `observer.py:396-404` (`publish_all` on reconnect), `mqtt/client.py:221-231` (no subscription to the gateway's own document topics), `mqtt/contact_devices.py:86-98,293-304` (the tracker reads `position_current` from the document) |
| Related | SEC-004, ADR 0015 ("every front end reads the same documents"), ADR 0024, design §7.2 row "Forged events"; IMP-008 |

**Description.** The gateway's documents (`state`, `stats`, `contacts`, `contacts/<key>`, `nodes`,
`history`, `settings`) are retained messages. Home Assistant's entities read them with templates;
a `device_tracker.mesh_<name>` takes `latitude`, `longitude` and `position_current` from
`contacts/<key>`, a `person` follows the tracker, and presence automations follow the person
(ADR 0024's whole purpose). Any broker client that may write below `<root>/` can publish its own
retained `contacts/<key>` and the broker keeps it until someone overwrites it. The gateway
overwrites a topic only when **its own** document for that topic changes (`_publish` skips a
payload equal to the last one it sent) and at a reconnect (`publish_all`). It never reads what
the broker holds. So:

* For a companion that answers every 15 minutes, a forged position stands for up to 15 minutes
  (the next answer changes `readings_at`, so the document changes and is republished).
* For a companion that is `away`, `no_answer`, excluded, or whose answers do not change the
  document, a forged document stands **until the state changes**, which can be hours or days.
* A forged `state` document with `"radio": "connected"` and `telemetry: "on"` stands until the
  next 60 s state publish (the state is published every 60 s regardless: good); a forged
  `contacts` index stands until the next table read changes it.
* An **empty** retained message on `contacts/<key>` (a "clear") makes Home Assistant's entities
  of that contact read `unknown`/unavailable until the gateway republishes; the gateway does not
  notice.

The same applies to a *stale* document: after a broker restore from backup, or a broker that lost
and regained its persistence, the broker may hold old documents until the gateway reconnects (it
does republish on reconnect, which covers the broker-restart case but not a broker whose retained
store was replaced while the gateway stayed connected).

**Evidence.** `observer._publish`:

```python
payload = encode(document)
if not force and self._published.get(topic) == payload:
    return
```

`mqtt/client._paho_connected` subscribes to the request topics, the status topic and the
discovery topics, not to `<root>/contacts/+`, `<root>/state` or the others.

**Preconditions and attacker model.** T6 with write access to the gateway's tree below
`<root>/`. With the documented ACL that is only the gateway's own user; without an ACL (SEC-004)
it is everyone; with Home Assistant's user as documented (`topic read meshcore/radio1/#`, write
only on request topics) Home Assistant cannot. A compromised device that shares the gateway's
credential, or an operator who gave a dashboard client `readwrite meshcore/#`, can.

**Impact.** A presence automation ("unlock when Alice comes home", "disarm when someone is home",
"alert when nobody is home and a door opens") acts on a position the attacker chose, for as long
as the forgery stands. Readings (battery, temperature) can be forged the same way. The `history`
document can show messages never sent. Nothing in the data lets Home Assistant or a person tell a
forged document from the gateway's: the documents carry no origin, time of publication by the
gateway, or signature (`published_at` does not exist; `heard_at` and `readings_at` are data the
forger writes too).

**Existing mitigations.** The ACL, documented as required; the 60 s state publish; the republish
on reconnect; the 15-minute rhythm of an answering companion.

**Suggested mitigation or implementation.**

1. **Subscribe to the gateway's own document topics and heal.** The adapter subscribes to
   `<root>/state`, `<root>/stats`, `<root>/contacts`, `<root>/contacts/+`, `<root>/nodes`,
   `<root>/history`, `<root>/settings` (seven filters; `contacts/+/send` is already a filter, so
   `contacts/+` adds the document level). On a message whose payload differs from
   `observer._published[topic]` (including an unexpected empty one, and a topic the gateway never
   published), the observer republishes its own payload (or clears a topic it does not own) and
   logs a warning "another client wrote `<topic>`: restored". The gateway's own publishes echo back
   equal to `_published` and cost one comparison each. Subtleties: at connect the retained
   documents arrive before the gateway has published (the `_published` dict is from this run and
   starts empty): treat the first retained message per topic as "the broker's copy", compare it
   with what the gateway *would* publish, and republish only if the gateway has a document for it
   (it publishes all of them at connect anyway); for `contacts/<key>` topics the gateway does not
   know, clear them only if the key is not in `retained_contacts` (the state file's memory). A
   retained document that is *older* than the gateway's (a stale broker) is overwritten the same
   way. A rate limit on heals (one per topic per 10 s) prevents a fight with a persistent writer
   from flooding the bus; the warning names it.
2. **Stamp the documents.** Add `published_at` (the gateway's clock, ISO) and `gateway` (the
   `origin` of discovery: name and version) to every document. Consumers can show a stale
   document; a forger can copy the stamp, so this is not integrity, but it makes "stale" visible
   and gives a tamper-evidence hook for 3.
3. **Sign the documents** (optional, for consumers that can verify): an HMAC-SHA256 over the
   canonical payload with a key from the configuration (`[mqtt] document_key_file`), carried in a
   `signature` field or on a sibling topic `contacts/<key>/signature`. Home Assistant's templates
   cannot verify HMACs, so this is for scripts and future dashboards (ADR 0021's web dashboard);
   for Home Assistant, 1 is the measure.
4. **Make the tracker's availability depend on the gateway's liveness** in Home Assistant: the
   tracker's `availability` list already includes `<root>/availability`; a forged document cannot
   make a tracker available while the gateway is offline. That holds today.
5. **Document** in `docs/mqtt-api.md` "Securing the broker" and design §7.2 that documents are
   data with no origin check, which is why the write ACL is required also for the *document*
   topics (the example ACL covers it with `readwrite meshcore/radio1/#` for the gateway only).

Tests: `tests/test_observer.py` / `tests/test_bridge.py` with a fake bus — a message on
`contacts/<key>` with a foreign payload makes the gateway republish its own and warn; an echo of
its own payload does nothing; a foreign retained document for an unknown key is cleared;
`tests/test_mqtt_client.py` (broker) — the subscriptions exist.

**References.** MQTT 3.1.1 §3.3.1.3 (retained messages); ADR 0015; `docs/mqtt-api.md`
"Securing the broker".

---

### SEC-006 The MQTT client id is predictable; birth messages and discovery clears trigger unbounded re-announcements

| | |
| --- | --- |
| Category | security (availability) |
| Component | `mqtt/client.py:77-83` (`make_paho_client`: `client_id = … or f"meshcore-home-gateway-{node_id}"`, `clean_session=True`), `bridge.py:1062-1072` (`_schedule_announcements`: cancels and reschedules on every call), `bridge.py:1018-1036` (`_connected`, `_ha_device_message`), `mqtt/contact_devices.py:474-489` (`announce_all`) |
| Related | SEC-004, SEC-005, design §6.5, `docs/platform-notes.md` Mosquitto ("The broker's log names the client id"), IMP-009 |

**Description.** Two availability issues on the bus:

1. **Client id squatting.** MQTT brokers disconnect an existing session when a new client
   connects with the same client id (MQTT 3.1.1 §3.1.4, "the Server MUST disconnect the existing
   Client"). The gateway's id is `meshcore-home-gateway-<node_id>`, documented and visible in the
   broker's log and in `$SYS`. Any client that may connect (with a credential if the broker
   requires one, SEC-004) and uses that id kicks the gateway off; paho reconnects after 1–60 s and
   kicks the squatter, who reconnects, and so on. Each of the gateway's reconnects runs
   `_connected`: it republishes the discovery document (with placeholders for removals), the
   `manage_contacts` document, every contact device document (up to 50), the state, and
   `publish_all` (every retained document again). The gateway uses `clean_session=True`, so a
   stable id buys nothing from the broker (no persistent session to resume); it is only a label.
2. **Announcement starvation and amplification.** `_schedule_announcements` is called for every
   `online` on `homeassistant/status` and for every empty, non-retained message on the gateway's
   own discovery topics. It cancels the pending timer and starts a new one of 0.5–2 s. A writer
   that sends `online` every 0.4 s keeps cancelling the timer: **the announcement never fires**,
   and after a real Home Assistant restart the entities do not appear until the writer stops. A
   writer that sends one every 3 s makes the gateway announce every 3 s: the gateway's discovery
   document (tens of KiB), the controls, and up to 50 contact documents (2–3 KiB each), about
   100–200 KiB per announcement, plus the pending removals each time. Home Assistant processes
   every announcement (discovery is not cheap for it: entity registry writes).

**Evidence.** `bridge.py:1062-1068`: the pending handle is cancelled and replaced
unconditionally:

```python
if self._reannounce is not None:
    birth = birth or self._reannounce_birth
    self._reannounce.cancel()
self._reannounce_birth = birth
delay = random.uniform(*DISCOVERY_DELAY_S)
self._reannounce = asyncio.get_running_loop().call_later(delay, self._announce_due)
```

`make_paho_client`, lines 77–83.

**Preconditions and attacker model.** For 1: T6 with a credential (or none, SEC-004) and the
node id (in the topics). For 2: T6 with write access to `homeassistant/status` or the gateway's
discovery topics: with the documented ACL that is Home Assistant's own user (`readwrite
homeassistant/#`), so the attacker is a device that holds Home Assistant's broker credential (a
compromised add-on or integration client), or anyone when there is no ACL.

**Impact.** 1: the gateway is offline for a share of the time the attacker chooses; every alert
queued meanwhile waits (the outbox keeps working while the broker is away, but `delivery` events
queue and commands are refused with `broker_unavailable`); the reconnect storm loads the broker
and Home Assistant. 2: entities missing after a Home Assistant restart for as long as the writer
persists, or a continuous discovery load on Home Assistant. Neither crosses into the home's
controls; both deny the gateway's function.

**Existing mitigations.** Credentials and the ACL (if configured); the random 0.5–2 s delay
(meant to spread load); the `manage_contacts` document's change budget (70 per hour) limits the
select's option churn but not the announcements themselves.

**Suggested mitigation or implementation.**

1. **Client id with a per-start random suffix by default**: `meshcore-home-gateway-<node_id>-<8
   hex>` (`secrets.token_hex(4)`), keeping `[mqtt] client_id` for operators who want a fixed one.
   Update `docs/platform-notes.md` and `docs/deployment.md` where the log line is quoted ("names
   the client (`meshcore-home-gateway-radio1`)" → "starts with"). The CLI's commands already use
   `-<pid>` suffixes. A changed default of `client_id` is listed as breaking in the compatibility
   policy ("a changed default of `mqtt.base_topic`, `mqtt.client_id`, … because brokers, ACLs …
   refer to these names"): a Mosquitto ACL can use `%c` patterns, so an operator may have bound
   the id. The team can weigh a *Changed* entry with a prefix that stays, or wait for 2.0, or
   add the suffix only when `client_id` is unset and document that pattern ACLs should use the
   prefix.
2. **A floor and a ceiling for announcements**: never cancel a pending announcement that is
   already due within the window; instead keep the earliest due time (`min`) so a storm of births
   makes it fire *sooner*, not never; and rate-limit announcements to one per 30 s after the
   first, with the latest request coalesced (a token bucket like `ManageEntities._changes`).
   Sketch in `_schedule_announcements`: if a timer is pending, only extend `birth` and return;
   in `_announce_due`, if the last announcement was less than `ANNOUNCE_MIN_INTERVAL_S` ago,
   reschedule for the remaining time once.
3. **Count and log** repeated births (more than N per minute) as a warning naming the status
   topic, so an operator sees a misbehaving client.

Tests: `tests/test_bridge.py` — 20 births 0.1 s apart produce exactly one announcement within
2.5 s; 100 births over 5 minutes produce at most 11 announcements; a fixed `client_id` is used
as given, an unset one gets a suffix of 8 hex digits.

**References.** MQTT 3.1.1 §3.1.4; Home Assistant MQTT discovery documentation (birth message);
`docs/platform-notes.md` Mosquitto section.

---

## B4/B5 Home Assistant and its users

### SEC-007 Names from the mesh reach the Home Assistant UI unflagged: a stranger can look like a contact or like the gateway

| | |
| --- | --- |
| Category | security (impersonation, social engineering) |
| Component | `mqtt/contact_devices.py:305-314` (device `name: node.name or key[:12]`), `core/contacts.node_label` (`<12 digits>…<8 digits> <name>`), `views.nodes_document` (`name`), `observer._advert` (the `node` event with `name`), `mqtt/discovery.py:249-255` (the gateway's device name), the dashboard's contacts and heard-nodes tables |
| Related | SEC-017, ADR 0017, `docs/home-assistant.md` "Add a node that advertised", IMP-010 |

**Description.** A node's name is chosen by whoever holds its key and reaches the gateway through
its advert. The gateway shows names in: the *Add heard node* select (after the key digits), the
*New node* event, the `nodes` document and the dashboard's heard-nodes table, and, once a node is
a contact, as the **device name** in Home Assistant, which prefixes every entity name of that
device ("Alice Battery", "Alice Position"), the map marker, the notify entity ("Alice"), and the
logbook. Nothing compares names:

* A stranger can advertise as `Alice` while Alice is a contact. The household sees a *New node*
  "Alice" and a select entry `c3d4…e5f6 Alice` next to nothing (Alice is already a contact, so she
  is not in the select). A member who reads the name and not the digits adds the stranger; the
  stranger's device is now "Alice" too, with `notify.mesh_alice_c3d4` (the slug gets the key's
  first four digits when taken) but the display name identical. Messages the household sends "to
  Alice" from the dashboard's form (which lists notify entities by name) may go to the stranger.
* A stranger can advertise as `MeshCore radio` (the gateway's default device name) or as the
  household's chosen gateway name; once added, Home Assistant lists two devices of that name.
* Names can use confusable characters (Cyrillic `а` for Latin `a`): the slug differs, the display
  does not.

The project's defence is correct in principle: the label shows 20 key digits and the guide says
"compare the digits with his screen". It relies on a person doing that every time.

**Evidence.** The code cited; `node_label` deliberately puts the digits first so that a name
cannot push them aside (good); nothing in `views`, `contact_devices` or `manage_entities` compares
a name with other names.

**Preconditions and attacker model.** T1 (any node in range chooses its name) plus a household
member who adds nodes by name, or who picks the wrong notify entity in a form.

**Impact.** Messages meant for a household member go to a stranger (confidentiality of A4);
a stranger's device sits in Home Assistant under a trusted name (its telemetry, if it answers,
is tracked as a household member's — SEC-016); confusion in automations that select notify
entities by name.

**Existing mitigations.** The 20-digit label; the slug suffix for a taken slug (entity IDs stay
distinct); the guide's instruction; the look-alike flag for *key prefix* collisions (which is a
different, far more expensive attack).

**Suggested mitigation or implementation.**

1. **Flag name collisions** in the `nodes` document and the `node` event: `name_taken: true`
   when the node's name, NFKC-normalised and case-folded, equals that of a contact, of the
   gateway's radio, of the Home Assistant device name, or of another heard node; list them in
   `name_taken_total`. Add ` (name of a contact)` or ` (name taken)` to the select's label after
   the name, and show it in the dashboard's heard-nodes table.
2. **Disambiguate device names in Home Assistant**: when a contact's name equals another
   contact's or the gateway's, name the device `Alice (c3d4…)` with the key's first digits (the
   later one; the first keeps the plain name). Home Assistant keeps a user-set `name_by_user`, so
   a rename by the gateway does not override a name the user gave.
3. **Confusables**: a light check with `unicodedata.normalize("NFKC")` plus a small table of
   common homoglyphs (Cyrillic/Greek letters that look Latin) flagging `name_confusable: true` is
   cheap; full Unicode confusable detection (UTS #39) is not in the standard library and not
   needed.
4. **The dashboard's form** (`script.mesh_message`) lists notify entities by *name*; a note in
   the guide that two contacts can share a name, and the `(key)` suffix of 2, cover it.

Tests: `tests/test_views.py` — a heard node named like a contact is flagged; `tests/test_contact_devices.py` —
the second contact named "Alice" gets the suffix in `dev.name`; `tests/test_contacts.py` — the
label carries the mark.

**References.** Unicode UTS #39 (confusables); Home Assistant device registry (`name_by_user`).

---

### SEC-008 Every Home Assistant user can send, add and remove contacts, toggle anyone's telemetry and read every position

| | |
| --- | --- |
| Category | security (authorisation within the household; privacy) |
| Component | `mqtt/discovery.py` (notify per target, `show_hidden` button), `mqtt/manage_entities.py` (the select and the text), `mqtt/contact_devices.py` (`remove` button, `telemetry` switch, `send` notify, the tracker, the readings), `config.py:133` (`[contacts] manage = true`), `config.py:140` (`[telemetry] enabled = true`) |
| Related | SEC-016, SEC-013, ADR 0017 ("Who may manage: … on by default … because a contact grants no rights"), `docs/home-assistant.md` "Who may", "The dashboard has no control to remove a contact …: that does not keep other users from them", IMP-011 |

**Description.** Home Assistant has two kinds of users, administrators and others; it has no
per-entity permissions in its user interface (a policy framework exists in the auth system but
is not exposed). Every user can call every entity's actions: press buttons, flip switches, pick
select options, type into text entities, and call `notify.send_message`; the entity search and
the actions UI reach entities that no dashboard shows. The project knows and documents this
(`docs/home-assistant.md`: "In Home Assistant every user can press a button and set a select or
text", "any user of Home Assistant can open a contact's *Remove from radio* button or its
*Telemetry* switch from the entity search and use it").

So with the defaults, a household member, a child with an account, a guest account, or a
compromised companion-app session can: send messages over the mesh to every target and every
companion (as the household; A7 airtime; social engineering of the recipients); add any heard
node as a contact and remove any non-protected contact (A6: the radio's table; SEC-013's
precondition; and removing a companion ends its presence tracking); exclude a household member
from telemetry (their presence disappears) or include a stranger (SEC-016); hide and show
devices; read every position, message text and reading (A3, A4) in the UI, the map, the logbook
and the history.

ADR 0017's reasoning "a contact grants no rights" is true for the *command* path and not for the
rest: being a contact means the radio decrypts and acknowledges the contact's messages
(SEC-013), the gateway polls the contact (SEC-016), and the contact's messages and telemetry land
in the home's databases (SEC-012, SEC-015).

**Evidence.** The components cited; the guide's own statements; Home Assistant's authorisation
model (the `system-users` group has `entities: all`).

**Preconditions and attacker model.** T7: any Home Assistant account, or any device with a
long-lived access token or a logged-in companion app.

**Impact.** Within the household, no separation between who may operate the mesh gateway and who
may use Home Assistant; for a shared Home Assistant (flatmates, a holiday home, a club) the
gateway's controls and data are available to everyone who can switch a light.

**Existing mitigations.** The documentation; `[contacts] manage = false` (which also disables
MQTT and CLI management); protected contacts cannot be removed; the broker ACL limits which
*broker* clients may write the request topics, but Home Assistant's own user needs all of them
for the controls to work.

**Suggested mitigation or implementation.** Separate the Home Assistant exposure from the
function, so that an operator can keep the function (MQTT, CLI) and not expose it to every
Home Assistant user:

1. `[homeassistant] controls = "all" | "none" | <list>` (or three booleans
   `manage_controls`, `telemetry_switch`, `contact_notify`): with `none`, the select, the text, the
   remove buttons, the telemetry switches and the show-hidden button are not announced (their
   components are removed through the placeholder mechanism the project already has); the MQTT
   requests and the CLI keep working, so an administrator uses `meshcore-home-gateway contacts
   …` or an automation with a condition on the user. Default: `all` (today's behaviour), with the
   guide recommending `none` for shared installations.
2. **Per-target notify exposure**: `[targets.<alias>] expose = false` hides a target's notify
   entity (an alarm channel that only automations should use) while keeping it reachable on MQTT.
3. **The positions**: Home Assistant shows trackers to everyone; the gateway cannot change that.
   The guide could recommend the `person` entity's own visibility settings and dashboards per
   user, and say plainly that the map is visible to every user.
4. **Document the Home Assistant policy file** for administrators who want more: a custom group
   with an entity policy (`.storage/auth`, `permissions`) can deny entities to a user group; it is
   unsupported in the UI but documented in the Home Assistant developer docs. A paragraph with a
   pointer suffices.

Tests: `tests/test_discovery.py` / `test_contact_devices.py` — with `controls = "none"` the
documents carry no select, text, button or switch components, and an earlier run's are removed.

**References.** Home Assistant developer documentation, "Permissions" (`auth/permissions`);
ADR 0017.

---

## B6 The host

### SEC-009 The broker password is a group-readable file, printed by `config answers`, never a systemd credential

| | |
| --- | --- |
| Category | security (credential handling) |
| Component | `config.py:466-468,475-488` (`password` in `MqttConfig`), `config.config_answers` (prints it), `cli._render` (`--group`, mode 0640), `deploy/systemd/meshcore-home-gateway.service` (no `LoadCredential=`), the installer's `configure` and `load_current` (the password through `pct exec` pipes), `docs/configuration.md` ("The output includes the broker password") |
| Related | SEC-004, OPS-00x, IMP-012 |

**Description.** The broker password lives in `config.toml` (0640, `root:meshcore-home-gateway`),
which is right for the service to read. Consequences the project accepts and documents:

* `meshcore-home-gateway config answers` prints the password to standard output; the installer's
  `--reconfigure` relies on it (`load_current`), so the password travels host ↔ container through
  `pct exec`'s pipes and sits in the host's bash variables for the run.
* `pct exec <id> -- cat /etc/meshcore-home-gateway/config.toml`, a backup of `/etc`, a support
  bundle, a screenshot of the file all carry it.
* Every process running as the service user (the gateway, and `send`/`listen`/`check-config` when
  run as that user) reads the whole configuration; a bug that lets an attacker read files as the
  service user (none found) would read the password.
* The CLI's `send`, `listen` and `contacts` commands run as root in the container (the documented
  way) and read the same file.

systemd offers `LoadCredential=` / `SetCredentialEncrypted=` so that a secret is handed to the
service in `$CREDENTIALS_DIRECTORY` as a file readable only by that process, kept out of the
configuration file, out of `config answers`, and (with `SetCredentialEncrypted`) encrypted at
rest with the host's TPM or a host key. Debian 13's systemd (257) supports all of it; in an
unprivileged LXC container `LoadCredential` works (it is a file copy into a private tmpfs);
encrypted credentials need the host's key, which a container does not have, so the plain
`LoadCredential` is the applicable form.

**Evidence.** `config.py` lines cited; `docs/configuration.md` "Installer answers": "The output
includes the broker password"; the unit file (no credential directives).

**Preconditions and attacker model.** T9/T10 (a process as the service user, root in the
container), the host's administrators, whoever receives a backup or a support bundle; T6 for the
plain-text transport when TLS is off (SEC-004).

**Impact.** The credential that may write every document and event of the gateway (A8), and with
it the command path (A1) and the presence path (A3), is in more places than it needs to be.

**Existing mitigations.** 0640 and the group; the state directory 0700; `UMask=0077`; the
installer's temporary answers file with `umask 077` and deletion; no logging of the password
anywhere (`_login` prints only whether one is set).

**Suggested mitigation or implementation.**

1. **`[mqtt] password_file = "/path"`** as an alternative to `password` (exactly one of the two):
   the file is read at start; `config answers` prints the path. With the unit's
   `LoadCredential=mqtt-password:/etc/meshcore-home-gateway/mqtt-password` and
   `password_file = "$CREDENTIALS_DIRECTORY/mqtt-password"` (or a fixed convention: if
   `password_file` is a bare name, look in `$CREDENTIALS_DIRECTORY`), the service reads it from
   systemd's private copy; the CLI commands, which run outside the unit, read the path directly
   (as root or with the group). The installer writes the password into the credential file (mode
   0640 `root:meshcore-home-gateway`, or 0600 root if only the service and root need it) and
   `password_file` into the configuration. Compatible: a new optional setting.
2. **Stop printing the secret**: `config answers` prints `mqtt.password_file=…` when that form is
   used; with the old form it has to print the password for `--reconfigure` to keep it, or
   `--reconfigure` could keep the password when the answer is empty *and* the file is unchanged
   (it already keeps it "while the user name stays the same", so the print is only needed to
   seed the dialog: a sentinel `(unchanged)` would do).
3. **Document the exposure** in `docs/deployment.md` §4 (backups: "the configuration holds the
   password; store backups encrypted") — it says to back up the file and not that.

Tests: `tests/test_config.py` — `password_file` loads, `password` and `password_file` together
is an error, a missing file is a configuration error; `tests/test_cli.py` — `config answers`
prints the path.

**References.** `systemd.exec(5)` `LoadCredential=`, `SetCredentialEncrypted=`; `systemd-creds(1)`.

---

## 9 What was looked for and not found

For the record, the reviewer looked for these classes of defect and found none; the team can
treat the list as the baseline the current code meets:

| Looked for | Where | Result |
| --- | --- | --- |
| Shell or command injection | the service (never spawns a process), the installer (arguments, regular expressions, `env -i`), the tools | none |
| SQL injection | `history.py` | parameters throughout |
| Template injection into Home Assistant | discovery documents (constants), the dashboard (`m()` macro, tested with hostile input) | none |
| Path traversal | `config.py` (absolute paths required), the state directory (fixed names), the installer's `FETCH_PY` (`filter="data"`, member names checked against a fixed list) | none |
| Deserialisation of untrusted data | `json.loads` on bounded input with type checks per key; `tomllib` on root's file; no `pickle`, no `yaml.load` | none |
| Unbounded buffers from the radio | `FrameDecoder` (≤ 179 bytes pending), decoders total and bounded by the frame | none |
| Unbounded buffers from the bus | request payloads checked at 1 KiB (after paho read them, documented), queues drained synchronously | none beyond the documented paho limit |
| Timing side channels in the command check | the 4-byte prefix resolution and the allowlist compare strings; nothing secret is compared | not applicable |
| Weak randomness where it matters | `secrets` for identifiers; `random` only for jitter (annotated) | fine |
| TLS misuse | `ssl.create_default_context` with hostname verification; no way to disable it; client certificates supported | fine |
| Secrets in logs | `_login` never prints the password; the installer strips the program's prefix only | none |
| Terminal escape injection | `_safe_text`, `_json_text` in every CLI output of mesh text | none |
| Race conditions between threads | paho's thread hands everything to the loop with `call_soon_threadsafe`; nothing else is threaded | none |
| Replay of commands | the ledger before publish, the exact-repeat memory sized to the age window | none |
| Look-alike acceptance | the memory, the fresh read, the connect-time rejection, the refusal at add | none |
| Setting the remote-CLI or telemetry flags on contacts | `codec.new_contact`, `add_update_contact` refuse any flag but favourite | none |
| Dangerous commands to the radio | no encoder exists; a test guards it | none |
