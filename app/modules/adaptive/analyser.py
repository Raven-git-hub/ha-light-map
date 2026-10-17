"""Analyser: the nightly learning pipeline. Pools per-bulb observations per LEARNING UNIT
into one target. Ported; behavioural changes gated behind flags.

The pipeline is ha-adaptive-light's (itself a port of the proven n8n pipeline: Parse
Timeline -> Merge Reactive -> Compute Daily Stats -> Create Almanac), and the learning maths
is unchanged: the same recency ladder, the same confidence bands and multipliers, the same
reactive weight, the same post-reactive boost, the same rolling weighted average, and the
same bootstrap and learning states. Every behavioural difference from the n8n version is
gated behind a flag on `AnalyserFlags`, at the predecessor's defaults, so this reproduces
the predecessor exactly and `tools/compare_analyser.py` can run the two side by side.

The one rework: observations are stored per bulb (long format: `heartbeat` + `bulb_reading`,
`reactive_event` + `reactive_change`) and pooled per learning unit here, at analysis time,
into one target per unit per section (`pool_unit`). A sample holds one reading per adaptive
bulb; stability is judged per bulb; a unit's daily brightness is the weighted mean over all
its members' eligible readings together, and its on-fraction likewise. A unit of one bulb is
exactly the predecessor's per-group computation: `tests/test_analyser.py` runs the
predecessor's analyser (`tests/reference/`, verbatim) and this one on the same readings, under
every combination of the flags, and requires the same almanac.

What the predecessor's store recorded and this one derives (docs/DESIGN.md, "The analyser"):

- the day a sample belongs to: its timestamp's date in HA's time zone, the zone of the
  `now` a pass is given;
- occupied: occupancy 1, or null in a room with no presence sensor configured (no sensor
  meant "always occupied" there; a configured sensor with no usable state read as not
  occupied there, and is excluded here);
- any light on: any adaptive bulb of the room on. A switched-only bulb is outside the
  learner entirely.

A reading with no usable state (`is_on` null) is no reading for its bulb (the predecessor
could only record it as off); a heartbeat or reactive event with no section is skipped; a
reactive event counts when it changed an adaptive bulb.

Hard rules:

- Pooling happens only here. Storage stays per bulb so learning units can be redrawn
  without losing history.
- Behavioural changes to the ported pipeline are gated behind flags.
- Reads come from the store's SQLite layer, which is rebuildable from the CSV archive.
- A lux reading of 0 is a floor, not a measurement.
- The learned brightness is kept whatever a unit's mode: holding a unit off is the
  config's, resolved by the almanac's publish, so turning it back to auto finds its target.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from datetime import tzinfo as TimeZone
from typing import Any

from app.core.config import SECTIONS
from app.core.store import Store, timestamp
from app.modules.adaptive.generate import adaptive_rooms, learning_units

# -- Configuration ---------------------------------------------------------------------


@dataclass(frozen=True)
class AnalyserFlags:
    """The three deliberate departures from the n8n behaviour.

    All default to the new behaviour (the predecessor's own defaults, so this reproduces
    ha-adaptive-light exactly). Set every one to its legacy value to reproduce the original
    n8n pipeline. A later change to the learning maths gets a flag here too, defaulting to
    the behaviour before it.
    """

    per_group_eligibility: bool = True
    # NEW (legacy: False). A group's brightness average includes only samples where THAT
    # group was on. Previously a group that was off for most of a section contributed zeros,
    # pulling its learned value toward a brightness it never actually sat at - which is how
    # Day ended up at tv_light=142 for a light that was off all day. (Light Map: per bulb,
    # and pooled per learning unit.)

    per_group_stability: bool = True
    # NEW (legacy: False). The stability filter is applied per group rather than per row.
    # Previously, one group ramping caused the whole sample to be discarded, throwing away
    # perfectly settled readings from every other group in the room. (Light Map: per bulb.)

    brightness_requires_measurable_lux: bool = False
    # NEW (legacy: True). Rows where ambient lux reads 0 are excluded from the LUX average
    # but still contribute BRIGHTNESS. Under the legacy behaviour they were dropped entirely,
    # which is why night and sleep learned so little - yet those are precisely the sections
    # where maintenance is disabled and the scene brightness is the entire behaviour. Lux
    # eligibility itself is unchanged: a reading of 0 is still never treated as a real
    # measurement.


# The n8n pipeline, for `tools/compare_analyser.py`.
LEGACY = AnalyserFlags(
    per_group_eligibility=False,
    per_group_stability=False,
    brightness_requires_measurable_lux=True,
)


@dataclass(frozen=True)
class LearningConfig:
    """The analyser's tuning: the config's `learning` (`from_config`), each field defaulting
    to the predecessor's, which reproduce its results."""

    lookback_days: int = 21
    publish_delay_days: int = 2
    bootstrap_min_days: int = 7
    reactive_weight: float = 5.0
    post_reactive_boost: tuple[float, ...] = (3.0, 2.0, 1.5)
    recency_weights: tuple[tuple[int | None, float], ...] = (
        (3, 3.0),
        (7, 2.0),
        (14, 1.0),
        (None, 0.5),
    )
    high_weight_threshold: float = 30.0
    medium_weight_threshold: float = 10.0
    high_multiplier: float = 1.5
    medium_multiplier: float = 1.0
    low_multiplier: float = 0.5
    flags: AnalyserFlags = field(default_factory=AnalyserFlags)

    @classmethod
    def from_config(
        cls, learning: Mapping[str, Any] | None, flags: AnalyserFlags | None = None
    ) -> "LearningConfig":
        """The config's `learning` block (validated by `core.config`) over the defaults."""
        learning = learning or {}
        fields: dict[str, Any] = {
            key: learning[key]
            for key in (
                "lookback_days",
                "publish_delay_days",
                "bootstrap_min_days",
                "reactive_weight",
            )
            if key in learning
        }
        if "post_reactive_boost" in learning:
            fields["post_reactive_boost"] = tuple(learning["post_reactive_boost"])
        if "recency_weights" in learning:
            fields["recency_weights"] = tuple(
                (step["max_age_days"], step["weight"]) for step in learning["recency_weights"]
            )
        fields.update(learning.get("confidence") or {})
        return cls(**fields, flags=flags or AnalyserFlags())

    def recency_weight(self, age_days: int) -> float:
        for max_age, weight in self.recency_weights:
            if max_age is None or age_days <= max_age:
                return weight
        return self.recency_weights[-1][1]

    def confidence(self, total_weight: float, reactive_count: int) -> str:
        if total_weight > self.high_weight_threshold or reactive_count > 0:
            return "high"
        if total_weight >= self.medium_weight_threshold:
            return "medium"
        return "low"

    def confidence_multiplier(self, band: str) -> float:
        return {
            "high": self.high_multiplier,
            "medium": self.medium_multiplier,
            "low": self.low_multiplier,
        }[band]


