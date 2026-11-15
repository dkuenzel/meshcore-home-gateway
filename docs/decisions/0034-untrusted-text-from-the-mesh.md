---
status: proposed
date: 2026-11-15
decision-makers: maintainers
---

# Untrusted text from the mesh, canonical where it enters and encoded at every sink

## Context and Problem Statement

Other people choose the names and texts that come from the mesh. Anyone in range gives a radio
any name, and the gateway keeps it for every heard node
([ADR 0032](0032-heard-nodes-kept-with-name-signal-and-time-by-default.md)). A contact renames
itself with every newer advert: the firmware copies the advert's name over the stored one
(`src/helpers/BaseChatMesh.cpp:191`; every firmware line here is at companion-v1.18.0). Anyone
with a channel's secret writes any name before a channel text (`core/model.py:118-127`; every
line of the gateway here is at 2.2.0, `b02a3d8`). The
firmware checks neither the bytes nor UTF-8 of a text (`examples/companion_radio/MyMesh.cpp:461,
577`: "TODO: UTF-8 ??").

The gateway hands these strings to sinks it does not control: Home Assistant's own pages (device
names, attributes, the logbook, the more-info dialog), Homie, terminals that read the broker, the
companion apps, and a language model behind Assist. Up to 2.2 the design keeps text as it came and
lets consumers escape it. `[messages] clean_received`, off by default, is the opt-in for consumers
that cannot (design §11.1 of 2.2, SEC-017).

An audit of #179 ran a hostile corpus (84 strings, 8 raw byte strings) through the real code of
2.2.0rc2, through Home Assistant 2026.11.0 and through the Markdown pipeline of the frontend it pins
(20261028.4). A second pass verified each finding. It found no injection path: no mesh string
becomes a topic, an ID, a template, YAML, HTML, Markdown, SQL, a file name or a terminal escape
where the gateway writes it. What reaches the household is look and bytes. With the default,
control, escape and direction characters reach Home Assistant's device names, the JSON documents
and Homie's `$name`. A right-to-left override makes a contact read as another contact in Home
Assistant, without the key digits that a taken name gets.

The rules for such text live in five places today:

* `core/text.py:199-231`: `_clean` and `_DIRECTION` for outgoing texts, reused by
  `clean_received` and `clean_name`;
* `core/contacts.py:120-225`: `_UNSAFE`, `INVISIBLE`, `COPY_INVISIBLE`, the look-alike table of
  `name_key` and the labels' `_shown`;
* `cli/common.py:83-109`: `_safe_text`, `_json_text` and their own `_UNSAFE`;
* the macro `m()` of `dashboard.yaml`, written out in each of the 43 Markdown cards that print a
  value (line 18 and its copies); the test of rule 19 ties it to `COPY_INVISIBLE`;
* `cli/history.py:28, 131-141`: the CSV formula guard.

Each new sink adds a place. 2.3 and 2.4 add several: the heard-node page, the replace fold,
*Enrolling*'s list of new devices, findings that name a contact, and the maintenance port's card
with the app name a client sends. Where does the gateway make untrusted text safe for every sink,
what does it keep, and what is the default?

## Decision Drivers

* No text from the mesh becomes code or markup in any sink, whoever sent it (OWASP's contextual
  output encoding).
* The sinks the gateway does not control receive a bounded repertoire: no control, escape or
  direction characters. MQTT 5.0 says that UTF-8 strings should not include U+0001–U+001F and
  U+007F–U+009F (§1.5.4); Homie requires UTF-8 without a byte order mark.
