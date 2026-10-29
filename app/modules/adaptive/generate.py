"""Adaptive generator: scene + maintenance + watchdog automations. Commands SOFT SWITCHES
for power; reads learning-unit targets for brightness. `conditions: []` on scene
automations.

Emits only the `al_` namespace, per adaptive room (one with an `adaptive_config`, whether or
not any of its bulbs is adaptive yet):

- helpers: `input_boolean.al_active_<room>` (the guard, raised while the lane's own
  automations change the room's lights; off at every HA start, and lowered by the last of
  the room's runs to finish), `input_boolean.al_hold_<room>` (the maintenance
  hold: the user intervened, stay out for the rest of the section), and
  `input_select.al_scene_<room>` (the section the room is in; its options are the six
  canonical section ids, so its state is the section id). Each named from the room id,
  because HA derives a network-created helper's id from its slugified name.
- `al_scene_<room>_<section>`: the crossover into a section, fired by the runtime (P5.2)
  with `automation.trigger`.
- `al_maint_<room>`: lux maintenance on HA's own clock, for a room with adaptive bulbs and
  a lux sensor.
- `al_watchdog_guard_<room>`, `al_watchdog_stale_<room>`: a guard stuck on (or left on by
  a run a reload cut short), a scene select not moving.
- the bridge fragment (`bridge_brightness`): the brightness an adaptive bulb's core bridge
  applies on soft-on, registered with `core.switching.register_bridge_brightness`.

`sensor.al_almanac_<room>` is only read here; P5.2 publishes it, in the attribute shape
`almanac` documents (the P5.1/P5.2 seam). Never touches the switching plane's `lm_*`, but
commands its soft switches. The composition root registers all of this only while its
cutover switch is on (off until P6: these are the live ha-adaptive-light's own names).

Ported from ha-adaptive-light's generator (its guard, hold, scene select, almanac read,
settle delay, maintenance step and watchdogs), reworked to the two planes: the predecessor
called `light.turn_on`/`light.turn_off` on group entities, which decided power and
brightness in one command.

Hard rules:

- Scene automations carry `triggers: []` and `conditions: []`. They are fired via
  `automation.trigger` (default `skip_condition: true`); a condition placed there would be
  silently ignored.
- Power goes only through soft switches. At a crossover each adaptive bulb's power intent is
  set on its soft switch: on for a unit with a target this section, held off for a unit the
  almanac says is "off" for it (never a baked-in `light.turn_off`; nothing here calls
  `light.turn_off` at all). The soft->bulb bridge makes the bulb follow.
- Every brightness command is guarded on "soft is on" and on the bulb being on, so it can
  never power a bulb on. A crossover that changes section while a light stays on pushes
  the new brightness to the bulb directly, because the soft switch did not change and the
  bridge won't re-fire.
- The brightness a bulb comes on at is supplied to the core bridge as a gated fragment: a
  template that reads the unit's target from the almanac at runtime, applied only when
  there is one; otherwise the bridge's plain `light.turn_on` brings the bulb back at its
  own last brightness. The bridge, its target, and its power stay core's.
- Maintenance nudges only already-on bulbs, brightness only, never power. It iterates bulbs
  rather than units or groups.
- Nothing from the almanac is hardcoded: not the targets, and not `mode` either (the P5.2
  publish writes a unit held off as "off"). Automations read `sensor.al_almanac_<room>` at
  runtime and branch, and an absent or partial almanac leaves bulbs as they are.
- Bulbs come through `core.inventory.active_bulbs`: an ignored bulb is in none of it.
- No template engine: plain Python data serialised by `core.generate`, HA Jinja kept as
  inert strings, byte-identical output for the same config (rooms, units and bulbs sorted
  by id). Entity IDs only.
"""

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.core import config as config_mod
from app.core import inventory, switching
from app.core.config import SECTIONS
from app.modules.adaptive import almanac

PREFIX = "al"

GUARD_ICON = "mdi:lightbulb-auto"
HOLD_ICON = "mdi:hand-back-right"
SCENE_ICON = "mdi:theme-light-dark"

