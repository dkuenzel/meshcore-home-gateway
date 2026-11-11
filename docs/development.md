# Development

How to work on meshcore-home-gateway. The rules (design first, test first, evidence for protocol
facts) are in [`CONTRIBUTING.md`](../CONTRIBUTING.md); [architecture.md](architecture.md) is the short
way into the code.

## Set up

```sh
make venv    # .venv with the hash-pinned tools from requirements/dev.txt and the package (editable)
make hooks   # pre-push hook: make check on the commit to push (refuses uncommitted changes)
make matrix  # tests with each Python from 3.11 to 3.14, each in its own .venv-python3.X
make help    # all targets
```

Requirements: Linux with systemd (for `make unit-check`), Python ≥ 3.11 with `venv`
(`python3-venv` on Debian and Ubuntu), GNU make, git, `mosquitto` for the integration tests, GNU
gettext (`msgfmt`) for the tests of the message catalogs (without it they fail;
`MESHCORE_HOME_GATEWAY_SKIP_GETTEXT_TESTS=1` skips them deliberately), `uv` only to update the
lock file, `g++` and `libssl-dev` only for the firmware tests
([testing.md](testing.md#tests-against-the-real-firmware)).

## Layout

```text
src/meshcore_home_gateway/
  core/          pure rules and calculations (no I/O): text, airtime, budget, contacts, commands, model
  protocol/      companion protocol: framing, codec (pure)
  radio/         transports (serial, TCP) and the companion session
  mqtt/          topics, payloads, Home Assistant discovery, paho adapter
  ports.py       the ports: what the application asks of the radio and the bus
  wiring.py      puts the service together: the adapters, the parts and who hears of what
  bridge.py      application service: radio supervision, the radio's events, the tasks
  outbox.py      the outgoing queue and its delivery; inbox.py: received messages, commands
  announcer.py   announcements to Home Assistant; status.py: the state document
  config.py      configuration loading, validation, rendering
  state.py       persistent state (command ledger)
  systemd.py     sd_notify
  cli/           command line: the parser and main, a module per group of commands
  texts.py       Text and TextError: texts made where no language is known (pure)
  catalog.py     the PO reader and the MO writer (pure)
  i18n.py        languages, their choice and the catalogs (Translations, below)
  locale/        the template of the texts and one PO file per language
  dashboard.py   the Home Assistant dashboard, from the template dashboard.yaml
deploy/          systemd unit, Proxmox installer and its catalogs, example configuration
docs/            design, decisions, protocol, API, operations
tests/           pytest suites, emulated radio, broker fixture; tests/manual: checks run by hand;
                 tests/firmware: the real firmware on a simulated air (make firmware-test)
tools/           build and release helpers: zipapp, release plan, public interface comparison,
                 the pinned fetch of the firmware, the translation templates and checks (i18n.py),
                 the browser check and its gateway on the emulated radio (docs/testing.md)
.github/         CI workflow, Dependabot, issue forms, pull request template
```

## Test-driven workflow

1. Write or extend a test that describes the behaviour and fails.
2. Implement until it passes; keep core modules free of I/O.
3. Refactor; `make lint test` stays green.
4. Update the documentation of the interface you touched (`mqtt-api.md`, `configuration.md`,
   `home-assistant.md`, `protocol.md`) and `CHANGELOG.md`; after a change of a public interface,
   `make interface` ([Compatibility](#compatibility)).

### Text from the mesh and the bus

Names, message texts, labels and links come from strangers on the air or from any client of the
broker. The code keeps them data everywhere they go, and new code follows the same rules:

* **Bounded at the entry:** a frame holds at most 176 bytes, a name 32, a request 1 KiB; what the
  gateway echoes back stays within such a bound (a refused request's alias: 64 characters).
* **Bounded in rate:** what a sender or a channel makes the gateway publish and store (events,
  history rows), what it logs about malformed frames and refused requests.
* **Never code:** never part of a format string, a Jinja template, a shell command, SQL (always
  parameters) or a file path. Discovery documents are written from constants; the mesh's values
  are only what a template reads.
* **Logs:** names, labels and texts with `%r`, so that they cannot forge a line or steer a
  terminal; message texts only at debug level.
* **Terminals:** the command line prints them through `_safe_text` or `_json_text`.
* **Home Assistant:** Markdown cards print every value through the macro `m()`, a message's
  text through `mt()` (Translations, below); nothing from the mesh becomes an entity ID, a unique
  ID or an event type.
* **MQTT:** JSON only; texts are passed on as received (`docs/mqtt-api.md`: consumers treat them
  as plain data, never as markup), or with `[messages] clean_received` without control and
  direction characters, as outgoing texts are; commands are judged on the text as received.

### Adding a protocol message

1. Find the command or response in the firmware (`examples/companion_radio/MyMesh.cpp` at the
   release named in [`protocol.md`](protocol.md)) and add its layout to `protocol.md` with the
   source lines.
2. Add a golden frame to `tests/test_codec.py`, derived from the source, not from our encoder.
3. Implement the encoder or decoder in `protocol/codec.py`.
4. Teach the emulated radio in `tests/radio_emulator.py` to answer it, then use it in the session.

### Adding a configuration setting

`config.py` (dataclass and validation), its answer type in `config._ANSWER_TYPES` (every setting
needs one: `--reconfigure` writes the configuration back from its answers, and
`tests/test_config.py` checks that no setting is missing), `docs/configuration.md`,
`deploy/config.example.toml`, a test in `tests/test_config.py`, the setting in `ANSWERED` of
`tests/public_interface.py` (`config answers` must print every setting), and `make interface`. If
the installer should ask for it, also the dialog in `deploy/proxmox/meshcore-home-gateway-lxc.sh`.
A feature behind a switch gets the switch turned on in `MAXIMAL_TEXT` of
`tests/public_interface.py`, so that the snapshot records its surface.

## Translations

Texts for people are translated through gettext catalogs
([design §6.12](design.md#612-translations)); translators read
[translations.md](translations.md). A text is marked where the code holds it, as a string
literal, so that `make translations` finds it:

* `tr("…", name=value)` in the command line (`cli/`): a text the command line shows, in its language. `trn("…",
  "…", count, name=value)` for a count; `{count}` is filled in. `tr_help("…")` for a help or
  description text: argparse %-formats it later, so it is never filled.
* `N_("…")` marks a text in a table that is shown elsewhere (`tr(table[key], …)`); `N_ha("…")`
  the same for a Home Assistant name.
* `ha_name("…", name=value)`, from `i18n.translator(config.homeassistant.language).ha_name`, for
  every name sent to Home Assistant (`msgctxt "home assistant"`).
* `Text("…", name=value)`, `TextError("…", …)`, `ConfigError("…", …)` and `LinkError(reason,
  "…", …)` for a text made where no language is known: its `str()` is the English text (logs,
  MQTT, tests), and the command line shows it translated (`tr("…{detail}", detail=exc.text)`).
* In the dashboard's template, `_("…")` with a JSON string inside (`msgctxt "dashboard"`).

The rules:

* A whole line or sentence is one text, with named fields whose values the call gives; never an
  f-string, `+`, `%` or `.format()` in a text, never a sentence glued from parts. The program's
  prefix (`meshcore-home-gateway: `) stays outside every text. Commands, paths and option names a
  user copies are fields where the line allows it.
* A `# Translators:` comment directly above the call or its statement goes into the catalog: say
  where the text is shown and what a field holds. Every text of the dashboard with a field has
  one. In a card of the dashboard's template it is
  `{#- Translators: … -#}` on the line above the text (a `#` line there is card content); never in
  a GitHub-alert line, which it would join to the next.
* Never translate a log call, machine output (`--json`, `--list`, the results with `--wait`,
  `config answers`, `--version`), a value of MQTT or Home Assistant (entity IDs, event types,
  options, attribute keys), a configuration key, or data from the mesh, the radio or the user.
* Never name anything `_` (`cli.tcp_address` assigns it). Reach `i18n.translator`,
  `i18n.LANGUAGES` and `i18n.choose` through the module (`i18n.translator(…)`), never by
  importing the names: the tests replace them.
* After a change of a marked text, run `make translations`: it writes the templates, merges the
  catalogs, writes the installer's language packs and their pins. The tests fail until it ran.
  They run with a strict catalog that fails the lookup of any text the template lacks, so a
  forgotten mark shows as a failing test; a test marked `real_catalogs` looks up texts of its own.
* `tests/i18n_helpers.py` offers languages to a test: `speaking(monkeypatch, pseudo())` gives the
  pseudo-language `xx`, which marks every text (`‹…›`); `wide()`, `hostile()` and `percents()`
  give catalogs that would break a careless caller.

**The dashboard's template** (`src/meshcore_home_gateway/dashboard.yaml`) is YAML with a few
lines of its own, which `dashboard.py` reads:

* `# @each chats` … `# @end` repeats its lines once per chat page, `switches` and `removes` once
  per contact's switch or remove button. Inside, `@chats.title@` and the like are the item's
  fields. Each value matches a pattern first (`dashboard.FIELDS`): none is text from the mesh. A
  chat page, switch or button whose values do not match is left out, and the command says so.
  A directive may also stand inside a card's content, indented as its lines: there it repeats
  lines of the content, such as the keys of the contacts a list holds.
* `# @if trackers`, `# @if chats` (a list that is not empty) or, inside its `@each`,
  `# @if chats.write` (a field that is not empty), then `# @else` and `# @end`.
* `@gw@`, `@node@`, `@version@`, `@major@`, `@minor@`, `@guide@` (the guide of this
  version, for a link to one of its sections) and `@decimal@` (the decimal mark of the
  dashboard's language, `i18n.DECIMAL_MARKS`, which `num()` writes, rule 22) are filled in;
  `["@trackers@"]` and `["@chats@"]` become JSON lists, `["@models@"]` the pairs of each model of
  a contact's device, as the gateway names it in the dashboard's language, and its node type. A chat page in `["@chats@"]` says with
  `write` only whether it has a field: the entity IDs of its field and its *Send* button stand
  only in the guard of its footer (rule 3 below).

`tests/test_dashboard.py` checks these rules on the shipped file:

1. A marker is a whole YAML value or sits in a literal block (`content: |-`), never inside
   `{{ }}`, `{% %}` or a flow value. A field in a marker is `node`, `version`, `name` (the title
   of the item, only inside an `@each`), or a Jinja variable the card sets before it. A whole
   value takes `name` only.
2. Every Markdown card that prints a value starts its content with the macro `m()` (after the
   line that names a problem card's class, rule 9 below):
   `{%- macro m(x) -%}{{ (x or '') | string | regex_replace('[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u00ad\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]', '') | regex_replace('[\t\r\n\u2028\u2029]', ' ') | e }}{%- endmacro -%}`.
   It prints every value read from Home Assistant (states, attributes, the registry) through it,
   through `mt()`, or through a macro of the card, on a line that is one `<div>`, `<table>`,
   `<details>` or `<ha-qr-code` element, or a row of `<table role="presentation" width="100%">`
   elements. A macro prints only through `m()`, `mt()` and the macros above it, and a field is set
   from one of them or is a whole number. A mapping ends in `m(v)`, in `<code>` where the value is
   a code. `| e` appears nowhere else. GitHub-alert lines print only our texts and integers.
3. The mapping macros (`said`, `telemetry`, `what`) have one branch per value of the gateway's
   list (`DeliveryState`, `telemetry.STATES`, the `contact` events' types).
4. `on`, `off` and versions are quoted (YAML 1.1 reads `on` as true).
5. `_("` and `@word@` appear only as markers and placeholders.

The dashboard of 2.1 follows the numbered rules below as well. `tests/dashboard_rules.py` checks
them on the dashboard made for a household with one chat of every kind, and each finding names
its rule; the shipped template breaks none of them.

1. A value goes onto one `<div>`, `<table>`, `<details>` or `<ha-qr-code` element per line, or
   onto a line of `<table role="presentation" width="100%">` elements.
2. A mapping macro (`said`, `mark`, `why`, `what`, `telemetry`, `status`, `profile`, `part`,
   `place`, `problem`, `mon`, `kind`, `audience`, `node`) gives each branch a text, an unknown
   value through `m()`, and has a branch for each value of the gateway's list where there is one.
3. A condition holds no marker and no `m()`, and never `has_value()`. The entity ID of a contact
   appears only inside a card or row whose template condition tests, for that entity, that it
   exists, that it is not `unavailable`, its device's `via_device_id` and `serial_number`; for a
   field of the gateway's own, that its device is the gateway. A footer's condition names both
   its entities, each tested on its own, and its button is on its field's device.
4. No `condition: state`, except with an `attribute` on the radio's entity; no `state_not`.
5. No remove button as an entity row; a remove asks first, and its question names `{name}`.
6. A link goes to `./` and a page of the same dashboard, or to an anchor of the guide of this
   version (`v@version@`). A link to `./{{ x.path }}` or `./{{ m(x.path) }}` stands only in a
   card that loops over `["@chats@"]` with `for x in`.
7. No marker inside an HTML attribute.
8. Every card but a heading has a `<details>` last; a tile, entities, map or history-graph card
   may have it in a `text_only` card that follows it, and a chat's refused line has the help of
   the thread under it (rule 18); every page ends with such a card; what a mapping macro says is explained there or on *Help*
   (but the names of the months); *Help* names every page and every control.
9. The status line and the problem cards list the same problems in the same order: the branches
   of the status line's `problem()`, and the problem a card names on its first line,
   `{#- problem: <class> -#}`.
10. The counts in the tests follow the template.
11. No GitHub alert (`> [!`): a coloured box is a `<div><ha-alert>` line.
12. The tabs are `overview`, `messages` and `contacts`, without icons. *Overview*'s status line
    has one row of links under it, outside every `<details>`: *Map*, *Gateway*, *Settings* and
    *Help*, in this order; and a heading of *Contacts* has buttons to *Gateway*, *Settings* and
    *Help*.
13. A card that prints a message's text prints it through `mt()`; `m()` and `mt()` read the same
    in every card.
14. The three tabs' titles have at most 22 letters together in every language shipped, or the
    tabs do not fit a phone.
15. No condition has `enabled`, no number in a condition is a string.
16. A text or a name that quotes a label of the dashboard, a name that names a button's label
    (*Return, then Send*), and a text that names a label of `NAMED` by its words, quote marks or
    not (D-63: *Send*, *Contacts*, *Gateway*, *Settings*, *Ask for position and battery*, the
    controls and headings of *Contacts*), is listed in `QUOTES` with the labels it names, or in
    `PLAIN` where the words mean no label; and every language says each label as it translates
    it, its first letter in either case as the language's grammar wants (a German heading ends
    in its button's verb). A text that asks for the keyboard's key says
    the key before ↵ in a word of the name beside *Send*.
17. No text names a section of the guide; it links to its anchor instead. Only a line that
    starts with "For your administrator:" names Home Assistant's menus.
18. A chat page's footer is a `vertical-stack` of one text row whose three actions are `none`
    and a tile with a `button` feature. The refused line (a Markdown card that hides while empty,
    rule 21) is the card right above the thread, whose help it shares (rule 8): the frontend caps
    the footer at a quarter of the screen, and on top of the field the line pushed *Send* below
    a phone's screen; under the thread, the footer covered it until the page was scrolled (#143,
    [platform-notes.md](platform-notes.md#home-assistant-dashboards)).
19. `m()` removes the C0 and C1 controls but tab, LF and CR, and the invisible characters the
    gateway drops before it reads a contact link; never U+200C and U+200D.
20. No page has a `back_path`: the back arrow goes back to the page it came from.
21. No card of the made dashboard hides itself. The template marks a Markdown card that may be
    empty with `show_empty: false`, and the generator turns that into a visibility condition of
    the card's own content: Home Assistant's card wrapper takes a card that hides itself out of
    the page, and it does not come back until the page is loaded again; a card hidden by its
    conditions comes back once they hold.
22. A card prints a number through `num(v, digits=none)`, which it defines after `m()` and which
    reads the same in every card: the number with the dashboard's decimal mark in place of the
    point (`@decimal@`; German writes 0,4), a whole number without `.0`, rounded to `digits`
    places when given; anything else as `m()` prints it. No number is formatted with `'%.1f'`,
    `m()` prints a number only as a whole one (set from `| int` or `| count`), and a value whose
    key names a unit (`frequency_mhz`, `burst_s`) goes through `num()`. A message's text and a
    name keep their points. A Markdown card's template knows neither the viewer's language nor
    the number format of their profile
    ([platform-notes.md](platform-notes.md#home-assistant-dashboards)), so the dashboard's
    language decides when the dashboard is made. A fraction below 0.0001 comes out in Python's
    exponent form (`1e-05`).

A translation of a dashboard text is used only when `dashboard.unsafe()` finds nothing in it (no
Jinja, HTML, Markdown link or address, no line break); the catalog check applies the same test.

**The installer** has its own catalog (`deploy/proxmox/locale/installer.pot`). Its texts go
through `t 'text' name "$value"` (a dialog's text, `{name}` fields filled in), `tp context 'text'`
(a label: the buttons), `die 'text' …` for every error, and `$say 'text' …` in the two functions
that write a block both into the log and into a box (`_fill` for the log, `t` for the box). A text
is a single-quoted literal, or double-quoted without `$`, backtick and backslash; only `die`
passes a text it was given on (`t "$@"`). No `eval`, `source` or bash's `$"…"`: a pack is data,
and the extractor and a test refuse them. The functions live between the marker lines
`# --- messages` and `# --- end of messages`, which the tests cut out and run alone. The block
between `# --- language packs` and `# --- end of language packs` offers the languages and pins
their packs; `make translations` writes it. The language menu shows only while it offers more
than English. `knows_language` decides whether the program to configure reads
`homeassistant.language`: the checkout's own program (`OWN_PROGRAM`) always does, a version
before 1.1.0a4 never.

## Tool chain updates

`make lock` re-resolves the lock files in `requirements/` from their `.in` files with hashes and
takes the newest release of every tool that is at least `COOLDOWN_DAYS` (14) old: `dev.txt` (the
development tools), `release.txt` (the build tools alone, for the release build) and `audit.txt`
(pip-audit). The cutoff is the moment `COOLDOWN_DAYS` days before the run, given to uv as a
time: uv reads a bare date as the end of that day, so a release uploaded late on that day was
taken at 13 days old ([platform-notes.md](platform-notes.md#python)). `COOLDOWN_DAYS=0` takes
every release up to the run. The files are resolved for Linux alone
([ADR 0005](decisions/0005-runtime-dependencies-from-the-distribution.md)) and from inside their
directory: the form Dependabot writes, so that its pull requests change versions and hashes only
and need no `make lock` (SUP-004). `make lock UPGRADE=` writes them again with the versions they
pin.

`make ha-lock` does the same for the Home Assistant test environment
(`home-assistant/requirements.in`, one pinned `pytest-homeassistant-custom-component`, which pins
Home Assistant), with the versions that `home-assistant/overrides.txt` raises past known
vulnerabilities; run it in Dependabot's pull requests for that environment too, so that the
overrides hold (whether Dependabot applies them is not checked).
`make ha-lock HA_SERIES=2026.11` writes the lock of the second series,
`home-assistant/requirements-2026.11.txt`, from `requirements-2026.11.in` (Home Assistant's own
plugin, `pytest-homeassistant`; [testing.md](testing.md#home-assistant-contract-tests)), under the
same cooldown.
After a Home Assistant update, run `make ha-test` and adjust "tested with" in
`docs/home-assistant.md`. Review the diff like code.

`make audit` (CI; it needs the network) looks up every locked version in the Python Packaging
Advisory Database with pip-audit (SUP-005). A known vulnerability in a tool chain fails it. One in
the Home Assistant test environment, whose versions the pinned Home Assistant release decides,
warns: raise the version in `home-assistant/overrides.txt` and run `make ha-lock`.

GitHub Actions are pinned to commit SHAs; Dependabot proposes updates with the same cooldown.
`paho-mqtt` stays at the version of Debian 13 (ADR 0005), and Dependabot ignores it: raise it with
the deployment target. A changed lock file gives `make venv` a fresh environment, so that no
package the lock dropped stays installed. The firmware that the firmware tests build is pinned in
`tests/firmware/sources.toml`, which Dependabot does not read: move it with a firmware release
([testing.md](testing.md#testing-another-firmware-release)).

## Compatibility

From 1.0.0 on four interfaces are public: the MQTT API, the configuration format, the command
line and Home Assistant discovery
([ADR 0023](decisions/0023-public-interfaces-kept-by-a-snapshot.md)). Within a major version
each one only grows: what works with X.0.0 works the same with every X.Y.Z. An incompatible
change waits for the next major version, and its changelog entry starts with `**Breaking:**` and
says what users must change. Pre-releases promise nothing beyond the last release.

**MQTT API** ([mqtt-api.md](mqtt-api.md))

- Compatible: new topics; new keys in documents and events; new values in any list of values
  (`event_type`, `reason`, `state`, `status`, `type`, `flags`, …); new requests and new optional
  keys in a request; larger size limits of requests; larger documents and list bounds within the
  ceilings of [ADR 0015](decisions/0015-provider-neutral-documents-on-the-bus.md): a document
  that Home Assistant reads into attributes stays below 16 KiB, every document below 64 KiB.
- Breaking:
  - a topic removed or renamed, or its retained flag or QoS changed;
  - a key removed or renamed, or its JSON type, unit or meaning changed;
  - a value removed or given another meaning;
  - a document no longer published again after a reconnect, or no longer cleared when its
    contact goes;
  - a request form that is no longer accepted;
  - smaller limits or bounds, or a ceiling of ADR 0015 raised.
- Consumers ignore topics, keys and values they do not know.

**Configuration** ([configuration.md](configuration.md)). A file that loads with X.0.0 loads
with every X.Y.Z and means the same.

- Compatible: new optional settings and tables (a new optional table may require settings of
  its own); wider ranges, formats and choices (a new language of `homeassistant.language`); a
  number where an integer was required; a changed default (listed under *Changed*).
- Breaking:
  - a setting or table removed or renamed (unknown keys are errors, so the service would not
    start);
  - a narrower type, range, format or choice;
  - a setting that becomes required;
  - a new rule between settings;
  - another meaning or unit for a value;
  - a changed default of `mqtt.base_topic`, `mqtt.client_id`, `homeassistant.prefix` or
    `homeassistant.status_topic`, because brokers, ACLs and Home Assistant refer to these names.
- A setting to be retired is deprecated first (*Deprecated*). It still loads, with a warning in
  the log, until the next major version.
- A file written for a newer version need not load with an older one.
- The Proxmox installer's options and its answers file
  ([unattended installation](deployment.md#unattended-installation)) only grow, like the
  configuration. The snapshot below records the answer type of every setting in the answers
  file; review keeps the installer's options and its own answers (`setup`, `ct.*`,
  `radio.choice`, `targets.channels`, `targets.contacts`).

**Command line** (`meshcore-home-gateway --help`)

- Compatible: new commands, options and choices; new keys in JSON output; new line kinds in
  `probe --list profile` and `contacts list` (the first field names the kind).
- Breaking:
  - a command, option or choice removed or renamed;
  - an option that switches between a flag and taking a value;
  - a new required option, argument, option group or subcommand, a new exclusion between
    options, or arguments in another order;
  - a narrower range or format of a value an option or argument takes;
  - another number of columns in a tab-separated list (`probe --list`, `contacts list`): scripts
    read them with `read`;
  - a key removed from JSON output;
  - a value of a machine-readable output removed or renamed;
  - another exit status for a documented case;
  - another `--version` output;
  - other default paths.
- Not interface: abbreviated options (write them in full, since a new option can make an
  abbreviation ambiguous); text meant for people (reports, summaries, messages on standard
  error), which may change and are translated.

**Home Assistant discovery** ([home-assistant.md](home-assistant.md)). What automations,
dashboards and statistics refer to:

- Compatible: new entities, event types, options and attribute keys; a device class or state
  class where an entity had none.
- Breaking: an entity removed (the gateway deletes it in Home Assistant); its unique ID, default
  entity ID, platform or unit changed; its device class or state class removed or changed (an
  entity without its state class keeps no long-term statistics, and a device class decides the
  units Home Assistant converts to); an event type, option or attribute key removed.
- Not interface: names (they are translated), icons, entity categories, and which entities
  are enabled by default. The generated dashboard is not an interface either; the entity IDs it
  names are.

**Platforms.** A major version keeps its floors: Python 3.11 with paho-mqtt 2.x (Debian 13 for
the installer), Home Assistant 2026.8, and MeshCore companion firmware 1.18.0 (protocol version
3). Raising a floor is breaking; supporting more is not. The generated dashboard follows the
frontend features it uses and states its own floor: it needs Home Assistant 2026.10, whose
frontend evaluates its template conditions, while the gateway and its entities keep 2026.8. A firmware version tested in addition is
an addition; dropping a tested one is breaking. The snapshot below records the tested firmware
versions (`TESTED_FIRMWARE`); the floors stay with review.

**Not public:** the Python modules, the files in the state directory (the gateway migrates
them), log messages, the translation catalogs, the installer's language packs and their format,
and the dashboard's template.

**How it is checked.**

- `tests/public_interface.json` records the structural part of the four interfaces, computed from
  the code by `tests/public_interface.py`. `tests/test_public_interface.py` fails when the code
  and the snapshot differ. The bounds and choices of a setting are those the parser takes. The
  slugs of contact entity IDs are recorded for example names; a slug made otherwise is breaking.
  The device and state classes are recorded since 1.1.0a2: against an older release each reads
  as added. The telemetry value lists are `Literal` types in `core/telemetry.py`, which mypy
  checks every producer against; Home Assistant's options are built from them, and the snapshot
  checks that they are.
- After a deliberate change, `make interface` writes the snapshot again and lists what changed
  against the committed snapshot: breaking, changed (a default, a wider range) or added, one
  finding per fact (each value removed from a list is one). Review its diff like code. A merge
  conflict in `tests/public_interface.json` is resolved by `make interface`, not by hand.
- `make interface` also writes `docs/mqtt-api.asyncapi.yaml` from the snapshot and
  `__version__` (`tools/asyncapi.py`), the MQTT API for machines; `tests/test_asyncapi.py` fails
  when it is stale, and checks its channels and keys against the snapshot by rules of its own.
- The CI job *release plan* (`make release-plan`) compares the snapshot with the one of the last
  release on every pull request and push, and its log shows the result. A finding that removes or
  narrows needs a `**Breaking:**` entry (or a `### Removed` section) since that release, which
  makes the next release a major one. `### Removed` is for a public interface only: removing
  what is none (the generated dashboard, its template, a log text) is a `### Changed` entry. A
  finding that adds or changes needs an entry since then
  that makes the next release at least a minor one (`### Added` or `### Changed`): neither
  growth nor a change ships in a patch. A release must contain the snapshot, so the releases
  after it are compared with it. Only these findings need a changelog entry: removing what no
  release had needs none.
- Once the changelog makes the next release a major one, further breaks pass; the log lists every
  break the release takes, and review gives each its own `**Breaking:**` entry.
- A false alarm (something recorded that was never interface, or a fix that adds what was
  missing) is waived with a reason in `tests/public_interface_waivers.json`, until the next
  release; the key is the last release (`X.Y.Z`, as the refusal names it). The file maps it to
  the findings, each as the refusal shows it, and a reason for each:
  `{"1.0.0": {"mqtt > publish > <base_topic>/<node_id>/rx: removed": "never published"}}`. A
  waiver covers that one finding: another value removed from the same list, or another bound,
  needs its own.
- A positional argument is recorded with its name, so that arguments in another order show. A
  rename alone reads as breaking too; since no script can tell, it is waived. Each option is
  recorded with all its names, so a new alias is an addition, also of a required option. A
  command's subcommand is a positional too, and its mutually exclusive option groups are recorded
  with whether one of them is required, each option by all its names: a new exclusion between
  options that existed, or a required group that a call of before need not satisfy, is breaking.
- The snapshot does not see, so review keeps:
  - the JSON types and nullability of keys (1.1.0a3 added `advert_carries_position`, a boolean or
    `null`, and `advert_checked_at`, a string or `null`, to `contacts/<key>`);
  - the size limits and list bounds of MQTT;
  - the value lists that are not under `mqtt > values` or `cli > values`: the `reason` of
    deliveries, contact and setting events (1.1.0a2 added `not_a_companion`, `too_many`,
    `not_saved` and `not_forgotten` to those of contact events, and uses `rate_limited` and
    `state_unwritable` for telemetry requests too), each setting's `state`, `heard_via`,
    `position_source`, and `direction`, `kind` and `state` in the history;
  - the string formats of settings;
  - the forms an option's value takes: `--tcp`'s HOST[:PORT], the keys, labels, links and cards
    `contacts add` accepts;
  - the values inside machine-readable lines: the actions of `probe --json` and
    `probe --list profile`, the yes and no of `probe --list profile`, the marks of
    `contacts list`;
  - publishing again after a reconnect;
  - the exit status of each case;
  - the platform floors: Python, paho-mqtt, Home Assistant, the firmware and protocol versions;
  - meanings and units of values, rules between settings, and the texts a request accepts (such
    as `on`, `off` and `forget` of `contacts/<key>/telemetry`). Since 1.1.0a3 the state of
    *Companion position in adverts* follows `advert_carries_position…` of the `contacts` index,
    while `advert_position` keeps its meaning: the radio's record holds a position from one of the
    contact's adverts.

## Release

Releases are built, signed and published by the workflow `.github/workflows/release.yml`
([ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md)); nobody builds
release files by hand. The tools that decide what a release is and contains,
`tools/release_plan.py`, `tools/public_interface.py`, `tools/release_notes.py`,
`tools/build_pyz.py`, `tools/asyncapi.py` and the `release` target of the `Makefile`, are
reviewed like the security-relevant parts of the program: a change to them changes what every
installation gets.

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from 1.0.0 on, for
the four public interfaces of [Compatibility](#compatibility): an incompatible change raises the
major version, a feature (a slice of the roadmap,
[design §13](design.md#13-roadmap-the-mesh-observer)) the minor version, fixes the patch version.

Every finished increment is published as a pre-release, so each step is visible, signed and
installable: the increments of a feature as `X.Y.0aN` of the minor version it leads to (the
telemetry slices 6a, 6b and 6c as `1.1.0a1`, `1.1.0a2`, `1.1.0a3`), fixes as `X.Y.ZaN`. A
release candidate `X.Y.ZrcN` is what the [release check](testing.md#release-check) runs with
([ADR 0030](decisions/0030-release-checked-by-ci-self-test-and-use.md)); the release follows when
it passes. `releases/latest` and the installer's default never pick a pre-release
([deployment.md](deployment.md)). Nobody tags by hand
([ADR 0022](decisions/0022-releases-tagged-by-ci-on-main.md)):

1. For a release (not a pre-release): the release check with the release candidate. CI passed on
   it; the maintainer updates their installation with the candidate's installer, its self-test
   passes, and they use Home Assistant as every day (checks R1–R5 of
   [testing.md](testing.md#release-check)); the pull request of the release names the Home
   Assistant version. The release ships the program of that candidate: only `__version__`
   may differ in `src/`, `deploy/`, `pyproject.toml` and `tools/build_pyz.py`. So before the
   candidate, and at the latest with the first version written after a release, a pre-release
   too (a test checks it), a change raises `RELEASED_AFTER` in
   `src/meshcore_home_gateway/radio/session.py` to the date of the last release: the gateway
   takes a host clock before it, or more than ten years after it, as wrong and does not write it
   to the radio (design §6.1). One left behind only weakens that check; one in the future would
   refuse every host clock, which a test catches.
2. The pull request that finishes the increment sets `__version__` in
   `src/meshcore_home_gateway/__init__.py`, runs `make interface` (the AsyncAPI document names the
   version), adds its row to the table of Home Assistant releases tested with
   ([home-assistant.md](home-assistant.md#requirements)) and moves the *Unreleased* entries of
   `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` (a pre-release's section says that it had no
   release check). The date is the day the tag is due: the day of the merge for a pre-release,
   the day after it for a release (step 3); once the day of the merge is known, the section is
   dated again if it moved. The version must be the next one, which `make release-plan` and the
   CI job *release plan* check: the kind of change comes from the changelog entries since the last
   release (an entry that starts with `**Breaking:**` or a `### Removed` section: major; Added,
   Changed or Deprecated: minor; Fixed or Security only: patch), the stage from the version
   written; pre-releases count up from 1. Only Keep a Changelog's six headings are allowed. The
   same check compares the public interface with that of the last release
   ([Compatibility](#compatibility)), also when the version does not change. The sections since
   the last release stay as they were tagged: after merging `main`, the branch's entries must
   still be under *Unreleased* (git can move them into the newest section without a conflict).
   `git fetch --tags origin && make release-plan` runs the check locally (it needs the tags; on a
   branch that does not contain the last release it refuses: merge `origin/main` first). It reads
   the commit, not the working tree, so commit first.
   `RELEASE_TAG=vX.Y.Z make release` builds the release files into `dist/release/` as the
   workflow does. A pull request that keeps `__version__` releases nothing.
3. After the merge of a pre-release, a release candidate too, CI on `main` tags the merge commit
   once every job passed on it, and starts `release.yml` on the tag (job *tag and release*); a
   push of several commits is tagged on its last one. A release waits a day (SUP-001): the
   workflow `release-due.yml` looks every hour and tags the commit that wrote the version once it
   has been on `main` for 24 hours and CI passed on it, then starts the release the same way,
   once: a release that failed is followed by the next version. A run of `release.yml` that
   failed before it published (a flaky test in its job *check*) needs no new version: *Re-run
   failed jobs* on that run runs the failed job and the ones after it on the same tag, which
   stays. Until the tag is made, disabling the workflow stops the release
   (`gh workflow disable release-due.yml`, or *Disable workflow* in the Actions tab; enable it
   again for the next version). A revert of the version does not stop it: the release plan
   refuses it, because the tag of the version it goes back to is on another commit. A published
   release cannot be withdrawn. Everything a release is built from
   was on `main` for a day, and its program was tagged as the candidate before; a pre-release
   reaches only an installation that names it, so a faulty candidate is followed by the next one
   as soon as CI passed on it. A re-run, of the failed job or of all jobs, is safe: it finds the
   tag on the same commit and starts the release only if there is none and none is running.
   Until a release is tagged, the release plan of every other change refuses ("no tag vX.Y.Z
   here"): merge other changes before the version change or after the tag, and run it again once
   the tag is there.
4. The workflow's first job, *admit*, checks the tag (`tools/release_plan.py admit`): its commit
   must be on `main`. A release must be where `release-due.yml` would tag it, which `admit` asks
   the same way (`release_plan.py due` on `main`): on the commit that wrote the version `main`
   has, a day after that commit's date, with CI passed on it on `main`, and while
   `release-due.yml` is enabled. So a tag pushed by hand, by mistake or as told by an injected
   text, is not published before its time, nor after the release was stopped. One pushed within
   the day is refused with the time it is due; `release-due.yml` then starts the release on it,
   as on its own tag, because it does not count a run that *admit* refused. A pre-release needs
   only a commit of `main` that carries its version: CI's job tags one only after every job
   passed, but one tagged by hand is published also where CI has not passed. The check is part
   of the tagged commit: a tag on a commit off `main` brings that commit's `release.yml`, which
   can leave it out. Then the workflow runs
   `make check` in one job and builds the release files in another, in an
   environment of the build tools alone
   (`make release VENV=.venv-release REQUIREMENTS=requirements/release.txt`), so that no test or
   lint tool runs where they are made (SUP-002). The build refuses a tag that differs from
   `__version__`, builds the zipapp, wheel and sdist reproducibly, adds the systemd unit (its
   second line names the version: `# Version: X.Y.Z`, refused without it) and the Proxmox
   installer with the version and the SHA-256 of the zipapp and the unit written in
   (`RELEASE="X.Y.Z"`, `RELEASE_PYZ_SHA256`, `RELEASE_UNIT_SHA256`: run from the release's
   address, it installs this release) and, beside it, the language pack of each language it
   offers (`meshcore-home-gateway-lxc.<language>.messages`, refused unless it is the pack the
   installer pins) and the AsyncAPI document (`meshcore-home-gateway-X.Y.Z.asyncapi.yaml`,
   refused unless it is the snapshot's for that version), and writes `SHA256SUMS`. A third job
   builds the files again on an arm64 runner and compares `SHA256SUMS`. After all three, the last
   job attests every file (Sigstore-signed build provenance) and publishes the GitHub release
   with the changelog section as notes (its relative links point at the files of the tag).
5. Check the result like a user: download a file and run

   ```sh
   gh attestation verify meshcore-home-gateway-X.Y.Z.pyz --repo dkuenzel/meshcore-home-gateway \
     --signer-workflow dkuenzel/meshcore-home-gateway/.github/workflows/release.yml \
     --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
   ```

The release tags are protected by a repository ruleset for `v*`: only its bypass actor, a
repository role of the maintainer's login, can move or delete them (for emergencies), and by
policy nobody does, so the attestation's tag names one commit. The agent works through that
login, so the ruleset does not hold against it, nor does the one of `main`
([SECURITY.md](../SECURITY.md#how-the-repository-and-the-releases-are-protected)). Creating them is open to write access, which only the
maintainer and the workflows of this repository have (pull requests from forks get read-only
tokens); *admit* refuses a release that `release-due.yml` would not tag. A release that failed is
followed by the next version, never by a moved tag.

A manual run of the workflow ("Run workflow" in the Actions tab) is a dry run on a branch: it
checks, builds and rebuilds the same files and keeps them as a workflow artifact, without
attestation or release; on a tag it releases that tag, as a pushed tag does, if *admit* lets it.

The provenance meets SLSA Build Level 2: a hosted build platform makes and signs it
([GitHub](https://docs.github.com/en/actions/concepts/security/artifact-attestations)). Level 3
would need the build in a reusable workflow that the release workflow cannot influence; the
rebuild on another runner image and the immutable releases cover part of that gap
([ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md)).
