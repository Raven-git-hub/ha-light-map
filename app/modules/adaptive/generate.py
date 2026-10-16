"""Adaptive generator: scene + maintenance + watchdog automations. Commands SOFT SWITCHES
for power; reads learning-unit targets for brightness. `conditions: []` on scene
automations.

Emits only the `al_` namespace: `al_scene_`, `al_maint_`, `al_active_`, `al_hold_`,
`sensor.al_almanac_`, `al_watchdog_`. Never touches the switching plane's `lm_*`.

Hard rules:

- Scene automations carry `conditions: []`. They are fired via `automation.trigger`
  (default `skip_condition: true`); a condition placed there would be silently ignored.
- Power goes only through soft switches. At a crossover each adaptive bulb's power intent is
  set on its soft switch: on for an auto/learned unit, held off for a unit that is "off for
  this section" (never a baked-in `light.turn_off`). The soft->bulb bridge makes the bulb
  follow.
- Every brightness command is guarded on "soft is on", so it can never power a bulb on. A
  crossover that changes section while a light stays on pushes the new brightness to the
  bulb directly, because the soft switch did not change and the bridge won't re-fire.
- The brightness a bulb comes on at is supplied to the core bridge as a fragment
  (`core.switching.register_bridge_brightness`, registered by `app.main`): a template that
  reads the unit's target from the almanac at runtime and renders a valid value on every
  path. The bridge, its target, and its power stay core's.
- Maintenance nudges only already-on bulbs, brightness only, never power. It iterates bulbs
  rather than one-entity groups.
- Nothing from the almanac is hardcoded: automations read `sensor.al_almanac_<room>` at
  runtime and branch.
- No template engine: plain Python data serialised by `core.generate`, HA Jinja kept as
  inert strings, byte-identical output for the same config.
"""

from collections.abc import Mapping
from typing import Any


def scene_automations(config: Mapping[str, Any], room: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a room's `al_scene_` automations, one per section crossover."""
    raise NotImplementedError


def maintenance_automations(
    config: Mapping[str, Any], room: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return a room's `al_maint_` lux-maintenance automations."""
    raise NotImplementedError


def watchdog_automations(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the `al_watchdog_` automations."""
    raise NotImplementedError


def generate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return every artefact in the `al` namespace; the generator registered with
    `core.generate`."""
    raise NotImplementedError
