"""Storage: CSV archive (source of truth) + SQLite query layer (rebuildable). Event log. CSV
re-ingest. No state that lives only in SQLite.

Readings are stored per bulb, long format (a `heartbeat` row plus one `bulb_reading` row per
bulb; a `reactive_event` row plus one `reactive_change` row per bulb), which is what
learning-unit pooling needs. The almanac is insert-only snapshots keyed by learning unit, so
history comes for free. The SQLite DDL is `schema/storage.schema.sql`.

Layout under the data directory:

- `archive/<table>/<YYYY-MM>.csv`: one CSV stream per table, by the row's UTC month, with
  the table's columns minus the surrogate ids. A child row (a reading, a reactive change)
  names its parent by the parent's natural key, (`room_id`, `ts`), so the archive reads back
  without the database. A file whose header doesn't match the current columns is never
  appended to: writing moves on to `<YYYY-MM>.1.csv`, and so on.
- `light_map.db`: the SQLite query layer, which also records how many bytes of each
  archive file it holds (`archive_file`).

Every write goes to the CSV first (flushed to disk) and then to SQLite, so a database
failure never costs an observation. The query layer records the size and mtime of each
archive file as of the last write it holds, and moves that record forward only from where
it was: once any write misses the query layer, the record stays behind (and the store says
it is `stale`), so the gap can't be covered over by the next successful write. `open`
compares the record with the archive and rebuilds the query layer from it when they differ
(the database deleted, behind after a failed write or a crash between the two writes, or
an archive file edited). A database from another schema version is recreated; one that
won't open or read cleanly is moved aside, never deleted, and rebuilt. A write to a CSV
that fails is taken back to where the file was, and a file that doesn't end in a newline
(cut short some other way) is never appended to, so a torn line can't swallow the rows
after it. Re-ingest skips what it can't read (counted, logged) rather than failing.

Methods are synchronous and guarded by one lock: the observer (on the event loop) and the
HTTP layer (from its threadpool) share a store. A write is a few rows and takes
microseconds to milliseconds, not worth a thread of its own.

Hard rules:

- CSV is the archival source of truth; SQLite is a rebuildable query layer. Never add state
  that only lives in SQLite: everything must be reconstructable from the CSV archive.
- The almanac is insert-only; a new learned value is a new snapshot, never an update.
- Validate, then write: a row that doesn't fit its table is refused before anything is
  archived, so the archive never holds what the query layer would reject.
"""

import csv
import json
import logging
import math
import os
import re
import sqlite3
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "storage.schema.sql"
DB_NAME = "light_map.db"
ARCHIVE_DIR = "archive"

LEVELS = ("debug", "info", "warning", "error")

# SQLite's INTEGER range: a bigger whole number would pass a check and fail at the insert.
_INT64 = (-(2**63), 2**63 - 1)


@dataclass(frozen=True)
class _Column:
    name: str
    kind: str  # ts | text | int | real | bool | json
    required: bool = False


def _columns(*spec: str) -> tuple[_Column, ...]:
    """`"name:kind"`, with a trailing `!` on the kind for a required column."""
    columns = []
    for item in spec:
        name, _, kind = item.partition(":")
        columns.append(_Column(name, kind.rstrip("!"), kind.endswith("!")))
    return tuple(columns)


