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
   `meshcore-home-gateway contacts add`. Best add it after the gateway heard its advert (*Add
   heard node*), so that the gateway checks that advert at once
   ([positions in adverts](#positions-in-adverts)); one the gateway did not hear before the add
   (added from a key or a link first, or after a restart of the gateway between hearing and adding
   it) is checked at its next advert.
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
   an advert reaches everyone in range. The gateway checks it: when a companion's advert carries
   its position, *Position in adverts* on its device turns on, also when it sends 0, 0 because it
   never had a position. A companion without a fix sends its last or saved position. After
   switching it off, press *Advert* within range of the gateway's radio (the button's advert
   crosses no repeater), or send a flood advert from the app, so that the gateway sees an advert
   without it.
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
* Its device in Home Assistant has the switch *Telemetry* and the sensors *Telemetry state* and
  *Position in adverts* at once; *Battery*, *MCU temperature* (diagnostic), a sensor per reading
  (*Temperature 2*, *Humidity 2*, …) and its tracker (`device_tracker.mesh_<name>`) appear with
  the first answer that carries them, together with the event *Telemetry* `answered` on the
  gateway's device. Its *Last heard* follows adverts and messages at once and telemetry answers at
  most once an hour.
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
* Whether the GPS is on, and how old a position is. Some tracker boards send their stored
  position with the GPS off; a companion with the GPS on but without a fix sends its last valid
  position, after a reboot the one it saved, in its answers and its adverts. The gateway shows when
  it received a position (`position_received_at`) and when it last changed (`position_at`), never
  when the companion took it.
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
* Adverts its radio does not hear: the device button's advert reaches only the gateway's radio in
  direct range, since it crosses no repeater. Adverts while the gateway is stopped, and over TCP an
  advert whose report the radio dropped, are not checked either. Until it has checked a
  companion's newest advert, the gateway goes by the radio's record and errs to warn
  ([below](#positions-in-adverts)).

## Positions and presence

* **A position from telemetry expires** by the rule above: its tracker turns `unavailable`, and
  a `person` then follows its other trackers, or turns `unknown` with none. Presence triggers react
  differently to that: `zone.left` never fires for a person whose only GPS tracker is the mesh
  tracker, a state trigger `from: home` does. The table and a recommended automation are in
  [home-assistant.md](home-assistant.md#positions-and-presence).
* A telemetry position carries an accuracy of 15 m, so Home Assistant counts a companion up to
  15 m outside a zone's radius as inside it.
* **A position from adverts** stays only while the last advert the gateway checked carried it,
  or, before a first check (after the update), while the radio's record holds one: once the
  gateway checked an advert without it, its tracker turns `unavailable` unless telemetry gives a
  position. A position in an advert can be one the companion saved long ago.
* A position from telemetry is stored in the history (and written by Home Assistant's recorder)
  only when it moved 20 m or more: a companion at rest adds nothing per answer. Each advert with a
  changed position is stored, and so is a switch between adverts and telemetry. Beyond 100 000
  positions the history drops the oldest first, never a companion's newest one, which goes only
  when it is older than `[history] positions_days`.

## Positions in adverts

An advert reaches everyone in range, so the gateway checks whether each companion's newest advert
carries its position ([ADR 0025](decisions/0025-positions-in-adverts-checked-from-the-packet-log.md)).
It reads the advert in its radio's packet log, once the radio has taken it, and sends nothing.
*Position in adverts* on the companion's device and `advert_carries_position` in its document
say the result, `advert_checked_at` when the checked advert was made. The gateway's *Companion
position in adverts* is on while that sensor is on for a companion not excluded from telemetry,
and the event *Contact* `position_in_advert` comes when such a companion starts to share
([home-assistant.md](home-assistant.md#examples)).

Until the gateway has checked a companion's newest advert (after the update, an advert it missed,
or one made while it was stopped), it goes by the radio's record: the sensor is on while the last
check carried a position or the record holds one from an earlier advert. The radio keeps that
position after the companion stops sharing, so the sensor can be on although it no longer shares.
It can also be off although it shares, while the record holds no position: the companion never had
one, or it was added (by the gateway before 1.1.0a3, from a key or a link before the gateway heard
it, or after a restart of the gateway between hearing and adding it) and has not adverted since.
So press *Advert* once on each companion within range of the gateway's radio, or send a flood
advert from its app, after the update whatever its sensor shows, and after switching sharing off.

## Privacy

"Allow All" instead of "From Specific Contacts" gives the position to every contact of the
companion: anyone it has as a contact can ask. "Share Position in Advert" gives it to everyone in
range. The radio's record of a companion keeps the last position its adverts carried; the gateway
shows a position from adverts only while the last advert it checked carried it, or, before it has
checked one (after the update), while the record holds one. So press *Advert* near the gateway
after the update and after switching sharing off. The gateway itself reveals
nothing it learns on the mesh; positions and readings reach the MQTT broker and Home Assistant,
and the history keeps them for `[history] positions_days` and `readings_days`. Excluding a
companion stops the requests and forgets its current values;
`meshcore-home-gateway contacts telemetry --forget KEY off` also deletes its readings and
telemetry positions from the gateway's history. Home Assistant keeps its own history
([home-assistant.md](home-assistant.md#privacy)).
