"""Inventory: discover every light.* entity; build rooms from HA Areas; manage switching
groups; keep the committed bulb->room snapshot in sync with HA (read Areas to refresh,
write back on edit).

A room is an HA Area and contains bulbs; a bulb is one light.* entity, the actuation atom.
Bulbs with no Area sit in an "Unassigned" bucket. Switching groups (UI: "group soft
switch") are user-created per room and are independent of the adaptive module's learning
units.

Hard rules:

- HA Areas are the source of truth for room membership. The config holds a committed
  snapshot of the resolved mapping (with each room's ha_area_id) so generation stays a pure,
  deterministic function of the config. An Area edited directly in HA shows stale until
  refreshed; a deploy uses the committed snapshot.
- Registry writes (reassigning a bulb's Area, creating or renaming an Area) are verified
  after the fact and reconcilable, never destructive by surprise.
- Entity IDs only, never device IDs.
"""

from collections.abc import Mapping
from typing import Any

from app.core.ha import HAClient


async def discover_lights(ha: HAClient) -> list[dict[str, Any]]:
    """Return every light.* entity in HA, each with its Area (if any) from the registry."""
    raise NotImplementedError


async def read_rooms(ha: HAClient) -> list[dict[str, Any]]:
    """Build rooms from HA's Areas and the lights in them, plus the Unassigned bucket."""
    raise NotImplementedError


async def sync_snapshot(ha: HAClient, config: Mapping[str, Any]) -> dict[str, Any]:
    """Read Areas from HA and return the config with its room snapshot refreshed.

    The merge itself is `core.config.reconcile_area_snapshot`; this side does the HA reads.
    """
    raise NotImplementedError


async def move_bulb(ha: HAClient, entity_id: str, area_id: str | None) -> None:
    """Write a bulb's room change back to HA as an Area reassignment."""
    raise NotImplementedError


async def create_room(ha: HAClient, name: str) -> dict[str, Any]:
    """Create a room by creating its HA Area."""
    raise NotImplementedError


async def rename_room(ha: HAClient, area_id: str, name: str) -> None:
    """Rename a room by renaming its HA Area."""
    raise NotImplementedError


def upsert_switching_group(
    config: Mapping[str, Any], room_id: str, group: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the config with a switching group added to, or replaced in, a room."""
    raise NotImplementedError


def remove_switching_group(config: Mapping[str, Any], room_id: str, group_id: str) -> dict[str, Any]:
    """Return the config with a switching group removed from a room."""
    raise NotImplementedError
