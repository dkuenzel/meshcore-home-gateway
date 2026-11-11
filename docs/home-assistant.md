# Home Assistant

The gateway announces itself through MQTT device discovery
([ADR 0010](decisions/0010-home-assistant-through-mqtt-device-discovery.md)). Home Assistant
needs nothing besides its MQTT integration: no custom integration, no HACS repository, no app and
no YAML. Automations are built in the automation editor with the trigger *Event received* and the
action *Send a notification message*.

Public interface: see [compatibility](development.md#compatibility).

## Requirements

* Home Assistant 2026.8 or newer, tested with 2026.10 and 2026.11.
* The MQTT integration connected to the same broker, with discovery and the birth message enabled
  (both are the defaults).
* In the gateway configuration: `homeassistant.discovery = true` (the default), `prefix` equal to
  the discovery prefix and `status_topic` equal to the birth message topic in the MQTT options of
  Home Assistant (defaults `homeassistant` and `homeassistant/status`).
* With a topic ACL on the broker, Home Assistant's user needs `readwrite` on `homeassistant/#`
  ([mqtt-api.md](mqtt-api.md#securing-the-broker)), for the switch *Telemetry*
  `topic write <base_topic>/<node_id>/contacts/+/telemetry`, and for writing and sending from
  Home Assistant `topic write <base_topic>/<node_id>/homeassistant/draft/+/+/request`, which the
  example ACL of earlier versions lacks: without them the switch and *Send* do nothing, and only
  the broker's debug log shows why (`log_type debug` or `all`; in the Mosquitto add-on, `debug`
  in its option `log_type`, not `debug: true`, which also logs passwords).
* The generated dashboard ([below](#dashboard)) needs Home Assistant 2026.10 or newer: it shows
  cards by template conditions, which the frontend evaluates since 2026.10. The gateway and its
  entities keep 2026.8.

Why 2026.8: device-based discovery exists since 2024.11, the entity IDs below (from
`default_entity_id`) since 2025.10, QoS 1 for every entity from the device level since 2026.5.2,
the `event.received` trigger without the Labs setting since 2026.7, and since 2026.8 every event
is a state change of its own, also two events within the same millisecond.

The Home Assistant releases each gateway release was tested with: the contract tests
([testing.md](testing.md#home-assistant-contract-tests)) run against the one their environment
pins, the release check against a real installation ([testing.md](testing.md#release-check); up to
1.0.0 the hardware checklist). The pull request of a release adds its row.

| Gateway | Contract tests | Release check |
| --- | --- | --- |
| 2.1.1rc2 | 2026.10.2, and 2026.11.0 from its own environment | the browser check of 2.1.1rc1 on 2026.10.2 and 2026.11.0; the maintainer's use before 2.1.1 |
| 2.1.1rc1 | 2026.10.2, and 2026.11.0 from its own environment | the browser check on 2026.10.2 and 2026.11.0; not finished: the clock fixes followed in 2.1.1rc2 |
| 2.1.0, the program of 2.1.0rc1 | 2026.10.2, and 2026.11.0 from its own environment | the browser check of 2.1.0rc1 on 2026.10.2 and 2026.11.0; the maintainer's use follows 2.1.0, as they decided |
| 2.1.0rc1 | 2026.10.2, and 2026.11.0 from its own environment | the browser check on 2026.10.2 and 2026.11.0; the maintainer's use follows 2.1.0, as they decided |
| 2.0.0, the program of 2.0.0rc3 | 2026.10.2 | 2026.11.0 |
| 2.0.0rc2 | 2026.10.2 | not finished: the clock correction followed in 2.0.0rc3 |
| 2.0.0rc1 | 2026.10.2 | stopped: the installer's self-test showed an empty report |
| 2.0.0a1 | 2026.10.2 | not run (pre-release) |
| 1.1.0a1 – 1.1.0a4 | 2026.10.0 | not run (pre-releases) |
| 1.0.0, the program of 1.0.0rc1 | 2026.10.0 | 2026.10 |
| 0.1.0a1 | 2026.10.0 | not run (pre-release) |

## Entities

One device for the gateway, named after `homeassistant.device_name`, and one for each contact of
the radio (next section). Once the radio has connected, the gateway's device page shows its model,
firmware and public key (as serial number). Its entities, with their IDs for node `radio1`:

| Entity | Platform | Content |
| --- | --- | --- |
| `binary_sensor.meshcore_home_gateway_radio1_radio` | connectivity, diagnostic | radio link up; attributes below |
| `sensor.meshcore_home_gateway_radio1_battery` | voltage (V), diagnostic | radio battery |
| `sensor.meshcore_home_gateway_radio1_radio_clock_offset` | duration (s), diagnostic | how far the radio's clock is ahead of the gateway's (negative: behind); the gateway moves it forward, and sets it back by restarting the radio when it is up to an hour ahead, or whole jumps of MeshCore's nRF52 clock ([deployment.md](deployment.md#5-troubleshooting)) |
| `binary_sensor.meshcore_home_gateway_radio1_radio_link_contested` | problem, diagnostic; only with a radio over TCP | *Radio link taken over*: on for an hour after another client took the TCP link to the radio, which lets it control the radio and read its private key ([deployment.md](deployment.md#5-troubleshooting)); a best effort, since a client that connects while the gateway is stopped leaves no trace; unknown while the radio is away or reports no uptime |
| `sensor.meshcore_home_gateway_radio1_airtime` | duration (s), diagnostic | airtime of the last hour |
| `sensor.meshcore_home_gateway_radio1_airtime_available` | duration (s), diagnostic | airtime that may be spent now; attribute `budget_s_per_hour` |
| `sensor.meshcore_home_gateway_radio1_queue` | diagnostic | messages waiting behind the one being sent: 0 while a single message waits for the radio, for airtime or for its acknowledgement; *Messages on their way* counts that one too (#112) |
| `binary_sensor.meshcore_home_gateway_radio1_degraded` | problem, diagnostic | *Gateway degraded*: on while a part of the gateway is not as it should be (the history broken, the state file unwritable, an exclusion unsaved, a bug caught in the last hour, the host holding it up, the disk nearly full); the `health` section as attributes ([mqtt-api.md](mqtt-api.md#state-state), [what to do](deployment.md#5-troubleshooting)); unknown with a gateway of an earlier version |
| `event.meshcore_home_gateway_radio1_message` | event: `contact`, `channel` | every received message |
| `event.meshcore_home_gateway_radio1_delivery` | event: `queued` … `rejected` | delivery steps of outgoing messages |
| `sensor.meshcore_home_gateway_radio1_message_history` | timestamp | *Message history*: the time of the newest message in or out, `unknown` while there is none; the `history` document as attributes `messages` (the latest 50 at most, newest first, with their keys as in [mqtt-api.md](mqtt-api.md#history-history)), `total` and `truncated`. It stays available while the gateway is offline, so the messages stay readable |
| `sensor.meshcore_home_gateway_radio1_pending_messages` | count | *Messages on their way*: the messages the gateway accepted and has not delivered or given up yet, the one being sent included; the `pending` document as attributes `messages` (oldest first, with `step` `queued` or `sent` and their keys as in [mqtt-api.md](mqtt-api.md#messages-on-their-way-pending)), `total` and `truncated`. A message `queued` for more than a minute waits for the radio or for airtime. It stays available while the gateway is offline |
| `sensor.meshcore_home_gateway_radio1_configuration` | timestamp, diagnostic | *Configuration*: when the gateway started; the settings in effect as attributes (`targets` with each one's `room`, `messages`, `contacts`, `telemetry`, `homeassistant`, `radio`, `airtime`, `history`, `commands`), as in [mqtt-api.md](mqtt-api.md#configuration-config): never a host, password, key or command word. Read only: Home Assistant cannot change them |
| `event.meshcore_home_gateway_radio1_command` | event: the configured words | accepted commands (only with `commands.words`) |
| `notify.meshcore_home_gateway_radio1_send_<alias>` | notify, one per target | sends the message text to that target; `-` in an alias becomes `_`, so two aliases that differ only there get the same IDs, the later ones ending in `_2` (the gateway warns, [configuration.md](configuration.md#targetsalias)) |
| `text.meshcore_home_gateway_radio1_write_<alias>` | text, one per target not in `hidden_targets` | *Write to …*: the draft of a message to that target, which the gateway keeps until it is sent ([below](#writing-and-sending)); `max` is the room of a text to the target in bytes |
| `button.meshcore_home_gateway_radio1_write_<alias>_send` | button, one per target not in `hidden_targets` | *Send what is written to …*: sends that draft |
| `sensor.meshcore_home_gateway_radio1_rx_rssi`, `..._rx_snr` | signal strength, diagnostic | last received packet (only with `diagnostics.publish_rx_log`; disabled until enabled in Home Assistant) |
| `sensor.meshcore_home_gateway_radio1_contacts` | count | contacts in the radio's table; attribute `max`, and `manage_reason`: why requests to add or remove contacts are refused now (`management_off`, `no_radio`, `untested_firmware`), `null` while they are taken |
| `sensor.meshcore_home_gateway_radio1_nodes` | count | nodes heard in the last 7 days that are not contacts |
| `event.meshcore_home_gateway_radio1_contact` | event: `added`, `removed`, `hidden`, `shown`, `refused`, `failed`, `excluded`, `included`, `position_in_advert` | a contact added to or removed from the radio, the result of a contact request (`refused`, `failed`, with `request`, `id` and `reason`), a contact excluded from telemetry or included again, a device hidden in Home Assistant and shown again, or a household companion whose adverts carry its position ([example](#examples)) |
| `event.meshcore_home_gateway_radio1_node` | event: `new` | a node heard for the first time |
| `sensor.meshcore_home_gateway_radio1_noise_floor` | signal strength (dBm), diagnostic | the radio's noise floor |
| `sensor.meshcore_home_gateway_radio1_radio_airtime` | duration (s), diagnostic | what the radio itself counted as sent in the last hour, adverts and acknowledgements included; attributes `window_s`, `airtime_factor`, `duty_cycle_percent` |
| `binary_sensor.meshcore_home_gateway_radio1_remote_cli` | problem, diagnostic | a contact may use the radio's command line over the air; attribute `contacts` ([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md)) |
| `binary_sensor.meshcore_home_gateway_radio1_advert_position` | problem, diagnostic | *Companion position in adverts*: a companion not excluded from telemetry sends its position in its newest advert, which everyone in range can read (until the gateway has checked that advert: on while its last check carried one or the radio's record holds one); attributes `contacts` (at most 16 keys) and `total` |
| `button.meshcore_home_gateway_radio1_show_hidden` | configuration | shows the contacts deleted in Home Assistant again (next section) |
| `select.meshcore_home_gateway_radio1_add_node` | select, configuration | *Add heard node*: adds the heard node picked to the radio ([below](#adding-and-removing-contacts)) |
| `text.meshcore_home_gateway_radio1_add_contact` | text, configuration | *Add contact (key or link)*: adds the contact of a public key or contact link |
| `binary_sensor.meshcore_home_gateway_radio1_look_alike` | problem, diagnostic | a heard node has the first 6 key bytes of a contact, a protected key or another heard node, so the radio could not tell them apart; attributes `nodes` (at most 16 keys) and `total` |
| `sensor.meshcore_home_gateway_radio1_profile` | enum, diagnostic: `ok`, `written`, `drift`, `differs`, `problem` | the managed radio profile at the last connect; the `settings` document as attributes ([configuration.md](configuration.md#radioprofile)) |
| `event.meshcore_home_gateway_radio1_setting` | event: `written`, `failed`, `not_taken`, `not_converging`, `drift` | a radio setting set, refused, not taken, given up or found changed |
| `sensor.meshcore_home_gateway_radio1_uptime`, `..._packets_received`, `..._packets_sent`, `..._receive_errors` | diagnostic | the radio's uptime and packet counters; disabled until enabled in Home Assistant |
| `sensor.meshcore_home_gateway_radio1_telemetry_status` | enum, diagnostic: `off`, `on`, `paused`, `unsupported` | whether the gateway asks the companions for telemetry; attribute `reason` while `paused` ([below](#telemetry-of-the-households-companions)) |
| `sensor.meshcore_home_gateway_radio1_telemetry_airtime`, `..._telemetry_mesh_airtime` | duration (s), diagnostic | *Telemetry airtime last hour*: the gateway's own transmit time for telemetry (its lane allows 18 s an hour); *Telemetry mesh airtime last hour*: the airtime its requests caused in the mesh, as estimated (90 s an hour); disabled until enabled in Home Assistant |
| `event.meshcore_home_gateway_radio1_telemetry` | event: `answered`, `unanswered` | a companion left two requests in a row unanswered, or answers again; attributes `key`, `prefix`, `name`, `state` |

A `-` in the node ID becomes `_` in the entity IDs, as in an alias: node `radio-1` gives
`sensor.meshcore_home_gateway_radio_1_battery`. The discovery topic, the device and the unique
IDs keep the node ID as it is, so two gateways `radio-1` and `radio_1` on one broker are two
devices, but the entity IDs of the one announced second end in `_2`; pick node IDs that differ in
more than that. Node IDs and aliases that Home Assistant would shorten further (a trailing `-` or
`_`, two of them in a row) are refused by the configuration check.

All entities share the gateway's availability topic and turn `unavailable` when it is offline,
except *Message history* and *Messages on their way*, which stay readable;
the controls that change the radio's contacts also while the radio is not connected or contact
management is off (also on untested firmware). The state values come from retained documents, so they are back right after a
restart of either side.

### Language of the names

The names the gateway sends follow `homeassistant.language`, English or German
([configuration.md](configuration.md#homeassistant)): the entity names, the models of the
contacts' devices (*Companion*, *Repeater*, *Room server*, *Sensor*), the reading types
(*Temperature 2*) and the default device name `MeshCore radio`. This guide gives them in English.
The same in every language:

* the entity IDs and unique IDs, so automations, dashboards, statistics and recorder settings
  keep working when the language changes;
* the event types, the options of the select and of the enum sensors, which Home Assistant shows
  as they are (MQTT entities have no translations), attribute keys, units and classes;
* data: a contact's name, a target's alias in *Send to …*, a device name you set, and the type of
  a node this version does not know.

A contact's SNR sensor has no name of its own: Home Assistant names it after its class, in its own
language. A contact's notify entity has the contact's name.

A change of language renames the display names at the next start of the gateway. Home Assistant
takes the new names of the entities it knows and keeps their IDs, their history and the names you
gave them; no entity is added or removed.

## Contacts

Every contact in the radio's table gets a device of its own, listed under *Connected devices* on the
gateway's page ([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md)). A
contact added to the radio has its device within seconds; a contact removed loses it. Nothing has to
be configured. One exception: a contact target or command sender that the radio profile puts on
the radio before it was ever heard gets its device with its first advert, so its entity IDs come
from its own name, not from a placeholder; one the radio already holds with a position (written
by another client of the radio, such as a phone app) gets it at once. The entities, for a companion named *Alice*:

| Entity | Platform | Content |
| --- | --- | --- |
| `notify.mesh_alice` | notify, named like the device | sends a direct message, through the same queue, airtime budget and duplicate check as a target |
| `text.mesh_alice_write` | text | *Write*: the draft of a direct message to the companion ([below](#writing-and-sending)); not for a contact target's key, whose chat is the target's |
| `button.mesh_alice_write_send` | button | *Send what is written*: sends that draft |
| `event.mesh_alice_message` | event: `contact` | each message from the contact, with the attributes of `received` |
| `sensor.mesh_alice_last_heard` | timestamp | when the gateway last heard the contact (advert, message; its telemetry answers at most once an hour) |
| `sensor.mesh_alice_hops` | diagnostic | repeaters the last advert or message passed (*unknown*: direct, or not known) |
| `sensor.mesh_alice_snr` | signal strength (dB), diagnostic | its signal-to-noise ratio, of its last advert or message |
| `device_tracker.mesh_alice` | GPS, attributes `latitude`, `longitude`, `altitude_m`, `position_at` (when it last changed), `position_source` (`advert`, `telemetry`); `gps_accuracy` 15 for a position from telemetry | the contact's position; only once one is known; `unavailable` while no position is known: a position from telemetry that is not current, or one from adverts that a checked advert no longer carries |
| `button.mesh_alice_remove` | configuration | *Remove from radio* ([below](#adding-and-removing-contacts)); `unavailable` for a contact target or command sender |
| `switch.mesh_alice_telemetry` | switch, configuration | *Telemetry*: on while the gateway may ask the companion for telemetry, off excludes it ([below](#telemetry-of-the-households-companions)); companions, and sensors with `[telemetry] ask_types` |
| `sensor.mesh_alice_telemetry_state` | enum | *Telemetry state*: whether its answers are current, and why not; companions, and sensors with `[telemetry] ask_types` |
| `binary_sensor.mesh_alice_advert_position` | problem | *Position in adverts*: on while the companion's newest advert carries its position, which everyone in range can read, as the gateway checked it; while that advert is not checked (before the first check, or one the gateway missed), on while the last check carried one or the radio's record holds a position from an earlier advert; companions only |
| `sensor.mesh_alice_battery` | voltage (V) | its battery, from its telemetry answers |
| `sensor.mesh_alice_mcu_temperature` | temperature (°C), diagnostic | its processor's temperature |
| `sensor.mesh_alice_ch2_temperature`, … | the reading's class and unit | each further reading of its answers, such as those of an environment sensor on channel 2 |

Components exist only where they have data: repeaters and sensors get no notify, draft and
message entities, room servers no notify and draft entities, and the tracker appears with the
first position, from the contact's telemetry ([companions.md](companions.md), `[telemetry]`) or
its adverts.
*Battery*, *MCU temperature* and the reading sensors appear with the first telemetry answer that
carries them, at most 16 readings per contact. Every companion has the switch *Telemetry*,
*Telemetry state* and *Position in adverts* at once, also while telemetry is off, so it can be
included or excluded before it is asked; with `[telemetry] ask_types` a sensor has the first
two.
Every contact has the remove button. A component, once there, stays as long as the
contact does (Home Assistant would clear the whole device when the gateway removed a disabled
one). The tracker is the only entity with coordinates,
so Home Assistant's map shows each contact once; it turns `unavailable` while no position is known.
Its attributes change when telemetry reports a move of 20 m or more, when an advert carries a
changed position, and when the position switches between adverts and telemetry, so a companion at
rest adds no state per answer. With `gps_accuracy` Home Assistant counts a companion up to 15 m
outside a zone's radius as inside it. A position from telemetry expires when the companion stops
answering ([below](#telemetry-of-the-households-companions)); one from adverts stays while the
last advert the gateway checked carried it, or, before it has checked one (after the update),
while the radio's record holds one (press *Advert* near the gateway after the update and after
switching sharing off, [companions.md](companions.md#positions-in-adverts)).

* **Entity IDs** start with `[homeassistant] contact_entity_prefix` (`mesh` by default, as in the
  table) and `_`, then the contact's name as it was when the device first
  appeared (letters and digits only, `ä` as `a`); a later rename changes the device's name, not the
  IDs, also when the contact leaves the table and comes back within 90 days (the gateway keeps
  the names of at most 1 000 contacts that left). Two contacts with the same name: the second gets the first four digits of its key added.
  Home Assistant numbers an ID that is already taken (another gateway with the same contact) with
  `_2`.
* **Too many contacts**: at most `homeassistant.contact_devices` (default 50) get a device, first
  protected contacts (targets, command senders), then favourites, then companions, then the most
  recently heard; a contact keeps its device as long as it stays in the table. `0` switches the
  contact devices off and removes them.
* **Delete a device** on its page in Home Assistant to hide that contact: the gateway notices and
  does not announce it again, until the contact leaves the radio's table or *Show hidden contacts*
  on the gateway's device is pressed. The contact stays on the radio. When the device comes back,
  Home Assistant restores its name, area and the entities' settings, except renamed entity IDs,
  which return to the IDs above. A deletion made while the gateway is not connected to the broker
  is not noticed; the device comes back with the next announcement. Hiding does not exclude
  from telemetry: a hidden companion, and one without a device because of the limit, has no
  switch *Telemetry* but is still asked; exclude it with
  `meshcore-home-gateway contacts telemetry KEY off`.
* A contact device whose entity was disabled by hand: removing it makes Home Assistant up to
  2026.10 log *No device components to cleanup*, which is harmless.

## Writing and sending

Every chat has a draft that the gateway keeps: a target that is not in `hidden_targets`
(*Write to …* and *Send what is written to …* on the gateway's device) and a companion that is no
contact target (*Write* and *Send what is written* on its device). Write the text into the text
entity and press Return, or leave the field: the gateway keeps the text and Home Assistant shows
it. *Send what is written* sends what the gateway keeps, as `notify.send_message` would: the same
queue, airtime budget, duplicate check, history and *Delivery* events. Once the message is queued,
the field is emptied; a message that is refused (too long with `truncate = false`, a duplicate)
stays in the field, to be changed, and *Delivery* says why. A *Send* with nothing written is
refused as `empty`.

* The draft is shared: everyone who sees the chat sees and sends the same draft, and a text
  another user stores replaces yours in your field.
* After a broker or Home Assistant restart, the field shows the draft the gateway keeps: read it
  before you send. A draft unchanged for 15 minutes is emptied, and so is every draft when the
  gateway stops or restarts.
* `max` counts bytes, the field characters: a text with accents or emoji can fit the field and
  still be too long for the radio; `[messages] truncate` then cuts it or refuses it.
* A *Send* pressed while Home Assistant has no connection to the broker fails with an error and
  is not sent later.
* Wait until the field is empty before you write the next message: a text typed in the moment
  after *Send* can be cut when the gateway empties the field, and the rest goes out with the next
  *Send*.
* A text that is only the word `unknown` or `unavailable` cannot be stored: Home Assistant's
  field refuses it.

Writing grants nothing new: every user can send with the notify entities already, and
`homeassistant.controls = false` changes neither. The topics, the request forms and the rules are
in [mqtt-api.md](mqtt-api.md#drafts-of-home-assistants-chats-homeassistantdraftkindchat).

## Adding and removing contacts

The gateway adds contacts to the radio and removes them on request, so a new handheld needs
neither a phone app nor a PC
([ADR 0017](decisions/0017-contacts-on-the-radio-each-with-its-own-device.md)). With
`[contacts] manage = false` the controls stay and are `unavailable`, and so they are while the
radio runs firmware the gateway is not tested with (as for the managed profile;
`radio.profile.manage = "always"` allows any): after a firmware update of the radio, until a
version of the gateway tested with it is installed. The attribute `manage_reason` of *Contacts*
says which ([deployment.md](deployment.md#5-troubleshooting)). Every request ends in an
event of *Contact* (`event.meshcore_home_gateway_radio1_contact`): `added` or `removed`, or
`refused` or `failed` with a `reason` ([mqtt-api.md](mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)).

**Add a node that advertised.** Bob sends an advert from his handheld. Within half a minute the
gateway's device offers him in *Add heard node*, as `b0b0b0b0b0b0…12121212 Bob`: the first 12 and
the last 8 digits of his key, then his name; room servers and sensors end in ` (room)` or
` (sensor)`. Compare the digits with his screen and pick him. The
radio gets his record as it heard it, signed by his key; his device appears within seconds, with
`notify.mesh_bob`. The select offers the 20 most recently heard companions, room servers and
sensors, ordered by name; repeaters only by key or link. Nodes whose first 6 key bytes another
node has are not offered: *Look-alike node heard* names them. A node whose name a contact, the
gateway or another heard node has ends in ` (name taken)`: anyone can name a node anything, so
compare its digits with the screen of the person it claims to be. A contact whose name, as a
person reads it, the gateway's device or a contact added before has gets a device named with its
key's first 8 digits, such as *Bob (b0b0b0b0)*. The select shows nothing chosen
after a pick, so the same choice can be made again.

**Add by key or link.** Paste a public key (64 hex digits) or a contact link
(`meshcore://contact/add?…`, the text of a MeshCore QR code, for example read with the phone's
camera) into *Add contact (key or link)* and press Return (↵), as on the dashboard; leaving the
field sends it too. Home Assistant refuses other text before it sends it.
A link's name and type are claims until the node's first advert replaces them; a key without a
name gets its first 12 digits. Contact cards (`meshcore://` and the hex of an advert) are not
taken: the field ends at 255 characters, and a card cut there would still be read, with a cut
name. Add them with `meshcore-home-gateway contacts add`. The field can keep the text after
sending; clear it before the next one.

**Remove.** *Remove from radio* on the contact's device page (*Configuration*). It is
`unavailable` for a contact target or command sender (protected), with management off or on
untested firmware, and while the radio or the gateway is away. After a removal the radio drops the node's direct messages, messages queued to
it fail with `unknown_contact`, and its device goes; while it counts as heard, it is offered again
in *Add heard node*, and adding it back restores the device with its name, area and labels.
*Delete* on the device page is something else: it hides the contact in Home Assistant only
([Contacts](#contacts)), and the contact stays on the radio.

**The gateway in the MeshCore app.** The radio drops direct messages from nodes it has not
added, and the app on a phone does the same: the phone's owner adds the gateway by scanning its
contact link as a QR code. The link is the attribute `contact_link` of
`binary_sensor.meshcore_home_gateway_radio1_radio`; a Markdown card shows it:

```yaml
type: markdown
title: Add the gateway in the MeshCore app
content: >-
  {% set link = state_attr('binary_sensor.meshcore_home_gateway_radio1_radio', 'contact_link') %}
  {% if link %}<ha-qr-code data="{{ link | e }}" scale="4"></ha-qr-code>{% else %}The radio is not
  known yet.{% endif %}
```

The link carries the radio's name and key, no position, and adding it needs no airtime. The
command line prints it too: `meshcore-home-gateway contacts link`.

**On a dashboard.** The device page presses *Remove from radio* without asking; on a dashboard the
button's row can ask first, and like the device page it cannot be pressed while the button is
`unavailable`:

```yaml
type: entities
title: Mesh contacts
entities:
  - entity: select.meshcore_home_gateway_radio1_add_node
  - entity: text.meshcore_home_gateway_radio1_add_contact
  - entity: button.mesh_bob_remove
    name: Remove Bob from the radio
    confirmation:
      text: Remove Bob from the radio? His direct messages are dropped until he is added again.
```

Keep the button's own row: a row of `type: button` with a `tap_action` stays pressable while the
button is `unavailable`, asks, and then does nothing.

**Who may.** Anyone who can use these controls changes the radio's table: in Home Assistant every
user can press a button and set a select or text. The broker's ACL decides which clients may write
the topics ([mqtt-api.md](mqtt-api.md#securing-the-broker)); `manage = false` switches the
requests off for all of them.

## Telemetry of the household's companions

The gateway asks the companions of its radio for their battery, their sensors and their position
([ADR 0018](decisions/0018-telemetry-pulled-from-the-households-companions.md)); a handheld answers
once it is set up as [companions.md](companions.md) says. Telemetry is on by default
([configuration.md](configuration.md#telemetry)). With `ask_types = ["chat", "sensor"]` the
household's sensors get the same entities, without *Position in adverts*
([companions.md](companions.md#sensors)). Alice's handheld, with its GPS on and a BME280
on channel 2, shows on its device page:

| Section | Entity | Shows |
| --- | --- | --- |
| Configuration | *Telemetry* (`switch.mesh_alice_telemetry`) | on: the gateway asks her handheld; off: it does not |
| Sensors | *Battery* (`sensor.mesh_alice_battery`) | 4.12 V |
| Sensors | *Telemetry state* (`sensor.mesh_alice_telemetry_state`) | `answering` |
| Sensors | *Temperature 2*, *Humidity 2*, *Pressure 2*, *Altitude 2* (`sensor.mesh_alice_ch2_temperature`, `sensor.mesh_alice_ch2_humidity`, `sensor.mesh_alice_ch2_barometric_pressure`, `sensor.mesh_alice_ch2_altitude`) | 21.3 °C, 55.5 %, 1013.2 hPa, 120 m |
| Diagnostic | *MCU temperature* (`sensor.mesh_alice_mcu_temperature`) | 23.4 °C |
| – | *Position* (`device_tracker.mesh_alice`) | on the map |

A repeater, room server or sensor contact gets none of these: only companions are asked. A
companion that never answers keeps the switch and the state.

**Readings.** Each reading of an answer has a sensor, named after its type and channel. Channel 1
holds the companion's own readings, channels 2 and up the sensors it found when it booted, in
that order. A second reading of one type on one channel is named like *Temperature 1 (2)*. The
entity ID ends in the reading's key without its unit, such as `sensor.mesh_<name>_ch2_temperature`
([mqtt-api.md](mqtt-api.md#reading-keys) has the keys):

| Name | Entity ID ends in | Class, unit |
| --- | --- | --- |
| Voltage | `_ch<c>_voltage` | voltage, V |
| Current | `_ch<c>_current` | current, A |
| Power | `_ch<c>_power` | power, W |
| Temperature | `_ch<c>_temperature` | temperature, °C |
| Humidity | `_ch<c>_humidity` | humidity, % |
| Pressure | `_ch<c>_barometric_pressure` | atmospheric pressure, hPa |
| Altitude | `_ch<c>_altitude` | distance, m (barometric) |
| Luminosity | `_ch<c>_luminosity` | none: lux on most boards, 0–100 on the T1000-E |
| Generic, Analog input | `_ch<c>_generic`, `_ch<c>_analog_input` | none |
| Percentage | `_ch<c>_percentage` | %, no class |
| Frequency | `_ch<c>_frequency` | frequency, Hz |
| Distance | `_ch<c>_distance` | distance, m |

*Battery* is the answer's first value, the channel 1 voltage, in volts (a percentage: the
template [below](#on-a-dashboard)). *MCU temperature* is its last one, the channel 1 temperature:
on a board without a processor sensor, a channel 1 sensor that comes last gets that name. Every
numeric telemetry sensor has the state class `measurement`, so Home Assistant keeps long-term
statistics of it. A sensor missing at one boot moves the later ones to a lower channel: the old
sensor then stays `unknown`; disable it under *Settings → Entities*. At most 16 reading sensors
per contact; a further reading stays in the document. Home Assistant records a state for each
answer in which a value changed, so a noisy sensor adds one with most answers
([Privacy](#privacy)).

**States.** *Telemetry state* says whether the values are current, and why not. Home Assistant
shows the raw values, as below: MQTT entities have no translations yet.

| State | Meaning |
| --- | --- |
| `waiting` | not asked yet, or not since its last answer more than a day ago |
| `answering` | its answers are current and carry a position, or one did within the last hour |
| `no_position` | its answers are current, without a position: no GPS, GPS off, no fix, or location not allowed |
| `away` | it answered within the day, not lately: out of range, switched off, or not asked meanwhile (telemetry paused, the radio away, no path) |
| `no_answer` | asked, no answer within a day: it does not allow the gateway, does not have it as a contact, or is out of range; the gateway cannot tell which |
| `no_path` | not asked: the radio has no path to it and has not heard it within 30 minutes; a message from it, or its advert, brings it back |
| `look_alike` | another contact or the radio itself has its first 6 key bytes, so an answer could not be told apart: not asked |
| `excluded` | the household excluded it, or with `ask = "household"` did not include it |
| `off` | telemetry is off in the configuration |

**Expiry.** Telemetry is current while the companion answers. After two unanswered requests in a
row, or an hour without an answer (three of its intervals if that is longer), its tracker and
telemetry sensors turn unavailable until it answers again. Its position counts while an answer
within the last hour carried one. *Telemetry state* and the switch stay available, and the
documents keep the last values. A position from adverts does not expire; it goes once a checked
advert no longer carries it.

| Moment (EU/UK Narrow, next to the gateway) | In Home Assistant |
| --- | --- |
| added to the radio | its device with *Telemetry* off and *Telemetry state* `excluded` until it is switched on (a contact target or command sender: on and `waiting`); with `ask = "all"` on and `waiting`, or `no_path` if the radio has no path to it and has not heard it within 30 minutes |
| first answer (about 76 s after the radio connects, then about every 15 minutes at rest) | *Battery*, *MCU temperature*, the readings and the tracker appear; `answering`; event `answered` |
| a lost answer | nothing: it is asked again 30 s later |
| GPS off | `no_position` an hour after the last answer with a position; the tracker `unavailable`, the sensors stay |
| leaves range right after an answer | at its next request and the retry: `away`, tracker and telemetry sensors `unavailable`, event `unanswered`; after about 16 minutes at rest, 2 while moving, 48 at rest behind two repeaters |
| a day without an answer | `no_answer` (asked less and less, up to every 6 hours), or `no_path` when the radio lost its path |
| comes back | asked at least every 30 minutes (or its interval, if longer): `answering`, all available, event `answered` |
| the radio unplugged | *Telemetry status* `paused` with `no_radio` at once; an hour after its last answer `away`, its tracker and sensors `unavailable`; no event |
| the gateway restarts | every gateway entity but *Message history* and *Messages on their way* `unavailable` for the seconds the restart takes; then the states come back from its state file and the readings from its history: nothing expires; if the radio does not come back, as for the radio unplugged |
| *Telemetry* switched off | at once `excluded`, its telemetry sensors `unavailable`; event *Contact* `excluded` |
| *Telemetry* switched on | `waiting`, asked at once (as the airtime lanes allow); values with its next answer; event *Contact* `included`, then `answered` |

**Events.** *Telemetry* (`event.meshcore_home_gateway_radio1_telemetry`) has `unanswered` when a
companion that answered within the day leaves two requests in a row unanswered, once per absence,
and `answered` when it answers again after that, or for the first time. No event comes when
nothing was asked (the radio away, telemetry paused, no path, the gateway stopped), nor for a
companion that never answered. Its attributes are `key`, `prefix`, `name` and `state`, never a
position or a reading.

**Excluding a companion.** Switch *Telemetry* off on its device page, run
`meshcore-home-gateway contacts telemetry KEY off`, or publish `off` to `contacts/<key>/telemetry`
([mqtt-api.md](mqtt-api.md#excluding-a-contact-from-telemetry-contactskeytelemetry)). A full key also works before the contact is on the radio, so a
neighbour can be excluded before it is added; a protected contact (a target, a command sender)
can be excluded too. At once the gateway asks it no more (a request waiting for the radio is
withdrawn, an answer in flight is dropped) and forgets its battery, readings and telemetry
position: the tracker shows the position from its adverts while its last checked advert, if any,
carried one (which can be old), or turns `unavailable`. *Last heard* keeps the time of its last
answer.

The switch follows the gateway, not the click: it turns off once the gateway took the request,
and a refused request leaves it on (the event *Contact* says why). The gateway keeps the
exclusions in `telemetry-excluded.json` in its state directory: also when the contact leaves the
radio and comes back, while telemetry is off, and across restarts. Its history keeps the
contact's readings and telemetry positions for `readings_days` and `positions_days`;
`meshcore-home-gateway contacts telemetry --forget KEY off` deletes them too. Home Assistant's own
history stays ([Privacy](#privacy)). Every user who may switch entities can switch it on again;
the broker's ACL decides who may write `contacts/+/telemetry`.

**Only the household's.** By default (`[telemetry] ask = "household"`) the switch *Telemetry* of
a new companion is off: the gateway asks only the companions you switch on, and the contact
targets and command senders ([configuration.md](configuration.md#telemetry)). Switching one off
excludes it as above. With `ask = "all"` every companion that is not excluded is asked.

**The first hour.** With `ask = "all"`, where telemetry never ran (an update from 1.0.0, from
1.1.0a1 with telemetry off, or a new installation), the gateway asks nobody for an hour after the
radio first connects:
*Telemetry status* reads `paused` with the reason `first_start`, and its log names the number of
companions it will ask. The switches and states are there meanwhile: exclude every companion that
is not the household's. Then the status reads `on`.

**Telemetry status** (`sensor.meshcore_home_gateway_radio1_telemetry_status`) is `on`, `off` (in
the configuration), `unsupported` (the radio's firmware does not take the request) or `paused`.
While paused, its attribute `reason` says why: `first_start` (the first hour), `no_radio` (the
radio is not connected), `exclusions_unreadable` (`telemetry-excluded.json` cannot be read: fix or
delete it, then restart the gateway), `auto_add` (the radio adds contacts of some type on its own)
or `radio_airtime` (the radio transmitted 180 s in the last hour).

### On a dashboard

An entities card for a handheld:

```yaml
type: entities
title: Alice's handheld
entities:
  - entity: sensor.mesh_alice_telemetry_state
  - entity: sensor.mesh_alice_battery
  - entity: device_tracker.mesh_alice
  - entity: switch.mesh_alice_telemetry
```

The battery as a percentage: a template sensor (*Settings → Devices & services → Helpers →
Template → Sensor*, class *Battery*, unit %, state class *Measurement*, device: the contact's),
linear from 3.0 V (the MeshCore app's 0 %) to 4.2 V:

```jinja
{% set v = states('sensor.mesh_alice_battery') | float(none) %}
{{ none if v is none else ([0, [100, ((v - 3.0) / 1.2 * 100) | round(0)] | min] | max) | int }}
```

## Positions and presence

A `person` follows its device trackers and skips one that is `unavailable`: when the mesh tracker
expires, the person follows its other GPS trackers, or turns `unknown` with none, and its zone no
longer counts it. The map drops the marker; `contacts/<key>` keeps the last position. For a
person whose only GPS tracker is the mesh tracker:

| Trigger | On expiry (`home` → `unknown`) | On return (`unknown` → `home`) |
| --- | --- | --- |
| `zone`, `event: leave` / `enter` | `leave` fires | `enter` fires |
| state of the person, `from: home` | fires | – |
| `zone.left` / `zone.entered` | never | never |
| `zone.occupancy_cleared` / `occupancy_detected` | fires if it was the last one in the zone | fires |

"Never" includes a real departure, which also ends as `unknown`. Every expiry counts: a companion
that leaves range, an hour without a position, an outage of the radio, an exclusion without a
position from adverts, a companion that stops sending its position in adverts, without a position
from telemetry, and the seconds of every restart of the gateway. Use a state trigger on
the person `from: home` with `for: "00:05:00"` (a restart lasts seconds) and the condition
*Telemetry status* `on` for 10 minutes: the status is not `on` while the radio is away, telemetry
is paused or the gateway is stopped, and reads `on` again before the companions answer; it is also
`unavailable` for a moment when Home Assistant starts or reconnects to the broker
([example](#examples)).

### Traces on the map

A companion's trace is its tracker's history in Home Assistant: one point for each move of
20 m or more reported by telemetry, one for each advert that carried a changed position, and
one when the position switches between adverts and telemetry; none per answer at rest. A map
card draws it when `hours_to_show` is set; the Map panel and a card without it show only where
the trackers are now.

```yaml
type: map
entities:
  - device_tracker.mesh_alice
hours_to_show: 24
```

List one tracker per companion. The trace reaches back as far as the recorder keeps states
(10 days by default); the gateway's own history keeps positions for `positions_days`, but Home
Assistant cannot read it. While a tracker is `unavailable` (its telemetry expired, or its
position from adverts went), it has no position: the card draws no marker for it, its trace ends
in a dot where it was last until that is older than `hours_to_show`, and once it has a position
again the card joins the points before and after with a straight line. A restart of the gateway or of Home Assistant adds a second point on the
same spot. A tracker kept out of the recorder ([Privacy](#privacy)) has no trace after a
reload; a card open at that moment still draws the points that come while it stays open. With
`show_all: true` instead of `entities`, the card draws the traces of every tracker and person,
and shows a person instead of a tracker that is the person's current source.

## What reaches Home Assistant

| MQTT | In Home Assistant |
| --- | --- |
| `state`: `radio`, `battery_v`, `radio_clock_offset_s`, `radio_link_contested` (a radio over TCP), `airtime_1h_s`, `airtime_available_s`, `queue` | the sensors above |
| `state`: `radio_name`, `public_key`, `model`, `firmware`, `frequency_mhz`, `bandwidth_khz`, `spreading_factor`, `coding_rate`, `tx_power_dbm`, `contact_link`, `version` | attributes of the radio link (`version` as `bridge_version`) |
| `state`: `radio`; `contacts`: `manage`; `contacts/<key>`: `removable` | whether the controls that change the contacts are available |
| `state`: `airtime_budget_s_per_hour` | attribute of *Airtime available* |
| `state`: `health` | *Gateway degraded*, with the section's keys as attributes |
| `received`, `delivery`, `command` | every key becomes an attribute of the event entity, also the nested `command` object |
| `rx`: `rssi_dbm`, `snr_db` | the packet sensors; `raw`, `length` and `received_at` stay on MQTT |
| `send/<alias>` | the notify entities of the targets |
| `stats`: `noise_floor_dbm`, `tx_airtime_1h_s`, `uptime_s`, `packets_received`, `packets_sent`, `receive_errors` | the radio's sensors; `tx_airtime_window_s`, `airtime_factor`, `duty_cycle_percent` as attributes of *Radio transmit time last hour* |
| `contacts`: `total`, `max`, `remote_cli…`, `advert_carries_position…` | *Contacts* and the two problem sensors; `advert_position…` stays on MQTT |
| `nodes`: `total` | *Nodes heard* |
| `nodes`: `look_alike`, `look_alike_total` | *Look-alike node heard* |
| `contact`, `node` | the events *Contact* and *New node* |
| `contacts/<key>` | a contact's sensors and tracker |
| `contacts/<key>`: `telemetry_state`, `telemetry_excluded`, `battery_v`, `mcu_temperature_c`, `readings` | *Telemetry state*, the switch *Telemetry*, *Battery*, *MCU temperature* and the reading sensors; `readings_at` stays on MQTT |
| `contacts/<key>`: `telemetry_current`, `position_current` | whether the telemetry sensors and a tracker of a position from telemetry are available |
| `contacts/<key>`: `advert_carries_position` | *Position in adverts*; `advert_position` and `advert_checked_at` stay on MQTT |
| `state`: `telemetry`, `telemetry_reason`, `telemetry_airtime_1h_s`, `telemetry_mesh_airtime_1h_s` | *Telemetry status* with the attribute `reason`, and the two telemetry airtime sensors |
| `telemetry` | the event *Telemetry* |
| `contacts/<key>/telemetry` | the switch *Telemetry* |
| `contacts/<key>/received`, `contacts/<key>/send` | a contact's message event and notify entity |
| `homeassistant/show_hidden` | the button *Show hidden contacts* |
| `homeassistant/draft/<kind>/<chat>`, its `…/request` | *Write to …* and *Send what is written to …*, a companion's *Write* and *Send what is written* ([writing and sending](#writing-and-sending)) |
| `homeassistant/add_node`, `add_contact`, `contacts/<key>/remove` | *Add heard node*, *Add contact (key or link)*, *Remove from radio* |
| `settings`, `setting` | *Radio profile* (every key as an attribute) and the event *Radio setting* |

An entities card shows an attribute, such as the text of the last message, without YAML.

## First steps

Once the gateway runs, its device is under *Settings → Devices & services → MQTT*, named after
`homeassistant.device_name`.

**Read a message.** A received message is an event of *Message*
(`event.meshcore_home_gateway_radio1_message`). Its text, sender, signal and times are attributes
of that event; the logbook and the device's activity show only the event type (`contact` or
`channel`), not the attributes. *Developer tools → States*, filtered for the entity, shows them
all. On a dashboard, a Markdown card shows the last message:

```yaml
type: markdown
title: Last mesh message
content: |-
  {% set e = 'event.meshcore_home_gateway_radio1_message' %}
  {% set text = state_attr(e, 'text') %}
  {% if text is none %}none yet{% else %}<div><b>{{ (state_attr(e, 'sender_name') or '?') | replace('\r', ' ') | replace('\n', ' ') | e }}</b>: {{ text | e | replace('\r', ' ') | replace('\n', ' ') }}</div>{% endif %}
```

Anyone in radio range chooses a message's text and a sender's name (a contact's own, or a channel
sender's claimed one), so the card escapes them, replaces their line breaks with spaces and keeps
them in an HTML block on one line: in Markdown, even an escaped text such as
`![](https://example.org/pixel.png)` or `[here](https://example.org)` becomes an image that the
browser loads or a link, and a blank line would end the block
([platform-notes.md](platform-notes.md#home-assistant-dashboards)). The card of 1.1.0a3 and
earlier kept the line breaks of the sender's name: paste it again.

**Send a message.** Each target is a notify entity, *Send to <alias>*; it shows *unknown* until it
sends for the first time. It has no input field of its own: it sends with the action
`notify.send_message`. To try it, open *Developer tools → Actions*, switch to YAML mode and run:

```yaml
action: notify.send_message
target:
  entity_id: notify.meshcore_home_gateway_radio1_send_alerts
data:
  message: Test from Home Assistant
```

In an automation, the same action is *Notifications: Send a notification message* (examples
below). A dashboard button that sends a fixed text:

```yaml
type: button
name: Test message
icon: mdi:send
tap_action:
  action: perform-action
  perform_action: notify.send_message
  target:
    entity_id: notify.meshcore_home_gateway_radio1_send_alerts
  data:
    message: Test from Home Assistant
```

**Send to a contact.** Each companion contact has its own notify entity, named like the contact,
used in the same way: `entity_id: notify.mesh_alice`.

**See whether it arrived.** *Delivery* shows each step as an event: `queued`, then `sent` (final
for a channel) and `confirmed` when the recipient of a direct message acknowledged it, or `failed`
or `expired` with a `reason` attribute ([mqtt-api.md](mqtt-api.md#delivery-delivery)). `confirmed`
says that the acknowledgement's code arrived, which a stranger who guesses a fixed text can send
too: for alerts, set `[messages] confirm_nonce` and let the recipient answer
([what it proves](mqtt-api.md#what-confirmed-proves)). The dashboard marks a confirmation that
came sooner than the path allows (the attribute `suspect`). The same
test without Home Assistant, in the gateway's container:
`meshcore-home-gateway send --wait 60 alerts "test"`.

## How discovery behaves

* The gateway publishes the discovery document when it connects to the broker, a random 0.5–2 s
  after Home Assistant's birth message (`online` on `status_topic`), and when the radio's model,
  firmware or key first becomes known or changes. The document is not retained; after a restart of
  Home Assistant the entities are back with the birth message, without any action. After birth
  messages it announces itself at most every 30 s, and a later one never postpones it: another
  client that floods `status_topic` cannot keep the entities away (the log names the topic).
* A component of an earlier configuration (a removed target, command words or the rx log switched
  off) is removed by the gateway, also when Home Assistant restarted in between: each announcement
  sends a placeholder under the old ID, which Home Assistant binds, and then the removal. The
  state file keeps a removal pending for 30 days, since the gateway cannot see whether Home
  Assistant received it; Home Assistant must have run in that time. A leftover entity can be
  deleted under *Settings → Entities*.
* When Home Assistant removes a disabled entity (the packet sensors after the rx log was switched
  off, an entity disabled by hand), or a current entity is deleted, up to 2026.10 it clears the
  whole device's discovery topic, and all its entities go; since 2026.11 the device keeps its
  other entities. The gateway watches that topic and announces again a moment later, which brings
  them back; Home Assistant logs *No device components to cleanup* meanwhile
  ([platform-notes.md](platform-notes.md#home-assistant-mqtt)). For the same reason the gateway's
  device comes back when it is deleted while the gateway runs: stop the gateway first.
* Each contact device has its own discovery topic one level below the gateway's,
  `homeassistant/device/meshcore_home_gateway_radio1/contact_<key>/config`. The gateway subscribes
  to them to notice a device deleted in Home Assistant, which publishes an empty retained message
  there. A contact that left the table is removed in the same way as a component, placeholders
  first and then the empty document; if Home Assistant may not have been running then (no birth
  message seen since the gateway connected), once more after its next birth message. That can
  bring the device back for a moment and log *Unable to remove unknown dispatcher*, which is
  harmless. The state file keeps the announced devices, the pending removals, the hidden contacts
  and the slugs of the entity IDs.
* The select and the text that add contacts have a discovery topic of their own on the gateway's
  device, `homeassistant/device/meshcore_home_gateway_radio1/manage_contacts/config`: the select's
  options exist only in discovery and follow the heard nodes, at most every 30 seconds, at once
  when a contact joined or left the table (as after an add or a removal), and at most 70 times in
  an hour, 2 more for each start of the gateway: Home Assistant warns from the 101st change of an
  entity's capabilities within an hour. In the first minutes after a start, and after many
  changes, a change can wait up to a minute.
  While the broker is away they wait; the announcement after the reconnect sends the current
  ones, or, with no change left in the budget, those sent before, and the current ones within a
  minute. Home Assistant
  keeps the entity when its options change. The gateway watches that topic like its own and
  announces again when Home Assistant clears it.
* To retire the gateway, stop it (or set `homeassistant.discovery = false`), then delete its
  device and its contacts' devices in Home Assistant.
* Events are never retained: Home Assistant ignores replayed retained events.
* `notify.send_message` publishes only `message`; `title` is dropped by Home Assistant. Send
  through the notify entities rather than `mqtt.publish`, which since 2026.10 needs an
  administrator when a user starts it.

These rules come from Home Assistant's MQTT discovery code (`homeassistant/components/mqtt/`:
`schemas.py`, `discovery.py`, `entity.py`, `event.py`, `notify.py`, `device_tracker.py`). The
contract tests in [`home-assistant/tests/`](../home-assistant/tests/) check them with the real MQTT
integration of the release pinned in
[`home-assistant/requirements.in`](../home-assistant/requirements.in): the entity IDs above, the
device pages, sensors and attributes, availability, the removal of components and of contact
devices (also after a restart of Home Assistant), a device deleted by the user, the map, the
controls that add and remove contacts, the telemetry entities and their expiry, a `person` and
the triggers of [Positions and presence](#positions-and-presence), the Markdown cards, the actions
of the first steps, the dashboard cards, the battery template, the names in another language
(the same entities, a rename that keeps the user's names), the [dashboard](#dashboard) and its
script, and the examples below, exactly as written here (`make ha-test`,
[testing.md](testing.md)).

## Dashboard

`meshcore-home-gateway dashboard` writes a dashboard for the gateway it runs beside, as YAML for
Home Assistant. It has three tabs, *Overview*, *Chats* and *Contacts*, and pages without a tab:
one for each chat, *Map*, *Gateway*, *Settings* and *Help*. Every card has a fold that explains
it, every page but a chat's and *Help* ends in *What this view is for*, which links to *Help*, and
*Help* names every page, button and switch and ends in what the administrator does.

* *Overview* answers whether everything is fine. Its first line says it in a colour: red for
  act now (the gateway not found or offline, the radio disconnected or taken over by another
  program), blue for no report yet or messages that have waited over a minute, yellow when
  something needs a look, naming the first problem, and green for *All fine*. Below it, the links
  to *Map*, *Gateway*, *Settings* and *Help*, then a card for each problem while it exists, each
  with what to do: another program took the radio's link (red), a fault in the gateway, a node
  nearby that imitates a contact (named after that contact), a contact with remote access to the
  radio, a position broadcast to everyone, the radio's settings changed or not set, asking for
  position and battery paused, the radio's clock far off, a full contact table, and a gateway of
  another major or minor version than the dashboard's (which of the two to update). The card of a
  position broadcast to everyone says to make sure in the MeshCore app that sharing the location
  in adverts is off, then to send an advert near this home's radio: until the gateway has read a
  device's newest advert, it goes by the position the radio remembers, so the card can come back
  after an update or a restart ([companions.md](companions.md#positions-in-adverts)).
  *Latest chats* shows the three chats with the newest messages, each a link to its page, "new"
  for a message received in the last 15 minutes, and a message not sent yet as on its way.
  *Where everyone is* lists the contacts with a position, in words, and links to the map.
* *Chats*: *All chats*, every chat page with its kind and newest message, those only automations
  write to last, then the contacts with a device that have no chat page yet and the channels
  heard that have none; and *Latest messages*, all messages together as a thread.
* A chat's page shows the chat's messages, under the line that says why the gateway refused a
  message to the chat, while there is one: received on the left, sent from this home on the
  right with their time and ✓ (sent) or ✓✓ (delivered, which the person's radio confirmed);
  a message not delivered, not sent or maybe forged in yellow, with the reason in words; one on
  its way in italics. The newest six stay open, fewer when they are long, the older ones in a
  fold. The delivery code that `[messages] confirm_nonce` adds to every direct message is
  hidden: the gateway publishes it only in the final delivery event and the history (#87), so
  the cards cut ` #` and 8 hex digits from the history's sent direct messages while
  *Configuration* says the setting is on; with it off, a text of the household's that ends that
  way shows whole. The cards follow the setting of now, the gateway that of the time a message
  was sent: after a change, the messages sent before it, also one kept on its way across the
  restart that changed it, show the other way until they leave the history. While the gateway
  is offline, Home Assistant drops *Configuration*'s attributes and the cards cut it as before.
  The thread's help links to *Help*, which explains each reason. The line that says why a
  message was refused stands above the thread, where the field does not cover it; it does not
  repeat the text, which stays in the field, and a text sent twice is in the thread. Under the
  thread, why writing is not possible when it is not, and *About*: where the person is, the
  battery, asking for position and battery with how the device answers, or the chat's text
  before and after messages. The page ends in the field to write in and its *Send* button
  (writing, below). A person whose device is gone, removed from
  the radio or hidden ([Contacts](#contacts)), keeps the page with the messages; its head says
  how to get the contact back.
* *Contacts*: buttons to *Gateway*, *Settings* and *Help* at the top; *People and devices*,
  this gateway's contacts by their devices, heard newest
  first, each with its type (person, repeater, room or sensor), its battery and the column
  *Asking*: whether the gateway asks its device for its position and battery and how it answers,
  in the words of a chat's page;
  *Sensors*, the readings of the contacts that report some; *Ask for position and battery*, a
  switch for each person, and for each sensor asked for readings (`[telemetry] ask_types`);
  *Add or remove*: *Add at once: pick a node heard nearby* and *Add by key or contact link*
  (paste it, then tap Return), the result of the last request below them,
  the QR code of the gateway under *Let someone add this home*
  ([above](#adding-and-removing-contacts)), a *Remove* for each contact, which asks first, and
  *Hidden contacts* with *Show again*; and *Nodes heard nearby*, the nodes you can add. While the
  controls cannot act, a line above them says why: the gateway offline or the radio disconnected
  (in red, as on *Overview*), `controls = false`, contact management off, the radio not reported
  yet, or firmware the gateway is not tested with; above the switches also asking turned off in
  the settings, while the switches still keep who would be asked. A list whose rows all hide
  shows no empty frame.
* *Map*: the contacts with a position and their traces of the last 24 hours
  ([Traces on the map](#traces-on-the-map)), and below it *Where everyone is*, in words. A
  contact whose position is not current has no marker there, as on Home Assistant's own Map
  panel, but keeps its trace, which ends in a dot where it was last, until that is more than 24
  hours ago; the map's help says so, a marker for where a device is now and a dot for each
  position of its line.
* *Gateway*: *Health*, each part of the gateway with its state, those that need a look named
  first, the messages on their way and those waiting over a minute, counted as the first line of
  *Overview* counts them, and the airtime left of the hour's budget; *Radio*, its name, model,
  firmware and radio settings, how the gateway reaches it, its background noise and battery and
  a day of airtime; *Radio settings*, what the managed profile wants and what the radio has,
  with who may ask the radio for its battery, position and readings as the radio acts on it;
  *Latest of each kind*, the newest contact, telemetry, setting, node and command event; a
  command the gateway refused after the newest accepted one shows in its place, with the reason
  in words, such as *too many wrong codes* for `code_locked`, but only until the next message
  arrives, of any kind, a channel message too: Home Assistant has the refusal only as the
  attribute `command` of the newest message (`event.meshcore_home_gateway_radio1_message`).
  The gateway's log warns of a sender refused for too many wrong codes. While
  the gateway is offline, these cards say so instead of values: Home Assistant keeps no
  attributes of an unavailable entity, so they would read as never reported.
* *Settings*: what your administrator set, read only, from the gateway's *Configuration*:
  chats and messages, privacy, contacts and Home Assistant, the radio and airtime, each setting
  with its section and key of the configuration file, such as `[messages]` `truncate`; and the
  versions of the dashboard, the gateway and the
  radio. A gateway before 2.1 reports no settings, and the page says so and shows only what it
  can tell; offline, it says that instead.
* *Help*: what the gateway does, the pages, every button and switch, the kinds of messages, what
  the dashboard cannot do, what each word of the cards means, who sees what, and what the
  administrator does.

It uses only the gateway's entities and the cards of Home Assistant, no custom card. Its texts
follow `homeassistant.language`; entity IDs, reason codes and names from the mesh stay as they
are. Its numbers follow the language too: German writes 0,4 s and 3,9 V, every language a whole
number without `.0`; a number in a message or a name stays as it was written. The number format of your
Home Assistant profile does not reach these cards, whose templates know neither the viewer's
language nor that format ([platform-notes.md](platform-notes.md#home-assistant-dashboards)). A
card that finds the contacts' devices in Home Assistant's registry, which tells no card
of a change, renders again each minute: a contact that gets its device, or a tracker, shows
within a minute. Every name and text from the mesh shows as text: no name or message can add a link or load
an image ([platform-notes.md](platform-notes.md#home-assistant-dashboards)).

**Make it** in the gateway's container, as root (it reads the service's state, read only, for the
contacts on the map; `--state-dir` names another directory):

```sh
meshcore-home-gateway dashboard > mesh.yaml
```

From the Proxmox host, with the full path (`pct exec` passes the host's `PATH`,
[platform-notes.md](platform-notes.md#proxmox-ve)); the file lands on the host:

```sh
pct exec <id> -- /usr/local/bin/meshcore-home-gateway dashboard > mesh.yaml
```

It says on standard error how many contacts the map lists, such as "The map lists 1 contact with
a position.", how many chat pages it has and how many contacts have none, and last "This dashboard
needs Home Assistant 2026.10 or later."

**Add it** in one of two ways:

* In the browser, as an administrator: *Settings → Dashboards → Add dashboard → New dashboard
  from scratch*, open it, *Edit* → ⋮ → *Raw configuration editor*, replace everything with the
  file and *Save*. The editor drops the file's comments.
* As a YAML-mode dashboard: put the file into Home Assistant's configuration directory, here as
  `dashboards/mesh-radio1.yaml`, and add to `configuration.yaml`:

```yaml
lovelace:
  dashboards:
    mesh-radio1:
      mode: yaml
      filename: dashboards/mesh-radio1.yaml
      title: Mesh
      icon: mdi:radio-tower
      show_in_sidebar: true
```

The dashboard's path (`mesh-radio1`) needs a hyphen. Home Assistant reads a replaced file at the
next page load. The dashboard's links are relative, so they stay inside it under any path; opened
from an address typed or bookmarked with a `/` at the end, such as `/mesh-radio1/overview/`, a
tap on a link does nothing.

**Writing.** A chat's page ends in a field *Write to …* and a tile *Return, then Send*: type,
tap Return (↵), then *Send*. Return, or leaving the field, keeps the text at the gateway; only
*Send* sends it, once. The field is shared by everyone in the home who uses Home Assistant, and
a text left in it is cleared after 15 minutes. A *Send* the gateway refuses says why above the
thread, also when the field held no text. The field and the button are entities of a gateway of
version 2.1; with an earlier gateway, the page says so instead. A channel that only automations
write to (`homeassistant.hidden_targets`) has no field, nor have two targets whose aliases differ
only in `-` and `_`, whose fields Home Assistant cannot tell apart: their pages say so for the
administrator ([configuration.md](configuration.md#targetsalias)). While the field or the button
is disabled or unavailable in Home Assistant, or has another entity ID than the page expects, the
page says which for the administrator.

**The script for automations.** The dashboard does not use the script *Mesh message*; it stays
for automations and the developer tools, as a form that sends to a target or a contact. Add it
once, for every gateway: *Settings → Automations & scenes → Scripts → Create script*, ⋮ → *Edit
in YAML*, paste the block and *Save*. Keep its name: its ID comes from it
(`script.mesh_message`).

```yaml
alias: Mesh message
description: Send a message over the mesh to a target or a contact.
icon: mdi:message-arrow-right
mode: queued
fields:
  to:
    name: To
    required: true
    selector:
      entity:
        filter:
          - integration: mqtt
            domain: notify
  message:
    name: Message
    required: true
    selector:
      text:
sequence:
  - action: notify.send_message
    target:
      entity_id: "{{ to }}"
    data: "{{ {'message': message} }}"
```

*To* offers every notify entity of MQTT: the targets (*Send to <alias>*) and the companion
contacts, by name. A household user who is no administrator sends with it, as with
`notify.send_message` itself. The script hands on the whole `data` as one template, so the text
stays a text: written as `message: "{{ message }}"`, Home Assistant would read `+493023125000` and
`1.10` as numbers (sending `493023125000` and `1.1`), strip spaces around a text, and refuse
`None`.

**Make it again** after an update of the gateway to another major or minor version (*Overview*
then shows a card with both versions) and after a roll back to one, with that version's command
([deployment.md](deployment.md#update-reconfigure-remove)), after a target was added or a contact
got a device that takes texts (each needs its chat page; *All chats* names the contacts without
one), when the map's hint names a contact (one that got a position after the dashboard was made), and after the radio
got a contact that should have its switch and button there. The
contacts table, the sensors, the result of the last request, the nodes heard, *Gateway* and
*Settings* follow by themselves.
The dashboard names the entity IDs the gateway gives: a gateway entity whose ID was renamed shows
as missing, and a contact whose IDs were renamed drops out of the table and off the map, also in
a dashboard made again. A dashboard pasted again loses the edits made to it: to change it, copy
it under another name.

With two gateways on one broker whose contacts share a name, Home Assistant gives the second
gateway's contact entity IDs ending in `_2`, while the second gateway's dashboard lists the first
one's tracker of that name. For the same contact (the same key) that is the same node, and the
map is right. For two nodes of one name, the map shows the other gateway's node, and its hints say
so in yellow and keep naming the contact: put the tracker's ID ending in `_2` (as *Settings →
Entities* shows it) into the map card, which the next paste undoes.

*Ask for position and battery* and *Remove* give every user of the dashboard what Home Assistant
already gives every user: a contact's *Telemetry* switch and *Remove from radio* button, which
the device page shows to administrators only, are open to any user through the entity search
(press `e`) and the actions. *Remove* on the dashboard asks first, naming the contact; the button
of the device page and the entity search do not.

Each switch and *Remove* of *Contacts* shows only while its entity ID is that of this gateway's
contact with the key the dashboard was made for, so a renamed, removed or reused ID never
operates another contact. When a contact of this gateway has its control under another ID (a
second gateway whose contact of the same name got the ID first, or a contact prefix set after
the dashboard was made), a yellow line names that ID, for the administrator; a contact that got
its device after the dashboard was made is listed as *Not listed here yet*.

The dashboard shows messages, names and the contacts' positions to every user of Home Assistant,
as the entities do. The file holds the names of the contacts on the map: do not post it.

## Examples

Each example is one automation, unless it says it is a script. Paste it in the automation editor
(*Edit in YAML*), or add it under the `automation:` key of an existing configuration.

Send an alert to a channel when the smoke detector fires:

```yaml
alias: Smoke alert over the mesh
triggers:
  - trigger: state
    entity_id: binary_sensor.kitchen_smoke
    to: "on"
actions:
  - action: notify.send_message
    target:
      entity_id: notify.meshcore_home_gateway_radio1_send_alerts
    data:
      message: "Smoke in the kitchen"
```

Answer the command `status` from an allowlisted handheld:

```yaml
alias: Status on request over the mesh
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_command
    options:
      event_type: [status]
actions:
  - action: notify.send_message
    target:
      entity_id: notify.meshcore_home_gateway_radio1_send_operator
    data:
      message: >-
        Doors {{ states('binary_sensor.doors') }},
        power {{ states('sensor.grid_power') }} W
mode: queued
```

Get told when a message could not be delivered:

```yaml
alias: Mesh delivery failed
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_delivery
    options:
      event_type: [failed, expired, rejected]
actions:
  - action: persistent_notification.create
    data:
      title: Mesh message not delivered
      message: >-
        {{ trigger.to_state.attributes.target }}:
        {{ trigger.to_state.attributes.event_type }}
        ({{ trigger.to_state.attributes.reason }})
mode: queued
```

Do not report a failed delivery over the mesh to the same target: when the radio cannot reach it,
the report fails too and uses airtime.

Send a message to a contact target and wait until it is confirmed, as a script (paste it in the
script editor, *Edit in YAML*). It sends through the target's notify entity, takes the `id` of
the first delivery event for that target, and returns when the request's final event comes:

```yaml
alias: Send over the mesh and wait
mode: queued
fields:
  recipient:
    name: Target
    description: the alias of a contact target, as in send/<alias>
    required: true
    selector:
      text:
  message:
    name: Message
    required: true
    selector:
      text:
sequence:
  - variables:
      delivery: event.meshcore_home_gateway_radio1_delivery
      before: "{{ states(delivery) }}"
  - action: notify.send_message
    target:
      entity_id: "notify.meshcore_home_gateway_radio1_send_{{ recipient | slugify }}"
    data: "{{ {'message': message} }}"
  - wait_template: >-
      {{ states(delivery) != before and state_attr(delivery, 'target') == recipient }}
    timeout: "00:00:30"
  - if:
      - "{{ not wait.completed }}"
    then:
      - variables:
          result: {confirmed: false, event_type: null, reason: no_answer}
      - stop: The gateway did not answer
        response_variable: result
  - variables:
      id: "{{ state_attr(delivery, 'id') }}"
  - wait_template: >-
      {{ state_attr(delivery, 'id') == id and state_attr(delivery, 'event_type')
         in ['confirmed', 'failed', 'expired', 'rejected', 'duplicate'] }}
    timeout: "00:15:00"
  - variables:
      result:
        confirmed: >-
          {{ wait.completed and state_attr(delivery, 'event_type') == 'confirmed'
             and not state_attr(delivery, 'suspect') }}
        event_type: "{{ state_attr(delivery, 'event_type') if wait.completed else none }}"
        reason: "{{ state_attr(delivery, 'reason') if wait.completed else 'timeout' }}"
  - stop: Final event received
    response_variable: result
```

An automation calls it as `script.send_over_the_mesh_and_wait` (not with `script.turn_on`,
which does not wait) and escalates when the message was not confirmed:

```yaml
alias: Smoke alert that must arrive
triggers:
  - trigger: state
    entity_id: binary_sensor.kitchen_smoke
    to: "on"
actions:
  - action: script.send_over_the_mesh_and_wait
    data:
      recipient: operator
      message: "Smoke in the kitchen"
    response_variable: sent
  - if:
      - "{{ not sent.confirmed }}"
    then:
      - action: persistent_notification.create
        data:
          title: Smoke alert not confirmed over the mesh
          message: "{{ sent.event_type }} ({{ sent.reason }})"
mode: queued
```

`confirmed` is false also for a `suspect` acknowledgement
([mqtt-api.md](mqtt-api.md#what-confirmed-proves)); `reason` is `no_answer` when no delivery event
came, for example for a target hidden from Home Assistant, which has no notify entity. The script
is for contact targets: a channel message ends with `sent`, never `confirmed`. It tells its own
request from another by the target alone until it has the `id`: while it runs, let nothing else
send to the same target (`mode: queued` keeps its own runs apart). The 15 minutes cover the
default `queue_ttl_s` of 10 minutes and the wait for the last acknowledgement. The whole `data` is
one template for the reason given at the [form](#dashboard): the text stays a text.

Get told when the gateway or its radio has been away for five minutes: the radio link turns off
when the radio is unplugged or the link breaks, and every entity turns unavailable when the
gateway stops or loses the broker (its last will):

```yaml
alias: Mesh gateway away
triggers:
  - trigger: state
    entity_id: binary_sensor.meshcore_home_gateway_radio1_radio
    to: ["off", "unavailable"]
    for: "00:05:00"
actions:
  - action: persistent_notification.create
    data:
      title: Mesh gateway away
      message: >-
        {{ 'The gateway is offline' if trigger.to_state.state == 'unavailable'
           else 'The gateway has lost its radio' }}
mode: single
```

Get told when a contact was not added or removed (a node already on the radio is left out), or
an exclusion from telemetry was not stored (`not_saved`: it took effect, and the gateway tries
again every minute) or its history not deleted (`not_forgotten`):

```yaml
alias: Mesh contact not changed
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_contact
    options:
      event_type: [refused, failed, excluded, included]
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.reason not in [none, 'already_contact'] }}
actions:
  - action: persistent_notification.create
    data:
      title: Mesh contact not changed
      message: >-
        {% set a = trigger.to_state.attributes %}
        {{ a.request }} {{ a.prefix or 'no key' }}: {{ a.reason }}
mode: queued
```

A notification renders Markdown, so the message has only machine values: the name a node claims
would be markup.

Warn a handheld's owner when its battery runs low:

```yaml
alias: Handheld battery low
triggers:
  - trigger: numeric_state
    entity_id: sensor.mesh_alice_battery
    below: 3.5
    for: "00:10:00"
actions:
  - action: notify.send_message
    target:
      entity_id: notify.mesh_alice
    data:
      message: "Your handheld battery is low"
```

A `numeric_state` trigger ignores `unavailable`, so a handheld out of range cannot warn: pair a
warning that matters with the next example.

Get told when a handheld stops answering:

```yaml
alias: Mesh handheld not answering
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_telemetry
    options:
      event_type: [unanswered]
actions:
  - action: persistent_notification.create
    data:
      title: Mesh handheld not answering
      message: >-
        {% set a = trigger.to_state.attributes %}
        {{ a.prefix }}: {{ a.state }}
mode: queued
```

It needs no condition: the event comes only for requests that were sent.

Get told when Alice left home ([Positions and presence](#positions-and-presence)):

```yaml
alias: Alice left home
triggers:
  - trigger: state
    entity_id: person.alice
    from: home
    for: "00:05:00"
conditions:
  - condition: state
    entity_id: sensor.meshcore_home_gateway_radio1_telemetry_status
    state: "on"
    for: "00:10:00"
actions:
  - action: persistent_notification.create
    data:
      title: Alice left home
      message: "{{ trigger.to_state.state }}"
mode: single
```

It also fires when Alice's only position came from her adverts and she switches "Share Position
in Advert" off: once the gateway checks her next advert, the tracker turns `unavailable`.

For a person whose only GPS tracker is the mesh tracker, `zone.left` never fires, and a restart of
the gateway makes the person `unknown` for seconds, which the `for` skips. The condition skips an
outage of the radio, a pause and a stopped gateway, and the wait for the first answers after them:
*Telemetry status* reads `on` again at once, and the companions answer about 76 s apart (EU/UK
Narrow). Its 10 minutes cover about seven companions; add 76 s for each more. It also skips a
departure in the first 5 minutes after them, and after Home Assistant starts or reconnects to the
broker (every MQTT entity, the status too, is `unavailable` for a moment).

Get told when a handheld shares its position in adverts:

```yaml
alias: Mesh handheld shares its position
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_contact
    options:
      event_type: [position_in_advert]
actions:
  - action: persistent_notification.create
    data:
      title: Mesh handheld shares its position
      message: >-
        {{ trigger.to_state.attributes.prefix }}: its adverts carry its position; turn off
        "Share Position in Advert" on it and press Advert near the gateway
mode: queued
```

Everyone in range can read an advert. The event comes when a handheld starts sharing (also with
0, 0, from one that never had a position), when one that shares is added after the gateway heard
its advert (*Add heard node*; one the gateway did not hear before the add, added from a key or a
link first or after a restart of the gateway between hearing and adding it, is reported at its
next advert), and when one that shares is included in telemetry again; *Position in adverts* on
its device stays on while it shares. Machine values only: a name from the mesh would be Markdown
in a notification.

Use `event.received`: a state trigger on an event entity misses its first event (the state changes
from `unknown`), and a trigger on the attribute `event_type` misses the second of two equal events.
The event's keys are in `trigger.to_state.attributes`, for example `args` of a command.

## Shared households

Home Assistant shows every entity to every user, and lets every user operate it; only
administrators change the configuration. So every user sees every tracker on the map and in the
history, can send to every target and contact, and can add and remove the radio's contacts and
switch telemetry for a companion. Dashboards per user hide cards, not the entities behind them.

* `[homeassistant] controls = false` keeps *Add heard node*, *Add contact (key or link)*, *Show
  hidden contacts* and each contact's *Remove from radio* and *Telemetry* unavailable: nobody
  operates them in Home Assistant, which leaves unavailable entities out of its actions too. An
  administrator uses `meshcore-home-gateway contacts` in the container, or MQTT
  ([mqtt-api.md](mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)); the broker's
  ACL decides which clients may write those topics.
* `[homeassistant] hidden_targets` leaves out the notify entity and the draft (*Write to …*,
  *Send what is written to …*) of a target that only automations should use, such as an alarm
  channel: they send with `mqtt.publish` to `send/<alias>`
  ([configuration.md](configuration.md#homeassistant)).
* The notify entities of the contacts send as the household's radio, for every user.
* The dashboard grants nothing new: its fields and *Send* buttons send to the targets and
  contacts that every user can send to already, and *Settings* only shows the configuration,
  which the administrator changes in the file. Everyone shares a chat's draft: what one user
  leaves in a field, another user's *Send* sends, until it is emptied after 15 minutes
  ([Writing and sending](#writing-and-sending)). *Configuration* shows every user the settings,
  the hidden targets' names and prefixes included.
* Home Assistant's permission policies can restrict a group of users to reading entities, or to
  some of them; its interface does not offer them (developer documentation,
  [Permissions](https://contributors.home-assistant.io/core/internals/permissions/)).

## A second gateway

A second gateway (another radio, another `node_id`) gets its own device and entities. Its
contacts' entity IDs would begin like the first one's, and Home Assistant would number the
second `_2`: give it another start, such as `[homeassistant] contact_entity_prefix = "mesh_2"`,
before its first start ([configuration.md](configuration.md#homeassistant)). A companion that is a
contact of both radios then has two trackers; one `person` follows the newer of them:

```yaml
person:
  - name: Alice
    id: alice
    device_trackers:
      - device_tracker.mesh_alice
      - device_tracker.mesh_2_alice
```

The same in the interface: *Settings → People → Alice → Track device*, both trackers.

## Privacy

Messages on a channel come from anyone who knows its key: the *Public* channel's key is in the
firmware, so in a lively mesh most of them are strangers' texts and claimed names. By default
(`[messages] publish_channels = "targets"`) only the channels you chose as targets are published
and kept; with `"all"` every channel's messages become events, history rows and recorder entries
like the household's own. `ignore_channels` leaves out single channels
([configuration.md](configuration.md#messages)).

The nodes heard are other people's radios: *Nodes heard*, the *New node* event, *Look-alike node
heard* and the select *Add heard node*. By default (`[contacts] heard_nodes = "full"`, since 2.2)
the *New node* event carries the name its owner chose, when it was heard and its signal, which
the recorder keeps, and *Add heard node* offers the nodes by name; `"keys"` shows their keys and
types only, `"off"` keeps nothing but the look-alikes' keys
([configuration.md](configuration.md#contacts)). The recorder leaves the options of *Add heard
node* out, but `.storage/core.restore_state` keeps the last of them with the names, as it keeps
the last *New node* event, and no recorder exclusion reaches it. What the gateway keeps, of whom
and for how long, and the pattern that keeps the *New node* events out of the recorder, are in
[privacy.md](privacy.md#other-people).

Message texts, sender keys and sender names are attributes of the event entities, so Home
Assistant stores them in its history database, and the last event of each entity also in
`.storage/core.restore_state` (restored after a restart): *Message* and each contact's *Message*
hold the messages received, *Delivery* every step of a message sent, with its text, and
*Command* a command's word and arguments. *Message history* holds the latest messages in and out
as attributes, so the recorder stores them again with every message, and *Messages on their way*
the texts being sent with each of their steps. Every draft a user stores or the gateway empties
is a state of its text entity, *Write to …* or a contact's *Write*. To keep the texts out of the
history, exclude these entities from the recorder by patterns, which take every target's and
every contact's entities:

```yaml
recorder:
  exclude:
    entity_globs:
      - event.meshcore_home_gateway_*_message*
      - event.meshcore_home_gateway_*_delivery*
      - event.meshcore_home_gateway_*_command*
      - sensor.meshcore_home_gateway_*_message_history*
      - sensor.meshcore_home_gateway_*_pending_messages*
      - text.meshcore_home_gateway_*_write_*
      - event.mesh_*_message*
      - text.mesh_*_write*
```

The first three lines keep out the messages received, the messages sent and the commands'
arguments; the next two the history and the messages on their way; the sixth the drafts of the
targets; the last two each contact's messages and draft. The `*` after `meshcore_home_gateway_`
stands for any `node_id`, so the block serves a second gateway too, and the `*` at the end also
takes the IDs Home Assistant numbers with `_2`. If you set `[homeassistant] contact_entity_prefix`,
write your prefix instead of `mesh` in the last two lines; a prefix that begins with `mesh_`,
such as a second gateway's `mesh_2`, is taken already, any other needs its own two lines. The
*Send* buttons hold no text and stay recorded. *Configuration* holds no message; it shows every
user the settings, the names, prefixes and suffixes of the hidden targets included, and the
recorder stores it at each start and radio connect: add
`sensor.meshcore_home_gateway_*_configuration` to keep it out too.

The contacts' trackers store their positions in the history too, which a map card with
`hours_to_show` draws as their traces ([Traces on the map](#traces-on-the-map)): a companion that
moves writes one state per 20 m telemetry reports it moved, at most about one per telemetry answer
(every 90 s or more), one per advert with a changed position, and one when the position switches
between adverts and telemetry. Readings, *Telemetry state*, the switch *Telemetry* and the
gateway's event *Telemetry* (a dated log of who left range and came back, with `prefix`, `name` and
`state`) are recorded as well, a noisy reading with most answers. To keep them out, exclude them
too:

```yaml
recorder:
  exclude:
    entity_globs:
      - device_tracker.mesh_*
      - sensor.mesh_*_battery*
      - sensor.mesh_*_mcu_temperature*
      - sensor.mesh_*_ch[1-9]*
      - sensor.mesh_*_telemetry_state*
      - switch.mesh_*_telemetry*
      - event.meshcore_home_gateway_*_telemetry
```

The `*` at the end also takes the IDs Home Assistant numbers with `_2`; with a
`contact_entity_prefix` of your own, change `mesh` as above. Excluding `device_tracker.mesh_*`
keeps the positions out of the database, while a map card open at that moment still draws the
points that come while it stays open. *Position in adverts* and the
*Contact* event record when a companion's adverts started or stopped carrying its position, never
where: no pattern is needed for them. An excluded sensor keeps no statistics. A contact whose name
gives such a part of an entity ID, such as *Team ch2* (`_ch` and a digit) or *Old battery*, loses
the history of its other sensors too, which is harmless. Home Assistant keeps the last event's
attributes in `core.restore_state`, as for messages, and a `person` keeps its own copy of the
position. Excluding a contact in the gateway does not purge Home Assistant's history:
`recorder.purge_entities` removes its states and events, **not** the long-term statistics of its
`measurement` sensors; delete those under *Developer tools → Statistics*. The gateway's own history
of the contact goes with `--forget` ([above](#telemetry-of-the-households-companions)).
