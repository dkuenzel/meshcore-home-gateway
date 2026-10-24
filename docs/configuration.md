# Configuration

One TOML file, by default `/etc/meshcore-home-gateway/config.toml`, owner `root`, group
`meshcore-home-gateway`, mode 0640 (it contains the broker password). Unknown keys are errors, so a
typo never falls back to a default silently. Check a file with `meshcore-home-gateway check-config
--config <file>`; the service exits with code 78 on configuration errors and systemd does not
restart it then.

A complete example: [`deploy/config.example.toml`](../deploy/config.example.toml).

## `[radio]`

| Key | Default | Meaning |
| --- | --- | --- |
| `transport` | required | `serial` (USB) or `tcp` (WiFi companion firmware) |
| `device` | – | serial device, an absolute path; use the stable `/dev/serial/by-id/...` path. Required for `serial`, an error with `tcp` |
| `host` | – | radio address (host name or IP address, without port). Required for `tcp`, an error with `serial`. The TCP interface has no authentication |
| `port` | `5000` | TCP port; only with `tcp` |
| `set_clock` | `true` | move the radio clock forward to the bridge clock at connect (it cannot be moved back) |

The serial port always runs at 115200 baud with DTR asserted; 1200 baud is refused because it
would put nRF52 radios into their bootloader.

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
(`unsupported`). What the radio has, what the profile wants and what
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
| `tls` | `false` | TLS with certificate and host name verification |
| `ca_file` | system store | CA certificate file for TLS, an absolute path; only with `tls = true` |
| `cert_file`, `key_file` | – | client certificate and key for TLS authentication, absolute paths; only with `tls = true`. `key_file` needs `cert_file` and can be left out when the certificate file holds the key |
| `client_id` | `meshcore-home-gateway-<node_id>` | MQTT client identifier |
| `keepalive_s` | `60` | keep-alive interval, 10–3600 |
| `base_topic` | `meshcore` | first topic levels; segments of `A-Z a-z 0-9 - _` separated by `/` |
| `node_id` | required | this bridge's name in topics and in Home Assistant; 1–32 of `a-z 0-9 - _`, starting with a letter or digit, not ending with `-` or `_`, no two of them in a row (Home Assistant would merge those). Only the entity IDs have `_` for `-` ([home-assistant.md](home-assistant.md#entities)) |

A TLS file that cannot be loaded (missing, unreadable, not a certificate or key) is a
configuration error: `check-config` and `config render` load the files, and the service exits with
code 78. The paths must be absolute, like `radio.device`: the service runs in `/`, not in the
directory where `check-config` is started.

## `[homeassistant]`

