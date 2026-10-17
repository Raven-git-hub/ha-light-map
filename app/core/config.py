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
- Bulb ids are global, because each names a soft switch (`input_boolean.lm_soft_<id>`) and
  a bridge (`lm_bridge_<id>`). That includes the bulbs in `unassigned`. Switching-group ids
  share that one namespace: a group mints no entity of its own (since P2), but a hard
  switch binds to a bulb or a group, and keeping the two apart lets either be named by
  id alone.
- Hard-switch ids are global (`lm_hard_<id>`).
- Learning-unit ids are unique within their room (the almanac is per room).
- Schedule-profile ids are global.
- A bulb's `ha_match_key`, when it has one, is unique across all bulbs: one HA registry
  entry is one bulb.

An ignored bulb (`ignored: true`, P4.1: a light that isn't a room bulb, such as an
indicator LED) stays in the snapshot and keeps its id reserved, but is set aside from
everything else: the cross-check refuses one that is a switching-group or learning-unit
member or carries hard switches, since with no soft switch none of them would act on
anything.

A room's `adaptive_config` (P5.0) is Adaptive Light's block, but its shape is checked here,
with the rest of the document, so the config loads or doesn't as a whole. Adaptive-ness is
the block's `adaptive_bulbs`, never a flag on the bulb, so core needs nothing from the
module; the six sections are core's vocabulary (`SECTIONS`, mirrored by the schema's
`section` enum), and the module takes them from here. The cross-check makes the learning
units partition exactly the adaptive bulbs and the scene matrix cover exactly those units
over the six sections; the defaults reshape the matrix to the current units on every load
(`normalize_scenes`), so a unit removed anywhere leaves no orphan column. Whether a bulb can
dim is not checked here: the config can't see HA, and nothing that decides whether it loads
may depend on HA. The adaptive editor enforces it.

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
  id) is the opaque HA id the config holds for matching: the reconcile matches on it, and
  an Area write compares it with HA's registry entry before moving a light. It is never
  addressed, required to exist in HA, or written into generated artefacts. The one carved-out exception is a hard switch's `device` source, which names
  a device id only to read a button press from a device exposing nothing else.
- A hard switch can't be wired into a feedback loop: its source may not be one of Light
  Map's own entities, a `toggle` may not take a bulb Light Map drives as its source, and
  an event source may not listen to the HA core events the plane's own changes fire.
- A light is never ignored unless the user said so: the reconcile makes every new bulb
  `ignored: false` and carries the flag on a matched one, never guessing which lights are
  indicator LEDs (a wrong guess would hide a real bulb).
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

from app.core.ha import DEFAULT_RECONNECT_BACKOFF, token_from_env

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "config.schema.json"

CURRENT_VERSION = 4
DEFAULT_PROFILE = "default"

# The six fixed sections of the day, in canonical order (not necessarily chronological on a
# given day), as the schema's `section` enum lists them. Core's vocabulary, so the config can
# validate the scene matrix without importing the adaptive module, which takes them from here.
SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")

# A learning unit's setting for a section: `auto` learns; `off` holds the unit off for it.
MODES = ("auto", "off")
DEFAULT_MODE = "auto"

# Sections whose lux readings sit at the sensor's noise floor, where maintenance can't work:
# brightness there comes from the scene alone. (The predecessor first got this by an
# arithmetic accident, a margin of 5 around a target of 3 making every reading in-band; it
# made it explicit so it no longer depends on that arithmetic holding.)
NO_MAINTENANCE_BY_DEFAULT = ("night", "sleep")

# What a room's sensors may be. A lux sensor reports a number; a presence sensor must read
# on/off, because the observer counts `on` as occupied: a person or device_tracker reports
# `home`, which would read as unoccupied, always.
SENSOR_DOMAINS: dict[str, tuple[str, ...]] = {
    "lux_sensors": ("sensor",),
    "presence_sensors": ("binary_sensor", "input_boolean"),
}

# Light Map's own namespaces: the switching plane's and the adaptive module's.
OWN_PREFIXES = ("lm_", "al_")


