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
        self.assertEqual(boundaries[0].name, "sunrise")  # display names are deferred

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

    def test_a_clamp_never_moves_a_section_out_of_its_day(self):
        # Three hours apart at least, under clamp: night moves to 22:30, and sleep would move
        # to 01:30 tomorrow, after tomorrow's own first boundary: never in force, though
        # reported as run (the predecessor's clamp). It collides as under collapse instead:
        # night and sleep tie (100), and the earlier section wins.
        profile = {**schedule(), "collision_policy": "clamp", "min_section_minutes": 180}
        found = run(profile, mild())
        self.assertEqual(found["night"].planned, at(22, 30))
        self.assertFalse(found["sleep"].ran)
        self.assertIn(
            "gap conflict: night (priority 100) takes precedence over sleep", found["sleep"].reason
        )
        for b in found.values():
            if b.ran:
                self.assertEqual(b.planned.date(), DAY, b.section)
        self.assertEqual(sweep.check_day(list(found.values()), profile, DAY), [])

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

    def test_a_fall_back_hour_never_takes_the_section_back(self):
        # Beirut's clocks go back from 00:00 to 23:00 on 25 October 2026, so 23:00-24:00 on
        # the 24th happens twice. Sleep starts at 23:30: once in force, the repeated hour
        # must not read as before it again (comparing wall clocks did, and the runtime would
        # fire night a second time).
        beirut = ZoneInfo("Asia/Beirut")
        profile = with_section(schedule(), "sleep", trigger={"type": "clock", "time": "23:30"})
        provider = FixedSunProvider(time(6, 30), time(17, 0), beirut)
        moment, seen = datetime(2026, 10, 24, 17, 0, tzinfo=UTC), []
        while moment < datetime(2026, 10, 24, 23, 0, tzinfo=UTC):
            local = moment.astimezone(beirut)
            section = scheduler.active_section_at(profile, provider, local, beirut)[0]
            if not seen or seen[-1] != section:
                seen.append(section)
            moment += timedelta(minutes=5)
        self.assertEqual(seen, ["sunset", "night", "sleep"])

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


NEW_YORK = ZoneInfo("America/New_York")
SPRING = date(2026, 3, 8)  # New York's clocks go from 02:00 EST to 03:00 EDT
FALL = date(2026, 11, 1)  # and back from 02:00 EDT to 01:00 EST


def clocks(**times):
    """The standard profile with each named section starting at a clock time instead."""
    profile = copy.deepcopy(schedule())
    for section in profile["sections"]:
        if section["id"] in times:
            section["trigger"] = {"type": "clock", "time": times[section["id"]]}
    return profile


def in_force(profile, provider, day, zone):
    """What the runtime has in force minute by minute across `day` (as `active_section_at`
    answers it), as `[(section, local start HH:MM, local time it was first seen)]`: the
    sections the Now tab's report must agree with."""
    seen = []
    moment = datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone).astimezone(UTC)
    while moment < end:
        section, since = scheduler.active_section_at(
            profile, provider, moment.astimezone(zone), zone
        )
        if not seen or seen[-1][:2] != (section, f"{since:%H:%M}"):
            seen.append((section, f"{since:%H:%M}", f"{moment.astimezone(zone):%H:%M}"))
        moment += timedelta(minutes=1)
    return seen


def reported(boundaries):
    return [(b.section, f"{b.planned:%H:%M}") for b in boundaries if b.ran]


