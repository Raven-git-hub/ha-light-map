"""Adaptive Light: the first module. Learns a preferred light level per section and owns
brightness when on, for the rooms flagged adaptive.

Through the four module seams:

1. Config: the room's `adaptive_config` block (P5.0): lux and presence sensors, schedule
   profile, `adaptive_bulbs` (the rest of the room's active bulbs are switched-only, on the
   plain plane), learning units partitioning exactly the adaptive bulbs, and the scene
   matrix: per section, the room-wide tuning knobs and a mode (auto/off) per learning unit.
   Its shape is validated by `core.config` with the rest of the document, and edited by
   the adaptive editor (the Config tab), so the module reads it ready-checked.
2. Artefacts: the `al_` namespace (`al_scene_`, `al_maint_`, `al_active_`, `al_hold_`,
   `sensor.al_almanac_`, `al_watchdog_`), reconciled in its own lane.
3. Observation: per-bulb heartbeats and reactive events from `core.observe`.
4. Actuation: commands soft switches for power and sets brightness guarded on soft-on. It
   never powers a bulb on or off directly. Its bulbs come on at their learned target
   because it supplies the core bridge's soft-on brightness (an almanac-reading template).

A learning unit is a set of bulbs sharing one learned brightness target per section: a free
partition of the room's adaptive bulbs, independent of switching groups. Its members keep
their own soft switches and can be on or off independently.

An ignored bulb (a light the user set aside, such as an indicator LED; P4.1) is never part
of the plane: it has no soft switch and is no learning unit's member. Read a room's bulbs
through `core.inventory.active_bulbs`, never straight from the config; a room's adaptive
ones are those of them in its `adaptive_bulbs`.

The six sections are core's vocabulary (`SECTIONS`, from `core.config`, re-exported here):
the module depends on core for them, never the reverse, so core can validate the scene
matrix without importing the module.

P5.0 fills only the config and its editor; the engine (generator P5.1; scheduler,
analyser, almanac and runtime P5.2) is still stubbed and raises NotImplementedError.
"""

from app.core.config import SECTIONS

__all__ = ["SECTIONS"]
