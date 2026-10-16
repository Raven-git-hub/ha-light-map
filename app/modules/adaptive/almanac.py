"""Almanac: the learned model, keyed by learning unit; pushed to HA over REST.

Stored as insert-only snapshots (the store's `almanac` table, archived to CSV), so history
comes for free. Published to HA as `sensor.al_almanac_<room>`, keyed by learning-unit id.

Hard rules:

- Insert-only: a new learned value is a new snapshot, never an update.
- Nothing from the almanac is hardcoded into generated YAML. The automations and the bridge
  read this sensor at runtime, so a change needs only a republish, not regeneration.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.ha import HAClient
from app.core.store import Store


def latest(store: Store, room_id: str) -> dict[str, Any]:
    """Return a room's current almanac: the latest snapshot per learning unit and section."""
    raise NotImplementedError


def record(store: Store, snapshots: Sequence[Mapping[str, Any]]) -> None:
    """Append new insert-only almanac snapshots."""
    raise NotImplementedError


async def publish(ha: HAClient, room_id: str, almanac: Mapping[str, Any]) -> None:
    """Push a room's almanac to HA as `sensor.al_almanac_<room>`."""
    raise NotImplementedError
