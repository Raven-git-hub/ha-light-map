"""Inventory: discover every light.* entity; build rooms from HA Areas; manage switching
groups; keep the committed bulb->room snapshot in sync with HA (read Areas to refresh,
write back on edit).

A room is an HA Area and contains bulbs; a bulb is one light.* entity, the actuation atom.
Bulbs with no Area sit in an "Unassigned" bucket (the config's `unassigned` list).
Switching groups (UI: "group soft switch") are user-created per room and are independent
of the adaptive module's learning units.

A bulb is identified by its HA entity-registry entry, recorded as its `ha_match_key`, so
renaming the light's entity_id in HA keeps the same bulb (its id, hard switches, and
memberships). The key is opaque: it is matched, never addressed. A light outside the
registry has no key and is matched by entity_id.

A light's Area is its *effective* Area, the way HA itself resolves it: the entity's own
Area if it has one, else its device's. Most lights get their Area from their device and
carry none of their own, so reading only the entity registry would put nearly every bulb
in Unassigned.

HA light groups (a `light.*` whose state lists member `entity_id`s, or a Hue room/zone)
are discovered and flagged but are not bulbs: driving a group as well as its members
would command the same lights twice. Use the member lights and a switching group.

The edits that write to HA (`move_bulb`, `create_room`, `rename_room`) change only HA's
registries. Follow them with `sync_snapshot`, then save the config, to commit the change to
the snapshot.

Hard rules:

- HA Areas are the source of truth for room membership. The config holds a committed
  snapshot of the resolved mapping (with each room's ha_area_id) so generation stays a pure,
  deterministic function of the config. An Area edited directly in HA shows stale until
  refreshed; a deploy uses the committed snapshot.
- Registry writes (reassigning a bulb's Area, creating or renaming an Area) are verified
  after the fact and reconcilable, never destructive by surprise: each checks its
  preconditions before writing, writes only what was asked, and re-reads HA to confirm the
  write landed.
- Entity IDs only, never device IDs. The device registry is read only to resolve an
  inherited Area; device IDs never reach the result or the config. A bulb's
  `ha_match_key` is an opaque match key: matched, never addressed or validated.
"""

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from app.core import config as config_mod
from app.core.config import ConfigError
from app.core.ha import HAClient

UNASSIGNED = "Unassigned"


class InventoryError(Exception):
    """A registry edit was refused before writing, or did not land as asked."""


# -- Reading ---------------------------------------------------------------------------


async def discover_lights(ha: HAClient) -> list[dict[str, Any]]:
    """Return every light.* entity in HA, each with its Area (if any) from the registry."""
    states = await ha.get_states()
    entities = await ha.list_entity_registry()
    devices = await ha.list_device_registry()
    return join_lights(states, entities, devices)


