---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# History in a local SQLite database

## Context and Problem Statement

Dashboards show recent messages, position traces and telemetry over time; today every restart of
the gateway forgets everything it has not published as a retained document, and Home Assistant's
recorder is not a provider-neutral source ([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)).
The gateway needs its own history: messages in and out, positions, telemetry readings and when
nodes were heard. Which storage format?

## Decision Drivers

* No new runtime dependency ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md)).
* Small volumes, append-mostly; queries "the last N", "key K since time T", purges by age.
* Crash safety: a power cut must not corrupt the history or, worse, the replay protection.
* Bounded disk use with defaults that need no tuning.
* Privacy: message texts and positions of the household's people end up on disk.
* A failing history must never stop messaging.

## Considered Options

* SQLite through Python's `sqlite3` module
* JSON Lines files with rotation
* Columnar files (Apache Parquet through `pyarrow`) or an embedded analytics database (DuckDB)
* Retained topics on the broker as the only store
* Home Assistant's recorder

## Decision Outcome

Chosen option: "SQLite", because it is part of the standard library on every supported system,
handles the access pattern with indexes, and is crash safe with its write-ahead log.

* **One file**, `history.sqlite3` in the service's state directory (`/var/lib/meshcore-home-gateway`,
  mode 0700, file 0600), written only by the service. Write-ahead log, `synchronous = NORMAL`
  (a power cut can lose the last transactions, never the database), schema version in
  `PRAGMA user_version` with migrations in code.
* **Tables:** messages (in and out, with delivery outcome), positions (contact, time, latitude,
  longitude, altitude, source: telemetry or advert), readings (contact, time, quantity, value:
  battery, temperature, humidity, …), sightings (node, time, hops, SNR, RSSI, how it was heard).
* **Retention** by age and by row count per table, purged hourly; defaults: messages 30 days or
  10 000 rows, positions and readings 30 days, sightings 7 days. Positions are stored only for
  contacts, never for nodes the gateway merely heard
  ([ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md), privacy).
* **Separate from the state file:** the replay ledger and the announced components stay in the
  small, atomically replaced `state.json` ([ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md));
  bulk history never shares a file with them.
* **Failure is not fatal:** a database that cannot be opened or written is logged, an unreadable
  one is moved aside (`.corrupt`) and a new one started; until then the gateway runs without
  history, and the recent lists start empty.
* **Readers** go through the providers (recent lists as retained documents); the command line can
  read the file directly on the gateway host, and an export to JSON Lines or CSV can follow for
  analysis elsewhere.

### Consequences

* Good, because nothing new is installed: Debian's `libpython3.11-stdlib` (bookworm) and
  `libpython3.13-stdlib` (trixie) ship `_sqlite3`, linked against SQLite 3.40.1 and 3.46.1.
* Good, because the expected volume is tiny for SQLite *(calculated)*: three companions polled
  every 5 minutes give about 860 positions and 2 600 readings a day; with 100 messages a day and
  30 days of retention the database stays below 150 000 rows, around 20 MB.
* Good, because analysis tools read SQLite directly; DuckDB, for example, attaches a SQLite file
  with its `sqlite` extension, so users get columnar speed without the gateway depending on it.
* Bad, because the history holds message texts and positions on the container's disk; the
  directory is private to the service user, retention bounds it, and the documentation says how to
  turn the history off or delete it.
* Bad, because SQLite and the event loop: writes are short and batched, and run in the loop; if
  they ever stall it, they move to a worker thread.

### Confirmation

Tests cover schema creation and migration, retention by age and count, a corrupt file, a full disk
(write errors) while messages keep flowing, and that positions of non-contacts are never stored.

## Pros and Cons of the Options

### SQLite

* Good, because indexed range queries, transactions and crash safety come built in.
* Bad, because the format is a single file that only one process should write; the service is that
  process.

### JSON Lines with rotation

* Good, because it is plain text and trivial to append.
* Bad, because every query is a scan, purging by age rewrites files, and a torn last line after a
  power cut needs repair code.

### Parquet or DuckDB

* Good, because columnar storage compresses well and scans millions of rows fast.
* Bad, because neither is available from the distribution: `python3-pyarrow` exists only in Debian
  testing and unstable (25.0.0-2), and there is no `python3-duckdb` or `python3-polars` package
  (packages.debian.org, searched 2026-10-23). A pip install would break ADR 0005.
* Bad, because Parquet files are immutable: appends need new files and compaction, which is
  complexity for a few thousand rows a day.

### Retained topics on the broker only

* Good, because there is nothing to store locally.
* Bad, because a document is limited in size (ADR 0015) and lost when the broker's persistence is
  reset; there are no range queries.

### Home Assistant's recorder

* Good, because Home Assistant users already have it.
* Bad, because it is not provider-neutral and records what entities show, not the gateway's data;
  attributes over 16 KiB are dropped.

## More Information

* File lists: <https://packages.debian.org/bookworm/amd64/libpython3.11-stdlib/filelist>,
  <https://packages.debian.org/trixie/amd64/libpython3.13-stdlib/filelist>; versions of
  `libsqlite3-0` on <https://packages.debian.org/bookworm/libsqlite3-0> and
  <https://packages.debian.org/trixie/libsqlite3-0> (fetched 2026-10-23).
* Revisit when the volume grows by orders of magnitude (repeater fleets, long-term network
  statistics) or when a columnar library becomes available from the distribution.