* Legitimate content stays readable: emoji sequences (UTS #51), every script with its marks,
  right-to-left letters. #179 asks for it.
* A name that reads like another one is marked, also for the look-alikes a household meets
  (`AIice`, `A1ice`, `B0b`).
* One definition, so that the dashboard, Home Assistant's pages, the command line and the
  documents show the same name, and a new sink cannot miss a rule.
* A setting keeps its meaning within a major version
  ([compatibility](../development.md#compatibility)).
* Checks that cannot go stale unnoticed. The manual check of the frontend's pipeline still
  rebuilds frontend 20260930.2 (`tests/manual/markdown_pipeline.mjs:3`); Home Assistant 2026.11.0
  pins 20261028.4 (`homeassistant/components/frontend/manifest.json`).

## Considered Options

* One module: text canonical where it enters the core, by default; every sink encodes as today
* Today's split: every sink encodes; `clean_received` stays an opt-in
* Cleaning at each sink that leaves the gateway, instead of at the entry
* A strict repertoire: ASCII names, or the identifier profile of UTS #39
* Names canonical whatever `clean_received` says, already in 2.3

## Decision Outcome

Chosen option: "one module", because it is the only one that bounds what every sink receives,
keeps emoji and every script, and keeps the meaning of `clean_received = false` until 3.0.

### The module

`core/untrusted.py` is pure and uses the standard library only. It holds every rule for text the
gateway did not write: names and texts from the mesh, and texts and contact links from the bus.
Sinks take such text only through it. The sets of the five places above move into it, and their
users import them from there.

| Part | What it does |
| --- | --- |
| `canonical_text(text)` | A message text as the gateway publishes and keeps it: the repertoire below. Idempotent, never longer. |
| `canonical_name(name)` | `canonical_text` on one line: line breaks as spaces, runs of spaces as one, no space at either end. |
| `shown(text)` | A line for a terminal, a CSV cell or a label: each control, format or separator character left is a space, except a joiner inside a sequence and the tags of a subdivision flag. |
| `name_key(name)` | A name as it is compared with others, to mark a taken name (SEC-007). Moves here from `core/contacts.py`. |
| `csv_cell(text)` | `shown`, then the formula guard. |
| Named classes | `DROPPED`, `LINE_BREAKS`, `KEPT_JOINERS`, `INVISIBLE`, `COPY_INVISIBLE`, `MARK_CAP`. The dashboard's `m()`, contact links (`protocol/links.py:27`) and the tests read them here. |

### The repertoire

`canonical_text`:

* drops the C0 controls except the line feed, DEL and the C1 controls. A tab becomes a space, CR
  and CR LF a line feed, as `_clean` does today (`core/text.py:208-217`);
* turns VT, FF, NEL, U+2028 and U+2029 into a line feed (`canonical_name`: a space);
* drops the nine explicit direction formatting characters, U+202A–U+202E and U+2066–U+2069
  (UAX #9; the set of Trojan Source, CVE-2021-42574), the byte order mark U+FEFF, and the
  interlinear annotation characters U+FFF9–U+FFFB;
* drops the tag characters U+E0000–U+E007F, except in a subdivision flag: U+1F3F4, tags, then
  U+E007F (UTS #51, ED-14a). Tags outside a flag hide text from people, not from a language
  model;
* keeps a zero-width joiner or non-joiner only between two characters that are not spaces;
* keeps at most 30 combining marks after one base character: the bound of UAX #15's stream-safe
  format (§13);
* runs of spaces become one, and no space stays at either end, as `_clean` does today;
* keeps everything else. That includes emoji with their joiners, variation selectors, skin tones
  and regional indicators, every script with its marks, the direction marks LRM, RLM and ALM, the
  zero-width space, the soft hyphen and the word joiner. These have uses in some scripts, and they
  draw as nothing.

It does not normalise: a text keeps the code points that came. `name_key` normalises, for the
comparison only.

The codec stays faithful: bytes become text, with U+FFFD for what is not UTF-8
(`protocol/codec.py:775`, `protocol/packet.py:109`). One change: a name that ends inside a UTF-8
sequence, because a sender or an app cut it to its bytes, loses that sequence instead of ending in
U+FFFD. companion-v1.18.0 cuts its own name at a whole character
(`src/helpers/AdvertDataHelpers.cpp:21`); not every sender does.

### Where text enters

With `clean_received`, the module applies where text enters the core, at the places where
`clean_received` applies today:

* the radio's records and adverts (`observer.py:1089-1099`), the heard nodes restored from the
  history (`observer.py:629`) and the documents the state file kept (`observer.py:944-945`);
* the inbox: the text, after the command is judged (`inbox.py:212-213`), the sender's name
  (`inbox.py:283-284`), and with the text the name a channel message claims;
* the refusals the state file kept (`state.py:232-242`) and a removal's result
  (`contact_manager.py:402`).

Commands are judged on the text as it came, as before
([ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md), `inbox.py:212`).

What the gateway sends and writes to the radio follows the same rules, whatever the setting says,
as today: a text to send (`core/text.py:85-105`) and a name from a contact link
(`core/text.py:151-155`). Today they share `_clean` with `clean_received`. So an automation that
relays a stranger's text sends no hidden tags.

### What each sink does

| Sink | Its encoding (2.2) | With this record |
| --- | --- | --- |
| MQTT documents and events | JSON (`mqtt/payloads.py:22-23`, `views.py:45-46`); topics carry keys, aliases and indices, never a name | values canonical by default; keys and types unchanged |
| Homie | `$name` as UTF-8 (`mqtt/homie.py:315-317`) | canonical by default |
| Home Assistant device names | the name, with 8 key digits when `name_key` finds it taken (`mqtt/contact_devices.py:435-443, 823-836`) | canonical by default; `name_key` with the skeleton (below) |
| Home Assistant's select *Add heard node* | a label: 12 and 8 key digits, then the name (`core/contacts.py:208-225`) | `shown` keeps emoji sequences whole |
| Home Assistant attributes and events | the published values; no entity's state carries text from the mesh, except a select's option: a label of at most 75 characters (a state holds 255, `homeassistant/const.py:61` in 2026.11.0) | — |
| Home Assistant's more-info dialog | the frontend makes a link of an attribute that starts with `http` | accepted and documented (2.2.1); by default no direction override reorders a link's text any more |
| Entity IDs, unique IDs, event types | slugs of `[a-z0-9_]`, at most 32, fixed at first appearance (`mqtt/contact_devices.py:838-844`); keys; fixed lists | — |
| Dashboard | `m()` removes controls and invisible characters and escapes; a value only on a line that opens an HTML block, or in `ha-qr-code`'s `data` attribute (guard tightened in 2.2.1) | `m()`'s class is the module's (rule 19); the frontend's pipeline checked in CI (below) |
| Command line | `_safe_text`, `_json_text` (`cli/common.py:83-106`) | `shown`: family emoji and subdivision flags whole |
| CSV export | `'` before a cell that starts with `=`, `+`, `-` or `@` (`cli/history.py:131-141`) | the first character after leading spaces and after NFKC decides, so a fullwidth `＝` counts |
| Logs | names and texts with `%r`; texts at debug level only | — |
| History and state file | SQL parameters; JSON | canonical by default; rows kept before stay |
| Installer | names from `probe` in its log; since 2.2.1 never in a whiptail box, which turns `\n` into a line break | the update review ([ADR 0036](0036-the-settings-registry-recommendations-and-the-update-review.md)) carries no text from the mesh, by test |

Lengths are bounded before the module: a name at 31 bytes (`core/text.py:16`), a claimed channel
name at 32 characters (`core/model.py:115`), a frame at 176 bytes. The module never lengthens a
text and caps the marks. Each sink keeps its own cut: 31 bytes for the radio's table, 32
characters for a slug, the text room for a draft.

### Names that identify

`name_key` folds what reads the same. A mark costs a longer device name, never a refusal:
`name_key` only marks (device names, `name_taken`, labels).

* As today: NFKC, case folding, invisible characters left out (`core/contacts.py:178-190`).
* New: the UTS #39 skeleton for characters that look like Latin letters or digits. The table holds
  the 480 entries of `confusables.txt` 15.1.0 that NFKC leaves as they are, of the 1,454 whose
  prototype is made of ASCII letters and digits. 15.1.0 is the Unicode version of Python 3.13,
  Debian 13's ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md)). A tool generates
  the table from the file and checks the file's SHA-256; the generated module names the version.
* The skeleton is case-sensitive: it maps a capital `I` to `l`, so case folding alone would split
  `AIice` from `Alice`. The key therefore takes the skeleton of the case-folded skeleton, and counts
  `i` as `l`. Then `Alice`, `ALICE`, `AIice` and `A1ice` compare equal, as do `Bob` and `B0b`, and
  `Home` and `Horne`.
* Today's table of Cyrillic and Greek letters stays for the two the skeleton does not fold to
  Latin (`κ`, `χ`), so every name marked in 2.2 stays marked.

An override no longer passes: `canonical_name` removes it before a name is compared or shown, so a
stored name displays as it compares.

### The default

`[messages] clean_received` defaults to `true` from 2.3.0. A changed default is compatible and
listed under *Changed* ([compatibility](../development.md#compatibility)). It is the first default
that changes once the settings registry exists, so the update into 2.3 asks about it
([ADR 0036](0036-the-settings-registry-recommendations-and-the-update-review.md), #186). This
record gives the registry the facts of the change:

| Field | Value |
| --- | --- |
| Key, title | `messages.clean_received`, "Clean names and texts from the mesh" |
| History of the default | `false` (2.0.0, the version that added the key), `true` (2.3.0) |
| Recommended, since, concern | `true`, 2.3.0, security |
| What the change does | It changes what the gateway publishes and keeps from now on. No stored row is deleted: the messages the history kept before keep their texts and names. At the first start the state file's copies of names are cleaned, as 2.2 does when a household switches it on (`observer.py:939-945`, `state.py:232-242`). It grants no right and publishes nothing more. |

ADR 0036 classes such a change as *neutral* and decides how the update review and an unattended
update treat it. This record decides only the default and what it cleans.

With `false`, texts and names stay as they came, as documented
([mqtt-api.md](../mqtt-api.md#received-messages-received)), and the sinks still encode them.
`name_key`, `shown`, the CSV guard and the cut sequence apply whatever the setting says. Names
canonical whatever the setting says is a 3.0 decision, since it changes the meaning of `false`.

What the household sees after the update into 2.3:

* The update review names the change before anything is written (ADR 0036).
* Device names in Home Assistant lose these characters once. A look-alike's device name gains its 8
  key digits, unless a user renamed the device in Home Assistant. Entity IDs stay.
* The dashboard looks as before: `m()` already dropped these characters. *Add heard node* and the
  command line show family emoji and subdivision flags whole.
* *Settings* shows *Received texts cleaned* as on, from the `config` document. Its explanation
  then says that names lose these characters too, and that emoji stay.

### The frontend's Markdown pipeline in CI

* The check moves from `tests/manual/` into the tests CI runs. It rebuilds the Markdown pipeline
  of the frontend that each pinned Home Assistant series uses: the allow list of
  `src/resources/markdown-worker.ts`, and marked with `breaks` and `gfm`.
* `marked` and `xss` come from npm, with `npm ci --ignore-scripts` from a `package-lock.json` per
  series with integrity hashes, at the versions the frontend's `package.json` names. They follow
  the frontend's pins, as paho-mqtt follows Debian's; Dependabot does not raise them.
* It runs on the cards the dashboard's contract test renders with the hostile corpus, in every
  language and series. It fails on an element the card's Markdown did not hold as raw HTML, and
  on a URL attribute (`src`, `srcset`, `href`, `poster`, `data` and the like) that the template did
  not write. The tag comparison of the verification catches an element whatever its URL.
* A test compares the frontend version each copy of the allow list names with the requirement of
  the installed Home Assistant (`home-assistant-frontend==20261028.4` in 2026.11.0). A new pin
  fails it until the allow list is compared again.
* Node and npm run in CI only. Nothing of them ships.

### Smaller choices

| Choice | Chosen | Rejected, and why |
| --- | --- | --- |
| Mark cap | 30 per base, the standard's bound | 4 to 8, common in display code: bounds overprinting better, but its effect on legitimate text in every script is not tested; revisit after the browser check |
| Confusables | the Latin and digit part, after NFKC | the whole table (6,311 entries in 15.1.0): 4,857 entries whose prototype is not ASCII, each a possible false mark, for no case found; every look-alike in the audit poses as a Latin name |
| CSV form | `'` before a formula start, as documented ([design §6.9](../design.md#69-command-line)) | a tab inside the quoted cell, OWASP's form against Excel: it changes the data for every other reader. OWASP notes that Excel may drop the `'` after a save and a reopen; the guide says so and points programs to `--format jsonl` |
| More-info links | accepted as Home Assistant's behaviour, documented in 2.2.1 | a changed `text` (breaking); a character before `http` (changes the data, breaks automations that compare texts) |
| The frontend check | in CI, with npm | by hand: it went stale; in a browser: heavier, and the release's browser check does that part |
| Names a channel message claims (#179 SEC-4) | one "claimed" look for every such name, never a link: a rule for the redesign (#170, #171) in 2.4; no new key | `sender_name_taken`: it would mark every post of the household's own members, whose names match contacts |
| Raw text | none kept | a raw copy in the history: keeps more of strangers; `false` serves a household that wants raw text |

### Findings of the audit of #179

The audit's own numbering, not that of the security review of 2026-11 (SEC-001 and on).
Severities as verified.

| Finding | Severity | Closed by |
| --- | --- | --- |
| SEC-1: control, escape and direction characters reach Home Assistant, the documents and Homie by default; the *New node* event carries any stranger's name | low | the repertoire and the default `true` (2.3) |
| SEC-2: `name_key` misses an override and digit-for-letter look-alikes (`A1ice`, `B0b`, `AIice`) | low | the entry removes overrides; the skeleton (2.3) |
| SEC-3: positions not range-checked | low | one validator for adverts, records and telemetry (2.2.1) |
| SEC-4: a name a channel message claims looks like a checked one | low | one "claimed" look, never a link (2.4, with #170 and #171) |
| SEC-5: the more-info dialog links text that starts with `http` | low | documented (2.2.1); accepted |
| SEC-6: companion-app notifications interpret forwarded text | info | the guide's recipe (2.2.1) |
| SEC-7: the check of the frontend's pipeline is stale, partial and manual | low | the rule-1 guard (2.2.1); the check in CI with the version test (2.3) |
| SEC-8: device names keep invisible characters the dashboard drops | info | one class for `m()` and links; names compare without them. The rest is accepted: they draw as nothing in both places |
| SEC-9: emoji sequences broken in labels and the command line; a cut name ends in U+FFFD | info | `shown`; the decoder drops a cut sequence (2.3) |
| SEC-10: the CSV guard misses a leading tab or CR and fullwidth signs | info | the guard after leading spaces and NFKC (2.3) |
| SEC-11: the documentation claims more than the code does | info | corrected (2.2.1); rewritten to the module (2.3) |
| SEC-12: Assist and its language model read names that contacts chose; tag characters hide text | info | tags dropped outside flags (2.3); the guide's note (2.2.1) |
| SEC-13: combining-mark floods unbounded | info | 30 marks per base (2.3) |

### Consequences

* Good, because every sink outside the gateway's control receives text without control, escape or
  direction characters by default: Home Assistant's pages, Homie, terminals and apps.
* Good, because one module holds the rules, and a new sink takes text only from it. The five places
  become one.
* Good, because emoji and every script stay readable; labels and the command line show emoji
  sequences whole.
* Good, because `AIice`, `A1ice` and `B0b` are marked like `Alice` and `Bob`.
* Good, because the frontend check fails in CI when its pipeline or the pinned frontend changes,
  instead of going stale.
* Bad, because texts and names lose characters by default, and the history keeps the canonical text.
  A household that needs raw text sets `false`.
* Bad, because the skeleton also marks names that differ but read alike, such as `Home` and
  `Horne`. Such a device name gains 8 key digits.
* Bad, because device names change once in Home Assistant where a name held a dropped character or a
  newly folded look-alike.
* Bad, because CI gains a package source, npm, for one check. The lock file's hashes, the skipped
  install scripts and the frontend's own pins bound it.
* Bad, because `false` keeps names raw until 3.0, so the documentation names two behaviours.

### Trust boundaries

None moves, and no entry point is added. B1 the air: names, texts and claimed names stay untrusted;
the module bounds their repertoire and decides nothing about who sent them. B2 the radio's link:
over TCP the radio is not authenticated, and its records pass the same entry. B3 the broker: the
documents carry canonical text by default; texts and contact links from the bus pass the same rules
on their way to the radio. B4 Home Assistant: device names, attributes and events are canonical by
default; the more-info dialog still links text that starts with `http`. B5 its users: the dashboard
looks as before; Assist reads no hidden tags. B6 the container: the history and the state file keep
canonical text by default. B7 the project: npm packages run in CI only, pinned by hash. The threat
model's controls and remains of B4 change in the same pull request.

### Confirmation

* `tests/hostile_corpus.py`: one corpus with neutral examples, a legitimate set (emoji sequences,
  flags, scripts with marks, right-to-left letters), hostile sets and raw bytes, including `AIice`
  and `None`. Every new sink's tests use it.
* `tests/test_untrusted.py`: the corpus as examples, and Hypothesis properties over `st.text()` and,
  through the decoder, `st.binary()`: idempotent; never longer; no character of `DROPPED`; a name on
  one line; every legitimate entry unchanged; joiners only between characters; at most 30 marks per
  base; `name_key(canonical_name(x)) == name_key(x)`; the pairs that must compare equal and those
  that must not.
* The codec: every decoder with the raw bytes, no exception and no surrogate; a cut name leaves no
  U+FFFD.
* End to end on the emulated radio: with the defaults every payload parses strictly and holds no
  character of `DROPPED`, Homie's `$name` included; topics stay ASCII; no log record holds a raw
  control character; the history holds the canonical text. With `false`, texts as they came.
* The Home Assistant contract tests, both series: the corpus as contacts with devices; the
  registry's device names hold no character of `DROPPED` and keep the legitimate emoji; `AIice`,
  `A1ice` and `B0b` get key digits.
* Rule 19 of the dashboard's tests: each copy of `m()` removes the module's class.
* The frontend's pipeline in CI, and its version test.
* The registry's tests of [ADR 0036](0036-the-settings-registry-recommendations-and-the-update-review.md):
  the default's history, its flags, its changelog line; the snapshot of the public interfaces
  records the changed default.
* Mutation checks: removing a class from `DROPPED`, the mark cap, the skeleton table, the `i`/`l`
  fold or the CSV guard's strip each fails a test.

## Pros and Cons of the Options

### One module, canonical at the entry by default

* Good, because what leaves the gateway has one bounded repertoire, for every consumer.
* Good, because the encoders at the sinks stay: two independent controls.
* Good, because the same name reads the same everywhere.
* Bad, because the default deletes characters from what is published and kept.

### Today's split

* Good, because nothing changes, and texts stay as received for automations.
* Bad, because the sinks the gateway does not control receive raw characters by default (#179
  SEC-1).
* Bad, because five sets drift apart (#179 SEC-8, SEC-9), and each new sink must repeat the rules.

### Cleaning at each sink that leaves the gateway

* Good, because the history and the state file would keep text as it came.
* Bad, because each adapter repeats the rule, and one name differs between sinks.
* Bad, because the documents, one source for every front end
  ([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)), would still carry raw text to the
  consumers the gateway does not know.

### A strict repertoire

* Good, because it leaves the least to interpret, and UTS #39's identifier profile is a standard.
* Bad, because it drops the emoji and scripts that #179 asks to keep. A name here is a person's
  label, not an identifier in a program.
* Bad, because ASCII exists already where it is needed: `ascii_only` for radios that cannot draw
  more.

### Names canonical whatever `clean_received` says, now

* Good, because device names and Homie would be safe also where a household set `false`.
* Bad, because it changes the meaning of `false` in a minor version, which is breaking
  ([compatibility](../development.md#compatibility)).

## More Information

The maintainer decided on 2026-11-15 (#179): positions off the globe are no position, in 2.2.1;
`clean_received` defaults to `true` in 2.3, and households are advised to set it now; names cleaned
whatever the setting says is a 3.0 decision; the check of the frontend's Markdown pipeline runs in
CI, with npm packages. The repertoire, the cap of 30 and one look for claimed names follow the
recommendations the maintainer accepted the same day. So does ADR 0036's rule that *deletes* means
stored data, which lets the flip reach unattended installations too.

This record amends [ADR 0005](0005-runtime-dependencies-from-the-distribution.md), which carries a
dated line: npm packages for one check in CI, none in production.

2.2.1 carries the parts of #179 that change no default: the range check (SEC-3), the guard of
rule 1 (SEC-7), and the documentation of SEC-5, SEC-6, SEC-11 and SEC-12. This record brings the
rest in 2.3.0a1, right after the settings registry, so the update into 2.3 asks about its own
changed default. SEC-4 follows with the redesign in 2.4.

New sinks take their text from the module, and their tests use the corpus: the heard-node page
(#173), the replace fold (#167), the labels of former contacts (#166), the findings that name a
contact (#185), *Enrolling*'s list of new devices, and in 2.4 the maintenance port's card with the
app name a client sends (#187). Command words stay ASCII, and their arguments stay untrusted text
(#176). If the household overrides of the settings registry come (an open decision), a radio name
set from Home Assistant passes `canonical_name` like a contact link's.

Sources: [threat model](../threat-model.md), B1 to B4;
[development.md](../development.md#text-from-the-mesh-and-the-bus);
[configuration.md](../configuration.md#messages); MeshCore companion-v1.18.0 for the firmware lines;
Home Assistant 2026.11.0 and frontend 20261028.4; MQTT 5.0 §1.5.4; the Homie convention; UAX #9;
UAX #15 §13; UTS #39 and `confusables.txt` 15.1.0; UTS #51; Trojan Source (CVE-2021-42574); OWASP's
pages on XSS prevention, input validation and CSV injection.

Revisit when:

* a household reports a legitimate name or text that the repertoire changes: marks beyond the cap,
  or a joiner the rule drops;
* the skeleton's marks on distinct names become a nuisance, or a look-alike outside Latin poses as a
  household member;
* Home Assistant's frontend changes its Markdown pipeline, starts rendering an attribute as markup,
  or stops linking text that starts with `http`; the version test names each new pin;
* Debian's Python moves to a newer Unicode, or Unicode adds direction or tag characters;
* 3.0: names canonical whatever `clean_received` says, or the switch goes;
* a sink appears that interprets text and takes it from elsewhere than the module.