# A crossover keeps its guard up for its transition and this long after, so the bulbs'
# last transition steps are never taken for a person at a dimmer.
SETTLE_SECONDS = 30
# Maintenance's nudge fades over this long, and holds its guard a little longer.
MAINT_TRANSITION_SECONDS = 30
MAINT_SETTLE_SECONDS = 35
# The largest single maintenance nudge, in HA's 0-255 brightness, whatever max_step_pct is.
MAX_STEP = 10

# The guard is short-lived by design: on for longer than this, a run died mid-way, and a
# stuck guard silently stops maintenance and observation. A room whose crossover transition
# is longer gets its longest legitimate hold plus the margin instead, or the watchdog would
# release the guard in the middle of a slow fade.
GUARD_WATCHDOG_MINUTES = 10
GUARD_WATCHDOG_MARGIN_MINUTES = 5
# The longest legitimate gap between sections is sleep -> sunrise (about 7.5 hours).
STALE_SCENE_HOURS = 10

_GENERATED = "Generated by Light Map; edits made in Home Assistant are overwritten on deploy."

# What may be embedded in a Jinja string literal: ids and entity ids as the schema allows
# them. A config is validated before anything is generated from it; this keeps a quote in an
# unvalidated one from ever reaching a template as code.
_LITERAL = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)?$")


# -- Names -----------------------------------------------------------------------------


def guard_id(room_id: str) -> str:
    return f"{PREFIX}_active_{room_id}"


def hold_id(room_id: str) -> str:
    return f"{PREFIX}_hold_{room_id}"


def scene_select_id(room_id: str) -> str:
    return f"{PREFIX}_scene_{room_id}"


def guard_entity(room_id: str) -> str:
    return f"input_boolean.{guard_id(room_id)}"


def hold_entity(room_id: str) -> str:
    return f"input_boolean.{hold_id(room_id)}"


def scene_select_entity(room_id: str) -> str:
    return f"input_select.{scene_select_id(room_id)}"


def scene_id(room_id: str, section: str) -> str:
    return f"{PREFIX}_scene_{room_id}_{section}"


def maint_id(room_id: str) -> str:
    return f"{PREFIX}_maint_{room_id}"


def watchdog_guard_id(room_id: str) -> str:
    return f"{PREFIX}_watchdog_guard_{room_id}"


def watchdog_stale_id(room_id: str) -> str:
    return f"{PREFIX}_watchdog_stale_{room_id}"


# HA names a helper created over the network by slugifying its name, so each is named from
# the room id (already a slug), never its display name: "Living Room 2" with id
# "living_room" would otherwise land as al_active_living_room_2. (The predecessor's names,
# exactly: see docs/DESIGN.md, "The cutover switch".)


def guard_name(room_id: str) -> str:
    return f"AL Active {room_id}"


def hold_name(room_id: str) -> str:
    return f"AL Hold {room_id}"


def scene_select_name(room_id: str) -> str:
    return f"AL Scene {room_id}"


# -- The adaptive rooms ----------------------------------------------------------------