# -- Sample model ----------------------------------------------------------------------


@dataclass
class BulbReading:
    """One bulb in one sample (the predecessor's GroupReading, per bulb)."""

    is_on: bool
    brightness: int | None
    stable: bool = True


@dataclass
class Sample:
    ts: str
    source: str  # 'heartbeat' | 'reactive'
    lux: float | None
    any_light_on: bool
    bulbs: dict[str, BulbReading]
    weight: float = 1.0
    settled: bool = True  # every bulb stable (row-level view)


# -- Loading ---------------------------------------------------------------------------


def load_timeline(
    store: Store,
    room_id: str,
    bulb_ids: Sequence[str],
    cutoff_date: date,
    cfg: LearningConfig,
    *,
    tz: TimeZone,
    presence_configured: bool,
) -> dict[str, dict[str, list[Sample]]]:
    """timeline[local_date][section] -> chronological samples, from the room's heartbeats and
    reactive events on or after `cutoff_date` (local), with the readings of `bulb_ids`."""
    bulbs = set(bulb_ids)
    cutoff = cutoff_date.isoformat()
    # Every row from the cutoff's local midnight on; the local date is worked out per row.
    since = timestamp(datetime.combine(cutoff_date, time(0, 0), tzinfo=tz))
    timeline: dict[str, dict[str, list[Sample]]] = defaultdict(lambda: {s: [] for s in SECTIONS})

    readings: dict[int, dict[str, BulbReading]] = defaultdict(dict)
    for row in store.query(
        """
        SELECT r.heartbeat_id, r.bulb_id, r.is_on, r.brightness
        FROM bulb_reading r
        JOIN heartbeat h ON h.id = r.heartbeat_id
        WHERE h.room_id = ? AND h.ts >= ?
        """,
        (room_id, since),
    ):
        # A bulb HA had no usable state for has no reading: never counted as off.
        if row["bulb_id"] in bulbs and row["is_on"] is not None:
            readings[row["heartbeat_id"]][row["bulb_id"]] = BulbReading(
                bool(row["is_on"]), row["brightness"]
            )

    for row in store.query(
        "SELECT id, ts, section, lux, occupancy FROM heartbeat "
        "WHERE room_id = ? AND ts >= ? ORDER BY ts",
        (room_id, since),
    ):
        if row["section"] not in SECTIONS:
            continue  # recorded before any module reported sections
        # No presence sensor means always occupied, not never: the predecessor's rule.
        occupied = row["occupancy"] == 1 or (row["occupancy"] is None and not presence_configured)
        if not occupied:
            continue
        local_date = _local_date(row["ts"], tz)
        if local_date < cutoff:
            continue
        bulbs_now = readings.get(row["id"], {})
        timeline[local_date][row["section"]].append(
            Sample(
                ts=row["ts"],
                source="heartbeat",
                lux=row["lux"],
                any_light_on=any(r.is_on for r in bulbs_now.values()),
                bulbs=bulbs_now,
                weight=1.0,
            )
        )

    # Reactive events are NEVER filtered on occupancy: a human moving a dimmer is definitive
    # proof of presence, whatever the sensor says.
    rx_bulbs: dict[int, dict[str, BulbReading]] = defaultdict(dict)
    rx_changed: dict[int, int] = defaultdict(int)
    for row in store.query(
        """
        SELECT c.reactive_event_id, c.bulb_id, c.is_on_after, c.brightness_after, c.changed
        FROM reactive_change c
        JOIN reactive_event e ON e.id = c.reactive_event_id
        WHERE e.room_id = ? AND e.ts >= ?
        """,
        (room_id, since),
    ):
        if row["bulb_id"] not in bulbs:
            continue
        if row["is_on_after"] is not None:
            rx_bulbs[row["reactive_event_id"]][row["bulb_id"]] = BulbReading(
                bool(row["is_on_after"]), row["brightness_after"]
            )
        rx_changed[row["reactive_event_id"]] += int(row["changed"])

    for row in store.query(
        "SELECT id, ts, section, lux_after FROM reactive_event "
        "WHERE room_id = ? AND ts >= ? ORDER BY ts",
        (room_id, since),
    ):
        if row["section"] not in SECTIONS:
            continue
        if rx_changed[row["id"]] == 0:
            continue  # phantom event: nothing the learner learns changed past the min-delta
        local_date = _local_date(row["ts"], tz)
        if local_date < cutoff:
            continue
        if local_date not in timeline:
            continue  # no heartbeats that day
        timeline[local_date][row["section"]].append(
            Sample(
                ts=row["ts"],
                source="reactive",
                lux=row["lux_after"],
                any_light_on=True,
                bulbs=rx_bulbs.get(row["id"], {}),
                weight=cfg.reactive_weight,
            )
        )

    for day in timeline.values():
        for section in SECTIONS:
            day[section].sort(key=lambda s: s.ts)

    return dict(timeline)


