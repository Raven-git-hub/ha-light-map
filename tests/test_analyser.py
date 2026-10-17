"""The analyser port (P5.2a). Parity first: the predecessor's analyser
(`tests/reference/`, verbatim) and this one run over the same readings, under every
combination of `AnalyserFlags` and with the tuning changed, and a learning unit of one bulb
must reproduce the predecessor's per-group result exactly. Then what is new: pooling a
unit's bulbs, the per-bulb store's mapping (local days, occupancy, no-state readings,
switched-only bulbs, unrecorded sections), the three states, and the nightly `run` over a
config. Against fixtures in a temporary store; nothing here reaches HA."""

import contextlib
import csv
import io
import itertools
import json
import random
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.core import config
from app.core.store import Store
from app.modules.adaptive import analyser
from app.modules.adaptive.analyser import AnalyserFlags, LearningConfig
from tests import analyser_fixtures as fx
from tests.fakes import committed_config
from tests.reference import ha_adaptive_light_analyser as predecessor
from tools import compare_analyser

TODAY = date(2026, 6, 14)
SECTIONS = config.SECTIONS


def every_flag_combination():
    for values in itertools.product((True, False), repeat=3):
        yield dict(
            zip(
                (
                    "per_group_eligibility",
                    "per_group_stability",
                    "brightness_requires_measurable_lux",
                ),
                values,
                strict=True,
            )
        )


