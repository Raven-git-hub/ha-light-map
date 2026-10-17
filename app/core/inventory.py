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
registries. To commit one to the snapshot, apply the same change to the config with
`snapshot_with_move` / `snapshot_with_area` and save it: they record exactly that change,
the way a sync would, without also committing whatever else HA has changed meanwhile
(which `sync_snapshot` would, and which must be reviewed through `snapshot_changes` first).

Some lights aren't room bulbs (indicator LEDs, a mug warmer's LED), and the user can set
one aside: an ignored bulb (`ignored: true`, P4.1) stays in the snapshot, followed through
syncs like any bulb so it can be un-ignored, but is left out of everything else.
`active_bulbs` is the inventory everything else works from (generation, deploy, the
observer, and the modules): every bulb that isn't ignored. Discovery still finds every
light, and nothing here ever decides on its own that a light is to be ignored.

The rest of the Map's edits are pure config edits (`set_adaptive`, `set_hard_switches`,
`set_ignored`, the switching-group edits), and so is the adaptive editor's save
(`set_adaptive_config`, P5.0): HA is not touched, and the result is checked when it is saved
(validate-then-write). `bulb_capability` reads whether a bulb can dim from its HA state,
for the adaptive editor's guard only: the config never depends on it. `live_bulbs` and `going_live` say which bulbs a hard
switch makes live: during the parallel build (P0-P4) binding one is the step that makes a
bridge authoritative beside the live ha-adaptive-light, so the Map warns before saving one.

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
- Ignoring is the user's choice, never a guess: a new light is never ignored, because
  guessing that a light is an indicator LED would hide a real bulb. An ignored bulb is in
  no switching group or learning unit and has no hard switches.
"""

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from app.core import config as config_mod
from app.core.config import ConfigError, is_ignored
from app.core.ha import HAClient

UNASSIGNED = "Unassigned"


class InventoryError(Exception):
    """A registry edit was refused before writing, or did not land as asked."""


class NotFound(ConfigError):
    """An edit named a room, bulb, or switching group the config doesn't have."""


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


async def move_bulb(
    ha: HAClient, entity_id: str, area_id: str | None, *, match_key: str | None = None
) -> None:
    """Write a bulb's room change back to HA as an Area reassignment.

    `area_id` None moves the bulb to Unassigned. Refused, without writing, when the light
    has no registry entry, the Area doesn't exist, the bulb would stay in its device's
    Area anyway, or (given the bulb's `match_key`) the light now at `entity_id` is a
    different registry entry (`check_move` says so in advance). Verified by re-reading the
    registry; raises InventoryError if the bulb did not end up where asked.
    """
    if await _preflight(ha, entity_id, area_id, match_key):
        return
    await ha.set_entity_area(entity_id, area_id)
    # Verified by re-reading rather than trusting the write's echo.
    after = await ha.get_entity_registry_entry(entity_id)
    landed = await _area_of(ha, after) if after is not None else None
    if landed != area_id:
        raise InventoryError(
            f"moving {entity_id} to {area_id or UNASSIGNED} did not land: HA has it in "
            f"{landed or UNASSIGNED}"
        )


async def check_move(
    ha: HAClient, entity_id: str, area_id: str | None, *, match_key: str | None = None
) -> str | None:
    """Why `move_bulb` would refuse this move, or None if it would go ahead (or the bulb
    is already there). Reads HA, writes nothing: lets a caller moving several bulbs
    refuse the lot before writing any (see `core.deploy.deploy_areas`)."""
    try:
        await _preflight(ha, entity_id, area_id, match_key)
    except InventoryError as exc:
        return str(exc)
    return None


