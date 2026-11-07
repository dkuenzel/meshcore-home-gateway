# What the gateway keeps

The gateway records what its radio hears, for the household: its messages, its companions'
positions and readings, its contacts and the nodes around the house. Part of that is about people
outside the household: everyone in range whose radio sends adverts, and everyone who writes on a
channel the radio has the key of, such as the *Public* channel, whose key is in every firmware.
This page lists what is kept, where, for how long, and how to switch each part off.

Data protection law usually exempts what a household keeps for itself (in the EU, Art. 2(2)(c)
GDPR); the exemption does not cover what leaves the household, and the Court of Justice held that
a home camera filming public space falls outside it (Ryneš, C-212/13): recording every radio that
passes the house is closer to that than to a phone's contacts, which is why `[contacts]
heard_nodes` can keep less of strangers. This is not legal advice. Do not post dashboards,
screenshots or copies of the history that show other people's names, keys or messages, and keep
backups private and encrypted ([state and backup](deployment.md#4-operations)). The archive of
`meshcore-home-gateway backup` holds all of the state, and what `history export` prints holds what
the history holds: treat both as the history itself.

## Where

| Place | Who can read it | How long |
| --- | --- | --- |
| Retained topics on the broker (`meshcore/<node_id>/…`) | every client the broker lets read them ([securing the broker](mqtt-api.md#securing-the-broker)) | until the gateway replaces or clears them; after the gateway is removed, until you clear them (`meshcore-home-gateway retire`, [operations](deployment.md#4-operations)) |
| Events on the broker (`message`, `node`, `contact`, `rx`, …) | the clients subscribed at the time | not kept by the broker |
| `history.sqlite3` in `/var/lib/meshcore-home-gateway/` | the service, and whoever has the container or a backup | per kind, below ([`[history]`](configuration.md#history)) |
| `state.json` and `telemetry-excluded.json` beside it | as the history | while the gateway needs them ([what each holds](deployment.md#4-operations)) |
| Home Assistant's recorder and `.storage/core.restore_state` | Home Assistant's users and backups | the recorder's `purge_keep_days` (10 days by default); the long-term statistics of sensors without end ([Home Assistant](home-assistant.md#privacy)) |
| The journal of the container | its administrators | journald's limits (the installer allows it 100 MB, the oldest lines go first); message texts only with `log_level = "debug"` |

## The household

| What | Where | How long | Off |
| --- | --- | --- | --- |
| Direct messages with contacts and messages on channels: text, the sender's name and key, signal | `message` events, `history` (retained) and Home Assistant's *Message history*, the history | 30 days (`messages_days`) | `[history] enabled = false` keeps no history; Home Assistant's recorder: [exclusions](home-assistant.md#privacy) |
| Contacts: key, name, type, path, when and how they were last heard, the checks of their adverts | the contacts' documents (retained), `state.json`, the history | while they are contacts; the history's row 7 days after the last time heard | remove the contact |
| Companions' positions and readings, from telemetry and from adverts | the contacts' documents (retained), `state.json` while current, the history | 30 days (`positions_days`, `readings_days`) | only the companions included are asked (`[telemetry] ask = "household"`, the default); exclude a companion from telemetry; `contacts telemetry --forget KEY off` deletes what the history has of it ([companions](companions.md#privacy)) |
| The newest accepted command time of each sender | `state.json` | while the sender is one | — |
| Messages on their way, accepted and not sent or given up yet: target, recipient's name and text | `pending` (retained) and Home Assistant's *Messages on their way*; `state.json` from the start of a stop | until each is final; in `state.json` until the next start of 2.0 or later queues them again or ends them (a roll back to 1.x keeps them meanwhile, [deployment.md](deployment.md#update-reconfigure-remove)) | Home Assistant's recorder: [exclusions](home-assistant.md#privacy) |
| The settings in effect: the targets with their aliases, prefixes and suffixes and the first 8 digits of a contact target's key, hidden ones included; never hosts, passwords, keys or command words | `config` (retained) and Home Assistant's *Configuration* | until the gateway publishes it again: at the next start or radio connect | — (it holds no message and no position); every user of Home Assistant and every broker client that may read `meshcore/<node>/#` reads it |
| Drafts of Home Assistant's chats: text a user wrote and has not sent | the gateway's memory, `homeassistant/draft/<kind>/<chat>` (retained, also in the broker's persistence file such as `mosquitto.db`) and the text entities *Write to …* and *Write* | until it is sent; emptied after 15 minutes without a change and at a clean stop of the gateway | Home Assistant's recorder: [exclusions](home-assistant.md#privacy); every user of Home Assistant and every broker client that may read `meshcore/<node>/#` reads them |

## Other people

| What | Where | How long | Off |
| --- | --- | --- | --- |
| Nodes heard but not added: key and type; with `[contacts] heard_nodes = "full"` also the name its owner chose, when it was first and last heard, hops, SNR and RSSI; never a position | `nodes` (retained, the last 50), `node` events, Home Assistant's *Nodes heard*, *New node* and *Look-alike node heard*, the history | 7 days after the last time heard, in memory and in the history | `[contacts] heard_nodes = "off"` keeps nothing but the keys of look-alikes of contacts and protected keys ([configuration](configuration.md#contacts)) |
| Messages on the channels that are a target: text and the name the sender claims; with `[messages] publish_channels = "all"` on every channel | `message` events, `history` (retained) and Home Assistant's *Message history*, the history | 30 days | a channel that is not a target is dropped (`[messages] publish_channels = "targets"`, the default); `ignore_channels` leaves out single ones ([configuration](configuration.md#messages)) |
| Other people's companions among the radio's contacts | as the household's | as the household's | none of them is asked until included (`[telemetry] ask = "household"`, the default); with `"all"` exclude them ([telemetry](configuration.md#telemetry)) |
| Every packet the radio receives: raw bytes with other people's ciphertext and the positions in their adverts, and the signal | `rx` events; Home Assistant's *Last packet RSSI* and *Last packet SNR* (disabled by default) | not kept by the gateway | off unless `[diagnostics] publish_rx_log = true`: a diagnostic, it shows the packets as received, positions included |

A look-alike, a node with the first 6 bytes of a contact's key, is kept even with `heard_nodes =
"off"`: it is the warning that someone could pass for that contact
([ADR 0009](decisions/0009-inbound-commands-from-allowlisted-contacts.md)).

The gateway sends nothing it learns on the mesh back to the mesh. As the managed profile keeps
it, its radio puts no position into its adverts and answers no one's telemetry requests
([configuration](configuration.md#radioprofile)).