class ClockChangeTest(unittest.TestCase):
    """P6a: a day the clocks change on is worked out in real time, so what the Now tab
    reports as run is what the runtime puts in force (`active_section_at`)."""

    SUN = FixedSunProvider(time(7, 30), time(19, 0), NEW_YORK)

    def day(self, profile, on=SPRING, provider=None, zone=NEW_YORK):
        found = scheduler.compute_day(profile, provider or self.SUN, on, zone)
        self.assertEqual(sweep.check_day(found, profile, on), [])
        self.assertEqual(sweep.check_in_force(found, profile, provider or self.SUN, zone), [])
        return {b.section: b for b in found}, found

    def test_a_start_the_clocks_skip_runs_from_the_gaps_end_and_is_in_force(self):
        profile = clocks(sunrise="00:30", day="01:30", afternoon="02:30", sunset="04:00")
        found, boundaries = self.day(profile)
        afternoon = found["afternoon"]
        self.assertTrue(afternoon.ran)
        self.assertEqual(afternoon.planned, datetime(2026, 3, 8, 3, 0, tzinfo=NEW_YORK))
        self.assertEqual(afternoon.planned.utcoffset(), timedelta(hours=-4))
        self.assertEqual(
            afternoon.shifted,
            "02:30 doesn't happen on 2026-03-08 (the clocks go forward past it), so it "
            "starts at 03:00",
        )
        self.assertIsNone(afternoon.reason)
        self.assertEqual(found["day"].ends, afternoon.planned)
        self.assertIsNone(found["sunset"].shifted)
        # The runtime agrees, minute by minute: each section reported as run comes into
        # force at the time reported, and nothing else does.
        self.assertEqual(
            [(s, at_) for s, at_, _ in in_force(profile, self.SUN, SPRING, NEW_YORK)][1:],
            reported(boundaries),
        )
        self.assertIn(
            ("afternoon", "03:00", "03:00"), in_force(profile, self.SUN, SPRING, NEW_YORK)
        )

    def test_the_predecessors_day_runs_on_paper_and_never_in_force(self):
        # Afternoon at 02:00 and sunset at 02:30, both skipped, then night at 03:00. Read by
        # the wall clock (the predecessor's way), all three ran; the runtime, by instant,
        # read the two skipped times as 03:00 and 03:30 and only ever put night in force.
        profile = clocks(
            sunrise="01:00", day="01:30", afternoon="02:00", sunset="02:30", night="03:00"
        )
        found, boundaries = self.day(profile)
        # Now both start at the gap's end, with night: they collide, honestly, and collapse.
        for section in ("afternoon", "sunset"):
            self.assertFalse(found[section].ran, section)
            self.assertIn("gap conflict", found[section].reason)
            self.assertEqual(found[section].planned, datetime(2026, 3, 8, 3, 0, tzinfo=NEW_YORK))
        self.assertTrue(found["night"].ran)
        self.assertEqual(found["day"].ends, found["night"].planned)
        seen = {s for s, _, _ in in_force(profile, self.SUN, SPRING, NEW_YORK)}
        self.assertEqual(seen, {"sleep", "sunrise", "day", "night"})
        self.assertEqual(
            [(s, at_) for s, at_, _ in in_force(profile, self.SUN, SPRING, NEW_YORK)][1:],
            reported(boundaries),
        )

    def test_lengths_and_a_clamps_move_are_real_time(self):
        # 01:50 EST to 03:10 EDT is 80 minutes by the wall clock but 20 really: too short.
        found, _ = self.day(clocks(sunrise="00:30", day="01:50", afternoon="03:10", sunset="04:00"))
        self.assertFalse(found["afternoon"].ran)
        self.assertIn("gap conflict: day", found["afternoon"].reason)
        # Under clamp it moves 30 real minutes after day: 02:20 EST is 03:20 EDT.
        profile = {
            **clocks(sunrise="00:30", day="01:50", afternoon="03:10", sunset="04:00"),
            "collision_policy": "clamp",
        }
        found, _ = self.day(profile)
        self.assertEqual(found["afternoon"].planned, datetime(2026, 3, 8, 3, 20, tzinfo=NEW_YORK))
        self.assertEqual(found["day"].ends, found["afternoon"].planned)
        # Where the clocks go back, 01:40 EDT to 02:00 EST is 20 minutes by the wall clock
        # but 80 really: long enough.
        profile = clocks(sunrise="00:30", day="01:40", afternoon="02:00", sunset="04:00")
        found, _ = self.day(profile, on=FALL)
        self.assertTrue(found["day"].ran and found["afternoon"].ran)

    def test_a_sun_offset_is_elapsed_time(self):
        # Five hours before a 06:45 EDT sunrise is 00:45 EST: by the wall clock it was
        # 01:45 (and on another zone's day, a time the clocks skip).
        sun = FixedSunProvider(time(6, 45), time(19, 0), NEW_YORK)
        profile = copy.deepcopy(schedule())
        profile["sections"][0]["trigger"] = {
            "type": "sun",
            "event": "sunrise",
            "offset_minutes": -300,
        }
        found, _ = self.day(profile, provider=sun)
        self.assertEqual(found["sunrise"].planned, datetime(2026, 3, 8, 0, 45, tzinfo=NEW_YORK))
        self.assertIsNone(found["sunrise"].shifted)

    def test_the_day_is_its_real_span(self):
        # Beirut skips midnight itself (00:00 -> 01:00): a section at 00:30 starts at 01:00,
        # and so does one an offset pushes before the day.
        beirut = ZoneInfo("Asia/Beirut")
        sun = FixedSunProvider(time(6, 30), time(19, 0), beirut)
        profile = clocks(sunrise="00:30")
        found, _ = self.day(profile, date(2026, 3, 29), sun, beirut)
        self.assertEqual(f"{found['sunrise'].planned:%H:%M %z}", "01:00 +0300")
        profile["sections"][0]["trigger"] = {
            "type": "sun",
            "event": "sunrise",
            "offset_minutes": -500,
        }
        found, _ = self.day(profile, date(2026, 3, 29), sun, beirut)
        self.assertEqual(f"{found['sunrise'].planned:%H:%M %z}", "01:00 +0300")
        # Nuuk skips the day's last hour (23:00 -> 00:00): a section at 23:30 can't start on
        # its day after 22:59, and is in force from then until the next day's first.
        nuuk = ZoneInfo("America/Nuuk")
        sun = FixedSunProvider(time(6, 30), time(19, 0), nuuk)
        found, _ = self.day(clocks(sleep="23:30"), date(2026, 3, 28), sun, nuuk)
        self.assertEqual(f"{found['sleep'].planned:%Y-%m-%d %H:%M}", "2026-03-28 22:59")
        self.assertIn("so it starts at 22:59", found["sleep"].shifted)
        moment = datetime(2026, 3, 29, 3, 0, tzinfo=nuuk)
        self.assertEqual(
            scheduler.active_section_at(clocks(sleep="23:30"), sun, moment, nuuk),
            ("sleep", found["sleep"].planned),
        )

    def test_the_fall_back_hour_still_resolves_to_its_first_pass(self):
        # 01:30 happens twice on New York's fall-back day: a section starting then starts at
        # the first, and the second never takes it back.
        profile = clocks(sunrise="01:30", day="09:00")
        found, _ = self.day(profile, on=FALL)
        self.assertEqual(found["sunrise"].planned.utcoffset(), timedelta(hours=-4))
        seen = in_force(profile, self.SUN, FALL, NEW_YORK)
        self.assertEqual(
            [s for s, _, _ in seen],
            ["sleep", "sunrise", "day", "afternoon", "sunset", "night", "sleep"],
        )
        self.assertEqual(seen[1], ("sunrise", "01:30", "01:30"))

    def test_the_now_tab_says_a_start_was_moved_and_why(self):
        from app.main import schedule_day

        profile = clocks(sunrise="00:30", day="01:30", afternoon="02:30", sunset="04:00")
        view = schedule_day(profile, self.SUN, datetime(2026, 3, 8, 12, 0, tzinfo=UTC), NEW_YORK)
        afternoon = next(b for b in view["boundaries"] if b["section"] == "afternoon")
        self.assertEqual(afternoon["start"], "03:00")
        self.assertEqual(afternoon["outcome"], "clamped")  # shown as MOVED
        self.assertEqual(
            afternoon["reason"],
            "02:30 doesn't happen on 2026-03-08 (the clocks go forward past it), so it "
            "starts at 03:00",
        )
        day_ = next(b for b in view["boundaries"] if b["section"] == "day")
        self.assertEqual((day_["outcome"], day_["reason"], day_["ends"]), ("ran", None, "03:00"))