# The CSV streams, column for column. A child's (`room_id`, `ts`) is its parent's key; in
# SQLite it becomes the parent's id.
_STREAMS: dict[str, tuple[_Column, ...]] = {
    "heartbeat": _columns(
        "ts:ts!",
        "room_id:text!",
        "section:text",
        "lux:real",
        "lux_sensors:int!",
        "occupancy:bool",
        "deferred_ms:int!",
    ),
    "bulb_reading": _columns(
        "ts:ts!", "room_id:text!", "bulb_id:text!", "is_on:bool", "brightness:int"
    ),
    "reactive_event": _columns(
        "ts:ts!",
        "room_id:text!",
        "section:text",
        "window_seconds:int!",
        "lux_before:real",
        "lux_after:real",
        "occupancy:bool",
    ),
    "reactive_change": _columns(
        "ts:ts!",
        "room_id:text!",
        "bulb_id:text!",
        "is_on_before:bool",
        "is_on_after:bool",
        "brightness_before:int",
        "brightness_after:int",
        "changed:bool!",
        "user_id:text",
    ),
    "almanac": _columns(
        "ts:ts!",
        "room_id:text!",
        "section:text!",
        "learning_unit_id:text!",
        "brightness:int",
        "on_fraction:real",
        "lux_target:real",
        "lux_margin:real",
        "maintenance_enabled:bool",
        "max_step_pct:real",
        "trust_weight:real",
        "days_contributing:int",
        "high_confidence_days:int",
        "days_analysed:int",
        "valid_from:text",
        "mode:text",
    ),
    "event_log": _columns(
        "ts:ts!",
        "level:text!",
        "category:text!",
        "room_id:text",
        "message:text!",
        "detail:json",
    ),
}

# child table -> (parent table, the child's foreign-key column)
_PARENTS = {
    "bulb_reading": ("heartbeat", "heartbeat_id"),
    "reactive_change": ("reactive_event", "reactive_event_id"),
}

# Parents before children, for inserting; the reverse for deleting.
_ORDER = ("heartbeat", "bulb_reading", "reactive_event", "reactive_change", "almanac", "event_log")

_DEFAULTS = {"lux_sensors": 0, "deferred_ms": 0}

_RANGES = {
    "brightness": (0, 255),
    "brightness_before": (0, 255),
    "brightness_after": (0, 255),
    "lux": (0, None),
    "lux_before": (0, None),
    "lux_after": (0, None),
    "lux_sensors": (0, None),
    "deferred_ms": (0, None),
    "window_seconds": (1, None),
    "on_fraction": (0, 1),
    "days_contributing": (0, None),
    "high_confidence_days": (0, None),
    "days_analysed": (0, None),
}


class StoreError(RuntimeError):
    """The store isn't open, or couldn't be opened."""


def timestamp(value: datetime | str | None = None) -> str:
    """The one timestamp format the store keeps: ISO-8601 UTC with microseconds, so every
    `ts` sorts correctly as text. `value` must carry a timezone; None means now."""
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"not an ISO-8601 timestamp: {value!r}") from None
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        # A naive time would be filed under whatever zone the container happens to run in.
        raise ValueError(f"a timestamp needs a timezone: {value!r}")
    try:
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except OverflowError:
        raise ValueError(f"out of range: {value!r}") from None


