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
| `device` | – | serial device; use the stable `/dev/serial/by-id/...` path. Required for `serial` |
| `host` | – | radio address. Required for `tcp`. The TCP interface has no authentication |
| `port` | `5000` | TCP port |
| `set_clock` | `true` | move the radio clock forward to the bridge clock at connect (it cannot be moved back) |

The serial port always runs at 115200 baud with DTR asserted; 1200 baud is refused because it
would put nRF52 radios into their bootloader.

## `[mqtt]`

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | required | broker address |
| `port` | `1883` | broker port (8883 is usual with TLS) |
| `username`, `password` | – | broker credentials |
| `tls` | `false` | TLS with certificate and host name verification |
| `ca_file` | system store | CA certificate file for TLS; only with `tls = true` |
| `cert_file`, `key_file` | – | client certificate and key for TLS authentication; only with `tls = true`. `key_file` needs `cert_file` and can be left out when the certificate file holds the key |
| `client_id` | `meshcore-home-gateway-<node_id>` | MQTT client identifier |
| `keepalive_s` | `60` | keep-alive interval, 10–3600 |
| `base_topic` | `meshcore` | first topic levels; segments of `A-Z a-z 0-9 - _` separated by `/` |
| `node_id` | required | this bridge's name in topics and entity IDs; 1–32 of `a-z 0-9 - _` |

A TLS file that cannot be loaded (missing, unreadable, not a certificate or key) is a
configuration error: `check-config` loads the files, and the service exits with code 78.

## `[homeassistant]`

| Key | Default | Meaning |
| --- | --- | --- |
| `discovery` | `true` | publish the discovery document and follow Home Assistant's birth message |
| `prefix` | `homeassistant` | discovery prefix (as set in Home Assistant's MQTT options) |
| `device_name` | `MeshCore radio` | name of the device in Home Assistant |
| `status_topic` | `homeassistant/status` | topic of Home Assistant's birth message (MQTT options, *birth message*); it does not follow `prefix` |

## `[targets.<alias>]`

The only destinations the bridge sends to; publish to `send/<alias>`. The alias is 1–32 of
`a-z 0-9 - _`. Set exactly one of:

| Key | Meaning |
| --- | --- |
| `channel` | channel index on the radio (0–255). The channel must be configured on the radio; `meshcore-home-gateway probe` lists them |
| `contact` | the contact's full public key, 64 hex digits. The radio addresses contacts by the first 6 bytes; the bridge refuses to send if those match another contact too |

## `[messages]`

| Key | Default | Meaning |
| --- | --- | --- |
| `ascii_only` | `false` | transliterate to ASCII (ä → ae, typographic quotes, accents); for handhelds without Unicode fonts |
| `truncate` | `true` | cut texts above the radio limit and mark them with `...`; `false` rejects them |
| `dedup_window_s` | `300` | the same text to the same target is sent once per window; `0` disables |
| `queue_size` | `20` | waiting messages, 1–1000; more are rejected |
| `queue_ttl_s` | `600` | time to live of a message: never sent within it, it expires; sent but not acknowledged within it, it fails |
| `max_attempts` | `3` | sends of a direct message until acknowledged, 1–4 (the radio's attempt counter) |
| `flood_after` | `2` | unacknowledged attempts after which the stored path is reset, so the next attempt floods |
| `ignore_channels` | `[]` | channel indices whose messages are not published (busy public channels) |

## `[airtime]`

| Key | Default | Meaning |
| --- | --- | --- |
| `budget_s_per_hour` | `36` | seconds of transmission per hour for the bridge's messages (1 % of the hour) |
| `burst_s` | `10` | seconds that may be spent at once; at least 5 (a long packet at SF 8 / 62.5 kHz takes about 2 s) |

Over any interval *T* the bridge's transmissions stay below `burst_s + budget_s_per_hour × T / 3600`.
The radio's own traffic (acknowledgements it sends, adverts) is not counted; in the EU band
869.40–869.65 MHz the device limit is 10 % of every hour. The radio has its own limiter, the
*airtime factor*, which defaults to 50 % (docs/protocol.md, section 7).

## `[commands]`

| Key | Default | Meaning |
| --- | --- | --- |
| `senders` | `[]` | full public keys (64 hex digits) allowed to send commands |
| `words` | `[]` | command words, case-insensitive single words; the first word of a direct message |
| `max_age_s` | `900` | accept only commands whose sender timestamp is within this many seconds of the bridge clock; `0` disables the check, except that timestamps more than a day ahead are always refused (one of them would make every later command of that sender a replay) |

Commands also need a sender timestamp newer than that sender's last accepted command; the bridge
keeps these timestamps in `/var/lib/meshcore-home-gateway/state.json`.

Commands that the bridge fetches while it connects are rejected: those that waited in the radio
while the bridge was not running (restart, update, unplugged radio), and those that arrive during
the few seconds of the connection setup. The sender sends them again
([ADR 0009](decisions/0009-inbound-commands-from-allowlisted-contacts.md)).

Recommended radio settings (in the MeshCore app, before the bridge takes the radio over): add
contacts manually and switch off every contact type in the auto-add settings. In manual mode the
radio still adds the types enabled there, and a contact of any type can send direct messages. A node
that the radio adds with the 6-byte key prefix of an allowed sender blocks that sender's commands
until 24 hours after it was last seen; a node that only advertises is not added and does not count.
The bridge logs a warning at connect when the radio adds contacts on its own, and
`meshcore-home-gateway probe` shows the settings. Connect a radio that takes commands by USB: over
TCP it drops push frames while its send queue is full, and the bridge warns about that too.

## `[diagnostics]`

| Key | Default | Meaning |
| --- | --- | --- |
| `publish_rx_log` | `false` | publish SNR, RSSI and raw bytes of every received packet on `rx` |
| `log_level` | `info` | `debug`, `info`, `warning`, `error`; message texts are logged only at `debug` |

## Installer answers

`meshcore-home-gateway config render --answers <file> --output <config> --group
meshcore-home-gateway` builds a configuration from `key=value` lines and validates it like the
service does. Keys are the dotted settings above (`mqtt.host=…`), `target.<alias>=channel:<index>`
or `target.<alias>=contact:<public key>`; lists are comma-separated; empty values are skipped. The
Proxmox installer uses it so that no shell code writes TOML.

`meshcore-home-gateway config answers --config <config>` does the reverse: it prints the settings
present in a configuration file as answers, with public keys normalised. The installer's
`--reconfigure` uses them as defaults and passes on the settings it does not ask for. The output
includes the broker password.
