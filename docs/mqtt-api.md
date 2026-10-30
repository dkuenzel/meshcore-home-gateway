# MQTT API

All topics hang below `<base_topic>/<node_id>`; with the defaults of the example configuration that
is `meshcore/radio1`. Everything uses QoS 1, except the `command` events (QoS 0, see there). The
API is a public interface under semantic versioning
([compatibility](development.md#compatibility)).

| Topic | Direction | Retained | Payload |
| --- | --- | --- | --- |
| `availability` | bridge → | yes | `online` / `offline`; last will `offline` |
| `state` | bridge → | yes | JSON state document |
| `send/<alias>` | → bridge | must not be | UTF-8 text |
| `delivery` | bridge → | no | JSON, `event_type` = delivery state |
| `received` | bridge → | no | JSON, `event_type` = `contact` or `channel` |
| `command` | bridge → | no | JSON, `event_type` = command word |
| `rx` | bridge → | no | JSON per received packet, only with `publish_rx_log` |
| `stats` | bridge → | yes | JSON: the radio's own measurements |
| `contacts` | bridge → | yes | JSON index of the radio's contact table |
| `contacts/<key>` | bridge → | yes | JSON per contact, `<key>` its full public key in lower-case hex; cleared (empty retained message) when the contact leaves the table |
| `contacts/<key>/send` | → bridge | must not be | UTF-8 text for a companion in the contact table |
| `contacts/<key>/remove` | → bridge | must not be | any payload, or JSON `{"id": "…"}`: remove that contact from the radio ([managing contacts](#managing-contacts-add_contact-and-contactskeyremove)) |
| `add_contact` | → bridge | must not be | a public key, contact link or contact card, or JSON `{"contact": "…", "id": "…"}`: add a contact to the radio |
| `contacts/<key>/telemetry` | → bridge | must not be | `on` or `off`, or JSON `{"telemetry": "off", "id": "…", "forget": true}`: ask a companion for its telemetry, or exclude it ([excluding](#excluding-a-contact-from-telemetry-contactskeytelemetry)) |
| `contacts/<key>/received` | bridge → | no | the `received` events of messages from that contact |
| `nodes` | bridge → | yes | JSON: nodes heard but not added |
| `history` | bridge → | yes | JSON: the latest messages in and out |
| `settings` | bridge → | yes | JSON: the managed radio profile at the last connect |
| `contact` | bridge → | no | JSON, `event_type` = `added`, `removed`, `hidden`, `shown`, `excluded`, `included`, `refused` or `failed` |
| `telemetry` | bridge → | no | JSON, `event_type` = `answered` or `unanswered`: a companion answers again or stopped answering ([telemetry events](#telemetry-events-telemetry)) |
| `node` | bridge → | no | JSON, `event_type` = `new`: a node heard for the first time |
| `setting` | bridge → | no | JSON, `event_type` = `written`, `failed`, `not_taken`, `not_converging` or `drift`: a radio setting of the profile |
| `homeassistant/show_hidden` | → bridge | must not be | any payload: show the contacts deleted in Home Assistant again |
| `homeassistant/add_node` | → bridge | must not be | the label of a heard node, as Home Assistant's select offers it: add it to the radio ([managing contacts](#managing-contacts-add_contact-and-contactskeyremove)) |
| `<prefix>/device/meshcore_home_gateway_<node_id>/config` | bridge → | no | Home Assistant discovery; `<node_id>` as configured, `-` included |
| `<prefix>/device/meshcore_home_gateway_<node_id>/manage_contacts/config` | both | no | discovery of the controls that add contacts, on the gateway's device ([home-assistant.md](home-assistant.md#adding-and-removing-contacts)) |
| `<prefix>/device/meshcore_home_gateway_<node_id>/contact_<key>/config` | both | no | discovery of each contact's device ([home-assistant.md](home-assistant.md#contacts)); the gateway reads it to notice a device deleted in Home Assistant |

Times are ISO 8601 in UTC with seconds. Unknown values are `null`; keys are always present.

The retained JSON topics are the gateway's *documents* ([ADR 0015](decisions/0015-provider-neutral-documents-on-the-bus.md)):
every front end (Home Assistant through discovery, other home automation systems through their
MQTT bindings, later dashboards) reads the same ones. A document is published when it changes
and again after every reconnect to the broker. Units are in the key names, machine values are in
lower case, and texts from the mesh (names, messages) are plain data: never interpret them as
markup. Lists are bounded in size and carry `total` and `truncated`.

Within a major version topics, keys and values are only added. Ignore topics and keys you do not
know, and expect new values in every list of values below (`event_type`, `reason`, `state`,
`status`, `type`, `flags`, `errors` and the like): treat one you do not know as "other". Retained
flags, QoS and the topic layout stay as listed here. The size limits of requests only grow;
documents and list bounds grow only within the ceilings of
[ADR 0015](decisions/0015-provider-neutral-documents-on-the-bus.md): a document that Home
Assistant reads into attributes stays below 16 KiB, every document below 64 KiB. Raising a ceiling
is breaking.

## Sending: `send/<alias>`

The payload is the text, UTF-8, at most 1 KiB. Only aliases from `[targets.*]` in the
configuration exist. The bridge cleans the text (control characters, direction overrides, repeated spaces), optionally
transliterates it to ASCII, and truncates or rejects texts above the radio's limit: 160 bytes for a
direct message, `160 − (radio name length + 2)` for a channel message. Before the radio has
connected once, its name is unknown: a channel text is then accepted up to 158 bytes and limited
when it is sent (truncated, or `failed` with `too_long`).

```sh
mosquitto_pub -h broker.example.org -u client -P secret -q 1 \
  -t meshcore/radio1/send/alerts -m "Water pump stopped"
```

**To a contact**: `contacts/<key>/send` sends a direct message to a companion in the radio's
contact table, `<key>` its full public key in lower-case hex, with the same cleaning, queue,
airtime budget and duplicate check as a target
([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md)). The deliveries
name the key as `target`. A key that is not in the table as the gateway last read it is rejected
(`unknown_contact`, also for a key in upper case or a prefix), and so is a repeater, room server or
sensor (`not_a_companion`). Channels are
reached through targets only: a channel reaches everyone who has its secret. A broker ACL decides
who may write `contacts/+/send`.

Publish requests without the retain flag. The broker forwards a request published with it to the
running bridge like any other, so it is sent once; the copy the broker keeps would be delivered
again after every reconnect, so retained messages on these topics are rejected there
(`reason: retained`). Requests published while the bridge is offline are lost (clean session);
check `availability` first if it matters.

## Delivery: `delivery`

One event per step. While the gateway runs, every request ends with exactly one final event:
`rejected` or `duplicate` at once; otherwise `expired` or `failed`, or `sent` for a channel
message and `confirmed` for a direct message. A stop or restart drops queued messages without one
([design §3](design.md#3-goals-and-non-goals)), and so does a broker outage with more than 1000
events waiting; wait for a final event with a timeout. The `rejected` and `duplicate` events of
requests refused at once are limited to 20 per second, in bursts of up to 100: a client that floods
the send topics gets no event for the excess, which the log counts (at most once a minute).

```json
{"event_type": "confirmed", "id": "7f3a91c2", "target": "operator", "text": "Back online",
 "attempt": 2, "reason": null, "rtt_ms": 2300, "airtime_ms": null}
```

| `event_type` | Meaning | `reason` values |
| --- | --- | --- |
| `queued` | accepted, waiting for the radio and for airtime | – |
| `sent` | handed to the radio; `attempt` numbers this transmission from 1, `airtime_ms` is the estimate | – |
| `confirmed` | the recipient acknowledged; `rtt_ms` from the radio | – |
| `failed` | given up | `no_acknowledgement` (sent, but not acknowledged in time), `unknown_contact`, `ambiguous_target`, `channel_not_configured`, `too_long`, `exceeds_airtime_burst`, `internal_error` (a bug; see the log), the radio's refusals: `disabled` or an error name such as `not_found`, `table_full`, `error_<code>` |
| `expired` | never sent within `queue_ttl_s` | `no_radio`, `queue_ttl` (waited behind other messages), `airtime_budget` |
| `duplicate` | same target and text within `dedup_window_s` | – |
| `rejected` | not accepted | `unknown_target`, `unknown_contact`, `not_a_companion`, `retained`, `too_large`, `invalid_utf8`, `empty`, `too_long`, `queue_full` |

`id` is a random identifier per request. In final events, `attempt` is the number of
transmissions so far (0 if the message never left). Retries of a direct message keep the radio
timestamp, so a recipient that got the message but whose acknowledgement was lost may show it
twice. An attempt that a radio disconnect interrupted after the radio had sent it is repeated as
the identical packet: nodes that saw the first copy drop it, so it earns no acknowledgement, and
the next attempt may show the message twice the same way.

## Received messages: `received`

Every message the radio receives, after dropping exact repeats (same sender, timestamp and text,
which is how retries arrive). Channels listed in `ignore_channels` are skipped.

```json
{"event_type": "contact", "text": "status please", "text_type": "plain",
 "sender_key": "a1b2…64 hex digits", "sender_prefix": "a1b2c3d4e5f6", "sender_name": "Alice",
 "target": "operator", "channel_index": null,
 "sent_at": "2026-10-18T09:00:00+00:00", "received_at": "2026-10-18T09:00:02+00:00",
 "snr_db": 7.25, "hops": 2,
 "command": {"word": "status", "accepted": true, "reason": null}}
```

| Key | Meaning |
| --- | --- |
| `event_type` | `contact` (direct message) or `channel` |
| `text` | message text; for channels without the `"name: "` prefix |
| `text_type` | `plain`, `cli_data`, `signed_plain` (room server post), `cli_command` |
| `sender_key` | full public key if the 6-byte prefix matches exactly one contact in the radio's contact table as the bridge last read it, else `null` |
| `sender_prefix` | first 6 bytes of the sender's key, hex (direct messages) |
| `sender_name` | contact name from the same table; for channels the **claimed** name from the text, not authenticated |
| `target` | alias of a configured target matching the sender or channel, else `null` |
| `channel_index` | channel messages only |
| `sent_at` | sender's clock; `received_at` the bridge's clock |
| `snr_db` | signal-to-noise ratio of the last hop, 0.25 dB steps; `null` for legacy frames |
| `hops` | repeaters a flooded message passed; `null` if it arrived by direct route |
| `command` | `null` unless the first word is a command word; then `accepted` and `reason` |

A message frame names its sender only by the 6-byte prefix, so `sender_key`, `sender_name` and
`target` are informational: a contact with the same prefix that the radio added or dropped since
the bridge last read the table is not reflected there. Base decisions on the `command` topic,
whose sender the bridge verifies.

## Commands: `command`

Only accepted commands ([ADR 0009](decisions/0009-inbound-commands-from-allowlisted-contacts.md)):
a plain direct message whose first word (ended by any whitespace, also a line break) is in
`commands.words`, from a contact whose full key is
in `commands.senders`, newer than the last accepted command of that sender and within
`max_age_s` of the bridge clock.

```json
{"event_type": "arm", "args": "now", "sender_key": "a1b2…", "sender_name": "Alice",
 "sent_at": "2026-10-18T09:00:00+00:00", "received_at": "2026-10-18T09:00:02+00:00"}
```

Treat `args` as untrusted input. Rejected attempts appear only on `received`, with
`command.accepted` false and one of these reasons:

| `command.reason` | Meaning |
| --- | --- |
| `sender_unknown` | the sender prefix matches no contact on the radio |
| `sender_ambiguous` | it matches several contacts, or another key with this prefix was a contact on the radio in the last 24 hours |
| `sender_not_allowed` | the contact's full key is not in `commands.senders` |
| `sender_unverified` | the bridge fetched the command while connecting (it waited in the radio, or arrived during the connection setup), or the contact table could not be read (also when the radio connection ended first); the sender can send it again |
| `state_unwritable` | the state file could not be written, so the replay protection could not be kept; the sender can send it again |
| `broker_unavailable` | the gateway knew the broker was unreachable: the command event would have reached the automations only after the reconnect, at any later time; the sender can send it again |
| `replay` | not newer than the last accepted command of this sender |
| `too_old`, `from_the_future` | outside `max_age_s` of the bridge clock; with `max_age_s = 0`, more than a day ahead |

An identical copy of a command's packet, also of a rejected one, is dropped as a repeat: it is
the sender's retry after a lost acknowledgement or a replay, which cannot be told apart. To have
a rejected command judged again, the sender sends it anew, with a newer timestamp. The repeat
memory lasts an hour, or twice `max_age_s` and a minute if that is longer; after
`broker_unavailable` the bridge has also stored the timestamp, so a later copy is a `replay`.
After `sender_unverified` (the sender was not verified) and `state_unwritable` it has not: a copy
that arrives after a restart of the gateway is judged afresh and accepted if it is still within
`max_age_s`, and with `max_age_s = 0` also one after the hour
([design §7.4](design.md#74-residual-risks)).

## State: `state`

Retained; published at start, on every change of the radio link or of `telemetry` and its
reason, and every 60 s.

```json
{"radio": "connected", "battery_v": 4.012, "airtime_1h_s": 3.2, "airtime_available_s": 9.4,
 "airtime_budget_s_per_hour": 36.0, "queue": 0, "radio_name": "Gateway",
 "public_key": "…", "model": "…", "firmware": "v1.18.0", "frequency_mhz": 869.618,
 "bandwidth_khz": 62.5, "spreading_factor": 8, "coding_rate": 8, "tx_power_dbm": 22,
 "contact_link": "meshcore://contact/add?name=Gateway&public_key=…&type=1",
 "version": "0.1.0", "telemetry": "on", "telemetry_reason": null, "telemetry_airtime_1h_s": 4.5,
 "telemetry_mesh_airtime_1h_s": 10.6}
```

`contact_link` adds the gateway in the MeshCore app, for example as a QR code (`null` until the
radio is known). It carries no position.

`telemetry` says whether the gateway asks the companions for their telemetry
([configuration.md](configuration.md#telemetry)): `off`, `on`, `paused` or `unsupported` (its
firmware does not take the request, until it reconnects); `null` if it cannot be told. While it
is `paused`, `telemetry_reason` says why, else it is `null`:

| `telemetry_reason` | Meaning |
| --- | --- |
| `first_start` | telemetry runs for the first time on this state directory: nobody is asked for an hour after the radio first connected, so the household can exclude strangers first |
| `no_radio` | the radio is not connected |
| `exclusions_unreadable` | `telemetry-excluded.json` cannot be parsed: fix or delete it, then restart the gateway ([excluding](#excluding-a-contact-from-telemetry-contactskeytelemetry)) |
| `auto_add` | the radio adds contacts of some type on its own |
| `radio_airtime` | the radio transmitted 180 s in the last hour |

`telemetry_airtime_1h_s` is the gateway's own transmit time for telemetry in the last hour,
`telemetry_mesh_airtime_1h_s` the estimated airtime its requests caused in the mesh (every hop
both ways); both `null` with telemetry off.

## Radio statistics: `stats`

Retained; from the radio's own counters (`GET_STATS`), read with the battery every 60 s. `null`
while the radio has not reported them, or when its firmware does not have them.

```json
{"read_at": "2026-10-18T09:00:00+00:00", "uptime_s": 3600, "queue_length": 0, "errors": [],
 "noise_floor_dbm": -118, "last_rssi_dbm": -97, "last_snr_db": 6.5, "tx_airtime_s": 41,
 "rx_airtime_s": 300, "tx_airtime_1h_s": 12, "tx_airtime_window_s": 3600,
 "packets_received": 120, "packets_sent": 30, "packets_sent_flood": 10,
 "packets_sent_direct": 20, "packets_received_flood": 70, "packets_received_direct": 50,
 "receive_errors": 2, "storage_used_kb": 12, "storage_total_kb": 976, "airtime_factor": 9.0,
 "duty_cycle_percent": 10.0}
```

| Key | Meaning |
| --- | --- |
| `uptime_s`, `tx_airtime_s`, `rx_airtime_s`, `packets_*`, `receive_errors` | since the radio booted; `tx_airtime_s` counts everything the radio sent: messages, acknowledgements, adverts, repeats |
| `tx_airtime_1h_s` | the radio's time on air within the last hour (or within `tx_airtime_window_s`, while the gateway has watched for less than an hour; a restart of the radio is bridged): its real duty cycle, unlike the gateway's own estimate `airtime_1h_s` in `state` |
| `errors` | the radio's error flags: `queue_full`, `cad_timeout`, `start_rx_timeout` |
| `airtime_factor`, `duty_cycle_percent` | the radio's own transmit limit, `1 / (1 + factor)` ([protocol.md §7](protocol.md#7-radio-parameters-and-time-on-air)); `null` on firmware without `GET_TUNING_PARAMS` |
| `receive_errors` | `null` before firmware 1.18 |

## Contacts: `contacts` and `contacts/<key>`

The radio's contact table, as the gateway last read it: an index, and one document per contact
([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md)). Subscribe to
`contacts/+` for all contacts. When a contact leaves the table, its topic is cleared with an
empty retained message, also if that happened while the gateway was stopped.

```json
{"total": 2, "max": 350, "manage": true, "remote_cli": [], "remote_cli_total": 0,
 "advert_position": [], "advert_position_total": 0, "telemetry_excluded": ["c3d4…"],
 "telemetry_excluded_total": 1, "truncated": false,
 "contacts": [{"key": "a1b2…", "name": "Alice", "type": "chat",
               "heard_at": "2026-10-18T09:00:02+00:00", "protected": true}]}
```

The index lists the contacts most recently heard first, up to 32 KiB. `max` is the radio's table
size; `manage` whether the gateway takes add and remove requests: `[contacts] manage`, and,
once the radio is known, firmware the gateway is tested with or `radio.profile.manage = "always"`. `remote_cli` lists (up to 16) contacts that may run the radio's command line over the air
(flag 0x10; the gateway never sets it, remove such contacts), `advert_position` companions whose
adverts carried a position, which everyone in range can read. `telemetry_excluded` lists (up to
16, sorted) the keys excluded from telemetry, also those not on the radio,
`telemetry_excluded_total` counts them all ([excluding](#excluding-a-contact-from-telemetry-contactskeytelemetry)).

```json
{"key": "a1b2…", "prefix": "a1b2c3d4e5f6", "name": "Alice", "type": "chat",
 "flags": ["favourite"], "favourite": true, "remote_cli": false, "protected": true,
 "removable": false, "verified": true, "path_known": true, "path_hops": 2, "last_advert_at": "2026-10-18T08:55:00+00:00",
 "first_heard_at": "2026-10-11T18:00:00+00:00", "heard_at": "2026-10-18T09:00:02+00:00",
 "heard_via": "message", "hops": 2, "snr_db": 7.25, "rssi_dbm": null,
 "latitude": 53.55, "longitude": 9.99, "altitude_m": 12.0,
 "position_at": "2026-10-18T08:40:05+00:00", "position_source": "telemetry",
 "position_accuracy_m": 15, "position_received_at": "2026-10-18T09:00:05+00:00",
 "advert_position": false, "telemetry_state": "answering", "telemetry_excluded": false,
 "telemetry_current": true, "position_current": true, "readings_at": "2026-10-18T09:00:05+00:00",
 "battery_v": 4.12, "mcu_temperature_c": 23.4,
 "readings": {"ch2_temperature_c": 21.3, "ch2_humidity_percent": 55.5,
              "ch2_barometric_pressure_hpa": 1013.2, "ch2_altitude_m": 120.0}}
```

| Key | Meaning |
| --- | --- |
| `type` | `chat` (a companion: someone's radio), `repeater`, `room`, `sensor` |
| `flags` | `favourite`, `telemetry_base`, `telemetry_location`, `telemetry_environment` (what this contact may request from the gateway's radio), `remote_cli`; unknown bits as one hex value |
| `protected` | a configured contact target or command sender ([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md)) |
| `removable` | a remove request would be taken: `manage` of the index, and this contact is not protected |
| `verified` | the radio holds the time of a signed advert of this key, so name and type are the key holder's own: a node the gateway heard advertising is `true` at once; one it had not heard (added from a link, a card or a bare key) is `false` until its first advert |
| `path_known`, `path_hops` | whether the radio has a direct path, and its repeaters; without one it floods. After a path reset (a message's flood fallback) `false` at once |
| `last_advert_at` | the contact's clock at its newest advert |
| `heard_at`, `heard_via`, `hops`, `snr_db`, `rssi_dbm` | when and how the gateway last heard from it (`advert`, `message`, `telemetry`): its telemetry answers move `heard_at` at most once an hour; `hops`, `snr_db` and `rssi_dbm` are those of its last advert or message (an answer reports none), `hops` `null` for a direct route, `rssi_dbm` only from adverts |
| `latitude`, `longitude`, `altitude_m`, `position_at`, `position_source` | its newest known position: from its adverts (`advert`) or reported by itself (`telemetry`). `position_at` is when the position last changed (for telemetry: moved 20 m or more; for an advert read at start, the time of the newest advert), never when the companion took it. A position from telemetry expires (`position_current`); the document keeps it |
| `position_accuracy_m` | 15 for a position from telemetry (the 0.0001° cell it is sent in, and the receiver's own error), else `null` |
| `position_received_at` | the newest telemetry answer that carried a position (also when it did not move); after a restart from the saved schedule (`null` if the clock went back meanwhile) |
| `advert_position` | its adverts carried a position: everyone in range can read it |
| `telemetry_state` | its telemetry state ([below](#telemetry-states)); `null` for a repeater, room server or sensor, which are never asked |
| `telemetry_excluded` | the household excluded it from telemetry ([excluding](#excluding-a-contact-from-telemetry-contactskeytelemetry)); `false` for other types |
| `telemetry_current` | its answers are current: `telemetry_state` is `answering` or `no_position`; `null` for other types |
| `position_current` | a position from telemetry that is current (`telemetry_state` `answering`); `false`: from telemetry, no longer confirmed; `null`: no position, or one from adverts, which does not expire |
| `readings_at` | the gateway's time of the answer that the next three keys come from (never the companion's); after a restart the newest answer the history kept, unless telemetry is on and its saved schedule knows a later one the history missed (then `null`, and no values) |
| `battery_v` | its battery: the answer's first value if it is the channel 1 voltage; the companion truncates it to 0.01 V (4.20 V reads 4.19) |
| `mcu_temperature_c` | its processor's temperature: the answer's last value if it is a channel 1 temperature (on a board without a processor sensor, a channel 1 sensor that comes last is named so) |
| `readings` | the answer's other values by [key](#reading-keys), in the answer's order; `{}` without; a key the newest answer lacks is absent. Bounded by the answer, so without `total` |

The values stay when the answers are no longer current (the two `_current` keys say so), and are
cleared when the contact is excluded.

### Telemetry states

`telemetry_state`, the first that applies from the top (Home Assistant lists them as `waiting`,
`answering`, `no_position`, `away`, `no_answer`, `no_path`, `look_alike`, `excluded`, `off`):

| State | Meaning |
| --- | --- |
| `excluded` | the household excluded it |
| `off` | telemetry is off in the configuration |
| `look_alike` | another contact or the radio itself has its first 6 key bytes, so an answer could not be told apart: it is not asked |
| `answering` | its answers are current and carry a position (or one did within the last hour) |
| `no_position` | its answers are current, without a position: no GPS, GPS off, no fix, or location not allowed |
| `away` | it answered within the day, not lately: out of range, switched off, or not asked meanwhile (telemetry paused, the radio away, no path) |
| `no_path` | not asked: the radio has no path to it, and the gateway floods only a companion heard within 30 minutes; a message from it or its advert brings it back |
| `no_answer` | asked, no answer within a day: it does not allow the gateway, does not have it as a contact, or is out of range (the gateway cannot tell which) |
| `waiting` | not asked yet, or not since its last answer more than a day ago |

Answers are current while at most one request in a row went unanswered and the last answer is at
most an hour old, or three of the contact's intervals if that is longer
([ADR 0024](decisions/0024-telemetry-values-in-home-assistant-expire.md)). The position counts
while the answers are current and the newest answer, or one within the last hour, carried one. A
restart of the gateway changes no state: the schedule is saved with how long ago each answer came.
When the radio does not come back after a restart, a companion's answers and its position still
expire at their bound: the gateway keeps the last document of each companion whose answers are
current and publishes it again with its new state, within a minute of the bound. It keeps that
document in its latest state until the radio's table is read again, also across further
restarts; an exclusion or inclusion meanwhile changes it too.

### Reading keys

`ch<channel>_<type>[_<n>][_<unit>]`, such as `ch2_temperature_c`: the channel (1 to 255), the
type's name in the CayenneLPP library, `_<n>` from 2 for a further reading of that channel and type
in one answer (counted without battery and MCU temperature: `ch1_temperature_2_c`), and the unit.
Channel 1 holds the companion's own readings; channels 2 and up the sensors it found at boot, in
that order, so a sensor missing at one boot moves the later ones and the old key goes absent. The
values are the integers sent divided as the type says, never rounded again.

| LPP type | `<type>` | Unit suffix |
| --- | --- | --- |
| 116 | `voltage` | `_v` |
| 117 | `current` | `_a` |
| 128 | `power` | `_w` |
| 103 | `temperature` | `_c` |
| 104 | `humidity` | `_percent` |
| 115 | `barometric_pressure` | `_hpa` |
| 121 | `altitude` | `_m` |
| 101 | `luminosity` | none: lux on most boards, 0–100 % on the T1000-E, which the gateway cannot tell apart |
| 100 | `generic` | none |
| 2 | `analog_input` | none |
| 120 | `percentage` | `_percent` |
| 118 | `frequency` | `_hz` |
| 130 | `distance` | `_m` |

GPS is the position, never a reading; other types are not published.
## Nodes heard: `nodes`

Nodes the radio heard advertising but did not add (it adds contacts only on request), most
recently heard first: the last 50 of the last 7 days, at most 15 KiB. A contact that leaves the
table is listed again while it counts as heard (among the last 50, within 7 days), so it can be
added back. **No positions**: the gateway does not keep where strangers are.

```json
{"total": 1, "truncated": false, "look_alike": [], "look_alike_total": 0,
 "nodes": [{"key": "c3d4…", "prefix": "c3d4e5f6a7b8", "name": "Hilltop", "type": "repeater",
            "first_heard_at": "2026-10-17T20:00:00+00:00", "look_alike": false,
            "heard_at": "2026-10-18T09:00:00+00:00", "heard_via": "advert", "hops": 3,
            "snr_db": 5.5, "rssi_dbm": -95}]}
```

`look_alike` marks a node whose 6-byte prefix another node has: a contact, a protected key or
another heard node. The radio names a node by that prefix in messages and sends, so it could not
tell them apart ([ADR 0009](decisions/0009-inbound-commands-from-allowlisted-contacts.md)): a
look-alike of a contact or a protected key is refused; of two heard nodes with one prefix, the one
added first keeps it and the other is refused from then on, so compare the full key with the
node's own screen before adding a flagged one. The top-level list names up to 16 of them,
`look_alike_total` counts them all. An older advert of a heard node does not replace the record
of its newest one, also not after a restart.

`hops`, `snr_db` and `rssi_dbm` come from the radio's packet log of the advert; that log is not
authenticated, so treat them as a hint.

## History: `history`

The latest messages in and out (up to 50, at most 15 KiB), newest first. They are kept in the
gateway's history database, so they survive a restart ([ADR 0016](decisions/0016-history-in-a-local-sqlite-database.md),
[configuration](configuration.md)).

```json
{"total": 2, "truncated": false,
 "messages": [{"at": "2026-10-18T09:01:00+00:00", "direction": "out", "kind": "contact",
               "contact_key": "a1b2…", "channel_index": null, "name": "Alice",
               "text": "Back soon", "state": "confirmed", "hops": null, "snr_db": null},
              {"at": "2026-10-18T09:00:02+00:00", "direction": "in", "kind": "channel",
               "contact_key": null, "channel_index": 0, "name": "Carol", "text": "On my way",
               "state": null, "hops": 1, "snr_db": 6.25}]}
```

`name` is the sender's contact name, for channels the **claimed** name from the text; for
outgoing messages the recipient. `state` is the final delivery state of an outgoing message
(`confirmed`, `failed`, `expired`, or `sent` for a channel). Requests refused at once
(`rejected`, `duplicate`) are not in the history.

## Contact and node events: `contact`, `node`

```json
{"event_type": "added", "key": "a1b2…", "prefix": "a1b2c3d4e5f6", "name": "Alice",
 "type": "chat", "request": null, "id": null, "reason": null}
{"event_type": "new", "key": "c3d4…", "prefix": "c3d4e5f6a7b8", "name": "Hilltop",
 "type": "repeater", "hops": 3, "snr_db": 5.5, "rssi_dbm": -95,
 "heard_at": "2026-10-18T09:00:00+00:00", "look_alike": false}
```

`contact` reports a contact that appeared in or left the radio's table (`added`, `removed`), and
the results of contact requests (`refused`, `failed`, and `added` or `removed` with `request` and
`id`; see [managing contacts](#managing-contacts-add_contact-and-contactskeyremove)), also of
telemetry requests (`excluded`, `included`, with `request: "telemetry"`; see
[excluding](#excluding-a-contact-from-telemetry-contactskeytelemetry)). After a
start of the gateway, it reports what changed while it was stopped: contacts that are new, and
contacts that are gone (`name` and `type` are then `null`). `hidden` and `shown` report a
contact device deleted in Home Assistant and shown again; the contact stays on the radio. `node` reports a node heard for the first time
in 7 days; at most one event every 5 seconds on average, in bursts of up to 10, so a flood of
adverts with invented keys does not flood the bus.

## Managing contacts: `add_contact` and `contacts/<key>/remove`

Requests to change the radio's contact table ([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md),
on unless `[contacts] manage = false`). Requests must not be retained; one published while the
gateway is offline is lost (clean session), and a stop drops those still queued.

`add_contact` takes, as text, one of:

* a public key: 64 hex digits, in any case;
* a contact link: `meshcore://contact/add?public_key=…&name=…&type=…` (the MeshCore app's QR
  codes): exactly one `public_key`, at most one `name` and one `type` (1 companion, 2 repeater,
  3 room server, 4 sensor; without one a companion);
* a contact card: `meshcore://` and the hex of an advert packet, as the app exports a contact.

`homeassistant/add_node` takes the label of a heard node, `<first 12 key digits>…<last 8> <name>`
(room servers and sensors with ` (room)` or ` (sensor)` added), as the select *Add heard node* of
Home Assistant offers it; the gateway resolves the digits among
the nodes it heard and the contacts (the name is not compared), and adds the node with its own
advert. It is subscribed only with Home Assistant's discovery on.

`contacts/<key>/remove` takes any payload; `<key>` is the full key in lower-case hex. Each
request may be a JSON object instead (its first character `{`): `{"contact": "…", "id": "…"}` and
`{"id": "…"}`. The `id` (1 to 64 of `A-Z a-z 0-9 . _ : -`) comes back in the request's result;
without one the gateway makes one of 8 hex digits. Other keys are ignored.

A request is checked at once, in this order: size (1 KiB), UTF-8, the JSON envelope, retained,
the key, text or label, `[contacts] manage`, the gateway's own key, protected (remove), and a free
place in the queue (8 requests wait at most). Then one at a time: the radio must be connected and on
tested firmware (as the managed profile: `radio.profile.manage = "always"` allows any), a token
of the radio-work budget is taken (10 at once, then 1 a minute: every request that reaches the
radio reads its table), the table is read again if it changed, and the rules apply to it. A
request is written at most once and never repeated; then the table is read back.

**Names.** A node the gateway heard advertising is added with its name, type and advert time as
the radio checked them (a later replay of an older advert is then dropped by the radio). A link
or card only claims name and type: they are written as given (on one line, at most 31 bytes),
the time as 0, and the node's first advert replaces them (`verified` turns `true`). A contact card's
signature is not checked. A bare key without a name gets its first 12 digits.

**Results.** A request ends in one `contact` event (for the exceptions see below) that carries `request` (`add`, `remove`),
`id`, `key` (`null` if the request named no valid key), `prefix`, and `name` and `type` as known:

| `event_type` | Meaning |
| --- | --- |
| `added`, `removed` | the table read shows the change: the request's result. The device, the documents and the recipient exist when it arrives |
| `refused` | a check said no; **nothing was written** |
| `failed` | the write went to the radio, and the change is not confirmed |

| Reason (`refused`) | Request | Meaning |
| --- | --- | --- |
| `too_large`, `invalid_utf8`, `retained` | both | as for send requests |
| `invalid_request` | both | a bad JSON envelope or `id` |
| `invalid_link` | add | no key, contact link or contact card (an empty text too) |
| `unsupported_link` | add | another `meshcore://` link (channels are not managed) |
| `unsupported_type` | add | a type other than 1 to 4 |
| `unknown_node` | add | the label names no heard node or contact, or more than one |
| `unknown_contact` | remove | the topic level is no key as the gateway writes it, or the contact is not in the table |
| `management_off` | both | `[contacts] manage = false` |
| `own_key` | add | the gateway's own key (its link pasted back) |
| `protected` | remove | a contact target or command sender, also with `protect_contacts = false` |
| `busy` | both | 8 requests are waiting |
| `no_radio` | both | the radio is not connected, or the connection ended before the write was sent |
| `untested_firmware` | both | firmware the gateway is not tested with, unless `radio.profile.manage = "always"` |
| `rate_limited` | both | the radio-work budget is spent |
| `already_contact` | add | the key is in the table (adding it would overwrite every field of it) |
| `look_alike` | add | another key in the table or a protected key has the same first 6 bytes |
| `table_full` | add | the table is full; the gateway never lets the radio make room |
| the radio's error name | both | the radio refused the table read |

| Reason (`failed`) | Meaning |
| --- | --- |
| `table_full`, `not_found`, `illegal_arg`, `unsupported_cmd`, `disabled`, `error_<code>` | the radio refused the write |
| `not_confirmed` | the radio answered OK, but its table does not show the change |
| `outcome_unknown` | the connection ended after the write went out; the next table read shows the truth as `added` or `removed` with `request: null` |
| `internal_error` | a bug; see the log |

`refused` events are limited to 20 per second (bursts of 100); the log counts the rest at most
once a minute, so a flood of bad requests cannot flood the bus. Such a request then has no event,
nor has one still queued when the gateway stops; otherwise every request has exactly one.

What follows a removal: the radio drops the node's direct messages; messages queued to it fail
with `unknown_contact`; a removed look-alike of a command sender keeps that sender
`sender_ambiguous` for 24 hours (ADR 0009). A removed contact is offered again among the heard
nodes while it counts as heard.

## Excluding a contact from telemetry: `contacts/<key>/telemetry`

The gateway asks every companion of its radio for its telemetry unless the household excluded it
([ADR 0018](decisions/0018-telemetry-pulled-from-the-households-companions.md)).
`contacts/<key>/telemetry`, `<key>` the full key in lower-case hex, takes `on` or `off` (spaces
around them are ignored), or a JSON object such as `{"telemetry": "off", "id": "x1", "forget":
true}`: `id` as for the other contact requests, and `forget` (only with `off`) also deletes the
contact's readings and its positions from telemetry from the gateway's history. Requests must not
be retained.

The request needs neither the radio nor `[contacts] manage`, and is taken at once, also while
telemetry is off. `off` is taken for any key, also one that is not on the radio yet, so a contact
can be excluded before it is added; a protected contact can be excluded too. It is checked in this
order; the first check that fails is the `reason` of a `refused` event:

| Reason | Check |
| --- | --- |
| `too_large`, `invalid_utf8` | as for send requests |
| `invalid_request` | a JSON object without a string `telemetry`; a bad `id`; `forget` not `true` or `false`, or `true` with `on`; a value other than `on` or `off` |
| `retained` | the request was retained |
| `rate_limited` | more than one telemetry request a second, beyond a burst of 30 (every request that got this far counts) |
| `unknown_contact` | the topic level is not a key as the gateway writes it; for `on`: neither a contact of the table as last read nor an excluded key |
| `not_a_companion` | the table as last read has the key as a repeater, room server or sensor |
| `too_many` | `off` for a key not yet excluded while 1024 keys are |

Then, at once: the exclusion is stored, the contact's document (if it is on the radio) and the
index are published, the history's rows are deleted if `forget` asks so, and the event follows. It
carries `request: "telemetry"`, `id`, `key`, `prefix`, and `name` and `type` as the radio's table
has them (`null` for a key not on the radio):

| `event_type` | `reason` | Meaning |
| --- | --- | --- |
| `excluded` | `null` | stored (and the history deleted, if asked) |
| `excluded` | `not_saved` | in effect, but not stored yet: the gateway writes it again every minute until it is (the log says when), so it is lost only if the gateway stops first |
| `excluded` | `not_forgotten` | in effect, but `forget` found no working history (off, broken, or of a newer version). It comes before `not_saved`: if the exclusion is not stored yet either, the gateway writes it again as for `not_saved` (the log says when) |
| `included` | `null` | stored |
| `failed` | `state_unwritable` | `on` could not be stored, or the file cannot be read: nothing changed, the contact stays excluded |

A repeated request gives the same event again, except `on` for a key that is not on the radio:
once it is no longer excluded, a repeat is refused `unknown_contact`. The file is written only
when the set changes. Refused requests share the limit of the `refused` events above.

**What an exclusion does.** The gateway asks the contact no more: a request waiting for the radio
is withdrawn, an answer in flight is dropped. It forgets the contact's battery, readings,
`readings_at`, `position_received_at` and a position from telemetry; the position its adverts
carried takes the place, which can be old. `heard_at` and `heard_via` of its last answer stay. Its
rows in the history stay until `readings_days` and `positions_days` (unless `forget`), and are not
restored while it is excluded. An inclusion makes it `waiting`, and it is asked at once, as the
airtime lanes allow.

**Where it is kept.** In `telemetry-excluded.json` in the state directory
([deployment.md](deployment.md#4-operations)): also when the contact leaves the table and
comes back, while telemetry is off, and across restarts. A file that cannot be parsed is left as
it is and pauses telemetry (`telemetry_reason` `exclusions_unreadable`) until it is fixed or
deleted and the gateway restarted; meanwhile `off` is in effect only until the gateway stops,
`on` fails, and no contact gets back its readings or a position from telemetry from the history.

## Telemetry events: `telemetry`

Not retained. A companion stopped answering, or answers again:

```json
{"event_type": "unanswered", "key": "a1b2…", "prefix": "a1b2c3d4e5f6", "name": "Alice",
 "state": "away"}
```

| `event_type` | When | `state` |
| --- | --- | --- |
| `unanswered` | a companion that answered within the day left its second request in a row unanswered | `away` |
| `answered` | an answer after two or more unanswered requests in a row, or the contact's first answer since it was added or included again | `answering`, `no_position` |

At most one `unanswered` per absence: the count starts again only with an answer. None while the
contact is excluded, none for a request that was not sent (the radio away, telemetry paused, no
path, the gateway stopped), and none for a companion that never answered. An event never carries a
position or a reading; it comes after the contact's document. `name` is that of the radio's table.

## Radio profile: `settings` and `setting`

What the managed profile found and did at the last connect
([ADR 0020](decisions/0020-managed-radio-profile-for-the-observer.md),
[configuration.md](configuration.md#radioprofile)), published after each connect:

```json
{"checked_at": "2026-10-18T09:00:00+00:00", "status": "drift", "mode": "auto",
 "observing": false, "firmware_code": 14, "firmware_tested": true,
 "settings": {
   "manual_add": {"live": true, "desired": true, "state": "written"},
   "autoadd": {"live": [], "desired": [], "state": "ok"},
   "telemetry": {"live": {"base": "deny", "location": "deny", "environment": "deny"},
                 "desired": {"base": "deny", "location": "deny", "environment": "deny"},
                 "state": "ok"},
   "advert_location": {"live": "none", "desired": "none", "state": "ok"},
   "duty_cycle_percent": {"live": 10.0, "desired": 10.0, "state": "written"},
   "name": {"live": "Kitchen radio", "desired": "Gateway", "state": "drift"}},
 "own_position": false,
 "protected_contacts": {"added": [], "favoured": ["a1b2…"], "missing": [], "failed": [],
                        "not_favourite": []}}
```

| Key | Meaning |
| --- | --- |
| `status` | `ok` (the radio has the profile), `written` (set at this connect), `drift` (the name was changed on the radio and is kept), `differs` (a setting or a protected contact differs, but the profile only observes), `problem` (a setting or a protected contact was refused, a setting did not read back as written or does not take, or is unsupported); when several apply, the first of `problem`, `differs`, `drift`, `written`, `ok` |
| `settings.<name>.live`, `.desired` | after the writes; `desired` is `null` for a setting left as found, `live` `null` for one the firmware does not report |
| `settings.<name>.state` | `ok`, `written` (set and read back), `drift`, `differs`, `failed` (refused by the radio), `not_taken` (written, but reads back otherwise), `not_converging` (no longer written: it did not take at three connects in a row), `unsupported`, `unmanaged` (left as found), `unknown` (the firmware does not report it) |
| `autoadd` | `overwrite_oldest` and the contact types the radio adds by itself |
| `own_position` | the radio keeps a position of its own (reported, never cleared by the gateway) |
| `protected_contacts` | keys added to the radio, made favourites, refused by the radio (`failed`, usually a full table), and while only observing: missing from the radio, or on it but no favourite (`not_favourite`) |

`setting` reports what happened as an event, with `setting`, `reason` for `written`, `failed`
and `not_taken` (`initial`: the gateway has nothing stored for this radio; `configured`: the
configuration asks for a new value; `drift`: the radio was changed), `live` (after the writes) and
`desired`. `written`, `failed` and `not_taken` come at each connect that writes; `drift` and
`not_converging` once per run of the gateway and radio, and again when the value changes:

```json
{"event_type": "written", "setting": "manual_add", "reason": "drift", "live": true,
 "desired": true}
```

## Radio diagnostics: `rx`

With `diagnostics.publish_rx_log = true`, one event per packet the radio receives, before it
decides whether the packet is for it — useful to compare antennas or locations:

```json
{"snr_db": 7.75, "rssi_dbm": -97, "length": 8, "raw": "0d00deadbeef00a5",
 "received_at": "2026-10-18T09:00:00+00:00"}
```

## Securing the broker

Give the bridge its own user. With Mosquitto, an ACL along these lines keeps clients apart:

```text
user meshcore-home-gateway
topic readwrite meshcore/radio1/#
topic readwrite homeassistant/device/meshcore_home_gateway_radio1/#
topic read homeassistant/status

user homeassistant
topic readwrite homeassistant/#
topic read meshcore/radio1/#
topic write meshcore/radio1/send/#
topic write meshcore/radio1/contacts/+/send
topic write meshcore/radio1/add_contact
topic write meshcore/radio1/contacts/+/remove
topic write meshcore/radio1/contacts/+/telemetry
topic write meshcore/radio1/homeassistant/show_hidden
topic write meshcore/radio1/homeassistant/add_node

user alarm-script
topic write meshcore/radio1/send/alerts
```

Write the node ID as configured, also in the discovery topics: for node `radio-1` it is
`homeassistant/device/meshcore_home_gateway_radio-1/config`. Only Home Assistant's entity IDs
have `_` for `-`.

Home Assistant needs `homeassistant/#` for discovery and its own birth and status messages. Only
the gateway may write the event topics (`received`, `command`, `delivery`, `contact`, `node`,
`telemetry`) and
the documents, because automations and dashboards trust them. When commands are configured, the ACL is not optional: any client that can write
`command` can trigger the automations.

paho reads every message completely before the gateway can check its size (1 KiB for a send
request), so a client could exhaust the gateway's memory with one huge request. If no other
client on the broker needs large messages, cap them in `mosquitto.conf`, for example
`max_packet_size 65536`.