def _local_date(ts: str, tz: TimeZone) -> str:
    return datetime.fromisoformat(ts).astimezone(tz).date().isoformat()


# -- Weighting and stability -----------------------------------------------------------


def apply_post_reactive_boost(samples: list[Sample], cfg: LearningConfig) -> None:
    boost = list(cfg.post_reactive_boost)
    remaining = 0
    for s in samples:
        if s.source == "reactive":
            remaining = len(boost)
        elif remaining > 0:
            s.weight = boost[len(boost) - remaining]
            remaining -= 1


def mark_stability(samples: list[Sample], bulb_ids: Sequence[str], cfg: LearningConfig) -> None:
    """A heartbeat reading is 'stable' when it matches the previous heartbeat - i.e. the
    light has settled rather than being mid-ramp. Reactive samples are always stable; the
    first heartbeat always is."""
    prev: Sample | None = None
    for s in samples:
        if s.source == "reactive":
            for b in s.bulbs.values():
                b.stable = True
            s.settled = True
        elif prev is None:
            for b in s.bulbs.values():
                b.stable = True
            s.settled = True
        else:
            for bulb_id in bulb_ids:
                cur, old = s.bulbs.get(bulb_id), prev.bulbs.get(bulb_id)
                if cur is None:
                    continue
                cur.stable = bool(old is not None and cur.brightness == old.brightness)
            s.settled = all(s.bulbs[b].stable for b in bulb_ids if b in s.bulbs)
        if s.source == "heartbeat":
            prev = s

    if not cfg.flags.per_group_stability:
        for s in samples:  # legacy: all-or-nothing
            for b in s.bulbs.values():
                b.stable = s.settled