def adaptive_rooms(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every adaptive room (flagged, with an `adaptive_config`), sorted by id."""
    return sorted(
        (
            room
            for room in config.get("rooms", [])
            if room.get("adaptive") is True and isinstance(room.get("adaptive_config"), Mapping)
        ),
        key=lambda room: room["id"],
    )


def learning_units(config: Mapping[str, Any], room: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A room's learning units, sorted by id, each `{id, name, bulbs}` with its member bulbs
    (sorted by id) read through `core.inventory.active_bulbs`.

    Raises ValueError if the units don't partition exactly the room's adaptive bulbs, or
    name a bulb that isn't an active bulb of the room: the config's cross-check refuses
    both, so this is a config that was never validated, and generating from it would
    address the wrong lights or none.
    """
    active = {
        bulb["id"]: bulb
        for home, bulb in inventory.active_bulbs(config)
        if home is not None and home["id"] == room["id"]
    }
    block = room["adaptive_config"]
    units = []
    members: list[str] = []
    for unit in sorted(block.get("learning_units", []), key=lambda u: u["id"]):
        bulbs = []
        for member in sorted(unit["members"]):
            if member not in active:
                raise ValueError(
                    f"room '{room['id']}': learning unit '{unit['id']}' names '{member}', "
                    "which is not an active bulb of the room"
                )
            bulbs.append(active[member])
        units.append({"id": unit["id"], "name": unit["name"], "bulbs": bulbs})
        members += [bulb["id"] for bulb in bulbs]
    if sorted(members) != sorted(set(block.get("adaptive_bulbs", []))):
        raise ValueError(
            f"room '{room['id']}': the learning units don't partition its adaptive bulbs"
        )
    return units


# -- Jinja (built as plain strings, never rendered here) -------------------------------


def _literal(text: str) -> str:
    """`text` as a Jinja string literal."""
    if not _LITERAL.match(text):
        raise ValueError(f"not an id or entity id: {text!r}")
    return f"'{text}'"


def _is_target(name: str) -> str:
    """Jinja: does variable `name` hold an on-target (a brightness 1..255, not a bool)?"""
    return (
        f"{name} is number and {name} is not boolean and {name} >= {almanac.MIN_TARGET} "
        f"and {name} <= {almanac.MAX_TARGET}"
    )


def _row(room_id: str, section: str) -> str:
    """Jinja: the almanac's row for a section (none when the sensor or the row is absent).
    `section` is a Jinja expression."""
    return f"state_attr({_literal(almanac.entity_id(room_id))}, {section})"


def _current_section(room_id: str) -> str:
    """Jinja: the section the room is in, from its scene select (its state is the section
    id; `unknown` while the select is missing reads no row, and so as nothing learned)."""
    return f"states({_literal(scene_select_entity(room_id))})"


def _pick_target(units: str, unit_id: str) -> str:
    """Jinja: set `t` to the unit's target in mapping `units` (none if anything is
    missing)."""
    return f"{{% set t = {units}.get({_literal(unit_id)}) if {units} is mapping else none %}}"


def _units_of(row: str) -> str:
    """Jinja: set `units` to the row's unit targets (none if the row or its `units` is
    missing)."""
    return (
        f"{{% set row = {row} %}}"
        f"{{% set units = row.get({_literal(almanac.UNITS)}) if row is mapping else none %}}"
    )


def _light_brightness(entity_id: str) -> str:
    return f"state_attr({_literal(entity_id)}, 'brightness')"


def _plain(text: str) -> str:
    """Free text (a room's name, from its HA Area) made safe for a service call's data,
    which HA renders as a template: without braces, a name can't open a Jinja tag."""
    return text.replace("{", "(").replace("}", ")")


def guard_holders(config: Mapping[str, Any], room: Mapping[str, Any]) -> list[str]:
    """The ids of the room's automations that raise its guard: its scenes, and its
    maintenance when it has one."""
    holders = [scene_id(room["id"], section) for section in SECTIONS]
    if maintenance_automations(config, room):
        holders.append(maint_id(room["id"]))
    return holders


def _idle(holders: Sequence[str]) -> str:
    """Jinja: is none of these automations running? HA counts an automation's runs in
    progress in its `current` attribute; its entity is `automation.<id>` (alias = id). An
    entity under another id reads as idle, which only takes the check back to the
    predecessor's unconditional release."""
    runs = ", ".join(
        f"state_attr({_literal(f'automation.{holder}')}, 'current') | int(0)" for holder in holders
    )
    return f"[{runs}] | sum == 0"


def _release_guard(room_id: str, others: Sequence[str]) -> dict[str, Any]:
    """Lower the room's guard, unless another of its runs still holds it. The guard is one
    boolean shared by every run in the room: a crossover fired (with automation.trigger,
    which skips conditions) during a maintenance run, or a second crossover during a first,
    would otherwise have the first run to finish lower it while the other's fade still had
    minutes to go, and the rest of that fade, which carries no context HA links to the
    run, would be recorded as a person at a dimmer. The last run out lowers it."""
    return {
        "if": [{"condition": "template", "value_template": f"{{{{ {_idle(others)} }}}}"}],
        "then": [
            {"action": "input_boolean.turn_off", "target": {"entity_id": guard_entity(room_id)}}
        ],
    }


# -- The bridge fragment ---------------------------------------------------------------


def bridge_brightness(
    config: Mapping[str, Any], room: Mapping[str, Any] | None, bulb: Mapping[str, Any]
) -> switching.GatedBrightness | None:
    """The bridge-brightness provider (`core.switching.register_bridge_brightness`): for an
    adaptive bulb, the `light.turn_on` data its core bridge applies on soft-on, gated on
    the bulb's unit having an on-target for the section; None for every other bulb
    (switched-only, in a room that isn't adaptive, in Unassigned), which keeps the plain
    path, byte for byte.

    Both templates read the section the room is in (its scene select) and the bulb's unit's
    target for it (the almanac), so a hard switch brings the bulb up right for the time of
    day. With a target (1..255) the bridge applies it. With none (no almanac, no section
    yet, nothing learned, or "off": a person turning on a unit held off is a power decision,
    and the soft switch owns power) the gate is false and the bridge takes its plain
    `light.turn_on`, so the bulb comes back at its own last brightness. (Until P5.2a this
    had to render a brightness anyway, and an off bulb reports none to keep, so that case
    came on at 255: full brightness at 3 a.m. in a sleep section held off.) The data is
    rendered beside the gate even when the gate is false, so it never fails to render: it
    falls back to 0, which is never applied. No transition: the same bridge serves a hard
    switch, where a fade over the section's crossover transition would read as broken.
    """
    if room is None or room.get("adaptive") is not True:
        return None
    if bulb["id"] not in room.get("adaptive_config", {}).get("adaptive_bulbs", []):
        return None
    unit = next(
        u for u in learning_units(config, room) if bulb["id"] in [b["id"] for b in u["bulbs"]]
    )
    room_id = room["id"]
    target = _units_of(_row(room_id, _current_section(room_id))) + _pick_target("units", unit["id"])
    return switching.GatedBrightness(
        when=target + f"{{{{ {_is_target('t')} }}}}",
        # int(0): the gate's false whenever this falls back, so it is never applied, but it
        # is rendered regardless, and a template error would fail the whole bridge run.
        data={"brightness": target + "{{ t | int(0) }}"},
    )


# -- Helpers ---------------------------------------------------------------------------


def helpers(config: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """The guard, hold and scene select of every adaptive room, as an HA package's helper
    sections (object id -> fields). Both domains are always named, empty or not: the lane
    owns them, so the last adaptive room's helpers are removed when it goes."""
    booleans: dict[str, dict[str, Any]] = {}
    selects: dict[str, dict[str, Any]] = {}
    for room in adaptive_rooms(config):
        room_id = room["id"]
        # The guard starts off whenever HA starts: no run survives a restart, and a guard
        # restored on would never change, so its watchdog (a state trigger, attached after
        # the restore) would never fire, and maintenance and observation would stay off
        # until the next crossover. The hold is restored: a person's intervention stands
        # for the rest of the section, restart or not.
        booleans[guard_id(room_id)] = {
            "name": guard_name(room_id),
            "initial": False,
            "icon": GUARD_ICON,
        }
        booleans[hold_id(room_id)] = {"name": hold_name(room_id), "icon": HOLD_ICON}
        selects[scene_select_id(room_id)] = {
            "name": scene_select_name(room_id),
            "icon": SCENE_ICON,
            # The canonical ids, so the select's state is the section id every template
            # reads. Display names are deferred (docs/DESIGN.md, "Section display names").
            "options": list(SECTIONS),
        }
    return {"input_boolean": booleans, "input_select": selects}


# -- Scene automations -----------------------------------------------------------------


def scene_automations(config: Mapping[str, Any], room: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a room's `al_scene_` automations, one per section crossover, in section
    order."""
    units = learning_units(config, room)
    holders = guard_holders(config, room)
    return [_scene(room, units, section, holders) for section in SECTIONS]


def _scene(
    room: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    section: str,
    holders: Sequence[str],
) -> dict[str, Any]:
    room_id = room["id"]
    own = scene_id(room_id, section)
    transition = int(room["adaptive_config"]["scenes"][section]["transition_seconds"])
    actions: list[dict[str, Any]] = [
        {"action": "input_boolean.turn_on", "target": {"entity_id": guard_entity(room_id)}},
        # A crossover always clears the maintenance hold: the user's intervention stood for
        # the section, and the section is over.
        {"action": "input_boolean.turn_off", "target": {"entity_id": hold_entity(room_id)}},
        # Before any soft switch changes: a bridge brought on below reads its brightness
        # for the section from here.
        {
            "action": "input_select.select_option",
            "target": {"entity_id": scene_select_entity(room_id)},
            "data": {"option": section},
        },
    ]
    if units:
        row = _row(room_id, _literal(section))
        actions.append(
            {
                "variables": {
                    "units": (
                        f"{{% set row = {row} %}}"
                        f"{{{{ row.get({_literal(almanac.UNITS)}) if row is mapping else none }}}}"
                    )
                }
            }
        )
        actions += [_unit_action(unit, transition) for unit in units]
        actions.append({"delay": {"seconds": transition + SETTLE_SECONDS}})
    # A room with no unit changes no light, so it has nothing to wait out.
    actions.append(_release_guard(room_id, [h for h in holders if h != own]))
    return {
        "id": own,
        "alias": own,
        "description": (
            f"Light Map adaptive crossover: {room['name']} enters {section}. Each learning "
            "unit's bulbs follow the almanac's target for the section, through their soft "
            "switches. Fired by Light Map with automation.trigger, so it has no triggers "
            f"and no conditions (automation.trigger skips them). {_GENERATED}"
        ),
        "mode": "single",
        "max_exceeded": "silent",
        "triggers": [],
        "conditions": [],
        "actions": actions,
    }


def _unit_action(unit: Mapping[str, Any], transition: int) -> dict[str, Any]:
    """Branch one unit on its almanac target (in the scene's `units` variable): a target
    turns its members' soft switches on, "off" turns them off, anything else (nothing
    learned) leaves them, and the bulbs, exactly as they are. The learner never decides a
    light should be off; only an explicit "off" does."""
    pick = _pick_target("units", unit["id"])
    softs = [switching.soft_entity(bulb["id"]) for bulb in unit["bulbs"]]
    # Members already on keep their soft switch as it is, so their bridge won't re-fire:
    # push the section's brightness to them directly, guarded on both the soft and the
    # bulb being on, since a brightness command on an off bulb would power it on. Pushed
    # before the soft switches are turned on, so a member this crossover powers on comes up
    # through its bridge only, at the fragment's (the same) brightness.
    #
    # Each push is continue_on_error: a single bulb that doesn't answer (a flaky Zigbee
    # device returning "device did not respond") must not abort the whole crossover before
    # it reaches the guard release at the end. It did once, and the guard sat raised until
    # the watchdog cleared it ten minutes later, suppressing maintenance and heartbeats in
    # between. A missed push is corrected at the next crossover (or by maintenance).
    pushes = [
        {
            "if": [
                {
                    "condition": "state",
                    "entity_id": switching.soft_entity(bulb["id"]),
                    "state": "on",
                },
                {"condition": "state", "entity_id": bulb["entity_id"], "state": "on"},
            ],
            "then": [
                {
                    "action": "light.turn_on",
                    "continue_on_error": True,
                    "target": {"entity_id": bulb["entity_id"]},
                    "data": {
                        "brightness": f"{{{{ units[{_literal(unit['id'])}] | int }}}}",
                        "transition": transition,
                    },
                }
            ],
        }
        for bulb in unit["bulbs"]
    ]
    return {
        "alias": f"{unit['name']} ({unit['id']})",
        "choose": [
            {
                "conditions": [
                    {
                        "condition": "template",
                        "value_template": f"{pick}{{{{ {_is_target('t')} }}}}",
                    }
                ],
                "sequence": [
                    *pushes,
                    {"action": "input_boolean.turn_on", "target": {"entity_id": softs}},
                ],
            },
            {
                "conditions": [
                    {
                        "condition": "template",
                        "value_template": f"{pick}{{{{ t == {_literal(almanac.HELD_OFF)} }}}}",
                    }
                ],
                # Held off for the section: the soft switch is held off, and the bridge
                # turns the bulb off. Never a light.turn_off from here.
                "sequence": [{"action": "input_boolean.turn_off", "target": {"entity_id": softs}}],
            },
        ],
    }


# -- Lux maintenance -------------------------------------------------------------------


def maintenance_automations(
    config: Mapping[str, Any], room: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return a room's `al_maint_` lux-maintenance automation: none for a room with no
    adaptive bulb or no lux sensor, which would have nothing to nudge or nothing to
    measure."""
    bulbs = sorted(
        (bulb for unit in learning_units(config, room) for bulb in unit["bulbs"]),
        key=lambda bulb: bulb["id"],
    )
    sensors = sorted(room["adaptive_config"].get("lux_sensors", []))
    if not bulbs or not sensors:
        return []
    room_id = room["id"]
    interval = int(config_mod.system(config)["heartbeat_interval_minutes"])
    # HA's time_pattern takes /1../59 for minutes: an hourly cadence is minute 0.
    every = {"minutes": 0} if interval >= 60 else {"minutes": f"/{interval}"}
    defaults = config_mod.scene_defaults(SECTIONS[0])
    variables = {
        "row": f"{{{{ {_row(room_id, _current_section(room_id))} }}}}",
        # Decided in one template, from the row: HA parses each variable's render back to
        # a value, and keeps one it doesn't read as a plain decimal (1e-05, nan) as a
        # string, so a number passed between variables could turn into text and fail the
        # arithmetic that used it, part-way through a run.
        #
        # Lux is the mean of the sensors reading above 0, as the observer takes it. A
        # reading of 0 is the sensor's floor, not a measurement: with every sensor there
        # (or none available) there is no lux, and no nudge, rather than a ramp toward
        # full brightness under a sensor that reads 0 all night.
        "nudge": (
            "{% set enabled = row is mapping and "
            f"row.get({_literal(almanac.MAINTENANCE_ENABLED)}) is sameas true %}}"
            f"{{% set target = row.get({_literal(almanac.LUX_TARGET)}) if row is mapping "
            "else none %}"
            f"{{% set margin = (row.get({_literal(almanac.LUX_MARGIN)}) if row is mapping "
            f"else none) | float({defaults['lux_margin']}) %}}"
            f"{{% set vals = [{', '.join(_literal(s) for s in sensors)}] | map('states') "
            "| map('float', none) | reject('none') | select('gt', 0) | list %}"
            "{% if not enabled or target is not number or target is boolean or not vals %}"
            "none"
            "{% else %}{% set lux = vals | sum / vals | count %}"
            "{% if lux < target - margin %}up"
            "{% elif lux > target + margin %}down"
            "{% else %}none{% endif %}{% endif %}"
        ),
    }
    others = [scene_id(room_id, section) for section in SECTIONS]
    actions: list[dict[str, Any]] = [
        {"variables": variables},
        {"condition": "template", "value_template": "{{ nudge != 'none' }}"},
        {"action": "input_boolean.turn_on", "target": {"entity_id": guard_entity(room_id)}},
        *[_nudge(bulb, defaults["max_step_pct"]) for bulb in bulbs],
        {"delay": {"seconds": MAINT_SETTLE_SECONDS}},
        _release_guard(room_id, others),
    ]
    return [
        {
            "id": maint_id(room_id),
            "alias": maint_id(room_id),
            "description": (
                f"Light Map adaptive lux maintenance: nudges {room['name']}'s adaptive bulbs "
                "that are on toward the almanac's lux target for the section, brightness "
                "only. Runs on HA's own clock, so it carries on while Light Map is down. "
                f"{_GENERATED}"
            ),
            "mode": "single",
            "max_exceeded": "silent",
            "triggers": [{"trigger": "time_pattern", **every}],
            "conditions": [
                # The guard: one of the lane's own runs is mid-change; skip this tick.
                {"condition": "state", "entity_id": guard_entity(room_id), "state": "off"},
                # The hold: the user intervened this section; stay out of the way.
                {"condition": "state", "entity_id": hold_entity(room_id), "state": "off"},
            ],
            "actions": actions,
        }
    ]


def _nudge(bulb: Mapping[str, Any], default_step_pct: float) -> dict[str, Any]:
    """One bulb's step toward the target. Only a bulb that is on (its soft switch and the
    bulb itself) with a brightness to step from: maintenance never powers a bulb on, and
    floors at 1, so it can dim a light but never turn one off. The step's fraction is read
    from the row here, where it is used, and held to (0, 1]. The turn_on is
    continue_on_error, as the crossover's pushes are: a bulb that doesn't answer must not
    abort the run before it releases the guard (see `_unit_action`)."""
    light = bulb["entity_id"]
    current = _light_brightness(light)
    step = (
        f"{{% set b = {current} | int(0) %}}"
        f"{{% set pct = (row.get({_literal(almanac.MAX_STEP_PCT)}) if row is mapping else none) "
        f"| float({default_step_pct}) %}}"
        "{% set pct = [[pct, 0] | max, 1] | min %}"
        f"{{% set step = [[(b * pct) | int(0), 1] | max, {MAX_STEP}] | min %}}"
        "{% if nudge == 'up' %}{{ [b + step, 255] | min }}"
        "{% else %}{{ [b - step, 1] | max }}{% endif %}"
    )
    return {
        "alias": f"{bulb['name']} ({light})",
        "if": [
            {"condition": "state", "entity_id": switching.soft_entity(bulb["id"]), "state": "on"},
            {"condition": "state", "entity_id": light, "state": "on"},
            {"condition": "template", "value_template": f"{{{{ {current} | int(0) > 0 }}}}"},
        ],
        "then": [
            {
                "action": "light.turn_on",
                "continue_on_error": True,
                "target": {"entity_id": light},
                "data": {"brightness": step, "transition": MAINT_TRANSITION_SECONDS},
            }
        ],
    }


# -- Watchdogs -------------------------------------------------------------------------


def guard_watchdog_minutes(room: Mapping[str, Any]) -> int:
    """How long a room's guard may stay on before its watchdog releases it: ten minutes,
    or the room's longest legitimate hold (its slowest crossover, or maintenance) plus a
    margin when that is longer."""
    scenes = room["adaptive_config"]["scenes"]
    longest = max(
        MAINT_SETTLE_SECONDS,
        *(int(scenes[section]["transition_seconds"]) + SETTLE_SECONDS for section in SECTIONS),
    )
    return max(GUARD_WATCHDOG_MINUTES, math.ceil(longest / 60) + GUARD_WATCHDOG_MARGIN_MINUTES)


def watchdog_automations(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the `al_watchdog_` automations: per adaptive room, a guard stuck on (released,
    and a persistent notification) and a scene select that hasn't moved (a notification).
    There is none on the hold, which is meant to last the rest of a section.

    The guard's watchdog also acts when automations are reloaded: a deploy that rewrites a
    scene or the maintenance while it runs stops that run before it lowers the guard. Then
    a guard that no run holds any more is released at once (quietly: it is a deploy's
    routine aftermath, not a fault), rather than on the stuck timer, which a reload of the
    watchdog itself would have dropped."""
    found: list[dict[str, Any]] = []
    for room in adaptive_rooms(config):
        room_id, name = room["id"], _plain(room["name"])
        minutes = guard_watchdog_minutes(room)
        holders = guard_holders(config, room)
        found.append(
            {
                "id": watchdog_guard_id(room_id),
                "alias": watchdog_guard_id(room_id),
                "description": (
                    f"Light Map adaptive watchdog: releases {name}'s guard if it is stuck on, "
                    f"which silently stops maintenance and observation. {_GENERATED}"
                ),
                "mode": "single",
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": guard_entity(room_id),
                        "to": "on",
                        "for": {"minutes": minutes},
                        "id": "stuck",
                    },
                    {"trigger": "event", "event_type": "automation_reloaded", "id": "reloaded"},
                ],
                "conditions": [
                    {"condition": "state", "entity_id": guard_entity(room_id), "state": "on"},
                    {
                        "condition": "template",
                        "value_template": (f"{{{{ trigger.id == 'stuck' or {_idle(holders)} }}}}"),
                    },
                ],
                "actions": [
                    {
                        "action": "input_boolean.turn_off",
                        "target": {"entity_id": guard_entity(room_id)},
                    },
                    {
                        "if": [{"condition": "trigger", "id": "stuck"}],
                        "then": [
                            {
                                "action": "persistent_notification.create",
                                "data": {
                                    "notification_id": watchdog_guard_id(room_id),
                                    "title": "Light Map",
                                    "message": (
                                        f"The adaptive guard for {name} "
                                        f"({guard_entity(room_id)}) was stuck on for {minutes} "
                                        "minutes and has been released: a scene or maintenance "
                                        "run probably stopped part-way."
                                    ),
                                },
                            }
                        ],
                    },
                ],
            }
        )
        found.append(
            {
                "id": watchdog_stale_id(room_id),
                "alias": watchdog_stale_id(room_id),
                "description": (
                    f"Light Map adaptive watchdog: says so if {name} hasn't changed section "
                    f"for {STALE_SCENE_HOURS} hours. {_GENERATED}"
                ),
                "mode": "single",
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": scene_select_entity(room_id),
                        "for": {"hours": STALE_SCENE_HOURS},
                    }
                ],
                "conditions": [],
                "actions": [
                    {
                        "action": "persistent_notification.create",
                        "data": {
                            "notification_id": watchdog_stale_id(room_id),
                            "title": "Light Map",
                            "message": (
                                f"{name} has not changed section for {STALE_SCENE_HOURS} hours "
                                f"({scene_select_entity(room_id)}): Light Map's adaptive "
                                "runtime may be down."
                            ),
                        },
                    }
                ],
            }
        )
    return found


# -- The lane --------------------------------------------------------------------------


def generate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return every artefact in the `al` namespace; the generator registered with
    `core.generate`. An HA package: the helpers (both domains, always named), then the
    automations, room by room (its scenes in section order, then its maintenance), then
    the watchdogs."""
    automations: list[dict[str, Any]] = []
    for room in adaptive_rooms(config):
        automations += scene_automations(config, room)
        automations += maintenance_automations(config, room)
    automations += watchdog_automations(config)
    return {**helpers(config), "automation": automations}


def references(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """What the lane addresses in HA, each mapped to who uses it (the shape
    `switching.references` gives, for `core.deploy.check_references`): every adaptive
    bulb, its soft switch (the switching plane's, so that lane must be deployed first),
    and every adaptive room's lux and presence sensors."""
    entities: dict[str, str] = {}
    for room in adaptive_rooms(config):
        room_id = room["id"]
        for unit in learning_units(config, room):
            for bulb in unit["bulbs"]:
                entities[bulb["entity_id"]] = f"bulb '{bulb['id']}'"
                soft = switching.soft_entity(bulb["id"])
                entities[soft] = f"the soft switch of bulb '{bulb['id']}'"
        block = room["adaptive_config"]
        for kind, label in (("lux_sensors", "lux"), ("presence_sensors", "presence")):
            for sensor in block.get(kind, []):
                entities.setdefault(sensor, f"a {label} sensor of room '{room_id}'")
    return {"entities": dict(sorted(entities.items())), "devices": {}}
