# Translations

How the gateway's texts are translated, for translators and for whoever adds a language. The
design is in [design §6.12](design.md#612-translations) and
[ADR 0019](decisions/0019-internationalisation-with-gettext.md); the rules for code that shows a
text are in [development.md](development.md#translations).

2.1.0 ships English and German: the command line, the installer, the names in Home Assistant
and the dashboard, each in both. Every other language comes as a contribution: the catalog files
of [Adding a language](#adding-a-language), in one pull request. A language is offered only when
its texts are complete.

## What is translated

* The command line's texts for people: help, errors, warnings, the summary of `check-config`, the
  report of `probe`. They follow the environment
  ([configuration.md](configuration.md#language-of-the-command-line)).
* The names the gateway sends to Home Assistant (entities, devices, models) and the generated
  dashboard. They follow `[homeassistant] language`
  ([configuration.md](configuration.md#homeassistant)).
* The Proxmox installer's dialogs, buttons and boxes.

Never translated: logs, machine output (`--json`, `--list`, `listen`, `config answers`, …), MQTT
topics and values, entity IDs, event types, options, attribute keys, configuration keys,
command and option names, reason codes, and what comes from the mesh or the radio (names, texts,
labels). The user guides stay English.

## The files

| File | Holds |
| --- | --- |
| `src/meshcore_home_gateway/locale/meshcore-home-gateway.pot` | the template of the program's texts: the command line, argparse's own words, the Home Assistant names (`msgctxt "home assistant"`) and the dashboard's texts (`msgctxt "dashboard"`) |
| `src/meshcore_home_gateway/locale/<language>.po` | one language's translations of them |
| `deploy/proxmox/locale/installer.pot` | the template of the installer's texts; its buttons have `msgctxt "button"` |
| `deploy/proxmox/locale/<language>.po` | one language's translations of them |
| `deploy/proxmox/locale/<language>.messages` | the installer's language pack, written from that file by `make translations`; never edit it |

The templates and the catalogs are written by `make translations` in one form: no line wrapping,
no dates, no names or addresses (the git history credits the translators), entries sorted by
file and text. Edit the PO files with any editor or with Poedit (switch off line wrapping and
compiling on save: git ignores the compiled `.mo` file, but the wheel would pack it, so
`make dist` refuses to build while one is there). Run `make translations` before a pull request;
`make check` then shows what is missing or wrong (`.venv/bin/python tools/i18n.py check` shows
the same without the other tests).

## Rules for every text

* Translate the whole text. A text is one line or one sentence; never split or join texts.
* Keep every field as it is: `{alias}` stays `{alias}`, also `{value!r}` and `{duty:.3g}`; move
  it where the language needs it. A text may use no field its English text does not have.
* A text marked `python-format` (help texts and argparse's words) keeps its `%` directives
  (`%(prog)s`, `%s`, `%%`): argparse fills them in, and a lone `%` would stop `--help`.
* Keep as they are: options (`--wait`), environment variables (`$STATE_DIRECTORY`), paths,
  configuration keys (`homeassistant.language`), `KEY12…KEY8`, ADR numbers, document paths
  (`docs/deployment.md`), web addresses, `pct exec` and `journalctl`.
* No control or format character but the line breaks of the English text. No no-break space
  (U+00A0, U+202F) in the command line's texts: the installer's boxes show them too, and whether
  whiptail draws them is not checked yet.
* Plural texts get every form of the language's `Plural-Forms` header, each with the same
  fields.
* `# Translators:` notes in the code reach the catalogs as `#.` comments: they say where a text
  is shown, what a field holds, and limits such as "at most 12 characters" for the installer's
  buttons.
* The dashboard's three tabs, *Overview*, *Chats* and *Contacts*, have at most 22 letters
  together, or they do not fit a phone.

A translation that breaks a rule is refused by `make check`; at run time the program shows the
English text instead.

## Review of a translation

The checks keep a translation from breaking a field, a template or a value; they cannot judge
what it says. A translated installer dialog could ask for the host's root password where the
English one asks for the broker's, or leave out what a warning warns of, and a translation comes
as data from someone the maintainer may not know, in a language the maintainer may not read
(SUP-006). So a new or changed translation is read by a second speaker of the language before it
is merged; where none is found, the maintainer translates it back by machine and reads that.
Texts whose note says *security text* get the closest reading: they warn of a risk (the radio's
TCP interface, a broker without a user name or TLS, a confirmation that may be forged, a
look-alike key), say how a connection is secured, or ask for a secret, and the translation must
say all of it and nothing more. A new text that names such things (TLS, a password, a user name,
authentication) needs the note, or a place in the test's list of texts that are none
(`tests/test_translations.py`).

German is the project's own language: the project started in it, writes its catalogs and its
maintainer reads them, so a German text needs no second speaker (the maintainer's decision for
2.1.0). A new or changed German security text is still translated back by machine and compared
with the English before it is merged. Every other language is reviewed as above.

## Home Assistant names

`msgctxt "home assistant"`: entity names, the models of the contacts' devices, the reading types
and the default device name `MeshCore radio`. Use the words of Home Assistant's own translation
for the language where it has them (*Battery*, *Signal strength*). Home Assistant shows the device
name before an entity's name, so an entity's name does not repeat it. Sentence case, as Home
Assistant writes names.

## The dashboard

`msgctxt "dashboard"`: the titles, headings, empty states, hints and status lines of the
generated dashboard ([home-assistant.md](home-assistant.md#dashboard)), and the words for the
delivery steps, telemetry states and contact events it shows.

* Keep `{fields}` as everywhere: `{node}` and `{version}` are filled in when the dashboard is
  made; any other field (`{n}`, `{queue}`, `{running}`, `{names}`) is a value the card computes.
* No `<`, `>`, `{{`, `{%`, `{#`, `@`, `](`, `://`, `www.` or `_("`, and no line break: a card
  would read them as HTML, Jinja, a Markdown link or an address. The command then uses the English
  text, and `make check` refuses the translation.
* The counts have no plural forms: write them as "Messages waiting: {queue}.".
* Reason codes (`no_acknowledgement`, `look_alike`) are not in the catalog: the dashboard shows
  them as codes in every language.
* `{#- Translators: … -#}` notes inside a card's content reach the catalog like `# Translators:`
  notes.

## The installer

The installer's texts are in its own catalog, because it runs on the Proxmox host from its
address, without the program. `make translations` turns each catalog into a plain-text pack and
writes the pack's SHA-256 into the script; the installer fetches the pack from where the script
comes from (a release publishes each pack beside its installer, a checkout has it next to the
script) and uses it only when the digest matches. Its buttons (`msgctxt "button"`) have at most
12 characters. Its own log lines stay English.

## Adding a language

For a language `ll` (its ISO 639-1 code, two lower-case letters):

1. Copy the two templates to `src/meshcore_home_gateway/locale/ll.po` and
   `deploy/proxmox/locale/ll.po`. Unless `PLURAL_FORMS` in `tools/i18n.py` has the language's
   plural rule (it has those of German, Dutch, French and Polish), add it there, or give each
   file's header the line `"Plural-Forms: …\n"` with the rule from GNU gettext's manual
   (*Plural forms*). `make translations` writes the rest of the header.
2. Translate every text of both files.
3. Add the language to `LANGUAGES` in `src/meshcore_home_gateway/i18n.py`, with its own name for
   itself: `"ll": "Name"`.
4. Run `make translations`: it merges both catalogs, writes the installer's pack
   `deploy/proxmox/locale/ll.messages` and pins it in the script.
5. Run `make interface`: the language is a new choice of `[homeassistant] language`, which the
   snapshot of the public interface records (`tests/public_interface.json`; its diff adds `ll`
   and removes nothing).
6. Run `make check` and `make ha-test`; they check every listed language.
7. In the pull request, add the language to the *Added* entries of `CHANGELOG.md` and to the
   choices of `language` in [configuration.md](configuration.md#homeassistant). A second speaker
   of the language reviews the pull request ([Review of a translation](#review-of-a-translation)).

Nothing else changes for a language: `tests/test_adding_a_language.py` adds a pseudo-language to a
copy of the checkout through exactly these steps and runs the checks that read the languages in
it. A language besides English also shows the installer's language menu, as German does; the
tests of the English dialogs run a copy of the script that offers English only. `make release`
publishes each pack beside the installer. A language listed in `LANGUAGES` is a choice of
`[homeassistant] language`, which a later version keeps
([compatibility](development.md#compatibility)): remove one only in a major version.

A new text in a later change needs its translation in every listed language before `make check`
passes. Its author writes them, or asks in the pull request.
