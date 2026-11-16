---
status: proposed
date: 2026-11-15
decision-makers: maintainers
---

# The settings registry, recommendations and the update review by the new version

## Context and Problem Statement

Each release can change what a setting means for a household. 2.3 changes two defaults:
`[messages] clean_received` becomes `true` (#179,
[ADR 0034](0034-untrusted-text-from-the-mesh.md)), and `[airtime] burst_s` is derived from the
radio's preset (#181). Later releases bring recommended settings that grant something, such as
free commands (#176) or the maintenance port (#187).

Today only the *installed* version renders and checks the configuration, and only the *next*
version knows what changed. So a release that changes a default asks the administrator to read
the changelog and edit `config.toml` by hand, in a shell, right before the update. Line numbers
here are at `b02a3d8` (2.2.0) unless a line says otherwise:

* 1.x to 2.0: `sed -i` lines for `tcp_unauthenticated` and `insecure`, then the update at once,
  because the installed version refuses a key it does not know (`docs/deployment.md:319-334`).
* 2.2: `heard_nodes = "keys"` to keep the old behaviour, `radio_controls = false` right before the
  update, an ACL line, an export before the first start deletes messages
  (`docs/deployment.md:347-372`). The analysis of #185 counted up to seven such steps for 2.2.
* `--update` shows no dialog at all (`deploy/proxmox/meshcore-home-gateway-lxc.sh:1543-1558`).
  It stages the new zipapp, checks the current file with it and switches
  (`lxc.sh:846-871`).

Four facts of today's code shape the answer:

* **No machine-readable place says what a version recommends, or how a default changed.** The
  defaults sit in the parser twice: the dataclass fields (`config.py:60-227`) and the parse calls
  (`config.py:541-920`). The installer's answer types are a third table (`_ANSWER_TYPES`,
  `config.py:922-991`). The public-interface snapshot records each default
  (`tests/public_interface.json`), and the release plan classes a changed default as *Changed*
  (`tools/public_interface.py:149-152`). Why it changed lives only in the changelog's prose.
* **The installer pins defaults.** `ask_advanced` returns the default without a dialog in the
  default setup (`lxc.sh:228-236`), and `ask_bridge` stores every answer (`lxc.sh:1079-1093`). A
  default installation therefore writes `homeassistant.discovery = true`,
  `homeassistant.device_name = "MeshCore radio"` and `messages.ascii_only = false`, beside the
  identity and connection settings, and `--reconfigure` (`lxc.sh:1577`) writes them again into a
  file that lacked them. The program itself already keeps defaults out of a file:
  `config answers` lists only the settings present, "so settings left at their default keep
  following the default" (`config.py:1038-1045`, `tests/test_config.py:639-640`). A pinned
  preference looks like the administrator's choice to any later comparison.
* **The installer has little room left.** It must fit one argument of `bash -c "$(wget …)"`:
  96 KiB (`BUDGET = 98_304`, `tests/test_installer_language.py:254`). It has 77,781 bytes at 2.2.0
  (20,523 left) and 81,647 bytes on `fix-2.2.1` at `96e5ee0` (16,657 left), measured with `wc -c`.
* **whiptail cuts text without a mark.** A menu of 20x78 shows 10 lines of text above a list of 3,
  one of 24x78 shows 14, and a yes/no box of height 22 shows 16. 2.2.1 writes menu items without
  tags (`--notags`), at most 75 columns in a box of 78. `tests/test_dialog_texts.py` models the
  wrap and fails when a text needs more lines than its box shows (`docs/platform-notes.md`,
  *whiptail*, on `fix-2.2.1`; whiptail 0.52.24).

The service cannot write its own configuration: `/etc` is read-only in its sandbox
(`ProtectSystem=strict`, `deploy/systemd/meshcore-home-gateway.service:51`), and it should stay
so. Home Assistant gives every user the same rights on entities (`USER_POLICY`,
`homeassistant/auth/permissions/system_policies.py:7`), and an MQTT device cannot raise a Repairs
issue. MQTT discovery takes only the components of `SUPPORTED_COMPONENTS`
(`homeassistant/components/mqtt/const.py:423-456`, subscribed in `discovery.py:616-628`): the
entity platforms, `device_automation` and `tag`. The integration raises issues only about its own
configuration (`__init__.py:325`, `:528`; `entity.py:293`, `:439`). All at 2026.10.2.

Where do recommendations and the history of defaults live? Which program compares them with the
file? Who asks, and what may change without a person? How does the household learn of it?

## Decision Drivers