def join_lights(
    states: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    devices: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Join HA's states to its entity and device registries: one record per light.*.

    Each record: `entity_id`; `ha_match_key` (the registry entry's `id`, or None);
    `name` (HA's friendly name); `state`; `area_id` (the effective Area, or None);
    `area_inherited` (True when that Area comes from the device); `in_registry` (False for
    a light with no registry entry, e.g. a YAML light with no unique_id, which HA can't
    give an Area at all); `is_group`. Sorted by entity_id.

    The match key is the registry entry's `id` rather than the integration's `unique_id`:
    `id` is HA's own handle for the entry, kept across entity_id renames and unique
    across integrations, while a `unique_id` is unique only within its integration and
    often embeds hardware identifiers that have no business in a committed config.
    """
    entries = {e["entity_id"]: e for e in entities if isinstance(e.get("entity_id"), str)}
    device_areas = {d["id"]: d.get("area_id") for d in devices if isinstance(d.get("id"), str)}
    lights = []
    for state in states:
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("light."):
            continue
        attributes = state.get("attributes") or {}
        entry = entries.get(entity_id)
        area_id, inherited = _effective_area(entry, device_areas)
        key = entry.get("id") if entry is not None else None
        lights.append(
            {
                "entity_id": entity_id,
                "ha_match_key": key if isinstance(key, str) and key else None,
                "name": attributes.get("friendly_name") or entity_id.partition(".")[2],
                "state": state.get("state"),
                "area_id": area_id,
                "area_inherited": inherited,
                "in_registry": entry is not None,
                "is_group": is_light_group(attributes),
            }
        )
    lights.sort(key=lambda light: light["entity_id"])
    return lights


def is_light_group(attributes: Mapping[str, Any]) -> bool:
    """True for a light that is really a group of lights.

    HA's group platform (light groups, old-style groups) lists its members in an
    `entity_id` attribute; Hue rooms and zones set `is_hue_group`. Groups from other
    integrations may not be detectable this way.
    """
    return isinstance(attributes.get("entity_id"), list) or attributes.get("is_hue_group") is True


def _effective_area(
    entry: Mapping[str, Any] | None, device_areas: Mapping[str, str | None]
) -> tuple[str | None, bool]:
    if entry is None:
        return None, False
    if entry.get("area_id"):
        return entry["area_id"], False
    device_area = device_areas.get(entry.get("device_id")) if entry.get("device_id") else None
    return (device_area, True) if device_area else (None, False)


async def read_rooms(ha: HAClient) -> list[dict[str, Any]]:
    """Build rooms from HA's Areas and the lights in them, plus the Unassigned bucket."""
    areas = await ha.list_areas()
    lights = await discover_lights(ha)
    return bucket_rooms(areas, lights)


def bucket_rooms(
    areas: Sequence[Mapping[str, Any]], lights: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Bucket lights into their Areas: `{area_id, name, bulbs}` per Area, sorted by name,
    then the Unassigned bucket (`area_id` None) last, always present.

    Every Area appears, with or without lights. Light groups are left out (they are not
    bulbs), and a light whose Area HA doesn't list lands in Unassigned.
    """
    rooms: dict[str, dict[str, Any]] = {}
    for area in areas:
        area_id = area.get("area_id")
        if isinstance(area_id, str) and area_id:
            rooms[area_id] = {"area_id": area_id, "name": area.get("name") or area_id, "bulbs": []}
    unassigned: dict[str, Any] = {"area_id": None, "name": UNASSIGNED, "bulbs": []}
    for light in sorted(lights, key=lambda light: light["entity_id"]):
        if not light.get("is_group"):
            rooms.get(light.get("area_id") or "", unassigned)["bulbs"].append(dict(light))
    ordered = sorted(rooms.values(), key=lambda room: (room["name"].casefold(), room["area_id"]))
    return [*ordered, unassigned]


async def sync_snapshot(ha: HAClient, config: Mapping[str, Any]) -> dict[str, Any]:
    """Read Areas from HA and return the config with its room snapshot refreshed.

    The merge itself is `core.config.reconcile_area_snapshot`; this side does the HA reads.
    Nothing is written: show `config.snapshot_changes(config, result)`, then save.
    """
    areas = await ha.list_areas()
    lights = await discover_lights(ha)
    return config_mod.reconcile_area_snapshot(config, areas, lights)


# -- Writing back to HA ----------------------------------------------------------------


async def move_bulb(ha: HAClient, entity_id: str, area_id: str | None) -> None:
    """Write a bulb's room change back to HA as an Area reassignment.

    `area_id` None moves the bulb to Unassigned. Refused, without writing, when the light
    has no registry entry, the Area doesn't exist, or the bulb would stay in its device's
    Area anyway. Verified by re-reading the registry; raises InventoryError if the bulb did
    not end up where asked.
    """
    if not entity_id.startswith("light."):
        raise InventoryError(f"{entity_id} is not a light")
    entry = await ha.get_entity_registry_entry(entity_id)
    if entry is None:
        raise InventoryError(
            f"{entity_id} has no entity-registry entry (no unique_id), so HA can't give it an Area"
        )
    if area_id is not None and area_id not in {a.get("area_id") for a in await ha.list_areas()}:
        raise InventoryError(f"there is no HA Area '{area_id}'")
    if await _area_of(ha, entry) == area_id:
        return
    if area_id is None and entry.get("device_id"):
        device_area = await _area_of(ha, {**entry, "area_id": None})
        if device_area is not None:
            # Clearing the entity's own Area would leave it in its device's, so the write
            # would report success while the bulb stayed put. Unassigning the device
            # instead would move every other entity on it too: that is the user's call, in
            # HA, not a side effect of moving one bulb.
            raise InventoryError(
                f"{entity_id} inherits Area '{device_area}' from its device; clear the "
                "device's Area in HA to unassign it"
            )
    await ha.set_entity_area(entity_id, area_id)
    # Verified by re-reading rather than trusting the write's echo.
    after = await ha.get_entity_registry_entry(entity_id)
    landed = await _area_of(ha, after) if after is not None else None
    if landed != area_id:
        raise InventoryError(
            f"moving {entity_id} to {area_id or UNASSIGNED} did not land: HA has it in "
            f"{landed or UNASSIGNED}"
        )


async def _area_of(ha: HAClient, entry: Mapping[str, Any]) -> str | None:
    """A registry entry's effective Area, reading the device registry only if needed."""
    devices = (
        await ha.list_device_registry()
        if entry.get("device_id") and not entry.get("area_id")
        else []
    )
    return _effective_area(entry, {d.get("id"): d.get("area_id") for d in devices})[0]


async def create_room(ha: HAClient, name: str) -> dict[str, Any]:
    """Create a room by creating its HA Area; return the verified Area entry."""
    name = name.strip()
    if not name:
        raise InventoryError("a room needs a name")
    clash = _same_name(await ha.list_areas(), name)
    if clash is not None:
        raise InventoryError(f"HA already has an Area named {clash.get('name')!r}")
    created = await ha.create_area(name)
    area_id = created.get("area_id") if isinstance(created, dict) else None
    return await _verify_area(ha, area_id, name, f"creating Area {name!r}")


async def rename_room(ha: HAClient, area_id: str, name: str) -> dict[str, Any]:
    """Rename a room by renaming its HA Area; return the verified Area entry.

    The Area's id, and so the room's, is unchanged.
    """
    name = name.strip()
    if not name:
        raise InventoryError("a room needs a name")
    areas = await ha.list_areas()
    current = next((a for a in areas if a.get("area_id") == area_id), None)
    if current is None:
        raise InventoryError(f"there is no HA Area '{area_id}'")
    if current.get("name") == name:
        return dict(current)
    clash = _same_name([a for a in areas if a.get("area_id") != area_id], name)
    if clash is not None:
        raise InventoryError(f"HA already has an Area named {clash.get('name')!r}")
    await ha.rename_area(area_id, name)
    return await _verify_area(ha, area_id, name, f"renaming Area '{area_id}'")


def _same_name(areas: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    # HA refuses an Area name that matches an existing one ignoring case and spacing;
    # checking first turns a failed write into a clear message.
    def key(text: Any) -> str:
        return "".join(str(text).casefold().split())

    return next((a for a in areas if key(a.get("name", "")) == key(name)), None)


async def _verify_area(ha: HAClient, area_id: Any, name: str, what: str) -> dict[str, Any]:
    landed = next((a for a in await ha.list_areas() if a.get("area_id") == area_id), None)
    if not area_id or landed is None or landed.get("name") != name:
        raise InventoryError(f"{what} did not land as asked")
    return dict(landed)


# -- Switching groups (config edits only) ----------------------------------------------


def upsert_switching_group(
    config: Mapping[str, Any], room_id: str, group: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the config with a switching group added to, or replaced in, a room.

    A pure edit: HA is not touched, and the input is not modified. Whether the group's
    members resolve is checked when the config is saved (validate-then-write).
    """
    doc = copy.deepcopy(dict(config))
    room = _room(doc, room_id)
    group = copy.deepcopy(dict(group))
    if not group.get("id"):
        raise ConfigError("a switching group needs an id")
    groups = room.setdefault("switching_groups", [])
    for index, existing in enumerate(groups):
        if existing.get("id") == group["id"]:
            groups[index] = group
            break
    else:
        groups.append(group)
    return doc


def remove_switching_group(
    config: Mapping[str, Any], room_id: str, group_id: str
) -> dict[str, Any]:
    """Return the config with a switching group removed from a room.

    A pure edit, like `upsert_switching_group`; the group's hard switches go with it.
    """
    doc = copy.deepcopy(dict(config))
    room = _room(doc, room_id)
    groups = room.get("switching_groups", [])
    remaining = [g for g in groups if g.get("id") != group_id]
    if len(remaining) == len(groups):
        raise ConfigError(f"room '{room_id}' has no switching group '{group_id}'")
    room["switching_groups"] = remaining
    return doc


def _room(doc: dict[str, Any], room_id: str) -> dict[str, Any]:
    room = next((r for r in doc.get("rooms", []) if r.get("id") == room_id), None)
    if room is None:
        raise ConfigError(f"no room '{room_id}'")
    return room
