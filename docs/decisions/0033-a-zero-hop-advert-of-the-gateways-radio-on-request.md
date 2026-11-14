---
status: accepted
date: 2026-11-13
decision-makers: maintainers
---

# A zero-hop advert of the gateway's radio on request

## Context and Problem Statement

The gateway holds its radio's only link, and the companion firmware sends an advert only when its
app or its board's button asks for one: its advert intervals are commented out
(`examples/companion_radio/MyMesh.cpp:2436-2447`, `NodePrefs.h:195-196`). So the gateway's radio
never advertises. A person nearby can add the household's gateway only by its QR code or contact
link, and a new name of the radio ([ADR 0020](0020-managed-radio-profile-for-the-observer.md)'s
`name`) never reaches the companions that have it as a contact: a node changes a contact's name
only from an advert newer than the one it stored (`src/helpers/BaseChatMesh.cpp:133-136, 191`).
How may the household make the radio advertise, without a position, a flood or a lasting change
to the radio?

## Decision Drivers

* The observer reveals nothing but its name and key
  ([ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md),
  [ADR 0020](0020-managed-radio-profile-for-the-observer.md)): never a position.
* Bounded airtime, alerts first ([ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md),
  [ADR 0028](0028-outgoing-queue-kept-across-restarts.md)).
* No write that lasts on the radio.
* The household's Home Assistant users are trusted to press it (the maintainer's decision); an
  administrator can switch that off.
* Nodes keep the newest advert time of a contact: an advert stamped by a radio clock that is
  ahead makes them ignore the radio's later adverts.
* Every front end (MQTT, the command line, Home Assistant) has the same rules.

## Considered Options

* A zero-hop advert on request, with guards.
* A flood advert on request.
* Periodic zero-hop adverts by the gateway.
* None: the QR code and the contact link only, as in 2.1.
* A contact card exported from the radio (`CMD_EXPORT_CONTACT`) and shared out of band.

## Decision Outcome

Chosen option: a zero-hop advert on request, with guards, because it is the only one that lets a
neighbour add the gateway without a phone at the screen and brings a new name to the
household's companions, while it never floods and never carries a position.

* **The command.** The gateway writes `SEND_SELF_ADVERT` (7) without its optional byte: the code
  alone sends the advert zero hop, a second byte 1 would flood it (`MyMesh.cpp:1297-1316`). The
  codec's encoder takes no argument, so no caller can ask for a flood. The command writes no
  preferences and no contacts (`MyMesh.cpp:1297-1316`); `OK` means the radio queued the packet,
  `ERR 3` that no packet buffer was free (`src/Mesh.cpp:407-410`).
* **The request** is published on `<base_topic>/<node_id>/advert/send`: any payload of at most
  1 KiB, or a JSON object whose `id` (1 to 64 of `A-Z a-z 0-9 . _ : -`) comes back in the
  event; without one the gateway makes one of 8 hex digits. The outcome goes to `advert`, one
  event per request: `sent` (the radio queued it), `refused` (nothing was written) or `failed`
  (written and not confirmed: the radio's error name, `outcome_unknown` when the connection ended
  after the write, `internal_error` for a bug). The event carries `event_type`, `id`, `reason`,
  `name` and `airtime_ms` of an advert that was written, and `next_at`, when the next one is
  possible. A request that repeats the `id` of the one in progress, or of the one whose advert
  went out or may have in the last 10 minutes, is a copy (a QoS 1 resend) and gets no event; a
  request that ended with nothing sent leaves its `id` free, so the same request may come again.
