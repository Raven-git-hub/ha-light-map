"""Almanac: the learned model, keyed by learning unit; pushed to HA over REST.

Stored as insert-only snapshots (the store's `almanac` table, archived to CSV), so history
comes for free. Published to HA as `sensor.al_almanac_<room>`, keyed by learning-unit id.

The attribute contract (settled in P5.1; `docs/DESIGN.md`, "The almanac contract"). The
generated automations and the bridge fragment (`generate`) read the sensor's attributes,
never its state; `publish` (P5.2) must write exactly this shape:

    attributes.<section>  (one per section id, `core.config.SECTIONS`)
      = {
          units:               {<learning-unit id>: <target>}
          lux_target:          number | null
          lux_margin:          number
          max_step_pct:        number in (0, 1]
          maintenance_enabled: true | false
        }

    <target> = an integer 1..255   on, at this brightness
             | "off"               held off for the section (the unit's mode is off)
             | null, or absent     nothing learned yet: leave the unit's bulbs as they are

A target is the unit's effective one: `publish` resolves the config's `mode` into it, so
the generated automations never read the mode, and turning a unit off for a section is a
republish. Unit targets sit under `units` rather than beside the knobs because a unit id is
a free slug: one called `lux_target` would collide. (The predecessor mixed group ids and
knobs in one mapping and used 0 for off; `"off"` can't be mistaken for a brightness, where
a 0 pushed as one would turn a light off through a brightness command.) Everything is read
defensively: an absent sensor, section, `units` or unit key, or a value that isn't one of
the three forms, reads as "nothing learned yet". Other attributes (`friendly_name`, ...)
may be added and are ignored.

Hard rules:

- Insert-only: a new learned value is a new snapshot, never an update.
- Nothing from the almanac is hardcoded into generated YAML. The automations and the bridge
  read this sensor at runtime, so a change needs only a republish, not regeneration.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.ha import HAClient
from app.core.store import Store

# The contract's names (see the module docstring). Constants only: importing this module
# still does no work.
UNITS = "units"
LUX_TARGET = "lux_target"
LUX_MARGIN = "lux_margin"
MAX_STEP_PCT = "max_step_pct"
MAINTENANCE_ENABLED = "maintenance_enabled"
HELD_OFF = "off"
MIN_TARGET = 1
MAX_TARGET = 255


def entity_id(room_id: str) -> str:
    """The almanac sensor's entity_id for a room."""
    return f"sensor.al_almanac_{room_id}"


def latest(store: Store, room_id: str) -> dict[str, Any]:
    """Return a room's current almanac: the latest snapshot per learning unit and section."""
    raise NotImplementedError


def record(store: Store, snapshots: Sequence[Mapping[str, Any]]) -> None:
    """Append new insert-only almanac snapshots."""
    raise NotImplementedError


async def publish(ha: HAClient, room_id: str, almanac: Mapping[str, Any]) -> None:
    """Push a room's almanac to HA as `sensor.al_almanac_<room>`, in the attribute contract's
    shape."""
    raise NotImplementedError