# -- Daily statistics ------------------------------------------------------------------


@dataclass
class UnitStats:
    """A learning unit's day in one section: its pooled brightness (None: nothing eligible),
    the weight behind it, and how much of the section its bulbs were on."""

    brightness: int | None
    weight: float
    on_fraction: float | None


@dataclass
class SectionStats:
    lux_target: float | None
    total_weight: float
    sample_count: int
    reactive_count: int
    confidence: str
    units: dict[str, UnitStats]


def pool_unit(samples: Sequence[Sample], members: Sequence[str], cfg: LearningConfig) -> UnitStats:
    """Pool a learning unit's member-bulb readings for one section and day into one target.

    The predecessor's per-group averaging, over every member's readings together: each
    eligible reading adds its brightness times its sample's weight, so a unit's target is
    the weighted mean over all its bulbs and samples, and a unit of one bulb is exactly the
    predecessor's per-group result. Expects `apply_post_reactive_boost` and `mark_stability`
    to have run over the samples (`compute_daily_stats` does).
    """
    flags = cfg.flags
    total = weight = on_weight = denominator = 0.0
    for s in samples:
        if not s.any_light_on:
            continue
        for bulb_id in members:
            reading = s.bulbs.get(bulb_id)
            if reading is None:
                continue
            stable = reading.stable if flags.per_group_stability else s.settled
            if not stable:
                continue
            if flags.brightness_requires_measurable_lux and not (s.lux and s.lux > 0):
                continue

            denominator += s.weight
            if reading.is_on:
                on_weight += s.weight

            eligible = reading.is_on if flags.per_group_eligibility else True
            if eligible and reading.brightness is not None:
                total += reading.brightness * s.weight
                weight += s.weight

    return UnitStats(
        brightness=round(total / weight) if weight > 0 else None,
        weight=weight,
        on_fraction=round(on_weight / denominator, 3) if denominator > 0 else None,
    )


def compute_daily_stats(
    samples: list[Sample], units: Mapping[str, Sequence[str]], cfg: LearningConfig
) -> SectionStats | None:
    """One day's statistics for one section: the room's lux target and each unit's pooled
    brightness (`units`: unit id -> member bulb ids)."""
    if not samples:
        return None

    bulb_ids = sorted({b for members in units.values() for b in members})
    apply_post_reactive_boost(samples, cfg)
    mark_stability(samples, bulb_ids, cfg)

    reactive_count = sum(1 for s in samples if s.source == "reactive")

    # --- room-level lux -------------------------------------------------------------------
    # A reading of 0 is the sensor's floor, not a measurement: excluded, exactly as in the
    # original pipeline.
    lux_sum = lux_weight = 0.0
    settled_weight = 0.0
    settled_count = 0
    for s in samples:
        if not (s.any_light_on and s.settled):
            continue
        settled_weight += s.weight
        settled_count += 1
        if s.lux is not None and s.lux > 0:
            lux_sum += s.lux * s.weight
            lux_weight += s.weight

    if settled_weight == 0:
        return None

    lux_target = round(lux_sum / lux_weight, 1) if lux_weight > 0 else None

    return SectionStats(
        lux_target=lux_target,
        total_weight=round(settled_weight, 1),
        sample_count=settled_count,
        reactive_count=reactive_count,
        confidence=cfg.confidence(settled_weight, reactive_count),
        units={unit_id: pool_unit(samples, members, cfg) for unit_id, members in units.items()},
    )