* **The checks, in this order**; the first that fails is the reason of `refused`: `too_large`,
  `invalid_utf8`, `invalid_request` (the request's form, as the contact requests'); `retained`
  and `empty` (a left-over, or what clearing one publishes); `no_radio`; `untested_firmware`,
  firmware the gateway is not tested with, also with `radio.profile.manage = "always"`, since an
  advert's route and position are facts of the tested firmware that cannot be read back;
  `advert_location`, the radio's advert location policy as the radio reports it (byte 45 of
  `SELF_INFO`) is not 0, also when `manage = "observe"` leaves it as found: with policy 1 the
  advert always carries a position, 0, 0 included (`AdvertDataHelpers.h:27-30`,
  `AdvertDataHelpers.cpp:4-11`); `no_name`, a radio without a name, whose advert every node drops
  (`BaseChatMesh.cpp:122-127`); `busy`, another request is between its checks and its outcome;
  `too_soon`, an advert was written less than 10 minutes ago; `messages_waiting`, a message is
  queued or being delivered (the radio is half-duplex, and messages have the first claim on the
  budget); `airtime_budget`, the message budget cannot pay the advert's estimated time on air at
  once and still hold one long message beside it, so that an alert after it goes out at once
  (where the two do not fit into `burst_s` together, at slow presets, no advert goes until it is
  raised); `rate_limited`, the requests that passed asked the radio too often (each reads its
  clock, also when it is then refused), as the contact requests' radio work. Then the radio's
  clock is read afresh (`GET_DEVICE_TIME`): `clock_unknown` when the radio answers with an error,
  so that no advert goes with a clock the gateway did not check, and `clock_ahead` when it is
  more than 60 s ahead of the host's.
* **The claim.** Once the advert holds the radio's command lock, the gateway checks the
  firmware, the position policy, the name, the 10 minutes, the messages and the budget again; a message that came while
  the clock was read goes first. Only then is the estimated airtime taken from the budget (its
  level stored at once), the 10 minutes started and the command written. A `refused` advert
  counts for neither. The 10 minutes are kept in memory only: a restart allows one at once. A
  radio `ERR` means it built no advert (`MyMesh.cpp:1304-1316`): the airtime is given back and
  the 10 minutes do not start.
* **Home Assistant.** The gateway's device gets the button *Send advert nearby*, which publishes
  `send` on `advert/send` at QoS 0, and the event entity *Advert nearby* (`advert`). The button
  is behind the new setting `[homeassistant] radio_controls`, `true` by default: anyone who can
  use the Home Assistant instance can press it. With `radio_controls = false`, or with
  `controls = false`, the button stays announced and is unavailable, as Home Assistant's other
  controls ([ADR 0031](0031-household-chat-problems-and-settings-on-the-generated-dashboard.md),
  SEC-008): removing a component of the gateway's device can make Home Assistant clear the whole
  device. The button is also unavailable while the gateway is offline or the radio not
  connected; the other checks show as the outcome after a press. MQTT and the command line work
  whatever the two settings say; the broker's ACL decides who may write `advert/send`.
* **The command line**, `meshcore-home-gateway advert [--wait SECONDS]`, goes through the broker
  as `contacts` does: it publishes `{"id": "cli-…"}` on `advert/send` once the gateway is
  online and prints the `advert` event with that id. The service holds the radio's only link.
* The Homie device gets no property: `advert/send` serves every MQTT client.
* The commands never sent ([ADR 0020](0020-managed-radio-profile-for-the-observer.md)) are not
  affected: command 7 is not among them and writes nothing that lasts.

### Consequences

* Good, because a neighbour adds the gateway without typing its key or scanning its QR code, and
  a new name reaches the household's companions in range.
* Good, because it is one packet of about 1.2 s at EU/UK Narrow (103 bytes and the name, at most
  134), at most six an hour, never a position or a flood, and nothing written to the flash.
* Good, because every front end has the same rules, and each request ends in one event that
  says why.
* Bad, because only nodes in direct range hear it: no repeater passes a zero-hop packet on
  (`src/Mesh.cpp:78-108, 344-357`), and a repeater keeps a zero-hop advert only of another
  repeater as a neighbour (`examples/simple_repeater/MyMesh.cpp:650-661`), while the gateway's
  radio advertises as a companion (`BaseChatMesh.cpp:21-30`). A companion farther away still
  needs the QR code.
* Bad, because every Home Assistant user can make the radio announce itself (bounded by the 10
  minutes and the budget) unless an administrator switches it off.
