# The predecessor's analyser, VERBATIM, as the oracle for Light Map's analyser parity tests
# (tests/test_analyser.py). Not used by the app, and never edited: a parity test is only as
# good as its oracle's fidelity. Copied from Raven-git-hub/ha-adaptive-light (the same
# owner's), app/analyser.py, at commit 9ec76a73b9fab7abc1a731cd1fa2c1351f0ef205. Below this
# header the file is byte for byte the predecessor's.
#
"""
Adaptive Light - analyser.

Port of the proven n8n pipeline (Parse Timeline -> Merge Reactive ->
Compute Daily Stats -> Create Almanac) onto the SQLite store.

The learning maths is unchanged: same recency ladder, same confidence
bands and multipliers, same reactive weight, same post-reactive boost,
same rolling weighted average. Every behavioural difference from the
n8n version is gated behind a flag on AnalyserFlags and defaults are
stated there, so the two can be run against identical data and diffed.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AnalyserFlags:
    """The three deliberate departures from the n8n behaviour.

    All default to the new behaviour. Set every one to its legacy value
    to reproduce the original pipeline exactly.
    """

    per_group_eligibility: bool = True
    # NEW (legacy: False). A group's brightness average includes only
    # samples where THAT group was on. Previously a group that was off
    # for most of a section contributed zeros, pulling its learned value
    # toward a brightness it never actually sat at - which is how Day
    # ended up at tv_light=142 for a light that was off all day.

    per_group_stability: bool = True
    # NEW (legacy: False). The stability filter is applied per group
    # rather than per row. Previously, one group ramping caused the
    # whole sample to be discarded, throwing away perfectly settled
    # readings from every other group in the room.

    brightness_requires_measurable_lux: bool = False
    # NEW (legacy: True). Rows where ambient lux reads 0 are excluded
    # from the LUX average but still contribute BRIGHTNESS. Under the
    # legacy behaviour they were dropped entirely, which is why night
    # and sleep learned so little - yet those are precisely the sections
    # where maintenance is disabled and the scene brightness is the
    # entire behaviour. Lux eligibility itself is unchanged: a reading
    # of 0 is still never treated as a real measurement.


@dataclass(frozen=True)
class LearningConfig:
    lookback_days: int = 21
    publish_delay_days: int = 2
    bootstrap_min_days: int = 7
    reactive_weight: float = 5.0
    post_reactive_boost: tuple[float, ...] = (3.0, 2.0, 1.5)
    recency_weights: tuple[tuple[int | None, float], ...] = (
        (3, 3.0), (7, 2.0), (14, 1.0), (None, 0.5),
    )
    high_weight_threshold: float = 30.0
    medium_weight_threshold: float = 10.0
    high_multiplier: float = 1.5
    medium_multiplier: float = 1.0
    low_multiplier: float = 0.5
    flags: AnalyserFlags = field(default_factory=AnalyserFlags)

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


# ---------------------------------------------------------------------
# Sample model
# ---------------------------------------------------------------------

@dataclass
class GroupReading:
    is_on: bool
    brightness: int | None
    stable: bool = True


@dataclass
class Sample:
    ts: str
    source: str                       # 'heartbeat' | 'reactive'
    lux: float | None
    any_light_on: bool
    groups: dict[str, GroupReading]
    weight: float = 1.0
    settled: bool = True              # every group stable (row-level view)


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------

def load_timeline(
    conn: sqlite3.Connection,
    room_id: str,
    group_ids: Iterable[str],
    cutoff_date: str,
    cfg: LearningConfig,
) -> dict[str, dict[str, list[Sample]]]:
    """timeline[local_date][section] -> chronological samples."""
    group_ids = list(group_ids)
    timeline: dict[str, dict[str, list[Sample]]] = defaultdict(
        lambda: {s: [] for s in SECTIONS}
    )

    hb_rows = conn.execute(
        """
        SELECT id, local_date, section, ts, ambient_lux, any_light_on
        FROM heartbeat
        WHERE room_id = ? AND local_date >= ? AND occupied = 1
        ORDER BY ts
        """,
        (room_id, cutoff_date),
    ).fetchall()

    hb_groups: dict[int, dict[str, GroupReading]] = defaultdict(dict)
    for hb_id, gid, is_on, brightness in conn.execute(
        """
        SELECT hg.heartbeat_id, hg.group_id, hg.is_on, hg.brightness
        FROM heartbeat_group hg
        JOIN heartbeat h ON h.id = hg.heartbeat_id
        WHERE h.room_id = ? AND h.local_date >= ?
        """,
        (room_id, cutoff_date),
    ):
        hb_groups[hb_id][gid] = GroupReading(bool(is_on), brightness)

    for hb_id, local_date, section, ts, lux, any_on in hb_rows:
        timeline[local_date][section].append(
            Sample(
                ts=ts,
                source="heartbeat",
                lux=lux,
                any_light_on=bool(any_on),
                groups=hb_groups.get(hb_id, {}),
                weight=1.0,
            )
        )

    # Reactive events are NEVER filtered on occupancy: a human moving a
    # dimmer is definitive proof of presence, whatever the sensor says.
    rx_rows = conn.execute(
        """
        SELECT id, local_date, section, ts, lux_after
        FROM reactive
        WHERE room_id = ? AND local_date >= ?
        ORDER BY ts
        """,
        (room_id, cutoff_date),
    ).fetchall()

    rx_groups: dict[int, dict[str, GroupReading]] = defaultdict(dict)
    rx_changed: dict[int, int] = defaultdict(int)
    for rx_id, gid, is_on_after, b_after, changed in conn.execute(
        """
        SELECT rg.reactive_id, rg.group_id, rg.is_on_after,
               rg.brightness_after, rg.changed
        FROM reactive_group rg
        JOIN reactive r ON r.id = rg.reactive_id
        WHERE r.room_id = ? AND r.local_date >= ?
        """,
        (room_id, cutoff_date),
    ):
        rx_groups[rx_id][gid] = GroupReading(bool(is_on_after), b_after)
        rx_changed[rx_id] += int(changed)

    for rx_id, local_date, section, ts, lux_after in rx_rows:
        if rx_changed[rx_id] == 0:
            continue  # phantom event: nothing exceeded reactive_min_delta
        if local_date not in timeline:
            continue  # no heartbeats that day
        timeline[local_date][section].append(
            Sample(
                ts=ts,
                source="reactive",
                lux=lux_after,
                any_light_on=True,
                groups=rx_groups.get(rx_id, {}),
                weight=cfg.reactive_weight,
            )
        )

    for day in timeline.values():
        for section in SECTIONS:
            day[section].sort(key=lambda s: s.ts)

    return dict(timeline)


# ---------------------------------------------------------------------
# Weighting and stability
# ---------------------------------------------------------------------

def apply_post_reactive_boost(samples: list[Sample], cfg: LearningConfig) -> None:
    boost = list(cfg.post_reactive_boost)
    remaining = 0
    for s in samples:
        if s.source == "reactive":
            remaining = len(boost)
        elif remaining > 0:
            s.weight = boost[len(boost) - remaining]
            remaining -= 1


def mark_stability(samples: list[Sample], group_ids: list[str], cfg: LearningConfig) -> None:
    """A heartbeat reading is 'stable' when it matches the previous
    heartbeat - i.e. the light has settled rather than being mid-ramp.
    Reactive samples are always stable; the first heartbeat always is."""
    prev: Sample | None = None
    for s in samples:
        if s.source == "reactive":
            for g in s.groups.values():
                g.stable = True
            s.settled = True
        elif prev is None:
            for g in s.groups.values():
                g.stable = True
            s.settled = True
        else:
            for gid in group_ids:
                cur, old = s.groups.get(gid), prev.groups.get(gid)
                if cur is None:
                    continue
                cur.stable = bool(old is not None and cur.brightness == old.brightness)
            s.settled = all(
                s.groups[gid].stable for gid in group_ids if gid in s.groups
            )
        if s.source == "heartbeat":
            prev = s

    if not cfg.flags.per_group_stability:
        for s in samples:                      # legacy: all-or-nothing
            for g in s.groups.values():
                g.stable = s.settled


# ---------------------------------------------------------------------
# Daily statistics
# ---------------------------------------------------------------------

@dataclass
class SectionStats:
    lux_target: float | None
    total_weight: float
    sample_count: int
    reactive_count: int
    confidence: str
    group_brightness: dict[str, float | None]
    group_weight: dict[str, float]
    group_on_fraction: dict[str, float | None]


def compute_daily_stats(
    samples: list[Sample], group_ids: list[str], cfg: LearningConfig
) -> SectionStats | None:
    if not samples:
        return None

    apply_post_reactive_boost(samples, cfg)
    mark_stability(samples, group_ids, cfg)

    reactive_count = sum(1 for s in samples if s.source == "reactive")
    flags = cfg.flags

    # --- room-level lux -------------------------------------------------
    # A reading of 0 is the sensor's floor, not a measurement: excluded,
    # exactly as in the original pipeline.
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

    # --- per-group brightness ------------------------------------------
    g_sum = {g: 0.0 for g in group_ids}
    g_weight = {g: 0.0 for g in group_ids}
    g_on_weight = {g: 0.0 for g in group_ids}
    g_denominator = {g: 0.0 for g in group_ids}

    for s in samples:
        if not s.any_light_on:
            continue
        for gid in group_ids:
            reading = s.groups.get(gid)
            if reading is None:
                continue
            stable = reading.stable if flags.per_group_stability else s.settled
            if not stable:
                continue
            if flags.brightness_requires_measurable_lux and not (s.lux and s.lux > 0):
                continue

            g_denominator[gid] += s.weight
            if reading.is_on:
                g_on_weight[gid] += s.weight

            eligible = reading.is_on if flags.per_group_eligibility else True
            if eligible and reading.brightness is not None:
                g_sum[gid] += reading.brightness * s.weight
                g_weight[gid] += s.weight

    brightness = {
        g: (round(g_sum[g] / g_weight[g]) if g_weight[g] > 0 else None)
        for g in group_ids
    }
    on_fraction = {
        g: (round(g_on_weight[g] / g_denominator[g], 3) if g_denominator[g] > 0 else None)
        for g in group_ids
    }

    return SectionStats(
        lux_target=lux_target,
        total_weight=round(settled_weight, 1),
        sample_count=settled_count,
        reactive_count=reactive_count,
        confidence=cfg.confidence(settled_weight, reactive_count),
        group_brightness=brightness,
        group_weight=g_weight,
        group_on_fraction=on_fraction,
    )


# ---------------------------------------------------------------------
# Rolling almanac
# ---------------------------------------------------------------------

def build_almanac(
    daily: dict[str, dict[str, SectionStats | None]],
    group_ids: list[str],
    scene_config: dict[str, dict],
    cfg: LearningConfig,
) -> dict:
    """scene_config[section] carries lux_margin, max_step_pct,
    maintenance_enabled and groups[gid]['mode'] from the user config."""
    dates = sorted(daily)
    if not dates:
        return {"_meta": {"version": 3, "mode": "provisional", "days_analysed": 0}}

    most_recent = datetime.strptime(dates[-1], "%Y-%m-%d").date()

    acc = {
        s: {
            "lux_sum": 0.0, "lux_weight": 0.0,
            "g_sum": {g: 0.0 for g in group_ids},
            "g_weight": {g: 0.0 for g in group_ids},
            "on_sum": {g: 0.0 for g in group_ids},
            "on_weight": {g: 0.0 for g in group_ids},
            "days": 0, "high_days": 0, "trust": 0.0,
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

            for g in group_ids:
                if stats.group_brightness[g] is not None:
                    # Per-group divisor: a group only contributes weight
                    # on the days it was actually on.
                    a["g_sum"][g] += stats.group_brightness[g] * combined
                    a["g_weight"][g] += combined
                if stats.group_on_fraction[g] is not None:
                    a["on_sum"][g] += stats.group_on_fraction[g] * combined
                    a["on_weight"][g] += combined

            a["days"] += 1
            a["trust"] += combined
            if stats.confidence == "high":
                a["high_days"] += 1

    days_analysed = len(dates)
    mode = "learning" if days_analysed >= cfg.bootstrap_min_days else "bootstrap"
    delay = cfg.publish_delay_days if mode == "learning" else 0
    valid_from = (most_recent + timedelta(days=delay)).isoformat()

    almanac: dict = {
        "_meta": {
            "version": 3,
            "updated": dates[-1],
            "valid_from": valid_from,
            "days_analysed": days_analysed,
            "mode": mode,
        }
    }

    for section in SECTIONS:
        a = acc[section]
        sc = scene_config.get(section, {})
        if a["days"] == 0:
            almanac[section] = None
            continue

        entry: dict = {
            "lux_target": round(a["lux_sum"] / a["lux_weight"], 1) if a["lux_weight"] else None,
            "lux_margin": sc.get("lux_margin", 5),
            "max_step_pct": sc.get("max_step_pct", 0.04),
            "maintenance_enabled": sc.get(
                "maintenance_enabled", section not in ("night", "sleep")
            ),
            "days_contributing": a["days"],
            "high_confidence_days": a["high_days"],
            # Accumulated effective weight behind this section: recency x
            # confidence x per-day settled weight, summed over the window.
            # Surfaced so the UI can show trust approaching its thresholds
            # smoothly, rather than only the bucketed low/medium/high.
            "trust_weight": round(a["trust"], 1),
            "on_fraction": {},
        }

        for g in group_ids:
            mode_g = sc.get("groups", {}).get(g, {}).get("mode", "auto")
            if mode_g == "off":
                # Forced off in the app UI: permanent for this section.
                entry[g] = 0
                entry["on_fraction"][g] = 0.0
                continue
            entry[g] = round(a["g_sum"][g] / a["g_weight"][g]) if a["g_weight"][g] else None
            entry["on_fraction"][g] = (
                round(a["on_sum"][g] / a["on_weight"][g], 3) if a["on_weight"][g] else None
            )

        almanac[section] = entry

    return almanac


def analyse_room(
    conn: sqlite3.Connection,
    room_id: str,
    group_ids: list[str],
    scene_config: dict[str, dict],
    cfg: LearningConfig | None = None,
    today: date | None = None,
) -> dict:
    cfg = cfg or LearningConfig()
    today = today or date.today()
    cutoff = (today - timedelta(days=cfg.lookback_days)).isoformat()

    timeline = load_timeline(conn, room_id, group_ids, cutoff, cfg)
    daily: dict[str, dict[str, SectionStats | None]] = {}
    for d, sections in timeline.items():
        daily[d] = {
            s: compute_daily_stats(sections[s], group_ids, cfg) for s in SECTIONS
        }
    return build_almanac(daily, group_ids, scene_config, cfg)


if __name__ == "__main__":  # pragma: no cover
    import sys
    conn = sqlite3.connect(sys.argv[1])
    print(json.dumps(analyse_room(conn, sys.argv[2], sys.argv[3:], {}), indent=2))
