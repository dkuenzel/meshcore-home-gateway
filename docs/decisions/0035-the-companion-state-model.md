---
status: proposed
date: 2026-11-15
decision-makers: maintainers
---

# The companion state model: presence, positions that age, and confirmed movement, in one module

## Context and Problem Statement

Today (2.2.0, `b02a3d8`) the gateway keeps four separate rules about a companion:

* **Heard.** `heard_at` follows adverts and messages at once. Telemetry answers alone move it at
  most once an hour (`HEARD_REFRESH_S`, `core/mesh.py:29`). Nothing says that a companion went
  silent.
* **Telemetry positions** expire after two unanswered requests in a row, or when the last answer
  is older than `max(1 h, 3 × interval)` ([ADR 0024](0024-telemetry-values-in-home-assistant-expire.md)).
  The tracker then turns `unavailable`, and the document keeps the values (`views.py:144-158`).
* **Advert positions** never expire by time (`views.py:166-171`, `docs/companions.md:204-206`).
  The radio's record keeps a position after its companion stopped sharing it: an advert without
  one leaves the old one in place (`src/helpers/BaseChatMesh.cpp:193-196` at
  `companion-v1.18.0`). [ADR 0025](0025-positions-in-adverts-checked-from-the-packet-log.md)
  errs to warn while the newest advert is unchecked. The documented way out is to remove the
  contact and add it again (`docs/companions.md:195-199`).
* **Movement** has two rules. The observer publishes a new telemetry fix from 20 m on
  (`FIX_MOVED_M`, `core/mesh.py:28`). The scheduler counts a companion as moving from 50 m on
  (`MOVED_M`, `core/telemetry.py:50`). Drift at rest beyond 20 m becomes a route on the map. The
  next request counts from the last request, not from the last report (`core/telemetry.py:492`).

Three issues of the 2.2.0rc2 release check meet here:

* #168: a companion flashed again and silent for days kept a position warning on *Overview*,
  twice and without an age.
* #184: the gateway should clear the old advert position itself; a position should expire when
  reports stop; the last known one should stay on the map with its age.
* #177: a companion at rest should draw no route; the interval should count from the last
  report, polled or pushed.

Their analyses drafted three state machines, three timers and three new entities per companion.
How does the gateway decide once whether a companion is around, where it is and whether it
moves, so that every front end shows one cause and one message?

## Decision Drivers

* Presence ([ADR 0024](0024-telemetry-values-in-home-assistant-expire.md)): a `person` never
  follows a position that nobody confirmed.
