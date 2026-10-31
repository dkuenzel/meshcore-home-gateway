---
status: accepted
date: 2026-10-31
decision-makers: maintainers
---

# Message catalogs compiled when they are loaded

## Context and Problem Statement

[ADR 0019](0019-internationalisation-with-gettext.md) compiles the PO files into `.mo` catalogs
when the zipapp is built. But the zipapp is not the only way the program runs: the wheel is a
release file too, a source checkout runs the package as it is, and the contract tests import it
into Home Assistant's environment. None of them has a build step that could compile. flit puts
every file of the package directory into the wheel and the sdist, so a `.mo` written into `src/`
would ship there, built or stale. And the installer builds the zipapp inside the container with
`tools/build_pyz.py` alone. Where and when are the catalogs compiled?

## Decision Drivers

* One behaviour wherever the program runs: the zipapp, the wheel, a checkout and the tests.
* No runtime dependency beyond the standard library
  ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md)).
* Nothing generated that could reach a release unnoticed; reproducible builds
  ([ADR 0013](0013-signed-releases-with-github-artifact-attestations.md)).
* A language costs little at start: `run` loads one, the command line at most one.

## Considered Options

* `.mo` at build time, as ADR 0019 says; the wheel and a checkout stay English
* `.mo` at build time in the zipapp, and the `.po` compiled when loaded where no build ran
* `.mo` files committed to the repository
* Every catalog compiled when it is loaded, everywhere

## Decision Outcome

Chosen option: "every catalog compiled when it is loaded", because it is the only one with one
behaviour in every form of the program and no build step.

* The package carries the PO files (`src/meshcore_home_gateway/locale/<language>.po`).
  `i18n.translator(language)` reads a language's file from the package's resources when it is
  first used, compiles it in memory with our own MO writer (`catalog.write_mo`) and loads the
  bytes into `gettext.GNUTranslations`, once per process and language.
* The writer gives the bytes of GNU `msgfmt --no-hash` for every catalog without a partly
  translated plural entry. Such an entry, and every fuzzy or untranslated one, is left out, so its
  text is English; GNU keeps it, and gettext then shows an empty form.
* `tools/build_pyz.py` packs the PO files with the sources, not the template. No `.mo` file exists:
  git ignores one, and `make dist` refuses to build while one is in `src/` (Poedit writes it
  next to the PO file when it saves).
* A file that cannot be read or compiled gives English and a warning in the log.

### Consequences

* Good, because the zipapp, the wheel, a checkout and the tests behave alike, and the tests take
  the path production takes.
* Good, because there is no build step and nothing generated that could leak into a release.
* Bad, because the first use of a language in a process costs a few milliseconds (about 5 ms to
  read and compile the 372 texts of 1.1.0a4 from a checkout on Python 3.11 and 3.14; up to 14 ms
  were measured from a zipapp), and the PO reader is runtime code, not tooling.
* Neutral, because the source checkout's own `tools/i18n.py` writes the PO files in one canonical
  form, so the reader only needs to read what that writer, GNU gettext, Poedit and Weblate write.

### Confirmation

`tests/test_catalog.py` (a golden catalog made with GNU `msgfmt --no-hash`, and byte identity
with GNU `msgfmt` where it is installed), `tests/test_i18n.py` (the package's PO file read,
compiled once and cached; English and a warning for a file that cannot be read),
`tests/test_build_pyz.py` (the archive holds the PO files, no template and no `.mo`).

## Pros and Cons of the Options

### `.mo` at build time, the wheel and a checkout English

* Good, because ADR 0019 needs no change.
* Bad, because the wheel, a release file, would stay English while a language is set, and the
  tests would check a path production does not take.

### `.mo` in the zipapp, `.po` compiled elsewhere

* Good, because the zipapp loads a little faster.
* Bad, because two production paths, a compiler inside `build_pyz.py`, and generated files merged
  into the sorted archive.

### `.mo` committed

* Good, because nothing is compiled anywhere.
* Bad, because binaries in git that can drift from their PO file, and their bytes depend on the
  tool and its options (GNU `msgfmt` adds a hash table unless given `--no-hash`).

### Compiled when loaded

* Good, because one behaviour and no build step.
* Bad, because a few milliseconds per language and process, and the reader is runtime code.

## More Information

This record amends [ADR 0019](0019-internationalisation-with-gettext.md) (its *Python* point
"compiled at build time" and its pros and cons of gettext catalogs, "it is part of the zipapp
build"); everything else of ADR 0019 stays. The details: [design §6.12](../design.md#612-translations).
Revisit if a catalog grows so large that compiling it slows the start, or if Python's `gettext`
gains a reader of PO files.
