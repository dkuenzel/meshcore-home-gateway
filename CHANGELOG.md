# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The MQTT API, the configuration
format, the command line and Home Assistant discovery are public interfaces
([compatibility](docs/development.md#compatibility)).

## [Unreleased]

## [2.1.0] - 2026-11-09

The household's chat, its problems and its settings on the generated dashboard, and German.

`meshcore-home-gateway dashboard` writes the dashboard again for the household, with three tabs,
*Overview*, *Chats* and *Contacts*, and the pages *Map*, *Gateway*, *Settings* and *Help*. Each
chat has a page that reads like a messenger, with a field to write and a *Send* button.
*Overview* says in its first line whether everything is fine and names the first problem, with a
card for each problem that says what to do. *Contacts* has each person's switch to ask for
position and battery and a *Remove* that asks first, and *Settings* shows what the administrator
set, read only. For this the gateway announces new entities in Home Assistant: *Message
history*, *Messages on their way*, *Configuration*, and a draft with its *Send* button for each
chat. German comes with this release: the command line, the installer, the names in Home
Assistant and the dashboard. The release also carries the fixes of the review of 2.0.0, among
them a limit of wrong codes per command sender (#79), the delivery code of `confirm_nonce` kept
off the broker until the delivery ended (#87) and the requests a stop used to lose (#92).

For the administrator: make the dashboard again after the update. It needs Home Assistant
2026.10 or later; the gateway and its entities keep 2026.8. With a broker ACL, Home Assistant's
user needs `topic write <base_topic>/<node_id>/homeassistant/draft/+/+/request`, or *Send* does
nothing ([mqtt-api.md](docs/mqtt-api.md#securing-the-broker)). The guide's recorder block now
keeps the texts sent and the commands out too: copy it again
([home-assistant.md](docs/home-assistant.md#privacy)). Before a roll back to 2.0, read the update
guide: 2.0 neither publishes nor clears the retained `pending`, `config` and drafts that 2.1
added, and the guide gives the commands that clear them
([deployment.md](docs/deployment.md#update-reconfigure-remove)).

Known limits of the dashboard: a command the gateway refused leaves *Latest of each kind* with
the next message received (#141); the delivery code of `confirm_nonce` is cut by the setting of
now, not by that of the message's time (#142); and on a desktop screen with a long thread, the
line that says why a *Send* was refused can stand behind the field until the page is scrolled.

2.1.0 had no release candidate, as the maintainer decided: CI, the Home Assistant contract tests
on 2026.10.2 and 2026.11.0 and the browser check of the dashboard on both checked it. The
maintainer's use follows the release; what it finds ships as 2.1.1.

### Added

- The attribute `manage_reason` of *Contacts* in Home Assistant, which the guide and the
  changelog of 2.0.0 already named: why requests to add or remove contacts are refused now,
  `null` while they are taken (#99).
- A warning at the start and in `check-config` for targets whose aliases differ only in `-` and
  `_`, such as `a-b` and `a_b`: Home Assistant makes the same entity IDs of them and numbers the
  later ones (`_2`). Their chat pages in the dashboard have no message field and say why, and
  `meshcore-home-gateway dashboard` names the pair.
- The entries of the `history` document name the target a message came through or went to
  (`target`), and a sent message's also the id of its delivery events (`id`), when the gateway
  accepted it (`queued_at`, also across a stop), whether its acknowledgement was `suspect`, and
  why it failed or expired (`reason`); each key is always there, `null` where it does not apply.
  The history database keeps them in new columns, which 2.0 leaves empty after a downgrade, and
  `history export` adds them at the end of `messages` (`delivery_id` for the id)
  ([mqtt-api.md](docs/mqtt-api.md#history-history)).
- The sensor *Message history* in Home Assistant: the time of the newest message, and the latest
  messages in and out as attributes. It stays available while the gateway is offline. The
  recorder stores the messages with every change unless it excludes the sensor
  ([home-assistant.md](docs/home-assistant.md#privacy)).
- The retained document `pending` and the sensor *Messages on their way* in Home Assistant: the
  messages the gateway accepted and has not delivered or given up yet, the one being sent
  included, which *Queue* never counts (#112). Each names its step (`queued` or `sent`), its
  attempts and when it was accepted: one `queued` for more than a minute waits for the radio or
  for airtime. The sensor stays available while the gateway is offline. After a downgrade to 2.0
  the broker keeps the last `pending`; an empty retained message clears it, such as
  `mosquitto_pub -r -n -t meshcore/radio1/pending`
  ([mqtt-api.md](docs/mqtt-api.md#messages-on-their-way-pending)).
- Writing from Home Assistant, then sending: a draft text and a *Send* button for each target not
  in `hidden_targets` (*Write to …*, *Send what is written to …* on the gateway's device) and for
  each companion that is no contact target (*Write*, *Send what is written* on its device). The
  gateway keeps one draft per chat, retained on `homeassistant/draft/<kind>/<chat>`, and takes
  both requests on `…/request` in the order they come, so *Send* sends the text stored just
  before it, through the same checks, queue and events as a send request. A queued message
  empties the draft; a refused one stays, to be changed. A draft unchanged for 15 minutes, and
  every draft at a clean stop, is emptied. After a downgrade to 2.0 the broker keeps the last
  drafts; an empty retained message on each clears it, such as
  `mosquitto_pub -r -n -t meshcore/radio1/homeassistant/draft/target/family`
  ([mqtt-api.md](docs/mqtt-api.md#drafts-of-home-assistants-chats-homeassistantdraftkindchat),
  [home-assistant.md](docs/home-assistant.md#writing-and-sending)). With a broker ACL, Home
  Assistant's user needs `topic write meshcore/radio1/homeassistant/draft/+/+/request`, which the
  example ACL of earlier versions lacks: without it *Send* does nothing
  ([mqtt-api.md](docs/mqtt-api.md#securing-the-broker)).
- The retained document `config` and the sensor *Configuration* in Home Assistant: the settings
  in effect since the gateway started, read only, so that the household sees what the
  administrator set. Each target with its kind, prefix, suffix, whether it is an alert or hidden
  target, and the room of a text to it; the sections `messages`, `contacts`, `telemetry`,
  `homeassistant`, `radio`, `airtime`, `history` and `commands` by an allowlist, which leaves out
  every host, port, device path, user name, password, client ID, key, the codes and the command
  words. It goes out at the start and at every connect to the radio and to the broker. After a
  downgrade to 2.0 the broker keeps the last `config`; an empty retained message clears it, such
  as `mosquitto_pub -r -n -t meshcore/radio1/config`
  ([mqtt-api.md](docs/mqtt-api.md#configuration-config)).
- `meshcore-home-gateway commands revoke KEY` removes the code secret of a key that is no longer
  in `[commands] senders` from the codes file. It refuses a key that is still a sender, which
  would then command without a code; `enrol` gives such a sender a new secret
  ([configuration.md](docs/configuration.md#commands)) (#80).
- The dashboard has a page for each chat: one per channel target, one per contact target
  without a device, and one per contact with a device that takes texts, at most 30 besides the
  contact targets'. A chat that has a message field shows it at the bottom of its page, with a
  *Send* button. The page of a contact target with a device is that contact's.
  `meshcore-home-gateway dashboard` says on standard error how many chat pages the dashboard
  has, which contacts have none, and which chat pages, switches and buttons it left out because
  a name does not fit a page's address or an entity ID.
- A chat's page shows the chat as a messenger does: received messages on the left, those sent
  from this home on the right with ✓ or ✓✓, a message not delivered, not sent or maybe forged in
  yellow with the reason in words, one on its way in italics, the newest open and the older ones
  folded, the delivery code of `confirm_nonce` hidden while the gateway adds one (in the history
  only, #87). At its top it says how to write (type,
  Return, then *Send*); under the thread, why a *Send* was refused, also when the field held no
  text, why writing is not possible when it is not (the field or its *Send*
  button disabled, unavailable or under another ID, for the administrator), and about the person
  or the chat: where the person is, the battery, asking for position and battery with how the
  device answers, the text the gateway adds before and after each message. A person removed from
  the radio or hidden keeps the page, which says how to get the contact back. These need the
  message history and the messages on their way of gateway 2.1.
- *Chats* lists every chat page under *All chats* with its newest message, one not sent yet as
  on its way, and "new" for a message received in the last 15 minutes, the contacts and channels
  that have no page yet, and all messages together under *Latest messages*. *Overview* shows the
  three latest chats and where everyone is. *Help* explains the problems and why a message was
  not sent, and opens from each tab's *What this view is for* and from each chat's help.
- *Contacts* on the dashboard, written again for the household: buttons to *Gateway*,
  *Settings* and *Help*; *People and devices*, this gateway's contacts by their devices, each with its type (person, repeater, room or sensor), its
  battery and *Asking*, how its device answers asking for position and battery, and a link to its
  chat page; *Sensors*, the readings of the contacts that report some; *Ask for position and
  battery*, a switch for each person; *Add or remove* with the result of the last request under
  the fields, the QR code under *Let someone add this home*, a *Remove* for each contact that
  asks first, naming the contact, and *Hidden contacts* with *Show again*; *Nodes heard nearby*,
  the nodes you can add. A line above the controls says why they cannot act when they cannot:
  the gateway offline or the radio disconnected (in red, as on *Overview*), `controls = false`,
  contact management off, the radio not reported yet, or firmware the gateway is not tested with
  (#99); above the switches also asking turned off in the settings. A list whose rows all hide
  shows no empty frame. Cards that find the contacts' devices in Home Assistant's registry, on
  every page, render again each minute, so a new contact or tracker shows within a minute.
- *Gateway* on the dashboard: *Health*, each part of the gateway with its state, those that need
  a look named first, the messages on their way counted as the first line of *Overview* counts
  them, and the airtime left of the hour's budget; *Radio*; *Radio settings*, with who may ask
  the radio for its battery, position and readings as the radio acts on it; *Latest of each
  kind*, the newest event of each kind, and a command the gateway refused, with the reason in
  words (`code_locked` too), until the next message arrives. *Settings*: what the administrator
  set, read only, from the sensor *Configuration*, each setting with its section and key, and
  the versions. While the gateway is offline, these cards say so instead of values that would
  read as never reported. *Help* names every page, button and switch, and says what the
  dashboard cannot do, the kinds of messages and what each word of the cards means
  ([home-assistant.md](docs/home-assistant.md#dashboard)).
- The update guide says how to roll back from 2.1 to 2.0: clear the retained `pending`, `config`
  and drafts, which 2.0 neither publishes nor clears, and make the dashboard again with 2.0
  ([deployment.md](docs/deployment.md#update-reconfigure-remove)).
- German, the first language besides English: the command line under a German locale (such as
  `LANG=de_DE.UTF-8`), the installer's dialogs from its language menu, and with
  `[homeassistant] language = "de"` the names in Home Assistant and the dashboard
  ([translations.md](docs/translations.md),
  [configuration.md](docs/configuration.md#homeassistant)).

### Changed

- A message that ended `failed` or `expired` no longer counts as a duplicate: the same text to the
  same target can be sent again at once. A `confirmed` one still does, for `dedup_window_s`
  ([mqtt-api.md](docs/mqtt-api.md#delivery-delivery)).
- `add_contact` and `contacts add` drop the invisible characters a copied key, contact link or
  card may carry before they read it: the soft hyphen, the zero-width space, direction marks and
  controls, the invisible operators and the byte order mark. The joiners stay, as part of a name
  ([mqtt-api.md](docs/mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)).
- Tested with Home Assistant 2026.11 as well. Since 2026.11 it clears a deleted device's discovery
  topic once for each of its entities; the gateway takes the copies as one deletion (#115).
- A command sender with a code secret gets at most 5 wrong codes an hour; a command without a
  code counts too, but not a command that came late (it waited in the radio or the mesh) with
  the sender's code of the time it was sent. Then the gateway refuses its commands without a
  look at the code, with the new reason `code_locked`, until the first of them is an hour old,
  and its log warns each time the sender reaches the limit; the times are in `state.json`, so a
  restart does not end it. Before, nothing counted wrong codes: a host posing as the radio over
  TCP found a code within a minute, a stolen handheld over the air in about two weeks; now a
  guesser wins with about 1 in 2,800 a day
  ([design §7.4](docs/design.md#74-residual-risks)) (#79).
- With `[messages] confirm_nonce`, the random suffix of a direct message reaches the broker only
  once its delivery ended: in the final delivery event and the history. The `queued` and `sent`
  events and `pending` show the text without it, also for a message a stop kept when the next
  start has the setting changed. Before, every event carried it from `queued` on, so a client
  that may read the gateway's topics, with a board in range, could compute the acknowledgement's
  code and forge `confirmed` ([mqtt-api.md](docs/mqtt-api.md#what-confirmed-proves)) (#87).
- The dashboard shows three tabs, *Overview*, *Chats* (before: *Messages*) and *Contacts*, with
  their titles and without icons, so that they fit a phone. *Map*, *Gateway*, *Settings* and
  *Help* are pages without a tab: *Overview* links to them, and the *Contacts* heading has
  buttons for *Gateway*, *Settings* and *Help*. The back arrow of every page goes back to the
  page it was opened from. The radio's health moved from *Overview* to *Gateway*.
- The generated dashboard needs Home Assistant 2026.10 or later: it shows cards by template
  conditions, which the frontend evaluates since 2026.10. `meshcore-home-gateway dashboard` says
  so after every dashboard it writes. The gateway and its entities still work with Home
  Assistant 2026.8 and later ([home-assistant.md](docs/home-assistant.md#requirements)).
- *Overview* says in its first line, in a colour, whether everything is fine, and names the
  first problem; a card for each problem says what to do, in place of the problem sensors' tiles
  and of the tiles of the radio link, airtime, queue and battery. The messages waiting over a
  minute count the message being sent too. The version card shows only for another major or
  minor version, and says which of the two to update.
- The dashboard drops control and direction characters from every name and text, and breaks long
  words in messages so that they fit a phone.
- The dashboard says how a contact's device answers asking for position and battery in the
  words of a chat's page: "not asked (switch off)" for a companion nobody turned on, which 2.0
  called "excluded"; it shows times as a history does, such as "10:31" or "yesterday 10:31",
  not "5 min ago". The field to add a contact is *Add by key or contact link*; the line below it
  says to tap Return. *Gateway* has no tile of the airtime left: its *Health* row shows it with
  the budget. Make the dashboard again to get these.
- The dashboard has no tile *Write a message* and no hint to add the script *Mesh message* any
  more, which Home Assistant 2026.10 and later never showed (#110): each chat page has its own
  field. The guide keeps the script for automations.
- The dashboard's card *Telemetry and removal*, a fixed list of the contacts' switches and
  remove buttons, which pressed a button without asking first, is gone; each contact's switch
  and *Remove* now stand in *Ask for position and battery* and *Add or remove*.

### Fixed

- With `[homeassistant] controls = false`, the controls are unavailable in Home Assistant as soon
  as it has the gateway's announcement, and available again at once when the setting is back to
  `true`; before, the change waited for the next message on the gateway's availability topic
  (#82).
- The `history` document stays within 15 KiB as a whole; before, only its messages did, and the
  document could be up to 43 bytes larger (#111).
- The guide said *Queue* counts the messages waiting for the radio. It counts those waiting behind
  the one being sent, so a single message that waits reads 0 (#112).
- The guide's recorder block, meant to keep message texts out of Home Assistant's database, left
  in the texts sent and the commands' arguments: it now also excludes *Delivery* and *Command*.
  It is written as patterns only, which fit any node ID, a second gateway and the IDs Home
  Assistant numbers with `_2`, and the guide says what each line keeps out and what to write for
  a `contact_entity_prefix` of your own ([home-assistant.md](docs/home-assistant.md#privacy))
  (#116).
- A send request or the answer to a command that came while a stop handled the last received
  messages was published `queued` and then lost: the stop had kept the queue already. The stop
  now keeps the queue as its last step, with these requests and those still waiting to be taken.
  A request after it, such as a press of *Send* or a request that comes as the radio closes, ends
  `rejected` with the new reason `stopping`, until the gateway publishes `offline`, which the
  dashboard shows as "the gateway was stopping"
  ([mqtt-api.md](docs/mqtt-api.md#delivery-delivery)) (#92).
- `retire` also clears the Homie device `homie/meshcore-<node_id>/…`, also with `[homie]` off
  now while the broker lets the gateway's user read and write it; before, its contacts' names,
  battery, last heard and position stayed on the broker (#96). It reads the retained messages
  with QoS 0 now, until none has come for a second, and looks again after clearing: one left is
  an error. With QoS 1, Mosquitto passes on at most 1040 retained messages to a subscription, and
  a device with 50 companions has 1062. It leaves alone a Homie device that a gateway is
  connected to: node IDs that differ only in `_` and `-` share one. Switching `[homie]` off
  leaves the device there: the privacy page lists it, and
  [mqtt-api.md](docs/mqtt-api.md#homie-homiehomie_device) says how to clear it.
- A start announces the Homie device once, with its nodes. It stays `init` until 5 s after the
  radio's first contact table (at most 65 s), and a change of its nodes waits 5 s for those that
  follow it. Before, it went out `ready` without nodes and then once more for every contact, up
  to 50 times a start, each making a controller read it again (#97).
- The Homie device has a node `gateway` with the property `radio` (`connected` or
  `disconnected`), so that `$nodes` is never empty. Before, a device without contacts sent an
  empty `$nodes`, which removes the retained one: openHAB, which requires `$nodes`, never brought
  the device up (#97). Its empty `$extensions` reaches only the controllers subscribed at the
  time, as Homie 4.0.0 has no other form of an empty value; openHAB does not read it
  ([mqtt-api.md](docs/mqtt-api.md#homie-homiehomie_device)).
- A text on a Homie node's `send/set` that came after the gateway published `offline` as it
  stopped was still echoed, also after `$state` `disconnected`, and then lost without a delivery
  event. It is no longer taken or echoed. One that comes earlier in the stop takes the way of one
  on `contacts/<key>/send`: it is kept for the next start, or ends `rejected` with reason
  `stopping` ([mqtt-api.md](docs/mqtt-api.md#homie-homiehomie_device)).
- After a restart of the broker the log no longer warns that another client wrote the gateway's
  documents. paho sends again what the broker had not acknowledged when it stopped, such as a
  state from before the radio was connected; the gateway takes those copies as its own, and
  publishes the current document again if the old one arrived last. As it stops, the broker still
  sends the gateway its own last will, which the log names once for `availability`: harmless
  ([deployment.md](docs/deployment.md#5-troubleshooting)). The system test of a broker restart
  read the state paho sent again for the current one, and failed now and then (#101).
- The guide's restore of a backup puts each file back with its owner, group and mode: the
  configuration and the secrets `root:meshcore-home-gateway` 0640, the state the service's own.
  It said only where the files go, and the archive holds them as `root` with mode 0600: in a new
  container the copy of the codes file of `--with-secrets` kept that, and the service stopped
  with a configuration error ([deployment.md](docs/deployment.md#4-operations), #98). It also
  removes the history's write-ahead log, which a stop cut short leaves behind: SQLite applied it
  to the restored history at the next start, which then held the old messages again.
- The security documents state three controls as strong as they are (#105). A command code shows
  that the sender's app made it in that time window, but does not bind the text: whoever reads a
  command before the gateway, such as a host posing as the radio over TCP, can send another text
  with its code, once ([configuration.md](docs/configuration.md#commands)); the guide said it
  authenticates the text. The broker password and the code secrets are files the service's group
  reads; a systemd credential that root alone reads takes a drop-in, which the guide now gives
  for the codes file too ([deployment.md](docs/deployment.md#4-operations)); the threat model
  listed the credential as a control. And `time-sync.target`, which the unit is ordered after,
  waits for a synchronised clock only where `systemd-time-wait-sync` or `chrony-wait` is enabled,
  neither by default nor in a container; the unit, the design, the guide and the entry of 2.0.0a1
  said the service starts after the clock was synchronised.
- User documents that 2.0.0 left outdated (#106): the privacy page named `message` events, a
  topic the gateway never had, where received messages go to `received` and
  `contacts/<key>/received` ([privacy.md](docs/privacy.md)); the README said that every message
  is published, where only the channels that are a target are, by default, and a flood is cut to
  bursts; the update note of 2.0.0 left out that telemetry also asks the contact targets and
  command senders, at once after the update
  ([deployment.md](docs/deployment.md#update-reconfigure-remove)), as did the summary of 2.0.0
  above; the Home Assistant guide called *Telemetry* and *Telemetry state* the companions' alone,
  where `[telemetry] ask_types` gives sensors both, and said that contact entity IDs start with
  `mesh_`, where `contact_entity_prefix` chooses the start
  ([home-assistant.md](docs/home-assistant.md#contacts)); and the design's note on the
  dashboard's template did not name all that `dashboard.py` fills in and keeps.
- The triage log of the review of 2026-11 states where the work is: its state was that of
  2026-11-04, with pull requests up to #64, and its 162 fixed items counted six that defer a
  part. It now lists the 10 deferred items, the 6 deferred parts and the 3 drafts for MeshCore
  not sent, each with the condition that brings it back and the issue that tracks it
  ([triage log](docs/audit/2026-11-security-architecture-review/13-triage-log.md#deferred-to-be-tracked),
  #104).
- The update guide: from 1.x to 2.0, the setting that a radio over TCP (`[radio]
  tcp_unauthenticated`) or commands over a broker without credentials or TLS (`[mqtt] insecure`)
  need cannot come from `--reconfigure`, which asks with the installed version. The guide shows
  the line to add by hand right before `--update`, and the troubleshooting table has a row for
  `mqtt.insecure` ([deployment.md](docs/deployment.md#update-reconfigure-remove)) (#78).
- The development guide: a release that is due is stopped by disabling the workflow *Release
  due*, not by a revert of its version, which the release plan refuses; a run of `release.yml`
  that failed before it published is re-run on the same tag
  ([development.md](docs/development.md#release)) (#85).
- With `set_clock`, the radio is no longer restarted for a clock 1 minute to 1 hour ahead when
  no contact changed for longer than `set_clock_max_days`, or the host's clock is before the
  release: the restart starts the clock at the newest contact time, and the next connect then
  refused to move it forward, which left the radio weeks behind. The log says why it waits
  ([deployment.md](docs/deployment.md#5-troubleshooting)) (#86).
- *Radio link taken over* (`radio_link_contested`) is on also when the radio restarted after it
  closed the gateway's TCP link: the firmware closes it only for another client, and that client
  can restart the radio to hide that it read the key. Before, the restart read as a harmless one,
  and the log names it now (`radio: the radio closed the TCP connection, then restarted`). The
  gateway's own restart for the clock no longer counts as a link the radio closed. The guide
  calls the sensor what it is, a best effort: `false` is no proof
  ([mqtt-api.md](docs/mqtt-api.md#state-state)) (#88).
- Names are compared without any of the characters drawn as nothing, not only without the
  format characters: a node named `Bob` with a variation selector, a grapheme joiner, a Hangul
  filler or a braille blank added gets the `name_taken` mark, ` (name taken)` in *Add heard
  node* and, once added, its key's digits in its device name. A combining mark that shows, such
  as an accent, still makes another name. A name of such characters alone, such as one Hangul
  filler, is still a name: a blank one, the same as every other blank name; one of format
  characters alone is none. One table of these characters serves the names and what
  `add_contact` drops from a copied key or link
  ([mqtt-api.md](docs/mqtt-api.md#nodes-heard-nodes)) (#89).
- The guide names a limit of `suspect`: its bound takes every hop at the radio's coding rate,
  so where repeaters or the recipient send at a faster one (4/5 against the radio's 4/8), a
  genuine acknowledgement of a long text can read `suspect`. Set the gateway's radio to the
  fastest coding rate of its mesh
  ([mqtt-api.md](docs/mqtt-api.md#what-confirmed-proves)) (#90).
- The guide says what `at` of a received message in `history` and `received_at` of `received`
  are: when the gateway took the message from the radio, whose frame holds no time of its own.
  The messages that waited in the radio while the gateway was away share the time of the
  connect; of two with one `at`, the one listed first came later. To list them oldest first,
  reverse the list, then sort it by `at` with a stable sort
  ([mqtt-api.md](docs/mqtt-api.md#history-history)) (#113).
- The guide says that contact names, like texts, are published as they came, control
  characters included, also with `clean_received`, which cleans only texts and the name a
  channel message claims; and that the 15 KiB of `history` hold fewer messages whose names and
  texts are full of control characters, about 10, since JSON writes each in 6 bytes
  ([mqtt-api.md](docs/mqtt-api.md#received-messages-received)) (#114).
- A stop cut short while it waited up to a minute for an acknowledgement lost every queued
  message: the queue was written only after the wait. A kill does that, and so does the unit of
  1.x with `TimeoutStopSec=20`, which a manual installation keeps when it updates only the
  zipapp. The queue is now in `state.json` from the stop's start, with the message in flight and
  its attempts, and written again at each change in the stop. The guide for other Linux systems
  has an update step, which installs the release's unit
  ([deployment.md](docs/deployment.md#3-other-linux-systems)) (#92).
- An alert waited behind a message that the gateway had taken out of the queue and that waited
  for the radio or for airtime before its first attempt: until that message had used the airtime
  and its acknowledgement had come, with the default budget more than a minute and a half. Such
  a wait now ends when an alert comes; the message goes back to the queue, first among the
  others, and continues after the alert
  ([configuration.md](docs/configuration.md#messages)) (#93).
- An acknowledgement that came in the moment its wait ended, by its time or by an alert, was
  lost: the message sent one more attempt, and could end `failed` with `no_acknowledgement`
  although it had arrived (#93).
- After a command's write of `state.json` failed (a full disk), `state_file` stayed `unwritable`
  and *Gateway degraded* on, and `status` exited 1, until some other part of the state changed,
  which can take hours on a quiet system. The gateway now writes the file again within a minute,
  also with nothing else changed, and clears both once the write succeeds (#94).
- After an update from 1.x, the retained `history` document of the start showed the newest
  messages of the history database, also those of channels that are no target, which 1.x had
  kept: strangers' texts and claimed names on the Public channel went back to the broker until
  50 new messages pushed them out. The document now leaves out the received messages of channels
  that are not published (`publish_channels`, `ignore_channels`), and shows the newest of the
  rest, also from behind them; the gateway's own messages to a channel target stay, as while it
  runs. The database keeps them until they are `messages_days` old; the update guide says how to
  be rid of them at once ([deployment.md](docs/deployment.md#update-reconfigure-remove)) (#103).
- The guide's roll back did not say what 1.x does with the state directory of 2.0: it asks every
  companion that is not excluded for telemetry, other people's too, and its first exclusion
  writes `telemetry-excluded.json` without the household's inclusions, so that 2.0, back again,
  asks none of them. It now says to keep a copy of the file and switch telemetry off first, and
  that the messages a stop kept stay in `state.json` while 1.x runs
  ([deployment.md](docs/deployment.md#update-reconfigure-remove)) (#95).
- When a broker that speaks MQTT 3.1.1 only refused the connection of `[mqtt] protocol = "5"`
  (`Unsupported protocol version`), the log said to test the login, and `check-config --broker`
  pointed to the broker's users and `mosquitto_passwd`. Both now say that the broker does not
  speak that version and name the setting
  ([deployment.md](docs/deployment.md#5-troubleshooting)) (#107).
- `doctor` said that security updates are installed automatically as soon as
  `unattended-upgrades` was installed, also with `APT::Periodic::Unattended-Upgrade "0"`. It now
  reads that setting and `APT::Periodic::Update-Package-Lists` as apt's daily job does, and warns
  when either is `"0"` or missing (#107).
- The release plan told a change checked before a merged release was tagged that CI on main tags
  it once its jobs pass. CI on main tags only pre-releases; the message now names
  `release-due.yml`, which tags a release a day after its version commit, and the time from which
  it does (#107).
- Removing a command sender that had a code secret stopped the gateway: the secret made the
  configuration an error (exit 78), which systemd does not restart, so alerts, telemetry and Home
  Assistant went down with the sender's commands. The installer's `--reconfigure` got there on
  its own when a sender was unticked or the insecure broker was declined. The gateway now ignores
  the secret of a key that is not a sender, and its start, `check-config` and `doctor` warn about
  it. Such a secret may be a sender's under its key before a correction by hand, and the
  corrected key has none: so while one is ignored, the commands of every sender without a secret
  are refused as `code_missing`, which counts as no wrong code, and the warnings name each such
  sender. That lasts until `commands revoke` removes the secret or `commands enrol` gives the
  sender one, and the service restarts. Once no secret is ignored, every sender without one
  commands without a code again: `commands revoke` then names them, and the warnings say to
  enrol a key corrected by hand first. Senders with a secret command with their codes. The
  installer shows the self-test's report also when it passed, with these warnings. 2.0 refuses
  such a secret again (exit 78): before a downgrade to 2.0, remove it with `commands revoke`
  (#80).
- The dashboard's cards that show only while they have something to say (the problems on
  *Overview*, the line under a chat's field that says a message was not sent, the map's hints)
  now appear on a page that is open. Home Assistant took such a card out of the page once it was
  empty, so it stayed away until the page was loaded again; it now shows by a condition of its
  own content.
- The dashboard's switches and remove buttons of the contacts show only while their entity is
  that of this gateway's contact with the key the dashboard was made for: a second gateway's
  dashboard operated the first gateway's contacts of the same name (#81), a removed contact's
  rows turned into warnings that the entity was not found (#100), and a contact prefix set later
  emptied the contacts table (#81). A yellow line names the entity ID such a contact has, for the
  administrator.

### Security

- The release workflow checks a tag pushed by hand as well. Its commit must be on `main`. A
  release must be where the hourly `release-due.yml` would tag it: on the commit that wrote the
  version of `main`, a day after that commit's date, with CI passed on it on `main`, and while
  `release-due.yml` is enabled, so that disabling it stops the release. A pre-release needs a
  commit of `main` with its version. Before, a `v*` tag pushed by hand on any commit was signed
  and published within minutes. A release tagged by hand within its day is refused with the time
  it is due, and `release-due.yml` starts it then. The check is part of the tagged commit, so a
  tag on a commit off `main` can still bring a workflow without it
  ([SECURITY.md](SECURITY.md#how-the-repository-and-the-releases-are-protected), #84).
- `SECURITY.md` names the exception to the protection of `main` and of the release tags: both
  rulesets let a repository role bypass them at any time, which the maintainer's login has, and
  the agent works through that login. It can push to `main` directly and move or delete a tag
  until its release is published (#102).
- The dashboard's *Overview* names a radio link another program took over as a problem, in
  red, with what to do. Before, it said "Gateway ready." while such a program could control the
  radio and read its private key, and no card showed the sensor *Radio link taken over* (#83).

## [2.0.0] - 2026-11-07

The second stable release. It ships the program of 2.0.0rc3, which CI, the installer's self-test
on the maintainer's installation and his use of Home Assistant 2026.11.0 checked
([release check](docs/testing.md#release-check)).

Since 1.0.0 the gateway asks the household's companions, and with `[telemetry] ask_types` also its
sensors, for their battery, readings and position. Home Assistant shows them, with the
companions' traces on a map, and each contact has a switch *Telemetry* (1.1.0a1 to 1.1.0a3,
2.0.0a1). `meshcore-home-gateway dashboard` writes a Home Assistant dashboard, the texts come from
translation catalogs (English is the only language so far), and the gateway is tested against
MeshCore's real firmware (1.1.0a4). With the changes of the independent security and
architecture review of 2026-11 (2.0.0a1) the gateway keeps less of other people by default,
refuses setups that would let strangers command it or control the radio, and keeps its outgoing
queue over a stop. New are MQTT 5, a Homie device, command codes from an authenticator app, the
commands `status`, `doctor`, `backup`, `retire` and `history export`, and the MQTT API as an
AsyncAPI document. The installer ends with the gateway's own check (2.0.0rc1, 2.0.0rc2), and the
gateway keeps the radio's clock on the host's (2.0.0rc3).

Read the **Breaking:** entries of 2.0.0a1 before updating from 1.x: a radio over TCP and commands
over a broker without credentials or TLS each need a setting, telemetry asks only the companions
the household includes, the MQTT client id gets a random part, only the channels that are a target
are published, and of other people's radios only their keys and types are kept
([deployment.md](docs/deployment.md#update-reconfigure-remove)). The sections from 1.1.0a1 to
2.0.0rc3 of [CHANGELOG.md](CHANGELOG.md) list every change.

## [2.0.0rc3] - 2026-11-06

The release candidate of 2.0.0: the program of 2.0.0rc2, and the gateway keeps the radio's clock
on the host's in both directions. 2.0.0 ships this program
([testing.md](docs/testing.md#release-check)).

### Changed

- With `[radio] set_clock` (the default) the gateway keeps the radio's clock on the host's. It
  moves a clock that falls behind forward at every heartbeat, not only when it connects. A clock
  a minute to an hour ahead, which the firmware sets back at a start alone, it sets back by
  restarting the radio: when no message is queued or being delivered, at most once a day, after
  taking the radio's messages. The radio is gone for a few seconds; over USB it comes back under
  the same device ([design §6.1](docs/design.md#61-radio-session)).

### Fixed

- `doctor` sent a radio clock ahead of the host's to `[radio] set_clock`, which could not move it
  back, and said that messages and commands carried the wrong time; they carry the host's. It now
  tells a clock ahead from one behind and says what the gateway does about each.

## [2.0.0rc2] - 2026-11-06

The release candidate of 2.0.0: the program of 2.0.0rc1 with the installer's self-test fixed,
which had stopped the release check of 2.0.0rc1 with an empty report. 2.0.0 ships this program
([testing.md](docs/testing.md#release-check)).

### Changed

- A release candidate is published once CI passed on it, as every pre-release, so that a faulty
  one is followed by the next at once. A release still waits a day after its version reached
  `main`, and ships the program of a candidate tagged before
  ([development.md](docs/development.md#release)).

### Fixed

- The installer's self-test, run from a terminal on the Proxmox host, found problems and showed
  an empty report: `pct exec` gives the command a terminal when one of the installer's outputs is
  one, and hung `doctor` up as soon as it started (exit status 129). It now runs `doctor` without
  one; a report that stays empty names doctor's exit status.

## [2.0.0rc1] - 2026-11-05

The release candidate of 2.0.0: the program of 2.0.0a1, and the installer now ends with the
gateway's own check. 2.0.0 ships this program. It is the first release checked without the
hardware checklist: CI, the installer's self-test on a real installation and the maintainer's use
of Home Assistant check it ([testing.md](docs/testing.md#release-check),
[ADR 0030](docs/decisions/0030-release-checked-by-ci-self-test-and-use.md)).

### Added

- The installer ends with the gateway's own check: after an installation, a reconfiguration and an
  update it runs `meshcore-home-gateway doctor` in the container until it passes, for about a
  minute while the radio connects. The final message says `Self-test: passed`, or shows the
  problems with doctor's report; the service keeps running either way
  ([deployment.md](docs/deployment.md#install)).

### Changed

- A release is checked by CI, the installer's self-test and the maintainer's use of Home Assistant
  with its release candidate, no longer by the hardware checklist, which now describes the
  behaviour on real radios and names the test of each step
  ([ADR 0030](docs/decisions/0030-release-checked-by-ci-self-test-and-use.md)). The Homie device
  is maintained by those who use it: its documents keep their tests, openHAB is not checked
  before a release.

### Fixed

- The hourly release run tags a release candidate or a release only once CI passed on the commit
  that wrote its version, and starts its release only once. It had tagged 2.0.0a1 a minute after
  the merge, before CI on `main` had finished; 2.0.0a1 holds exactly what CI passed in its pull
  request.

## [2.0.0a1] - 2026-11-05

A pre-release of 2.0.0, with the changes from the independent security and architecture review of
2026-11 ([issue #34](https://github.com/dkuenzel/meshcore-home-gateway/issues/34), [triage
log](docs/audit/2026-11-security-architecture-review/13-triage-log.md)). By default the gateway now
keeps less of other people. It refuses setups that would let strangers command it or control the
radio. The outgoing queue survives a stop. New options: MQTT 5, a Homie device, command codes from
an authenticator app, and the MQTT API as an AsyncAPI document.

Read the **Breaking:** entries under *Changed* before updating. A radio over TCP and commands over a
broker without credentials or TLS each need a setting, added right after the update. Telemetry asks
only the companions the household includes: switch *Telemetry* on for each of them
([deployment.md](docs/deployment.md#update-reconfigure-remove)). The hardware checklist was not run
for it.

### Added

- `radio_clock_offset_s` in the `state` document and the diagnostic sensor *Radio clock offset*:
  how far the radio's clock is ahead of the host's, read at connect and with every heartbeat
  ([mqtt-api.md](docs/mqtt-api.md#state-state)). The radio's clock cannot be set back, so an
  offset far above 0 needs attention
  ([deployment.md](docs/deployment.md#5-troubleshooting)).
- `[radio] set_clock_max_days` (default 30, 0 for no limit): the largest step by which
  `set_clock` moves a radio clock that was set
  ([configuration.md](docs/configuration.md#radio)).
- `[mqtt] insecure` (default `false`): accepts a broker connection without a user name, or
  without TLS to a broker that is not on the gateway's host or link, and silences the warnings
  about it ([configuration.md](docs/configuration.md#mqtt)).
- `check-config --broker` logs in once more without credentials and warns when the broker takes
  such a client: then only its ACL keeps anyone who reaches it off the gateway's topics
  ([mqtt-api.md](docs/mqtt-api.md#securing-the-broker)).
- The gateway subscribes to its retained topics (`availability`, `state`, `stats`, `contacts`,
  `contacts/<key>`, `nodes`, `history`, `settings`), to undo what other clients write there
  (*Security*); its user needs read access to them, as the documented ACL gives it.
- `[radio] tcp_unauthenticated` (default `false`, required as `true` with `tcp`): says that you
  know what the radio's TCP interface exposes ([configuration.md](docs/configuration.md#radio)).
- `radio_link_contested` in the `state` document and, with a radio over TCP, the diagnostic
  problem sensor *Radio link taken over*: on for an hour after the radio closed the gateway's TCP
  link while it kept running, as it does when another client connects; that client can control
  the radio and read its private key ([mqtt-api.md](docs/mqtt-api.md#state-state), what to do:
  [deployment.md](docs/deployment.md#5-troubleshooting)).
- `[messages] clean_received` (default `false`): received texts lose their control and direction
  characters before they are published and kept in the history, as outgoing texts do
  ([configuration.md](docs/configuration.md#messages)). Off, texts stay as they came.
- `[messages] publish_channels` (default `targets`): only the messages of channels that are a
  target are published and kept; on the *Public* channel, whose key is in the firmware, most
  messages are strangers'. `all` publishes every channel
  ([configuration.md](docs/configuration.md#messages)).
- `[contacts] heard_nodes` (default `keys`): of the nodes heard but not added, other people's
  radios, only their keys and types are published and kept, no names, times or signal, also in
  the history and in the select *Add heard node*; `off` nothing but the keys of look-alikes,
  `full` everything ([configuration.md](docs/configuration.md#contacts)).
- [`docs/threat-model.md`](docs/threat-model.md): the assets, the actors, seven trust boundaries
  and what stands in the way at each; design §7 is now its summary, and every decision record
  names the boundaries it touches. [ADR 0029](docs/decisions/0029-what-a-contact-is-trusted-with.md)
  states what a contact of the radio is trusted with, design §11.1 the choices that had no record.
- [`docs/architecture.md`](docs/architecture.md), the short way in; the observer, Home Assistant
  and telemetry have design documents of their own, telemetry with state and sequence diagrams
  ([design §6](docs/design.md#6-detailed-design) lists where each section went).
- [`docs/privacy.md`](docs/privacy.md): what the gateway keeps of the household and of other
  people, where, for how long and how to switch it off; a *Privacy* section in the README.
- `[telemetry] ask` (default `household`): only the companions the household included (the switch
  *Telemetry*, `contacts telemetry KEY on`) and the contact targets and command senders are
  asked; a new contact is not asked until included, and the first start holds nothing. `all` asks
  every companion not excluded. The inclusions are kept in `telemetry-excluded.json` beside the
  exclusions ([configuration.md](docs/configuration.md#telemetry)).
- `[homeassistant] controls` (default `true`): with `false`, Home Assistant shows the controls
  that change the radio or the telemetry unavailable (*Add heard node*, *Add contact*, *Show
  hidden contacts*, each contact's *Remove from radio* and *Telemetry*), so no user of it can
  operate them; MQTT and the command line keep working.
- `[homeassistant] hidden_targets`: targets without a notify entity in Home Assistant, such as an
  alarm channel only automations should use.
- `[homeassistant] contact_entity_prefix` (default `mesh`): how the contacts' entity IDs begin,
  for a second gateway.
- The Home Assistant guide: *Shared households* (what every Home Assistant user sees and can do,
  and the settings above), *A second gateway* (one `person` for two trackers), and an automation
  that tells when the gateway or its radio has been away for five minutes
  ([home-assistant.md](docs/home-assistant.md#shared-households)).
- `name_taken` in the `nodes` document (each entry, and the keys at the top) and in the `node`
  event: the node's name, as a person reads it (NFKC, case folded, Cyrillic and Greek letters that
  look Latin as those), is a contact's, the gateway's or another heard node's
  ([mqtt-api.md](docs/mqtt-api.md#nodes-heard-nodes)).
- `[mqtt] password_file`: the broker password in a file of its own instead of `password`, so the
  configuration, `config answers`, backups and screenshots of it carry only its path; a systemd
  credential named like the file (`LoadCredential=`) is read instead, so the file may be root's
  alone. `config render --password-file <path>` writes the answer `mqtt.password` there
  ([configuration.md](docs/configuration.md#mqtt), [deployment.md](docs/deployment.md#4-operations)).
- `[messages] confirm_nonce` (default `false`): direct messages end with ` #` and 8 random hex
  digits, the same on every attempt, so that their acknowledgement, a hash of the text, cannot be
  computed by a stranger who guesses a fixed text; direct texts are then limited to 150 bytes,
  and the events and the history carry the text as sent
  ([configuration.md](docs/configuration.md#messages)).
- `suspect` in `delivery` events: `true` in a `confirmed` one whose round trip was shorter than
  the message's path through repeaters allows, as a forger beside the gateway's radio answers;
  the log warns, the dashboard marks it and `send --wait` exits 1
  ([mqtt-api.md](docs/mqtt-api.md#what-confirmed-proves)).
- `health` in the `state` document and the problem sensor *Gateway degraded*: whether the
  history works, the state file and the exclusions from telemetry are written, how many bugs the
  gateway caught in the last hour, how long its event loop was held up, how many events were
  dropped while the broker was away, and the free disk space; `degraded` while one is not as it
  should be, which `systemctl status` and the dashboard show too
  ([mqtt-api.md](docs/mqtt-api.md#state-state), what to do:
  [deployment.md](docs/deployment.md#5-troubleshooting)).
- A warning at the start and in `check-config` when `[diagnostics] log_level` is `debug`, which
  puts texts, positions and keys into the journal.
- `[messages] alert_targets`: the targets whose messages leave before the others; an alert also
  goes between two attempts of a message in flight, which continues after it. A request chooses
  by its target, so a second target for the same recipient gives a client both
  ([configuration.md](docs/configuration.md#messages)).
- `manage_reason` in the `contacts` index, and as an attribute of *Contacts* in Home Assistant:
  why requests to add or remove contacts are refused now (`management_off`, `no_radio`,
  `untested_firmware`), `null` while they are taken; `contacts list` says it after the lists. After
  a firmware update of the radio, Home Assistant's controls turn unavailable until a version of
  the gateway tested with that firmware is installed: this says why
  ([mqtt-api.md](docs/mqtt-api.md#contacts-contacts-and-contactskey),
  [deployment.md](docs/deployment.md#5-troubleshooting)).
- `disabled` as the state of a setting in the `settings` document, apart from `unsupported`: the
  radio answered that the command is compiled out of its firmware. Like an unsupported one, it is
  not written to that firmware again; `probe` and the installer's plan name it.
- The installer installs `unattended-upgrades` in the container, which installs Debian's
  security and point-release updates every day; `--update` installs it in an older
  installation. It limits the journal to 100 MB, empties the package cache after installing,
  refuses a radio that another container of the host already passes through, and its final
  message names how the broker connection is secured (TLS or not, the user name or none) and, for
  a radio over TCP, that the radio is reached without authentication
  ([deployment.md](docs/deployment.md#what-the-script-does)).
- A release's unit names its version in its second line, which tells after a rollback which
  version it came with.
- Commands for an installation's life ([design.md](docs/design.md#69-command-line),
  [deployment.md](docs/deployment.md#4-operations)): `status` shows what the running gateway
  reports (its radio, clock, airtime, queue, telemetry, health and contacts; `--json` for scripts)
  and exits with 1 while it is offline or degraded; `doctor` checks the configuration, the broker
  (login, TLS, clients without a user name), the running gateway and the host (free space,
  automatic updates); `backup FILE` writes the configuration and the state into an archive while
  the service runs, without the broker password and the code secrets unless `--with-secrets`;
  `retire --yes` removes the gateway from Home Assistant and clears its retained documents before
  its container goes;
  `history export` prints the history's messages, positions or readings as JSON lines or CSV.
- Codes for commands ([configuration.md](docs/configuration.md#commands)): a sender with a code
  secret in `[commands] codes_file` ends each command with the 6-digit code of an authenticator
  app (TOTP, RFC 6238), which the gateway accepts once, for its clock's 30 seconds and those before
  and after. The code authenticates the text, so neither a host posing as a radio over TCP nor a
  node sharing the sender's key prefix can command without the secret.
  `meshcore-home-gateway commands enrol KEY` writes a new secret and prints it as an `otpauth://`
  line for the app. New rejection reasons `code_missing`, `code_invalid` and `code_reused`
  ([mqtt-api.md](docs/mqtt-api.md#commands-command)); senders without a secret keep the rules.

- `[mqtt] protocol = "5"` (default `"3.1.1"`): the gateway connects with MQTT 5. Its messages
  carry the user properties `gateway`, `version` and `published_at`, its `delivery`, `received`
  and `command` events an expiry (`queue_ttl_s`, for commands `max_age_s`), and the delivery
  events of a send request that request's correlation data; response topics are ignored
  ([mqtt-api.md](docs/mqtt-api.md#mqtt-5)). Home Assistant connects with MQTT 5 by default.
- `prefix` and `suffix` of a target ([configuration.md](docs/configuration.md#targetsalias)):
  text put before and after every message to it, such as `"Shed: "`; they count toward the
  radio's limit and stay whole when a text is cut. The installer keeps them on `--reconfigure`.
- `[commands] reply` (default `"none"`): `"accepted"` answers the sender of a published command
  by direct message with `ok: <word>`, `"all"` also a command refused after the sender checks
  with `rejected: <reason>`; a sender the checks did not identify gets no answer
  ([configuration.md](docs/configuration.md#commands)).
- A Home Assistant script that sends a message to a contact target and waits for its final
  delivery event, and an automation that escalates when it was not confirmed
  ([home-assistant.md](docs/home-assistant.md#examples)).
- `[telemetry] ask_types` (default `["chat"]`): `["chat", "sensor"]` also asks the household's
  sensors (MeshCore's sensor firmware) for battery, readings and position, as companions are
  asked, with the same switch, state, exclusions and inclusions. A sensor answers only a radio
  in its access list: `setperm <the gateway radio's key> 1` on the sensor
  ([companions.md](docs/companions.md#sensors)).
- `contrib/mqtt2prometheus`: configurations for the exporter mqtt2prometheus, which serves the
  gateway's `state` and `stats` and the contacts' documents to Prometheus without a listener in
  the gateway ([README](contrib/mqtt2prometheus/README.md)).
- `make firmware-play` for contributors: the hardware checklist's household on the firmware's
  simulated air, a local broker and the gateway, for exploring by hand until Ctrl-C
  ([testing.md](docs/testing.md#tests-against-the-real-firmware)).
- The MQTT API for machines: `docs/mqtt-api.asyncapi.yaml`, an AsyncAPI 3.1 document of every
  topic the gateway publishes and reads, with QoS, retain flag and parameters, and the keys and
  values of each document, generated from the interface snapshot so that it follows the code.
  Each release attaches the document of its version, `meshcore-home-gateway-X.Y.Z.asyncapi.yaml`,
  listed in `SHA256SUMS` ([mqtt-api.md](docs/mqtt-api.md)).
- `[homie] enabled`: the radio's contacts as a device of the Homie convention 4.0.0, which openHAB
  and the other Homie controllers discover without configuration: a node per contact (at most
  `[homie] contacts`, 50) with battery, position, last heard and telemetry state, and for
  companions a field to send a text, fed by the same documents as Home Assistant. Off by default;
  the device has a connection of its own, with `$state` as its last will, and the broker's ACL
  needs `homie/meshcore-<node_id>/#` for the gateway's user
  ([mqtt-api.md](docs/mqtt-api.md#homie-homiehomie_device)).
- The dashboard's *Contacts* view has a card *Telemetry and removal* with each contact's switch
  *Telemetry* and button *Remove from radio*, for household users who open no device page; the
  last delivery step and the last contact event show their reason as a text, a reason without
  one as its code ([home-assistant.md](docs/home-assistant.md#dashboard)).
- Documentation: the four USB release builds of MeshCore 1.18.0 that have no USB interface, among
  them the T-Echo Lite's with display, and what the gateway reports for them; the T-Echo Lite's
  `non_shell` USB build, which works but never draws on the e-paper (platform notes,
  troubleshooting).

### Changed

- **Breaking:** a radio over TCP is a configuration error until `[radio] tcp_unauthenticated =
  true` accepts it: whoever reaches the TCP interface controls the radio, and the release
  firmware hands that client the radio's private key, which decrypts every direct message of the
  radio, also those recorded from the air before. Use USB, or put the radio in a network that
  only the gateway reaches and add the setting. The version before does not know the setting, so
  update right after adding it; `--reconfigure` asks for it.
- **Breaking:** commands (`[commands] senders`) over a broker connection without a user name,
  or without TLS to a broker beyond the gateway's host or link, are a configuration error until
  `[mqtt] insecure = true` accepts it: any client that reaches the broker, or reads the password
  on the network, could publish command events. Add credentials and TLS, or the setting, before
  updating: the version before does not know the setting, so update right after adding it (the
  installer's `--update` keeps the installed version while the configuration does not pass).
- **Breaking:** the default MQTT client id gets a random part per start,
  `meshcore-home-gateway-<node_id>-<8 hex digits>`: a broker drops a client when another
  connects with its id, and the fixed one could be read off the topics. A broker ACL or
  `clientid_prefixes` that names the id can use the prefix `meshcore-home-gateway-`; set
  `[mqtt] client_id` to keep a fixed one.
- **Breaking:** only the messages of the channels that are a target are published and kept in
  the history (`[messages] publish_channels = "targets"`): the neighbours' chatter on the
  *Public* channel no longer reaches the bus, Home Assistant or the history. Set
  `publish_channels = "all"` after the update to keep every channel; the version before does not
  know the setting.
- **Breaking:** of the nodes heard but not added, other people's radios, only their keys and
  types are published and kept (`[contacts] heard_nodes = "keys"`): no names, times or signal in
  `nodes`, the `node` events, the history or the select *Add heard node*. Set
  `heard_nodes = "full"` after the update to keep them.
- **Breaking:** telemetry asks only the companions the household included, and the contact
  targets and command senders (`[telemetry] ask = "household"`): after the update no other
  companion is asked until it is included with its switch *Telemetry* or
  `contacts telemetry KEY on`. Set `ask = "all"` after the update to keep asking every companion
  that is not excluded.
- The select *Add heard node* (and the dashboard's table of the nodes heard) marks a node with a
  taken name ` (name taken)`; a contact whose name the gateway's device or a contact added before
  has gets a device named with its key's first 8 digits, such as *Bob (b0b0b0b0)*.
- The clock of the radio is read also without `set_clock`, to show its offset.
- The service starts after `time-sync.target` where a service provides it (`systemd-timesyncd`,
  `chrony`): the radio's clock is set from the host's.
- The installer asks for TLS in the default setup too, and an empty user name must be
  confirmed; with commands over a connection without either, it asks to accept that
  (`mqtt.insecure`), or the commands are left out ([deployment.md](docs/deployment.md)).
- The installer says what a radio over TCP exposes and asks to accept it
  (`radio.tcp_unauthenticated`), or offers the radios again; `check-config` calls the TCP radio
  unauthenticated in its summary.
- The traceback of a bug that a part of the gateway catches is logged at most once a minute per
  place, with how many came since, instead of with every event that meets it.
- The hourly purge of the history runs in a thread on a connection of its own; the event loop
  serves the radio meanwhile, and what is written in the meantime follows the purge
  ([ADR 0016](docs/decisions/0016-history-in-a-local-sqlite-database.md)).
- A warning when the event loop was held up for a second or more: the host gives the container
  no CPU, or its disk is slow (a backup, swapping).
- `state.json` is read once and held in memory as one store for every part of the gateway, and
  written whole at the status tick (every 30 s) and at the stop, so a table read costs one write
  instead of one per part; what a crash must not lose is written at once: the newest command time
  per sender, the kept documents and advert checks, the telemetry schedule after a flood and the
  radio profile's attempts ([design.md](docs/design.md#52-concurrency)).
- The slugs of the entity IDs of contacts that left the radio's table are kept for 90 days and
  at most 1 000, the newest; the radio profile's memory for the current radio and the two saved
  before it. Both grew with every contact and radio there ever was.
- A stop keeps the outgoing queue in `state.json` and the next start sends it: each message is
  `queued` again with its `id`, or gets the final event it would have had meanwhile. Before, a
  stop or restart, also an update or a reconfiguration by the installer, dropped the queued
  messages without a final event. A message whose acknowledgement is awaited gets it for up to a
  minute (the unit's `TimeoutStopSec` is 90 s instead of 20 s); one that needs another attempt
  continues after the start with the next one and the same timestamp
  ([ADR 0028](docs/decisions/0028-outgoing-queue-kept-across-restarts.md)).
- The airtime budget's level is kept across restarts: a start takes it back, refilled for the time
  stopped, but at least what a long direct message needs, so that the first alert after a restart
  goes out at once. Before, every start began with a full burst, so a crash loop could spend a
  burst per restart ([ADR 0008](docs/decisions/0008-outbound-safety-targets-airtime-budget-deduplication.md)).
- The unit no longer gives up after 20 failed starts in 10 minutes, after which only
  `systemctl reset-failed` started the service again: each automatic restart waits longer, from
  5 s up to 5 minutes, and a start by hand begins at 5 s again. The service may use 192 MB of
  memory (`MemoryMax=`), and its system call filter also refuses the obsolete calls. These need
  systemd 254 or newer (Debian 13 has 257); an older systemd ignores them with a warning
  ([deployment.md](docs/deployment.md#4-operations)).
- The warning at every connect to firmware the gateway is not tested with names what it switches
  off (the radio profile's writes and contact requests) and comes also with
  `radio.profile.manage = "observe"`.
- `make lock` writes the lock files for Linux alone and in the form Dependabot writes, so that its
  pull requests change versions and hashes only; `requirements/release.txt` (the release build)
  and `requirements/audit.txt` (`make audit`) are new lock files, and `make lock UPGRADE=` writes
  the files again with the versions they pin.
- A translation is read by a second speaker of the language before it is merged, and the texts
  that warn of a risk, say how a connection is secured or ask for a secret tell their translators
  so ([translations.md](docs/translations.md#review-of-a-translation)).
- Property tests (Hypothesis, a new development tool) feed made-up frames, packets, links,
  catalogs and configurations to the parsers that read them; a test that hangs prints where it
  stands after 5 minutes. The code names the records and design sections that hold its rules
  instead of the numbers of working documents that are not in the repository, and a test keeps
  each such reference resolving.
- For developers: the application reaches the radio and the bus through one module of ports
  (`meshcore_home_gateway.ports`), and a test keeps it from importing the companion session, the
  transports or the MQTT client. The bridge's outbox, received messages, announcements to Home
  Assistant and state document are modules of their own, and one module (`wiring.py`) puts the
  service together and lists who hears of what
  ([ADR 0007](docs/decisions/0007-hexagonal-architecture-with-an-asyncio-core.md)). Their log
  lines carry the new modules' names, such as `meshcore_home_gateway.outbox` for the deliveries.
  The command line is a package with a module per group of commands
  ([development.md](docs/development.md#layout)).

### Fixed

- A stall of the host longer than a radio command's timeout (5 s), such as a backup of the
  Proxmox host, ended the radio session although the radio's answer had come meanwhile, and the
  gateway reconnected; it now takes the answer.
- A state file moved aside as unreadable, or replaced while the service ran, lost the sections
  written only when they change (the contact devices and their slugs, the components, the kept
  documents) until they changed again, days later; a restart meanwhile announced the devices
  afresh. Every write now carries every section.
- A configuration that `config render` writes, as the installer does, shows a line or paragraph
  separator, a bidirectional control or a zero-width character in a value as an escape
  (`\u2028`), which TOML reads as before but an editor no longer hides.

### Security

- A release candidate or a release is published at the earliest a day after its version reached
  `main`, so that a merged change can still be stopped before it is signed and published for
  good; pre-releases go out at once ([development.md](docs/development.md#release)).
- The installer published with a release carries the SHA-256 of that release's zipapp and unit
  and installs only those files: whoever could replace them in the release could replace its
  `SHA256SUMS` too, but not the installer's attestation, so verifying the installer now verifies
  the program it installs. `--release` with another version checks against that release's
  `SHA256SUMS` alone. The README and the guide put the verified installation first
  ([deployment.md](docs/deployment.md#install)).
- The installer keeps the broker password in `/etc/meshcore-home-gateway/mqtt-password` (0640
  `root:meshcore-home-gateway`) instead of the configuration, and `--reconfigure` moves a
  password that is still in the configuration there: `config answers`, which the
  reconfiguration reads through `pct exec`, then prints only the path.
- The documentation says what `confirmed` proves: that a packet with the message's
  acknowledgement code arrived, which MeshCore sends in the clear and anyone who knows or guesses
  the text computes, so a stranger can confirm an alert nobody received and stop its retries.
  For alerts it recommends `confirm_nonce` and an answer from the recipient
  ([mqtt-api.md](docs/mqtt-api.md#what-confirmed-proves), [protocol.md](docs/protocol.md),
  [design §7.4](docs/design.md#74-residual-risks)).
- The documentation says what the TCP interface exposes, as verified in the firmware: the
  release build exports and imports the radio's private key to and from any client, MeshCore's
  direct messages have no forward secrecy, and a client can send any packet or move the radio
  off the mesh's frequency; with how to isolate such a radio and what to do after a takeover
  ([configuration.md](docs/configuration.md#radio),
  [design §7.2](docs/design.md#72-threats-and-mitigations), [protocol.md](docs/protocol.md)).
- By default the gateway keeps less of other people: it publishes only the messages of the
  channels the household chose as targets, not the neighbours' chatter on the *Public* channel;
  it keeps of other people's radios their keys and types only; and it asks only the companions
  the household includes for their telemetry, so adding a neighbour's node to exchange messages
  no longer starts tracking it when its companion allows everyone. The **Breaking:** entries
  under *Changed* name the settings that keep the old behaviour.
- A contact or a channel that floods messages no longer fills the bus, the history and Home
  Assistant's recorder: beyond a burst of 10 direct messages of one sender, one every 2 seconds
  is published, beyond 20 on one channel, one a second; the others are still judged as commands
  (an accepted command always goes out), and the log counts them at most once a minute
  ([mqtt-api.md](docs/mqtt-api.md#received-messages-received)). The radio's own duty cycle, which
  acknowledging such messages spends, stays out of the gateway's reach
  ([design §7.4](docs/design.md#74-residual-risks)).
- Malformed pushes from the radio are logged at most once a minute, with their count, instead of
  one warning each; 20 in a row end the session, which the reconnect resynchronises.
- The `delivery` event of a refused send request echoes at most 64 characters of the alias, which
  the sender chose with the topic.
- A flood of adverts with invented keys, which any board in range can send (a key costs
  microseconds), stays bounded
  ([design §7.2](docs/design.md#72-threats-and-mitigations)). The heard nodes keep those heard
  again (a newer advert, or a former contact) longest, so the flood no longer pushes out the
  nodes the household knows from the `nodes` document and the *Add heard node* select. While only
  heard nodes change, the `nodes` document goes out at most every 30 seconds instead of with
  every advert (up to 15 KiB each); it still goes out at once when a contact joins or leaves the
  table. On firmware 1.12 and newer a stranger's advert no longer makes the gateway read the
  radio's whole contact table, which kept the radio busy listing it and, over TCP, made it drop
  pushes. The history keeps when a node was last heard only for the contacts and the heard nodes
  the gateway lists, and for 7 days instead of 30, as
  [ADR 0016](docs/decisions/0016-history-in-a-local-sqlite-database.md) says, instead of a row
  for every key ever heard ([configuration.md](docs/configuration.md#history)).
- `set_clock` no longer writes a host clock that is not plausible to the radio, which cannot be
  set back and would keep a wrong time (a typo of the year, a dead clock battery of the host, a
  forged time server): not one before the gateway's release or more than ten years after it, and
  not one more than `set_clock_max_days` ahead of a radio clock that was set. A radio clock from
  before the release was never set and is written as before. A warning says why the clock was
  left behind ([design §6.1](docs/design.md#61-radio-session)).
- At start and in `check-config` the gateway warns when it logs in to the broker without a user
  name, or without TLS to a broker beyond its host or link.
- Birth messages on Home Assistant's status topic no longer postpone the gateway's announcement:
  one already due is kept, and announcements after births and discovery clears come at most
  every 30 seconds. More than 5 births in a minute are logged with the topic
  ([home-assistant.md](docs/home-assistant.md#how-discovery-behaves)).
- What another broker client writes on the gateway's retained topics, a forged position for a
  presence automation for example, or an empty message that makes a contact's entities unknown,
  is undone: the gateway publishes its own payload again, or clears a topic where it has
  published nothing in this run, at once and then at most every 10 seconds per topic, and the log
  names the topic. The log also says when the gateway cannot read its own topics: a broker
  refused a subscription, or its `online` did not come back within 10 seconds (Mosquitto's
  `acl_file` grants every subscription)
  ([mqtt-api.md](docs/mqtt-api.md#securing-the-broker),
  [ADR 0015](docs/decisions/0015-provider-neutral-documents-on-the-bus.md)).
- A release is built in an environment of the build tools alone, built again on a runner of
  another architecture (arm64) and compared byte for byte before it is attested and published
  ([development.md](docs/development.md#release)). CI looks every locked tool up in the Python
  Packaging Advisory Database (`make audit`), and the test environment of Home Assistant no longer
  holds versions with known vulnerabilities (Pillow, certifi, uv). SECURITY.md lists how the
  repository and the releases are protected.

## [1.1.0a4] - 2026-10-31

A pre-release of 1.1.0, with two slices: the texts of the command line, the Proxmox installer
and the names in Home Assistant come from translation catalogs, and `meshcore-home-gateway
dashboard` writes a first Home Assistant dashboard for the gateway, its texts in the same
catalogs. No language but English ships yet: a language comes as a contribution of its catalog
files ([translations.md](docs/translations.md#adding-a-language)), German first. Machine
interfaces stay the same in every language. Also new: tests against MeshCore's real firmware. If
you use the guide's *Last mesh message* card, paste it again (*Fixed*). The hardware checklist
was not run for it.

### Added

- Tests against the real firmware (`make firmware-test`, a CI job;
  [ADR 0026](docs/decisions/0026-gateway-tested-against-the-real-firmware-built-natively.md)):
  MeshCore's companion and repeater firmware of the pinned release (1.18.0), fetched by tag and
  checked against its commit, built for Linux unchanged and run on a simulated LoRa air with the
  EU/UK Narrow preset; the gateway's probe reads a companion through its serial transport.
  Scenarios of the hardware checklist's steps 29, 30, 33, 34, 35 and 39, and parts of steps 41
  and 43 (the check for positions in adverts), run the gateway's service against it, with the
  companion's phone app played on the gateway's own codec
  ([testing.md](docs/testing.md#steps-2945-on-the-simulated-air) lists what they show).
  `make check` still needs neither a compiler nor network.
- `[homeassistant] language` (default `en`, so far the only choice): the language of the entity,
  device and model names the gateway sends to Home Assistant, also of the default device name
  `MeshCore radio` and of the dashboard
  ([configuration.md](docs/configuration.md#homeassistant)). Entity IDs, event types, options,
  attributes and units stay the same in every language; a change renames only the display names
  at the next start, and names you changed stay
  ([home-assistant.md](docs/home-assistant.md#language-of-the-names)).
- The command line follows the language of its environment (`LANGUAGE`, `LC_ALL`, `LC_MESSAGES`,
  `LANG`; [configuration.md](docs/configuration.md#language-of-the-command-line)): help, errors,
  warnings, `check-config` and the `probe` report. Machine output (`--list`, `--json`, `listen`,
  `send --wait`, the results of `contacts add`, `remove` and `telemetry` with `--wait`,
  `contacts list`, `contacts link`, `config answers`, `--version`), `run` and the service's log
  stay English.
- The Proxmox installer writes `[homeassistant] language` for a new installation: `en`, or the
  value of the answers file; a reconfiguration keeps the configured one, and a version before
  1.1.0a4, which lacks the setting, gets it only from the answers file. Once the gateway offers
  languages besides English, the installer asks for its own language first (the default from
  your session), shows its dialogs, buttons and error boxes in it, passes it to the program,
  whose messages its log then quotes in that language, and makes it the default of
  `[homeassistant] language` for a new installation, which the advanced setup also asks. It reads
  the language's pack from where the script comes from (its release, the checkout next to it,
  else `main`) into memory, checks it against a digest the script carries, and continues in
  English when the pack cannot be loaded.
- `meshcore-home-gateway dashboard`: writes a Home Assistant dashboard for this gateway as YAML
  (for the raw configuration editor or a YAML-mode dashboard): the gateway's status and its
  radio's health, the last message and delivery with a form to write one, the contacts, the last
  contact event and the heard nodes, and a map with the contacts' traces. English; its texts are
  in the translation catalog. `--state-dir` names the service's state, from which it takes the
  contacts on the map.
- docs/home-assistant.md *Dashboard*: installing it, the script *Mesh message* for the form, and
  when to make it again.
- [docs/translations.md](docs/translations.md): the rules for translators and how to add a
  language. [ADR 0027](docs/decisions/0027-message-catalogs-compiled-when-loaded.md): the
  message catalogs are compiled when they are loaded, in the zipapp, the wheel and a checkout
  alike; it amends ADR 0019.

### Changed

- `check-config` writes "1 sender" or "2 senders" instead of "sender(s)", and names the language
  of the names in Home Assistant.

### Fixed

- The *Last mesh message* card in docs/home-assistant.md (*First steps*) now also replaces line
  breaks in the sender's name: a contact's name is chosen by its owner, and with a blank line in
  it the card's text after that line could load an image from the internet. Paste the card
  again if you use it.

## [1.1.0a3] - 2026-10-31

A pre-release of 1.1.0, the third of the three telemetry slices: the gateway checks whether the
household's companions send their position in their adverts, and the guide shows their traces on
a map. After the update, press *Advert* once on each companion within range of the gateway's
radio (the button's advert crosses no repeater), or send a flood advert from its app, so that the
gateway can check it. The hardware checklist was not run for it.

### Added

- The check for positions in adverts: `contacts/<key>` gains `advert_carries_position` and
  `advert_checked_at`. For a companion, `advert_carries_position` says whether its newest advert
  carries its position (also 0, 0 from a companion that never had one), which everyone in range
  can read, as the gateway checked it; while the gateway has not checked its newest advert, the
  last check or, if the radio's record holds a position from an earlier advert, `true`; `null`
  for repeaters, room servers and sensors. `advert_checked_at` is the checked advert's time
  (`null` before a check). The gateway reads the advert in the radio's packet log once the radio
  has accepted it, also the advert of a node it adds afterwards, and sends nothing. The last
  check of each companion is kept in `state.json`, without a position. The `contacts` index gains
  `advert_carries_position` (the first 16 companions not excluded from telemetry for which it is
  `true`) and `advert_carries_position_total`.
- The event `position_in_advert` on `contact` when a companion that is not excluded from
  telemetry starts to send its position in its adverts, is added after the gateway heard its
  newest advert carrying it (in the same run of the gateway), or is included in telemetry again
  while its adverts do. Never a position. The event entity *Contact* lists it.
- *Position in adverts* (`binary_sensor.mesh_<name>_advert_position`, problem) on every
  companion's device.
- docs/home-assistant.md: a map card that draws a companion's trace from Home Assistant's
  history, what a trace shows across expiry and restarts, and an automation that tells the
  household when a handheld shares its position.
- [ADR 0025](docs/decisions/0025-positions-in-adverts-checked-from-the-packet-log.md): positions
  in adverts are checked from the radio's packet log; until the newest advert is checked, the
  radio's record decides, erring to warn. It amends ADR 0018.

### Changed

- *Companion position in adverts* on the gateway's device follows the check: on while a
  companion not excluded from telemetry has `advert_carries_position` `true`, also when it sends
  0, 0; its attributes `contacts` and `total` follow. It was on while the radio's record of any
  companion held a position, also long after the companion stopped sharing, and off for one that
  sends 0, 0 because it never had a position. `advert_position` keeps its meaning (the radio's
  record holds a position from one of the contact's adverts); the documentation now says so.
- A position from adverts is shown only while the radio's record holds one and the contact's last
  checked advert, if any, carried one: once a checked advert carries none (or 0, 0), its tracker
  turns `unavailable` unless telemetry gives a position, also after an exclusion and a restart (a
  crash included). A checked advert with a position brings it back. Until the gateway has checked
  one (after the update), and for repeaters, room servers and sensors, which it does not check,
  the record decides: an old position of a companion stays until the *Advert* asked for above. A
  record without a position now drops an advert position of every contact, also one never
  checked, repeaters included. Before, the tracker kept the last position the companion ever
  shared. A position in an advert can be one the companion saved long ago: the advert does not
  say when the fix was taken.

### Fixed

- While the radio's receive delay is 0 (its default), hops, SNR and RSSI of an advert come only
  from the log directly before the radio's report of it, also when the gateway reads the report
  more than 10 s after the log; for a contact, only when that log is newer than the record the
  gateway read; for a node the radio does not hold, only when it has the advert's time. An advert
  without such a log has none (`null`), as one that was not logged: before, any log of its key
  within 10 s gave them, such as a later copy of an older advert or a forged one. With a receive
  delay above 0, or one the radio does not report, a log of its key within 10 s gives them, as
  before.

## [1.1.0a2] - 2026-10-30

A pre-release of 1.1.0, the second of the three telemetry slices: the household's handhelds
report their battery and sensors to Home Assistant, their telemetry expires when they stop
answering, a contact can be excluded, and telemetry is on by default. The hardware checklist was
not run for it.

### Added

- Readings of each companion in `contacts/<key>`: `battery_v`, `mcu_temperature_c`, `readings`
  (keys `ch<channel>_<type>[_<n>][_<unit>]`, such as `ch2_temperature_c`; luminosity has no
  unit, `ch1_luminosity`, because the T1000-E sends 0–100 % in it) and `readings_at`;
  `telemetry_state` (`waiting`, `answering`, `no_position`, `away`, `no_answer`, `no_path`,
  `look_alike`, `excluded`, `off`), `telemetry_current`, `position_current` and
  `telemetry_excluded`. In Home Assistant, on the contact's device: *Battery* (V),
  *MCU temperature*, a sensor per reading (`sensor.mesh_<name>_ch2_temperature`, …),
  *Telemetry state* and the switch *Telemetry*.
- Excluding a contact from telemetry: the switch, `contacts/<key>/telemetry` (`on`/`off`, or JSON
  with `id` and `"forget": true`) and `meshcore-home-gateway contacts telemetry KEY on|off
  [--forget]`. `off` is taken at once, also for a contact that is not on the radio yet: the
  gateway stops asking, forgets the contact's current battery, readings and telemetry position,
  and keeps the choice in `telemetry-excluded.json` in its state directory (while that file
  cannot be read, telemetry pauses and no contact gets back its readings or a telemetry position
  from the history); `forget` also deletes the contact's readings and telemetry positions from
  the history. Results on `contact`: `excluded` and `included` with `request: "telemetry"`, the
  reasons `not_saved` and `not_forgotten` (`not_forgotten` first, when both apply), the refusals
  `not_a_companion` and `too_many` (and `rate_limited` for too many telemetry requests); the
  *Contact* event entity has the two new types. The `contacts` index gains `telemetry_excluded`
  (the first 16 keys) and `telemetry_excluded_total`.
- Events on `telemetry`: `unanswered` when a companion that answered within the day leaves two
  requests in a row unanswered, `answered` when it answers again or for the first time; the event
  entity *Telemetry* on the gateway's device. The sensor *Telemetry status* with the attribute
  `reason` (the new `state.telemetry_reason`: `first_start`, `no_radio`, `exclusions_unreadable`,
  `auto_add`, `radio_airtime`); the two telemetry airtime sensors, disabled until enabled.
- The history keeps the readings of every answer (a new table; `[history] readings_days`,
  30 days) and restores the newest at start, unless telemetry is on and its saved schedule knows a
  later answer the history missed; `position_received_at` also comes back after a restart.
- `listen --topic telemetry`.
- The interface snapshot records the device and state classes of the Home Assistant entities.

### Changed

- **`[telemetry] enabled` is `true` by default:** the gateway asks every companion of its radio
  that is not excluded. The first time telemetry runs on an installation, it waits an hour after
  the radio connects (*Telemetry status* `paused`, reason `first_start`, and a warning in the log
  with the number of companions it will ask): exclude every companion that is not the
  household's in that hour. A companion's firmware refuses telemetry to everyone until its owner
  allows it; one set to *Allow All* that has the gateway as a contact answers. A configuration
  copied from the 1.1.0a1 example says `enabled = false`: remove that line to follow the new
  default. To update from 1.0.0 without any request, see *Update* in docs/deployment.md. With a
  broker ACL, give Home Assistant's user `topic write <base_topic>/<node_id>/contacts/+/telemetry`
  ([docs/mqtt-api.md](docs/mqtt-api.md#securing-the-broker)), or the switch *Telemetry* does
  nothing.
- `state.telemetry` is `paused`, with the reason `no_radio`, while the radio is away (it stayed
  `on`); the state document follows each change of `state.telemetry` at once. It reads `on` again
  as soon as the radio is back, before the companions answer: the example in
  docs/home-assistant.md of a person leaving home waits for 10 minutes of `on`.
- A contact's tracker turns `unavailable` while its telemetry position is not current: after two
  unanswered requests in a row, after an hour (three of its intervals, if longer) without an
  answer, or an hour after the last answer with a position
  ([ADR 0024](docs/decisions/0024-telemetry-values-in-home-assistant-expire.md)). A `person`
  then follows its other trackers or turns `unknown` (docs/home-assistant.md lists which
  presence triggers fire). Positions from adverts are unchanged. This holds after a restart
  without the radio too: `state.json` keeps the last document of each companion whose answers
  are current, with its position and readings, also with the history off.
- A companion the radio's table adds again, within a day of its removal, is asked within a
  minute like a new one, and its first answer raises `answered`; it no longer gets back the
  schedule it had.
- A downgrade to 1.1.0a1 asks excluded contacts while it runs (it leaves their file alone),
  removes the four new gateway entities (Home Assistant then removes and restores the gateway's
  device once) and leaves a removed contact's *Telemetry* switch behind (delete it under
  *Settings → Entities*). Its history keeps working; the readings wait there, unpurged, until a
  later version runs again.

### Fixed

- A contact target or command sender that the radio holds with a position but without an advert
  time (written by another client of the radio, such as a phone app) gets its device at once; it
  waited for its first advert.
- Home Assistant no longer logs template errors when a contact leaves the radio: *Last heard*,
  *Hops*, *SNR* and the tracker's attributes read the empty message that clears the contact's
  document as unknown.

## [1.1.0a1] - 2026-10-30

A pre-release of 1.1.0, the first of the three telemetry slices: the gateway asks the household's
companions for their telemetry and puts their positions on the map. The hardware checklist was not
run for it; 1.1.0 follows its telemetry steps.

### Added

- Telemetry from the radio's companions ([ADR 0018](docs/decisions/0018-telemetry-pulled-from-the-households-companions.md),
  `[telemetry] enabled`, **off by default** for now): the gateway asks each companion (chat
  contact) of the radio for its battery, readings and position with `SEND_TELEMETRY_REQ`, one at
  a time and after the messages, and takes an answer only for the contact asked. Direct only: a
  companion without a path is flooded only when it was heard within 30 minutes when the flood is
  planned, at most 4 floods an hour; the gateway never resets or writes a path for it. Within
  18 s an hour of the gateway's own transmit time and an estimated 90 s an hour of mesh airtime,
  both starting empty at every start; about every 15 minutes at rest and 90 seconds while moving
  for a neighbour, once an hour without a position, backing off for one that does not answer.
  Positions go to `contacts/<key>` and the contact's tracker, with the new `position_accuracy_m`
  (15) and `position_received_at`; `state.telemetry`, `telemetry_airtime_1h_s` and
  `telemetry_mesh_airtime_1h_s`. A guide for setting up the companions:
  [docs/companions.md](docs/companions.md). **No contact can be excluded yet** (it follows with
  the next release, which turns telemetry on by default): switched on, it asks every chat contact.

### Changed

- `contacts/<key>.heard_via` can be `telemetry`: a companion heard only by its telemetry answers
  moves `heard_at` (and *Last heard*) at most once an hour, and `hops`, `snr_db` and `rssi_dbm`
  stay those of its last advert or message. `position_at` is when the position last changed. A
  contact's tracker carries `gps_accuracy` 15 for a position from telemetry, so a companion up to
  15 m outside a zone counts as inside.
- After a path reset (a message's flood fallback) the gateway's copy of the contact table and
  `contacts/<key>.path_known` show it at once, so the next message to that contact is budgeted
  as the flood it will be.

### Fixed

- The history's position cap (100 000) no longer removes a contact's newest position, which only
  `positions_days` removes: a companion at rest kept a single position, and lost it to the
  positions of a moving one.

## [1.0.0] - 2026-10-30

The first stable release. It ships the program of 1.0.0rc1, run on hardware with a LilyGo T-Echo
Lite on companion firmware 1.18.0 over USB, in a Proxmox container, with Home Assistant 2026.10.
From now on the four public interfaces follow Semantic Versioning, kept by the compatibility
policy and its snapshot of the code: an incompatible change raises the major version, a feature
the minor version, a fix the patch version. Releases are tagged by CI from the version a pull
request writes.

### Added

- The compatibility policy ([docs/development.md](docs/development.md#compatibility),
  [ADR 0023](docs/decisions/0023-public-interfaces-kept-by-a-snapshot.md)): what each public
  interface keeps within a major version, what is compatible and what is breaking. Home Assistant
  discovery is the fourth public interface: unique IDs, default entity IDs, platforms, units,
  event types, options and attribute keys (names, icons and categories are not). Abbreviated
  options, texts for people and log messages are not interface; raising a platform floor is
  breaking.
- A guard for it: `tests/public_interface.json` records the structure of the four interfaces,
  computed from the code without a radio or a broker; a test fails when the two differ, and
  `make interface` writes it again and lists what changed. The CI job *release plan* (and
  `make release-plan`) compares it with the last release on every change and refuses a removal or
  narrowing without a `**Breaking:**` entry or a `### Removed` section since, and an addition or
  change when the changelog since has no entry or only fixes; a release must contain it. A false
  alarm is waived for one release in `tests/public_interface_waivers.json`.

### Changed

- Releases are tagged by CI ([ADR 0022](docs/decisions/0022-releases-tagged-by-ci-on-main.md)):
  the pull request that finishes an increment writes the next version and its changelog section,
  the CI job *release plan* (and `make release-plan`) refuses one that is not the next (the kind
  of change comes from the changelog: `**Breaking:**` or Removed is major, Added, Changed or
  Deprecated minor, Fixed or Security patch), and after the merge CI tags the commit once its
  checks passed and starts the release workflow on the tag. A release ships the program of its
  release candidate. A run of the release workflow on a tag releases, also when started by hand.

## [1.0.0rc1] - 2026-10-24

The release candidate for 1.0.0: the gateway that has carried messages end to end since its first
version (the MeshCore app on a companion, the mesh, the gateway's radio, MQTT, Home Assistant and
back), with slices 1 to 5 of the mesh observer since. The code is that of 0.1.0a1; only the
version and the rules below changed. 1.0.0 follows once the core steps of the
[hardware checklist](docs/testing.md#hardware-checklist) pass with this candidate. From 1.0.0 on,
the MQTT API, the configuration format and the command line follow Semantic Versioning: an
incompatible change raises the major version, a feature the minor version, a fix the patch
version.

### Changed

- Versioning ([docs/development.md](docs/development.md#release)): every finished increment of a
  feature is a pre-release of the minor version it leads to (`1.1.0a1`, `1.1.0a2`, …); a release
  candidate is what the hardware checklist runs with, and the release follows when it passes.

## [0.1.0a1] - 2026-10-24

The first pre-release: the bridge (messages both ways, commands from allowlisted contacts,
Home Assistant discovery, the Proxmox installer) and slices 1 to 5 of the mesh observer
([design §13](docs/design.md#13-roadmap-the-mesh-observer)): what the radio reports, documents
and a local history, a Home Assistant device for every contact, the managed radio profile and
contact management. Built, signed and attested like a release; the
[hardware checklist](docs/testing.md#hardware-checklist) was not run for it.

### Changed

- `nodes` is published again after every table read; a contact that leaves the table is listed
  as a heard node again while it counts as heard; an older advert no longer replaces a heard
  node's record, in the history either: it keeps the advert time (history schema 2, migrated at
  start). The `contact` event comes after the contact's documents.
- The managed profile does not add a protected contact while another contact has its first
  6 bytes (`protected_contacts.failed`); `check-config` and the service warn about two protected
  keys with one prefix.
- The installer suggests no MQTT user name any more: the dialog's field starts empty, and an
  answers file without `mqtt.username` logs in without one (so `mqtt.password` alone is refused;
  before, both took the user name `meshcore-home-gateway`).
- The Proxmox installer runs straight from its address, like the Proxmox community scripts:
  `bash -c "$(wget -qO- URL)"`. Nothing is stored on the host; the program is downloaded inside
  the new container: from a release, its zipapp and unit, checked against its `SHA256SUMS` (the
  release publishes the installer with its version written in); before the first release, the
  source of `main` (`--ref` for another branch, tag or commit), built there. Run from a clone, it
  installs the clone, as before. Files go into the container through command input, no longer
  through temporary files on the host; when the program is downloaded, the settings are checked in
  the new container. `--release` installs another release, for example to go back to one.
- Settings that would have no effect are configuration errors: `mqtt.password` without
  `mqtt.username`, `radio.device` with `transport = "tcp"`, `radio.host` or `radio.port` with
  `transport = "serial"`. `radio.device` and the TLS files must be absolute paths (the service
  runs in `/`). Node IDs and aliases may not end with `-` or `_` or have two of them in a row
  (Home Assistant would merge their entity IDs). An update refuses such a configuration and keeps
  the running version; fix the named setting, for example with `--reconfigure`.
- The Home Assistant discovery topic, device identifier and unique IDs keep a `-` of
  `mqtt.node_id`, so node IDs that differ only in `-` and `_` are two devices. Entity IDs are
  unchanged; a gateway whose node ID contains `-` appears as a new device.
- The `rejected` and `duplicate` delivery events of send requests refused at once are limited to
  20 per second (bursts of 100); a log warning counts the rest at most once a minute, so a flood
  of bad requests cannot push accepted events out of a subscriber's queue on the broker.
- `make matrix` fails when a Python of `PYTHONS` is missing; the pre-push hook refuses a push
  with uncommitted changes or of a commit that is not checked out; `make check` builds the
  zipapp once; a changed lock file gives a fresh virtual environment.

### Fixed

- The broker login: the installer's dialogs run in the `C.UTF-8` locale (in a locale the host
  lacks, whiptail dropped every non-ASCII character typed, passwords included); the user name
  field starts empty (whiptail added what was typed to the suggested name); the password dialog
  names its user, and `--reconfigure` no longer keeps the old password for a new user name.
- The installer logs in to the broker before it reads the radio and, when the broker refuses,
  shows its answer and offers to enter the broker settings again; the final message shows the
  result. `check-config --broker` does the same login, `check-config` names the user.
- The gateway names the user in a refused login, and logs attempts that no broker answers
  (`cannot reach HOST:PORT: reason`) and connections closed before an answer; they went
  unlogged. `mqtt.host` and `radio.host` must be a host name or IP address: `mqtt://`, a port or
  brackets are refused with a hint.
- The service user could not read a configuration written under a strict umask; the program's
  directory and the configuration's are created readable whatever the host's umask. The final
  message names the program by its path, which `pct exec` needs when the host's `PATH` lacks
  `/usr/local/bin`.
- Commands: an identical copy of a command's packet, also of a rejected one, stays a duplicate
  while it could pass the age check (an hour, or twice `max_age_s`); after `broker_unavailable`
  the sender's timestamp is stored, so a copy is a `replay` also after a restart.
- On stop the gateway releases the radio first and still publishes the messages the radio has
  handed over, also one whose answer is on its way (for up to 2 s); they were lost.
- A direct message is not sent to a look-alike contact that the radio has just added: the gateway
  reads the contact table again before sending, also after waiting for airtime.
- Channel texts accepted before the radio's first connection are limited for its real name when
  they are sent, and deduplicated on the text that is sent.
- Truncated texts no longer end in half a character: flags, joined emoji, skin tones, keycaps,
  letters with their marks and the Thai, Lao and halfwidth kana signs that follow a letter stay
  whole or go before the `...`.
- A serial port whose terminal setup fails is closed again and reported as a connection error;
  it kept the exclusive lock until a restart.
- `listen` and `send --wait` survive payloads nested too deeply for the JSON parser; `config
  render` reports an output it cannot write, loads the TLS files like `check-config`, checks
  target aliases before writing them, and syncs the file to disk.
- Installer: an interrupted run (Ctrl-C, a closed SSH session, kill, a closed output pipe) starts
  a service stopped by `--reconfigure` again and names a new container to remove; target answers
  are checked before the container is created and count also when the radio offers no channel or
  chat contact; `--reconfigure` keeps every alias of a target; without a user name no password
  is asked; `--update` no longer needs whiptail; the settings check leaves no `__pycache__` in
  the checkout; a failed lookup of the next container ID or the storages stops the run.
- Documentation: the airtime bound is stated per run of the service and for the counted airtime
  (a flood to a far recipient takes more); retries, the time to live, `flood_after`, the
  connection handshake, resynchronisation, rollback, retained requests, node IDs in topics and
  the unauthenticated TCP link are described as the code behaves.
- Over TCP the session no longer sends the 256 zero bytes of the serial resynchronisation: they
  misaligned the WiFi firmware's frame parser, so no TCP session could start. The TCP emulator
  in the tests now parses like the WiFi firmware.
- After a long broker outage the reconnect no longer loses `online`, discovery and state behind a
  full message queue: only events are bounded (1000, the excess is dropped and logged), and
  retained documents are not queued while the broker is unreachable.
- Commands that arrive while the broker is unreachable are refused (`broker_unavailable`) instead
  of reaching the automations at an arbitrary later time.
- An acknowledgement that arrives while the next attempt waits for airtime ends the delivery as
  `confirmed`, without another transmission or path reset.
- A command queued behind a session that has just ended is no longer written to the radio; the
  error names the cause.
- A clean shutdown no longer logs a reconnect warning; an unconfirmed `offline` is logged.
- Text cleaning removes only control characters and direction overrides; joiners, variation
  selectors and other format characters stay, so emoji sequences survive. Vertical tab, form feed
  and Unicode line separators become line breaks; spaces from transliteration are collapsed.
- With `max_age_s = 0` a command dated more than a day ahead is refused (`from_the_future`)
  instead of blocking its sender's later commands as replays.
- `listen` ends when its reader goes away (`| head`) and prints at most `--count` events;
  `send` refuses aliases that are not configured and impossible `--wait` values.
- Configuration, answers and state files that are not valid UTF-8 are configuration errors (the
  state file is moved aside); blank strings are refused as settings.
- A TCP connect timeout names the radio; the hourly airtime budget in the state document no longer
  shows float artefacts; names from the mesh are quoted in the log.
- Command events are published with QoS 0: after a broker loss that the gateway had not noticed
  yet (one to two `keepalive_s`), a command could still reach the automations after the
  reconnect, at any later time; now it is lost and the sender can send it again.
- Removing components in Home Assistant also works after Home Assistant restarted (a
  placeholder binds the old entity first) and after a run that reached neither broker nor Home
  Assistant (removals stay pending for 30 days). The placeholder is enabled, and the gateway
  announces again when Home Assistant clears its device's topic: removing a disabled entity, such
  as the packet sensors after `diagnostics.publish_rx_log` was switched off, made Home Assistant
  clear the topic itself, which took every entity of the gateway away until its next start.
- Events queued while the broker is down are no longer logged as refused.
- Texts: in ASCII mode invisible format characters (soft hyphen, zero-width space) are dropped
  instead of becoming `?`; a text of only such characters is rejected as empty; `send` refuses a
  text that is not UTF-8 instead of printing a traceback.
- Installer: a malformed answers line is reported by its number also when the broker password
  contains `=`; an answers file that is not UTF-8 is reported by line, without a traceback.
- Releases: `make release` admits only version forms that the release workflow classifies right
  (no `1.0.0rc1.post1`, no local versions); the documented attestation check names the release
  workflow and the tag.
- Installer: `--update` refuses a container without an installation instead of installing
  packages, a user and a service into it; `--reconfigure` keeps an anonymous broker anonymous and
  a port left at its default, and refuses answers it cannot apply (`radio.choice`, `ct.*`); a
  malformed answers line is reported by its number, without its content (it may hold the
  password); container IDs below 100 are refused as invalid, not as taken.
- `make pyz` builds the zipapp with the isolated interpreter `/usr/bin/python3 -I`, as installed;
  `make lock` takes the newest releases within the cooldown; the sdist contains `.github/` and
  `.githooks/`, which its tests and `make check` need.
- Home Assistant's birth message is followed on its own topic (`homeassistant.status_topic`,
  default `homeassistant/status`) instead of below the discovery prefix: with a custom prefix the
  entities stayed unavailable after a restart of Home Assistant.
- Documentation matches the code: retained send requests are rejected, not ignored; the final
  delivery states; the `STATUS=` line; exit code 2; what Home Assistant's birth message
  republishes; topic ACLs are required with commands; manual installation steps, roll back, a
  renamed serial device and the limits of `--reconfigure` in the deployment guide.

### Added

- Contact management on request ([ADR 0017](docs/decisions/0017-contacts-on-the-radio-each-with-its-own-device.md),
  `[contacts] manage`, on by default): `add_contact` takes a public key, a
  `meshcore://contact/add` link or a contact card, `contacts/<key>/remove` removes a contact. A
  node the gateway heard is added with the record the radio checked; a link or card only claims
  name and type until the node's first advert (`contacts/<key>.verified`), and a card's
  signature is not checked. Contact targets and command senders are never removed; a key with
  the first 6 bytes of a contact or a protected key is refused; the gateway sets no flag but the
  favourite; nothing is written on untested firmware unless `radio.profile.manage = "always"`.
  A request ends in one `contact` event (none only for refusals beyond the rate limit and requests
  queued at a stop): `added` or `removed` with its `request` and `id`, the
  new `refused` (nothing was written) or `failed`; every `contact` event now carries `prefix`,
  `request`, `id` and `reason`. A queue of 8 requests and a budget of radio work (10, then 1 a
  minute) bound the radio's load; refusals are limited like those of send requests.
- Contact management in Home Assistant (docs/home-assistant.md): on the gateway's device the
  select *Add heard node* (the 20 most recently heard companions, room servers and sensors that
  are no look-alikes, as `<first 12 key digits>…<last 8> <name>`, rooms and sensors marked) and
  the text *Add contact (key or link)*, in a discovery document of their own
  (`…/manage_contacts/config`); *Remove from radio* on every contact's device; the problem sensor
  *Look-alike node heard*; the attribute `contact_link` of the radio link, with a QR card for the
  MeshCore app. The controls are available while the radio is connected and management is on (a
  remove button not for a protected contact); `contacts.manage` and `contacts/<key>.removable` are
  now `false` on firmware the gateway is not tested with, unless `radio.profile.manage =
  "always"`. The select's options change at most every 30 seconds (at once after an add or a
  removal) and 70 times an hour (2 more for each start of the gateway), below Home Assistant's
  warning limit. New request topic `homeassistant/add_node` (the select's label; refused
  `unknown_node` when it names no heard node or contact), subscribed with Home Assistant's
  discovery; `contacts add` and `remove` take a label with the name after it, as the select
  lists it. Examples: a dashboard row that asks
  before it removes, an automation that reports refused and failed requests.
- `meshcore-home-gateway contacts list|add|remove|link`: through the broker, with the full key
  shown to compare; a heard node is named by its first 12 and last 8 key digits, never by a bare
  prefix. `listen --topic contact|node`.
- `state.contact_link` (the gateway's own contact link, for a QR code), `contacts.manage`,
  `contacts/<key>.removable` and `.verified`, `nodes[].look_alike`, `nodes.look_alike` and
  `look_alike_total`, `node.look_alike`.
- Documents for every front end, retained on the broker and published when they change
  ([ADR 0015](docs/decisions/0015-provider-neutral-documents-on-the-bus.md), docs/mqtt-api.md):
  `stats` (the radio's own measurements: noise floor, its real time on air in the last hour,
  packet counters, uptime, error flags, its duty-cycle setting), `contacts` (index of the radio's
  contact table) and `contacts/<key>` (one per contact: when and how it was last heard, its path,
  its last known position), `nodes` (nodes heard but not added, without positions) and `history`
  (the latest messages in and out). Events `contact` (added, removed) and `node` (a node heard for
  the first time).
- A managed radio profile ([ADR 0020](docs/decisions/0020-managed-radio-profile-for-the-observer.md),
  `[radio.profile]`): at every connect the gateway sets its radio up as an observer, writing only
  what differs: contacts added manually only, telemetry denied to everyone, no position in
  adverts, the 10 % duty cycle in 869.40–869.65 MHz, the name if configured, and the protected
  contacts (targets, command senders) on the radio as favourites. A change made on the radio is
  set back with a warning, except the name; untested firmware is only observed. A value that does
  not take is no longer written after three connects in a row, per firmware version. The result is
  the `settings` document with `setting` events, and in Home Assistant *Radio profile* and
  *Radio setting*. `probe` shows what the service will do at its next connect, the protected
  contacts included (`--config` for the profile of a configuration, `--state-dir` for the
  service's stored state, read only); the installer asks for the radio's name in the advanced
  setup and shows that plan. A protected contact put on the radio before it was ever heard gets
  its Home Assistant device with its first advert.
- Every contact of the radio is a recipient: `contacts/<key>/send` sends a direct message to a
  companion in the contact table, through the same queue, airtime budget and duplicate check as a
  target ([ADR 0017](docs/decisions/0017-contacts-on-the-radio-each-with-its-own-device.md));
  rejected as `unknown_contact` or `not_a_companion`. Messages of a contact also arrive on
  `contacts/<key>/received`.
- A Home Assistant device for every contact, linked to the gateway's device, which appears when
  the contact is added to the radio and goes when it is removed (docs/home-assistant.md,
  *Contacts*): a notify entity to message it, its messages as events, when it was last heard, hops
  and signal, and a tracker once its position is known. Entity IDs `mesh_<name>_…`, kept when the
  contact is renamed. A device deleted in Home Assistant hides the contact there (event `hidden`)
  until *Show hidden contacts* is pressed. At most `homeassistant.contact_devices` contacts (default
  50) get a device, the household first.
- Entities on the gateway's device for the new documents: *Contacts*, *Nodes heard*, the events
  *Contact* and *New node*, *Noise floor*, *Radio transmit time last hour*, problem sensors for a
  contact with remote access and for a companion whose adverts carry its position, and the radio's
  uptime and packet counters (disabled until enabled).
- A local history in `/var/lib/meshcore-home-gateway/history.sqlite3` (SQLite, from the standard
  library; [ADR 0016](docs/decisions/0016-history-in-a-local-sqlite-database.md)): messages,
  positions of contacts, when nodes were last heard. Settings `[history]` `enabled`,
  `messages_days`, `positions_days` (30 days by default).
- `probe` reports what the radio tells about privacy and airtime: its position and whether its
  adverts carry it, who may request its telemetry (base, location, environment), the extra
  acknowledgements, its own duty-cycle factor (`airtime_factor`), and the flags of every contact;
  a contact that may run the radio's command line over the air (flag 0x10) is marked.
- Contract tests against the real Home Assistant MQTT integration (`make ha-test`, a CI job): the
  discovery document, the state and event payloads and the automations of the Home Assistant
  guide, with a hash-pinned Home Assistant release that Dependabot proposes updates for.
- Home Assistant: the device shows the radio's model, firmware and key (announced again when they
  become known or change); sensors *Airtime available* and *Queue*; the radio's static facts as
  attributes of *Radio link*; packet RSSI and SNR sensors with `publish_rx_log` (disabled by
  default). Components of an earlier configuration (a removed target) are removed by the gateway.
  The guide names the required version (2026.8), maps every MQTT value to its place in Home
  Assistant and uses the `event.received` trigger, which also fires on an entity's first event.
- The project is named meshcore-home-gateway (developed as meshcore-bus until 2026-10-22; ADR
  0012): command, package, unit, user, paths, MQTT client identifier and Home Assistant IDs.
- Design document, architecture decision records, protocol reference (MeshCore companion
  firmware 1.18.0), MQTT API, Home Assistant, configuration, deployment, testing and development
  documentation.
- Companion protocol subset: framing with resynchronisation, command encoders, decoders for
  responses, pushes and messages in the legacy and the version 3 layout; device information of
  older firmware (80 and 81 bytes) and the auto-add answer of firmware 1.12 and 1.13 (without the
  hop limit) are accepted.
- Radio session over USB serial (termios, DTR asserted, exclusive) or TCP: handshake, clock
  synchronisation, contact-adding settings, contacts and channels, message sync, acknowledgement
  events, heartbeat. A malformed or unexpected answer ends the session, so the bridge reconnects
  and resynchronises.
- Bridge service: configured targets, text rules with optional ASCII transliteration, duplicate
  suppression, bounded queue with time to live, airtime budget from the LoRa time-on-air formula,
  acknowledgement tracking with retries and path reset, delivery events with reasons (`expired`
  only for messages that never left), inbound deduplication, retained state document.
- Command policy with allowlisted full public keys and replay protection. The command word is the
  first word of the text, ended by any whitespace (also a line break or a tab). The sender must be
  the only contact with its 6-byte prefix in the table read after the message arrived, and no
  other key with that prefix may have been a contact of the radio in the last 24 hours (table
  reads, advert, path and overwrite pushes), so a look-alike that came and went makes the sender
  ambiguous, while one that the radio did not add does not count. Rejections by the cached table
  need no read, one read serves a batch, and a read in progress is reused. Commands fetched while
  the bridge connects are refused. The bridge warns at connect about contact types the radio adds
  on its own and about commands over TCP.
- MQTT adapter on paho-mqtt with last will, Home Assistant device discovery with sensor, event and
  notify entities. TLS files that cannot be loaded are configuration errors (exit code 78, no
  restart loop), and TLS file settings without `tls = true` or a key without a certificate are
  refused rather than ignored.
- Command line: `run`, `check-config` (also loads the TLS files), `probe` (also shows how the
  radio adds contacts, leaves waiting messages in the radio, and takes IPv6 addresses as
  `[ADDRESS]:PORT`), `send` (with `--wait` following its own request), `listen`, `config render`,
  `config answers`. Texts from the mesh reach the terminal without control or format characters;
  malformed arguments and unknown groups are reported without a traceback.
- systemd unit with notify, watchdog and sandboxing; reproducible zipapp build.
- Proxmox VE installer: default and advanced setup in dialogs or from an answers file (unknown
  installer answers refused, every configuration setting passed on, the radio never guessed),
  settings checked with the program's own code before the radio is read, unprivileged Debian 13
  container with nesting, radio passed through by its stable path, update that switches only to a
  version accepting the configuration (also with an unchanged version string), reconfigure that
  keeps the current settings and the targets the radio no longer offers.
- Release workflow (ADR 0013): a tag `vX.Y.Z` builds the zipapp, wheel and sdist reproducibly
  with the same `make` targets as a local check, attests them with Sigstore-signed build
  provenance and publishes them with `SHA256SUMS` on a GitHub release, the version's changelog
  section as its notes (links to the files of the tag); `make release` is the local dry run.
  Every finished increment is a pre-release `X.Y.ZaN` (docs/development.md).
- Continuous integration that runs the same `make` targets as a local check (`make check`,
  `make matrix`), with a pre-push hook, hash-pinned tools with a 14-day cooldown and SHA-pinned
  actions.