class SweepTest(unittest.TestCase):
    def test_a_year_at_every_site_has_no_unexpected_collapse(self):
        for label, latitude, longitude, zone in sweep.SITES:
            provider = AstralSunProvider(latitude, longitude, ZoneInfo(zone))
            for policy, minimum in (
                ("collapse", 30),
                ("clamp", 30),
                ("clamp", 300),
                ("clamp", 720),
            ):
                with self.subTest(label, policy=policy, minimum=minimum):
                    profile = {
                        **schedule(),
                        "collision_policy": policy,
                        "min_section_minutes": minimum,
                    }
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

    def test_the_sites_include_every_kind_of_clock_change(self):
        changes = {}
        for label, _, _, zone in sweep.SITES:
            tz = ZoneInfo(zone)
            for day in sweep.clock_changes(tz, 2026):
                before = datetime.combine(day, time(0, 0), tzinfo=tz).utcoffset()
                after = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).utcoffset()
                changes.setdefault(label.split(" (")[0], []).append((day, after - before))
        self.assertNotIn("Equatorial", changes)
        self.assertNotIn("Perth", changes)
        self.assertEqual(
            changes["New York"], [(SPRING, timedelta(hours=1)), (FALL, timedelta(hours=-1))]
        )
        # Southern: forward in the spring, which is October.
        self.assertEqual(changes["Sydney"][1], (date(2026, 10, 4), timedelta(hours=1)))
        self.assertEqual(changes["Lord Howe"][1], (date(2026, 10, 4), timedelta(minutes=30)))
        # At midnight, and at the day's end.
        self.assertEqual(changes["Beirut"][0], (date(2026, 3, 29), timedelta(hours=1)))
        self.assertEqual(changes["Santiago"][1], (date(2026, 9, 6), timedelta(hours=1)))
        self.assertEqual(changes["Nuuk"][0], (date(2026, 3, 28), timedelta(hours=1)))

    def test_the_scheduler_is_sound_across_every_clock_change(self):
        probes = sweep.clock_change_probes()
        self.assertEqual(len(probes), 62)
        self.assertEqual(sweep.sweep_clock_changes(2026), [])
        self.assertEqual(sweep.sweep_clock_changes(2027), [])

    def test_the_sweep_fails_if_the_fix_is_reverted(self):
        # The mutation check: the scheduler's day worked out by the wall clock again (the
        # predecessor's reading: no gap end, no instants, offsets added to the wall clock).
        wall_clock = {
            "_instant": lambda moment: moment,
            "_existing": lambda moment: moment,
            "_after": lambda moment, elapsed: moment + elapsed,
        }
        with unittest.mock.patch.multiple(scheduler, **wall_clock):
            found = sweep.sweep_clock_changes(2026)
        self.assertTrue(found)
        # The in-force check catches it by itself: a section reported as run that the
        # runtime never puts in force.
        never = [line for line in found if "the runtime has" in line or "never in force" in line]
        self.assertTrue(never)
        self.assertIn(
            "New York (40.71N), probe_14_collapse: 2026-03-08: afternoon is reported in force "
            "from 02:00, but just as it starts the runtime has night (from 03:00) in force",
            found,
        )

    def test_the_in_force_check_reads_a_report_as_the_runtime_would(self):
        # The predecessor's report for New York's spring-forward day, by hand: sunset at
        # 02:30, a time the clocks skip, reported as run before night at 03:00.
        def b(section, hh, mm, offset):
            zone = timezone(timedelta(hours=offset))
            planned = datetime(2026, 3, 8, hh, mm, tzinfo=zone).astimezone(NEW_YORK)
            return Boundary(section, section, planned, 100)

        report = [b("day", 1, 30, -5), b("night", 3, 0, -4), b("sleep", 22, 0, -4)]
        skipped = Boundary("sunset", "sunset", datetime(2026, 3, 8, 2, 30, tzinfo=NEW_YORK), 50)
        report.insert(1, skipped)
        for i, boundary in enumerate(report):
            boundary.ends = report[i + 1].planned if i + 1 < len(report) else None
        profile = schedule()
        problems = sweep.check_in_force(
            report, profile, ClockChangeTest.SUN, NEW_YORK, days=lambda day: report
        )
        # Read by instant, the skipped 02:30 comes after night's 03:00: the runtime moves
        # from day straight to night, and sunset never comes into force.
        self.assertEqual(
            problems,
            [
                (
                    "day is reported in force from 01:30, but just as it ends the runtime has "
                    "night (from 03:00) in force"
                ),
                "sunset is never in force: the next section starts no later than it does",
            ],
        )
        problems = sweep.check_day(report, {**profile, "min_section_minutes": 1}, SPRING)
        self.assertIn("sunset starts at 02:30, a time the clocks skip on 2026-03-08", problems)
        self.assertIn("night starts before sunset", problems)

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
        # A kept section that starts on another day would never be in force.
        broken = copy.deepcopy(day)
        broken[5].planned = at(1, 30, DAY + timedelta(days=1))
        broken[4].ends = broken[5].planned
        self.assertIn(
            f"sleep starts on {DAY + timedelta(days=1)}, outside its day",
            sweep.check_day(broken, schedule(), DAY),
        )

    def test_the_tool(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sweep.main(["--year", "2026"]), 0)
        self.assertIn("Profile 'standard'", out.getvalue())
        self.assertIn("Nuuk (64.18N)", out.getvalue())
        self.assertIn("The scheduler across every clock change of 2026", out.getvalue())
        self.assertIn("sound: every section run is in force", out.getvalue())
        self.assertIn("No unexpected collapses.", out.getvalue())
        # The rest needs only one site to show.
        self.enterContext(unittest.mock.patch.object(sweep, "SITES", sweep.SITES[2:3]))
        # A committed config's profiles, one of them chosen.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(committed_config()))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sweep.main(["--config", str(path), "--profile", "evening"]), 0)
            self.assertIn("Profile 'evening'", out.getvalue())
            self.assertNotIn("Profile 'default'", out.getvalue())
            # An unexpected collapse fails the run. (The broken day has no time zone, which
            # the in-force check would compare with the sites' days: it is left out here,
            # and has tests of its own.)
            broken = scheduler.compute_day(schedule(), mild(), DAY)
            broken[0].reason, broken[0].outcome = None, "collapsed"
            with (
                unittest.mock.patch.object(sweep, "compute_day", return_value=broken),
                unittest.mock.patch.object(sweep, "check_in_force", return_value=[]),
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                self.assertEqual(sweep.main(["--config", str(path), "--profile", "evening"]), 1)
            self.assertIn("UNEXPECTED", printed.getvalue())
            # So does a clock change the scheduler gets wrong, whatever the profiles.
            with (
                unittest.mock.patch.object(sweep, "sweep_clock_changes", return_value=["x"]),
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                self.assertEqual(sweep.main(["--config", str(path), "--profile", "evening"]), 1)
            self.assertIn("UNEXPECTED x", printed.getvalue())
            path.write_text("{")
            with self.assertRaises(SystemExit) as caught:
                sweep.main(["--config", str(path)])
            self.assertIn("doesn't load", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