* One cause, one message (#168): the fact that matters now outranks older facts, and every fact
  shows its age.
* No old position comes back once a companion stopped sharing it
  ([ADR 0025](0025-positions-in-adverts-checked-from-the-packet-log.md)).
* Privacy: positions reach the gateway only, and the time a last known position stays visible is
  bounded ([ADR 0018](0018-telemetry-pulled-from-the-households-companions.md),
  [privacy.md](../privacy.md)).
* The radio: a write by the gateway never undoes a removal, an advert or a path the radio took in
  between, and `manage = "observe"` writes nothing
  ([ADR 0020](0020-managed-radio-profile-for-the-observer.md)).
* Airtime: the lanes and shares of [ADR 0018](0018-telemetry-pulled-from-the-households-companions.md)
  stay as they are.
* Home Assistant: a component is never conditional and never disabled by default (a disabled
  entity clears the whole device, [platform notes](../platform-notes.md#home-assistant-mqtt)); a
  `person` reads only device trackers.
* The public interfaces only grow ([ADR 0023](0023-public-interfaces-kept-by-a-snapshot.md)).
* DRY and KISS: one module, one entry point, one tick, no new timer.

## Considered Options

The model:

* One pure module with one entry point, fed with the precise time of every report and run by
  the observer's existing minute tick.
* One rule per issue, where each issue's data lives today: presence in the views, expiry in the
  observer, motion in the scheduler.
* Presence and ages in the dashboard only, from *Last heard* (the template rule of 2.2.1,
  extended).

The last known position:

* A sensor *Last known position* per companion, with coordinates only while the position is last
  known.
* The tracker kept available after expiry, with a forced state or a growing accuracy.
* A second `device_tracker` for the last position.

The clear of an old advert position:

* A fresh read of the one record and the write, in one hold of the command lock, by the contact
  manager's worker, with the radio's push order deciding.
* A write-back from the last table read (the plan of 2.2).
* A read after the write, compared with the write.
* `IMPORT_CONTACT` of the logged advert.

The position at rest:

* The reported fix where the companion came to rest.
* The mean of the fixes at rest.
* Every fix 20 m or more from the last published one (today).

## Decision Outcome

Chosen option: "one pure module with one entry point, run by the observer's minute tick", with
the *Last known position* sensor, the fresh read in one lock hold, and the reported fix at rest.
It is the only option that gives every front end the same states from one set of times, adds no
timer, and lets new sources and consumers join without new rules.

### The module

* **One pure, total module**, `core/companion.py`, keeps one record per contact of the radio.
  The observer, the poller and the contact manager feed it; the views read it.
* **One entry point**, `reported(key, at, source)`, takes every report with the gateway's
  precise time. The sources: `advert` (a pushed `ADVERT`, and a newer `last_advert` in the first
  table read of a run, at the advert's own time when it lies within the stop and not in the
  future), `message` (its `received_at`), `telemetry` (an accepted answer, with or without a fix),
  `path` (`PATH_UPDATED`, except the path of the gateway's own flood). `pushed` joins with a
  push subscription (see *Open decisions*).
* **Facts it reads, never owns**: the telemetry state and interval of the scheduler
  (ADR 0024 unchanged), the advert check (ADR 0025), whether the radio is live, and the
  configuration.
* **Outputs**, each orthogonal to the others: `presence`, `position_state` and `motion`.
  `telemetry_state` stays the scheduler's. Membership stays with the contact manager: a removed
  contact has no document (as today); forgotten (#166) and replaced (#167) contacts are events,
  not presence values.
* **One tick.** The observer's minute tick (`observer.py:536`, called by the bridge's status loop
  every 60 s, `bridge.py:76`, `:356`) recomputes every contact and publishes the documents that
  changed. Telemetry outcomes arrive through the poller's step, as today. No new timer.
* **Ages, not times, across a restart.** The module saves its ages in the state file, as the
  schedule does (ADR 0024): a restart neither expires nor revives anything. An age below zero
  (the clock went back) counts as zero. The state file holds no position beyond what it holds
  today (a current companion's kept document).

### Presence (#168)

| `presence` | Condition | Asked companion | Other contact |
| --- | --- | --- | --- |
| `heard` | silent for less than W, and, if asked, fewer than two unanswered requests in a row since its last report | W = `max(1 h, 3 × its interval)`, ADR 0024's bound | W = 24 h |
| `quiet` | not `heard`, silent for less than L | L = 24 h, the telemetry day | L = 7 days |
| `long_quiet` | silent for L or longer | | |
| `never` | no report since it became a contact | | |

* **Asked** means a candidate the poller asks now. While telemetry is off or paused, the gateway
  asks nobody, and every contact counts as not asked.
* **Silence counts only while the gateway listens.** The tick adds the elapsed time to each
  contact's silence while the radio is live. Time with the gateway stopped or its radio away is
  not silence: the gateway heard nothing, so it says nothing about the companion. A newer
  `last_advert` in the first table read after a stop is a report (above). This keeps the rule of
  2.2.1 ("after a start each companion has a day to be heard", decided for the patch) in the
  gateway, and makes it exact.
* **Thresholds are fixed.** They reuse ADR 0024's bound and the telemetry day; there is no new
  setting.
* `heard_at` keeps its meaning and its hourly step while answers come. It also follows a path
  update at once, with `heard_via` `path`: a path the companion sent is hearing from it. When a
  companion turns `quiet`, `heard_at` catches up to its last report. So the time shown beside a
  silence is exact, and *Last heard* writes one more recorder row per silence, not one per
  answer.

### Positions (#184)

Every published position has a **report time**, `position_reported_at`: the newest telemetry
answer that carried a fix, or the newest heard advert that carried the position (a new key,
`advert_heard_at`, the gateway's time of the newest advert it heard). Before the gateway heard
any advert, the fix's own time stands in (`Fix.at`, `core/mesh.py:231-251`).

| `position_state` | Condition | Map |
| --- | --- | --- |
| `current` | telemetry: current by ADR 0024, and reported less than 2 × the interval ago; a companion's advert: reported less than 1 h ago | the tracker's marker |
| `ageing` | telemetry: still current by ADR 0024, but no fix reported for 2 × the interval or longer | the tracker's marker; lists show the age in grey |
| `last_known` | not current because reports stopped (silence), and reported less than the horizon ago | a blue-grey clock marker at the last position |
| `fixed` | the advert position of a repeater, room server or sensor: a site, without expiry (as today) | the tracker's marker |
| `null` | withdrawn, older than the horizon, or no position | none |

* **Silence ages a position.** It is ADR 0024's rule for telemetry (two unanswered requests, the
  time bound, telemetry paused, the radio away), and 1 h after its report for a companion's
  advert position (`CURRENT_MIN_S`, `core/telemetry.py:74`). The document keeps the coordinates
  while the position is `last_known`.
* **Withdrawal ends a position at once, without a last known one.** Withdrawal means a checked
  advert without a position; answers that are current but carry no fix for more than an hour
  (`no_position`, `LOCATED_S`, `core/telemetry.py:76`); an exclusion from telemetry; a removal
  from the radio; or telemetry switched off in the configuration. The document's coordinates
  become `null`. The history keeps the trace for its retention.
* **The horizon** is `[telemetry] last_known_days`, 7 by default, 0 to 30. 0 shows no last known
  position: silence then ends a position as withdrawal does. With the history on, the horizon is
  never longer than `[history] positions_days`. Without the history, a last known position lives
  in memory until the next restart, as today's expired values do.
* **Which clock counts.** Currency follows ADR 0024's ages: a stopped gateway ages nothing, a radio
  that is away does (a `person` must not stay `home` on a position nobody confirms). The horizon is
  a retention and follows the wall clock, as the history's purge does. Every time shown is a wall
  time.

### Movement (#177)

One movement rule replaces the two of today (`FIX_MOVED_M`, `MOVED_M`). It applies to telemetry
fixes only; advert positions follow ADR 0025.

* **At rest** the published position is the reported fix where the companion came to rest: the
  last fix of its trip, with the accuracy of 15 m (`TELEMETRY_ACCURACY_M`, `views.py:28`). It is
  not a mean. Each answer with a fix refreshes the report time, never the coordinates.
* **An excursion** is a fix farther than the rest radius R from the rest fix. R is
  `[telemetry] rest_radius_m`, 50 m by default (today's `MOVED_M`), 25 to 500. The gateway then
  asks once more after the moving interval (a probe, inside the lanes).
* **A probe within R drops the excursion.** It is neither published nor stored.
* **A probe beyond R confirms the move.** The excursion fix and the probe fix are published, so
  the trip starts where the move was seen. Every fix of the trip is then published, and only the
  fixes of a trip draw a line.
* **A trip ends** with two answers within R of each other (today's `STILL_POLLS`). The interval
  then doubles from the moving interval up to the rest interval (90 s, 3, 6, 12, 15 min next to
  the gateway), instead of jumping.
* **The interval counts from the last report**, polled or pushed: `due = last report + interval`.
  An unanswered request keeps its back-off from the request. Adverts, messages and paths end a
  back-off, as today (`core/telemetry.py:340-356`), and never postpone a request.
* **Published**: `motion` (`resting` or `moving`, `null` for a contact without a telemetry
  position) and `motion_since`. A probe counts as resting and the settling as moving: the inner
  states stay private, and a later value is a compatible addition.
* **Restart**: the class is saved as today; the rest fix is seeded from the kept document's
  position (in the state file only while current, as today). No new position is stored.
* **Hints** (a path change, a message over another path) are an extension point of the model,
  off in this increment.

### The automatic clear (#184)

The radio's record keeps no position that its newest checked advert lacks. The firmware has no
command that changes only the position: `ADD_UPDATE_CONTACT` (9) writes the whole record, and
adds a key that is not in the table (`MyMesh.cpp:194-217`, `:1328-1348` at `companion-v1.18.0`).
So the clear is designed against the race.

* **When**, all of:
  1. the record holds a position;
  2. the gateway checked the record's newest advert (ADR 0025), and it carries none;
  3. the radio is connected, and the profile writes: tested firmware with `manage = "auto"`, or
     `"always"`; never with `"observe"`;
  4. no push for this key in the last 10 s, one attempt per contact and advert, one clear per
     contact per 10 minutes.

  It is triggered where the observer logs "its advert carries no position (the radio's record
  keeps an older one)" (`observer.py:723-733`), and by a sweep when the session becomes live.
  Only companions qualify: they are the only type whose adverts are checked
  (`core/mesh.py:24-25`).
* **Who.** The observer queues an internal job for the contact manager. Its single worker takes
  jobs one at a time (`contact_manager.py:329-347`), so a clear never overlaps an add or a
  removal by the gateway. The worker sees the session only after the profile's own contact
  writes at connect (ADR 0020).
* **How, in one hold of the session's command lock:**
  1. `GET_CONTACT_BY_KEY` (30) reads that one record as the radio has it now
     (`MyMesh.cpp:1371-1377`). `ERR_NOT_FOUND` ends the clear: the contact is gone. The answer's
     frame number is s0 (`radio/session.py:766-778` numbers every frame).
  2. The fresh record must hold a position and the checked `last_advert`; otherwise a newer
     advert came, and its own check decides later.
  3. `ADD_UPDATE_CONTACT` with the record's own bytes 0–134 and eight zero bytes: the position
     0, 0 and no `lastmod`, so the radio stamps the change. The write is withdrawn, unsent, when
     a push for this key (`ADVERT`, `PATH_UPDATED`, `CONTACT_DELETED`) arrived after s0
     (`_command(..., unless=...)`, `radio/session.py:847-875`); it is tried once more after 10 s.
  4. Its `OK` is frame s1. The firmware writes pushes and answers to one link in the order it
     handles them (`MyMesh::loop`, `MyMesh.cpp:2415-2434`; pushes written at once, `:342-411`)
     *(inferred from the code; a firmware test confirms it)*. So a push for this key between s0
     and s1 is a change the radio made between the read and the write. The gateway then undoes
     its own effect: `CONTACT_DELETED` → `REMOVE_CONTACT`; `PATH_UPDATED` → `RESET_PATH`;
     `ADVERT` → that newer advert counts as not checked, which errs to warn (ADR 0025) until the
     next advert. It never writes logged data back.
* **Then** the table read that every contact write triggers shows the record without a position.
  The observer publishes; the `contact` event `position_cleared` (key, prefix and name, never a
  coordinate) and one info line follow. The name is the one the observer keeps, canonical by
  default ([ADR 0034](0034-untrusted-text-from-the-mesh.md)).
* **Bounds.** A clear takes a token of the contact manager's radio-work budget
  (`contact_manager.py:70`, `:356`), and only while more than half of the burst is left, so the
  household's requests come first. It waits for the next tick otherwise.
* **Visibility.** The `settings` document lists the contacts whose record still holds a position
  their newest checked advert lacks (`contacts_old_advert_position`, `…_total`): waiting, failed,
  or not written under `observe`. `probe --list profile` gains the line kind
  `old_advert_position`.
* **What others see.** The radio writes its contacts file within 5 s (`LAZY_CONTACTS_WRITE_DELAY`,
  `MyMesh.cpp:112`). Its stored advert is the newest, which carries no position either
  (`BaseChatMesh.cpp:190`). Nothing is transmitted. The next advert with a position brings it
  back, and the check warns.
* **The codec** gains `get_contact_by_key` and `contact_without_position(record)`. Both work on
  a record the radio sent; the codec gets no general contact writer, so ADR 0020's deny list keeps
  its meaning.

### The advert check survives radios that clear withdrawn positions (G0)

A proposed firmware change (U1, see *Open decisions*) makes an advert without a position clear
the stored one. With it, `from_record` (`core/adverts.py:86-93`) reads a record that turned to
0, 0 as proof of a flagged advert, and `outcome` lets that evidence win over the adjacent log
(`core/adverts.py:133`). The warning then never ends: 3 of 9 advert tests fail against such a
build, 2 of them for this reason (the harness run of 2026-11-15 in the analysis on #189).

* A record that turned to **none** stops being evidence on its own when an adjacent log exists:
  the adjacent log decides, which at receive delay 0 is never a forged copy (ADR 0025).
* A record that turned to **another position** stays evidence: both firmwares need the flag for
  it.
* Without a log, a change to none still errs to warn.
* The gateway's own clear writes 0, 0 without a newer advert, so `from_record` ignores it
  (`core/adverts.py:89`).
* The firmware tests build a second, patched companion as a variant in
  `tests/firmware/sources.toml`, pinned like the first
  ([ADR 0026](0026-gateway-tested-against-the-real-firmware-built-natively.md)).

G0 needs no detection of the firmware and changes nothing on stock firmware. It must merge before
the gateway's radio runs a firmware with U1, from a fork or from an upstream release.

### Home Assistant

| Entity | Change |
| --- | --- |
| *Heard* (`sensor`, new; unique ID `…_presence`, entity ID `sensor.mesh_<slug>_heard`) | device class `enum`, options `heard`, `quiet`, `long_quiet`, `never`; state `presence`; no entity category; unknown for a document of an older version |
| *Last known position* (`sensor`, new; unique ID `…_last_position`, entity ID `sensor.mesh_<slug>_last_position`) | device class `timestamp`, icon `mdi:map-clock-outline`, no entity category; state `position_reported_at` while the position is `ageing` or `last_known`, else unknown; the attributes `latitude`, `longitude`, `gps_accuracy` (telemetry only) and `position_source` only while `last_known` |
| *Position* (`device_tracker`) | available while `position_state` is `current`, `ageing` or `fixed`; a document without the key keeps today's rule (`position_current is not false`, `mqtt/contact_devices.py:119-123`); new attributes `motion` and `motion_since` |
| *Position in adverts* (`binary_sensor`) | new attribute `advert_heard_at`, so a warning can say how old the advert is |
| *Last heard* | unchanged; exact once the companion is quiet (above) |
| *Position in adverts* on the gateway's device | new attributes `heard` and `heard_total`: the companions that carry their position and were heard within the last day |

* **Announced for every companion and every contact of a type the gateway asks, enabled.** They
  come with the device, are never disabled by default, and stay once announced (the keep rule,
  `mqtt/contact_devices.py:239-242`). The tracker of every such contact is announced from the
  start too, in the same pre-release (#169 step 1); today it appears with the first position
  (`mqtt/contact_devices.py:417`).
* **At most one entity of a contact carries coordinates at a time.** The tracker carries them in
  `current`, `ageing` and `fixed`; the sensor only in `last_known`. An unavailable tracker carries
  no position attributes (core `helpers/entity.py:1124-1130`, 2026.10.2). The rule of
  `mqtt/contact_devices.py:12-14` becomes "at most one entity per contact at a time".
* **A sensor never moves a `person`**: a `person` reads only `device_tracker` entities and skips
  unavailable ones (core `components/person/__init__.py:85`, `:93`, 2026.10.2). So the presence
  trap of ADR 0024 stays closed.
* **The last known sensor shows its age without a state per minute**: Home Assistant renders a
  timestamp relative ("3 hours ago") in rows, tiles and more-info.
* **Motion gets no entity of its own** (the maintainer's decision): it is a document key and two
  tracker attributes. Its changes fall on fixes that are published anyway, so they add few
  recorder rows.
* **Not exposed to Assist by default**: enum and timestamp sensors are not among the domains and
  classes Home Assistant exposes by default (`homeassistant/exposed_entities.py`, 2026.11.0).
* **Recorder**: *Heard* writes one row per change of presence; *Last known position* about five
  rows per absence; nothing per answer.
* **Homie**: each contact's node gains `presence` and `position-state` (enums with the documents'
  values). `position` keeps the coordinates while the document has them.

### What the dashboard shows

**One cause, one message.** Every card that names a companion reads its state through one macro.
The highest state below is the one message; the facts below it show muted, with their age, and
raise no problem.

| Precedence | State | The row says |
| --- | --- | --- |
| 1 | the gateway offline, or its radio away | one line at the top ("The radio is away since 09:05"); no companion row raises a problem |
| 2 | a former contact (on its chat page) | "No longer a contact of this radio" |
| 3 | `long_quiet` | "Not heard for 5 days" |
| 4 | `never` | "Not heard yet" |
| 5 | `quiet` | "Not heard for 3 h" |
| 6 | the facts: advert position, battery, position state | each with its age (below) |
| 7 | `heard` | the facts as today |

* **The advert fact.** It is a problem (amber, with the advert's age and the way out) while the
  companion was heard within the last day. Otherwise it is a grey dated line in the details. The
  *Overview* problem "a position broadcast to everyone" reads the gateway's new `heard` attribute,
  not a template rule. This is 2.2.1's decided rule, now decided by the gateway.
* **The status line names; the row explains.** The status line says "Needs a look: Bob's position
  is public". The row carries the mark, the advert's age and the action. No card repeats the
  status line's sentence.
* **Positions in lists** (*Where everyone is* on *Overview* and *Map*):
  * `current`: the place, "since 10:31";
  * `ageing`: the place, then grey "position from 10:31";
  * `last_known`: grey `mdi:map-clock-outline` and "last known 3 hours ago", with the zone's name
    when it lies in a zone;
  * `null`: "no position", with the reason in the fold (GPS off, location not allowed,
    excluded).
* **The map.** The generated map lists each companion's *Last known position* beside its tracker,
  with `color: blue-grey` and `label_mode: icon`. `blue-grey` (#607d8b) is the one theme colour
  measured at 3:1 or more against white, near-black and both assumed map land tones (4.37, 4.32,
  3.81 and 3.65, computed for #184's analysis; WCAG 1.4.11). The browser check measures it against
  the real tiles. The tracker's trail leads to it. Fewer lines appear, because only trips draw.
* **Words beside every icon** (WCAG 1.4.1), rows in two columns at 360 px, every new text in the
  catalogs.
* **Texts that change.** "A device whose position is not current has no marker, only its line"
  becomes "A device not heard from shows where it was last, in blue-grey with a clock, for 7
  days". The advert card drops "remove its contact and add it again": the gateway clears the
  record itself.
* **What waits for the redesign** (2.4, #170): the marks on every card, and the *now* map without
  lines and the *trips* map. 2.3 changes the cards that show these facts today.

### Public interfaces

| Interface | Change | Compatible |
| --- | --- | --- |
| MQTT, `contacts/<key>` | new keys `presence`, `position_state`, `position_reported_at`, `advert_heard_at`, `motion`, `motion_since`; a new value `path` of `heard_via` | yes: new keys and a new value |
| MQTT, `contacts` | new keys `advert_carries_position_heard`, `…_heard_total` | yes |
| MQTT, `contact` event | new `event_type` `position_cleared` | yes: a new value |
| MQTT, `settings` | new keys `contacts_old_advert_position`, `…_total` | yes |
| MQTT, `config` | new keys `telemetry.rest_radius_m`, `telemetry.last_known_days` | yes |
| MQTT, Homie | new properties `presence`, `position-state` | yes |
| Configuration | new optional `[telemetry] rest_radius_m` (50, 25–500) and `last_known_days` (7, 0–30) | yes: new optional settings |
| Command line | new line kind `old_advert_position` in `probe --list profile` | yes |
| Home Assistant | two new entities per contact; new attributes of the tracker and of both *Position in adverts* sensors | yes: new entities and attribute keys |

No key, value, entity or option is removed or renamed, and no key changes its type or meaning.
These behaviours change, listed under *Changed*:

* A companion's advert position expires 1 h after its report; a `person` that relied on it turns
  `unknown` after that hour.
* A withdrawn position, and one older than the horizon, is `null` in the document. The keys keep
  their meaning: `latitude` is "its newest known position", and the gateway no longer knows one.
  This narrows ADR 0024's "the document keeps it" to silence.
* `position_at` keeps its meaning, "when the position last changed"; it changes with a confirmed
  move instead of every 20 m.
* `position_current` keeps every value and its meaning; `null` still means "no position, or one
  from adverts". The documentation's "which does not expire by time" goes; `position_state` tells
  whether an advert position is current.
* `heard_at` follows path updates (`heard_via` `path`), and catches up to the last report when a
  companion turns quiet.
* The gateway writes contact records under the managed profile without a request.

`make interface` records the additions. The behaviours are checked by tests, not by the snapshot.

### Consequences

* Good, because a silent companion says so, with its age, and its old facts stop raising
  problems: one cause, one message on every card.
* Good, because a companion that stops answering leaves the map by itself and stays visible,
  dated, at its last place for up to 7 days.
* Good, because a withdrawn position never comes back, and the workaround "remove the contact and
  add it again" goes.
* Good, because a companion that stopped sharing no longer warns after an advert the gateway
  missed: the record now says what its newest advert carried, so ADR 0025's fallback becomes
  exact.
* Good, because a resting companion draws no route: in a simulation with assumed noise, 1 to 2
  points a day instead of about 24 to 61 (#177's analysis).
* Good, because one module serves the later consumers without new rules: *Enrolling*'s
  "answering" check (2.3a4), #167's replace candidate ("heard after OLD's last report"), #188's
  "heard again", and pushed reports.
* Bad, because the gateway now writes contact records on its own initiative, under the managed
  profile. `manage = "observe"` is the only way to stop it.
* Bad, because every Home Assistant user sees each companion's presence, and its last known
  position for up to 7 days (T7).
* Bad, because a confirmed move shows one probe later: 90 s next to the gateway, up to about
  5 minutes over three repeaters. A zone change waits as long.
* Bad, because an unconfirmed excursion is data the gateway received and drops. A companion that
  leaves range right after one far fix shows its last known position where it rested.
* Bad, because a companion's advert position is current for an hour only. A household that shares
  positions in adverts on purpose sees them turn last known.
* Bad, because two new entities per companion add to the device page and the entity list.
* Neutral, because a push lost on a TCP link during the few milliseconds of a clear can leave the
  older advert's fields in the record until the next advert. It is the class of ADR 0025's
  "an advert the gateway could not check".

### Trust boundaries

* **B1, the air.** A contact outside the household (T3) can make the gateway clear its own
  record's position, at most once per 10 minutes, by an advert without one. A stranger (T1)
  cannot: only checked adverts of a contact count. The clear only erases a position from a
  record, and never writes one. A forged unflagged copy decides only in ADR 0025's residual case,
  and G0 keeps the adjacent log deciding. Nothing new is transmitted.
* **B2, the radio's link.** The gateway sends one new read (30) and a contact write (9) outside
  connect, under ADR 0020's firmware gate. It trusts the firmware's push order; a firmware test
  pins it.
* **B3, the broker.** The documents gain keys. No new request is accepted.
* **B4 and B5, Home Assistant and its users.** Presence and last known positions reach every
  user (T7) and the recorder (T8), and not Assist by default. `privacy.md` names how long.
* **B6, the container.** The state file gains ages, never a position. The history gets fewer rows
  at rest, and never an excursion.
* **B7, the project.** Not touched.

The threat model's rows T3 (a bounded clear of its own record) and T7 (presence, and last known
positions for up to 7 days) change in the same pull request.

### Confirmation

* **Pure units** of `core/companion.py`: every transition by time, report and failure; asked and
  not asked; listening and deaf time; restored ages; the clock going back; an interval change.
  Properties (hypothesis): without a new report the states never move towards `heard` or
  `current`; a report with a fix always gives `current`; a withdrawal always gives `null`;
  `last_known` never follows a withdrawal; noise within R never changes the published fix; an
  unconfirmed excursion is never published or stored.
* **The scheduler**: probe timing, the settle doubling, the base on the last report, the lanes
  still bounding, a restart.
* **Codec**: golden frames for command 30 and for the cleared record (bytes 135–142 zero, no
  `lastmod`).
* **Session** on a fake transport: a push before the write withdraws it; a push between write and
  `OK` gives `REMOVE_CONTACT`, `RESET_PATH` or an unchecked advert; no other command runs between
  read and write.
* **Contact manager**: a removal queued during a clear runs after it; a clear of a removed
  contact writes nothing; `observe` writes nothing; the budget's half.
* **Advert check (G0)**: the rule table on both firmware behaviours; the harness suites against
  the stock and the patched companion.
* **Emulated radio** (`tests/radio_emulator.py` gains command 30): share, stop, checked advert,
  record 0, 0, one `position_cleared`; the sweep after a restart; states over a set clock in the
  documents and retained topics.
* **Real firmware** (ADR 0026): command 30 answers the record; the cleared record survives a
  reboot; `lastmod` moves; an advert delivered while the write is queued arrives before the `OK`.
* **Home Assistant contract tests** on 2026.10.2 and 2026.11.0: the tracker unavailable and the
  sensor with coordinates in `last_known`; a `person` follows only `current` and `ageing`; a 2.2
  document keeps today's behaviour; an advert-only tracker turns unavailable after an hour; the
  map card with `color: blue-grey` and `label_mode: icon` loads on both. Both options and the icon
  `mdi:map-clock-outline` are in the frontends 20260930.4 and 20261028.4 (a string check of the
  bundles, 2026-11-15).
* **Dashboard rules**: every card naming a companion reads its state; no card repeats the status
  line; every icon has a word; mutants.
* **Browser check** in every language at 360×800 and 1280×800, light and dark: the blue-grey
  marker at 3:1 against the real map tiles, or another theme colour.
* **Hardware checklist**: stop sharing, advert nearby, see `position_cleared`; walk out of range
  and watch `ageing`, `last_known` and the `person`; switch GPS off and see the position end
  without a marker; 24 h at rest indoors draws one point.

## Pros and Cons of the Options

### One module, one entry point, the existing tick

* Good, because one set of times decides every state, and every front end reads the same result.
* Good, because new sources (`pushed`) and consumers join through the entry point, not new rules.
* Bad, because the module grows with each consumer, so its tables must stay the only place a rule
  lives.

### One rule per issue

* Good, because each change stays where its issue's code is today.
* Bad, because it builds three timers, three entities and three ADRs over the same times, and
  their thresholds drift apart.

### The dashboard alone

* Good, because it needs no gateway change; it is the template rule decided for 2.2.1.
* Bad, because only Home Assistant gets it, `heard_at` moves hourly, and a template cannot clear
  a record or stop a route.

### A *Last known position* sensor

* Good, because a `person` can never follow it, and the map shows it without a custom card.
* Bad, because it is one entity more per companion.

### The tracker kept available after expiry

* Good, because it adds no entity.
* Bad, because a `person` follows it (the presence trap), and a growing accuracy widens zones
  (core `components/device_tracker/entity.py:362-390`, 2026.10.2).

### A second tracker

* Good, because the map shows it.
* Bad, because a user can add it to a `person`, and it gets zone states.

### The fresh read in one lock hold, with the push order

* Good, because it closes the removal and profile races by design and detects the advert and
  path races exactly.
* Bad, because it adds a read command and a two-command transaction to the session.

### A write-back from the last table read

* Good, because it is simple.
* Bad, because a removal in between brings the contact back with its old flags, and a newer
  advert or path in between is overwritten.

### A read after the write

* Good, because it is simple.
* Bad, because the write already erased what an advert in between brought.

### `IMPORT_CONTACT` of the logged advert

* Good, because the radio checks the signature.
* Bad, because it imports a raw packet (ADR 0020's deny list in spirit), and a radio with client
  repeat on floods it again.

### The reported fix at rest

* Good, because a position stays what the companion reported (`docs/mqtt-api.md`, contact
  document), and the 15 m accuracy keeps zones as they are.
* Bad, because a single fix at rest carries its own error.

### The mean at rest

* Good, because a mean of fixes minutes apart lowers the error.
* Bad, because a computed mean is not what the companion reported, which arguably changes what a
  position means.

### Every 20 m (today)

* Good, because nothing changes.
* Bad, because drift becomes routes, and two movement rules disagree.

## More Information

### Amended records

Each carries a dated amendment line of 2026-11-15 that names this record as a proposal for
2.3.0a2:

* [ADR 0018](0018-telemetry-pulled-from-the-households-companions.md): the interval counts from
  the last report; one movement rule (50 m, confirmed by a probe) decides both the class and what
  is published.
* [ADR 0020](0020-managed-radio-profile-for-the-observer.md): a new enforced row, "contacts keep
  no position their newest checked advert lacks", written outside connect by the contact
  manager's worker under the same firmware gate.
* [ADR 0024](0024-telemetry-values-in-home-assistant-expire.md): its bound is also the end of
  `heard` for an asked companion; a companion's advert position expires 1 h after its report;
  the document keeps an expired position only through silence, up to the horizon; the *Last known
  position* sensor.
* [ADR 0025](0025-positions-in-adverts-checked-from-the-packet-log.md): advert positions expire by
  time; the clear makes the fallback exact; a record that turned to none is no evidence when an
  adjacent log exists (G0).

### Decisions

Decided by the maintainer on 2026-11-15, as recommended in the analyses on #168, #177 and #184
and in the 2.3 plan, whose decisions
[design §13.1](../design.md#131-release-23-the-foundations-and-updates-that-ask) numbers:

| Decision | Outcome |
| --- | --- |
| The gateway clears old advert positions itself (#184) | yes, under the managed profile only, race-free as above; no extra setting |
| Withdrawal ends a position at once (#184) | yes, without a last known marker |
| The horizon (#184) | 7 days, `last_known_days` 0–30; every Home Assistant user sees last known positions |
| Advert positions expire (#184) | after 1 h |
| Infrastructure positions (#184) | `fixed` |
| The rare advert race (#184) | errs to warn until the next checked advert |
| The quieter *Overview* warning (#168) | a companion not heard for a day leaves it (2.2.1), now decided by the gateway |
| Presence thresholds (#168) | fixed, from ADR 0024 and the telemetry day |
| New entities (#168, #177, #184) | *Heard* and *Last known position*, enabled, for every companion; motion as a key only |
| The motion model (#177) | 50 m; a move confirmed before it is published; the reported fix at rest; excursions dropped; the lanes kept; a *now* and a *trips* map in 2.4 |
| The release shape (decision 1 of the 2.3 plan) | 2.3.0a2, after 2.3.0a1's text module and before *Enrolling* (2.3.0a4), which reads `presence` |

### Open decisions

Two decisions of the release plan for 2.3 are still open
([design §13.1](../design.md#131-release-23-the-foundations-and-updates-that-ask)). This record
depends on neither.

* **Household overrides** (decision 2 of the 2.3 plan) is recorded in
  [ADR 0036](0036-the-settings-registry-recommendations-and-the-update-review.md), which owns the
  settings. Until it is decided, `rest_radius_m` and `last_known_days` are file settings that the
  administrator sets.
* **The upstream firmware route** (#177, #189; decision 10 of the 2.3 plan). Recommended: three
  small contributions in order: U1 the advert clear, U3 push subscriptions after an issue, U4 a motion
  wake later. G0 lands in 2.3 whatever is decided: it is right on stock firmware, and it is needed
  before the gateway's radio runs any firmware with U1. The source `pushed` and a subscription
  manager wait for a protocol shape upstream agrees to. Polling stays the default.

### Revisit when

* the hardware checklist or a week of use shows W, L, the hour or the 7 days wrong in a busy
  mesh;
* MeshCore releases U1: the clear then never fires on that firmware, and stays for older radios
  until the firmware floor passes it;
* MeshCore offers pushed telemetry for companions (ADR 0018's own revisit clause);
* the firmware gains a command that writes only the position, or reports an advert's content;
* noise measurements show 50 m too small or too large, or the telemetry carries a fix age or an
  accuracy;
* Home Assistant changes how a `person`, the map's entity options or availability work.

### Sources

The gateway at `b02a3d8` (2.2.0): `core/mesh.py`, `core/telemetry.py`, `core/adverts.py`,
`views.py`, `observer.py`, `bridge.py`, `contact_manager.py`, `radio/session.py`,
`protocol/codec.py`, `mqtt/contact_devices.py`, `mqtt/homie.py`, `dashboard.yaml`;
`docs/companions.md`, `docs/mqtt-api.md`, `docs/development.md` (compatibility),
`docs/platform-notes.md`. MeshCore `companion-v1.18.0` (`81dbae5`):
`examples/companion_radio/MyMesh.cpp`, `src/helpers/BaseChatMesh.cpp`. Home Assistant core
2026.10.2 and 2026.11.0, frontends 20260930.4 and 20261028.4. The analyses and the maintainer's
decisions on #168, #177, #184 and #189 (2026-11-15), which cite the practice of OwnTracks,
Traccar, Meshtastic, Zigbee2MQTT and ZHA.

Related: [ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md),
[ADR 0017](0017-contacts-on-the-radio-each-with-its-own-device.md),
[ADR 0029](0029-what-a-contact-is-trusted-with.md),
[ADR 0031](0031-household-chat-problems-and-settings-on-the-generated-dashboard.md).