* No hand edits at an update. The maintainer asked for dialogs at `--update` (#186) and for
  workflows that need the user as little as possible (#174, #185).
* Consent. Nothing that grants a right or widens access changes without a person's explicit
  answer. Stored data is not deleted without the release having decided it, and never silently.
* One source. The docs, the changelog, the release plan, the installer's dialog, Home Assistant
  and the release information say the same words, from one table.
* A file keeps its meaning within a major version
  ([compatibility](../development.md#compatibility)).
* The installer stays a thin shell within its budget: logic and texts live in the program.
* The household sees open recommendations where it looks, read only. *Settings* shows, it does
  not change ([ADR 0031](0031-household-chat-problems-and-settings-on-the-generated-dashboard.md)).
* whiptail is optional. Proxmox does not install it, and `--update` needs it only when something
  is open (`docs/deployment.md:33-35`).
* An unattended run is predictable: the same input gives the same file.

## Considered Options

* The new version reviews the file against a registry of defaults and recommendations, and asks
  only what needs a person
* As today: the changelog and hand edits
* dpkg's way: always keep the file's value, with *keep* preselected for every item
* Always the recommended: preselect it, and take it in unattended runs too
* Migrate silently with a backup (Zigbee2MQTT), or tie defaults to the first installed version
  (NixOS `system.stateVersion`)

## Decision Outcome

Chosen option: "the new version reviews against a registry", because it is the only one that ends
the hand edits, keeps every grant behind a person's answer, and gives the docs, the installer and
Home Assistant one source.

Household overrides (values Home Assistant may change, decision 2 of the 2.3 plan) are no option
here: they serve another need, and they are still open (*Open decisions*). The review and the
overrides are orthogonal, so accepting this record decides nothing about them.

### The registry

One pure module, `core/settings.py`, holds one entry per configuration key. It knows what the
parser does not: the history of each default, the current recommendation and its reason, what a
change does, and who may change the key. The parser stays the only validator.

```python
class Effect(StrEnum):  # what moving to a value does; one per change, the weightiest
    DELETES = "deletes"  # the first start with it deletes stored data
    GRANTS = "grants"  # someone gains a right: a sender, Home Assistant's users, a LAN client
    WIDENS = "widens"  # more data is published, kept, read or sent off the host, whoever's
    NEUTRAL = "neutral"  # none of these


@dataclass(frozen=True, slots=True)
class Default:  # one value the default has had, oldest first
    since: str  # "2.0.0", "2.3.0"
    value: object  # DERIVED: the gateway decides
    # of the change from the behaviour before: the entry before, or, for a key's first entry,
    # the version before the key
    effect: Effect
    # a first entry that changes behaviour: the value that behaves as the version before the
    # key did (last_known_days: 0)
    before: object = SAME


@dataclass(frozen=True, slots=True)
class Recommendation:
    value: object  # DERIVED: "let the gateway decide", which removes the line
    since: str
    concern: Concern  # SECURITY | PRIVACY | RELIABILITY | FEATURE
    reason: str  # msgid, one sentence, at most 140 characters in every language
    effect: Effect | Callable[[object], Effect]  # of taking it, from the value in effect
    applies: Callable[[Context], bool] = always  # e.g. only with heard_nodes = "full"


@dataclass(frozen=True, slots=True)
class Key:
    key: str  # dotted, as configuration.md writes it: "messages.clean_received"
    answer: type  # str, int, float, bool or list: the installer answer's type
    how: How  # who may change it, and where
    defaults: tuple[Default, ...]
    title: str | None = None  # msgid, at most 60 characters; required with a recommendation
    docs: str | None = None  # "configuration.md#messages"
    recommended: Recommendation | None = None
    deprecated: Deprecation | None = None  # since, and the rewrite the program can do
    derive: Callable[[Context], object] | None = None  # for DERIVED values
```

* **Every key has an entry.** About 70 today, each with its type and its history. `_ANSWER_TYPES`
  goes: `config.py` builds the installer's answer types from the registry, so two tables become
  one.
* **History.** `defaults` starts at 2.0.0, or at the version that added the key. The review
  serves the 2.x major; a 1.x file goes through 2.0's breaking notes. A key added later can
  change behaviour with its first default: then its first entry names the effect against the
  version before the key, and `before` the value that behaves as that version did. Such an entry
  is a changed default like any other: a `changes_now` item, and the consent rules apply. The
  first entries:

  | Key | History of the default | Effect of the last change |
  | --- | --- | --- |
  | `messages.clean_received` | `false` (2.0.0), `true` (2.3.0) | neutral: no stored row is deleted (ADR 0034) |
  | `airtime.burst_s` | `10.0` (2.0.0), derived (2.3.0) | neutral |
  | `contacts.heard_nodes` | `"keys"` (2.0.0), `"full"` (2.2.0) | widens ([ADR 0032](0032-heard-nodes-kept-with-name-signal-and-time-by-default.md)) |
  | `telemetry.last_known_days` (new, [ADR 0035](0035-the-companion-state-model.md)) | `7` (2.3.0); `before` `0`: 2.2 shows no last known position | widens: last known positions reach every Home Assistant user and the recorder |
  | the update check's switch (new, ADR 0039, which names the key) | on (2.3.0); `before` off: 2.2 has no check | widens: the container's address reaches GitHub once a day |

  `clean_received`, `heard_nodes` and `publish_channels` came with 2.0.0a1 (`CHANGELOG.md:955-962`).
* **Recommendations.** A recommendation is a last resort: if the gateway can derive a value from
  what it knows, it derives it, and the entry recommends *let the gateway decide*. A
  recommendation is for a value judgement a person must own: privacy, security, a feature with a
  trade-off. A key whose default changed recommends the new default from that version on. The
  first entries:

  | Key | Recommended | Since | Concern | Effect of taking it |
  | --- | --- | --- | --- | --- |
  | `messages.clean_received` | `true` | 2.3.0 | security | neutral |
  | `airtime.burst_s` | let the gateway decide | 2.3.0 | reliability | neutral |
  | `contacts.heard_nodes` | `"full"` | 2.2.0 | feature | widens |
  | `telemetry.last_known_days` | `7` | 2.3.0 | feature | widens (from `0`) |
  | `messages.publish_channels` | `"targets"` | 2.0.0 | privacy | deletes (from `"all"`: the next start deletes the other channels' messages) |
  | `diagnostics.log_level` | `"info"`, only while the file has `"debug"` (`applies`) | 2.0.0 | privacy | neutral (from `"debug"`: logs less) |
  | `diagnostics.publish_rx_log` | `false` | 2.0.0 | privacy | neutral |
  | `mqtt.password_file` | a file, when `password` is inline | 2.0.0 | security | neutral |

  Later releases add their own: free commands (#176, grants), the maintenance port (#187,
  grants), installing updates from Home Assistant (grants), `[contacts] replace_targets` (#167,
  grants). No recommendation depends on a region: the band-derived duty cycle stays a default the
  file can change (#174, decided 2026-11-15).
* **Effect.** Each change has one effect, the weightiest: *deletes*, then *grants*, then
  *widens*, then *neutral*. *Deletes* means stored data: a start that deletes rows of the history
  or the state. Cleaning texts that arrive later deletes nothing stored. So the `clean_received`
  flip is *neutral* and applies everywhere. *Widens* counts every kind of data, whoever's it is:
  more of other people's data published or kept, and the household's own data logged or sent off
  the host (`"debug"` writes texts, positions and keys into the journal; the daily update check
  shows the container's address to GitHub). *Grants* and *widens* are told apart for the words
  the administrator reads (a right, or more data and whose); every consent rule below treats them
  alike.
* **An effect belongs to a move**, from the value in effect to the new one: `"info"` logs less
  than `"debug"` and more than `"warning"`. Where the value in effect decides, `effect` is a
  function of it, and the review computes each item's effect from the file. `log_level`'s
  recommendation also applies only to `"debug"`, so a quieter file is not asked at all.
* **Who may change it** (`How`). One of: *the installer* (a dialog of the default or the advanced
  setup, named), *the file* (an answers file or by hand), *the gateway* (it decides unless the
  file sets the key), *the radio* (a seed key of
  [ADR 0020](0020-managed-radio-profile-for-the-observer.md): a change made in the MeshCore app
  is kept). The maintenance port (2.4) adds *the MeshCore app during maintenance*. Home Assistant
  gets a value here only if household overrides are built (open, below).
* **Core stays pure.** `Context` holds the values in effect as a dotted mapping and the radio's
  modulation when known. `core` imports no `config` (`tests/test_architecture.py`); `config.py`
  reads the registry.

### What reads the registry, and what keeps it true

| Reader | What it takes | Kept equal by |
| --- | --- | --- |
| The parser (`config.py`) | the answer types | it builds them from the registry |
| `config review` (below) | everything | it is the registry's own function |
| The `config` document's `review` section | open and kept items of allowlisted keys | the document's allowlist test |
| `docs/configuration.md` | a generated table *Recommended values and changed defaults* between markers | `python3 tools/recommendations.py` writes it, as `tools/platform_notes_index.py` writes its index; `tests/test_docs.py` fails while it differs |
| `CHANGELOG.md` | each version's section names every key whose default or recommendation changes in it | a test of `[Unreleased]` and of each released section, beside `tests/test_release_notes.py` |
| `tools/release_plan.py` | a default changed against the last release needs a `Default` entry with the version being released | the snapshot already classes it as *Changed*; the plan gains this one check |
| The catalogs | `title` and `reason` msgids | `make translations`; a length test per language |
| `release.json` (ADR 0039) | this version's recommendations | built by `make release`; not decided here |

**Guard tests** (`tests/test_settings.py`):

* The registry's keys and the keys the parser reads are the same set. The parser's recorder of
  `tests/test_config.py` (`settings_read`) lists them.
* Every key is in its section's table of `docs/configuration.md` (the parse that
  `tests/test_dashboard.py:3080-3092` already does).
* The last `Default` equals what `parse_config` of a minimal file gives; `DERIVED` equals the
  parser's unset value.
* Histories are ordered and their versions valid. A first entry that is not neutral names
  `before`, and `parse_config` with `before` behaves as the version before the key (a fixture
  per such key).
* For every recommended key with a finite set of values (booleans and choices), the effect of
  the move from each allowed value to the recommended one: a move that publishes, keeps, logs or
  sends more is never neutral, so never preselected.
* A key whose default changed recommends the new default from that version.
* Every `title` is at most 60 and every `reason` at most 140 characters in every shipped
  language and in the pseudo-language `xx`; neither holds a tab or a line break.

So a changed default cannot ship without its history entry, its changelog line and its docs row.

### The derived burst (#181)

The maintainer decided on 2026-11-15: derive `burst_s` from the preset, capped at one hour's
budget.

* **Unset, the gateway derives it at each connect:** an advert with a 31-byte name plus a long
  flooded direct message, rounded up, at least 10 s, at most `max(10, budget_s_per_hour)`. The
  helper is 2.2.1's `advert_reserve_s` (`core/airtime.py:153-160` on `fix-2.2.1`). EU/UK Narrow
  and every faster preset keep 10 s. Slow presets get what an advert and an alert need
  (11–29 s in the table of [Radio presets](../configuration.md#radio-presets)). At 62.5 kHz
  SF 12 CR 4/8 the cap holds it at 36 s, and 2.2.1's warning still says that no advert fits.
* **Set, it stays exact.** An administrator may want short peaks low. The 2.2.1 warnings apply.
* **The budget resizes without a gift.** A new method, `AirtimeBudget.resize` (`core/budget.py:16`
  has none today), keeps the tokens, at most the new capacity; growing adds none, so a reconnect
  gains nothing. The state file's `airtime` record
  (`state.py:896-901`) gains the effective burst and the modulation, so a start before the radio
  connects uses the last effective burst. Without either, the floor applies: 10 s.
* **One fact, published where state lives.** `state` gains `airtime_burst_s` (the value in effect)
  and `airtime_burst_source` (`configured` or `derived`). *Airtime available* shows both as
  attributes, beside `budget_s_per_hour` (`mqtt/discovery.py:243-259`). The `config` document
  keeps `airtime.burst_s` as a number: its documented meaning is "the value in effect"
  (`docs/mqtt-api.md:1108-1109`), so it shows the same number, computed by the same function. A
  test keeps the two equal.
* **The regulatory bound is unchanged.** It is the radio's own limiter, 10 % in the 10 % band by
  the managed profile. A larger burst changes the gateway's short-term peaks, not the hour.

### The installer stops pinning preferences

Whenever the installer renders a file, at a new installation and at `--reconfigure`, it writes a
preference only when the file or the answers already set it, or when a dialog that was shown got
an answer other than the default. The default setup's `ask_advanced` answers without a dialog, so
it never pins. The preferences are
`homeassistant.discovery`, `homeassistant.device_name` and `messages.ascii_only`. Identity and
connection settings stay written: `mqtt.base_topic`, `mqtt.node_id`, `mqtt.port`, `mqtt.tls` and
`homeassistant.language` (the language menu's choice). Brokers, ACLs and entity IDs refer to them.
Files of earlier installations keep their pins; the review says when a value "is the default of
the version that wrote it".

### `config review`

One subcommand of the version under review, run as root in the container. The installer already
runs the staged zipapp there for `check-config` (`lxc.sh:865`). It never connects to the radio or
the broker.

```text
meshcore-home-gateway config review [--config FILE] [--from-version X.Y.Z] [--state-dir DIR]
                                    [--json | --list | --plan DECISIONS
                                     | --apply DECISIONS --output FILE [--group G]
                                       [--password-file PATH]]
```

* `--from-version`: the installed version. Without it there are no changed defaults to report. A
  pre-release counts as its release: 2.3.0rc1 ships 2.3.0's program.
* `--state-dir`: read only. It tells the program the radio's last modulation, so the derived burst
  is known. A state file of 2.2 keeps the airtime of a long message (`longest_s`), which tells
  whether the derived burst stays 10 s: an advert is shorter than a long message.
* `--json`: items, notes and kept marks, for people and other tools.
* `--list`: for the installer. Tab-separated lines whose first field names the kind of line, as
  `probe --list` does: the header (versions, the language of the texts, counts), `item` (key,
  kind, the preselected answer, the text), `choice` (key, word, label), `note`, `prompt`. New line
  kinds stay compatible. The texts arrive finished, translated and fitted.
* `--plan DECISIONS`: the final screen's text. It writes nothing.
* `--apply DECISIONS --output FILE`: writes the new file through the same `config answers` and
  `render_config` round trip as `--reconfigure` (mode 0640, `--group`). DECISIONS is `-` or a
  file of `KEY=recommended|keep|later|export` lines. Only keys that are open items of this review
  are accepted; anything else is a usage error and writes nothing.
* Without a mode: a report for people, with the exact answer line per item. It serves Linux
  systems without the Proxmox installer (`docs/deployment.md` §3).
* Exit status: 0 done (also with open items), 2 usage error, 78 a file that cannot be reviewed.

**Kinds of item.** The kind decides the choices and the preselected answer:

| Kind | When | Choices |
| --- | --- | --- |
| `changes_now` | a default changed after `--from-version`, the file does not set the key, and the value in effect changes for this installation | *Take the new behaviour* / *Keep the old behaviour* (writes the old value); with *deletes*, *Export first* too |
| `deviates` | the file sets a value other than the recommendation | *Take the recommended* / *Keep mine (not asked again)* / *Decide later*; with *deletes*, *Export first, then take it* too |
| `offered` | the file does not set the key, and the default is not the recommendation | as `deviates` |
| `deprecated` | the file sets a key that goes in the next major | *Replace it* / *Keep until 3.0 (asked again)* |

**Items that delete.** Every item whose taking deletes stored data offers *Export first* and
takes the decision word `export`, whatever its kind. Rule 1 decides what is preselected: *Export
first* for a `changes_now` item, whose deletion the release decided; *Keep* for the others, where
taking is a choice. With `--state-dir` the item says how many rows the first start would delete,
counted read only in the history. 2.3 has one such item: `messages.publish_channels = "targets"`
taken from `"all"`, which deletes the messages of the channels that are not published at the next
start (`wiring.py:252`).

Migrations and acceptances (a renamed key, a new rule between settings) are items of 3.0 only
(as #186's design plans), with the same machinery.

**Never an item:**

* a key marked kept for this recommendation's `since` or a later one;
* a key whose value in effect already is the recommended one, or would not change by taking it;
* anything the gateway decides itself, with two exceptions: the file pins it (then *Let the
  gateway decide* removes the line), or its default became derived after `--from-version` and the
  derived value differs from the old default for this installation (a `changes_now` item, such as
  the derived burst at a slow preset);
* identity and trust settings without a recommendation: the broker, credentials, topics, the node
  ID, the radio link, senders, words, exposure. The review never proposes a value for them.

**Values are written as the file writes them** (`true`, `"full"`), and keys as
`configuration.md` writes them (`[messages] clean_received`), so what the administrator reads is
what the docs and the file say. No item carries text from the mesh; a test keeps it so
([ADR 0034](0034-untrusted-text-from-the-mesh.md)).

### The consent rules

Decided by the maintainer on 2026-11-15, as recommended in the 2.3 plan (decision 5,
[design §13.1](../design.md#131-release-23-the-foundations-and-updates-that-ask)):

1. **Preselection.** The recommended value is preselected only when taking it is *neutral*: it
   grants nothing, widens nothing and deletes no stored data. Otherwise *Keep* is preselected.
   Before a first start that deletes data because the release changed a default, *Export first*
   is preselected; the deletion stays the release's decided default.
2. **An unattended run without a `review` answer changes nothing the file sets and records
   nothing as kept.** `review` and `review.KEY` answers act as the table of *Unattended runs*
   says. The next interactive review asks again, and Home Assistant shows the open items
   meanwhile. This is debconf's rule: under a non-interactive frontend a question is not marked
   as seen (`Debconf/ConfModule.pm:188-197`, debconf 1.5.86).
3. **A changed default that grants or widens is written with its old value by an unattended
   update.** The behaviour then changes only with a person's answer: the next interactive review
   shows the key as `deviates`, with *Keep* preselected.
4. **`review=recommended` in an answers file takes only neutral items.** An item that grants,
   widens or deletes needs its own line, `review.KEY=recommended`.
5. **"Keep mine" is kept in the file** as `[settings] kept = ["key@version"]`.
6. **Taking a recommendation equal to the new default removes the line**, so the file follows the
   defaults from then on. A kept mark for the key goes with it.
7. **The installer stops pinning preference defaults**, at a new installation and at
   `--reconfigure` (above).
8. **Home Assistant shows recommendations read only** (below).
9. **`deletes` means stored data**, so the decided `clean_received` flip applies everywhere.

**The first item, by these rules.** The update into 2.3 asks about the new default of
`[messages] clean_received` ([ADR 0034](0034-untrusted-text-from-the-mesh.md)), which is
*neutral*. Where the file does not set the key, it is a `changes_now` item: the final screen lists
it, and an unattended update applies it. Where the file sets `false`, it is a `deviates` item:
*Take the recommended* is preselected, *Keep mine* is recorded in `[settings] kept`, and *Decide
later* asks again at the next update; an unattended update changes nothing there. The second
item is new: `[telemetry] last_known_days` ([ADR 0035](0035-the-companion-state-model.md)) widens,
so an interactive update asks with *Keep the old behaviour* preselected, and an unattended one
writes `last_known_days = 0`.

### `[settings] kept`

```toml
[settings]
kept = ["contacts.heard_nodes@2.2.0", "messages.clean_received@2.3.0"]
```

* A list of `KEY@X.Y.Z` strings. A mark hides the key's recommendations up to that version. A
  newer recommendation for the key asks again.
* A malformed entry is an error, like any setting. A key the version does not know is accepted: a
  newer version wrote it.
* Answer type `list`, so `config answers` and `config render` round-trip it and `--reconfigure`
  keeps it. An inline table (`settings.md` S9) cannot round-trip: `config answers` writes a
  sub-table's keys with three dots (`config.py:1056-1058`), and `render_config` splits at the last
  dot (`config.py:1017`), finds no such answer and refuses it.
* It changes no behaviour of the gateway; it only decides what is asked and what *Settings*
  marks. Editing it by hand is safe. A forged mark can hide a question, never change a setting,
  and the file is root's ([ADR 0006](0006-deployment-in-an-unprivileged-proxmox-container.md)).
* It lives in the file, not the state directory: next to the value it explains, in the
  configuration's backup, and readable by the service, which shows the marks. A comment cannot
  work: rendering drops comments.
* `[settings]` is a new optional table, a compatible change. Round 1 reserved the same table for
  `changeable` (household overrides, open below).

### The installer: `--update`, `--review` and `--reconfigure`

The service keeps running with the old version during every dialog. Nothing is written before the
final answer.

| Step | Where | What |
| --- | --- | --- |
| 1 | host | checks, packages, journal limit, user, as today |
| 2 | container | **stage**: the new zipapp under its temporary name, verified as today (`SHA256SUMS` and the digest the installer pins, `lxc.sh:748-803`); it stays staged. `die`, the error trap and the signal trap remove it |
| 3 | container | `config review --list` with the staged program, the installed version (`meshcore-home-gateway --version`, as `--reconfigure` reads it, `lxc.sh:1570`) and the state directory. A staged version without the command skips steps 3–5, as `check-config --broker` is detected today (`lxc.sh:510`) |
| 4 | host | **ask** (below), only with a terminal and something to show |
| 5 | container | `--plan`, the final screen, then `--apply` into `config.toml.new` and `check-config` of it with the staged program |
| 6 | container | **switch**, by the staged program: `install switch` (below). The program first; then, only if step 5 wrote a file, the old file is kept as `config.toml.<old version>` (0640, the same owner and group) and the new one moved in place |
| 7 | container | restart, self-test, the final log: what was taken, kept and left for later, and the notes |

* **The program switches first.** If the host dies between the two renames, the new program runs
  with the old file, which it accepted in step 5. The other order could leave the old program with
  a `[settings]` table it refuses at its next start.
* **No comment is lost silently.** A file is written only when an answer writes. The old file is
  kept, and the final screen says so. The standard library has no TOML writer, so re-rendering
  drops comments, as `--reconfigure` does today.
* **Notes for after the update** come from the new version: a broker ACL line Home Assistant's
  user needs, from one table of Home Assistant's topics with the version each appeared
  (`meshcore-home-gateway broker acl` prints it; `docs/mqtt-api.md`'s example comes from the same
  table), "not needed with Home Assistant's Mosquitto app"; and "make the dashboard again" until
  the strategy of #169.
* **`--review CTID`** (new): the same loop against the *installed* version, without packages,
  staging or switch. When an answer writes, the old file is kept as
  `config.toml.<version>.review` and the service restarts. *Settings* points to it for "decide
  now".
* **`--reconfigure`** ends with the same review of the installed version, because it re-renders
  the file anyway.
* **Without a terminal and with nothing open, `--update` behaves as today**, without whiptail
  (`test_update_needs_no_whiptail`, `tests/test_installer.py:1652`).

### What the administrator sees

**The frontend.** Interactive means: standard input is a terminal and no `--answers` was given.
Then whiptail when installed, else numbered plain prompts. **The language** is the file's
`[homeassistant] language`: the program writes its texts in it and names it in the header line,
and the installer loads that language pack (digest checked) for whiptail's buttons. No language
menu.

**Which items are asked.** An item that grants, widens or deletes, and every `deviates`,
`offered` and `deprecated` item, gets its own dialog. A neutral `changes_now` item is not asked
one by one: the final screen lists it, with *Go through the changes one by one* to keep an old
behaviour. An update whose only items are neutral changed defaults is one screen.

**Many items.** With four or more items to ask, one menu comes first:

```text
┌──────────────────────── meshcore-home-gateway 2.4.0 ────────────────────────┐
│ 5 settings to decide before the update to 2.4.0.                            │
│                                                                              │
│ 3 grant nothing, widen nothing and delete nothing:                           │
│   [diagnostics] log_level, [mqtt] password_file, [airtime] burst_s           │
│ 2 need your answer:                                                          │
│   [commands] free (gives a right), [maintenance] enabled (gives a right)     │
│                                                                              │
│          Take the 3 recommended values, then ask about the other 2           │
│          Ask about each of the 5                                             │
│          Decide later: ask again at the next update                          │
│                                                                              │
│                    <Ok>                          <Cancel>                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

**One item** (a menu of 20x78 with its choices as the list: 10 lines of text above 3 choices):

```text
┌──────────────────────── meshcore-home-gateway 2.3.0 ────────────────────────┐
│ 2 of 2: How much the journal records                                         │
│ [diagnostics] log_level                                                      │
│                                                                              │
│ Yours:        "debug"                                                        │
│ Recommended:  "info"      since 2.0, privacy                                 │
│                                                                              │
│ "debug" writes the texts of the messages, the positions and the keys         │
│ into the journal, which keeps them for weeks.                                │
│ More: docs/configuration.md#diagnostics                                      │
│                                                                              │
│          Take the recommended: "info"                                        │
│          Keep mine: "debug" (not asked again)                                │
│          Decide later (asked at the next update)                             │
│                                                                              │
│                    <Ok>                          <Cancel>                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

The reason reuses the words of today's warning (`config_checks.py:197-207`).

* `--notags`: the items alone, the answer word as the hidden tag, as 2.2.1's menus do.
  `--default-item` carries the preselection. `--` comes before the text, as in every whiptail
  call of the installer.
* An item that grants says what: "Gives every sender in `[commands] senders` free commands"; one
  that widens says whose data: "Publishes the names of other people's radios". The words come from
  the effect and the reason.
* *Cancel* or Esc cancels the update: the staged file goes, the old version keeps running,
  nothing was written.
* The program fits each text to the lines its menu shows (10 above 3 choices, 9 above 4) and
  each label to 72 columns, in every language. Its tests use 2.2.1's model of whiptail's wrap.

**The final screen** (a menu of 24x78: 14 lines of text above 3 choices; *Update now*
preselected):

```text
┌──────────────────────── meshcore-home-gateway 2.3.0 ────────────────────────┐
│ Update container 105 from 2.2.0 to 2.3.0.                                    │
│                                                                              │
│ Changes with this update:                                                    │
│   [messages] clean_received     true (was false)      security               │
│ Your answers:                                                                │
│   [contacts] heard_nodes        "keys"   kept, not asked again               │
│   [diagnostics] log_level       "info"   taken                               │
│ After the update:                                                            │
│   Home Assistant's broker user needs one more line (not with the             │
│   Mosquitto app): topic write meshcore/radio1/situation/set                  │
│   Make the dashboard again: docs/home-assistant.md#dashboard                 │
│ The configuration before is kept as config.toml.2.2.0.                       │
│                                                                              │
│          Update now                                                          │
│          Go through the changes one by one                                   │
│          Cancel                                                              │
│                    <Ok>                          <Cancel>                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

A longer list ends in "and 3 more (in the log)". *Go through the changes one by one* is offered
only while neutral changed defaults were not asked.

**Plain prompts**, without whiptail, show the same texts:

```text
meshcore-home-gateway 2.3.0, 1 of 2: Names, signal and time of radios nearby
  [contacts] heard_nodes
  Yours:        "keys"
  Recommended:  "full"      since 2.2, feature
  Keeps the names, signal and time of the radios heard nearby, for Add heard node.
  Taking it publishes more of other people's data.
  More: docs/configuration.md#contacts
  1  Take the recommended: "full"
  2  Keep mine: "keys" (not asked again)
  3  Decide later (asked at the next update)
Choice [2]:
```

This item widens, so *Keep mine* is preselected: an empty line keeps `"keys"`.

Digits, so no translated letters. An empty line takes the preselected answer, anything else asks
again, and the end of input (Ctrl-D) cancels.

### Unattended runs

An unattended run is one without a terminal (cron, `ssh` without `-t`) or one with
`--answers FILE`.

| Item | No answer | `review=recommended` | `review=keep` | `review.KEY=…` |
| --- | --- | --- | --- | --- |
| `changes_now`, neutral | applies, as released | applies | the old value written, kept | as named |
| `changes_now`, grants or widens | the old value written, not kept | the old value written | the old value written, kept | as named |
| `changes_now`, deletes | applies, as released | applies | the old value written, kept | as named; `export` exports first |
| `deviates`, `offered`, `deprecated`, neutral | unchanged, nothing recorded | taken | kept | as named |
| the same, grants, widens or deletes | unchanged, nothing recorded | **unchanged** | kept | as named; `export` exports first |

* `review` takes `recommended`, `keep` or `later`; `review.<key>` takes `recommended`, `keep`,
  `later` or, for an item that deletes, `export`. They join the installer's answers
  (`INSTALLER_KEYS`, `lxc.sh:53-54`). An unknown review key, or one that is no item of this
  review, stops the run before anything is written. `review=later` keeps a terminal run silent.
* Configuration keys in an answers file are ignored by `--update`, as today, now with one log
  line: passing an installation's answers again must not reconfigure.
* The log names each open item in English, with how to decide it: "review:
  [messages] clean_received is false, recommended true since 2.3.0 (security); unchanged. Decide
  with --review 105 in a terminal, or review.messages.clean_received=recommended in --answers".
* A changed default that deletes applies unattended because the release decided it, as 2.2's purge
  did. No changed default of 2.3 deletes stored data, but taking one of its recommendations does:
  `messages.publish_channels = "targets"` from `"all"`. So 2.3 builds the count and *Export first*
  of #186's design for every item that deletes (*Items that delete*, above). An unattended run
  takes such an item only with its own line, `review.messages.publish_channels=recommended` or
  `=export`.

### The install steps move into the program (amends ADR 0006)

The switch moves from the installer's shell into a program subcommand, run by the staged zipapp
as root: `meshcore-home-gateway install switch --unit FILE [--config-new FILE]`. It puts the
program under its versioned name, points `current.pyz` and `/usr/local/bin/meshcore-home-gateway`
at it, installs the units, reloads systemd, and then switches the configuration as step 6 says.
It refuses unless run as root from a file in `/usr/local/lib/meshcore-home-gateway/`. It imports
what it needs before it renames its own file: zipimport reads a module by its path.

* **Why.** In 2.3 the gain is one tested switch of the program and the configuration, with their
  modes and owners right: Python tests it as units, the shell only through stubs. The one later
  reuse is the updater of 2.5 (installing from Home Assistant), which needs the switch without the
  installer. ADR 0039's check unit needs none: it runs without root and only fetches
  `release.json`, and the installer's own update installs its unit file.
* **What stays in the shell:** packages, the journal limit, the user, staging (the download, the
  digests, the build of a source checkout), `check-config` and the dialogs.
* **Bytes.** The installer keeps its five switch lines (`lxc.sh:866-870`) for a staged version
  before 2.3. `--release X.Y.Z` installs any release, and the installer's options only grow within
  a major ([ADR 0023](0023-public-interfaces-kept-by-a-snapshot.md)). So the move saves no bytes
  in 2.x. It keeps what the switch gains (the configuration, more units, a roll back) out of the
  installer. Until 3.0 there are two switch paths, the shell's for releases before 2.3 and the
  program's, and both are kept and tested. The old lines go in 3.0.
* **The budget.** About 16.6 KB remain after 2.2.1. The review's loop (whiptail and plain, the
  bulk menu, `--review`, the answers keys) is the largest bidder of 2.3. Its texts come from the
  program, so the installer's catalog grows by a few button labels only. The size test guards the
  rest.

### Home Assistant: what the household sees

**The rule.** The household never needs the installer to learn about a recommendation. The
administrator needs it only to change the file. Home Assistant applies nothing.

**The `config` document** gains a `review` section (additive,
[ADR 0015](0015-provider-neutral-documents-on-the-bus.md)), computed by the service at the start
and at each radio connect, when the document is published anyway:

```json
"review": {"version": "2.3.0",
           "open": [{"key": "messages.clean_received", "kind": "deviates", "current": false,
                     "recommended": true, "derived": false, "since": "2.3.0",
                     "concern": "security", "effect": "neutral"}],
           "kept": [{"key": "contacts.heard_nodes", "since": "2.2.0"}],
           "other_open": 0}
```

* Only keys of the document's allowlist (`config_view.py:42-96`) are listed; others are counted
  in `other_open`. Whether the broker password is inline is not every user's business.
* `derived: true` with `recommended: null` means "let the gateway decide".
* `changes_now` items are not in it: after the update the new default simply is in effect.
* Codes and values only, never prose. The texts come from the catalogs by key.
* The document's 15 KiB bound counts the section; the targets give way first, as today.
* No new entity: the section becomes attributes of the existing *Configuration* sensor
  (`mqtt/discovery.py:336-347`), so nothing is announced or removed.

**Marks on *Settings*** (today's view, `dashboard.yaml:2042-2199`; then the redesign of #170 and
the strategy of #169, from the same codes):

* At the top, while something is open: "2 settings differ from what this version recommends.
  Nothing is wrong: your administrator decides at the next update." An info line, never a warning
  colour: an open recommendation is no problem now (ADR 0031).
* The row of an open key, in its table *Setting / In this home / What it means*: under the value,
  "Recommended since 2.3: on"; under the meaning, "Why: removes control and direction characters
  from strangers' names and texts. Your administrator decides at the next update, or now with the
  installer's `--review`." For a widening or granting item it adds what taking it would allow.
* A kept key: "Kept on purpose by your administrator (recommended since 2.2)." The mark stores
  the recommendation's `since`, not the version that asked.
* `other_open`: "1 more recommendation concerns a setting this page does not show. Your
  administrator's next update asks."
* *Radio and airtime*: "bursts of 13 s, set by the gateway for this radio's settings" from
  `state` when `airtime_burst_source` is `derived`.
* The generator writes each key's title and reason into the dashboard in its language. A key it
  does not know (a newer gateway) shows "Recommended since 2.4: value" without a reason.

**Why no button.** A button would need a root writer of `config.toml`, fed by a request from the
service. Every Home Assistant user could press it, and even a tightening can delete data
(`publish_channels`) or break commands. Recommendations change a few times a year, at updates,
when the administrator is present anyway. Revisit with updates installed from Home Assistant,
whose install call only administrators may make.

### Public interfaces (all additive, ADR 0023)

| Interface | Addition |
| --- | --- |
| Configuration | `[settings] kept`; the default of `airtime.burst_s` becomes derived (*Changed*) |
| Command line | `config review` with its options, JSON keys and `--list` line kinds; `install switch`; `broker acl` |
| MQTT | `config.review`; `state.airtime_burst_s` and `state.airtime_burst_source` |
| Discovery | *Airtime available* gains the attributes `burst_s` and `burst_source` |
| Installer | `--review CTID`; dialogs in `--update` and `--reconfigure` in a terminal; the answers `review` and `review.<key>`; three preferences left out unless set, at a new installation and at `--reconfigure` |

Asking in `--update` changes behaviour only in a terminal with something open. Scripts without a
terminal behave as today, plus log lines and the rules above.

### Consequences

* Good, because no update from 2.3 on needs a hand edit for a changed default or a recommendation.
  The update into 2.3 is the first that asks: `clean_received` is its first item.
* Good, because nothing that grants, widens or deletes changes without a person's answer, and an
  unattended run is predictable.
* Good, because one table feeds the parser's answer types, the review, the docs table, the
  changelog check, the release plan and Home Assistant. A changed default cannot ship without its
  history entry, changelog line and docs row.
* Good, because the household sees what is open, why, and who decides, without a shell.
* Good, because the burst fits the radio's preset without a setting, and the household sees that
  the gateway set it.
* Good, because the configuration switch, the kept copy and the units live in one tested program
  step, which the updater of 2.5 reuses.
* Bad, because a file with `[settings]` is refused by versions before 2.3. A roll back needs the
  kept `config.toml.<old version>`; `docs/deployment.md` says so, and `--rollback` (2.5) restores
  it.
* Bad, because re-rendering drops comments. The old file is kept, and the final screen says so.
* Bad, because every key needs an entry and every changed default a history line, a reason and
  its translations. The guard tests enforce it; it is work at every release that changes a
  default.
* Bad, because a neutral changed default applies unattended, and a deleting one too, as releases
  do today. The log names each; the release's changelog decided it.
* Bad, because an administrator who keeps a security recommendation keeps the weaker setting. It is
  recorded and shown on *Settings* as kept on purpose.
* Bad, because the installer grows by the review's loop, within the 16.6 KB left. The legacy
  switch lines stay for older releases until 3.0, so two switch paths are kept and tested until
  then.
* Bad, because a new key whose default shows more than before asks existing installations, and an
  unattended update keeps the old behaviour. In 2.3 that is `last_known_days`: an unattended
  update shows no last known positions until someone takes the default.
* Bad, because the marks need a new dashboard until the strategy of #169 (2.5).
* Neutral, because at slow presets the gateway's short-term peaks grow, within the radio's own
  limiter.

### Trust boundaries

* **B6, the container.** The review runs the staged program as root, as `check-config` does
  today. It writes the configuration as `config render` does (0640, the service's group), and the
  password never leaves the container: the host's shell gets only `KEY=word` decisions, and no
  output prints a secret (`mqtt.password` never; the password-file item says "in the
  configuration file"). The service gains no right: it still cannot write its file. `kept` changes
  no behaviour.
* **B7, the project.** Nothing new is trusted. The staged program is the one verified against the
  release's `SHA256SUMS` and the pinned digest before it reviews anything.
* **B4 and B5, Home Assistant and its users.** The `review` section adds allowlisted keys and codes
  to a document every user reads, and nothing they could change. Home Assistant controls nothing
  new (ADR 0031).
* **B3, the broker.** No new topic or subscription. A new ACL line is named in the final screen,
  never written.
* **B1 and B2, the air and the radio.** The review never talks to the radio. The derived burst
  changes only the gateway's own short-term peaks.

The threat model's B6 and its residual risks say so in this pull request, marked as proposed for
2.3: the staged program reviews, an unattended run without a review answer changes nothing the
file sets, and what grants, widens or deletes needs an answer naming it; a kept security
recommendation keeps the weaker setting.

### Confirmation

* **Registry** (`tests/test_settings.py`): the guard tests above; the derived burst per preset of
  the table of *Radio presets*, the cap, the floor, an explicit value untouched, `resize` keeping
  and never adding tokens, a start before the connect using the stored burst.
* **`config review`** (`tests/test_config_review.py`): golden JSON in English, German and `xx` for
  fixtures of each kind and effect; `kept` hides its `since` and not a later one; a pre-release
  counts as its release; no changed defaults without `--from-version`; the pinned-default hint;
  every `--list` text within its lines and columns in every language, by 2.2.1's wrap model;
  backslashes round-trip through `printf %b`; the item of a deleting recommendation with its count
  from a fixture history and *Export first*; an unpinned derived burst at a slow preset is a
  `changes_now` item, and at EU/UK Narrow no item.
* **`--apply`**: each answer per kind; idempotence (the review of the result lists only *later*
  items); a property test that keys not named keep their values; an unknown key or word exits 2
  and writes nothing; mode and group as `config render`; `kept` round-trips.
* **No secret**: a property test with generated passwords, user names and hosts over `--json`,
  `--list`, `--plan` and the `review` section. No mesh text in any item.
* **Consent rules**: the registry test over every allowed value of each recommended key with a
  finite set of values (above); nothing non-neutral preselected; a new key's first default with
  its effect against the version before it (`last_known_days` from 2.2: *Keep the old behaviour*
  preselected, `0` written unattended); the unattended table row by row.
* **Codes and texts**: the guard test of
  [design §6.12](../design.md#612-translations) covers the review's `kind`, `concern` and
  `effect` and `airtime_burst_source` in every shipped language.
* **Installer** (`tests/test_installer.py`, the `pct exec` stub answering `config review` from an
  environment variable, as it answers `probe --list` from `PROBE_PROFILE`): nothing open, no
  dialog, the file byte for byte, no whiptail needed; whiptail flows with `--default-item`, the
  bulk menu from four items, the final screen, *Cancel* leaving the old version and removing the
  staged file; plain prompts in a pseudo terminal (empty line, digit, wrong answer, end of input);
  the unattended table; a staged version without the command behaving as today; the switch order;
  `config.toml.<old>` with its mode and group, and no copy when nothing was written; Ctrl-C
  leaving everything as it was; the dialogs in `xx`; the size test; `shellcheck`.
* **Pinning**: the answers a new installation sends hold none of the three preferences unless
  answered; `--reconfigure` of a file without them leaves them out, of one that sets them keeps
  them, and writes one whose shown dialog got another answer.
* **`install switch`**: run from a built zipapp in a temporary root, the program before the file,
  its own rename last.
* **Home Assistant**: contract tests on 2026.10 and 2026.11 render the marks from documents with
  open, kept and counted items; the allowlist test covers `review`; the browser check at 360x800
  and 1280x800 in every language.
* **Docs and release**: `tools/recommendations.py --check`; the changelog test; the release-plan
  check of a changed default without history (a fixture tag in a temporary repository, as the
  release-plan tests do).
* **Manual**: `tests/manual/installer_dialogs.py` drives an update with three items in the real
  whiptail; the hardware checklist gains an update from the previous release with one answer of
  each kind.

## Pros and Cons of the Options

### The new version reviews against a registry

* Good, because the version that knows what changed decides what to ask, before it is active.
* Good, because the consent rules follow each change's effect, not a blanket policy.
* Good, because the logic and its texts live in the program, which has no size limit and one
  catalog with Home Assistant.
* Bad, because it adds a registry, a subcommand and an installer loop to maintain.

### As today: the changelog and hand edits

* Good, because it costs nothing in code.
* Bad, because every minor release asks for `sed` in a shell right before the update, and an
  unattended update applies whatever the release decided, grants included.
* Bad, because the household does not learn of a deviation at all.

### dpkg's way: always keep

dpkg's conffile prompt defaults to "keep your currently-installed version", and `--force-confold`
keeps it without a prompt (`dpkg --force-help`, dpkg 1.22.6).

* Good, because it never changes a value the administrator might own.
* Bad, because a pinned default (the installer wrote it, nobody chose it) never moves, and a
  tightening the project recommends needs an extra keystroke every time.
* Bad, because dpkg compares whole files; here each item is one explained setting, so a reasoned
  preselection of what only tightens is safe.

### Always the recommended

The draft of #185 (`zerotouch.md` §6.1).

* Good, because it is the fewest keystrokes.
* Bad, because a recommendation that grants (free commands, the maintenance port, updates from
  Home Assistant) would be one Enter away, and unattended runs would take it with nobody present.
  That contradicts the maintainer's rule that security-relevant settings never change without
  consent.

### Migrate silently, or tie defaults to the first installed version

* Good (Zigbee2MQTT): mechanical migrations need no question; it keeps a backup and writes notes
  (`lib/util/settingsMigration.ts`, `master` at 3c841fdf).
* Bad (Zigbee2MQTT): its notes go to a log nobody reads, and a changed meaning is not a mechanical
  migration.
* Bad (NixOS `system.stateVersion`): the meaning of a file would depend on a hidden stamp
  (`nixos/modules/misc/version.nix:227-241`). The project's rule is that a file keeps its meaning,
  and the installer writes a value when new installations should differ.

## More Information

### Amended records

Each carries a dated amendment line of 2026-11-15 that names this record as a proposal for
2.3.0a1:

* [ADR 0006](0006-deployment-in-an-unprivileged-proxmox-container.md): `--update` reviews the
  settings with the staged version and asks in a terminal; it writes the configuration only on an
  answer and keeps the old one as `config.toml.<old version>`; the program's `install switch`
  switches the program and then the configuration; `--review CTID` is added.
* [ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md): `burst_s` is derived
  from the radio's preset when the file does not set it, capped at one hour's budget.

### Decisions

Decided by the maintainer on 2026-11-15, as recommended in the 2.3 plan
([design §13.1](../design.md#131-release-23-the-foundations-and-updates-that-ask)) and in the
analyses of #174, #181, #185 and #186:

| Decision | Outcome |
| --- | --- |
| The release shape (decision 1 of the 2.3 plan) | the registry, the review and the derived burst in 2.3.0a1, with ADR 0034's text module; the update into 2.3 is the first that asks |
| The consent rules (decision 5) | as listed above |
| Where "keep mine" is kept | `[settings] kept`, a list of `KEY@X.Y.Z` |
| Taking a recommendation equal to the new default | removes the line |
| The dialogs | whiptail when installed, numbered prompts otherwise, nothing when nothing is open; *Decide later*; the bulk menu; `--review CTID`; the language from the file, without a menu |
| The old file and comments | written only on an answer; `config.toml.<old version>` kept; re-rendered |
| Home Assistant's part | open and kept recommendations shown read only; no button that writes the file |
| The derived burst (#181) | derived from the preset, capped at one hour's budget; the effective value and its source in `state` |
| The in-container install steps | moved into the program, as the 2.3 plan proposed |
| The band-derived duty cycle (decision 11) | a default the file can change, not a recommendation by region |

Refinements of this record, for the review of this pull request:

* *Grants* and *widens* are two effects, not one flag as in #186's design: the consent rules
  speak of what "grants" and of what "widens access". The administrator reads which; every rule
  treats them alike.
* A neutral changed default is listed on the final screen instead of a dialog of its own, with *Go
  through the changes one by one*. #186's design asked each one.
* An item is listed only when taking it changes the value in effect for this installation (the
  derived burst stays 10 s at EU/UK Narrow, so most updates into 2.3 do not ask about it).
* The move of the install steps saves no installer bytes in 2.x (above); the 2.3 plan counted
  2.9 KB. Its gain is the tested switch, reused by the updater of 2.5, not by ADR 0039's unit.
* *Widens* counts the household's own data too, when it is logged or leaves the host. #186's
  design spoke of "widens access"; the first draft of this record narrowed it to other people's
  data, which would have preselected the update check.
* An effect belongs to a move from the value in effect, and a new key's first default carries
  its effect against the version before the key. So `last_known_days` and the update check ask
  existing installations, which decision 6 of the 2.3 plan foresees for the check ("offered to
  existing ones by the review").
* Every item that deletes offers *Export first* with its count, not only a changed default: the
  `publish_channels` recommendation deletes in 2.3.
* The installer stops pinning at `--reconfigure` too, not only at a new installation.
* The registry's histories start at 2.0.0: `clean_received`, `heard_nodes` and `publish_channels`
  came with 2.0.0a1, not 1.0.0 as the analyses wrote.

### Open decisions

Two decisions of the 2.3 plan are still open
([design §13.1](../design.md#131-release-23-the-foundations-and-updates-that-ask)). This record
depends on neither.

* **Household overrides** (settings increment C, #174; decision 2): `settings.json`,
  `config/set`, rows Home Assistant may change, with *Undo*. Recommended: defer until a setting
  that households change often needs it. The name and the power are then changed through the
  maintenance port (2.4), the change is reported after every connect, and a new name is announced
  by the gateway. For the maintainer: building it costs 3,500–5,000 lines for the two settings the
  analysis expects households to change (the radio's name and its transmit power), and it would
  not remove the update's hand edits, which concern the administrator's file. The design stays on
  file, and nothing here blocks it: a `row` field joins `Key`, `[settings] changeable` joins
  `kept`, and a row with a recommendation would show *Use recommended*.
* **The upstream firmware route** (decision 10) concerns
  [ADR 0035](0035-the-companion-state-model.md), which records it. It does not touch the
  settings.

### Related records, decided elsewhere

* [ADR 0034](0034-untrusted-text-from-the-mesh.md): the `clean_received` flip, the review's first
  `changes_now` item.
* [ADR 0035](0035-the-companion-state-model.md): `[telemetry] last_known_days` and
  `rest_radius_m` get registry entries. `last_known_days` recommends its default from 2.3.0, as
  every key whose default changed, and widens; `rest_radius_m` has no recommendation.
* ADR 0039 (2.3.0a1, to be written): the daily check in its own sandboxed unit (decision 6 of
  the 2.3 plan), `release.json` built from this registry, and the update entity whose summary
  says what the update will ask. Referenced here, decided there.
* Later records: the maintenance port (2.4, an `offered` item that grants), installing updates
  from Home Assistant (2.5, reusing `install switch` and the unattended table).

### Revisit when

* updates are installed from Home Assistant: whether its administrators may take neutral
  recommendations there;
* household overrides are decided: rows then show their recommendation with *Use recommended*;
* 3.0 is planned: migrations and acceptances as review items, the legacy switch lines go,
  names canonical whatever `clean_received` says;
* the installer has less than 4 KB left: move `plan_text` and the summary's texts into the
  program;
* whiptail changes its layout (the platform note's check);
* a release deletes stored data at its first start: check that the count and *Export first* of
  2.3 cover its case.

### Sources

The gateway at `b02a3d8` (2.2.0): `src/meshcore_home_gateway/config.py`, `config_view.py`,
`cli/config_cmd.py`, `status.py`, `state.py`, `outbox.py`, `core/budget.py`, `mqtt/discovery.py`,
`dashboard.yaml`; `deploy/proxmox/meshcore-home-gateway-lxc.sh`;
`deploy/systemd/meshcore-home-gateway.service`; `tests/test_config.py`, `tests/test_installer.py`,
`tests/test_installer_language.py`, `tests/test_architecture.py`, `tests/test_docs.py`,
`tests/public_interface.py`, `tools/public_interface.py`, `tools/platform_notes_index.py`;
`docs/deployment.md`, `docs/mqtt-api.md`, `docs/configuration.md`, `docs/development.md`
(compatibility); `CHANGELOG.md`. The branch `fix-2.2.1` at `96e5ee0`: the installer's size and
menus, `core/airtime.py`, `outbox.py`, `tests/test_dialog_texts.py`, `docs/platform-notes.md`
(*whiptail*). debconf 1.5.86ubuntu1 (`/usr/share/perl5/Debconf/ConfModule.pm`), dpkg
1.22.6ubuntu6.6, ucf 3.0043+nmu1 and whiptail 0.52.24-2ubuntu2, as installed. Home Assistant
core 2026.10.2 (`components/mqtt/const.py`, `components/mqtt/update.py`). Zigbee2MQTT `master` at
3c841fdf; nixpkgs `master` at 58a4d239. The analyses and the maintainer's decisions on #174,
#175, #179, #181, #185 and #186 (2026-11-15), and the 2.3 plan
([design §13.1](../design.md#131-release-23-the-foundations-and-updates-that-ask)).

Related: [ADR 0015](0015-provider-neutral-documents-on-the-bus.md),
[ADR 0020](0020-managed-radio-profile-for-the-observer.md),
[ADR 0023](0023-public-interfaces-kept-by-a-snapshot.md),
[ADR 0031](0031-household-chat-problems-and-settings-on-the-generated-dashboard.md),
[ADR 0032](0032-heard-nodes-kept-with-name-signal-and-time-by-default.md),
[ADR 0033](0033-a-zero-hop-advert-of-the-gateways-radio-on-request.md).