| Key | Default | Meaning |
| --- | --- | --- |
| `discovery` | `true` | publish the discovery document and follow Home Assistant's birth message |
| `prefix` | `homeassistant` | discovery prefix (as set in Home Assistant's MQTT options); one topic level of `A-Z a-z 0-9 - _` |
| `device_name` | `MeshCore radio` | name of the device in Home Assistant |
| `status_topic` | `homeassistant/status` | topic of Home Assistant's birth message (MQTT options, *birth message*); it does not follow `prefix`. Segments of `A-Z a-z 0-9 - _` separated by `/` |
| `contact_devices` | `50` | contacts that get a device of their own, 0–1000; `0` removes them ([home-assistant.md](home-assistant.md#contacts)) |

## `[targets.<alias>]`

Named destinations; publish to `send/<alias>`. Channels can be reached only through a target;
companions in the radio's contact table also through `contacts/<key>/send`
([mqtt-api.md](mqtt-api.md#sending-sendalias)). The alias is 1–32 of
`a-z 0-9 - _`, starting with a letter or digit. Set exactly one of:

| Key | Meaning |
| --- | --- |
| `channel` | channel index on the radio (0–255). The channel must be configured on the radio; `meshcore-home-gateway probe` lists them |
| `contact` | the contact's full public key, 64 hex digits. The radio addresses contacts by the first 6 bytes; the bridge refuses to send if those match another contact too |

## `[messages]`

| Key | Default | Meaning |
| --- | --- | --- |
| `ascii_only` | `false` | transliterate to ASCII (ä → ae, typographic quotes, accents); for handhelds without Unicode fonts |
| `truncate` | `true` | cut texts above the radio limit between whole characters (flags and emoji sequences included) and mark them with `...`; `false` rejects them |
| `dedup_window_s` | `300` | the same text to the same target is sent once per window, 0–86400; `0` disables |
| `queue_size` | `20` | waiting messages, 1–1000; more are rejected |
| `queue_ttl_s` | `600` | time to live of a message, 1–86400: no attempt starts after it. Never sent within it, it expires; sent but not acknowledged, it fails when the acknowledgement wait of its last attempt ends (up to 60 s after the time to live) |
| `max_attempts` | `3` | sends of a direct message until acknowledged, 1–4 (the radio's attempt counter) |
| `flood_after` | `2` | unacknowledged attempts after which the stored path is reset, so the next attempt floods; 1 to `max_attempts` (equal to it: never floods) |
| `ignore_channels` | `[]` | channel indices (0–255) whose messages are not published (busy public channels); they do not hold telemetry back |

## `[airtime]`

| Key | Default | Meaning |
| --- | --- | --- |
| `budget_s_per_hour` | `36` | seconds of transmission per hour for the bridge's messages (1 % of the hour), 0.001–3600; telemetry has its own lanes (`[telemetry]`) |
| `burst_s` | `10` | seconds that may be spent at once, 5–3600 (a long packet at SF 8 / 62.5 kHz takes about 2 s) |

Within one run of the service, the airtime the bridge counts over any interval *T* stays below
`burst_s + budget_s_per_hour × T / 3600`. The count is exact for direct sends and for flooded
sends to recipients up to 4 path bytes away; a flooded attempt to a more distant recipient takes
up to about 1 s more at EU/UK Narrow ([protocol.md §6](protocol.md#6-on-air-sizes)). The budget is
not stored: each start begins with a full burst, so every restart within *T* adds up to
`burst_s`.
The radio's own traffic (acknowledgements it sends, adverts) is not counted; in the EU band
869.40–869.65 MHz the device limit is 10 % of every hour. The radio has its own limiter, the
*airtime factor*, which the firmware sets to 50 % and the managed profile to 10 % in that band
(`[radio.profile]`, protocol.md section 7).

## `[commands]`

| Key | Default | Meaning |
| --- | --- | --- |
| `senders` | `[]` | full public keys (64 hex digits) allowed to send commands |
| `words` | `[]` | command words, case-insensitive single words; the first word of a direct message |
| `max_age_s` | `900` | accept only commands whose sender timestamp is within this many seconds of the bridge clock, 0–86400; `0` disables the check, except that timestamps more than a day ahead are always refused (one of them would make every later command of that sender a replay); it also bounds when a replayed copy of a rejected command can still pass ([design §7.4](design.md#74-residual-risks)) |

Commands also need a sender timestamp newer than that sender's last accepted command; the bridge
keeps these timestamps in `/var/lib/meshcore-home-gateway/state.json`.

Commands that the bridge fetches while it connects are rejected: those that waited in the radio
while the bridge was not running (restart, update, unplugged radio), and those that arrive during
the few seconds of the connection setup. The sender sends them again
([ADR 0009](decisions/0009-inbound-commands-from-allowlisted-contacts.md)).

The managed profile (`[radio.profile]`) makes the radio add contacts manually, with every contact
type switched off in the auto-add settings. In manual mode the radio still adds the types enabled
there, and a contact of any type can send direct messages. A node that the radio adds with the
6-byte key prefix of an allowed sender blocks that sender's commands until 24 hours after it was
last seen; a node that only advertises is not added and does not count. When the profile only
observes, the bridge logs a warning at connect if the radio adds contacts on its own, and
`meshcore-home-gateway probe` shows the settings. Connect a radio that takes commands by USB: over
TCP it drops push frames while its send queue is full (the bridge warns about that too), and a
host that takes the radio's network address can pose as the radio and forge commands
([design §7.2](design.md#72-threats-and-mitigations)).

## `[history]`

The gateway keeps a history in `/var/lib/meshcore-home-gateway/history.sqlite3`: messages in and
out, positions of contacts (from their adverts and their telemetry, one row per change), and when
each node was last heard
([ADR 0016](decisions/0016-history-in-a-local-sqlite-database.md)). It fills the `history`
document after a restart, and later the position traces. Positions are kept only for contacts,
never for nodes the gateway merely heard.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | keep the history; with `false` nothing is written (delete the file by hand to remove what is there) |
| `messages_days` | `30` | days messages are kept, 1–3650; at most 10 000 messages |
| `positions_days` | `30` | days positions are kept, 1–3650; at most 100 000 positions, the oldest first, but every contact keeps its newest until it is that old |

The file holds message texts and positions of the household's people: it is readable only by the
service user. If it cannot be opened or written, the gateway logs it and runs without a history;
a file that is not a database is moved aside to `history.sqlite3.corrupt`.

## `[contacts]`

| Key | Default | Meaning |
| --- | --- | --- |
| `manage` | `true` | add and remove contacts on request: from Home Assistant ([home-assistant.md](home-assistant.md#adding-and-removing-contacts)), the command line (`meshcore-home-gateway contacts`) and MQTT ([mqtt-api.md](mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)). A contact grants no rights; contact targets and command senders are never removed. With `false` the controls in Home Assistant are `unavailable` |

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
A companion answers only contacts it allows: set the household's companions up first
([companions.md](companions.md)).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | ask every companion (chat contact) of the radio's table, one at a time, after the messages; positions go to `contacts/<key>` and the contact's tracker in Home Assistant |

**No contact can be excluded yet** (ADR 0018 provides it; it follows in the next release, and the
default becomes `true` with it). Switching telemetry on asks every chat contact of the radio:
keep only household companions as chat contacts before you do. A contact that answers everyone
would be tracked like a household member.

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
  flood may go out later while it waits for its airtime), at most 4 floods an hour and one
  per companion per 30 minutes; one that has not answered within a day, once a day. The gateway
  never resets or writes a path for telemetry: a companion whose path a message's flood fallback
  (`flood_after`) reset is asked again once it advertises or sends something, then at once.

Needs: the radio adds contacts by hand only, no type automatically (the managed profile sets
that; otherwise requests pause, because a node the radio adds could take a companion's place in
its answers). Over TCP an answer can be lost while the radio's push queue is full; USB stays the
recommendation. The schedule (no positions) is kept in `state.json`.

## `[diagnostics]`

| Key | Default | Meaning |
| --- | --- | --- |
| `publish_rx_log` | `false` | publish SNR, RSSI and raw bytes of every received packet on `rx` |
| `log_level` | `info` | `debug`, `info`, `warning`, `error`; message texts are logged only at `debug` |

## Installer answers

`meshcore-home-gateway config render --answers <file> --output <config> --group
meshcore-home-gateway` builds a configuration from `key=value` lines and validates it like
`check-config` does, TLS files included. Keys are the dotted settings above (`mqtt.host=…`),
`target.<alias>=channel:<index>` or `target.<alias>=contact:<public key>`; lists are
comma-separated; empty values are skipped. The Proxmox installer uses it so that no shell code
writes TOML.

`meshcore-home-gateway config answers --config <config>` does the reverse: it prints the settings
present in a configuration file as answers, with public keys normalised. The installer's
`--reconfigure` uses them as defaults and passes on the settings it does not ask for. The output
includes the broker password.