@lru_cache(maxsize=1)
def _schema() -> tuple[str, int]:
    """The DDL and its version: a checksum of the text, so any change to the schema makes
    an existing query layer recreate itself instead of carrying on with old tables
    (`CREATE TABLE IF NOT EXISTS` would keep them). Read on first use, not at import."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    return ddl, zlib.crc32(ddl.encode("utf-8")) & 0x7FFFFFFF or 1


class Store:
    """The CSV archive and its SQLite query layer under one data directory.

    Construction does no I/O; `open` creates what is missing and checks the query layer
    against the archive. `retention_days` bounds the event log in the query layer (None
    keeps it all); `now` is the clock, for tests.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        retention_days: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.archive_dir = self.data_dir / ARCHIVE_DIR
        self.db_path = self.data_dir / DB_NAME
        self.retention_days = retention_days
        # True once a write reached the archive but not the query layer; the next `open`
        # rebuilds.
        self.stale = False
        self.stale_reason: str | None = None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        # (table, month) -> the archive file being appended to, its header checked.
        self._files: dict[tuple[str, str], Path] = {}

    def __repr__(self) -> str:
        return f"Store({str(self.data_dir)!r}, open={self.is_open})"

    @property
    def is_open(self) -> bool:
        return self._db is not None

    # -- Opening -----------------------------------------------------------------------

    def open(self) -> None:
        """Open the archive and the query layer, creating the SQLite schema if needed, and
        rebuild the query layer from the archive if the two are out of step."""
        with self._lock:
            if self._db is not None:
                return
            try:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(f"cannot create {self.archive_dir}: {exc.strerror}") from exc
            self.stale, self.stale_reason = False, None
            try:
                try:
                    reason = self._connect() or self._out_of_step()
                    if reason:
                        self._rebuild(reason)
                except sqlite3.DatabaseError as exc:
                    # Nothing in it is irreplaceable, but keep it for whoever wants to look.
                    self._close_db()
                    aside = self._move_aside()
                    log.error("the query layer is broken (%s); moved it to %s", exc, aside)
                    self._connect()
                    self._rebuild(f"the database was broken ({exc}); the old one is {aside.name}")
            except Exception as exc:
                self._close_db()
                if isinstance(exc, StoreError):
                    raise
                raise StoreError(f"cannot open the store in {self.data_dir}: {exc}") from exc

    def _rebuild(self, reason: str) -> None:
        counts = self.rebuild_from_csv()
        rows = sum(n for table, n in counts.items() if table in _STREAMS)
        log.warning("rebuilt the query layer from the archive (%s): %d rows", reason, rows)
        self.log_event(
            "warning",
            "store",
            f"Rebuilt the query layer from the archive: {reason}.",
            detail=counts,
        )

    def _connect(self) -> str | None:
        """Connect, recreating a query layer of another schema version; return why it was
        recreated, or None."""
        ddl, version = _schema()
        reason = None
        # Held by the store from the first moment, so a failure anywhere below (a broken
        # file fails its first read) still has it closed by whoever handles the failure.
        db = self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        found = db.execute("PRAGMA user_version").fetchone()[0]
        tables = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if found != version and tables:
            # Derived data in an old shape: drop it and rebuild in the current one.
            self._close_db()
            for suffix in ("", "-wal", "-shm"):
                self.db_path.with_name(DB_NAME + suffix).unlink(missing_ok=True)
            db = self._db = sqlite3.connect(self.db_path, check_same_thread=False)
            reason = "the query layer's schema changed"
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(ddl)
        db.execute(f"PRAGMA user_version = {version}")
        return reason

    def _move_aside(self) -> Path:
        stamp = self._now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = self.db_path.with_name(f"{DB_NAME}.broken-{stamp}")
        for suffix in ("", "-wal", "-shm"):
            source = self.db_path.with_name(DB_NAME + suffix)
            if source.exists():
                source.rename(aside.with_name(aside.name + suffix))
        return aside

    def _out_of_step(self) -> str | None:
        """Why the query layer doesn't hold exactly the archive, or None if it does."""
        assert self._db is not None
        recorded = {
            row["path"]: (row["size"], row["mtime_ns"])
            for row in self._db.execute("SELECT * FROM archive_file")
        }
        actual = {path: _signature(path_) for path, path_ in self._archive_files()}
        if recorded == actual:
            return None
        if not recorded:
            return "the query layer is empty"
        missing = sorted(set(recorded) - set(actual))
        if missing:
            return f"archive files are gone: {', '.join(missing[:3])}"
        return "the archive holds rows the query layer doesn't, or was edited"

    def close(self) -> None:
        """Flush and close the archive and the query layer."""
        with self._lock:
            self._close_db()

    def _close_db(self) -> None:
        db, self._db = self._db, None
        self._files.clear()
        if db is not None:
            db.close()

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise StoreError("the store is not open")
        return self._db

    # -- Writing -----------------------------------------------------------------------

    def append_heartbeat(
        self, heartbeat: Mapping[str, Any], readings: Sequence[Mapping[str, Any]]
    ) -> int | None:
        """Record one observation tick and its per-bulb readings; return the heartbeat's
        id, or None if only the archive took it."""
        row = self._row("heartbeat", heartbeat)
        children = self._children("bulb_reading", row, readings)
        return self._append("heartbeat", row, "bulb_reading", children)

    def append_reactive_event(
        self, event: Mapping[str, Any], changes: Sequence[Mapping[str, Any]]
    ) -> int | None:
        """Record one consolidated manual change and its per-bulb before/after; return the
        event's id, or None if only the archive took it."""
        row = self._row("reactive_event", event)
        children = self._children("reactive_change", row, changes)
        return self._append("reactive_event", row, "reactive_change", children)

    def append_almanac(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        """Record insert-only almanac snapshots."""
        rows = [self._row("almanac", snapshot) for snapshot in snapshots]
        with self._lock:
            db = self._conn()
            writes = self._archive("almanac", rows)
            self._commit(lambda: self._insert_rows(db, "almanac", rows), writes)

    def log_event(
        self,
        level: str,
        category: str,
        message: str,
        *,
        room_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> int | None:
        """Append an entry to the event log; return its id."""
        row = self._row(
            "event_log",
            {
                "level": level,
                "category": category,
                "message": message,
                "room_id": room_id,
                "detail": detail,
            },
        )
        with self._lock:
            db = self._conn()
            writes = self._archive("event_log", [row])
            return self._commit(lambda: self._insert(db, "event_log", row), writes)

    def _append(
        self, table: str, row: dict[str, Any], child_table: str, children: list[dict[str, Any]]
    ) -> int | None:
        with self._lock:
            db = self._conn()
            if db.execute(
                f"SELECT 1 FROM {table} WHERE room_id = ? AND ts = ?", (row["room_id"], row["ts"])
            ).fetchone():
                raise ValueError(
                    f"{table} for room '{row['room_id']}' at {row['ts']} is already recorded"
                )
            # Children first: a write cut short between the two files leaves children with
            # no parent, which a rebuild skips, never a parent silently missing its rows.
            writes = self._archive(child_table, children)
            writes += self._archive(table, [row])
            _, fk = _PARENTS[child_table]

            def insert() -> int:
                parent_id = self._insert(db, table, row)
                self._insert_rows(db, child_table, children, {fk: parent_id})
                return parent_id

            return self._commit(insert, writes)

    def _commit(self, write: Callable[[], Any], writes: Iterable[tuple[Path, int]]) -> Any:
        """Run `write` and move the archive files' records forward, in one transaction. The
        rows are already archived, so a failure here costs nothing but the query layer's
        currency: the record stays behind, and the next `open` rebuilds."""
        db = self._conn()
        try:
            with db:
                result = write()
                self._record_writes(db, writes)
            return result
        except sqlite3.Error as exc:
            log.exception("a query-layer write failed; the archive has the row")
            self._fall_behind(f"a write to the query layer failed ({exc})")
            return None

    def _fall_behind(self, why: str) -> None:
        if not self.stale:
            log.warning("the query layer is behind the archive (%s); the next start rebuilds", why)
        self.stale = True
        self.stale_reason = self.stale_reason or why

    def _children(
        self, table: str, parent: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        rows = [
            self._row(table, {**item, "ts": parent["ts"], "room_id": parent["room_id"]})
            for item in items
        ]
        bulbs = [row["bulb_id"] for row in rows]
        if len(set(bulbs)) != len(bulbs):
            raise ValueError(f"{table}: a bulb appears more than once: {sorted(bulbs)}")
        return rows

    @staticmethod
    def _insert(db: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> int:
        columns = list(row)
        cursor = db.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [row[c] for c in columns],
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    @staticmethod
    def _insert_rows(
        db: sqlite3.Connection,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if not rows:
            return
        columns = [c for c in _sql_columns(table)]
        db.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [[{**row, **(extra or {})}[c] for c in columns] for row in rows],
        )

    # -- Validation --------------------------------------------------------------------

    def _row(self, table: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Check `values` against the table and return the row as it is stored (see
        `_check_row`), its `ts` filled in with now when missing."""
        known = {c.name for c in _STREAMS[table]}
        unknown = [f"unknown column '{k}'" for k in values if k not in known]
        if unknown:
            raise ValueError(f"{table}: {'; '.join(unknown)}")
        if values.get("ts") is None:
            values = {**values, "ts": self._now()}
        return _check_row(table, values)

    # -- The archive -------------------------------------------------------------------

    def _archive(self, table: str, rows: Sequence[Mapping[str, Any]]) -> list[tuple[Path, int]]:
        """Append rows to their CSV streams, on disk before this returns; return each file
        written with its size before the write."""
        by_month: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_month.setdefault(row["ts"][:7], []).append(row)
        header = [c.name for c in _STREAMS[table]]
        written = []
        for month, month_rows in by_month.items():
            path = self._file(table, month, header)
            before = path.stat().st_size if path.exists() else 0
            try:
                with path.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    if before == 0:
                        writer.writerow(header)
                    writer.writerows(
                        [[_to_csv(row[name]) for name in header] for row in month_rows]
                    )
                    handle.flush()
                    # On disk before SQLite commits: otherwise a power cut could leave the
                    # query layer holding rows its source of truth lost.
                    os.fsync(handle.fileno())
            except BaseException:
                # A write cut short (a full disk) leaves a partial line the next append would
                # run into: take it back, and look for the file afresh next time.
                self._files.pop((table, month), None)
                with suppress(OSError):
                    os.truncate(path, before)
                raise
            written.append((path, before))
        return written

    def _file(self, table: str, month: str, header: list[str]) -> Path:
        cached = self._files.get((table, month))
        if cached is not None:
            return cached
        directory = self.archive_dir / table
        directory.mkdir(parents=True, exist_ok=True)
        n = 0
        while True:
            path = directory / (f"{month}.csv" if n == 0 else f"{month}.{n}.csv")
            if not path.exists() or path.stat().st_size == 0 or _appendable(path, header):
                break
            n += 1
        if n:
            log.warning(
                "%s %s: the file has other columns, or ends in a torn line; appending to %s",
                table,
                month,
                path.name,
            )
        self._files[(table, month)] = path
        return path

    def _archive_files(self) -> Iterator[tuple[str, Path]]:
        """Every archive file, as (path under the archive, path), streams in insert order
        and each stream's files in the order they were written."""
        for table in _ORDER:
            directory = self.archive_dir / table
            if directory.is_dir():
                for path in sorted(directory.glob("*.csv"), key=_file_order):
                    yield f"{table}/{path.name}", path

    def _record_writes(self, db: sqlite3.Connection, writes: Iterable[tuple[Path, int]]) -> None:
        """Move each written file's record forward, but only from where this write began.
        If the record is somewhere else, the file holds something the query layer never
        took (a write that failed here, or a crash between the two): the record must stay
        behind, or this write's new size would cover the gap and no start would rebuild."""
        for path, before in writes:
            name = path.relative_to(self.archive_dir).as_posix()
            held = db.execute("SELECT size FROM archive_file WHERE path = ?", (name,)).fetchone()
            if self.stale or (held["size"] if held else 0) != before:
                self._fall_behind(f"{name} holds rows the query layer doesn't")
                continue
            db.execute(
                "INSERT OR REPLACE INTO archive_file (path, size, mtime_ns) VALUES (?, ?, ?)",
                (name, *_signature(path)),
            )

    def _record_all(self, db: sqlite3.Connection, files: Iterable[Path]) -> None:
        db.executemany(
            "INSERT OR REPLACE INTO archive_file (path, size, mtime_ns) VALUES (?, ?, ?)",
            [(path.relative_to(self.archive_dir).as_posix(), *_signature(path)) for path in files],
        )

    # -- Re-ingest ---------------------------------------------------------------------

    def rebuild_from_csv(self) -> dict[str, int]:
        """CSV re-ingest: rebuild the SQLite query layer from the CSV archive.

        Replaces everything in the query layer, in one transaction. A row that doesn't
        decode, or a child whose parent isn't in the archive, is skipped and counted
        (`skipped`); event-log rows older than the retention are left out, as `prune_event_log`
        would have left them. Returns the rows ingested per table.
        """
        with self._lock:
            db = self._conn()
            counts = {table: 0 for table in _ORDER} | {"skipped": 0}
            cutoff = self._retention_cutoff()
            keys: dict[str, dict[tuple[str, str], int]] = {"heartbeat": {}, "reactive_event": {}}
            with db:
                for table in reversed(_ORDER):
                    db.execute(f"DELETE FROM {table}")
                db.execute("DELETE FROM archive_file")
                files = []
                for name, path in self._archive_files():
                    table = name.partition("/")[0]
                    files.append(path)
                    for row in self._read(path, table, counts):
                        try:
                            self._ingest(db, table, row, keys, counts, cutoff)
                        except (sqlite3.IntegrityError, OverflowError) as exc:
                            counts["skipped"] += 1
                            log.warning("skipping a %s row from %s: %s", table, path, exc)
                self._record_all(db, files)
            self._files.clear()
            self.stale, self.stale_reason = False, None
            return counts

    def _ingest(
        self,
        db: sqlite3.Connection,
        table: str,
        row: dict[str, Any],
        keys: dict[str, dict[tuple[str, str], int]],
        counts: dict[str, int],
        cutoff: str | None,
    ) -> None:
        if table in keys:
            cursor = db.execute(
                f"INSERT OR IGNORE INTO {table} ({', '.join(row)}) "
                f"VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
            key = (row["room_id"], row["ts"])
            if cursor.rowcount:
                keys[table][key] = cursor.lastrowid  # type: ignore[assignment]
                counts[table] += 1
            else:
                counts["skipped"] += 1  # the same parent archived twice
        elif table in _PARENTS:
            parent, fk = _PARENTS[table]
            parent_id = keys[parent].get((row["room_id"], row["ts"]))
            if parent_id is None:
                counts["skipped"] += 1  # orphan: its parent never landed
                return
            child = {k: v for k, v in row.items() if k not in ("ts", "room_id")}
            cursor = db.execute(
                f"INSERT OR IGNORE INTO {table} ({fk}, {', '.join(child)}) "
                f"VALUES (?, {', '.join('?' * len(child))})",
                [parent_id, *child.values()],
            )
            counts[table] += cursor.rowcount
            counts["skipped"] += 1 - cursor.rowcount
        elif table == "event_log" and cutoff and row["ts"] < cutoff:
            return
        else:
            self._insert(db, table, row)
            counts[table] += 1

    def _read(self, path: Path, table: str, counts: dict[str, int]) -> Iterator[dict[str, Any]]:
        """The rows of one archive file, decoded by the file's own header: a column it lacks
        is empty, one the stream no longer has is ignored.

        Whatever it can't read is skipped and counted, never raised: one bad line (a hand
        edit, a file re-saved by a spreadsheet) must not stop the rebuild, and with it the
        app's start. A byte that isn't UTF-8 becomes U+FFFD; a BOM is dropped; a line the
        CSV reader can't parse at all ends the file there. Every row the store writes ends
        in a newline, so a last row without one was cut short (a crash mid-write) and is
        dropped: the reader would take `12` torn from `128` as a whole value.
        """
        columns = _STREAMS[table]
        torn = path.stat().st_size > 0 and not _ends_a_line(path)
        held: tuple[int, dict[str, Any]] | None = None
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            while True:
                try:
                    raw = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    counts["skipped"] += 1
                    log.warning("stopped reading %s at line %d: %s", path, reader.line_num, exc)
                    held = None
                    break
                if held is not None:
                    yield from self._decode(path, table, columns, *held, counts)
                held = (reader.line_num, raw)
        if held is not None:
            if torn:
                counts["skipped"] += 1
                log.warning("skipping %s line %d: cut short", path, held[0])
            else:
                yield from self._decode(path, table, columns, *held, counts)

    @staticmethod
    def _decode(
        path: Path,
        table: str,
        columns: tuple[_Column, ...],
        line: int,
        raw: Mapping[str, Any],
        counts: dict[str, int],
    ) -> Iterator[dict[str, Any]]:
        try:
            # Checked exactly as a write is, so a line edited by hand into something the
            # database would refuse is skipped here, not left to abort the whole rebuild
            # on a constraint.
            yield _check_row(table, {c.name: _from_csv(c, raw.get(c.name)) for c in columns})
        except ValueError as exc:
            counts["skipped"] += 1
            log.warning("skipping %s line %d: %s", path, line, exc)

    # -- Reading -----------------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a read-only query (one SELECT, or WITH ... SELECT) against the SQLite layer."""
        if not re.match(r"\s*(select|with)\b", sql, re.IGNORECASE):
            # Not even a PRAGMA: query_only lets some through (foreign_keys, journal_mode),
            # and they would stay set on the store's one connection.
            raise ValueError("query runs SELECT statements only")
        with self._lock:
            db = self._conn()
            # query_only refuses any write in the statement, so a caller can't put state
            # into the query layer that the archive doesn't have.
            db.execute("PRAGMA query_only = ON")
            try:
                return [dict(row) for row in db.execute(sql, params).fetchall()]
            finally:
                if db.in_transaction:
                    db.rollback()  # a refused write leaves its implicit BEGIN open
                db.execute("PRAGMA query_only = OFF")

    def events(
        self,
        *,
        level: str | None = None,
        category: str | None = None,
        room_id: str | None = None,
        before: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Event-log entries, newest first: at `level` or above, in `category`, for
        `room_id`, older than the entry with id `before`. `detail` is decoded."""
        clauses: list[str] = []
        params: list[Any] = []
        if level is not None:
            if level not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            wanted = LEVELS[LEVELS.index(level) :]
            clauses.append(f"level IN ({', '.join('?' * len(wanted))})")
            params += wanted
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if room_id is not None:
            clauses.append("room_id = ?")
            params.append(room_id)
        if before is not None:
            clauses.append("id < ?")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.query(
            f"SELECT * FROM event_log {where} ORDER BY id DESC LIMIT ?", [*params, int(limit)]
        )
        for row in rows:
            try:
                row["detail"] = json.loads(row["detail"]) if row["detail"] else None
            except ValueError:
                pass  # shown as the text it is; the store only writes JSON
        return rows

    def event_facets(self) -> dict[str, list[str]]:
        """The categories and rooms the event log holds, for its filters."""
        return {
            "categories": [
                r["category"]
                for r in self.query("SELECT DISTINCT category FROM event_log ORDER BY category")
            ],
            "rooms": [
                r["room_id"]
                for r in self.query(
                    "SELECT DISTINCT room_id FROM event_log WHERE room_id IS NOT NULL "
                    "ORDER BY room_id"
                )
            ],
        }

    def summary(self) -> dict[str, Any]:
        """What has been recorded, for the status line: counts and the latest times."""
        heartbeats = self.query("SELECT COUNT(*) AS n, MAX(ts) AS last FROM heartbeat")[0]
        reactive = self.query("SELECT COUNT(*) AS n, MAX(ts) AS last FROM reactive_event")[0]
        return {
            "heartbeats": heartbeats["n"],
            "last_heartbeat": heartbeats["last"],
            "reactive_events": reactive["n"],
            "last_reactive_event": reactive["last"],
            "stale": self.stale,
            "stale_reason": self.stale_reason,
        }

    # -- Retention ---------------------------------------------------------------------

    def prune_event_log(self) -> int:
        """Drop event-log entries older than `retention_days` from the query layer (the
        archive keeps them); return how many went."""
        cutoff = self._retention_cutoff()
        if cutoff is None:
            return 0
        with self._lock:
            db = self._conn()
            with db:
                return db.execute("DELETE FROM event_log WHERE ts < ?", (cutoff,)).rowcount

    def _retention_cutoff(self) -> str | None:
        if not self.retention_days:
            return None
        try:
            return timestamp(self._now() - timedelta(days=self.retention_days))
        except (OverflowError, ValueError):
            return None  # further back than dates go: keep everything


# -- Values ----------------------------------------------------------------------------


def _check_row(table: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """`values` as the table stores them: every column, normalised (booleans 0/1, JSON as
    text, `ts` in the one format), defaults filled in, ranges and levels checked. Raises
    ValueError listing every problem. Shared by writing and re-ingest, so the archive and
    the query layer accept exactly the same rows."""
    problems = []
    row: dict[str, Any] = {}
    for column in _STREAMS[table]:
        value = values.get(column.name)
        if value is None or value == "":
            value = _DEFAULTS.get(column.name, value)
        try:
            row[column.name] = _normalise(column, value)
        except ValueError as exc:
            problems.append(f"{column.name}: {exc}")
    if table == "event_log" and "level" in row and row["level"] not in LEVELS:
        problems.append(f"level: must be one of {', '.join(LEVELS)}")
    if problems:
        raise ValueError(f"{table}: {'; '.join(problems)}")
    return row


def _sql_columns(table: str) -> list[str]:
    names = [c.name for c in _STREAMS[table]]
    if table in _PARENTS:
        names = [n for n in names if n not in ("ts", "room_id")] + [_PARENTS[table][1]]
    return names


def _normalise(column: _Column, value: Any) -> Any:
    """A value as the store keeps it, or ValueError."""
    if value is None or (isinstance(value, str) and value == "" and column.kind != "json"):
        if column.required:
            raise ValueError("required")
        return None
    kind = column.kind
    if kind == "ts":
        return timestamp(value)
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError(f"must be text, not {type(value).__name__}")
        return value
    if kind == "bool":
        if isinstance(value, bool) or value in (0, 1):
            return int(value)
        raise ValueError(f"must be true or false, not {value!r}")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"must be a whole number, not {value!r}")
        if not _INT64[0] <= value <= _INT64[1]:
            raise ValueError(f"{value} is out of range")
        return _in_range(column.name, value)
    if kind == "real":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"must be a number, not {value!r}")
        try:
            number = float(value)
        except OverflowError:
            raise ValueError(f"{value} is out of range") from None
        if not math.isfinite(number):
            raise ValueError(f"must be a finite number, not {value!r}")
        return _in_range(column.name, number)
    if kind == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("not JSON") from None
        # Canonical, and strict JSON (no NaN), so whatever reads it back can parse it.
        return json.dumps(
            _finite(value), sort_keys=True, ensure_ascii=False, default=str, allow_nan=False
        )
    raise AssertionError(kind)


def _finite(value: Any) -> Any:
    """`value` with every non-finite float (NaN, infinity) as None, all the way down."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _in_range(name: str, value: float) -> Any:
    low, high = _RANGES.get(name, (None, None))
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f"{value} is out of range")
    return value


def _to_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)  # round-trips exactly
    return str(value)


def _from_csv(column: _Column, text: str | None) -> Any:
    if text is None or text == "":
        return None
    if column.kind in ("int", "bool"):
        return int(text)
    if column.kind == "real":
        return float(text)
    return text


def _signature(path: Path) -> tuple[int, int]:
    """What says an archive file changed: its size, and its mtime for an edit that keeps
    the size."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _ends_a_line(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def _appendable(path: Path, header: list[str]) -> bool:
    """Whether new rows can follow a file's last: same columns, and its last line ended."""
    if not _ends_a_line(path):
        return False
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        try:
            return next(csv.reader(handle), None) == header
        except csv.Error:
            return False


def _file_order(path: Path) -> tuple[str, int]:
    month, _, n = path.stem.partition(".")
    return month, int(n) if n.isdigit() else 0