async def _preflight(
    ha: HAClient, entity_id: str, area_id: str | None, match_key: str | None = None
) -> bool:
    """Check a move before writing it: raise InventoryError if it must be refused, return
    True if the bulb is already where it was asked to go."""
    if not entity_id.startswith("light."):
        raise InventoryError(f"{entity_id} is not a light")
    entry = await ha.get_entity_registry_entry(entity_id)
    if entry is None:
        raise InventoryError(
            f"{entity_id} has no entity-registry entry (no unique_id), so HA can't give it an Area"
        )
    if match_key is not None and entry.get("id") != match_key:
        # The snapshot addresses the bulb by entity_id. If the bulb's light was renamed in
        # HA and another light has taken its old entity_id, writing would move that other
        # light, and the snapshot would record the bulb as moved. The match key is the
        # bulb's identity (see "Bulb identity" in docs/DESIGN.md): compare, never address.
        raise InventoryError(
            f"{entity_id} is not this bulb's light any more (it was renamed in HA, and "
            "another light has its old entity_id); sync from HA first"
        )
    if area_id is not None and area_id not in {a.get("area_id") for a in await ha.list_areas()}:
        raise InventoryError(f"there is no HA Area '{area_id}'")
    if await _area_of(ha, entry) == area_id:
        return True
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
    return False


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


# -- Committing an HA edit to the snapshot ---------------------------------------------


def snapshot_with_move(
    config: Mapping[str, Any], bulb_id: str, room_id: str | None
) -> dict[str, Any]:
    """Return the config with one bulb moved to a room (None: Unassigned) in the snapshot,
    exactly as a sync would record it once `move_bulb` has written it to HA.

    The bulb keeps its id and hard switches; it is dropped from the switching groups and
    learning units of the room it leaves (a group or unit left empty goes with it), which
    `config.snapshot_changes` reports. Nothing else in the snapshot changes. Pure.
    """
    _, bulb = find_bulb(config, bulb_id)
    area_id = None if room_id is None else _room(config, room_id)["ha_area_id"]
    areas, lights = config_mod.snapshot_as_ha(config)
    for light in lights:
        if light["entity_id"] == bulb["entity_id"]:
            light["area_id"] = area_id
    return config_mod.reconcile_area_snapshot(config, areas, lights)


def snapshot_with_area(config: Mapping[str, Any], area: Mapping[str, Any]) -> dict[str, Any]:
    """Return the config with the room for one HA Area added (a created Area) or renamed
    (a renamed one), as a sync would record it. `area` is the verified Area entry
    `create_room` / `rename_room` return. Nothing else in the snapshot changes. Pure."""
    areas, lights = config_mod.snapshot_as_ha(config)
    areas = [a for a in areas if a["area_id"] != area.get("area_id")]
    areas.append({"area_id": area.get("area_id"), "name": area.get("name")})
    return config_mod.reconcile_area_snapshot(config, areas, lights)


# -- Config edits (pure: HA is not touched) ----------------------------------------------


def find_bulb(
    config: Mapping[str, Any], bulb_id: str
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any]]:
    """Return a bulb and its room (None for an Unassigned bulb); NotFound if no bulb has
    that id."""
    for room in config.get("rooms", []):
        for bulb in room.get("bulbs", []):
            if bulb.get("id") == bulb_id:
                return room, bulb
    for bulb in config.get("unassigned", []):
        if bulb.get("id") == bulb_id:
            return None, bulb
    raise NotFound(f"no bulb '{bulb_id}'")


def set_adaptive(config: Mapping[str, Any], room_id: str, adaptive: bool) -> dict[str, Any]:
    """Return the config with a room flagged adaptive, or not.

    Flagging it gives the room a minimal adaptive_config (`{}`, which the defaults fill
    on save, every bulb switched-only) unless it has one; the adaptive editor
    (`set_adaptive_config`) fills it in.
    Unflagging drops adaptive_config, which the schema forbids on a room that isn't
    adaptive: `adaptive_config_content` says what that would lose.
    """
    doc = copy.deepcopy(dict(config))
    room = _room(doc, room_id)
    room["adaptive"] = bool(adaptive)
    if adaptive:
        room.setdefault("adaptive_config", {})
    else:
        room.pop("adaptive_config", None)
    return doc


