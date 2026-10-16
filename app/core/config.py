"""Config: load/validate/default the config against the JSON Schema
(`schema/config.schema.json`); cross-check references; reconcile the Area snapshot.
Validate-then-write: a config that won't load must never replace one that does.

Everything downstream is a pure function of this committed document. Learned data
(almanacs) is not stored here; it lives in the almanac store.

Loading runs migrate -> apply_defaults -> validate -> cross_check. Validation has two
passes because they catch different things: the JSON Schema checks shape (types,
patterns, required keys), and the cross-check catches what a schema can't express (ids
unique within their scope, every reference resolving). Each pass reports every problem it
finds at once, so a user fixing a config sees them together rather than one per attempt.

Id scopes, which follow the HA names the ids end up in:

- Room ids are global (`sensor.al_almanac_<room>`), and no two rooms share an HA Area.
- Bulb ids and switching-group ids share one global namespace, because both name a soft
  switch (`input_boolean.lm_soft_<id>`). That includes the bulbs in `unassigned`.
- Hard-switch ids are global (`lm_hard_<id>`).
- Learning-unit ids are unique within their room (the almanac is per room).
- Schedule-profile ids are global.
- A bulb's `ha_match_key`, when it has one, is unique across all bulbs: one HA registry
  entry is one bulb.

Hard rules:

- Config writes are validate-then-write, never the reverse.
- The HA token is never persisted in the config: it is stripped on load, save, and export,
  and supplied at runtime from LM_HA_TOKEN or LM_HA_TOKEN_FILE. LM_HA_BASE_URL, when set,
  overrides `homeassistant.base_url` (see `connection`).
- `version` is the schema version; the config is migrated forward on load.
- A schedule profile with id `default` must always exist.
- `adaptive_config` is required on a room when `adaptive` is true and forbidden when false.
- HA Areas are the source of truth for room membership; the config's room snapshot is
  reconciled against them, never overwritten destructively by surprise: the reconcile is
  a proposal, `snapshot_changes` lists what it would change, and nothing is written until
  the result is saved.
- Entity IDs only, never device IDs. A bulb's `ha_match_key` (its entity-registry entry
  id) is the one opaque HA id the config holds, and it is a match key for the reconcile
  only: never addressed, validated against HA, or written into generated artefacts.
"""

import copy
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from app.core.ha import token_from_env

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "config.schema.json"

CURRENT_VERSION = 1
DEFAULT_PROFILE = "default"

# Each entry upgrades a document from version N to N + 1. Version 1 is the first Light Map
# schema, so there are none yet. The predecessor's config is re-expressed by hand against
# this shape, not migrated (docs/DESIGN.md, "Data model").
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


class ConfigError(Exception):
    """The config failed to load, validate, or cross-check; the previous config stays.

    Carries every problem found, not just the first.
    """

    def __init__(self, problems: str | Sequence[str]) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


# -- Loading ---------------------------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    """Read the config at `path`, migrate it forward, apply defaults, validate, and
    cross-check it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    return loads(doc)


