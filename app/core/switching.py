"""Switching plane: generate the switching-plane artefacts: per-bulb/group soft-switch
helpers, hard->soft automations, soft->bulb bridge automations.

Names (the `lm_` lane):

- `input_boolean.lm_soft_<id>`: the soft switch, one per bulb and per switching group. A
  group's soft switch is a native HA group of its members' soft switches, so group state is
  derived (on if any member is on) with no hand-written state logic.
- `lm_hard_<id>` (hard->soft): each hard switch's trigger acts on its soft switch in its
  binding's mode: `toggle`, `follow`, or `press` (a one-way "all off" is a `press` that
  turns a group's soft switch off rather than toggling it).
- `lm_bridge_<id>` (soft->bulb): soft on turns the bulb(s) on and applies brightness (for
  an adaptive bulb, its learning unit's current target read from the almanac at runtime;
  for a plain bulb, a configured default or last brightness); soft off turns them off.

Hard rules:

- The soft switch owns power. The soft->bulb bridge is the only thing that powers a bulb on
  or off. Brightness rides alongside power, always guarded on "soft is on", so a brightness
  command can never power a bulb on.
- Soft switches are `input_boolean`s, pure power toggles: brightness never flows through
  them.
- Entity IDs only, never device IDs. A bulb is addressed by its `entity_id`, never by its
  `ha_match_key` (an opaque match key for the inventory's reconcile only).
- Output is plain Python data for `core.generate` to serialise: no template engine, HA
  Jinja kept as inert strings, byte-identical output for the same config, and no almanac
  values baked in.
- This lane emits only `lm_*` names and never touches the adaptive module's `al_*`.
"""

from collections.abc import Mapping
from typing import Any


def soft_switches(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the soft-switch helpers for every bulb and every switching group."""
    raise NotImplementedError


def hard_switch_automations(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one `lm_hard_<id>` automation per hard-switch binding."""
    raise NotImplementedError


def bridge_automations(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the `lm_bridge_<id>` soft->bulb automations."""
    raise NotImplementedError


def generate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return every artefact in the `lm` namespace; the generator registered with
    `core.generate`."""
    raise NotImplementedError