class StoreCase(unittest.TestCase):
    def setUp(self):
        # Thousands of fixture rows: durability is the store's tests' business, not these.
        fsync = mock.patch("app.core.store.os.fsync")
        fsync.start()
        self.addCleanup(fsync.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        self.store.open()
        self.addCleanup(self.store.close)

    def analyse(self, units, cfg=None, today=TODAY, presence_configured=True):
        return analyser.analyse_room(
            self.store,
            fx.ROOM,
            units,
            cfg or LearningConfig(),
            today,
            tz=fx.TZ,
            presence_configured=presence_configured,
        )


class ParityTest(StoreCase):
    """A unit of one bulb is the predecessor's group, to the last digit."""

    def assert_same(self, ours, theirs, bulbs):
        self.assertEqual(ours["_meta"]["mode"], theirs["_meta"]["mode"])
        self.assertEqual(ours["_meta"]["days_analysed"], theirs["_meta"]["days_analysed"])
        self.assertEqual(ours["_meta"].get("valid_from"), theirs["_meta"].get("valid_from"))
        for section in SECTIONS:
            mine, old = ours.get(section), theirs.get(section)
            if old is None:
                self.assertIsNone(mine, section)
                continue
            for key in ("lux_target", "days_contributing", "high_confidence_days", "trust_weight"):
                self.assertEqual(mine[key], old[key], f"{section} {key}")
            for bulb in bulbs:
                self.assertEqual(mine["units"][bulb], old[bulb], f"{section} {bulb}")
                self.assertEqual(
                    mine["on_fraction"][bulb], old["on_fraction"][bulb], f"{section} {bulb} on"
                )

    def load(self, fixture):
        """The same readings into this store and the predecessor's schema."""
        fx.to_store(fixture, self.store)
        self.conn = fx.to_predecessor(fixture)
        self.addCleanup(self.conn.close)

    def run_both(self, fixture, flags, **tuning):
        theirs = predecessor.analyse_room(
            self.conn,
            fx.ROOM,
            fixture.bulbs,
            fx.predecessor_scene_config(fixture.bulbs),
            predecessor.LearningConfig(flags=predecessor.AnalyserFlags(**flags), **tuning),
            TODAY,
        )
        ours = self.analyse(
            {b: [b] for b in fixture.bulbs},
            LearningConfig(flags=AnalyserFlags(**flags), **tuning),
        )
        return ours, theirs

    def test_one_bulb_units_reproduce_the_predecessor_under_every_flag_combination(self):
        fixture = fx.readings(1)
        self.load(fixture)
        for flags in every_flag_combination():
            with self.subTest(**flags):
                ours, theirs = self.run_both(fixture, flags)
                # Not vacuous: the window learned something in every section.
                self.assertTrue(all(theirs[s] for s in SECTIONS))
                self.assertEqual(theirs["_meta"]["mode"], "learning")
                self.assert_same(ours, theirs, fixture.bulbs)

    def test_across_seeds_and_tuning(self):
        tunings = [
            {},
            {"lookback_days": 5, "bootstrap_min_days": 9},  # bootstrap mode
            {"reactive_weight": 2.0, "post_reactive_boost": (4.0,), "publish_delay_days": 0},
            {
                "recency_weights": ((1, 5.0), (None, 0.25)),
                "high_weight_threshold": 60.0,
                "medium_weight_threshold": 20.0,
                "low_multiplier": 0.1,
            },
        ]
        for seed in (2, 3, 4):
            self.setUp()
            fixture = fx.readings(seed, off_brightness_zero=seed % 2 == 0)
            self.load(fixture)
            for tuning, flags in itertools.product(
                tunings, ({}, {"per_group_eligibility": False, "per_group_stability": False})
            ):
                with self.subTest(seed=seed, tuning=tuning, flags=flags):
                    ours, theirs = self.run_both(fixture, flags, **tuning)
                    self.assert_same(ours, theirs, fixture.bulbs)

    def test_across_the_spring_clock_change(self):
        # 29 March 2026: London's day of 23 hours, in the window. (The autumn change repeats
        # an hour of local times, which the predecessor's store ordered by its local text:
        # its own quirk, not compared here.)
        fixture = fx.readings(5, first=date(2026, 3, 20), days=12, skip_days=(4,))
        self.load(fixture)
        today = date(2026, 4, 1)
        for flags in every_flag_combination():
            with self.subTest(**flags):
                theirs = predecessor.analyse_room(
                    self.conn,
                    fx.ROOM,
                    fixture.bulbs,
                    fx.predecessor_scene_config(fixture.bulbs),
                    predecessor.LearningConfig(flags=predecessor.AnalyserFlags(**flags)),
                    today,
                )
                ours = self.analyse(
                    {b: [b] for b in fixture.bulbs},
                    LearningConfig(flags=AnalyserFlags(**flags)),
                    today=today,
                )
                self.assertEqual(theirs["_meta"]["days_analysed"], 11)
                self.assert_same(ours, theirs, fixture.bulbs)

    def test_the_maths_reaches_every_branch_in_the_fixtures(self):
        # The parity above only means something if the fixtures exercise what it compares.
        fixture = fx.readings(1)
        fx.to_store(fixture, self.store)
        timeline = analyser.load_timeline(
            self.store,
            fx.ROOM,
            fixture.bulbs,
            TODAY - timedelta(days=21),
            LearningConfig(),
            tz=fx.TZ,
            presence_configured=True,
        )
        samples = [s for day in timeline.values() for section in day.values() for s in section]
        reactive = [s for s in samples if s.source == "reactive"]
        self.assertTrue(reactive)
        self.assertTrue(any(s.lux == 0 for s in samples))
        daily = [
            analyser.compute_daily_stats(day[s], {b: [b] for b in fixture.bulbs}, LearningConfig())
            for day in timeline.values()
            for s in SECTIONS
        ]
        bands = {stats.confidence for stats in daily if stats}
        self.assertEqual(bands, {"low", "medium", "high"})
        self.assertTrue(any(s.weight in (3.0, 2.0, 1.5) for s in samples))  # the boost
        self.assertTrue(any(not s.settled for s in samples))  # a ramp
        heartbeats = {s.ts for s in samples if s.source == "heartbeat"}
        self.assertTrue(any(s.ts in heartbeats for s in reactive))  # at a heartbeat's instant
        self.assertTrue(  # a user turning a bulb off
            any(not r.is_on for s in reactive for r in s.bulbs.values())
        )


class DayCase(StoreCase):
    def day(self, rows, on=date(2026, 6, 13), section="day", lux=50.0):
        """Heartbeats ten minutes apart from 11:00, each `{bulb: brightness or None (off)}`."""
        for i, row in enumerate(rows):
            at = datetime.combine(on, time(11, 0), tzinfo=fx.TZ) + timedelta(minutes=10 * i)
            self.store.append_heartbeat(
                {"ts": at, "room_id": fx.ROOM, "section": section, "lux": lux, "occupancy": True},
                [
                    {"bulb_id": b, "is_on": level is not None, "brightness": level}
                    for b, level in row.items()
                ],
            )


class PoolingTest(DayCase):
    """A unit of several bulbs pools their readings into one target."""

    def test_a_unit_is_the_weighted_mean_over_its_bulbs_readings(self):
        # Steady: every reading stable after the first. a sits at 100 for 6 samples, b at
        # 200 for 4 and off for 2. Pooled over every eligible reading: (6x100 + 4x200) / 10.
        self.day([{"a": 100, "b": 200}] * 4 + [{"a": 100, "b": None}] * 2)
        result = self.analyse({"pair": ["a", "b"], "solo": ["a"]})
        self.assertEqual(result["day"]["units"], {"pair": 140, "solo": 100})
        # On for 10 of 11 settled readings: b's reading as it went off is mid-change.
        self.assertEqual(result["day"]["on_fraction"], {"pair": 0.909, "solo": 1.0})

    def test_it_is_not_the_mean_of_its_bulbs_means(self):
        # a on for 1 sample at 50, b on for all 10 at 250: the unit learns where its light
        # mostly was, (50 + 10 x 250) / 11 = 232, not (50 + 250) / 2.
        self.day([{"a": 50, "b": 250}] + [{"a": None, "b": 250}] * 9)
        self.assertEqual(self.analyse({"pair": ["a", "b"]})["day"]["units"], {"pair": 232})

    def test_one_bulb_ramping_doesnt_discard_the_others(self):
        # b ramps every sample (stable only in the first, as the first always is); a is
        # steady. Per bulb (the default), a's settled readings still count: (6 x 120 + 20) / 7.
        # Row-level (legacy), every row after the first is thrown away.
        self.day([{"a": 120, "b": 10 * i + 20} for i in range(6)])
        self.assertEqual(self.analyse({"pair": ["a", "b"]})["day"]["units"]["pair"], 106)
        legacy = LearningConfig(flags=AnalyserFlags(per_group_stability=False))
        # Only the first sample is settled: (120 + 20) / 2.
        self.assertEqual(self.analyse({"pair": ["a", "b"]}, legacy)["day"]["units"]["pair"], 70)

    def test_units_can_be_redrawn_over_the_same_history(self):
        self.day([{"a": 100, "b": 200, "c": 60}] * 6)
        first = self.analyse({"ab": ["a", "b"], "c": ["c"]})["day"]["units"]
        second = self.analyse({"a": ["a"], "bc": ["b", "c"]})["day"]["units"]
        self.assertEqual(first, {"ab": 150, "c": 60})
        self.assertEqual(second, {"a": 100, "bc": 130})

    def test_a_bulb_outside_every_unit_is_outside_the_learner(self):
        # A switched-only lamp (in no unit) on alone opens no sample to the lux target and
        # settles none; a bulb in a unit, off, reads as off.
        self.day([{"a": None, "lamp": 200}] * 6)
        self.assertIsNone(self.analyse({"a": ["a"]})["day"])
        self.setUp()
        self.day([{"a": None, "lamp": 200}] * 3 + [{"a": 90, "lamp": 200}] * 3, lux=80.0)
        result = self.analyse({"a": ["a"]})
        self.assertEqual(result["day"]["units"], {"a": 90})
        self.assertEqual(result["day"]["lux_target"], 80.0)


class StoreMappingTest(StoreCase):
    """What the predecessor's store recorded and this one derives."""

    def beat(self, at, *, section="day", occupancy=True, readings=None, lux=40.0):
        self.store.append_heartbeat(
            {"ts": at, "room_id": fx.ROOM, "section": section, "lux": lux, "occupancy": occupancy},
            [
                {"bulb_id": b, "is_on": on, "brightness": level}
                for b, (on, level) in (readings or {"a": (True, 100)}).items()
            ],
        )

    def test_a_samples_day_is_its_date_in_has_time_zone(self):
        # 00:30 BST on the 13th is 23:30 UTC on the 12th, as the store keeps it: a sleep
        # sample of the 13th. 23:50 BST on the 12th is a sample of the 12th.
        self.beat(datetime(2026, 6, 13, 0, 30, tzinfo=fx.TZ), section="sleep")
        self.beat(datetime(2026, 6, 12, 23, 50, tzinfo=fx.TZ), section="sleep")
        self.assertEqual(
            [r["ts"][:16] for r in self.store.query("SELECT ts FROM heartbeat ORDER BY ts")],
            ["2026-06-12T22:50", "2026-06-12T23:30"],
        )

        def days(cutoff):
            timeline = analyser.load_timeline(
                self.store,
                fx.ROOM,
                ["a"],
                cutoff,
                LearningConfig(),
                tz=fx.TZ,
                presence_configured=True,
            )
            return {d: len(sections["sleep"]) for d, sections in timeline.items()}

        self.assertEqual(days(date(2026, 6, 1)), {"2026-06-12": 1, "2026-06-13": 1})
        # The lookback's cutoff is a local day too.
        self.assertEqual(days(date(2026, 6, 13)), {"2026-06-13": 1})
        self.assertEqual(days(date(2026, 6, 14)), {})

    def test_occupancy(self):
        at = datetime(2026, 6, 13, 12, 0, tzinfo=fx.TZ)
        self.beat(at, occupancy=True)
        self.beat(at + timedelta(minutes=10), occupancy=False)
        self.beat(at + timedelta(minutes=20), occupancy=None)

        def samples(presence_configured):
            timeline = analyser.load_timeline(
                self.store,
                fx.ROOM,
                ["a"],
                date(2026, 6, 1),
                LearningConfig(),
                tz=fx.TZ,
                presence_configured=presence_configured,
            )
            return len(timeline["2026-06-13"]["day"])

        # With no presence sensor the room is always occupied (the predecessor's rule); with
        # one, a heartbeat that couldn't read it is left out, as the predecessor read it.
        self.assertEqual(samples(presence_configured=False), 2)
        self.assertEqual(samples(presence_configured=True), 1)

    def test_a_sensor_added_later_doesnt_rewrite_history(self):
        # Two weeks with no presence sensor (occupancy null, always occupied), then one is
        # added. Judged by today's config, every older row would read "unoccupied" and the
        # room would fall back to bootstrap on a day of data.
        at = datetime(2026, 6, 13, 12, 0, tzinfo=fx.TZ)

        def beat(minute, occupancy, configured):
            self.store.append_heartbeat(
                {
                    "ts": at + timedelta(minutes=minute),
                    "room_id": fx.ROOM,
                    "section": "day",
                    "lux": 40.0,
                    "occupancy": occupancy,
                    "presence_configured": configured,
                },
                [{"bulb_id": "a", "is_on": True, "brightness": 100}],
            )

        beat(0, None, False)  # no sensor then: occupied
        beat(10, None, True)  # a sensor, unreadable: not occupied
        beat(20, None, None)  # recorded before the observer noted it: as the room is now

        def samples(now_configured):
            timeline = analyser.load_timeline(
                self.store,
                fx.ROOM,
                ["a"],
                date(2026, 6, 1),
                LearningConfig(),
                tz=fx.TZ,
                presence_configured=now_configured,
            )
            return [s.ts[11:16] for s in timeline["2026-06-13"]["day"]]

        self.assertEqual(samples(now_configured=True), ["11:00"])
        self.assertEqual(samples(now_configured=False), ["11:00", "11:20"])

    def test_a_reading_with_no_state_is_no_reading(self):
        at = datetime(2026, 6, 13, 12, 0, tzinfo=fx.TZ)
        for i in range(4):
            self.beat(
                at + timedelta(minutes=10 * i), readings={"a": (True, 100), "b": (None, None)}
            )
        result = self.analyse({"a": ["a"], "b": ["b"]})
        # b was never seen off: no on-fraction, no brightness, rather than "off all day".
        self.assertEqual(result["day"]["units"], {"a": 100, "b": None})
        self.assertEqual(result["day"]["on_fraction"], {"a": 1.0, "b": None})

    def test_an_observation_with_no_section_is_skipped(self):
        at = datetime(2026, 6, 13, 12, 0, tzinfo=fx.TZ)
        self.beat(at, section=None)
        self.beat(at + timedelta(minutes=10), section="dusk")
        self.assertEqual(self.analyse({"a": ["a"]})["_meta"]["days_analysed"], 0)

    def test_a_reactive_event_counts_when_it_changed_an_adaptive_bulb(self):
        at = datetime(2026, 6, 13, 12, 0, tzinfo=fx.TZ)
        for i in range(3):
            self.beat(
                at + timedelta(minutes=10 * i), readings={"a": (True, 100), "lamp": (True, 50)}
            )

        def event(minute, changed):
            self.store.append_reactive_event(
                {
                    "ts": at + timedelta(minutes=minute),
                    "room_id": fx.ROOM,
                    "section": "day",
                    "window_seconds": 120,
                    "lux_after": 40.0,
                },
                [
                    {
                        "bulb_id": "a",
                        "is_on_after": True,
                        "brightness_after": 200,
                        "changed": changed == "a",
                    },
                    {
                        "bulb_id": "lamp",
                        "is_on_after": True,
                        "brightness_after": 90,
                        "changed": changed == "lamp",
                    },
                ],
            )

        event(3, "lamp")  # only the switched-only lamp: nothing the learner learns changed
        self.assertEqual(self.analyse({"a": ["a"]})["day"]["units"]["a"], 100)
        event(13, "a")
        result = self.analyse({"a": ["a"]})
        # Two heartbeats, the event (weight 5), and the heartbeat after it, boosted (3):
        # (2 x 100 + 5 x 200 + 3 x 100) / 10.
        self.assertEqual(result["day"]["units"]["a"], 150)


class StatesAndWeightsTest(DayCase):
    """The three states, recency, confidence and reactive weighting, by hand."""

    def test_the_three_states(self):
        self.assertEqual(
            self.analyse({"a": ["a"]})["_meta"], {"mode": "provisional", "days_analysed": 0}
        )
        for d in range(6):
            self.day([{"a": 100}] * 3, on=TODAY - timedelta(days=d + 1))
        meta = self.analyse({"a": ["a"]})["_meta"]
        # Bootstrap: valid from its last day of data, so a fresh room comes alive at once.
        self.assertEqual((meta["mode"], meta["days_analysed"]), ("bootstrap", 6))
        self.assertEqual(meta["valid_from"], (TODAY - timedelta(days=1)).isoformat())
        self.day([{"a": 100}] * 3, on=TODAY - timedelta(days=7))
        meta = self.analyse({"a": ["a"]})["_meta"]
        # Learning: valid two days after its last day of data.
        self.assertEqual((meta["mode"], meta["days_analysed"]), ("learning", 7))
        self.assertEqual(meta["valid_from"], (TODAY + timedelta(days=1)).isoformat())

    def test_recency(self):
        # Two days of 3 samples each (low confidence alike): 10 days old (weight 1) at 100,
        # yesterday (weight 3) at 200: (100 x 1 + 200 x 3) / 4.
        self.day([{"a": 100}] * 3, on=TODAY - timedelta(days=11))
        self.day([{"a": 200}] * 3, on=TODAY - timedelta(days=1))
        self.assertEqual(self.analyse({"a": ["a"]})["day"]["units"]["a"], 175)
        # Past the lookback, a day doesn't count at all.
        self.setUp()
        self.day([{"a": 100}] * 3, on=TODAY - timedelta(days=30))
        self.day([{"a": 200}] * 3, on=TODAY - timedelta(days=1))
        self.assertEqual(self.analyse({"a": ["a"]})["day"]["units"]["a"], 200)

    def test_confidence_bands_and_multipliers(self):
        cfg = LearningConfig()
        self.assertEqual(cfg.confidence(31, 0), "high")
        self.assertEqual(cfg.confidence(30, 0), "medium")
        self.assertEqual(cfg.confidence(10, 0), "medium")
        self.assertEqual(cfg.confidence(9.9, 0), "low")
        self.assertEqual(cfg.confidence(1, 1), "high")  # a reactive event: high
        # Same age (both yesterday's band: 3 days), different confidence: 35 samples at 100
        # (high: x1.5 x 35) and 5 at 200 (low: x0.5 x 5), on two different days.
        self.day([{"a": 100}] * 35, on=TODAY - timedelta(days=1))
        self.day([{"a": 200}] * 5, on=TODAY - timedelta(days=2))
        expected = round((100 * 1.5 * 35 + 200 * 0.5 * 5) / (1.5 * 35 + 0.5 * 5))
        result = self.analyse({"a": ["a"]})["day"]
        self.assertEqual(result["units"]["a"], expected)
        self.assertEqual(result["high_confidence_days"], 1)
        self.assertEqual(result["trust_weight"], round(3 * (1.5 * 35 + 0.5 * 5), 1))

    def test_the_tuning_comes_from_the_config(self):
        cfg = LearningConfig.from_config(
            {
                "lookback_days": 10,
                "post_reactive_boost": [2],
                "recency_weights": [
                    {"max_age_days": 1, "weight": 4},
                    {"max_age_days": None, "weight": 1},
                ],
                "confidence": {"high_multiplier": 2},
            },
            AnalyserFlags(per_group_eligibility=False),
        )
        self.assertEqual(cfg.lookback_days, 10)
        self.assertEqual(cfg.post_reactive_boost, (2,))
        self.assertEqual(cfg.recency_weights, ((1, 4), (None, 1)))
        self.assertEqual((cfg.high_multiplier, cfg.low_multiplier), (2, 0.5))
        self.assertFalse(cfg.flags.per_group_eligibility)
        self.assertEqual(LearningConfig.from_config({}), LearningConfig())
        self.assertEqual(LearningConfig.from_config(None).flags, AnalyserFlags())
        # The defaults are the predecessor's.
        self.assertEqual(
            {k: v for k, v in vars(LearningConfig()).items() if k != "flags"},
            {k: v for k, v in vars(predecessor.LearningConfig()).items() if k != "flags"},
        )
        self.assertEqual(vars(AnalyserFlags()), vars(predecessor.AnalyserFlags()))


def night_heartbeats(store, now, *, room="living_room", days=3):
    """The fixture house's living room on the `days` nights before `now`: the chandelier at
    100, 200 and off, the reading lamp at 60, no presence sensor."""
    levels = {"chandelier_1": 100, "chandelier_2": 200, "chandelier_3": None, "reading_lamp": 60}
    for d in range(days):
        day = now.date() - timedelta(days=d + 1)
        for i in range(6):
            at = datetime.combine(day, time(20, 30), tzinfo=fx.TZ) + timedelta(minutes=10 * i)
            store.append_heartbeat(
                {"ts": at, "room_id": room, "section": "night", "lux": 12.0, "occupancy": None},
                [
                    {"bulb_id": b, "is_on": v is not None, "brightness": v}
                    for b, v in levels.items()
                ],
            )


class RunTest(StoreCase):
    """The nightly pass over a config."""

    NOW = datetime(2026, 6, 14, 0, 15, tzinfo=fx.TZ)

    def setUp(self):
        super().setUp()
        self.doc = config.loads(committed_config())

    def heartbeats(self, room="living_room"):
        night_heartbeats(self.store, self.NOW, room=room)

    def test_one_row_per_unit_per_section(self):
        self.heartbeats()
        rows = analyser.run(self.store, self.doc, now=self.NOW)
        self.assertEqual(len(rows), 2 * 6)
        self.assertEqual(
            [(r["section"], r["learning_unit_id"]) for r in rows],
            [(s, u) for s in SECTIONS for u in ("chandelier", "reading")],
        )
        night = {r["learning_unit_id"]: r for r in rows if r["section"] == "night"}
        # The chandelier pools its three bulbs (one off): (100 + 200) / 2.
        self.assertEqual(night["chandelier"]["brightness"], 150)
        self.assertEqual(night["chandelier"]["on_fraction"], 0.667)
        self.assertEqual(night["reading"]["brightness"], 60)
        for row in night.values():
            self.assertEqual(row["lux_target"], 12.0)
            self.assertEqual((row["mode"], row["days_analysed"]), ("bootstrap", 3))
            self.assertEqual(row["valid_from"], "2026-06-13")
            self.assertEqual(row["ts"], self.NOW)
            # The knobs in force when the pass ran, for the record.
            self.assertEqual((row["lux_margin"], row["max_step_pct"]), (5, 0.04))
            self.assertIs(row["maintenance_enabled"], False)
        day = {r["learning_unit_id"]: r for r in rows if r["section"] == "day"}
        self.assertEqual(
            (
                day["reading"]["brightness"],
                day["reading"]["lux_target"],
                day["reading"]["days_contributing"],
            ),
            (None, None, 0),
        )
        # Rows the store takes as they are.
        self.store.append_almanac(rows)
        self.assertEqual(len(self.store.query("SELECT * FROM almanac")), 12)

    def test_a_unit_held_off_keeps_its_learned_value(self):
        self.heartbeats()
        room = self.doc["rooms"][0]
        room["adaptive_config"]["scenes"]["night"]["units"]["reading"]["mode"] = "off"
        night = {
            r["learning_unit_id"]: r
            for r in analyser.run(self.store, self.doc, now=self.NOW)
            if r["section"] == "night"
        }
        # The mode is the publish's to resolve: the target waits for the unit's return.
        self.assertEqual(night["reading"]["brightness"], 60)

    def test_nothing_for_a_room_with_no_data_or_no_unit(self):
        self.assertEqual(analyser.run(self.store, self.doc, now=self.NOW), [])
        self.heartbeats()
        block = self.doc["rooms"][0]["adaptive_config"]
        block["adaptive_bulbs"], block["learning_units"] = [], []
        block["scenes"] = config.normalize_scenes(block["scenes"], [])
        self.assertEqual(analyser.run(self.store, self.doc, now=self.NOW), [])

    def test_now_needs_a_time_zone(self):
        with self.assertRaises(ValueError):
            analyser.run(self.store, self.doc, now=datetime(2026, 6, 14, 0, 15))

    def test_other_rooms_readings_are_not_this_rooms(self):
        self.heartbeats(room="kitchen")
        self.assertEqual(analyser.run(self.store, self.doc, now=self.NOW), [])


class CompareToolTest(StoreCase):
    """`tools/compare_analyser.py`: legacy vs current on identical input."""

    def legacy_csv(self, directory, seed=7, days=10):
        """The predecessor's wide heartbeat CSV: four groups, 0 for off, naive local times."""
        rng = random.Random(seed)
        path = Path(directory) / "heartbeat.csv"
        level = {g: rng.randint(30, 220) for g in compare_analyser.GROUPS}
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["timestamp", "ambient_lux", "motion_detected", "any_light_on"]
                + [f"group_{i}_brightness" for i in range(1, 5)]
            )
            moment = datetime(2026, 6, 1)
            while moment < datetime(2026, 6, 1) + timedelta(days=days):
                values = []
                for g in compare_analyser.GROUPS:
                    if rng.random() < 0.2:
                        level[g] = rng.choice([0, rng.randint(1, 255)])
                    values.append(level[g])
                writer.writerow(
                    [
                        moment.isoformat(),
                        rng.choice([0, 0, 12.5, 80, 240]),
                        rng.random() < 0.9,
                        any(v > 0 for v in values),
                        *values,
                    ]
                )
                moment += timedelta(minutes=10)
        return path

    def predecessor_from_csv(self, path):
        """The CSV loaded the way the predecessor's own compare tool loaded it."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(fx.PREDECESSOR_DDL)
        with path.open(newline="") as handle:
            lines = list(csv.DictReader(handle))
        for r in lines:
            ts = r["timestamp"]
            cur = conn.execute(
                "INSERT INTO heartbeat (room_id, ts, local_date, section, ambient_lux, occupied, "
                "any_light_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    compare_analyser.ROOM,
                    ts,
                    ts[:10],
                    compare_analyser.section_for(ts[11:16]),
                    float(r["ambient_lux"]),
                    1 if r["motion_detected"] == "True" else 0,
                    1 if r["any_light_on"] == "True" else 0,
                ),
            )
            for i, g in enumerate(compare_analyser.GROUPS, start=1):
                b = int(r[f"group_{i}_brightness"])
                conn.execute(
                    "INSERT INTO heartbeat_group VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, g, int(b > 0), b),
                )
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    def test_legacy_and_current_reproduce_the_predecessors_tool(self):
        today = date(2026, 6, 12)
        path = self.legacy_csv(self.directory.name)
        self.assertEqual(compare_analyser.load_legacy_csv(path, self.store, tz=fx.TZ), 6 * 24 * 10)
        units = {g: [g] for g in compare_analyser.GROUPS}
        rows = compare_analyser.compare(
            self.store, [(compare_analyser.ROOM, units, True)], today=today, tz=fx.TZ
        )
        conn = self.predecessor_from_csv(path)
        scene = fx.predecessor_scene_config(compare_analyser.GROUPS)
        legacy, current = (
            predecessor.analyse_room(
                conn,
                compare_analyser.ROOM,
                list(compare_analyser.GROUPS),
                scene,
                predecessor.LearningConfig(flags=flags),
                today,
            )
            for flags in (
                predecessor.AnalyserFlags(
                    per_group_eligibility=False,
                    per_group_stability=False,
                    brightness_requires_measurable_lux=True,
                ),
                predecessor.AnalyserFlags(),
            )
        )
        differ = 0
        for row in rows:
            old, new = legacy[row["section"]], current[row["section"]]
            key = row["unit"] or "lux_target"
            self.assertEqual(row["a"], old[key], row)
            self.assertEqual(row["b"], new[key], row)
            if row["unit"]:
                self.assertEqual(row["on_fraction"], new["on_fraction"][row["unit"]])
            differ += row["a"] != row["b"]
        self.assertTrue(differ)  # the flags change something, so the diff shows something

    def test_the_same_flags_twice_differ_nowhere(self):
        path = self.legacy_csv(self.directory.name, days=3)
        compare_analyser.load_legacy_csv(path, self.store, tz=fx.TZ)
        rows = compare_analyser.compare(
            self.store,
            [(compare_analyser.ROOM, {g: [g] for g in compare_analyser.GROUPS}, True)],
            today=date(2026, 6, 4),
            tz=fx.TZ,
            a=AnalyserFlags(),
        )
        self.assertTrue(rows)
        self.assertTrue(all(row["a"] == row["b"] for row in rows))

    def test_the_tool_on_a_csv_and_on_a_config(self):
        path = self.legacy_csv(self.directory.name, days=2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                compare_analyser.main(["--legacy-csv", str(path), "--today", "2026-06-03"]), 0
            )
        self.assertIn("LUX TARGET", out.getvalue())
        self.assertIn("tv_light", out.getvalue())
        # A config's store is read from a copy of its archive: the live one is never opened.
        data = Path(self.directory.name) / "house"
        live = Store(data)
        live.open()
        night_heartbeats(live, RunTest.NOW)
        live.close()
        (data / "light_map.db").unlink()
        config_path = data / "config.json"
        config_path.write_text(json.dumps(committed_config()))
        before = sorted((p.name, p.stat().st_mtime_ns) for p in data.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertLogs("app.core.store", "WARNING") as logs:
            self.assertEqual(
                compare_analyser.main(
                    ["--config", str(config_path), "--tz", "Europe/London", "--today", "2026-06-14"]
                ),
                0,
            )
        self.assertIn("rebuilt the query layer from the archive", logs.output[0])  # the copy
        # Without HA's time zone it can't say which day a reading belongs to.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            compare_analyser.main(["--config", str(config_path)])
        self.assertIn("living_room", out.getvalue())
        self.assertIn("chandelier", out.getvalue())
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in data.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