def adaptive_config_content(room: Mapping[str, Any]) -> list[str]:
    """What unflagging a room would throw away: one line per part of its adaptive_config
    that holds more than the defaults. Empty when it holds nothing anyone set (a room just
    flagged, whose block the defaults filled: no adaptive bulb, the matrix at its defaults)."""
    adaptive_config = room.get("adaptive_config")
    if not isinstance(adaptive_config, Mapping):
        return []
    found = []
    for key, value in sorted(adaptive_config.items()):
        if key == "schedule_profile":
            if value != config_mod.DEFAULT_PROFILE:
                found.append(f"schedule profile '{value}'")
        elif key == "learning_units":
            if value:
                adaptive = len(adaptive_config.get("adaptive_bulbs") or [])
                found.append(f"{adaptive} adaptive bulb(s) in {len(value)} learning unit(s)")
        elif key == "adaptive_bulbs":
            continue  # every adaptive bulb is in a unit: said with the units
        elif key == "scenes":
            changed = config_mod.non_default_scenes(value)
            if changed:
                found.append(f"{len(changed)} scene setting(s) changed from the defaults")
        elif isinstance(value, list):
            if value:
                found.append(f"{key.replace('_', ' ')}: {', '.join(map(str, value))}")
        elif value not in (None, {}, ""):
            found.append(key.replace("_", " "))
    return found


