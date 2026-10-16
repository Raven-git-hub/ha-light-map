"""Storage: CSV archive (source of truth) + SQLite query layer (rebuildable). Event log. CSV
re-ingest. No state that lives only in SQLite.

Readings are stored per bulb, long format (a `heartbeat` row plus one `bulb_reading` row per
bulb), which is what learning-unit pooling needs. The almanac is insert-only snapshots keyed
by learning unit, so history comes for free. The SQLite DDL is `schema/storage.schema.sql`.

Hard rules:

- CSV is the archival source of truth; SQLite is a rebuildable query layer. Never add state
  that only lives in SQLite: everything must be reconstructable from the CSV archive.
- The almanac is insert-only; a new learned value is a new snapshot, never an update.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class Store:
    """The CSV archive and its SQLite query layer under one data directory."""

    def __init__(self, data_dir: Path) -> None:
        raise NotImplementedError

    def open(self) -> None:
        """Open the archive and the query layer, creating the SQLite schema if needed."""
        raise NotImplementedError

    def close(self) -> None:
        """Flush and close the archive and the query layer."""
        raise NotImplementedError

    def append_heartbeat(
        self, heartbeat: Mapping[str, Any], readings: Sequence[Mapping[str, Any]]
    ) -> None:
        """Record one observation tick and its per-bulb readings."""
        raise NotImplementedError

    def append_reactive_event(self, event: Mapping[str, Any]) -> None:
        """Record one consolidated manual change."""
        raise NotImplementedError

    def append_almanac(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        """Record insert-only almanac snapshots."""
        raise NotImplementedError

    def log_event(
        self,
        level: str,
        category: str,
        message: str,
        *,
        room_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Append an entry to the event log."""
        raise NotImplementedError

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a read-only query against the SQLite layer."""
        raise NotImplementedError

    def rebuild_from_csv(self) -> None:
        """CSV re-ingest: rebuild the SQLite query layer from the CSV archive."""
        raise NotImplementedError
