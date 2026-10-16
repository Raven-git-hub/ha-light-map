"""Analyser: the nightly learning pipeline. Pools per-bulb observations per LEARNING UNIT
into one target. Ported; behavioural changes gated behind flags.

The pipeline and its flag-gated diffing port from ha-adaptive-light intact. The rework:
observations are stored per bulb (long format) and pooled per learning unit here, at
analysis time, into one target per unit per section. The predecessor's per-group averaging
over on-samples carries over; a unit is now a set of bulbs rather than a single entity.

Hard rules:

- Pooling happens only here. Storage stays per bulb so learning units can be redrawn
  without losing history.
- Behavioural changes to the ported pipeline are gated behind flags.
- Reads come from the store's SQLite layer, which is rebuildable from the CSV archive.
- A lux reading of 0 is a floor, not a measurement.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.store import Store


def run(store: Store, config: Mapping[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    """Run the nightly pass over every adaptive room; return the new almanac snapshots."""
    raise NotImplementedError


def pool_unit(
    readings: Sequence[Mapping[str, Any]], unit: Mapping[str, Any], section: str
) -> dict[str, Any]:
    """Pool a learning unit's member-bulb readings for one section into one target."""
    raise NotImplementedError