def scene_defaults(section: str) -> dict[str, Any]:
    """A section's tuning knobs as a new matrix row gets them: the predecessor's defaults.
    The knobs are per section, room-wide; `mode` is the only per-unit setting."""
    return {
        "lux_target_seed": None,  # observe
        "lux_margin": 5,
        "max_step_pct": 0.04,
        "maintenance_enabled": section not in NO_MAINTENANCE_BY_DEFAULT,
        "transition_seconds": 120,
    }


# The observer's settings (`system`), each optional. Tuning defaults, not structural ones,
# so they are supplied when read (`system`) rather than written into the saved document:
# a config keeps saying only what its owner chose.
SYSTEM_DEFAULTS: dict[str, Any] = {
    "heartbeat_interval_minutes": 10,
    "reactive_window_seconds": 120,
    "reactive_min_delta": 5,
    "event_log_retention_days": 90,
}

# HA core events, which fire for the switching plane's own work rather than for a button:
# every soft switch change is a state_changed (and state_reported) that starts with a
# call_service, every automation run fires automation_triggered, a deploy fires
# automation_reloaded and registry updates, and a restart fires the start/stop events. A
# hard switch listening to one would be set off by Light Map itself, on every change, deploy,
# or restart.
CORE_EVENTS = (
    "area_registry_updated",
    "automation_reloaded",
    "automation_triggered",
    "call_service",
    "component_loaded",
    "core_config_updated",
    "device_registry_updated",
    "entity_registry_updated",
    "homeassistant_close",
    "homeassistant_final_write",
    "homeassistant_start",
    "homeassistant_started",
    "homeassistant_stop",
    "script_started",
    "service_registered",
    "service_removed",
    "state_changed",
    "state_reported",
)


def _v1_to_v2(doc: dict[str, Any]) -> dict[str, Any]:
    """P2 fixed `hard_switch.source` as a tagged union (version 1 took a string or an
    object of no fixed shape) and gave a press its direction, `turn`.

    A version-1 string source was an entity_id, so it becomes a `state` source. A
    version-1 press had no direction; the only press version 1 described was the one-way
    "all off", so it becomes `turn: off`. Anything else is left for validation to report.
    """
    holders = [bulb for room in _dicts(doc.get("rooms")) for bulb in _dicts(room.get("bulbs"))]
    holders += [
        g for room in _dicts(doc.get("rooms")) for g in _dicts(room.get("switching_groups"))
    ]
    holders += list(_dicts(doc.get("unassigned")))
    for holder in holders:
        for binding in _dicts(holder.get("hard_switches")):
            if isinstance(binding.get("source"), str):
                binding["source"] = {"type": "state", "entity_id": binding["source"]}
            if binding.get("mode") == "press" and "turn" not in binding:
                binding["turn"] = "off"
    return doc


def _v2_to_v3(doc: dict[str, Any]) -> dict[str, Any]:
    """P4.1 added `bulb.ignored`, which sets aside a light that isn't a room bulb.

    Version 2 had no way to ignore a light, so every version-2 bulb is a bulb: `ignored`
    false. The bump is what makes an older Light Map refuse a version-3 config outright
    instead of loading it without knowing which lights to leave alone.
    """
    for bulb in _all_bulb_dicts(doc):
        bulb.setdefault("ignored", False)
    return doc


