"""The scheduler port (P5.2a): trigger resolution, priorities, collisions (collapse and
clamp, order and gap), sun events that don't happen, the level-triggered section in force,
and `tools/sweep.py`'s year of days. Pure computation: nothing here reaches HA."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core import config
from app.modules.adaptive import scheduler
from app.modules.adaptive.scheduler import (
    AstralSunProvider,
    Boundary,
    FixedSunProvider,
    SunUnavailable,
)
from tests.fakes import committed_config, schedule
from tools import sweep

DAY = date(2026, 4, 10)
LONDON = {"latitude": 51.51, "longitude": -0.13, "elevation": 0, "time_zone": "Europe/London"}
UTC = timezone.utc


def at(hh, mm, on=DAY):
    return datetime.combine(on, time(hh, mm))


def mild():
    """Sunrise 06:30, sunset 19:30: every standard section runs."""
    return FixedSunProvider(time(6, 30), time(19, 30))


def run(profile, provider, on=DAY):
    return {b.section: b for b in scheduler.compute_day(profile, provider, on)}


def with_section(profile, section_id, **fields):
    profile = copy.deepcopy(profile)
    section = next(s for s in profile["sections"] if s["id"] == section_id)
    section.update(fields)
    return profile


class TriggerTest(unittest.TestCase):
    SUN = {"sunrise": at(6, 30), "sunset": at(19, 30)}

    def test_a_clock_time(self):
        self.assertEqual(
            scheduler.resolve_trigger({"type": "clock", "time": "20:30"}, self.SUN, DAY),
            at(20, 30),
        )

    def test_a_sun_event_with_an_offset(self):
        for offset, expected in ((0, at(19, 30)), (-180, at(16, 30)), (45, at(20, 15))):
            trigger = {"type": "sun", "event": "sunset", "offset_minutes": offset}
            self.assertEqual(scheduler.resolve_trigger(trigger, self.SUN, DAY), expected)
        # The offset defaults to 0.
        trigger = {"type": "sun", "event": "sunrise"}
        self.assertEqual(scheduler.resolve_trigger(trigger, self.SUN, DAY), at(6, 30))

    def test_earliest_and_latest(self):
        parts = [{"type": "sun", "event": "sunrise"}, {"type": "clock", "time": "05:30"}]
        earliest = {"type": "earliest", "of": parts}
        latest = {"type": "latest", "of": parts}
        self.assertEqual(scheduler.resolve_trigger(earliest, self.SUN, DAY), at(5, 30))
        self.assertEqual(scheduler.resolve_trigger(latest, self.SUN, DAY), at(6, 30))
        # A component that doesn't happen is left out; with none left, the trigger can't fire.
        self.assertEqual(scheduler.resolve_trigger(latest, {}, DAY), at(5, 30))
        sun_only = {"type": "earliest", "of": [parts[0], {"type": "sun", "event": "sunset"}]}
        with self.assertRaises(SunUnavailable):
            scheduler.resolve_trigger(sun_only, {}, DAY)
        nested = {"type": "latest", "of": [earliest, {"type": "clock", "time": "06:00"}]}
        self.assertEqual(scheduler.resolve_trigger(nested, self.SUN, DAY), at(6, 0))

    def test_an_unknown_trigger_is_an_error(self):
        with self.assertRaises(ValueError):
            scheduler.resolve_trigger({"type": "moonrise"}, self.SUN, DAY)

    def test_default_priority(self):
        self.assertEqual(scheduler.default_priority({"type": "clock", "time": "05:30"}), 100)
        self.assertEqual(scheduler.default_priority({"type": "sun", "event": "sunset"}), 50)
        composite = {
            "type": "earliest",
            "of": [{"type": "sun", "event": "sunrise"}, {"type": "clock", "time": "05:30"}],
        }
        # As firm as its firmest component: a clock floor isn't purely sun-relative.
        self.assertEqual(scheduler.default_priority(composite), 100)
        sun_only = {"type": "latest", "of": [{"type": "sun", "event": "sunrise"}] * 2}
        self.assertEqual(scheduler.default_priority(sun_only), 50)


class ComputeDayTest(unittest.TestCase):
    def test_a_mild_day_runs_all_six_in_order(self):
        boundaries = scheduler.compute_day(schedule(), mild(), DAY)
        self.assertEqual([b.section for b in boundaries], list(config.SECTIONS))
        self.assertTrue(all(b.ran for b in boundaries))
        self.assertEqual(
            [b.planned for b in boundaries],
            [at(5, 30), at(9, 30), at(16, 30), at(19, 30), at(20, 30), at(22, 0)],
        )
        # Each ends where the next starts; the day's last runs on into tomorrow.
        self.assertEqual([b.ends for b in boundaries[:-1]], [b.planned for b in boundaries[1:]])
        self.assertIsNone(boundaries[-1].ends)
        self.assertEqual(boundaries[0].name, "sunrise")  # display names arrive with P5.2b

    def test_an_order_inversion_collapses_the_lower_priority_under_either_policy(self):
        # A UK midsummer: sunset at 21:21 lands after night at 20:30.
        provider = FixedSunProvider(time(4, 43), time(21, 21))
        for policy in ("collapse", "clamp"):
            with self.subTest(policy):
                found = run({**schedule(), "collision_policy": policy}, provider)
                self.assertFalse(found["sunset"].ran)
                self.assertEqual(
                    found["sunset"].reason,
                    "order conflict: night (priority 100) takes precedence over sunset "
                    "(priority 50)",
                )
                self.assertTrue(found["night"].ran)
                self.assertEqual(found["afternoon"].ends, found["night"].planned)

    def test_a_gap_collapses_or_clamps(self):
        # Sunset at 20:10 leaves night at 20:30 twenty minutes after it: too soon.
        provider = FixedSunProvider(time(6, 30), time(20, 10))
        found = run(schedule(), provider)
        self.assertFalse(found["sunset"].ran)
        self.assertIn("gap conflict: night (priority 100)", found["sunset"].reason)
        self.assertEqual(found["night"].planned, at(20, 30))
        # Clamp keeps every section, moving the later one on: night starts 30 minutes after
        # sunset, at a time nobody asked for, and says so.
        found = run({**schedule(), "collision_policy": "clamp"}, provider)
        self.assertTrue(all(b.ran for b in found.values()))
        self.assertEqual(found["sunset"].planned, at(20, 10))
        self.assertEqual(found["night"].planned, at(20, 40))
        self.assertEqual(found["night"].reason, "clamped to 0:30:00 after sunset")
        self.assertEqual(found["sunset"].ends, at(20, 40))

    def test_the_minimum_is_the_profiles(self):
        provider = FixedSunProvider(time(6, 30), time(20, 10))
        found = run({**schedule(), "min_section_minutes": 15}, provider)
        self.assertTrue(found["sunset"].ran)
        found = run({**schedule(), "min_section_minutes": 0}, FixedSunProvider(None, time(20, 30)))
        self.assertTrue(found["sunset"].ran and found["night"].ran)

    def test_a_tie_goes_to_the_section_earlier_in_canonical_order(self):
        # Afternoon (sunset - 3 h) and day (sunrise + 3 h) at equal priority, 20 minutes apart.
        provider = FixedSunProvider(time(9, 0), time(15, 20))
        found = run(schedule(), provider)
        self.assertTrue(found["day"].ran)
        self.assertFalse(found["afternoon"].ran)
        self.assertIn(
            "day (priority 50) takes precedence over afternoon", found["afternoon"].reason
        )

    def test_an_explicit_priority_wins(self):
        provider = FixedSunProvider(time(4, 43), time(21, 21))
        found = run(with_section(schedule(), "sunset", priority=200), provider)
        self.assertTrue(found["sunset"].ran)
        self.assertFalse(found["night"].ran)
        self.assertIn("sunset (priority 200) takes precedence over night", found["night"].reason)

    def test_a_sun_event_that_doesnt_happen_collapses_its_sections(self):
        found = run(schedule(), FixedSunProvider(None, None))
        for section in ("day", "afternoon", "sunset"):
            self.assertFalse(found[section].ran)
            self.assertIsNone(found[section].planned)
            self.assertIn(f"on {DAY}", found[section].reason)
        # Sunrise has a clock floor, so it still starts at 05:30.
        self.assertEqual(found["sunrise"].planned, at(5, 30))
        boundaries = scheduler.compute_day(schedule(), FixedSunProvider(None, None), DAY)
        # Collapsed ones with no time sort at the end of the day.
        self.assertEqual([b.section for b in boundaries][:3], ["sunrise", "night", "sleep"])

    def test_an_offset_never_leaks_into_another_day(self):
        late = with_section(
            schedule(), "sleep", trigger={"type": "sun", "event": "sunset", "offset_minutes": 600}
        )
        self.assertEqual(run(late, mild())["sleep"].planned, at(23, 59))
        early = with_section(
            schedule(),
            "sunrise",
            trigger={"type": "sun", "event": "sunrise", "offset_minutes": -500},
        )
        self.assertEqual(run(early, mild())["sunrise"].planned, at(0, 0))

    def test_the_collision_policy_works_on_copies(self):
        candidates = [
            Boundary("sunset", "sunset", at(20, 10), 50),
            Boundary("night", "night", at(20, 30), 100),
        ]
        before = copy.deepcopy(candidates)
        result = scheduler.apply_collision_policy(candidates, policy="clamp")
        self.assertEqual(candidates, before)
        self.assertEqual([b.planned for b in result], [at(20, 10), at(20, 40)])
        # One already collapsed stays collapsed.
        gone = Boundary("day", "day", None, 50, outcome="collapsed", reason="no sunrise")
        result = scheduler.apply_collision_policy([gone, *candidates])
        self.assertEqual([b.section for b in result if b.ran], ["night"])
        self.assertEqual(result[-1].section, "day")

    def test_a_real_place_and_time_zone(self):
        midsummer = {
            b.section: b
            for b in scheduler.compute_boundaries(schedule(), date(2026, 6, 21), LONDON)
        }
        self.assertEqual(midsummer["sunrise"].planned.strftime("%H:%M"), "04:43")
        self.assertEqual(midsummer["sunrise"].planned.utcoffset(), timedelta(hours=1))
        self.assertFalse(midsummer["sunset"].ran)
        midwinter = {
            b.section: b
            for b in scheduler.compute_boundaries(schedule(), date(2026, 12, 21), LONDON)
        }
        self.assertEqual(midwinter["sunrise"].planned.strftime("%H:%M"), "05:30")
        self.assertTrue(all(b.ran for b in midwinter.values()))
        self.assertEqual(midwinter["night"].planned.utcoffset(), timedelta(0))

    def test_a_location_must_say_where(self):
        for location in ({}, {**LONDON, "time_zone": "Mars/Olympus"}, {**LONDON, "latitude": None}):
            with self.subTest(location), self.assertRaises(ValueError):
                scheduler.provider_for(location)
        self.assertEqual(scheduler.provider_for({**LONDON, "elevation": None}).elevation, 0)


class SectionAtTest(unittest.TestCase):
    """Level-triggered: the section in force is a function of the time."""

    def test_across_a_day(self):
        provider = mild()
        starts = [b.planned for b in scheduler.compute_day(schedule(), provider, DAY)]
        moment = at(0, 0)
        while moment < at(0, 0) + timedelta(days=1):
            section, started = scheduler.active_section_at(schedule(), provider, moment)
            past = [s for s in starts if s <= moment]
            if past:
                self.assertEqual(started, past[-1], moment)
            else:
                # Before the day's first boundary, yesterday's last is still in force.
                self.assertEqual((section, started), ("sleep", at(22, 0, DAY - timedelta(days=1))))
            moment += timedelta(minutes=5)

    def test_a_collapsed_section_is_never_in_force(self):
        provider = FixedSunProvider(time(4, 43), time(21, 21))
        seen = {
            scheduler.active_section_at(schedule(), provider, at(0, 0) + timedelta(minutes=m))[0]
            for m in range(0, 24 * 60, 5)
        }
        self.assertNotIn("sunset", seen)
        self.assertEqual(seen, set(config.SECTIONS) - {"sunset"})

    def test_a_day_with_nothing_looks_further_back(self):
        sun_only = copy.deepcopy(schedule())
        for section in sun_only["sections"]:
            section["trigger"] = {"type": "sun", "event": "sunset"}
        with self.assertRaises(SunUnavailable):
            scheduler.active_section_at(sun_only, FixedSunProvider(None, None), at(12, 0))

    def test_at_a_place_with_a_time_in_any_zone(self):
        # 23:30 UTC on midsummer's day is 00:30 the next day in London: still sleep.
        when = datetime(2026, 6, 21, 23, 30, tzinfo=UTC)
        self.assertEqual(scheduler.section_at(schedule(), when, LONDON), "sleep")
        self.assertEqual(
            scheduler.section_at(schedule(), datetime(2026, 6, 22, 12, 0, tzinfo=UTC), LONDON),
            "day",
        )
        # The clocks go forward at 01:00 GMT on 29 March 2026 (sunset 19:28 BST, night 20:30
        # BST, which is 19:30 UTC): the sections still follow local time.
        for hour, expected in (
            (0, "sleep"),
            (1, "sleep"),
            (5, "sunrise"),
            (18, "sunset"),
            (19, "night"),
        ):
            when = datetime(2026, 3, 29, hour, 30, tzinfo=UTC)
            self.assertEqual(scheduler.section_at(schedule(), when, LONDON), expected, hour)


class SweepTest(unittest.TestCase):
    def test_a_year_at_every_site_has_no_unexpected_collapse(self):
        for label, latitude, longitude, zone in sweep.SITES:
            provider = AstralSunProvider(latitude, longitude, ZoneInfo(zone))
            for policy in ("collapse", "clamp"):
                with self.subTest(label, policy=policy):
                    profile = {**schedule(), "collision_policy": policy}
                    result = sweep.sweep(profile, provider, 2026, label)
                    self.assertEqual(result.days, 365)
                    self.assertEqual(result.unexpected, [])

    def test_the_collapses_are_the_seasonal_ones(self):
        def swept(site):
            _, latitude, longitude, zone = next(s for s in sweep.SITES if s[0].startswith(site))
            return sweep.sweep(
                schedule(), AstralSunProvider(latitude, longitude, ZoneInfo(zone)), 2026
            )

        self.assertEqual(swept("Equatorial").collapses, {})
        self.assertEqual(swept("Perth").collapses, {})
        london = swept("London").collapses
        # Only sunset, and only around midsummer, when it falls too near or after night.
        self.assertEqual(list(london), ["sunset"])
        days = london["sunset"].days
        self.assertTrue(date(2026, 4, 1) < days[0] and days[-1] < date(2026, 9, 1))
        self.assertIn(date(2026, 6, 21), days)
        self.assertEqual(
            set(london["sunset"].reasons),
            {
                "gap conflict: night (priority 100) takes precedence over sunset (priority 50)",
                "order conflict: night (priority 100) takes precedence over sunset (priority 50)",
            },
        )
        tromso = swept("Tromso").collapses
        self.assertIn("no sunrise", tromso["day"].reasons)  # the polar night

    def test_a_broken_day_is_unexpected(self):
        day = scheduler.compute_day(schedule(), mild(), DAY)
        self.assertEqual(sweep.check_day(day, schedule()), [])
        broken = copy.deepcopy(day)
        broken[3].outcome, broken[3].reason = "collapsed", None
        self.assertIn("sunset collapsed with no reason", sweep.check_day(broken, schedule()))
        broken = copy.deepcopy(day)
        broken[1].planned = at(5, 40)
        problems = sweep.check_day(broken, schedule())
        self.assertIn("sunrise lasts less than 0:30:00", problems)
        self.assertTrue(any("ends at" in p for p in problems))
        broken = copy.deepcopy(day)
        broken[4], broken[5] = broken[5], broken[4]
        self.assertTrue(
            any("out of canonical order" in p for p in sweep.check_day(broken, schedule()))
        )
        self.assertTrue(sweep.check_day(day[:5], schedule()))

    def test_the_tool(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sweep.main(["--year", "2026"]), 0)
        self.assertIn("Profile 'standard'", out.getvalue())
        self.assertIn("No unexpected collapses.", out.getvalue())
        # A committed config's profiles, one of them chosen.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(committed_config()))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sweep.main(["--config", str(path), "--profile", "evening"]), 0)
            self.assertIn("Profile 'evening'", out.getvalue())
            self.assertNotIn("Profile 'default'", out.getvalue())
            # An unexpected collapse fails the run.
            broken = scheduler.compute_day(schedule(), mild(), DAY)
            broken[0].reason, broken[0].outcome = None, "collapsed"
            with (
                unittest.mock.patch.object(sweep, "compute_day", return_value=broken),
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                self.assertEqual(sweep.main(["--config", str(path), "--profile", "evening"]), 1)
            self.assertIn("UNEXPECTED", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
