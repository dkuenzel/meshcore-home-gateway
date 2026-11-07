# Configuration

One TOML file, by default `/etc/meshcore-home-gateway/config.toml`, owner `root`, group
`meshcore-home-gateway`, mode 0640 (it contains the broker password, unless `password_file` names
a file of its own). Unknown keys are errors, so a typo never falls back to a default silently. Check a file with `meshcore-home-gateway check-config
--config <file>`; the service exits with code 78 on configuration errors and systemd does not
restart it then.

Public interface: see [compatibility](development.md#compatibility).

A complete example: [`deploy/config.example.toml`](../deploy/config.example.toml).

## `[radio]`

| Key | Default | Meaning |
| --- | --- | --- |
| `transport` | required | `serial` (USB) or `tcp` (WiFi companion firmware) |
| `device` | – | serial device, an absolute path; use the stable `/dev/serial/by-id/...` path. Required for `serial`, an error with `tcp` |
| `host` | – | radio address (host name or IP address, without port). Required for `tcp`, an error with `serial` |
| `port` | `5000` | TCP port; only with `tcp` |
| `tcp_unauthenticated` | `false` | required as `true` with `tcp`, an error with `serial`: says that you know what the TCP interface exposes (below) |
| `set_clock` | `true` | keep the radio clock on the host's: move it forward when it is behind, at connect and every minute, when the host's clock is plausible (`set_clock_max_days`); restart the radio when it is 1 minute to 1 hour ahead, which sets it back to its newest contact time (at most once a day, when no message is being delivered, and only when the next connect may move it forward from there by `set_clock_max_days`) |
| `set_clock_max_days` | `30` | the largest step, in days, by which `set_clock` moves a radio clock that was set, 0–3650; a host clock further ahead, before the gateway's release or more than ten years after it is not written (a warning says why). A radio without a clock chip restarts at the newest time it wrote to a contact, so a radio that lay unused needs a larger step once. `0`: no limit (the behaviour of earlier versions) |

The serial port always runs at 115200 baud with DTR asserted; 1200 baud is refused because it
would put nRF52 radios into their bootloader.

The TCP interface of the WiFi companion firmware has no authentication: whoever reaches the
radio's port controls the radio. The release firmware also hands out the radio's private key to
any client and takes a new one (`ENABLE_PRIVATE_KEY_EXPORT` and `_IMPORT` in its build flags), and
MeshCore's direct messages have no forward secrecy: with the key, every direct message of the
radio can be decrypted, also those recorded from the air before; the household's commands, the
alerts and the companions' positions are direct messages. Prefer USB. For TCP, put the radio in a
network that only the gateway's host reaches (a VLAN of its own, or a second SSID with client
isolation), never the household's WiFi, and if you build the firmware yourself, leave the two
flags out, as their own comment in `platformio.ini` suggests. The gateway tells when another
client took the link (`radio_link_contested` in [`state`](mqtt-api.md#state-state);
[deployment.md](deployment.md#5-troubleshooting) says what to do then).

## `[radio.profile]`

The gateway keeps its radio set as an *observer*
([ADR 0020](decisions/0020-managed-radio-profile-for-the-observer.md)): it adds contacts only on
request (manual adding, no contact type added automatically), denies telemetry to everyone, puts
no position into its adverts, and in the EU band 869.40–869.65 MHz limits itself to the 10 % duty
cycle (airtime factor 9). At every connect, before it reads the contact table, it compares the
radio with this profile and writes only what differs, one command per setting; the radio rewrites
its preferences file for each. Nothing else is ever written: frequency, bandwidth, spreading
factor, coding rate, transmit power and the radio's keys stay as they are.

| Key | Default | Meaning |
| --- | --- | --- |
| `manage` | `auto` | `auto` writes on tested firmware only (companion 1.18) and only reports on other firmware; `observe` never writes; `always` writes on any firmware |
| `name` | as found | the radio's name in the mesh, 1–31 bytes of UTF-8. Written when this setting changes; a name changed on the radio later (the phone app) is kept and reported |
| `duty_cycle_percent` | 10 in 869.40–869.65 MHz, else as found | the radio's own transmit limit, 10–100 (the firmware cannot limit below 10 %; the gateway's own budget is `[airtime]`) |
| `protect_contacts` | `true` | keep the contact targets and command senders on the radio as favourites; a missing one is added, named after its alias (a command sender without one `sender-` and its first four key digits) until its next advert names it |

At the first connect of a radio its differences are written: a new or replaced radio, the first
run with the profile, or a radio after a factory reset, which gives it a new key, so the gateway
takes it for a new radio. Later, a setting changed on the radio (the phone app) is set back with
a warning in the log, except the name, which is kept and reported; a changed configuration is
written. A value that does not read back as written is reported (`not_taken`); after three
connects in a row that sent it, it is no longer written to that firmware version
(`not_converging`) until the configuration asks for another value, the radio has it, or the
firmware changes; a setting the firmware does not know is skipped on that firmware
(`unsupported`), and so is one whose command is compiled out of the firmware the radio runs
(`disabled`). What the radio has, what the profile wants and what
happened is in the `settings` document ([mqtt-api.md](mqtt-api.md#radio-profile-settings-and-setting))
and in Home Assistant (*Radio profile*).

`meshcore-home-gateway probe --config /etc/meshcore-home-gateway/config.toml` shows what the
service will do at its next connect: it reads the service's state (`--state-dir`, by default
`$STATE_DIRECTORY` or `/var/lib/meshcore-home-gateway`) without changing it and runs the service's
own comparison; each difference is set, kept (the name, changed on the radio), given up,
unsupported or only reported, and each contact target or command sender that is missing or no
favourite is added or made a favourite (or only reported). Stop the service first: the radio
takes one client. Without `--config` it compares with the plain observer profile, without
contacts. When it cannot read the state file (run it as root), it says so and assumes a first
connect.

## `[mqtt]`

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | required | broker address: a host name or IP address, without `mqtt://` and port |
| `port` | `1883` | broker port (8883 is usual with TLS) |
| `username`, `password` | – | broker credentials; `password` needs `username` |
| `password_file` | – | instead of `password`: the absolute path of a file that holds it (one line break at its end is not part of it), so that the configuration, `config answers`, a backup or a screenshot of it carry no secret. The service and the command line read it at start (mode 0640 `root:meshcore-home-gateway`, as the configuration); one that cannot be read, or is empty, is a configuration error. The installer writes `/etc/meshcore-home-gateway/mqtt-password` ([deployment.md](deployment.md#4-operations)) |
| `tls` | `false` | TLS with certificate and host name verification |
| `ca_file` | system store | CA certificate file for TLS, an absolute path; only with `tls = true` |
| `cert_file`, `key_file` | – | client certificate and key for TLS authentication, absolute paths; only with `tls = true`. `key_file` needs `cert_file` and can be left out when the certificate file holds the key |
| `client_id` | `meshcore-home-gateway-<node_id>-<8 random hex digits>` | MQTT client identifier. The default changes at every start: a broker drops a client when another connects with its id, and a fixed one is easy to read off the topics. A broker ACL that names clients can use the prefix `meshcore-home-gateway-` |
| `keepalive_s` | `60` | keep-alive interval, 10–3600 |
| `base_topic` | `meshcore` | first topic levels; segments of `A-Z a-z 0-9 - _` separated by `/` |
| `node_id` | required | this gateway's name in topics and in Home Assistant; 1–32 of `a-z 0-9 - _`, starting with a letter or digit, not ending with `-` or `_`, no two of them in a row (Home Assistant would merge those). Only the entity IDs have `_` for `-` ([home-assistant.md](home-assistant.md#entities)) |
| `insecure` | `false` | accept a broker connection without a user name, or without TLS to a broker that is not on this host or the link (loopback, link-local address). Commands (`[commands] senders`) need it then: any client that reaches the broker could publish command events. It also silences the warnings about it at start and in `check-config` ([mqtt-api.md](mqtt-api.md#securing-the-broker)) |
| `protocol` | `"3.1.1"` | the MQTT version of the gateway's connection: `"3.1.1"` or `"5"`. With 5 its messages say who published them and when, its events expire, and the delivery events of a send request carry its correlation data ([mqtt-api.md](mqtt-api.md#mqtt-5)); a broker that does not speak 5 refuses the connection. Clients of either version share a broker |

A TLS file that cannot be loaded (missing, unreadable, not a certificate or key) is a
configuration error: `check-config` and `config render` load the files, and the service exits with
code 78. The paths must be absolute, like `radio.device`: the service runs in `/`, not in the
directory where `check-config` is started.

## `[homeassistant]`

| Key | Default | Meaning |
| --- | --- | --- |
| `discovery` | `true` | publish the discovery document and follow Home Assistant's birth message |
| `prefix` | `homeassistant` | discovery prefix (as set in Home Assistant's MQTT options); one topic level of `A-Z a-z 0-9 - _` |
| `device_name` | `MeshCore radio` | name of the device in Home Assistant (the default is translated, see `language`) |
| `status_topic` | `homeassistant/status` | topic of Home Assistant's birth message (MQTT options, *birth message*); it does not follow `prefix`. Segments of `A-Z a-z 0-9 - _` separated by `/` |
| `contact_devices` | `50` | contacts that get a device of their own, 0–1000; `0` removes them ([home-assistant.md](home-assistant.md#contacts)) |
| `language` | `en` | language of the names the gateway sends to Home Assistant (entities, devices, models), also of the dashboard (`meshcore-home-gateway dashboard`): `en`, the only one so far ([more by contribution](translations.md#adding-a-language)); any other value is an error that lists the choices. A change renames the display names at the next start; entity IDs, automations and names you changed stay. The default `device_name` (`MeshCore radio`) is shown in this language; a name you set is kept (write another name to keep it English) |
| `controls` | `true` | with `false`, Home Assistant shows the controls that change the radio or the telemetry unavailable, so no user of it can operate them: *Add heard node*, *Add contact (key or link)*, *Show hidden contacts*, and each contact's *Remove from radio* and *Telemetry*. The requests on MQTT and the command line keep working ([shared households](home-assistant.md#shared-households)) |
| `hidden_targets` | `[]` | aliases of targets without a notify entity in Home Assistant, such as an alarm channel only automations should use; `send/<alias>` keeps working. Each must be a target |
| `contact_entity_prefix` | `mesh` | how the entity IDs of the contacts' devices begin (`device_tracker.mesh_alice`); a second gateway takes another, such as `mesh_2`. Lower-case letters and digits, words joined by single underscores, at most 20 characters. A change applies to new entities; Home Assistant keeps the IDs of those it has ([second gateway](home-assistant.md#a-second-gateway)) |

## `[targets.<alias>]`

Named destinations; publish to `send/<alias>`. Channels can be reached only through a target;
companions in the radio's contact table also through `contacts/<key>/send`
([mqtt-api.md](mqtt-api.md#sending-sendalias)). The alias is 1–32 of
`a-z 0-9 - _`, starting with a letter or digit. Set exactly one of:

| Key | Meaning |
| --- | --- |
| `channel` | channel index on the radio (0–255). The channel must be configured on the radio; `meshcore-home-gateway probe` lists them |
| `contact` | the contact's full public key, 64 hex digits. The radio addresses contacts by the first 6 bytes; the gateway refuses to send if those match another contact too |

Optionally also:

| Key | Default | Meaning |
| --- | --- | --- |
| `prefix` | none | text put in front of every message to this target, spaces included, such as `"Shed: "`: printable characters, at most 32 bytes |
| `suffix` | none | text put after every message to this target, such as `" #home"`; the same rules |

The prefix and suffix count toward the radio's limit and stay whole when a text is cut
(`truncate`); `ascii_only` transliterates them with the text. A request whose text is empty stays
rejected (`empty`). The delivery events and the history carry the text as sent, and duplicates
are judged on it. A message to `contacts/<key>/send` gets neither: that recipient is no target.

## `[messages]`

| Key | Default | Meaning |
| --- | --- | --- |
| `ascii_only` | `false` | transliterate to ASCII (ä → ae, typographic quotes, accents); for handhelds without Unicode fonts |
| `truncate` | `true` | cut texts above the radio limit between whole characters (flags and emoji sequences included) and mark them with `...`; `false` rejects them |
| `dedup_window_s` | `300` | the same text to the same target is sent once per window, 0–86400; `0` disables. A message that ended `failed` or `expired` does not count |
| `queue_size` | `20` | waiting messages, 1–1000; more are rejected. A stop keeps the waiting messages in `state.json` for the next start, which queues them again with their `id` ([ADR 0028](decisions/0028-outgoing-queue-kept-across-restarts.md)) |
| `queue_ttl_s` | `600` | time to live of a message, 1–86400: no attempt starts after it. Never sent within it, it expires; sent but not acknowledged, it fails when the acknowledgement wait of its last attempt ends (up to 60 s after the time to live) |
| `max_attempts` | `3` | sends of a direct message until acknowledged, 1–4 (the radio's attempt counter) |
| `flood_after` | `2` | unacknowledged attempts after which the stored path is reset, so the next attempt floods; 1 to `max_attempts` (equal to it: never floods) |
| `ignore_channels` | `[]` | channel indices (0–255) whose messages are not published (busy public channels); they do not hold telemetry back |
| `publish_channels` | `targets` | `targets`: only the messages of channels that are a [target](#targetsalias) are published and kept in the history, the others are dropped like those of `ignore_channels` (the *Public* channel's key is in the firmware: there, most messages are strangers'); `all`: every channel's |
| `clean_received` | `false` | drop control and direction characters (escape sequences, the right-to-left override) from received texts before they are published and kept in the history, as from outgoing ones; off, texts are data and stay as they came ([mqtt-api.md](mqtt-api.md#received-messages-received)) |
| `confirm_nonce` | `false` | end every direct message with a random suffix, ` #` and 8 hex digits, the same on each attempt, which the recipient sees: the acknowledgement is a hash of the text, so then only a radio that received the message can send it, not one that guesses a fixed text such as an alarm's ([mqtt-api.md](mqtt-api.md#what-confirmed-proves)). A direct text is then limited to 150 bytes; the events and the history carry it as sent, duplicates are judged without the suffix |
| `alert_targets` | `[]` | [targets](#targetsalias) whose messages leave before the others: an alert overtakes the waiting messages and goes between two attempts of one in flight, which continues after it. A request chooses by its target, so a second target for the same recipient gives a client both ([design §6.2](design.md#62-outbound-path)) |

## `[airtime]`

| Key | Default | Meaning |
| --- | --- | --- |
| `budget_s_per_hour` | `36` | seconds of transmission per hour for the gateway's messages (1 % of the hour), 0.001–3600; telemetry has its own lanes (`[telemetry]`) |
| `burst_s` | `10` | seconds that may be spent at once, 5–3600 (a long packet at SF 8 / 62.5 kHz takes about 2 s) |

The airtime the gateway counts over any interval *T* stays below
`burst_s + budget_s_per_hour × T / 3600`. The count is exact for direct sends and for flooded
sends to recipients up to 4 path bytes away; a flooded attempt to a more distant recipient takes
up to about 1 s more at EU/UK Narrow ([protocol.md §6](protocol.md#6-on-air-sizes)). The budget's
level is kept in `state.json` across restarts: a start takes it back, refilled for the time
stopped, but at least what a long direct message needs at the radio's settings, so that the first
alert after a restart goes out at once; a restart adds at most that much, not a burst.
The radio's own traffic (acknowledgements it sends, adverts) is not counted; in the EU band
869.40–869.65 MHz the device limit is 10 % of every hour. The radio has its own limiter, the
*airtime factor*, which the firmware sets to 50 % and the managed profile to 10 % in that band
(`[radio.profile]`, protocol.md section 7).

## `[commands]`

| Key | Default | Meaning |
| --- | --- | --- |
| `senders` | `[]` | full public keys (64 hex digits) allowed to send commands |
| `words` | `[]` | command words, case-insensitive single words; the first word of a direct message |
| `max_age_s` | `900` | accept only commands whose sender timestamp is within this many seconds of the gateway clock, 0–86400; `0` disables the check, except that timestamps more than a day ahead are always refused (one of them would make every later command of that sender a replay); it also bounds when a replayed copy of a rejected command can still pass ([design §7.4](design.md#74-residual-risks)) |
| `codes_file` | none | absolute path of the senders' code secrets, which `meshcore-home-gateway commands enrol` writes (below); a file that cannot be read, also a missing one, is a configuration error |
| `reply` | `none` | answer the sender by direct message: `accepted`: `ok: <word>` when the command was published; `all`: also `rejected: <reason>` when the checks identified the sender but refused the command (`replay`, `too_old`, `from_the_future`, `code_missing`, `code_invalid`, `code_reused`, `state_unwritable`, `broker_unavailable`); `none`: no answer (below) |

Commands also need a sender timestamp newer than that sender's last accepted command; the gateway
keeps these timestamps in `/var/lib/meshcore-home-gateway/state.json`.

**Answers.** With `reply`, the sender learns on its phone whether the command reached the
automations, without an automation of its own. The answer goes to the sender's full key as any
message does: through the queue and the airtime budget, with `delivery` events (the `target` is
the key) and a history entry; the same command sent again later is answered again. An answer
says only that the command was published, not what the automation did; an automation that has
more to say sends it to `contacts/<key>/send`. A sender that the checks did not identify
(`sender_unknown`, `sender_ambiguous`, `sender_not_allowed`, `sender_unverified`) gets no answer:
it would tell a stranger which words exist and that its key is not allowed. Each answer costs
airtime; a replayed copy of an old command is answered with `rejected: replay` at most as often
as the rate limit of received messages lets it through ([mqtt-api.md](mqtt-api.md#received-messages-received)).

**Codes.** A sender with a code secret ends each command with the 6-digit code of an
authenticator app (TOTP, RFC 6238: SHA-1, 30 s), as in `arm now 492039`; the code is not part of
`args`. The gateway accepts the code of the current 30 seconds of its clock and of those before
and after, each code once (`code_missing`, `code_invalid`, `code_reused` otherwise,
[mqtt-api.md](mqtt-api.md#commands-command)). A code authenticates the text whatever reached the
gateway: a host that poses as a radio over TCP, or a node that shares an allowed sender's key
prefix, cannot command without the secret. Senders without a secret keep the rules above. To
give one a secret (or a new one, which ends the old):

```sh
# once: codes_file = "/etc/meshcore-home-gateway/command-codes" under [commands]
meshcore-home-gateway commands enrol <full key>   # prints an otpauth:// line
systemctl restart meshcore-home-gateway
```

The sender adds the line to an authenticator app: as text, as a QR code
(`qrencode -t ansiutf8 '<line>'` shows one in the terminal), or by typing in the secret that
`enrol` also prints. The file holds `<key> = "<base32 secret>"` lines; `enrol` writes it with
mode 0640 and the configuration's group, and keeps the mode and group of a file that exists. A
systemd credential of its name is read instead, as for the broker password
([deployment.md](deployment.md#4-operations)). The phone's clock and the host's must agree within
30 seconds; phones set theirs from the network. The code travels in the message's text: the
`received` event and the history show it, which is harmless once the code is used.

Commands that the gateway fetches while it connects are rejected: those that waited in the radio
while the gateway was not running (restart, update, unplugged radio), and those that arrive during
the few seconds of the connection setup. The sender sends them again
([ADR 0009](decisions/0009-inbound-commands-from-allowlisted-contacts.md)).

The managed profile (`[radio.profile]`) makes the radio add contacts manually, with every contact
type switched off in the auto-add settings. In manual mode the radio still adds the types enabled
there, and a contact of any type can send direct messages. A node that the radio adds with the
6-byte key prefix of an allowed sender blocks that sender's commands until 24 hours after it was
last seen; a node that only advertises is not added and does not count. When the profile only
observes, the gateway logs a warning at connect if the radio adds contacts on its own, and
`meshcore-home-gateway probe` shows the settings. Connect a radio that takes commands by USB: over
TCP it drops push frames while its send queue is full (the gateway warns about that too), and a
host that takes the radio's network address can pose as the radio and forge commands
([design §7.2](design.md#72-threats-and-mitigations)).

## `[history]`

The gateway keeps a history in `/var/lib/meshcore-home-gateway/history.sqlite3`: messages in and
out, positions of contacts (from their adverts and their telemetry, one row per change), the
readings of the companions' telemetry answers (one row per value: the contact, the gateway's time
of the answer, the reading's key such as `battery_v` or `ch2_temperature_c`, and the value), and
when each node was last heard
([ADR 0016](decisions/0016-history-in-a-local-sqlite-database.md)). It fills the `history`
document after a restart and gives each companion its newest readings back. Home Assistant draws
traces from its own recorder ([home-assistant.md](home-assistant.md#traces-on-the-map)); the
history keeps positions for later front ends. Positions and readings are kept only for contacts,
never for nodes the gateway merely heard. When each node was last heard is kept for the contacts
and for the heard nodes the gateway lists (the last 50 of the last 7 days,
[mqtt-api.md](mqtt-api.md#nodes-heard-nodes)), so they come back after a restart; the hourly
purge removes the rows of all other nodes, and every row after 7 days. This is not a setting: the
rows serve only the restart.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | keep the history; with `false` nothing is written (delete the file by hand to remove what is there) |
| `messages_days` | `30` | days messages are kept, 1–3650; at most 10 000 messages |
| `positions_days` | `30` | days positions are kept, 1–3650; beyond 100 000 positions the oldest go first, except each contact's newest, which only its age removes |
| `readings_days` | `30` | days readings are kept, 1–3650; beyond 200 000 readings the oldest go first, except each contact's newest answer, which only its age removes |

The file holds message texts, positions and readings of the household's people: it is readable
only by the service user. To delete one companion's readings and its positions from telemetry,
exclude it with `--forget` ([`[telemetry]`](#telemetry)); its advert positions and when it was last
heard stay. If the file cannot be opened or written, the gateway logs it and runs without a
history; a file that is not a database is moved aside to `history.sqlite3.corrupt`. The readings
are a table that versions before 1.1.0a2 do not know: after a downgrade their history keeps
working, and the readings wait there, neither written nor purged, until a later version runs
again.

## `[contacts]`

| Key | Default | Meaning |
| --- | --- | --- |
| `manage` | `true` | add and remove contacts on request: from Home Assistant ([home-assistant.md](home-assistant.md#adding-and-removing-contacts)), the command line (`meshcore-home-gateway contacts`) and MQTT ([mqtt-api.md](mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)). A contact grants no rights; contact targets and command senders are never removed. With `false` the controls in Home Assistant are `unavailable` |
| `heard_nodes` | `"keys"` | what the gateway publishes and keeps of the nodes it heard but did not add, other people's radios ([privacy.md](privacy.md)): `"keys"` key and type only, also in the history and in the labels of *Add heard node*; `"full"` key, type, name, when and how they were heard; `"off"` nothing: no `nodes` entries, no `node` events, no history rows, nothing to add from the select, only the keys of look-alikes of contacts and protected keys ([mqtt-api.md](mqtt-api.md#nodes-heard-nodes)). Rows of an earlier setting lose name and signal at the start with `"keys"`, and go with the next hourly purge with `"off"` |

Who may send the requests is the broker's ACL to decide ([mqtt-api.md](mqtt-api.md#securing-the-broker)).
Contacts are written only on tested firmware, like the managed profile; `radio.profile.manage =
"always"` allows any. `check-config` and the service warn when two contact targets or command
senders share their first 6 bytes: the radio names a sender by them only (ADR 0009). The managed
profile, when it writes, adds the lower key unless the other is on the radio, and never both;
with `protect_contacts = false` or `manage = "observe"` the gateway adds neither, and refuses
requests to add either. If both are on the radio, remove one.

## `[telemetry]`

The gateway asks the radio's companions for their telemetry: battery, readings and, with a GPS,
their position ([ADR 0018](decisions/0018-telemetry-pulled-from-the-households-companions.md)).
A companion answers only contacts it allows: set the household's companions up
([companions.md](companions.md)), and exclude every other one.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | ask the companions (chat contacts) of the radio's table that `ask` names, one at a time, after the messages; battery, readings and positions go to `contacts/<key>` and the contact's device in Home Assistant |
| `ask` | `"household"` | whom: `"household"` only the companions the household included (the switch *Telemetry*, `contacts telemetry KEY on`, `on` on `contacts/<key>/telemetry`) and the contact targets and command senders, unless excluded; `"all"` every companion not excluded |
| `ask_types` | `["chat"]` | which contacts are asked, by type: `"chat"` the companions, `"sensor"` also the sensors (MeshCore's sensor firmware), as companions are: excluded, included, `ask`, switch and state alike. A sensor answers only a radio in its access list ([companions.md](companions.md#sensors)). Repeaters and room servers are never asked |

**Household.** With `ask = "household"` a contact the radio adds is not asked until somebody
includes it: its switch *Telemetry* is off and its state `excluded`, there is no first-start
hour, and adding a neighbour's node to exchange messages does not track it. The inclusions are
kept beside the exclusions in `telemetry-excluded.json` (`"included"`); `off` turns an inclusion
into an exclusion. With `"all"` the inclusions are kept but do not count.

**Exclusion.** A companion's firmware refuses telemetry to everyone until its owner allows it; one
set to *Allow All* that has the gateway as a contact answers, and with `ask = "all"` would be
tracked like a household member. Exclude every companion that is not the household's: with the
switch *Telemetry* on its device in Home Assistant, `meshcore-home-gateway contacts telemetry KEY
off`, or `off` on `contacts/<key>/telemetry`
([mqtt-api.md](mqtt-api.md#excluding-a-contact-from-telemetry-contactskeytelemetry)). A full key
works also before the contact is on the radio. `--forget` (`"forget": true`) also deletes the
contact's readings and positions from telemetry from the history. The gateway keeps the
exclusions in `telemetry-excluded.json` in its state directory: also when the contact leaves the
radio, while telemetry is off, and across restarts. With `enabled = false` it asks nobody, and
the switches and the exclusions still work, so a household can exclude before it switches
telemetry on.

**The first-start hour.** With `ask = "all"`, the first time telemetry runs on a state directory
(an update from 1.0.0, from 1.1.0a1 with telemetry off, or a new installation), the gateway asks
nobody for an hour after the radio first connects: `state.telemetry` is `paused` with the reason
`first_start`, and one warning in the log names the number of companions it will ask. Exclude the
strangers in that hour; then the log says "telemetry resumed". What is left of the hour is saved,
so a restart neither skips nor extends it. An installation that already ran telemetry has no hold.

**Expiry** ([ADR 0024](decisions/0024-telemetry-values-in-home-assistant-expire.md)). A
companion's answers are current while at most one request in a row went unanswered and its last
answer is at most an hour old (three of its intervals, if that is longer); its position while an
answer within the last hour carried one. Otherwise its tracker and telemetry sensors in Home
Assistant are `unavailable`, and its state says why
([mqtt-api.md](mqtt-api.md#telemetry-states)).

Fixed by ADR 0018, not settings:

* **Lanes**, separate from `[airtime]`: 18 s per hour of the gateway's own transmit time (0.5 %)
  and 90 s per hour of estimated airtime in the mesh (every hop of request and answer, a flood as
  repeated by 20 repeaters: an estimate nothing measures). Over any interval *T* telemetry stays
  below 0.82 s + 18 s × *T* / 3600 of own transmit time and 20.4 s + 90 s × *T* / 3600 in the
  mesh at EU/UK Narrow, plus at most one request beyond. The lanes start empty at every start (a
  debt is carried over), so unlike `[airtime]` a restart adds no burst: the first request goes out
  about 76 s after the radio connects (EU/UK Narrow). Requests pause while the radio's own measured
  transmit time reaches 180 s in an hour.
* **Intervals**: 1 % of channel time per companion while it moves, 0.1 % at rest, at least 90 s
  and 15 minutes; once an hour without a position. A companion that stops answering is asked again
  after 30 s, then at least every 30 minutes for a day (or at its normal interval, if that is
  longer: an hour without a position), then up to every 6 hours.
* **Routes**: direct only. A companion without a stored path is asked by flood only when it was
  heard within 30 minutes when the flood is planned (an advert, a message, a path it sent; the
  flood may go out later while it waits for its airtime or the hourly limit, with several
  companions waiting an hour or more), at most 4 floods an hour and one per companion per
  30 minutes; one that has not answered within a day, once a day. The gateway
  never resets or writes a path for telemetry: a companion whose path a message's flood fallback
  (`flood_after`) reset is asked again once it advertises or sends something, then at once.

Needs: the radio adds contacts by hand only, no type automatically (the managed profile sets
that; otherwise requests pause, because a node the radio adds could take a companion's place in
its answers). Over TCP an answer can be lost while the radio's push queue is full; USB stays the
recommendation. The schedule (no positions) is kept in `state.json`, and with it the last document
of each companion whose answers are current, its position and readings included, also with the
history off. The exclusions are kept in `telemetry-excluded.json`; one that cannot be parsed
pauses telemetry until it is fixed or deleted and the service restarted
([deployment.md](deployment.md#4-operations)), and meanwhile no contact gets back its readings or
a position from telemetry from the history.

## `[diagnostics]`

| Key | Default | Meaning |
| --- | --- | --- |
| `publish_rx_log` | `false` | publish SNR, RSSI and raw bytes of every received packet on `rx` |
| `log_level` | `info` | `debug`, `info`, `warning`, `error`; message texts are logged only at `debug`, with positions, keys and every frame of the radio, which journald keeps for weeks: the service and `check-config` warn about it; set it back after the search |

## `[homie]`

The radio's contacts as a device of the Homie convention, for openHAB and the other Homie
controllers ([mqtt-api.md](mqtt-api.md#homie-homiehomie_device)). Off unless enabled; its
connection to the broker is a second one, with the settings of `[mqtt]`.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | publish the device `homie/meshcore-<node_id>` (`_` as `-`); the broker's ACL needs `homie/meshcore-<node_id>/#` for the gateway's user |
| `contacts` | `50` | contacts with a node, 0 to 1000, chosen as for their devices in Home Assistant (`homeassistant.contact_devices`) |

## Installer answers

`meshcore-home-gateway config render --answers <file> --output <config> --group
meshcore-home-gateway` builds a configuration from `key=value` lines and validates it like
`check-config` does, TLS files included. With `--password-file <path>` the answer
`mqtt.password` goes into that file (mode 0640, the same group) and the configuration names it as
`password_file`; answers without a password remove a file the configuration no longer names. Keys are the dotted settings above (`mqtt.host=…`),
`target.<alias>=channel:<index>` or `target.<alias>=contact:<public key>`; lists are
comma-separated; empty values are skipped. The Proxmox installer uses it so that no shell code
writes TOML.

`meshcore-home-gateway config answers --config <config>` does the reverse: it prints the settings
present in a configuration file as answers, with public keys normalised. The installer's
`--reconfigure` uses them as defaults and passes on the settings it does not ask for. The output
includes the broker password when the file holds it, and only the path with `password_file`.

## Language of the command line

The command line's texts for people (help, errors, warnings, the summary of `check-config`, the
report of `probe`) follow the environment, as Python's gettext reads it: the first of `LANGUAGE`,
`LC_ALL`, `LC_MESSAGES` and `LANG` that is set and not empty decides. `LANGUAGE` may list several
languages (`LANGUAGE=pl:de`): the first of them that the gateway has its texts in, English
included, wins; with none of them, English. `LANGUAGE=en` gives English. `LC_ALL=C` (or `C.UTF-8`) gives English only while
`LANGUAGE` is unset: unlike GNU programs, which ignore `LANGUAGE` under `C`, Python reads it first.

`run` and the service's log stay English, so the journal reads the same everywhere; so do the
outputs that programs read (`probe --list`, `--json`, `listen`, `send --wait`, the results of
`contacts add`, `remove` and `telemetry` with `--wait`, `contacts list`, `contacts link`,
`config answers`, `--version`), the names of commands, options and settings, and the texts the
system or the broker gives (a file's error, the broker's reason). The names sent to Home Assistant
follow [`[homeassistant] language`](#homeassistant), never the environment.
