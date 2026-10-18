"""Almanac: the learned model, keyed by learning unit; pushed to HA over REST.

Stored as insert-only snapshots (the store's `almanac` table, archived to CSV), so history
comes for free: the analyser's nightly pass writes one row per learning unit per section,
learned or not (`record`), and the one in force for a unit and section is the newest whose
`valid_from` (a local date) has come (`latest`). Published to HA as
`sensor.al_almanac_<room>`, keyed by learning-unit id (`payload`, `publish`).

The attribute contract (settled in P5.1; `docs/DESIGN.md`, "The almanac contract"). The
generated automations and the bridge fragment (`generate`) read the sensor's attributes,
never its state; `publish` writes exactly this shape:

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

A target is the unit's effective one: `payload` resolves the config's `mode` into it (off is
`"off"`, learned or not; auto is the learned brightness, or null), so the generated
automations never read the mode, and turning a unit off for a section is a republish. Unit
targets sit under `units` rather than beside the knobs because a unit id is a free slug: one
called `lux_target` would collide. (The predecessor mixed group ids and knobs in one mapping
and used 0 for off; `"off"` can't be mistaken for a brightness, where a 0 pushed as one
would turn a light off through a brightness command.) Everything is read defensively: an
absent sensor, section, `units` or unit key, or a value that isn't one of the three forms,
reads as "nothing learned yet". Other attributes (`friendly_name`, `mode`, `days_analysed`)
are added and are ignored.

What the payload takes from where (P5.2a):

- each unit's learned brightness, the room's learned lux target, and the state: from the
  snapshot in force (`latest`). The state is that snapshot's `valid_from` (`unknown` before
  the first), so a stalled analyser shows as a date that stops moving;
- `mode`, the knobs (`lux_margin`, `max_step_pct`, `maintenance_enabled`) and, while nothing
  is learned, a `lux_target_seed` above 0: from the config as it is now, so an edit in the
  adaptive editor takes effect at the next publish, without waiting for a pass.

Hard rules:

- Insert-only: a new learned value is a new snapshot, never an update.
- Nothing from the almanac is hardcoded into generated YAML. The automations and the bridge
  read this sensor at runtime, so a change needs only a republish, not regeneration.
- The learner never decides a light should be off: only the config's `off` publishes
  `"off"`, and a learned brightness below 1 publishes as nothing learned.
- A state set over REST doesn't survive an HA restart: whoever publishes re-publishes after
  one (the runtime does, on reconnecting).
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.core.config import SECTIONS
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

# The state before anything has been learned for the room.
UNLEARNED_STATE = "unknown"


def entity_id(room_id: str) -> str:
    """The almanac sensor's entity_id for a room."""
    return f"sensor.al_almanac_{room_id}"


def record(store: Store, snapshots: Sequence[Mapping[str, Any]]) -> None:
    """Append new insert-only almanac snapshots (rows as `analyser.run` returns them)."""
    if snapshots:
        store.append_almanac(snapshots)


def latest(store: Store, room_id: str, *, today: date) -> dict[str, dict[str, dict[str, Any]]]:
    """Return a room's current almanac: the latest snapshot per learning unit and section,
    as `{section: {unit id: row}}`.

    The one in force is the newest whose `valid_from` (a local date) is `today` or earlier:
    a pass in learning mode takes effect two days on, so until then the last one stays in
    force, as the predecessor's almanac did. Rows of units the config no longer has are
    included (a unit redrawn keeps its history); `payload` reads the current units'.
    """
    rows = store.query(
        """
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY section, learning_unit_id
                ORDER BY valid_from DESC, ts DESC, id DESC
            ) AS n
            FROM almanac
            WHERE room_id = ? AND valid_from <= ?
        )
        WHERE n = 1
        ORDER BY section, learning_unit_id
        """,
        (room_id, today.isoformat()),
    )
    current: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        row.pop("n", None)
        current.setdefault(row["section"], {})[row["learning_unit_id"]] = row
    return current


def target(row: Mapping[str, Any] | None, mode: str) -> int | str | None:
    """A unit's effective target for a section: "off" when the config holds it off
    (whatever was learned), else its learned brightness (1..255), else None."""
    if mode == HELD_OFF:
        return HELD_OFF
    brightness = row.get("brightness") if row else None
    if isinstance(brightness, bool) or not isinstance(brightness, int):
        return None
    # A learned 0 (only the legacy flags can average one) is no target: pushed as a
    # brightness, it would turn a light off, and the learner never decides that.
    return brightness if MIN_TARGET <= brightness <= MAX_TARGET else None


def payload(
    room: Mapping[str, Any], current: Mapping[str, Mapping[str, Mapping[str, Any]]]
) -> dict[str, Any]:
    """The almanac sensor's state and attributes for an adaptive room, from the snapshot in
    force (`latest`) and the room's config as it is now (its units, their modes, the
    section's knobs and seed): `{"state": ..., "attributes": ...}`, attributes in the
    contract's shape, sections in canonical order and units sorted, so the same almanac and
    config always give the same payload."""
    block = room["adaptive_config"]
    unit_ids = sorted(unit["id"] for unit in block.get("learning_units", []))
    every_row = [row for section in current.values() for row in section.values()]
    newest = max(every_row, key=_recency, default=None)
    attributes: dict[str, Any] = {}
    for section in SECTIONS:
        knobs = block["scenes"][section]
        rows = current.get(section, {})
        # The room's lux target rides on every unit's row: take the newest in force (a unit
        # redrawn keeps the room's learned lux, which is the room's, not the unit's).
        learned = max(rows.values(), key=_recency, default={}).get("lux_target")
        seed = knobs.get("lux_target_seed")
        if isinstance(seed, bool) or not isinstance(seed, (int, float)) or seed <= 0:
            # A lux of 0 is a sensor's floor, never a measurement: a target of 0 can't be
            # met, and maintenance would dim the lights toward 1 trying.
            seed = None
        attributes[section] = {
            UNITS: {
                unit_id: target(rows.get(unit_id), knobs["units"][unit_id]["mode"])
                for unit_id in unit_ids
            },
            LUX_TARGET: learned if learned is not None else seed,
            LUX_MARGIN: knobs["lux_margin"],
            MAX_STEP_PCT: knobs["max_step_pct"],
            MAINTENANCE_ENABLED: knobs["maintenance_enabled"],
        }
    attributes["friendly_name"] = f"AL Almanac {room['id']}"
    attributes["mode"] = newest["mode"] if newest else None
    attributes["days_analysed"] = newest["days_analysed"] if newest else 0
    return {
        "state": newest["valid_from"] if newest else UNLEARNED_STATE,
        "attributes": attributes,
    }


def _recency(row: Mapping[str, Any]) -> tuple[str, str, int]:
    return (row.get("valid_from") or "", row.get("ts") or "", row.get("id") or 0)


async def publish(ha: HAClient, room_id: str, almanac: Mapping[str, Any]) -> None:
    """Push a room's almanac to HA as `sensor.al_almanac_<room>`, in the attribute contract's
    shape: `almanac` is a `payload`."""
    await ha.set_state(entity_id(room_id), str(almanac["state"]), almanac["attributes"])