def _v3_to_v4(doc: dict[str, Any]) -> dict[str, Any]:
    """P5.0 fixed `adaptive_config`: `adaptive_bulbs` says which of an adaptive room's
    bulbs are adaptive (the rest are switched-only), and `scenes` became the section x
    unit matrix.

    Version 3 had no tiers: a learning unit was the only thing that named a bulb for the
    module, so the bulbs in a room's units become its adaptive bulbs (in room order), and
    the rest switched-only. Nothing else is guessed: everything left out is filled by the
    defaults, which make an empty block's room all switched-only, so a room flagged with no
    units changes nothing in how any bulb behaves. A version-3 `scenes` was an open object
    nothing wrote but `{}`; it goes on as it is, to be normalised like any other, and what
    doesn't fit the new shape is left for validation to report rather than dropped. A room
    that isn't adaptive has no block, so a house with no adaptive room only changes version.
    """
    for room in _dicts(doc.get("rooms")):
        adaptive_config = room.get("adaptive_config")
        if not isinstance(adaptive_config, dict) or "adaptive_bulbs" in adaptive_config:
            continue
        in_units = {
            member
            for unit in _dicts(adaptive_config.get("learning_units"))
            for member in (unit.get("members") if isinstance(unit.get("members"), list) else [])
            if isinstance(member, str)
        }
        # Runs before validation, so it tolerates what it can't read: a bulb id that isn't a
        # string (a hand edit) is left for validation to report, not a crash at startup.
        adaptive_config["adaptive_bulbs"] = [
            bulb["id"]
            for bulb in _dicts(room.get("bulbs"))
            if isinstance(bulb.get("id"), str) and bulb["id"] in in_units
        ]
    return doc


# Each entry upgrades a document from version N to N + 1. The predecessor's config is
# re-expressed by hand against this shape, not migrated (docs/DESIGN.md, "Data model").
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _v1_to_v2,
    2: _v2_to_v3,
    3: _v3_to_v4,
}


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
        "reconnect_backoff": tuple(
            ha.get("reconnect_backoff_seconds") or DEFAULT_RECONNECT_BACKOFF
        ),
    }