def set_adaptive_config(
    config: Mapping[str, Any], room_id: str, adaptive_config: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the config with a room's whole adaptive_config replaced (the adaptive
    editor's save, P5.0).

    Refused for a room that isn't adaptive: the block exists only while the flag is set
    (the Map's checkbox), and saving one here must not flag the room behind the Map's back.
    A pure edit, like the others: the block's shape, the units' partition of the adaptive
    bulbs and the scene matrix are checked when the config is saved, and the defaults fill
    and reshape what the editor left out. Whether a bulb can dim is the editor's guard, not
    checked here: the config can't see HA.
    """
    doc = copy.deepcopy(dict(config))
    room = _room(doc, room_id)
    if room.get("adaptive") is not True:
        raise ConfigError(
            f"room '{room_id}' isn't adaptive: tick Adaptive on the Map before configuring it"
        )
    room["adaptive_config"] = copy.deepcopy(dict(adaptive_config))
    return doc


# What HA's `supported_color_modes` holds for a light that can only switch: HA requires
# `onoff` to be a light's only mode when it is one at all.
ON_OFF_ONLY = frozenset({"onoff"})
OFFLINE = ("unavailable", "unknown")


def capability_unknown(note: str) -> dict[str, Any]:
    """A bulb whose dimmability can't be told: never locked (see `bulb_capability`)."""
    return {"dimmable": None, "locked": False, "color_modes": None, "note": note}


def bulb_capability(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether a bulb can dim, from its HA state, for the adaptive editor's guard (P5.0):
    `{dimmable, locked, color_modes, note}`.

    The one reliable signal is `supported_color_modes`: exactly `{onoff}` means it can only
    switch, so it can't follow a learned brightness, and it is locked switched-only. When
    the signal isn't there (no such light, the light `unavailable` or `unknown`, the
    attribute missing or unreadable) the capability is unknown and the bulb is NOT locked:
    an offline dimmer would otherwise be locked out of learning by an outage. A UI-time
    guard only; the config never depends on it, since it can't see HA.
    """
    if state is None:
        return capability_unknown("Capability unknown: Home Assistant has no such light.")
    if state.get("state") in OFFLINE:
        return capability_unknown("Capability unknown (offline).")
    modes = (state.get("attributes") or {}).get("supported_color_modes")
    if not isinstance(modes, list) or not modes or not all(isinstance(m, str) for m in modes):
        return capability_unknown(
            "Capability unknown: Home Assistant doesn't report its colour modes."
        )
    if set(modes) == ON_OFF_ONLY:
        return {
            "dimmable": False,
            "locked": True,
            "color_modes": ["onoff"],
            "note": (
                "Can't dim: Home Assistant reports it only switches on and off, so it "
                "can't follow a learned brightness. It stays switched-only."
            ),
        }
    return {"dimmable": True, "locked": False, "color_modes": sorted(set(modes)), "note": None}


def adaptive_config_changes(
    before: Mapping[str, Any], after: Mapping[str, Any], names: Mapping[str, str]
) -> list[str]:
    """Describe, one line each, how a room's adaptive_config differs between two saves
    (both normalised): sensors, the schedule profile, each bulb's tier, the learning units,
    and the scene matrix. `names` gives bulbs' display names. For the event log, so a save
    from the adaptive editor says what it changed. Deterministic order."""
    changes: list[str] = []

    def name(bulb_id: Any) -> str:
        return names.get(bulb_id, str(bulb_id))

    for key in ("lux_sensors", "presence_sensors"):
        old, new = list(before.get(key, [])), list(after.get(key, []))
        label = key.replace("_", " ")
        added = [s for s in new if s not in old]
        removed = [s for s in old if s not in new]
        if added:
            changes.append(f"{label} added: {', '.join(map(str, added))}")
        if removed:
            changes.append(f"{label} removed: {', '.join(map(str, removed))}")
    if before.get("schedule_profile") != after.get("schedule_profile"):
        changes.append(
            f"schedule profile: {before.get('schedule_profile')} -> {after.get('schedule_profile')}"
        )
    old_tier = set(before.get("adaptive_bulbs", []))
    new_tier = set(after.get("adaptive_bulbs", []))
    for bulb_id in sorted(old_tier ^ new_tier, key=str):
        changes.append(
            f"{name(bulb_id)}: switched-only -> adaptive"
            if bulb_id in new_tier
            else f"{name(bulb_id)}: adaptive -> switched-only"
        )
    old_units = {u.get("id"): u for u in before.get("learning_units", [])}
    new_units = {u.get("id"): u for u in after.get("learning_units", [])}
    for unit_id in [u for u in new_units if u not in old_units]:
        unit = new_units[unit_id]
        members = ", ".join(name(m) for m in unit.get("members", []))
        changes.append(f"learning unit added: {unit.get('name')} ({members})")
    for unit_id in [u for u in old_units if u not in new_units]:
        changes.append(f"learning unit removed: {old_units[unit_id].get('name')}")
    for unit_id in [u for u in new_units if u in old_units]:
        old, new = old_units[unit_id], new_units[unit_id]
        if old.get("name") != new.get("name"):
            changes.append(f"learning unit renamed: {old.get('name')} -> {new.get('name')}")
        if old.get("members") != new.get("members"):
            changes.append(
                f"learning unit {new.get('name')}: members "
                f"{', '.join(name(m) for m in old.get('members', []))} -> "
                f"{', '.join(name(m) for m in new.get('members', []))}"
            )
    unit_names = {k: u.get("name") for k, u in [*old_units.items(), *new_units.items()]}
    old_scenes, new_scenes = before.get("scenes") or {}, after.get("scenes") or {}
    for section in config_mod.SECTIONS:
        old_row, new_row = old_scenes.get(section) or {}, new_scenes.get(section) or {}
        for knob in config_mod.scene_defaults(section):
            if knob in old_row and knob in new_row and old_row[knob] != new_row[knob]:
                changes.append(
                    f"{section} {knob.replace('_', ' ')}: {old_row[knob]} -> {new_row[knob]}"
                )
        old_cells, new_cells = old_row.get("units") or {}, new_row.get("units") or {}
        for unit_id, cell in new_cells.items():
            # A new unit's cells are part of "learning unit added", unless set off.
            was = (old_cells.get(unit_id) or {}).get("mode", config_mod.DEFAULT_MODE)
            if cell.get("mode") != was:
                unit = unit_names.get(unit_id, unit_id)
                changes.append(f"{section} {unit}: {was} -> {cell.get('mode')}")
    return changes


def set_hard_switches(
    config: Mapping[str, Any], owner_id: str, bindings: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Return the config with the hard switches bound to a bulb or a switching group
    replaced by `bindings` (bulb and group ids share one namespace, so `owner_id` names
    either).

    A binding with no `id` gets a new one, from the owner and its name, unused by any
    other hard switch; keys whose value is None are dropped (a form's empty `turn`). A
    pure edit: whether the bindings are well formed, and free of feedback loops, is checked
    when the config is saved (validate-then-write).
    """
    doc = copy.deepcopy(dict(config))
    owner = _owner(doc, owner_id)
    cleaned = [
        {key: copy.deepcopy(value) for key, value in dict(b).items() if value is not None}
        for b in bindings
    ]
    taken = {
        h.get("id")
        for holder in _holders(doc)
        if holder is not owner
        for h in holder.get("hard_switches", [])
    }
    # Only string ids: anything else is the schema's to refuse when the edit is saved, and
    # must not fail here first (a list can't go in a set).
    taken |= {b["id"] for b in cleaned if isinstance(b.get("id"), str) and b["id"]}
    for binding in cleaned:
        if not binding.get("id"):
            name = str(binding.get("name") or "switch")
            slug = config_mod.new_id(name, (), "switch")
            mine = slug == owner_id or slug.startswith(f"{owner_id}_")
            text = slug if mine else f"{owner_id} {name}"
            binding["id"] = config_mod.new_id(text, taken, f"{owner_id}_switch")
            taken.add(binding["id"])
    owner["hard_switches"] = cleaned
    return doc


def new_group_id(config: Mapping[str, Any], name: str) -> str:
    """An id for a new switching group, from its name: unused by any bulb or group (the
    two share one namespace)."""
    taken = {b.get("id") for _, b in _all_bulbs(config)}
    taken |= {
        g.get("id") for room in config.get("rooms", []) for g in room.get("switching_groups", [])
    }
    return config_mod.new_id(name, taken, "group")


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
        raise NotFound(f"room '{room_id}' has no switching group '{group_id}'")
    room["switching_groups"] = remaining
    return doc


# -- Ignored lights --------------------------------------------------------------------


def active_bulbs(config: Mapping[str, Any]) -> list[tuple[Mapping[str, Any] | None, Any]]:
    """Every bulb that isn't ignored, with its room (None for Unassigned), in config order:
    the inventory the switching plane, deploy, the observer, and the modules work from.

    An ignored bulb is a light the user set aside (an indicator LED, say). It stays in the
    snapshot, so a sync keeps following it and it can be un-ignored, but it gets no soft
    switch or bridge, isn't observed, and no module sees it. Read bulbs through here, not
    straight from `room["bulbs"]`, so none of them can pick one up by accident.
    """
    return [(room, bulb) for room, bulb in _all_bulbs(config) if not is_ignored(bulb)]


def ignored_bulbs(config: Mapping[str, Any]) -> list[tuple[Mapping[str, Any] | None, Any]]:
    """Every ignored bulb, with its room (None for Unassigned), in config order."""
    return [(room, bulb) for room, bulb in _all_bulbs(config) if is_ignored(bulb)]


def ignore_drops(config: Mapping[str, Any], bulb_id: str) -> list[str]:
    """What ignoring a bulb would throw away, one line each: its hard switches, its
    switching-group and learning-unit memberships, and any group or unit that would be
    left with no members (dropped too, with a group's hard switches). Empty when it would
    drop nothing, and for a bulb already ignored."""
    room, bulb = find_bulb(config, bulb_id)
    if is_ignored(bulb):
        return []
    drops = []
    own = [str(h.get("id")) for h in bulb.get("hard_switches", [])]
    if own:
        drops.append(f"its hard switch(es): {', '.join(own)}")
    holders = [("switching group", g) for g in (room or {}).get("switching_groups", [])]
    adaptive_config = (room or {}).get("adaptive_config") or {}
    holders += [("learning unit", u) for u in adaptive_config.get("learning_units", [])]
    for label, holder in holders:
        if bulb_id not in holder.get("members", []):
            continue
        name = holder.get("name") or holder.get("id")
        if [m for m in holder["members"] if m != bulb_id]:
            drops.append(f"its place in {label} {name!r}")
            continue
        switches = [str(h.get("id")) for h in holder.get("hard_switches", [])]
        drops.append(
            f"{label} {name!r}, which would have no members left"
            + (f", with its hard switch(es): {', '.join(switches)}" if switches else "")
        )
    return drops


def set_ignored(config: Mapping[str, Any], bulb_id: str, ignored: bool) -> dict[str, Any]:
    """Return the config with a bulb ignored, or not.

    Ignoring takes everything that hangs off the bulb with it, since none of it means
    anything without a soft switch: its hard switches, and its switching-group and
    learning-unit memberships (a group or unit left empty goes too; `ignore_drops` says
    what that loses). Un-ignoring brings it back bare: no bindings, no memberships, so
    nothing becomes live. Either way the bulb stays where it is in the snapshot, with its
    id. A pure edit: HA is not touched, and the result is checked when it is saved.
    """
    doc = copy.deepcopy(dict(config))
    room, bulb = find_bulb(doc, bulb_id)
    bulb["ignored"] = bool(ignored)
    if ignored:
        bulb["hard_switches"] = []
        if room is not None:
            config_mod.prune_members(room)
    return doc


# -- Which bulbs are live --------------------------------------------------------------


def hard_switch_targets(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every hard-switch binding by id: `{binding, owner, bulbs}`, where `owner` is
    `{kind: bulb | group, id, name}` and `bulbs` the sorted ids of the bulbs it switches
    (the bulb itself, or every member of the group)."""
    found: dict[str, dict[str, Any]] = {}
    for _, bulb in _all_bulbs(config):
        owner = {"kind": "bulb", "id": bulb.get("id"), "name": bulb.get("name")}
        for binding in bulb.get("hard_switches", []):
            found[binding.get("id")] = {
                "binding": binding,
                "owner": owner,
                "bulbs": [bulb.get("id")],
            }
    for room in config.get("rooms", []):
        for group in room.get("switching_groups", []):
            owner = {"kind": "group", "id": group.get("id"), "name": group.get("name")}
            for binding in group.get("hard_switches", []):
                found[binding.get("id")] = {
                    "binding": binding,
                    "owner": owner,
                    "bulbs": sorted(group.get("members", [])),
                }
    return dict(sorted(found.items(), key=lambda item: str(item[0])))


def live_bulbs(config: Mapping[str, Any]) -> dict[str, list[str]]:
    """Bulb id -> the ids of the hard switches that drive it, directly or through a
    switching group, for every bulb with at least one.

    Such a bulb's bridge is authoritative once deployed: pressing the switch drives the
    bulb through Light Map. During the parallel build that happens beside the live
    ha-adaptive-light, which is why the Map marks these bulbs "live".
    """
    live: dict[str, list[str]] = {}
    for binding_id, target in hard_switch_targets(config).items():
        for bulb_id in target["bulbs"]:
            live.setdefault(bulb_id, []).append(binding_id)
    return dict(sorted(live.items()))


def going_live(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The hard switches an edit from `before` to `after` makes drive something new: a new
    binding, one whose source, mode or turn changed, or one that now switches bulbs it
    didn't (a member added to its group). Each `{id, name, owner, bulbs}` (`bulbs`: every
    bulb it switches), by id. Renaming a binding, removing one, or removing a bulb from
    one makes nothing newly live and isn't listed."""

    def acts(binding: Mapping[str, Any]) -> tuple[Any, ...]:
        return (binding.get("source"), binding.get("mode"), binding.get("turn"))

    old = hard_switch_targets(before)
    changes = []
    for binding_id, target in hard_switch_targets(after).items():
        previous = old.get(binding_id)
        if (
            previous is None
            or acts(previous["binding"]) != acts(target["binding"])
            or set(target["bulbs"]) - set(previous["bulbs"])
        ):
            changes.append(
                {
                    "id": binding_id,
                    "name": target["binding"].get("name"),
                    "owner": target["owner"],
                    "bulbs": target["bulbs"],
                }
            )
    return changes


def _all_bulbs(config: Mapping[str, Any]) -> list[tuple[Mapping[str, Any] | None, Any]]:
    found: list[tuple[Mapping[str, Any] | None, Any]] = [
        (room, bulb) for room in config.get("rooms", []) for bulb in room.get("bulbs", [])
    ]
    return found + [(None, bulb) for bulb in config.get("unassigned", [])]


def _holders(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Everything a hard switch can be bound to: every bulb and switching group."""
    holders = [bulb for _, bulb in _all_bulbs(doc)]
    for room in doc.get("rooms", []):
        holders += room.get("switching_groups", [])
    return holders


def _owner(doc: dict[str, Any], owner_id: str) -> dict[str, Any]:
    owner = next((h for h in _holders(doc) if h.get("id") == owner_id), None)
    if owner is None:
        raise NotFound(f"no bulb or switching group '{owner_id}'")
    return owner


def _room(doc: Mapping[str, Any], room_id: str) -> Any:
    room = next((r for r in doc.get("rooms", []) if r.get("id") == room_id), None)
    if room is None:
        raise NotFound(f"no room '{room_id}'")
    return room
