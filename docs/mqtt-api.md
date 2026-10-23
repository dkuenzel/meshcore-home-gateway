# MQTT API

All topics hang below `<base_topic>/<node_id>`; with the defaults of the example configuration that
is `meshcore/radio1`. Everything uses QoS 1, except the `command` events (QoS 0, see there). The
API is a public interface under semantic
versioning ([ADR 0003](decisions/0003-mqtt-as-the-message-bus.md)).

| Topic | Direction | Retained | Payload |
| --- | --- | --- | --- |
| `availability` | bridge → | yes | `online` / `offline`; last will `offline` |
| `state` | bridge → | yes | JSON state document |
| `send/<alias>` | → bridge | must not be | UTF-8 text |
| `delivery` | bridge → | no | JSON, `event_type` = delivery state |
| `received` | bridge → | no | JSON, `event_type` = `contact` or `channel` |
| `command` | bridge → | no | JSON, `event_type` = command word |
| `rx` | bridge → | no | JSON per received packet, only with `publish_rx_log` |
| `<prefix>/device/meshcore_home_gateway_<node>/config` | bridge → | no | Home Assistant discovery |

Times are ISO 8601 in UTC with seconds. Unknown values are `null`; keys are always present.

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
| `rejected` | not accepted | `unknown_target`, `retained`, `too_large`, `invalid_utf8`, `empty`, `too_long`, `queue_full` |

`id` is a random identifier per request. In final events, `attempt` is the number of
transmissions so far (0 if the message never left). Retries of a direct message keep the radio
timestamp, so a recipient that got the message but whose acknowledgement was lost may show it
twice; the same holds for an attempt that a radio disconnect interrupted after the radio had sent
it.

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

The radio acknowledges a direct message when it receives it, so a rejected command does not come
back by itself: sent again, it has a newer timestamp. An identical copy of the packet is a replay.
The bridge drops it as a repeat for an hour; after `broker_unavailable` it has also stored the
timestamp, so a later copy is a `replay`. After `sender_unverified` (the sender was not verified)
and `state_unwritable` it has not: a copy that arrives after that hour, or after a restart of the
gateway, is judged afresh and accepted if it is still within `max_age_s` (always with
`max_age_s = 0`; [design §7.4](design.md#74-residual-risks)).

## State: `state`

Retained; published at start, on every change of the radio link and every 60 s.

```json
{"radio": "connected", "battery_v": 4.012, "airtime_1h_s": 3.2, "airtime_available_s": 9.4,
 "airtime_budget_s_per_hour": 36.0, "queue": 0, "radio_name": "Gateway",
 "public_key": "…", "model": "…", "firmware": "v1.18.0", "frequency_mhz": 869.618,
 "bandwidth_khz": 62.5, "spreading_factor": 8, "coding_rate": 8, "tx_power_dbm": 22,
 "version": "0.1.0"}
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
topic write homeassistant/device/meshcore_home_gateway_radio1/config
topic read homeassistant/status

user homeassistant
topic readwrite homeassistant/#
topic read meshcore/radio1/#
topic write meshcore/radio1/send/#

user alarm-script
topic write meshcore/radio1/send/alerts
```

Home Assistant needs `homeassistant/#` for discovery and its own birth and status messages. Only
the gateway may write the event topics (`received`, `command`, `delivery`), because automations
trust them. When commands are configured, the ACL is not optional: any client that can write
`command` can trigger the automations.

paho reads every message completely before the gateway can check its size (1 KiB for a send
request), so a client could exhaust the gateway's memory with one huge request. If no other
client on the broker needs large messages, cap them in `mosquitto.conf`, for example
`max_packet_size 65536`.