def loads(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Run an already-parsed document through the load pipeline and return the result."""
    if not isinstance(doc, Mapping):
        raise ConfigError("the config must be a JSON object")
    doc = apply_defaults(migrate(doc))
    if "access_token" in (doc.get("homeassistant") or {}):
        # Dropped rather than used, so the token in memory can only ever have come from the
        # environment, and nothing that exports this document can leak one.
        log.warning(
            "ignoring homeassistant.access_token in the config: the token comes from "
            "LM_HA_TOKEN or LM_HA_TOKEN_FILE"
        )
        doc = redact(doc)
    validate(doc)
    cross_check(doc)
    return doc


def blank() -> dict[str, Any]:
    """A fresh install: no rooms yet, and the `default` schedule profile that must exist."""
    return {
        "version": CURRENT_VERSION,
        "homeassistant": {"verify_ssl": True},
        "system": {},
        "rooms": [],
        "unassigned": [],
        "schedule_profiles": [{"id": DEFAULT_PROFILE, "name": "Default"}],
        "learning": {},
    }


def connection(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return how to reach HA at runtime, as `HAClient` keyword arguments.

    LM_HA_BASE_URL, when set, overrides `homeassistant.base_url`: the environment belongs
    to the deployment (where this container runs), the config to the house. The token
    only ever comes from the environment (`ha.token_from_env`).
    """
    ha = config.get("homeassistant") or {}
    base_url = os.environ.get("LM_HA_BASE_URL", "").strip() or str(ha.get("base_url") or "")
    if not base_url:
        raise ConfigError(
            "no Home Assistant URL: set LM_HA_BASE_URL (or homeassistant.base_url in the config)"
        )
    return {
        "base_url": base_url,
        "token": token_from_env(),
        "verify_ssl": bool(ha.get("verify_ssl", True)),
    }


def migrate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the config migrated forward to the current schema version."""
    doc = _copy(config)
    version = doc.get("version")
    # bool is an int subclass; `"version": true` must not pass as version 1.
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigError(f"version: must be an integer schema version, found {version!r}")
    if version > CURRENT_VERSION:
        raise ConfigError(
            f"version: {version} was written by a newer Light Map (this one reads up to "
            f"{CURRENT_VERSION})"
        )
    while version < CURRENT_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            raise ConfigError(f"version: no migration from version {version}")
        doc = step(doc)
        version += 1
        doc["version"] = version
    return doc


def apply_defaults(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the config with defaults filled in.

    JSON Schema documents defaults but doesn't apply them. Only structural defaults live
    here; anything that can't be defaulted safely is left for validation to report. Runs
    before validation, so it tolerates (and leaves alone) values of the wrong type.
    """
    doc = _copy(config)
    if isinstance(doc.setdefault("homeassistant", {}), dict):
        doc["homeassistant"].setdefault("verify_ssl", True)
    doc.setdefault("system", {})
    doc.setdefault("learning", {})
    doc.setdefault("rooms", [])
    doc.setdefault("unassigned", [])
    for room in _dicts(doc["rooms"]):
        room.setdefault("bulbs", [])
        room.setdefault("switching_groups", [])
        for holder in [*_dicts(room["bulbs"]), *_dicts(room["switching_groups"])]:
            holder.setdefault("hard_switches", [])
        adaptive_config = room.get("adaptive_config")
        if isinstance(adaptive_config, dict):
            adaptive_config.setdefault("lux_sensors", [])
            adaptive_config.setdefault("presence_sensors", [])
            adaptive_config.setdefault("schedule_profile", DEFAULT_PROFILE)
            adaptive_config.setdefault("learning_units", [])
    for bulb in _dicts(doc["unassigned"]):
        bulb.setdefault("hard_switches", [])
    return doc


def validate(config: Mapping[str, Any]) -> None:
    """Validate the config against the JSON Schema; raise ConfigError if it fails."""
    errors = sorted(_validator().iter_errors(config), key=lambda e: list(e.absolute_path))
    if errors:
        raise ConfigError([_describe(error) for error in errors])


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    # Read on first use rather than at import: importing a module must not do work.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _describe(error: jsonschema.ValidationError) -> str:
    where = "/".join(str(part) for part in error.absolute_path) or "(root)"
    message = error.message
    # The two schema rules whose stock messages restate the schema instead of the problem.
    if error.validator == "not" and error.validator_value == {"required": ["adaptive_config"]}:
        message = "adaptive_config is only allowed when adaptive is true"
    elif error.validator == "contains" and list(error.absolute_path) == ["schedule_profiles"]:
        message = f"a schedule profile with id '{DEFAULT_PROFILE}' is required"
    return f"{where}: {message}"


def cross_check(config: Mapping[str, Any]) -> None:
    """Check what the schema can't (ids unique, every reference resolves); raise
    ConfigError if a check fails.

    Expects a document of the right shape (validate first); missing optional keys are
    treated as empty.
    """
    problems = _cross_check_problems(config)
    if problems:
        raise ConfigError(problems)


def _cross_check_problems(doc: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    rooms = list(_dicts(doc.get("rooms", [])))
    unassigned = list(_dicts(doc.get("unassigned", [])))

    profile_ids = [p.get("id") for p in _dicts(doc.get("schedule_profiles", []))]
    if DEFAULT_PROFILE not in profile_ids:
        problems.append(f"schedule_profiles: a profile with id '{DEFAULT_PROFILE}' is required")
    problems += _duplicates("schedule_profiles: duplicate profile id", profile_ids)

    problems += _duplicates("rooms: duplicate room id", [r.get("id") for r in rooms])
    problems += _duplicates(
        "rooms: two rooms snapshot the same HA Area", [r.get("ha_area_id") for r in rooms]
    )

    # Where each bulb lives, for messages that point at the right place.
    bulb_home: dict[str, str] = {}
    soft_ids: list[tuple[str, str]] = []
    entity_ids: list[tuple[str, str]] = []
    match_keys: list[tuple[str, str]] = []
    hard_ids: list[tuple[str, str]] = []
    holders = [(f"room '{r.get('id')}'", r.get("bulbs", [])) for r in rooms]
    holders.append(("unassigned", unassigned))
    for where, bulbs in holders:
        for bulb in _dicts(bulbs):
            bulb_home.setdefault(bulb.get("id"), where)
            soft_ids.append((bulb.get("id"), f"bulb in {where}"))
            entity_ids.append((bulb.get("entity_id"), where))
            if bulb.get("ha_match_key"):
                match_keys.append((bulb["ha_match_key"], f"bulb '{bulb.get('id')}'"))
            hard_ids += [
                (h.get("id"), f"bulb '{bulb.get('id')}'")
                for h in _dicts(bulb.get("hard_switches", []))
            ]
    for room in rooms:
        for group in _dicts(room.get("switching_groups", [])):
            soft_ids.append((group.get("id"), f"switching group in room '{room.get('id')}'"))
            hard_ids += [
                (h.get("id"), f"switching group '{group.get('id')}'")
                for h in _dicts(group.get("hard_switches", []))
            ]
    problems += _clashes(
        "soft-switch id", soft_ids, "bulb and switching-group ids share one namespace"
    )
    problems += _clashes("entity_id", entity_ids, "a bulb can be in only one place")
    problems += _clashes("ha_match_key", match_keys, "one HA registry entry is one bulb")
    problems += _clashes("hard-switch id", hard_ids, None)

    for room in rooms:
        where = f"room '{room.get('id')}'"
        bulb_ids = {b.get("id") for b in _dicts(room.get("bulbs", []))}
        for group in _dicts(room.get("switching_groups", [])):
            problems += _dangling(
                f"{where} switching group '{group.get('id')}'", group, bulb_ids, bulb_home
            )
        adaptive_config = room.get("adaptive_config")
        if not isinstance(adaptive_config, dict):
            continue
        profile = adaptive_config.get("schedule_profile", DEFAULT_PROFILE)
        if profile not in profile_ids:
            problems.append(f"{where}: adaptive_config.schedule_profile '{profile}' does not exist")
        units = list(_dicts(adaptive_config.get("learning_units", [])))
        problems += _duplicates(
            f"{where}: duplicate learning-unit id", [u.get("id") for u in units]
        )
        unit_of: dict[str, str] = {}
        for unit in units:
            problems += _dangling(
                f"{where} learning unit '{unit.get('id')}'", unit, bulb_ids, bulb_home
            )
            for member in unit.get("members", []):
                # Learning units partition the room's bulbs: a bulb in two units would have
                # two learned targets for the same section.
                if member in unit_of and unit_of[member] != unit.get("id"):
                    problems.append(
                        f"{where}: bulb '{member}' is in learning units '{unit_of[member]}' "
                        f"and '{unit.get('id')}'; a bulb belongs to at most one"
                    )
                unit_of.setdefault(member, unit.get("id"))
    return problems


def _duplicates(label: str, values: Iterable[Any]) -> list[str]:
    seen: set[Any] = set()
    duplicated: list[Any] = []
    for value in values:
        if value in seen and value not in duplicated:
            duplicated.append(value)
        seen.add(value)
    return [f"{label} '{value}'" for value in duplicated]


def _clashes(label: str, entries: Sequence[tuple[Any, str]], why: str | None) -> list[str]:
    places: dict[Any, list[str]] = {}
    for value, where in entries:
        places.setdefault(value, []).append(where)
    suffix = f" ({why})" if why else ""
    return [
        f"{label} '{value}' is used more than once: {', '.join(wheres)}{suffix}"
        for value, wheres in places.items()
        if len(wheres) > 1
    ]


def _dangling(
    where: str, holder: Mapping[str, Any], bulb_ids: set[Any], bulb_home: Mapping[Any, str]
) -> list[str]:
    problems = []
    for member in holder.get("members", []):
        if member in bulb_ids:
            continue
        elsewhere = bulb_home.get(member)
        detail = f"it is in {elsewhere}" if elsewhere else "no bulb has that id"
        problems.append(f"{where}: member '{member}' is not a bulb in this room ({detail})")
    return problems


# -- Saving ----------------------------------------------------------------------------


def save(path: Path, config: Mapping[str, Any]) -> None:
    """Validate-then-write: strip the token, validate fully, then atomically replace the
    file at `path`.

    "Fully" means the whole load pipeline, run on exactly what JSON will carry to disk: a
    document that passes the schema but that `load` would still refuse (say, a version
    from a newer Light Map) must not be able to replace one that loads. What lands on disk
    is the normalised (migrated, defaulted) document.
    """
    path = Path(path)
    try:
        carried = json.loads(_dumps(redact(config)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"the config is not representable as JSON: {exc}") from exc
    _atomic_write(path, _dumps(loads(carried)))


def redact(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the config without `homeassistant.access_token`, for save and
    export."""
    doc = _copy(config)
    ha = doc.get("homeassistant")
    if isinstance(ha, dict):
        ha.pop("access_token", None)
    return doc


def _dumps(doc: Any) -> str:
    # allow_nan=False: NaN/Infinity are not JSON, and a file holding them would not load.
    return json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file in the same directory, so the rename is atomic (same filesystem)
    # and two writers can't trample one shared ".tmp" name.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            # Without the fsync a crash soon after the rename can leave a zero-length
            # config on disk, which is exactly what validate-then-write exists to prevent.
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# -- The Area snapshot -----------------------------------------------------------------


def reconcile_area_snapshot(
    config: Mapping[str, Any],
    areas: Sequence[Mapping[str, Any]],
    lights: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the config with its room snapshot reconciled against HA's Areas and lights.

    `areas` is HA's Area registry (`area_id`, `name`); `lights` is what
    `core.inventory.discover_lights` returns (`entity_id`, `ha_match_key`, `name`,
    `area_id` - the effective Area, inherited from the device where the entity has none -
    and `is_group`).

    HA is the source of truth for which rooms exist and which bulbs are in them; the
    config owns everything the user layered on top. So:

    - Every Area is a room. A room keeps its id and everything user-set on it (switching
      groups, `adaptive`, `adaptive_config`) for as long as its Area exists, matched by
      `ha_area_id`; its name follows the Area's. A new Area becomes a new, non-adaptive
      room. A room whose Area is gone from HA is dropped.
    - A bulb is matched by its `ha_match_key` (HA's entity-registry entry id, which
      survives an entity_id rename), or by `entity_id` only when the bulb has no key. A
      matched bulb keeps its id, hard switches, and memberships wherever it goes, and
      takes its light's current entity_id and name (and key, if it had none yet). A moved
      bulb follows its Area, a light with no (known) Area goes to `unassigned`, a bulb
      whose light HA no longer has is dropped, and a new light becomes a new bulb, under
      an id no bulb in the old snapshot used.
    - Switching-group and learning-unit members that are no longer bulbs in the room are
      dropped, and a group or unit left with no members is dropped with them.
    - HA light groups are not bulbs and are skipped: driving a group alongside its member
      bulbs would command the same lights twice.

    Order is stable: rooms and bulbs that stay keep their order; new rooms are appended by
    name, new or arriving bulbs by entity_id. The result is a pure function of the inputs
    (not of the order HA listed them in) and reconciling it again changes nothing. The
    token is never carried into the result. Use `snapshot_changes` to show what changed
    before saving.
    """
    doc = redact(config)
    area_names: dict[str, str] = {}
    for area in sorted(areas, key=lambda a: str(a.get("area_id"))):
        area_id = area.get("area_id")
        if isinstance(area_id, str) and area_id:
            name = area.get("name")
            area_names[area_id] = (
                name.strip() if isinstance(name, str) and name.strip() else area_id
            )
    live = sorted(
        (light for light in lights if not light.get("is_group")), key=lambda x: x["entity_id"]
    )
    old_rooms = list(_dicts(doc.get("rooms", [])))
    old_unassigned = list(_dicts(doc.get("unassigned", [])))

    rooms = []
    for room in old_rooms:
        if room.get("ha_area_id") in area_names:
            room["name"] = area_names[room["ha_area_id"]]
            rooms.append(room)
    linked = {room["ha_area_id"] for room in rooms}
    room_ids = {room.get("id") for room in rooms}
    for area_id in sorted(set(area_names) - linked, key=lambda a: (area_names[a].casefold(), a)):
        room_id = _unique(_slugify(area_id, "room"), room_ids)
        room_ids.add(room_id)
        rooms.append(
            {
                "id": room_id,
                "name": area_names[area_id],
                "ha_area_id": area_id,
                "adaptive": False,
                "bulbs": [],
                "switching_groups": [],
            }
        )
    linked_areas = {room["ha_area_id"] for room in rooms}

    # A bulb's identity is its HA entity-registry entry, not its entity_id. Renaming an
    # entity in HA keeps the registry entry (so its ha_match_key) and changes the
    # entity_id, and matching on entity_id read that as one bulb removed and another added:
    # a new id, its hard switches gone, its switching-group and learning-unit memberships
    # pruned. entity_id is the fallback only for a bulb with no key: a light outside the
    # registry, or a snapshot from before keys were recorded. A keyed bulb whose key HA no
    # longer has is gone, even if another light now holds its old entity_id.
    old_bulbs = [b for room in old_rooms for b in _dicts(room.get("bulbs", []))]
    old_bulbs += old_unassigned
    by_key: dict[str, dict[str, Any]] = {}
    by_entity: dict[str, dict[str, Any]] = {}
    for bulb in old_bulbs:
        if bulb.get("ha_match_key"):
            by_key.setdefault(bulb["ha_match_key"], bulb)
        else:
            by_entity.setdefault(bulb.get("entity_id"), bulb)
    matched: dict[str, dict[str, Any]] = {}
    for light in live:
        if light.get("ha_match_key") in by_key:
            matched[light["entity_id"]] = by_key.pop(light["ha_match_key"])
    for light in live:
        if light["entity_id"] not in matched and light["entity_id"] in by_entity:
            matched[light["entity_id"]] = by_entity.pop(light["entity_id"])

    # Every id in the old snapshot stays reserved, matched or not. A new bulb given a
    # just-dropped bulb's id would inherit that bulb's switching-group and learning-unit
    # memberships (they name bulbs by id) though not its hard switches: half an identity.
    taken = {b.get("id") for b in old_bulbs}
    taken |= {g.get("id") for room in rooms for g in _dicts(room.get("switching_groups", []))}

    bulbs: dict[str, dict[str, Any]] = {}
    location: dict[str, str | None] = {}
    for light in live:
        entity_id = light["entity_id"]
        name = light.get("name") or entity_id.partition(".")[2]
        key = light.get("ha_match_key")
        bulb = matched.get(entity_id)
        if bulb is None:
            bulb_id = _unique(_slugify(entity_id.partition(".")[2], "bulb"), taken)
            taken.add(bulb_id)
            bulb = {"id": bulb_id, "name": name, "entity_id": entity_id}
            bulb |= {"ha_match_key": key} if key else {}
            bulb["hard_switches"] = []
        else:
            bulb["name"] = name
            bulb["entity_id"] = entity_id
            if key:
                bulb["ha_match_key"] = key
        bulbs[entity_id] = bulb
        area_id = light.get("area_id")
        location[entity_id] = area_id if area_id in linked_areas else None

    def place(previous: Iterable[Mapping[str, Any]], here: str | None) -> list[dict[str, Any]]:
        # Bulbs already here keep their order; arrivals follow, sorted by entity_id.
        placed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bulb in previous:
            entity_id = bulb.get("entity_id")
            # `is`: only a matched bulb is already here. A dropped one can share its old
            # entity_id with the new bulb that has taken it over.
            if bulbs.get(entity_id) is bulb and location[entity_id] == here:
                if entity_id not in seen:
                    placed.append(bulb)
                    seen.add(entity_id)
        for light in live:
            entity_id = light["entity_id"]
            if location[entity_id] == here and entity_id not in seen:
                placed.append(bulbs[entity_id])
                seen.add(entity_id)
        return placed

    for room in rooms:
        room["bulbs"] = place(_dicts(room.get("bulbs", [])), room["ha_area_id"])
        _prune_members(room)
    doc["rooms"] = rooms
    doc["unassigned"] = place(old_unassigned, None)
    return doc


def _prune_members(room: dict[str, Any]) -> None:
    bulb_ids = {bulb["id"] for bulb in room["bulbs"]}

    def keep(holders: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = []
        for holder in holders:
            members = [m for m in holder.get("members", []) if m in bulb_ids]
            if members:
                holder["members"] = members
                kept.append(holder)
        return kept

    room["switching_groups"] = keep(_dicts(room.get("switching_groups", [])))
    adaptive_config = room.get("adaptive_config")
    if isinstance(adaptive_config, dict) and "learning_units" in adaptive_config:
        adaptive_config["learning_units"] = keep(_dicts(adaptive_config["learning_units"]))


def snapshot_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Describe, one line each, how the room snapshot differs between two configs.

    This is what makes a reconcile reviewable before it is saved: rooms added, removed,
    and renamed; bulbs added, removed, moved, renamed, and re-pointed at a renamed
    entity_id; match keys recorded; switching groups and learning units dropped or losing
    members. A bulb is followed by its id, which a reconcile keeps for a matched bulb and
    never hands to a new one. Deterministic order.
    """
    changes: list[str] = []
    rooms_before = {r.get("id"): r for r in _dicts(before.get("rooms", []))}
    rooms_after = {r.get("id"): r for r in _dicts(after.get("rooms", []))}

    for room_id in sorted(rooms_after.keys() - rooms_before.keys(), key=str):
        room = rooms_after[room_id]
        changes.append(
            f"room added: '{room_id}' ({room.get('name')}, Area {room.get('ha_area_id')})"
        )
    for room_id in sorted(rooms_before.keys() - rooms_after.keys(), key=str):
        room = rooms_before[room_id]
        lost = []
        if room.get("switching_groups"):
            lost.append(f"{len(room['switching_groups'])} switching group(s)")
        if room.get("adaptive_config") is not None:
            lost.append("its adaptive config")
        detail = f"; drops {' and '.join(lost)}" if lost else ""
        changes.append(
            f"room removed: '{room_id}' ({room.get('name')}, Area {room.get('ha_area_id')}){detail}"
        )
    for room_id in sorted(rooms_before.keys() & rooms_after.keys(), key=str):
        old, new = rooms_before[room_id], rooms_after[room_id]
        if old.get("name") != new.get("name"):
            changes.append(f"room renamed: '{room_id}': {old.get('name')!r} -> {new.get('name')!r}")

    def where_bulbs(doc: Mapping[str, Any]) -> dict[Any, tuple[str, Mapping[str, Any]]]:
        found: dict[Any, tuple[str, Mapping[str, Any]]] = {}
        for room in _dicts(doc.get("rooms", [])):
            for bulb in _dicts(room.get("bulbs", [])):
                found[bulb.get("id")] = (f"room '{room.get('id')}'", bulb)
        for bulb in _dicts(doc.get("unassigned", [])):
            found[bulb.get("id")] = ("unassigned", bulb)
        return found

    bulbs_before, bulbs_after = where_bulbs(before), where_bulbs(after)

    def by_entity(found: Mapping[Any, tuple[str, Mapping[str, Any]]]) -> Callable[[Any], str]:
        return lambda bulb_id: str(found[bulb_id][1].get("entity_id"))

    for bulb_id in sorted(bulbs_after.keys() - bulbs_before.keys(), key=by_entity(bulbs_after)):
        where, bulb = bulbs_after[bulb_id]
        changes.append(f"bulb added: {bulb.get('entity_id')} -> {where}")
    for bulb_id in sorted(bulbs_before.keys() - bulbs_after.keys(), key=by_entity(bulbs_before)):
        where, bulb = bulbs_before[bulb_id]
        changes.append(f"bulb removed: {bulb.get('entity_id')} (was in {where})")
    keys_recorded = 0
    for bulb_id in sorted(bulbs_before.keys() & bulbs_after.keys(), key=by_entity(bulbs_after)):
        (old_where, old), (new_where, new) = bulbs_before[bulb_id], bulbs_after[bulb_id]
        entity_id = new.get("entity_id")
        if old.get("entity_id") != entity_id:
            changes.append(f"bulb entity_id changed: {old.get('entity_id')} -> {entity_id}")
        if old_where != new_where:
            changes.append(f"bulb moved: {entity_id}: {old_where} -> {new_where}")
        if old.get("name") != new.get("name"):
            changes.append(f"bulb renamed: {entity_id}: {old.get('name')!r} -> {new.get('name')!r}")
        if old.get("ha_match_key") != new.get("ha_match_key"):
            if old.get("ha_match_key"):
                changes.append(f"bulb match key changed: {entity_id}")
            else:
                keys_recorded += 1
    if keys_recorded:
        changes.append(f"match key recorded for {keys_recorded} bulb(s)")

    for room_id in sorted(rooms_before.keys() & rooms_after.keys(), key=str):
        old, new = rooms_before[room_id], rooms_after[room_id]
        for label, old_sets, new_sets in (
            ("switching group", _groups(old), _groups(new)),
            ("learning unit", _units(old), _units(new)),
        ):
            for set_id in sorted(old_sets, key=str):
                if set_id not in new_sets:
                    changes.append(f"{label} removed: '{room_id}/{set_id}'")
                    continue
                kept = set(new_sets[set_id].get("members", []))
                gone = [str(m) for m in old_sets[set_id].get("members", []) if m not in kept]
                if gone:
                    changes.append(
                        f"{label} '{room_id}/{set_id}': members dropped: {', '.join(gone)}"
                    )
    return changes


# -- Helpers ---------------------------------------------------------------------------


def _groups(room: Mapping[str, Any]) -> dict[Any, Mapping[str, Any]]:
    return {g.get("id"): g for g in _dicts(room.get("switching_groups", []))}


def _units(room: Mapping[str, Any]) -> dict[Any, Mapping[str, Any]]:
    adaptive_config = room.get("adaptive_config") or {}
    return {u.get("id"): u for u in _dicts(adaptive_config.get("learning_units", []))}


def _slugify(text: str, fallback: str) -> str:
    """Lowercase `text` into a schema slug (`^[a-z0-9]+(_[a-z0-9]+)*$`), or `fallback`."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or fallback


def _unique(base: str, taken: set[Any]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _copy(config: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(config))


def _dicts(items: Any) -> Iterable[dict[str, Any]]:
    """The dict items of a list, or nothing if it isn't one: lets the defaults and checks
    walk a document that hasn't been validated yet without tripping over a bad type."""
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
