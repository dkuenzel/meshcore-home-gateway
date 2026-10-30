# Setting up the household's companions for telemetry

The gateway asks each companion of its radio for its telemetry: battery, readings and, with a GPS,
its position ([ADR 0018](decisions/0018-telemetry-pulled-from-the-households-companions.md),
[design §6.11](design.md#611-telemetry)). A companion answers only the contacts it allows, and
says nothing to the others: to the gateway, "not allowed", "out of range" and "switched off" all
look the same. The settings below are made on each companion, in the MeshCore app; the gateway
cannot read or set them. The labels are those of the MeshCore app 1.50 and may differ in other
versions.

## On the gateway

1. The radio must add contacts by hand only, with no contact type added automatically. The
   managed profile does that (`[radio.profile]`, [configuration.md](configuration.md)). While the
   radio adds contacts of any type on its own, telemetry pauses (`state.telemetry` is `paused`):
   a node it adds could share a companion's key prefix and answer in its place.
2. Each companion must be a contact of the radio (a chat contact): add it from Home Assistant
   ([home-assistant.md](home-assistant.md#adding-and-removing-contacts)) or with
   `meshcore-home-gateway contacts add`.
3. **Every chat contact of the radio is asked** unless it is excluded. Exclude every chat contact
   that is not the household's: with the switch *Telemetry* on its device in Home Assistant, or
   `meshcore-home-gateway contacts telemetry KEY off`. Best do it before you add it: a full key
   works also before the contact is on the radio, and a new contact is asked within a minute. The
   first time telemetry runs on an installation, the gateway asks nobody for an hour after the
   radio connects (*Telemetry status* `paused`, reason `first_start`, and a warning in its log with
   the number of companions it will ask): exclude the strangers in that hour
   ([home-assistant.md](home-assistant.md#telemetry-of-the-households-companions)).

## On each companion

4. Add the gateway as a contact: scan its QR code (a card in Home Assistant shows it,
   [home-assistant.md](home-assistant.md#adding-and-removing-contacts); the link is
   `state.contact_link`).
5. *Settings → Telemetry Settings*: "Allow Telemetry Requests?" **From Specific Contacts**;
   "Include Location in your Telemetry?" and "Include Environment in your Telemetry?" **For
   Specific Contacts**.
6. The gateway's contact entry → its permissions: "Allow Telemetry Requests?", location and
   environment **Yes**; "Allow Remote CLI" **No**. Mark it as a favourite, so a full contact table
   never drops it. (The radio stores this as the contact flags 0x0F.)
7. Check the counts the app shows: "1 contact(s) have the telemetry request permission" and the
   same for location. Any other number means someone else may ask too. This is the only check of
   "the gateway only".
8. *Position Settings*: "GPS Mode" **Enabled**. *Public Info*: "Share Position in Advert" **off**:
   an advert reaches everyone in range.
9. Send the gateway one message from the companion. It teaches both radios a route, so the first
   request goes direct; without it the gateway floods a request only while it has heard the
   companion within the last 30 minutes (an advert, also the one it was added from or one heard
   before a restart, or a message as it arrived). A companion the gateway may not ask for that
   reason reads `no_path` until it does.

Telemetry is on by default (`[telemetry] enabled`, [configuration.md](configuration.md#telemetry)).

## What to expect

* The first request about a minute and a quarter after the radio connects (the airtime lanes
  start empty; EU/UK Narrow), then about every 15 minutes while the companion rests next to the
  gateway and every 90 seconds while it moves (50 m or more); over repeaters less often (one
  repeater: 31 and 3.1 minutes, two: 47 and 4.7 minutes). Without a position in its answers, once
  an hour.
* A companion that stops answering (out of range, switched off) is asked again after 30 seconds,
  then at least every 30 minutes for a day, or at its normal interval if that is longer (an hour
  without a position, 47 minutes over two repeaters), on the route it had: it is found again that
  long after coming back, without pressing anything. An advert or a message from it brings its next
  request forward to its normal interval. After a day without an answer, and for a companion that
  never answered, the requests thin out to one every 6 hours.
* **Exception:** when the gateway sent the companion a message while it was away and the message
  fell back to a flood, the radio forgot the companion's route. It is then asked again only once
  it advertises or sends a message, and then at once: the gateway floods a companion only when it
  heard it within 30 minutes (the flood may go out later while it waits for its airtime or for the
  limit of 4 floods an hour), and it never writes a route for telemetry.
* Its device in Home Assistant has the switch *Telemetry* and the sensor *Telemetry state* at
  once; *Battery*, *MCU temperature* (diagnostic), a sensor per reading (*Temperature 2*,
  *Humidity 2*, …) and its tracker (`device_tracker.mesh_<name>`) appear with the first answer
  that carries them, together with the event *Telemetry* `answered` on the gateway's device. Its
  *Last heard* follows adverts and messages at once and telemetry answers at most once an hour.
* *Telemetry state*:

  | State | Meaning | What to do |
  | --- | --- | --- |
  | `waiting` | not asked yet, or not since its last answer more than a day ago | wait for its first request |
  | `answering` | its answers are current and carry a position, or one did within the last hour | – |
  | `no_position` | its answers are current, without a position | GPS on, outdoors, location allowed (steps 5, 6, 8) |
  | `away` | it answered within the day, not lately: out of range, switched off, or not asked meanwhile | it is found again within 30 minutes of coming back |
  | `no_answer` | asked, no answer within a day | check steps 4 to 7: "not allowed" and "out of range" look the same |
  | `no_path` | not asked: the radio has no route to it and has not heard it within 30 minutes | send the gateway a message from it, or advertise (step 9) |
  | `look_alike` | another contact or the gateway's radio has its first 6 key bytes | remove the other contact |
  | `excluded` | the household excluded it | switch *Telemetry* on to have it asked again |
  | `off` | telemetry is off in the configuration | – |

* **Expiry.** Telemetry is current while the companion answers. After two unanswered requests in
  a row, or an hour without an answer (three of its intervals if that is longer), its tracker and
  telemetry sensors turn unavailable until it answers again. Its position counts while an answer
  within the last hour carried one. At rest next to the gateway that is about 16 minutes after it
  left, 2 minutes while it moves, 48 minutes behind two repeaters; the event *Telemetry*
  `unanswered` comes then. While the gateway's radio is unplugged or telemetry is paused, nobody is
  asked: the values expire an hour after the last answer, without an event.

## What the gateway cannot see

* The permissions above: it only sees whether an answer comes.
* Whether the GPS is on. Some tracker boards send their stored position with the GPS off. The
  gateway shows when it received a position (`position_received_at`) and when it last changed
  (`position_at`), never when the companion took it.
* Companions without a GPS (or with it off) send no position; battery and readings still come.
* Which sensor sent a reading: a companion reports its own readings on channel 1 and the sensors
  it found when it booted on channels 2 and up, in that order. A sensor missing at one boot moves
  the later ones to a lower channel: the old sensor then stays `unknown`; disable it in Home
  Assistant.
* Whether *MCU temperature* is the processor's: the gateway takes the answer's last value if it is
  a channel 1 temperature. On a board without a processor sensor, a channel 1 sensor that comes
  last gets that name.
* The light level of a T1000-E: it sends 0–100 in the luminosity type, other boards lux, so
  *Luminosity 1* has no unit.

## Positions and presence

* **A position from telemetry expires** by the rule above: its tracker turns `unavailable`, and
  a `person` then follows its other trackers, or turns `unknown` with none. Presence triggers react
  differently to that: `zone.left` never fires for a person whose only GPS tracker is the mesh
  tracker, a state trigger `from: home` does. The table and a recommended automation are in
  [home-assistant.md](home-assistant.md#positions-and-presence).
* A telemetry position carries an accuracy of 15 m, so Home Assistant counts a companion up to
  15 m outside a zone's radius as inside it.
* A position is stored in the history (and written by Home Assistant's recorder) only when it
  moved 20 m or more: a companion at rest adds nothing per answer. Beyond 100 000 positions the
  history drops the oldest first, never a companion's newest one, which goes only when it is
  older than `[history] positions_days`.

## Privacy

"Allow All" instead of "From Specific Contacts" gives the position to every contact of the
companion: anyone it has as a contact can ask. The gateway itself reveals nothing it learns on
the mesh; positions and readings reach the MQTT broker and Home Assistant, and the history keeps
them for `[history] positions_days` and `readings_days`. Excluding a companion stops the requests
and forgets its current values; `meshcore-home-gateway contacts telemetry --forget KEY off` also
deletes its readings and telemetry positions from the gateway's history. Home Assistant keeps its
own history ([home-assistant.md](home-assistant.md#privacy)).
