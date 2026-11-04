"""`history export`: the local history (ADR 0016) as JSON lines or CSV (IMP-047), read only, while
the service runs. Times are ISO 8601 in UTC; in CSV, the control characters of texts from the
mesh become spaces, as in the command line's other lists, and a text that a spreadsheet would
compute (=, +, -, @ first) begins with a '."""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meshcore_home_gateway.cli.common import (
    DEFAULT_STATE_DIR,
    EX_FAILURE,
    EX_OK,
    PREFIX,
    _json_text,
    _safe_text,
    tr,
)

HISTORY_FILE = "history.sqlite3"
FORMULA_STARTS = ("=", "+", "-", "@")
# what each table exports, in this order: the column and whether it is a time (unix seconds)
TABLES: dict[str, tuple[str, tuple[tuple[str, bool], ...]]] = {
    "messages": (
        "message",
        (
            ("at", True),
            ("direction", False),
            ("kind", False),
            ("contact_key", False),
            ("channel_index", False),
            ("name", False),
            ("text", False),
            ("state", False),
            ("hops", False),
            ("snr_db", False),
        ),
    ),
    "positions": (
        "position",
        (
            ("contact_key", False),
            ("at", True),
            ("latitude", False),
            ("longitude", False),
            ("altitude_m", False),
            ("source", False),
        ),
    ),
    "readings": (
        "reading",
        (("contact_key", False), ("at", True), ("quantity", False), ("value", False)),
    ),
}


def _history_export(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.history import SCHEMA_VERSION  # noqa: PLC0415

    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    path = state_dir / HISTORY_FILE
    if not path.exists():
        print(PREFIX + tr("no history at {path}", path=path), file=sys.stderr)
        return EX_FAILURE
    since = args.since.timestamp() if args.since else None
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                newer = tr(
                    "the history is of a newer version of the program ({version}): update it",
                    version=version,
                )
                print(PREFIX + newer, file=sys.stderr)
                return EX_FAILURE
            rows = _rows(db, args.table, since)
            _write(rows, args.table, args.format)
        finally:
            db.close()
    except sqlite3.Error as exc:
        print(PREFIX + tr("cannot read {path}: {detail}", path=path, detail=exc), file=sys.stderr)
        return EX_FAILURE
    return EX_OK


def _rows(db: sqlite3.Connection, table: str, since: float | None) -> list[dict[str, Any]]:
    name, columns = TABLES[table]
    known = {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
    if not known:  # a history from before the table existed
        return []
    # a history the service has not yet migrated lacks the newer columns: empty in the export
    selected = ", ".join(c if c in known else f"NULL AS {c}" for c, _ in columns)
    query = f"SELECT {selected} FROM {name}"  # noqa: S608 - the names of TABLES
    query += " WHERE at >= ? ORDER BY at, id" if since is not None else " ORDER BY at, id"
    cursor = db.execute(query, (since,) if since is not None else ())
    return [
        {
            column: _iso(value) if time else value
            for (column, time), value in zip(columns, row, strict=True)
        }
        for row in cursor
    ]


def _write(rows: list[dict[str, Any]], table: str, form: str) -> None:
    if form == "jsonl":
        for row in rows:
            print(_json_text(row))
        return
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow([column for column, _ in TABLES[table][1]])
    for row in rows:
        writer.writerow(_cell(v) for v in row.values())


def _cell(value: Any) -> Any:
    """A CSV cell: texts from the mesh without control characters, and one that a spreadsheet
    would take for a formula (=, +, -, @) behind a ' that shows it as text (CSV injection)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    text = _safe_text(value)
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def _iso(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def since_time(text: str) -> datetime:
    """--since: a date or a date and time in ISO 8601; without a zone, UTC."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            tr("not a date such as 2026-11-01 or 2026-11-01T08:00:00+01:00: {value}", value=text)
        ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