# -- Rolling almanac -------------------------------------------------------------------


def build_almanac(
    daily: Mapping[str, Mapping[str, SectionStats | None]],
    unit_ids: Sequence[str],
    cfg: LearningConfig,
) -> dict[str, Any]:
    """The rolling almanac over the days analysed: `_meta` (the state, `valid_from`, the
    days analysed) and per section None (no day contributed) or the learned values, each
    unit's under `units` and `on_fraction`.

    The predecessor's `build_almanac`, less what is now the publish's: the scene matrix's
    knobs, and a unit held off (the predecessor published 0 for it here), which `almanac`
    resolves from the config, so the learned values are kept whatever the mode."""
    dates = sorted(daily)
    if not dates:
        return {"_meta": {"mode": "provisional", "days_analysed": 0}}

    most_recent = datetime.strptime(dates[-1], "%Y-%m-%d").date()

    acc = {
        s: {
            "lux_sum": 0.0,
            "lux_weight": 0.0,
            "g_sum": {u: 0.0 for u in unit_ids},
            "g_weight": {u: 0.0 for u in unit_ids},
            "on_sum": {u: 0.0 for u in unit_ids},
            "on_weight": {u: 0.0 for u in unit_ids},
            "days": 0,
            "high_days": 0,
            "trust": 0.0,
        }
        for s in SECTIONS
    }

    for d in dates:
        age = (most_recent - datetime.strptime(d, "%Y-%m-%d").date()).days
        recency = cfg.recency_weight(age)
        for section in SECTIONS:
            stats = daily[d].get(section)
            if stats is None:
                continue
            a = acc[section]
            combined = recency * cfg.confidence_multiplier(stats.confidence) * stats.total_weight
            if combined <= 0:
                continue

            if stats.lux_target is not None:
                a["lux_sum"] += stats.lux_target * combined
                a["lux_weight"] += combined

            for u in unit_ids:
                unit = stats.units[u]
                if unit.brightness is not None:
                    # Per-unit divisor: a unit only contributes weight on the days it was
                    # actually on.
                    a["g_sum"][u] += unit.brightness * combined
                    a["g_weight"][u] += combined
                if unit.on_fraction is not None:
                    a["on_sum"][u] += unit.on_fraction * combined
                    a["on_weight"][u] += combined

            a["days"] += 1
            a["trust"] += combined
            if stats.confidence == "high":
                a["high_days"] += 1

    days_analysed = len(dates)
    mode = "learning" if days_analysed >= cfg.bootstrap_min_days else "bootstrap"
    # A fresh room comes alive the next day; once learning, only complete days feed a
    # published almanac, and a big swing can't land overnight.
    delay = cfg.publish_delay_days if mode == "learning" else 0
    valid_from = (most_recent + timedelta(days=delay)).isoformat()

    almanac: dict[str, Any] = {
        "_meta": {
            "updated": dates[-1],
            "valid_from": valid_from,
            "days_analysed": days_analysed,
            "mode": mode,
        }
    }

    for section in SECTIONS:
        a = acc[section]
        if a["days"] == 0:
            almanac[section] = None
            continue
        almanac[section] = {
            "lux_target": round(a["lux_sum"] / a["lux_weight"], 1) if a["lux_weight"] else None,
            "days_contributing": a["days"],
            "high_confidence_days": a["high_days"],
            # Accumulated effective weight behind this section: recency x confidence x
            # per-day settled weight, summed over the window. Surfaced so a view can show
            # trust approaching its thresholds smoothly, rather than only the bucketed
            # low/medium/high.
            "trust_weight": round(a["trust"], 1),
            "units": {
                u: round(a["g_sum"][u] / a["g_weight"][u]) if a["g_weight"][u] else None
                for u in unit_ids
            },
            "on_fraction": {
                u: round(a["on_sum"][u] / a["on_weight"][u], 3) if a["on_weight"][u] else None
                for u in unit_ids
            },
        }
    return almanac


