"""The adaptive views (P5.2b): the Now, Analysis and Almanac tabs' reads and the
schedule-profile editor's read, check and save. Against the fake HA and the fixture store;
nothing here reaches a real HA.

The coexistence rule above all: until cutover ha-adaptive-light owns the live `al_`
entities on the same HA, so with the cutover switch off the views must read none of them
(the fake HA records every call it gets); the schedule comes from the config and HA's
location, the learned data from Light Map's own store. With the switch on (in a test's own
scope, `adaptive_wired`) the Now tab's live fields come from HA.
"""

import copy
import json
import unittest
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app import main
from app.core import config
from app.core.ha import HAError
from app.modules.adaptive import almanac, analyser, scheduler
from tests import analyser_fixtures
from tests.fakes import adaptive_wired, committed_config, scenes, schedule
from tests.test_api import ApiCase

LONDON = ZoneInfo("Europe/London")
# A Friday in October, noon in London (BST): the day section is in force.
NOW = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)
LIVING_BULBS = ("chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp")


def at(hh, mm, on=date(2026, 10, 16)):
    return datetime.combine(on, time(hh, mm), tzinfo=LONDON)


def by_section(day):
    return {b["section"]: b for b in day["boundaries"]}


def profile_with(**sections):
    """The standard schedule with some sections' triggers replaced (`night={...}`)."""
    profile = schedule()
    for section in profile["sections"]:
        if section["id"] in sections:
            section["trigger"] = sections[section["id"]]
            section.pop("priority", None)
    return profile


def seed(store, *, first=date(2026, 9, 25), days=18):
    """The living room's readings from `first`, as the observer records them (with a
    section each, as the runtime reports from cutover)."""
    fixture = analyser_fixtures.readings(
        5, bulbs=LIVING_BULBS, first=first, days=days, skip_days=(3,)
    )
    analyser_fixtures.to_store(fixture, store, room="living_room")


def counts(store):
    return {
        table: store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        for table in ("heartbeat", "bulb_reading", "reactive_event", "almanac", "event_log")
    }


# -- The schedule preview --------------------------------------------------------------