def system(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the observer's settings: the config's `system` over `SYSTEM_DEFAULTS`.

    `data_dir` is left out; see `data_dir`.
    """
    settings = dict(SYSTEM_DEFAULTS)
    settings.update(
        {k: v for k, v in (config.get("system") or {}).items() if k in SYSTEM_DEFAULTS}
    )
    return settings


def data_dir(config: Mapping[str, Any], config_path: Path) -> Path:
    """Where the store keeps the CSV archive and the SQLite query layer: `system.data_dir`,
    resolved against the config's own directory, or that directory itself.

    Next to the config by default, so the container's one volume holds both, and running
    outside it with `LM_CONFIG_PATH=data/config.json` keeps everything under `data/`.
    """
    base = Path(config_path).parent
    configured = (config.get("system") or {}).get("data_dir")
    return base / configured if configured else base


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
        for bulb in _dicts(room["bulbs"]):
            bulb.setdefault("ignored", False)
        adaptive_config = room.get("adaptive_config")
        if isinstance(adaptive_config, dict):
            adaptive_config.setdefault("lux_sensors", [])
            adaptive_config.setdefault("presence_sensors", [])
            adaptive_config.setdefault("schedule_profile", DEFAULT_PROFILE)
            # None adaptive, never all: the defaults can't see whether a bulb can dim, and a
            # room just flagged adaptive must change nothing until its bulbs are chosen.
            adaptive_config.setdefault("adaptive_bulbs", [])
            adaptive_config.setdefault("learning_units", [])
            adaptive_config["scenes"] = normalize_scenes(
                adaptive_config.get("scenes", {}), adaptive_config["learning_units"]
            )
    for bulb in _dicts(doc["unassigned"]):
        bulb.setdefault("hard_switches", [])
        bulb.setdefault("ignored", False)
    return doc


def normalize_scenes(scenes: Any, units: Any) -> Any:
    """Return a room's scene matrix reshaped to exactly its learning units x the six
    sections: every section, knob and unit cell present (a missing one takes its default,
    `auto` for a cell), and the column of a unit the room no longer has dropped.

    Units are removed in several places that don't touch the matrix (the editor, ignoring a
    unit's last member, a sync taking its bulbs out of the room); reshaping it here, on
    every load and save, is what keeps an orphan column from ever reaching the document, and
    gives a new unit its column. Only what is absent is filled: anything present stays as
    it is, an explicit null included, so a value of the wrong type, a mode that isn't one, a
    misspelt section or knob is left for validation to report, since dropping or fixing it
    would silently undo what someone wrote. Canonical order (sections, then knobs, then
    units in the room's unit order), so it is deterministic. Runs before validation, so it
    leaves alone what it can't read.
    """
    if not isinstance(scenes, dict) or not isinstance(units, list):
        return scenes
    unit_ids = list(dict.fromkeys(u["id"] for u in _dicts(units) if isinstance(u.get("id"), str)))
    reshaped: dict[str, Any] = {}
    for section in SECTIONS:
        entry = scenes.get(section, {})
        if not isinstance(entry, dict):
            reshaped[section] = entry
            continue
        row: dict[str, Any] = {
            knob: entry[knob] if knob in entry else default
            for knob, default in scene_defaults(section).items()
        }
        cells = entry.get("units", {})
        if isinstance(cells, dict):
            row["units"] = {}
            for unit_id in unit_ids:
                cell = cells.get(unit_id, {})
                if isinstance(cell, dict):
                    cell = {"mode": DEFAULT_MODE, **cell}
                row["units"][unit_id] = cell
        else:
            row["units"] = cells
        row |= {key: value for key, value in entry.items() if key not in row}
        reshaped[section] = row
    reshaped |= {key: value for key, value in scenes.items() if key not in reshaped}
    return reshaped


def non_default_scenes(scenes: Any) -> list[str]:
    """The matrix's settings that differ from what a new row or cell gets: `<section>
    <knob>` for a knob, `<section> <unit>: off` for a unit held off. What unflagging a room
    would throw away from its scenes."""
    if not isinstance(scenes, Mapping):
        return []
    found = []
    for section in SECTIONS:
        row = scenes.get(section)
        if not isinstance(row, Mapping):
            continue
        for knob, default in scene_defaults(section).items():
            if knob in row and row[knob] != default:
                found.append(f"{section} {knob.replace('_', ' ')}")
        cells = row.get("units")
        for unit_id, cell in cells.items() if isinstance(cells, Mapping) else []:
            if isinstance(cell, Mapping) and cell.get("mode", DEFAULT_MODE) != DEFAULT_MODE:
                found.append(f"{section} {unit_id}: {cell.get('mode')}")
    return found


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
    # The schema rules whose stock messages restate the schema instead of the problem.
    if error.validator == "not" and error.validator_value == {"required": ["adaptive_config"]}:
        message = "adaptive_config is only allowed when adaptive is true"
    elif error.validator == "contains" and list(error.absolute_path) == ["schedule_profiles"]:
        message = f"a schedule profile with id '{DEFAULT_PROFILE}' is required"
    elif error.validator == "not" and error.validator_value == {"required": ["turn"]}:
        message = "turn is only allowed when mode is press"
    elif error.validator == "const" and list(error.absolute_path)[-2:] == ["source", "type"]:
        message = (
            "follow needs a state source: it tracks an entity's on/off, which an event or "
            "device trigger doesn't have"
        )
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
            if is_ignored(bulb) and bulb.get("hard_switches"):
                # A binding acts on the bulb's soft switch, which an ignored bulb doesn't
                # have: its automation would target an entity that isn't there.
                problems.append(
                    f"bulb '{bulb.get('id')}' is ignored, so it has no soft switch for a hard "
                    "switch to act on; remove its hard switches (or un-ignore it)"
                )
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
        "bulb or switching-group id", soft_ids, "bulb and switching-group ids share one namespace"
    )
    problems += _clashes("entity_id", entity_ids, "a bulb can be in only one place")
    problems += _clashes("ha_match_key", match_keys, "one HA registry entry is one bulb")
    problems += _clashes("hard-switch id", hard_ids, None)

    problems += _hard_switch_loops(rooms, unassigned)

    for room in rooms:
        where = f"room '{room.get('id')}'"
        bulb_ids = {b.get("id") for b in _dicts(room.get("bulbs", []))}
        ignored = {b.get("id") for b in _dicts(room.get("bulbs", [])) if is_ignored(b)}
        for group in _dicts(room.get("switching_groups", [])):
            problems += _dangling(
                f"{where} switching group '{group.get('id')}'", group, bulb_ids, bulb_home
            )
            problems += _ignored_members(
                f"{where} switching group '{group.get('id')}'", group, ignored
            )
        adaptive_config = room.get("adaptive_config")
        if not isinstance(adaptive_config, dict):
            continue
        profile = adaptive_config.get("schedule_profile", DEFAULT_PROFILE)
        if profile not in profile_ids:
            problems.append(f"{where}: adaptive_config.schedule_profile '{profile}' does not exist")
        problems += _sensor_problems(where, adaptive_config)
        adaptive = [b for b in adaptive_config.get("adaptive_bulbs", []) if isinstance(b, str)]
        for bulb_id in adaptive:
            if bulb_id not in bulb_ids:
                elsewhere = bulb_home.get(bulb_id)
                detail = f"it is in {elsewhere}" if elsewhere else "no bulb has that id"
                problems.append(
                    f"{where}: adaptive bulb '{bulb_id}' is not a bulb in this room ({detail})"
                )
            elif bulb_id in ignored:
                problems.append(
                    f"{where}: adaptive bulb '{bulb_id}' is ignored; an ignored bulb can't be "
                    "adaptive"
                )
        units = list(_dicts(adaptive_config.get("learning_units", [])))
        problems += _duplicates(
            f"{where}: duplicate learning-unit id", [u.get("id") for u in units]
        )
        unit_of: dict[str, str] = {}
        for unit in units:
            problems += _dangling(
                f"{where} learning unit '{unit.get('id')}'", unit, bulb_ids, bulb_home
            )
            problems += _ignored_members(f"{where} learning unit '{unit.get('id')}'", unit, ignored)
            for member in unit.get("members", []):
                # Learning units partition the adaptive bulbs: a bulb in two units would have
                # two learned targets for the same section.
                if member in unit_of and unit_of[member] != unit.get("id"):
                    problems.append(
                        f"{where}: bulb '{member}' is in learning units '{unit_of[member]}' "
                        f"and '{unit.get('id')}'; a bulb belongs to at most one"
                    )
                elif member in bulb_ids and member not in ignored and member not in adaptive:
                    # A switched-only bulb keeps the plain plane; in a unit it would be both
                    # left alone and driven to the unit's target.
                    problems.append(
                        f"{where} learning unit '{unit.get('id')}': member '{member}' is "
                        "switched-only; list it in adaptive_bulbs, or take it out of the unit"
                    )
                unit_of.setdefault(member, unit.get("id"))
        for bulb_id in adaptive:
            if bulb_id in bulb_ids and bulb_id not in ignored and bulb_id not in unit_of:
                # Its unit is what it learns into and what the scenes address it by.
                problems.append(
                    f"{where}: adaptive bulb '{bulb_id}' is in no learning unit; every "
                    "adaptive bulb is in exactly one"
                )
        problems += _scene_problems(where, adaptive_config.get("scenes"), units)
    return problems


def _sensor_problems(where: str, adaptive_config: Mapping[str, Any]) -> list[str]:
    """Sensor ids that can't be what they're listed as. Whether they exist in HA is a
    deploy-time check: the config can't see HA."""
    problems = []
    for key, domains in SENSOR_DOMAINS.items():
        label = key.removesuffix("s").replace("_", " ")
        for entity_id in adaptive_config.get(key, []):
            if not isinstance(entity_id, str):
                continue
            domain, _, object_id = entity_id.partition(".")
            if object_id.startswith(OWN_PREFIXES):
                problems.append(
                    f"{where}: {label} {entity_id} is one of Light Map's own entities, not a "
                    "sensor in the room"
                )
            elif domain not in domains:
                expected = " or ".join(f"{d}.*" for d in domains)
                why = (
                    " (it must read on/off: a person or device_tracker reports home, which "
                    "would always read as unoccupied)"
                    if key == "presence_sensors"
                    else " (an illuminance sensor)"
                )
                problems.append(f"{where}: {label} {entity_id} is not a {expected}{why}")
    return problems


def _scene_problems(where: str, scenes: Any, units: Sequence[Mapping[str, Any]]) -> list[str]:
    """The matrix must cover every learning unit in all six sections, with a mode each, and
    nothing else: a column for a unit the room doesn't have would set a mode for nothing.

    The defaults reshape a loaded matrix to fit (`normalize_scenes`), so this guards a
    document that skipped them."""
    if not isinstance(scenes, Mapping):
        return [f"{where}: adaptive_config has no scenes"] if scenes is None else []
    unit_ids = [u.get("id") for u in units if isinstance(u.get("id"), str)]
    problems = [
        f"{where} scenes: '{key}' is not a section ({', '.join(SECTIONS)})"
        for key in scenes
        if key not in SECTIONS
    ]
    for section in SECTIONS:
        row = scenes.get(section)
        if not isinstance(row, Mapping):
            problems.append(f"{where} scenes: no entry for section '{section}'")
            continue
        cells = row.get("units")
        cells = cells if isinstance(cells, Mapping) else {}
        missing = [u for u in unit_ids if u not in cells]
        if missing:
            problems.append(
                f"{where} scenes '{section}': no mode for learning unit(s) {', '.join(missing)}"
            )
        for unit_id, cell in cells.items():
            if unit_id not in unit_ids:
                problems.append(
                    f"{where} scenes '{section}': '{unit_id}' is not a learning unit of the room"
                )
                continue
            mode = cell.get("mode") if isinstance(cell, Mapping) else None
            if mode not in MODES:
                problems.append(
                    f"{where} scenes '{section}' unit '{unit_id}': mode must be auto or off, "
                    f"found {mode!r}"
                )
    return problems


def _hard_switch_loops(rooms: list[dict[str, Any]], unassigned: list[dict[str, Any]]) -> list[str]:
    """Hard switches whose own action would trigger them again."""
    bulbs = [b for room in rooms for b in _dicts(room.get("bulbs", []))] + unassigned
    # Ignored bulbs count too, though nothing drives them while they are ignored: a toggle
    # on one would loop the moment it was un-ignored (refused then, naming another bulb's
    # switch), or a sync gave its light to a new bulb; and a status LED worth ignoring may
    # mirror a load Light Map does drive. The source picker doesn't offer them anyway.
    driven = {b.get("entity_id") for b in bulbs}
    bindings = [h for b in bulbs for h in _dicts(b.get("hard_switches", []))]
    for room in rooms:
        for group in _dicts(room.get("switching_groups", [])):
            bindings += _dicts(group.get("hard_switches", []))
    problems = []
    for binding in bindings:
        source = binding.get("source")
        if not isinstance(source, dict):
            continue
        where = f"hard switch '{binding.get('id')}'"
        entity_id = source.get("entity_id") if source.get("type") == "state" else None
        if isinstance(entity_id, str) and entity_id.partition(".")[2].startswith("lm_"):
            problems.append(
                f"{where}: its source {entity_id} is one of Light Map's own entities; bind the "
                "physical switch, or put the bulbs in a switching group"
            )
        elif isinstance(entity_id, str) and entity_id in driven and binding.get("mode") == "toggle":
            # Its targets' bridges change the bulb, which toggles them again, and again.
            problems.append(
                f"{where}: a toggle can't take {entity_id}, a bulb Light Map drives, as its "
                "source: each change would set off the next"
            )
        if source.get("type") == "event" and source.get("event_type") in CORE_EVENTS:
            problems.append(
                f"{where}: event {source['event_type']} is an HA core event that fires for "
                "Light Map's own work; bind the button's own event, or use a state source"
            )
    return problems


def _ignored_members(where: str, holder: Mapping[str, Any], ignored: set[Any]) -> list[str]:
    # A member's role is its soft switch (a group's hard switches act on it) or its learned
    # target (a unit's bulbs), and an ignored bulb has neither.
    return [
        f"{where}: member '{member}' is ignored; an ignored bulb is in no switching group or "
        "learning unit"
        for member in holder.get("members", [])
        if member in ignored
    ]


def is_ignored(bulb: Mapping[str, Any]) -> bool:
    """Whether a bulb is set aside (`ignored: true`): in the snapshot, out of the plane."""
    return bulb.get("ignored") is True


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
    """Validate-then-write: strip the token, validate fully (`checked`), then atomically
    replace the file at `path`. What lands on disk is the normalised (migrated, defaulted)
    document.
    """
    _atomic_write(Path(path), _dumps(checked(config)))


def checked(config: Mapping[str, Any]) -> dict[str, Any]:
    """Run `save`'s validation without writing: return exactly what `save` would write, or
    raise ConfigError.

    "Fully" means the whole load pipeline, run on exactly what JSON will carry to disk: a
    document that passes the schema but that `load` would still refuse (say, a version
    from a newer Light Map) must not be able to replace one that loads. An edit that also
    writes to HA (moving a bulb, say) checks its config here first, so the config write
    that follows the HA write can't be the step that fails.
    """
    try:
        carried = json.loads(_dumps(redact(config)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"the config is not representable as JSON: {exc}") from exc
    return loads(carried)


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
      matched bulb keeps its id, hard switches, memberships, and `ignored` flag wherever
      it goes, and takes its light's current entity_id and name (and key, if it had none
      yet). A moved bulb follows its Area, a light with no (known) Area goes to
      `unassigned`, a bulb whose light HA no longer has is dropped, and a new light
      becomes a new bulb, under an id no bulb in the old snapshot used, and not ignored:
      ignoring is only ever the user's choice.
    - Switching-group and learning-unit members that are no longer active (not ignored)
      bulbs in the room are dropped, and a group or unit left with no members is dropped
      with them.
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
            # Compared stripped, as the Area's name was: a snapshot name with stray spaces
            # (a hand edit) would otherwise be "renamed" by every reconcile, including the
            # one that commits a Map edit (`snapshot_as_ha`), unpreviewed.
            if str(room.get("name", "")).strip() != area_names[room["ha_area_id"]]:
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
            # Never guessed: a light that looks like an indicator LED may be a real bulb,
            # and ignoring it would hide it from the plane.
            bulb["ignored"] = False
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
        prune_members(room)
    doc["rooms"] = rooms
    doc["unassigned"] = place(old_unassigned, None)
    return doc


def snapshot_as_ha(
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The Areas and lights that reproduce the committed snapshot as it stands: one Area
    per room (its `ha_area_id` and name) and one light per bulb, in its room's Area or in
    none.

    Reconciling a config against its own `snapshot_as_ha` changes nothing. Changing one
    input first (a light's Area, a new or renamed Area) commits exactly that change, the
    way a sync would (a moved bulb's memberships pruned, a new Area's room id derived),
    without picking up anything else HA has changed meanwhile. That is how an edit made
    from the Map lands in the snapshot after its HA write: a full sync there would also
    commit every unrelated change in HA, unreviewed.
    """
    areas: list[dict[str, Any]] = []
    lights: list[dict[str, Any]] = []

    def add(bulbs: Any, area_id: str | None) -> None:
        for bulb in _dicts(bulbs):
            lights.append(
                {
                    "entity_id": bulb.get("entity_id"),
                    "ha_match_key": bulb.get("ha_match_key"),
                    "name": bulb.get("name"),
                    "area_id": area_id,
                    "is_group": False,
                }
            )

    for room in _dicts(config.get("rooms", [])):
        areas.append({"area_id": room.get("ha_area_id"), "name": room.get("name")})
        add(room.get("bulbs", []), room.get("ha_area_id"))
    add(config.get("unassigned", []), None)
    return areas, lights


def new_id(text: str, taken: Iterable[Any], fallback: str) -> str:
    """A schema slug made from `text` (or `fallback`), suffixed `_2`, `_3`, ... until it is
    not in `taken`: how the Map names a new switching group or hard switch."""
    return _unique(_slugify(text, fallback), set(taken))


def prune_members(room: dict[str, Any]) -> None:
    """Drop the switching-group and learning-unit members that aren't active bulbs of the
    room (gone from it, or ignored), and every group or unit that leaves empty, in place;
    and the adaptive bulbs that aren't, so a bulb leaves its tier with its unit (every
    adaptive bulb is in a unit, so `snapshot_changes` reports it through the unit). A
    dropped unit's scene column goes with it (`normalize_scenes`, which every load and save
    runs too), so what a sync or an ignore proposes is already a config that saves.

    The reconcile runs it on every room; `core.inventory.set_ignored` on the room of a bulb
    it ignores. A bulb arriving in an adaptive room arrives switched-only.
    """
    bulb_ids = {bulb["id"] for bulb in _dicts(room.get("bulbs", [])) if not is_ignored(bulb)}

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
    if not isinstance(adaptive_config, dict):
        return
    adaptive = adaptive_config.get("adaptive_bulbs")
    if isinstance(adaptive, list):
        adaptive_config["adaptive_bulbs"] = [
            b for b in adaptive if isinstance(b, str) and b in bulb_ids
        ]
    if "scenes" in adaptive_config:
        adaptive_config["scenes"] = normalize_scenes(
            adaptive_config["scenes"], adaptive_config.get("learning_units", [])
        )


def snapshot_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Describe, one line each, how the room snapshot differs between two configs.

    This is what makes a reconcile reviewable before it is saved: rooms added, removed,
    and renamed; bulbs added, removed, moved, renamed, re-pointed at a renamed entity_id,
    ignored, and un-ignored (a line about an ignored bulb says so); match keys recorded;
    switching groups and learning units dropped or losing members. A bulb is followed by
    its id, which a reconcile keeps for a matched bulb and never hands to a new one.
    Deterministic order.
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
        changes.append(
            f"bulb removed: {bulb.get('entity_id')} (was in {where}"
            + (", ignored)" if is_ignored(bulb) else ")")
            + _drops_hard_switches(bulb)
        )
    keys_recorded = 0
    for bulb_id in sorted(bulbs_before.keys() & bulbs_after.keys(), key=by_entity(bulbs_after)):
        (old_where, old), (new_where, new) = bulbs_before[bulb_id], bulbs_after[bulb_id]
        entity_id = new.get("entity_id")
        # Said, so a reviewer can tell a change that touches nothing in the plane.
        aside = " (ignored)" if is_ignored(old) and is_ignored(new) else ""
        if is_ignored(old) != is_ignored(new):
            changes.append(
                f"bulb ignored: {entity_id}" + _drops_hard_switches(old)
                if is_ignored(new)
                else f"bulb un-ignored: {entity_id}"
            )
        if old.get("entity_id") != entity_id:
            changes.append(
                f"bulb entity_id changed: {old.get('entity_id')} -> {entity_id}{aside}"
            )
        if old_where != new_where:
            changes.append(f"bulb moved: {entity_id}: {old_where} -> {new_where}{aside}")
        if old.get("name") != new.get("name"):
            changes.append(
                f"bulb renamed: {entity_id}: {old.get('name')!r} -> {new.get('name')!r}{aside}"
            )
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
                    changes.append(
                        f"{label} removed: '{room_id}/{set_id}'"
                        + _drops_hard_switches(old_sets[set_id])
                    )
                    continue
                kept = set(new_sets[set_id].get("members", []))
                gone = [str(m) for m in old_sets[set_id].get("members", []) if m not in kept]
                if gone:
                    changes.append(
                        f"{label} '{room_id}/{set_id}': members dropped: {', '.join(gone)}"
                    )
    return changes


def _drops_hard_switches(holder: Mapping[str, Any]) -> str:
    """The hard switches a removed bulb or switching group takes with it, named: they go
    from the config, and the next deploy removes their automations."""
    ids = [str(h.get("id")) for h in _dicts(holder.get("hard_switches", []))]
    return f"; drops hard switch(es): {', '.join(ids)}" if ids else ""


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


def _all_bulb_dicts(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every bulb in a document that may not be validated yet: rooms' and Unassigned's."""
    found = [b for room in _dicts(doc.get("rooms")) for b in _dicts(room.get("bulbs"))]
    return found + list(_dicts(doc.get("unassigned")))


def _copy(config: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(config))


def _dicts(items: Any) -> Iterable[dict[str, Any]]:
    """The dict items of a list, or nothing if it isn't one: lets the defaults and checks
    walk a document that hasn't been validated yet without tripping over a bad type."""
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