def analyse_room(
    store: Store,
    room_id: str,
    units: Mapping[str, Sequence[str]],
    cfg: LearningConfig,
    today: date,
    *,
    tz: TimeZone,
    presence_configured: bool,
) -> dict[str, Any]:
    """The rolling almanac for one room (`units`: unit id -> member bulb ids), from the
    lookback window ending `today` (local, in `tz`)."""
    cutoff = today - timedelta(days=cfg.lookback_days)
    bulb_ids = sorted({b for members in units.values() for b in members})
    timeline = load_timeline(
        store, room_id, bulb_ids, cutoff, cfg, tz=tz, presence_configured=presence_configured
    )
    daily = {
        d: {s: compute_daily_stats(sections[s], units, cfg) for s in SECTIONS}
        for d, sections in timeline.items()
    }
    return build_almanac(daily, sorted(units), cfg)


# -- The nightly pass ------------------------------------------------------------------


def run(
    store: Store,
    config: Mapping[str, Any],
    *,
    now: datetime,
    flags: AnalyserFlags | None = None,
) -> list[dict[str, Any]]:
    """Run the nightly pass over every adaptive room; return the new almanac snapshots (rows
    for `almanac.record`), one per learning unit per section, learned or not. A room with no
    learning unit, or no data in the lookback window, gets none.

    `now` must carry HA's time zone: a sample's day is its date there. Reads the store
    only; the caller records what it returns.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now needs HA's time zone: a sample's day is its date there")
    cfg = LearningConfig.from_config(config.get("learning"), flags)
    rows: list[dict[str, Any]] = []
    for room in adaptive_rooms(config):
        units = {
            unit["id"]: [bulb["id"] for bulb in unit["bulbs"]]
            for unit in learning_units(config, room)
        }
        if not units:
            continue
        block = room["adaptive_config"]
        almanac = analyse_room(
            store,
            room["id"],
            units,
            cfg,
            now.date(),
            tz=now.tzinfo,
            presence_configured=bool(block.get("presence_sensors")),
        )
        rows += snapshots(room, almanac, now=now)
    return rows


def snapshots(
    room: Mapping[str, Any], almanac: Mapping[str, Any], *, now: datetime
) -> list[dict[str, Any]]:
    """A room's almanac as the store's rows: one per learning unit per section, learned or
    not, so the newest row in force for a unit and section always comes from the newest pass
    in force. None for a room with no data (the last pass's snapshot stays in force, as the
    predecessor kept its last almanac)."""
    meta = almanac.get("_meta") or {}
    if not meta.get("days_analysed"):
        return []
    block = room["adaptive_config"]
    unit_ids = sorted(unit["id"] for unit in block.get("learning_units", []))
    rows = []
    for section in SECTIONS:
        entry = almanac.get(section) or {}
        knobs = block["scenes"][section]
        for unit_id in unit_ids:
            rows.append(
                {
                    "ts": now,
                    "room_id": room["id"],
                    "section": section,
                    "learning_unit_id": unit_id,
                    "brightness": (entry.get("units") or {}).get(unit_id),
                    "on_fraction": (entry.get("on_fraction") or {}).get(unit_id),
                    "lux_target": entry.get("lux_target"),
                    "lux_margin": knobs["lux_margin"],
                    "maintenance_enabled": knobs["maintenance_enabled"],
                    "max_step_pct": knobs["max_step_pct"],
                    "trust_weight": entry.get("trust_weight"),
                    "days_contributing": entry.get("days_contributing", 0),
                    "high_confidence_days": entry.get("high_confidence_days", 0),
                    "days_analysed": meta["days_analysed"],
                    "valid_from": meta["valid_from"],
                    "mode": meta["mode"],
                }
            )
    return rows