class ScheduleDayTest(unittest.TestCase):
    """`schedule_day`: the scheduler's day, shaped for the Now tab and the editor."""

    sun = scheduler.FixedSunProvider(time(6, 0), time(18, 0), LONDON)

    def test_across_a_day(self):
        day = main.schedule_day(schedule(), self.sun, at(12, 0), LONDON)
        self.assertEqual(day["date"], "2026-10-16")
        self.assertEqual(day["time_zone"], "Europe/London")
        self.assertEqual(
            [(b["section"], b["start"], b["ends"], b["outcome"]) for b in day["boundaries"]],
            [
                ("sunrise", "05:30", "09:00", "ran"),
                ("day", "09:00", "15:00", "ran"),
                ("afternoon", "15:00", "18:00", "ran"),
                ("sunset", "18:00", "20:30", "ran"),
                ("night", "20:30", "22:00", "ran"),
                ("sleep", "22:00", None, "ran"),
            ],
        )
        self.assertEqual([b["section"] for b in day["boundaries"] if b["current"]], ["day"])
        self.assertEqual(
            day["current"],
            {"section": "day", "since": "09:00", "since_date": "2026-10-16", "carried_over": False},
        )
        # Each section's trigger and its effective priority ride along.
        sections = by_section(day)
        self.assertEqual(sections["night"]["trigger"], {"type": "clock", "time": "20:30"})
        self.assertEqual(
            (sections["day"]["priority"], sections["day"]["default_priority"]), (50, False)
        )

        # The section in force moves through the day; a boundary instant belongs to the
        # section it starts.
        for moment, section in (
            (at(9, 0), "day"),
            (at(17, 59), "afternoon"),
            (at(23, 30), "sleep"),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(
                    main.schedule_day(schedule(), self.sun, moment, LONDON)["current"]["section"],
                    section,
                )

    def test_before_the_first_boundary_yesterdays_last_section_is_in_force(self):
        day = main.schedule_day(schedule(), self.sun, at(4, 0), LONDON)
        self.assertEqual(
            day["current"],
            {
                "section": "sleep",
                "since": "22:00",
                "since_date": "2026-10-15",
                "carried_over": True,
            },
        )
        # Nothing today is highlighted: today's sleep hasn't started.
        self.assertFalse(any(b["current"] for b in day["boundaries"]))

    def test_now_is_shown_in_has_time_zone(self):
        # 23:30 UTC on the 15th is 00:30 on the 16th in London: the day is London's.
        day = main.schedule_day(
            schedule(), self.sun, datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc), LONDON
        )
        self.assertEqual((day["date"], day["now"]), ("2026-10-16", "00:30"))

    def test_a_default_priority_is_the_triggers(self):
        profile = schedule()
        for section in profile["sections"]:
            section.pop("priority")
        sections = by_section(main.schedule_day(profile, self.sun, at(12, 0), LONDON))
        self.assertEqual(
            {s: (b["priority"], b["default_priority"]) for s, b in sections.items()},
            {
                "sunrise": (100, True),  # sunrise or 05:30: as firm as its clock
                "day": (50, True),
                "afternoon": (50, True),
                "sunset": (50, True),
                "night": (100, True),
                "sleep": (100, True),
            },
        )

    def test_a_sun_case_at_a_real_place(self):
        # London at midsummer: sunset (21:22) falls after night (20:30), an order inversion,
        # so sunset (a sun time, 50) is skipped for the clock's night (100), under either
        # policy. It keeps the time it would have started.
        location = {
            "latitude": 51.51,
            "longitude": -0.13,
            "elevation": 20,
            "time_zone": "Europe/London",
        }
        provider = scheduler.provider_for(location)
        noon = datetime(2026, 6, 21, 12, 0, tzinfo=LONDON)
        for policy in ("collapse", "clamp"):
            with self.subTest(policy=policy):
                profile = {**schedule(), "collision_policy": policy}
                sections = by_section(main.schedule_day(profile, provider, noon, provider.tzinfo))
                sunset = sections["sunset"]
                self.assertEqual(sunset["outcome"], "collapsed")
                self.assertEqual(sunset["start"], "21:22")
                self.assertEqual(sunset["ends"], None)
                self.assertIn("order conflict: night (priority 100)", sunset["reason"])
                # The day it leaves is still whole: afternoon runs on into night.
                self.assertEqual(sections["afternoon"]["ends"], "20:30")
                self.assertEqual(sections["sunrise"]["start"], "04:42")  # the sun, before 05:30

    def test_a_collision_collapses_or_moves(self):
        # Sleep set fifteen minutes after night, inside the 30-minute minimum: a gap.
        profile = profile_with(sleep={"type": "clock", "time": "20:45"})
        collapsed = by_section(main.schedule_day(profile, self.sun, at(12, 0), LONDON))
        self.assertEqual(collapsed["sleep"]["outcome"], "collapsed")
        self.assertEqual(collapsed["sleep"]["start"], "20:45")
        self.assertTrue(collapsed["sleep"]["reason"].startswith("gap conflict: night"))
        self.assertEqual(collapsed["night"]["ends"], None)  # night runs into tomorrow

        clamped = by_section(
            main.schedule_day({**profile, "collision_policy": "clamp"}, self.sun, at(23, 0), LONDON)
        )
        self.assertEqual(clamped["sleep"]["outcome"], "clamped")
        self.assertEqual(clamped["sleep"]["start"], "21:00")
        self.assertEqual(clamped["sleep"]["reason"], "moved later, to start 30 min after night")
        self.assertEqual(clamped["night"]["ends"], "21:00")
        self.assertTrue(clamped["sleep"]["current"])

    def test_a_sun_event_that_doesnt_happen(self):
        polar = scheduler.FixedSunProvider(None, None, LONDON)
        sections = by_section(main.schedule_day(schedule(), polar, at(12, 0), LONDON))
        self.assertEqual(
            {s: b["outcome"] for s, b in sections.items()},
            {
                "sunrise": "ran",  # 05:30: the clock component still resolves
                "day": "collapsed",
                "afternoon": "collapsed",
                "sunset": "collapsed",
                "night": "ran",
                "sleep": "ran",
            },
        )
        self.assertEqual(sections["day"]["start"], None)
        self.assertEqual(sections["day"]["reason"], "no sunrise on 2026-10-16")
        self.assertEqual(sections["sunrise"]["ends"], "20:30")