* Bad, because the radio's name and full key go to everyone in range, whose apps may keep them
  as a heard node or a contact, and may pass the stored advert on with *Share* or an export
  (`BaseChatMesh.cpp:143-150, 570-586`; `MyMesh.cpp:1359-1370`), as anyone can the QR code.
* Bad, because up to about 8 s an hour of the message budget go to adverts (22 % of the default
  36 s at six adverts of 1.3 s).
* Bad, because at slow presets an advert and a long message do not fit into the default burst
  of 10 s together (at 125 kHz, SF 12, CR 4/5 up to 5.4 s and 9.1 s): there no advert goes until
  `burst_s` is raised, and then each takes more of the hour.
* Bad, because a restart forgets the 10 minutes. The budget's level is kept across a restart,
  and the least a start grants (one long message, at most the burst) never pays an advert beside
  the reserve: a restart loop gets no advert from that floor.
* Bad, because while the radio transmits it hears nothing: messages are guarded, a telemetry
  answer in flight is not (the poller's retry covers it).
* Bad, because the advert carries the radio's clock, which the gateway sets from the host's: a
  host clock that is ahead passes the 60 s check, and nodes that take the advert ignore the
  radio's adverts until real time passes it. A radio clock behind makes nodes that stored a newer
  advert drop this one; the event still says `sent`.
* Neutral: 60 s ahead are safe beside the 10 minutes: the next advert is stamped later than one
  stamped up to 60 s ahead.

### Trust boundaries

* B1 the air: the advert is public and signed; it carries the name, the key and the radio's
  clock, never a position (the gateway checks the radio's own policy); zero hop, no repeater
  passes it on.
* B3 the broker: a new request topic. A client the ACL lets write `advert/send` makes the radio
  advertise, at most every 10 minutes; the event carries no secret.
* B5 Home Assistant's users: every user may press the button; `radio_controls = false` and
  `controls = false` make it unavailable; the broker's ACL decides what `mqtt.publish` can do.

### Confirmation

The golden frame `3C 01 00 07` and the encoder without a parameter (`tests/test_codec.py`);
`tests/test_advertiser.py` (one frame and one event per request, each reason, the claim, the 10
minutes, the budget and its reserve); `tests/test_session.py`; the command line's tests; the
discovery and Home Assistant contract tests (the button with its settings, any user can press
it); the firmware tests in `tests/firmware/test_advert_nearby.py` (a neighbour gets the current
name, zero hop, without a position; no repeater passes it on; a radio that would share its
position sends none).

## Pros and Cons of the Options

### A zero-hop advert on request, with guards

* Good, because the household decides when, and every guard says why it held.
* Bad, because only the radios in direct range hear it.

### A flood advert on request

* Good, because it reaches the whole mesh.
* Bad, because every repeater retransmits it ([ADR 0018](0018-telemetry-pulled-from-the-households-companions.md)
  assumes 20 for a flood) on the shared channel of [ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md),
  and it spreads the name and key beyond the neighbourhood. The maintainer: never a flood.

### Periodic zero-hop adverts by the gateway

* Good, because nobody has to press anything.
* Bad, because it spends airtime when nobody needs it, and adds a schedule.

### None: the QR code and the contact link only

* Good, because nothing changes.
* Bad, because a phone must be at the screen or a link copied, and a new name never arrives.

### A contact card shared out of band

* Good, because it needs no airtime.
* Bad, because it needs the same channel as the QR code, and it changes no stored name.

## More Information

* Firmware citations are of the pinned release (`tests/firmware/sources.toml`), MeshCore
  companion-v1.18.0.
* It amends [ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md) (an advert on
  request is charged to the message budget), [ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md)
  (the observer also transmits its advert on request) and
  [ADR 0031](0031-household-chat-problems-and-settings-on-the-generated-dashboard.md) (a control
  beside sending and the contacts', behind a new setting).
* Revisit when the companion firmware advertises by itself, when a firmware release changes
  command 7 (the firmware tests of the next pin), or if the guards prove too strict in use
  (`messages_waiting`).