# -- The views over the API ------------------------------------------------------------


class ViewCase(ApiCase):
    def setUp(self):
        super().setUp()
        self.client.app.state.clock = lambda: NOW

    def get(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def store(self):
        return self.client.app.state.data

    def assert_no_al_read(self):
        """The coexistence rule: with the switch off, HA was asked for its location and
        nothing else. A state read (`get_states`) would carry the old system's `al_`
        entities, so none is made, and no call names an `al_` entity."""
        self.assertTrue(self.ha.calls, "the view should have read HA's location")
        self.assertEqual({name for name, _ in self.ha.calls}, {"get_config"})
        self.assertNotIn("al_", json.dumps(self.ha.calls))


class NowTest(ViewCase):
    def test_with_the_switch_off_the_schedule_computes_and_nothing_al_is_read(self):
        self.assertFalse(main.adaptive_wired_in())
        # The old system's entities are there in HA, live, under the same names.
        self.ha.helper_states.update(
            {
                "input_select.al_scene_living_room": "night",
                "input_boolean.al_active_living_room": "on",
                "input_boolean.al_hold_living_room": "on",
            }
        )
        view = self.get("/api/rooms/living_room/now")
        self.assert_no_al_read()
        self.assertEqual(view["room"], {"id": "living_room", "name": "Living Room"})
        self.assertEqual(
            view["live"],
            {"wired": False, "title": main.NOT_LIVE_TITLE, "note": main.NOT_LIVE_NOTE},
        )
        self.assertNotIn("night", json.dumps(view["live"]))  # nothing of the old system's
        schedule_ = view["schedule"]
        self.assertTrue(schedule_["available"])
        self.assertEqual((schedule_["date"], schedule_["now"]), ("2026-10-16", "12:00"))
        self.assertEqual(
            [(b["section"], b["start"]) for b in schedule_["boundaries"]],
            [
                ("sunrise", "05:30"),
                ("day", "10:25"),
                ("afternoon", "15:06"),
                ("sunset", "18:06"),
                ("night", "20:30"),
                ("sleep", "22:00"),
            ],
        )
        self.assertEqual(schedule_["current"]["section"], "day")
        self.assertEqual(view["profile"]["id"], "evening")
        self.assertEqual(view["profile"]["shared_with"], [])
        # The targets are the store's, which holds nothing yet: nothing learned.
        targets = view["targets"]
        self.assertTrue(targets["available"])
        self.assertEqual(targets["section"], "day")
        self.assertEqual(targets["valid_from"], None)
        self.assertEqual(
            [(u["id"], u["mode"], u["target"]) for u in targets["units"]],
            [("chandelier", "auto", None), ("reading", "auto", None)],
        )
        self.assertEqual(targets["lux_target"], None)

    def test_a_unit_held_off_shows_off_learned_or_not(self):
        doc = committed_config()
        doc["rooms"][0]["adaptive_config"]["scenes"] = scenes(
            "chandelier", "reading", reading="off"
        )
        config.save(self.path, doc)
        targets = self.get("/api/rooms/living_room/now")["targets"]
        self.assertEqual(
            [(u["id"], u["target"]) for u in targets["units"]],
            [("chandelier", None), ("reading", "off")],
        )

    def test_targets_come_from_the_almanac_in_force(self):
        seed(self.store())
        local = NOW.astimezone(LONDON)
        almanac.record(self.store(), analyser.run(self.store(), config.load(self.path), now=local))
        rows = almanac.latest(self.store(), "living_room", today=local.date())
        expected = almanac.payload(committed_config()["rooms"][0], rows)["attributes"]["day"]
        targets = self.get("/api/rooms/living_room/now")["targets"]
        self.assertEqual({u["id"]: u["target"] for u in targets["units"]}, expected["units"])
        self.assertTrue(all(isinstance(u["target"], int) for u in targets["units"]))
        self.assertEqual(targets["lux_target"], expected["lux_target"])
        self.assertEqual(targets["lux_source"], "learned")
        self.assertEqual(targets["valid_from"], rows["day"]["chandelier"]["valid_from"])
        self.assert_no_al_read()

    def test_without_has_location_there_is_no_schedule_and_no_guess(self):
        self.ha.location = {"time_zone": "Europe/London"}  # no latitude or longitude
        view = self.get("/api/rooms/living_room/now")
        self.assertFalse(view["schedule"]["available"])
        self.assertIn("location is incomplete", view["schedule"]["error"])
        self.assertFalse(view["targets"]["available"])
        self.assertFalse(view["live"]["wired"])
        self.assert_no_al_read()

    def test_ha_unreachable_never_fails_the_view(self):
        async def down():
            raise HAError("connection refused")

        self.ha.get_config = down
        view = self.get("/api/rooms/living_room/now")
        self.assertFalse(view["schedule"]["available"])
        self.assertIn("connection refused", view["schedule"]["error"])
        self.assertEqual(self.ha.calls, [])

    def test_a_room_that_isnt_adaptive_or_doesnt_exist(self):
        for tab in ("now", "analysis", "almanac"):
            with self.subTest(tab=tab):
                self.assertEqual(
                    self.get(f"/api/rooms/kitchen/{tab}", 409)["error"], "not_adaptive"
                )
                self.assertEqual(self.get(f"/api/rooms/attic/{tab}", 404)["error"], "not_found")
        self.assertEqual(self.ha.calls, [])

    def test_with_the_switch_on_the_live_fields_come_from_ha(self):
        with adaptive_wired():
            self.assertTrue(main.adaptive_wired_in())
            self.ha.helper_states.update(
                {
                    "input_select.al_scene_living_room": "day",
                    "input_boolean.al_active_living_room": "on",
                    "input_boolean.al_hold_living_room": "off",
                }
            )
            live = self.get("/api/rooms/living_room/now")["live"]
        self.assertEqual(
            live,
            {
                "wired": True,
                "entities": {
                    "scene": "input_select.al_scene_living_room",
                    "guard": "input_boolean.al_active_living_room",
                    "hold": "input_boolean.al_hold_living_room",
                },
                "runtime": None,  # no observer, so no runtime, in this app
                "available": True,
                "error": None,
                "states": {"scene": "day", "guard": "on", "hold": "off"},
            },
        )
        self.assertEqual([name for name, _ in self.ha.calls], ["get_config", "get_states"])

    def test_with_the_switch_on_a_missing_entity_or_ha_down_is_said(self):
        with adaptive_wired():
            live = self.get("/api/rooms/living_room/now")["live"]
            self.assertEqual(live["states"], {"scene": None, "guard": None, "hold": None})

            async def down():
                raise HAError("down")

            self.ha.get_states = down
            live = self.get("/api/rooms/living_room/now")["live"]
            self.assertEqual(
                (live["available"], live["error"]),
                (False, "Home Assistant could not be read (down)"),
            )

    def test_the_runtime_is_reported_when_there_is_one(self):
        class Runtime:
            def status(self):
                return {
                    "running": True,
                    "located": True,
                    "sections": {"living_room": "day"},
                    "last_analysis": "2026-10-16T00:15:00+00:00",
                    "published": ["living_room"],
                    "failing": [],
                }

        self.client.app.state.runtime = Runtime()
        self.addCleanup(setattr, self.client.app.state, "runtime", None)
        with adaptive_wired():
            live = self.get("/api/rooms/living_room/now")["live"]
        self.assertEqual(
            live["runtime"],
            {
                "running": True,
                "section": "day",
                "last_analysis": "2026-10-16T00:15:00+00:00",
                "published": True,
            },
        )


class AnalysisTest(ViewCase):
    def test_an_empty_store_says_so_and_reads_nothing_al(self):
        view = self.get("/api/rooms/living_room/analysis")
        self.assert_no_al_read()
        self.assertEqual(
            view["state"],
            {"mode": "empty", "days_analysed": 0, "valid_from": None, "updated": None},
        )
        self.assertFalse(view["adaptive_live"])
        self.assertEqual([s["learned"] for s in view["sections"]], [False] * 6)
        self.assertEqual(
            view["clock"],
            {"time_zone": "Europe/London", "source": "ha", "today": "2026-10-16", "note": None},
        )
        self.assertEqual([u["id"] for u in view["units"]], ["chandelier", "reading"])

    def test_it_runs_the_analyser_as_the_nightly_pass_would_and_records_nothing(self):
        seed(self.store())
        before = counts(self.store())
        view = self.get("/api/rooms/living_room/analysis")
        self.assertEqual(counts(self.store()), before)  # not a row written, not even a log
        expected = analyser.analyse_room(
            self.store(),
            "living_room",
            {
                "chandelier": ["chandelier_1", "chandelier_2", "chandelier_3"],
                "reading": ["reading_lamp"],
            },
            analyser.LearningConfig(),
            date(2026, 10, 16),
            tz=LONDON,
            presence_configured=False,
        )
        self.assertEqual(view["state"]["mode"], "learning")
        self.assertEqual(view["state"]["days_analysed"], expected["_meta"]["days_analysed"])
        self.assertEqual(view["state"]["valid_from"], expected["_meta"]["valid_from"])
        for section in view["sections"]:
            with self.subTest(section=section["section"]):
                learned = expected[section["section"]]
                self.assertTrue(section["learned"])
                self.assertEqual(section["lux_target"], learned["lux_target"])
                self.assertEqual(section["trust_weight"], learned["trust_weight"])
                self.assertEqual(section["days_contributing"], learned["days_contributing"])
                self.assertEqual(section["high_confidence_days"], learned["high_confidence_days"])
                self.assertEqual(
                    {u: c["brightness"] for u, c in section["units"].items()}, learned["units"]
                )
                self.assertEqual(
                    {u: c["on_fraction"] for u, c in section["units"].items()},
                    learned["on_fraction"],
                )
        self.assert_no_al_read()

    def test_bootstrap_until_the_minimum_days(self):
        seed(self.store(), first=date(2026, 10, 11), days=4)
        state = self.get("/api/rooms/living_room/analysis")["state"]
        self.assertEqual(state["mode"], "bootstrap")
        self.assertLess(state["days_analysed"], 7)

    def test_a_room_with_no_learning_unit_has_nothing_to_analyse(self):
        doc = committed_config()
        block = doc["rooms"][0]["adaptive_config"]
        block["adaptive_bulbs"], block["learning_units"] = [], []
        config.save(self.path, doc)
        view = self.get("/api/rooms/living_room/analysis")
        self.assertEqual(view["state"]["mode"], "no_units")
        self.assertEqual(view["units"], [])

    def test_without_has_time_zone_days_are_utc_and_it_says_so(self):
        self.ha.location = {}
        clock = self.get("/api/rooms/living_room/analysis")["clock"]
        self.assertEqual(clock["source"], "utc")
        self.assertEqual(clock["time_zone"], "UTC")
        self.assertIn("days are counted in UTC", clock["note"])

    def test_without_the_store(self):
        self.client.app.state.data = None
        self.assertEqual(
            self.get("/api/rooms/living_room/analysis", 503)["error"], "store_unavailable"
        )
        self.assertEqual(
            self.get("/api/rooms/living_room/almanac", 503)["error"], "store_unavailable"
        )
        # The Now tab still shows the schedule.
        view = self.get("/api/rooms/living_room/now")
        self.assertTrue(view["schedule"]["available"])
        self.assertFalse(view["targets"]["available"])


class AlmanacTest(ViewCase):
    def test_an_empty_store_has_no_snapshot_and_nothing_al_is_read(self):
        view = self.get("/api/rooms/living_room/almanac")
        self.assert_no_al_read()
        self.assertEqual(view["snapshot"], None)
        self.assertEqual(view["note"], main.ALMANAC_SOURCE_NOTE)
        self.assertEqual([s["section"] for s in view["sections"]], list(config.SECTIONS))
        night = view["sections"][4]
        self.assertEqual(
            night["knobs"],
            {
                "lux_target_seed": None,
                "lux_margin": 5,
                "max_step_pct": 0.04,
                "maintenance_enabled": False,
                "transition_seconds": 120,
            },
        )
        self.assertEqual(
            night["units"]["chandelier"],
            {"mode": "auto", "target": None, "learned": None, "valid_from": None},
        )
        self.assertEqual(view["retired_units"], [])

    def test_never_the_ha_sensor(self):
        # HA holds the old system's almanac under the same name; the tab shows the store's.
        self.ha.states.append(
            {
                "entity_id": "sensor.al_almanac_living_room",
                "state": "2026-10-01",
                "attributes": {"day": {"units": {"chandelier": 200}, "lux_target": 99}},
            }
        )
        view = self.get("/api/rooms/living_room/almanac")
        self.assertEqual(view["sections"][1]["units"]["chandelier"]["target"], None)
        self.assertNotIn("2026-10-01", json.dumps(view))
        self.assert_no_al_read()

    def test_the_snapshot_in_force_with_its_targets_and_knobs(self):
        seed(self.store())
        local = NOW.astimezone(LONDON)
        rows = analyser.run(self.store(), config.load(self.path), now=local)
        almanac.record(self.store(), rows)
        # A unit held off for night: "off" whatever was learned; the knobs are the config's now.
        doc = config.load(self.path)
        block = doc["rooms"][0]["adaptive_config"]
        block["scenes"]["night"]["units"]["reading"]["mode"] = "off"
        block["scenes"]["night"]["lux_margin"] = 8
        # A unit the room no longer has keeps its history in the store.
        almanac.record(self.store(), [{**rows[0], "learning_unit_id": "old_lamp"}])
        config.save(self.path, doc)
        view = self.get("/api/rooms/living_room/almanac")
        in_force = almanac.latest(self.store(), "living_room", today=local.date())
        newest = in_force["day"]["chandelier"]
        self.assertEqual(view["snapshot"]["valid_from"], newest["valid_from"])
        self.assertEqual(view["snapshot"]["mode"], "learning")
        self.assertEqual(view["snapshot"]["ran_at"], "2026-10-16 12:00")  # the pass, in HA's zone
        night = view["sections"][4]
        self.assertEqual(night["units"]["reading"]["target"], "off")
        self.assertEqual(
            night["units"]["reading"]["learned"], in_force["night"]["reading"]["brightness"]
        )
        self.assertEqual(
            night["units"]["chandelier"]["target"], in_force["night"]["chandelier"]["brightness"]
        )
        self.assertEqual(night["knobs"]["lux_margin"], 8)
        self.assertEqual(night["lux_source"], "learned")
        self.assertEqual(view["retired_units"], ["old_lamp"])


# -- The schedule editor ---------------------------------------------------------------


class ScheduleEditorTest(ViewCase):
    def initial(self):
        # The kitchen adaptive too, on the living room's profile: one profile, two rooms.
        doc = committed_config()
        kitchen = doc["rooms"][1]
        kitchen["adaptive"] = True
        kitchen["adaptive_config"] = {"schedule_profile": "evening"}
        return doc

    def editor(self):
        return self.get("/api/schedule_profiles")

    def put(self, profiles, base):
        return self.client.put(
            "/api/schedule_profiles", json={"schedule_profiles": profiles, "base": base}
        )

    def test_the_read(self):
        view = self.editor()
        self.assertEqual([p["id"] for p in view["schedule_profiles"]], ["default", "evening"])
        self.assertEqual(view["version"], main.schedule_version(config.load(self.path)))
        self.assertEqual(
            view["rooms"],
            [
                {"id": "kitchen", "name": "Kitchen", "profile": "evening"},
                {"id": "living_room", "name": "Living Room", "profile": "evening"},
            ],
        )
        self.assertEqual(view["sections"], list(config.SECTIONS))
        self.assertEqual(view["default_profile"], "default")
        self.assertEqual(view["standard"], config.standard_schedule())
        self.assertEqual(view["location"]["time_zone"], "Europe/London")
        self.assertEqual(by_section(view["days"]["evening"])["night"]["start"], "20:30")
        self.assertEqual(self.ha.calls, [("get_config", ())])

    def test_save_validates_then_writes_logs_and_touches_no_ha(self):
        view = self.editor()
        profiles = copy.deepcopy(view["schedule_profiles"])
        evening = profiles[1]
        evening["name"] = "Evenings"
        evening["sections"][4]["trigger"] = {"type": "clock", "time": "21:00"}
        evening["sections"][3]["trigger"]["offset_minutes"] = -15
        del evening["sections"][3]["priority"]
        evening["collision_policy"] = "clamp"
        self.ha.calls.clear()
        response = self.put(profiles, view["version"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body["changes"],
            [
                "renamed evening to Evenings",
                "Evenings (evening): collisions now clamp",
                "Evenings (evening): sunset now starts 15 min before sunset",
                "Evenings (evening): sunset priority back to its default",
                "Evenings (evening): night now starts at 21:00",
            ],
        )
        saved = self.saved()["schedule_profiles"][1]
        self.assertEqual(saved["sections"][4]["trigger"], {"type": "clock", "time": "21:00"})
        self.assertNotIn("priority", saved["sections"][3])
        self.assertEqual(body["version"], self.editor()["version"])
        self.assertEqual(
            [name for name, _ in self.ha.calls], ["get_config"]
        )  # the editor's re-read
        self.assertEqual(self.ha.writes, [])
        log = self.get("/api/log?category=config")["entries"]
        self.assertEqual(log[0]["message"], "Saved the schedule profiles: 5 change(s).")
        self.assertEqual(log[0]["detail"]["changes"], body["changes"])

    def test_a_shared_profile_edit_reaches_every_room_that_selects_it(self):
        view = self.editor()
        profiles = copy.deepcopy(view["schedule_profiles"])
        profiles[1]["sections"][4]["trigger"] = {"type": "clock", "time": "21:15"}
        self.assertEqual(self.put(profiles, view["version"]).status_code, 200)
        for room, other in (("living_room", "Kitchen"), ("kitchen", "Living Room")):
            with self.subTest(room=room):
                now = self.get(f"/api/rooms/{room}/now")
                self.assertEqual(by_section(now["schedule"])["night"]["start"], "21:15")
                self.assertEqual([r["name"] for r in now["profile"]["shared_with"]], [other])

    def test_create_reorder_and_delete(self):
        view = self.editor()
        profiles = copy.deepcopy(view["schedule_profiles"])
        weekend = {"id": "weekend", "name": "Weekend", **config.standard_schedule()}
        weekend["sections"][0]["name"] = "Dawn"  # a section's name is kept as written
        profiles.insert(0, weekend)
        response = self.put(profiles, view["version"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changes"], ["added the profile Weekend (weekend)"])
        saved = self.saved()["schedule_profiles"]
        self.assertEqual([p["id"] for p in saved], ["weekend", "default", "evening"])
        self.assertEqual(saved[0]["sections"][0]["name"], "Dawn")
        # The room editor offers them in that order.
        offered = self.get("/api/rooms/living_room/adaptive_config")["schedule_profiles"]
        self.assertEqual([p["id"] for p in offered], ["weekend", "default", "evening"])

        view = self.editor()
        reordered = [view["schedule_profiles"][i] for i in (0, 2, 1)]
        response = self.put(reordered, view["version"])
        self.assertEqual(response.json()["changes"], ["reordered the profiles"])
        self.assertEqual(
            [p["id"] for p in self.saved()["schedule_profiles"]], ["weekend", "evening", "default"]
        )

        view = self.editor()
        unused = [p for p in view["schedule_profiles"] if p["id"] != "weekend"]
        response = self.put(unused, view["version"])
        self.assertEqual(response.json()["changes"], ["removed the profile Weekend (weekend)"])

    def test_the_version_check(self):
        view = self.editor()
        profiles = copy.deepcopy(view["schedule_profiles"])
        profiles[1]["name"] = "Mine"
        before = self.path.read_text()
        self.assert_error(self.put(profiles, "stale"), 409, "edit_stale")
        self.assert_unchanged(before)

        # Another tab saves first: this one is stale.
        other = copy.deepcopy(view["schedule_profiles"])
        other[1]["name"] = "Theirs"
        self.assertEqual(self.put(other, view["version"]).status_code, 200)
        self.assert_error(self.put(profiles, view["version"]), 409, "edit_stale")
        self.assertEqual(self.saved()["schedule_profiles"][1]["name"], "Theirs")

    def test_a_room_changing_its_profile_makes_the_editor_stale(self):
        # A room moved onto a profile the editor showed as unused (and so offered to
        # delete) makes the editor's save stale: a reload, not a 422 about a reference.
        view = self.editor()
        living = self.get("/api/rooms/living_room/adaptive_config")
        block = copy.deepcopy(living["adaptive_config"])
        block["schedule_profile"] = "default"
        response = self.client.put(
            "/api/rooms/living_room/adaptive_config",
            json={"adaptive_config": block, "base": living["version"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_error(self.put(view["schedule_profiles"], view["version"]), 409, "edit_stale")

    def test_what_the_save_refuses(self):
        view = self.editor()
        base = view["version"]
        before = self.path.read_text()

        def refused(mutate, expect):
            profiles = copy.deepcopy(view["schedule_profiles"])
            mutate(profiles)
            body = self.assert_error(self.put(profiles, base), 422, "invalid_config")
            self.assertTrue(any(expect in p for p in body["problems"]), body["problems"])
            self.assert_unchanged(before)
            return body

        def bad_time(profiles):
            profiles[1]["sections"][4]["trigger"]["time"] = "25:00"

        def offset(profiles):
            profiles[1]["sections"][1]["trigger"]["offset_minutes"] = 800

        def no_default(profiles):
            del profiles[0]

        def one_component(profiles):
            profiles[1]["sections"][0]["trigger"]["of"] = profiles[1]["sections"][0]["trigger"][
                "of"
            ][:1]

        def order(profiles):
            sections = profiles[1]["sections"]
            sections[4], sections[5] = sections[5], sections[4]

        def in_use(profiles):
            del profiles[1]  # the living room and the kitchen follow it

        def priority(profiles):
            profiles[1]["sections"][2]["priority"] = 1001

        def minimum(profiles):
            profiles[1]["min_section_minutes"] = 0

        refused(bad_time, "schedule_profiles/1/sections/4/trigger: time")
        refused(offset, "offset_minutes: 800 is greater than the maximum of 720")
        refused(no_default, "a schedule profile with id 'default' is required")
        refused(one_component, "is too short")
        refused(order, "sections must be sunrise, day, afternoon, sunset, night, sleep")
        refused(in_use, "adaptive_config.schedule_profile 'evening' does not exist")
        refused(priority, "1001 is greater than the maximum of 1000")
        refused(minimum, "0 is less than the minimum of 1")
        self.assertEqual(self.ha.writes, [])

    def test_the_check_writes_nothing(self):
        view = self.editor()
        before = self.path.read_text()
        profiles = copy.deepcopy(view["schedule_profiles"])
        profiles[1]["sections"][5]["trigger"] = {"type": "clock", "time": "20:45"}
        response = self.client.post(
            "/api/schedule_profiles/check", json={"schedule_profiles": profiles}
        )
        self.assertEqual(response.status_code, 200, response.text)
        checked = response.json()
        self.assertEqual(checked["problems"], [])
        sleep = by_section(checked["days"]["evening"])["sleep"]
        self.assertEqual((sleep["outcome"], sleep["start"]), ("collapsed", "20:45"))
        # A broken draft: its problems, and its day can't be computed.
        profiles[1]["sections"][5]["trigger"] = {"type": "clock", "time": "nope"}
        checked = self.client.post(
            "/api/schedule_profiles/check", json={"schedule_profiles": profiles}
        ).json()
        self.assertTrue(
            any("sections/5/trigger" in p for p in checked["problems"]), checked["problems"]
        )
        self.assertEqual(checked["days"]["evening"], None)
        self.assertIsNotNone(checked["days"]["default"])
        self.assert_unchanged(before)
        self.assertEqual(self.get("/api/log?category=config")["entries"], [])
        self.assertEqual(self.ha.writes, [])

    def test_without_has_location_the_editor_still_edits(self):
        self.ha.location = {}
        view = self.editor()
        self.assertFalse(view["location"]["available"])
        self.assertEqual(view["days"], {})
        profiles = copy.deepcopy(view["schedule_profiles"])
        profiles[0]["name"] = "Standard"
        self.assertEqual(self.put(profiles, view["version"]).status_code, 200)


if __name__ == "__main__":
    unittest.main()
