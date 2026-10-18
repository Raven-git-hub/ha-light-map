"""The adaptive per-room config and its editor (P5.0): the version-4 shape and its
migration, the schedule profiles and the analyser's tuning (version 5, P5.2a), the
cross-checks (adaptive bulbs, the partition into learning units, the scene
matrix, sensors, the schedule profile), the matrix's normalisation (orphan columns dropped,
new units and sections filled), the dimmability guard, and the editor's API (its read, with
HA's capability per bulb; its save, validate-then-write against the room's version).
Against fixtures and the fake HA; nothing here reaches a real HA."""

import ast
import copy
import json
import unittest
from contextlib import nullcontext
from pathlib import Path

from app import main
from app.core import config, inventory
from app.core.config import ConfigError
from app.core.ha import HAError
from tests.fakes import (
    STATES,
    committed_config,
    light,
    scenes,
    schedule,
    v2_config,
    v3_config,
    v4_config,
)
from tests.test_api import ApiCase


SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "schema" / "config.schema.json").read_text()
)


def problems_of(call, *args) -> list[str]:
    try:
        call(*args)
    except ConfigError as exc:
        return exc.problems
    raise AssertionError("expected a ConfigError")


def living(doc):
    return next(r for r in doc["rooms"] if r["id"] == "living_room")


def block(doc):
    return living(doc)["adaptive_config"]


def kitchen_adaptive(doc, **adaptive_config):
    """The fixture with the kitchen (one bulb, kitchen_strip) flagged adaptive."""
    kitchen = next(r for r in doc["rooms"] if r["id"] == "kitchen")
    kitchen["adaptive"] = True
    kitchen["adaptive_config"] = adaptive_config
    return doc


class VocabularyTest(unittest.TestCase):
    def test_the_sections_are_one_vocabulary(self):
        # Core's constant, the schema's enum, the matrix's required keys and the module's
        # import are the same six, in the same order.
        from app.modules import adaptive

        self.assertEqual(
            config.SECTIONS, ("sunrise", "day", "afternoon", "sunset", "night", "sleep")
        )
        self.assertEqual(tuple(SCHEMA["$defs"]["section"]["enum"]), config.SECTIONS)
        scenes_schema = SCHEMA["$defs"]["adaptive_config"]["properties"]["scenes"]
        self.assertEqual(tuple(scenes_schema["required"]), config.SECTIONS)
        self.assertEqual(
            tuple(SCHEMA["$defs"]["unit_scene"]["properties"]["mode"]["enum"]), config.MODES
        )
        self.assertIs(adaptive.SECTIONS, config.SECTIONS)

    def test_core_never_imports_the_module(self):
        root = Path(__file__).resolve().parent.parent / "app" / "core"
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name.startswith("app.modules"), f"{path.name} imports {name}")

    def test_the_defaults_are_the_predecessors(self):
        self.assertEqual(
            config.scene_defaults("day"),
            {
                "lux_target_seed": None,
                "lux_margin": 5,
                "max_step_pct": 0.04,
                "maintenance_enabled": True,
                "transition_seconds": 120,
            },
        )
        for section in ("night", "sleep"):
            self.assertFalse(config.scene_defaults(section)["maintenance_enabled"])
        self.assertEqual(config.DEFAULT_MODE, "auto")


class MigrationTest(unittest.TestCase):
    def test_version_3_migrates_to_4(self):
        old = v3_config()
        migrated = config._v3_to_v4(copy.deepcopy(old))
        self.assertEqual(config.migrate(old)["version"], config.CURRENT_VERSION)
        self.assertEqual(old["version"], 3)  # the input is untouched
        self.assertNotIn("adaptive_bulbs", block(old))
        # A version-3 unit was what named a bulb for the module: its bulbs are adaptive.
        self.assertEqual(
            block(migrated)["adaptive_bulbs"],
            ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"],
        )
        self.assertEqual(config.loads(v3_config()), committed_config())

    def test_a_bulb_in_no_unit_migrates_switched_only(self):
        old = v3_config()
        block(old)["learning_units"] = [
            {"id": "chandelier", "name": "Chandelier", "members": ["chandelier_2"]}
        ]
        loaded = config.loads(old)
        self.assertEqual(block(loaded)["adaptive_bulbs"], ["chandelier_2"])
        self.assertEqual(list(block(loaded)["scenes"]["night"]["units"]), ["chandelier"])

    def test_a_house_with_no_adaptive_room_only_changes_version(self):
        old = v3_config()
        living(old)["adaptive"] = False
        del living(old)["adaptive_config"]
        migrated = config.migrate(old)
        # Version 5 fills the profiles (each version-4 profile was an id and a name).
        self.assertEqual(
            {**migrated, "version": 3, "schedule_profiles": old["schedule_profiles"]}, old
        )
        self.assertEqual(config.loads(old)["rooms"], migrated["rooms"])

    def test_a_room_just_flagged_in_version_3_stays_all_switched_only(self):
        old = v3_config()
        kitchen_adaptive(old)  # the Map's tick wrote {}
        kitchen = next(r for r in config.loads(old)["rooms"] if r["id"] == "kitchen")
        self.assertEqual(kitchen["adaptive_config"]["adaptive_bulbs"], [])
        self.assertEqual(kitchen["adaptive_config"]["learning_units"], [])
        self.assertEqual(kitchen["adaptive_config"]["scenes"], scenes())

    def test_a_version_3_scenes_that_doesnt_fit_is_reported_not_dropped(self):
        old = v3_config()
        block(old)["scenes"] = {"evening": {"groups": {}}}
        found = problems_of(config.loads, old)
        self.assertTrue(any("evening" in p for p in found), found)

    def test_a_malformed_version_3_config_is_refused_not_a_crash(self):
        # The migration runs before validation: what it can't read is validation's to
        # report (a ConfigError, so the Map says the config is broken), never a TypeError
        # that stops the app starting.
        old = v3_config()
        living(old)["bulbs"][0]["id"] = ["chandelier_1"]
        self.assertTrue(problems_of(config.loads, old))
        old = v3_config()
        block(old)["learning_units"] = "chandelier"
        self.assertTrue(problems_of(config.loads, old))

    def test_version_4_refuses_what_version_3_let_through_by_hand(self):
        # Documented in DESIGN ("Version 4"): refused with the problem named, to fix by hand.
        for key, value, fragment in (
            ("presence_sensors", ["device_tracker.phone"], "presence sensor device_tracker.phone"),
            ("lux_sensors", ["input_number.lux"], "lux sensor input_number.lux"),
        ):
            old = v3_config()
            block(old)[key] = value
            with self.subTest(key):
                self.assertTrue(any(fragment in p for p in problems_of(config.loads, old)))
        old = v3_config()
        old["schedule_profiles"][1]["name"] = None
        self.assertTrue(
            any("schedule_profiles/1/name" in p for p in problems_of(config.loads, old))
        )

    def test_versions_1_and_2_migrate_through(self):
        self.assertEqual(config.loads(v2_config()), committed_config())

    def test_an_older_light_map_refuses_version_4(self):
        from unittest import mock

        with mock.patch.object(config, "CURRENT_VERSION", 3):
            self.assertIn("newer Light Map", problems_of(config.migrate, v4_config())[0])

    def test_an_older_light_map_refuses_version_5(self):
        from unittest import mock

        with mock.patch.object(config, "CURRENT_VERSION", 4):
            self.assertIn("newer Light Map", problems_of(config.migrate, committed_config())[0])


class ScheduleProfileTest(unittest.TestCase):
    """Version 5 (P5.2a): a schedule profile's sections, triggers and collision policy, and
    the analyser's tuning (`learning`)."""

    def test_version_4_backfills_every_profile_with_the_standard_schedule(self):
        old = v4_config()
        loaded = config.loads(old)
        self.assertEqual(old["schedule_profiles"][1], {"id": "evening"})  # input untouched
        self.assertEqual(loaded, committed_config())
        default, evening = loaded["schedule_profiles"]
        self.assertEqual(default, {"id": "default", "name": "Default", **schedule()})
        self.assertEqual(evening, {"id": "evening", **schedule()})  # no name invented

    def test_the_standard_schedule_is_the_predecessors(self):
        self.assertEqual(config.standard_schedule(), schedule())
        # A fresh copy each time: a caller filling a profile can't change the next one.
        config.standard_schedule()["sections"].clear()
        self.assertEqual(len(config.standard_schedule()["sections"]), 6)

    def test_the_real_minimal_default_profile_survives(self):
        # Exactly what P5.1's blank() wrote, and so what a house with no adaptive room holds.
        v4_blank = {
            "version": 4,
            "homeassistant": {"verify_ssl": True},
            "system": {},
            "rooms": [],
            "unassigned": [],
            "schedule_profiles": [{"id": "default", "name": "Default"}],
            "learning": {},
        }
        loaded = config.loads(v4_blank)
        self.assertEqual(loaded["version"], 5)
        self.assertEqual(
            loaded["schedule_profiles"], [{"id": "default", "name": "Default", **schedule()}]
        )
        self.assertEqual(config.loads(config.blank()), loaded)

    def test_what_a_version_4_profile_held_is_kept_or_reported_never_replaced(self):
        old = v4_config()
        old["schedule_profiles"][1]["collision_policy"] = "clamp"
        old["schedule_profiles"][1]["min_section_minutes"] = 20
        evening = config.loads(old)["schedule_profiles"][1]
        self.assertEqual(
            (evening["collision_policy"], evening["min_section_minutes"]), ("clamp", 20)
        )
        self.assertEqual(evening["sections"], schedule()["sections"])
        old = v4_config()
        old["schedule_profiles"][1]["sections"] = "evenings"
        self.assertTrue(
            any("schedule_profiles/1/sections" in p for p in problems_of(config.loads, old))
        )

    def test_the_collision_settings_default_but_the_sections_dont(self):
        doc = committed_config()
        del doc["schedule_profiles"][1]["collision_policy"]
        del doc["schedule_profiles"][1]["min_section_minutes"]
        evening = config.loads(doc)["schedule_profiles"][1]
        self.assertEqual(
            (evening["collision_policy"], evening["min_section_minutes"]), ("collapse", 30)
        )
        doc = committed_config()
        del doc["schedule_profiles"][1]["sections"]
        problems = problems_of(config.loads, doc)
        self.assertTrue(any("'sections' is a required property" in p for p in problems), problems)

    def test_the_sections_are_the_six_once_each_in_canonical_order(self):
        doc = committed_config()
        sections = doc["schedule_profiles"][0]["sections"]
        sections[0], sections[1] = sections[1], sections[0]
        problems = problems_of(config.loads, doc)
        self.assertTrue(any("schedule profile 'default': sections must be" in p for p in problems))
        doc = committed_config()
        doc["schedule_profiles"][0]["sections"][5] = copy.deepcopy(
            doc["schedule_profiles"][0]["sections"][4]
        )
        self.assertTrue(problems_of(config.loads, doc))
        doc = committed_config()
        doc["schedule_profiles"][0]["sections"].pop()
        self.assertTrue(problems_of(config.loads, doc))

    def test_a_trigger_must_be_one_of_the_four_shapes(self):
        bad = [
            {"type": "clock", "time": "25:00"},
            {"type": "clock", "time": "7:30"},
            {"type": "sun", "event": "noon"},
            {"type": "sun", "event": "sunset", "offset_minutes": 800},
            {"type": "sun", "event": "sunset", "offset_minutes": 1.5},
            {"type": "earliest", "of": [{"type": "clock", "time": "05:30"}]},
            {"type": "latest", "of": [{"type": "clock", "time": "05:30"}, {"type": "x"}]},
            {"type": "clock", "time": "05:30", "event": "sunrise"},
        ]
        for trigger in bad:
            with self.subTest(trigger):
                doc = committed_config()
                doc["schedule_profiles"][0]["sections"][4]["trigger"] = trigger
                problems = problems_of(config.loads, doc)
                self.assertTrue(any("schedule_profiles/0/sections/4" in p for p in problems))
        doc = committed_config()
        doc["schedule_profiles"][0]["sections"][4]["trigger"] = {
            "type": "latest",
            "of": [
                {"type": "clock", "time": "20:30"},
                {
                    "type": "earliest",
                    "of": [{"type": "sun", "event": "sunset"}, {"type": "clock", "time": "23:00"}],
                },
            ],
        }
        config.loads(doc)

    def test_priority_and_policy_are_checked(self):
        for path, value in (
            (("sections", 0, "priority"), 1001),
            (("sections", 0, "priority"), -1),
            (("collision_policy",), "shuffle"),
            (("min_section_minutes",), -5),
        ):
            with self.subTest(path):
                doc = committed_config()
                target = doc["schedule_profiles"][0]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                self.assertTrue(problems_of(config.loads, doc))

    def test_a_section_may_carry_a_name_and_the_minimum_is_a_minute(self):
        doc = committed_config()
        doc["schedule_profiles"][0]["sections"][0]["name"] = "Early"
        self.assertEqual(config.loads(doc)["schedule_profiles"][0]["sections"][0]["name"], "Early")
        doc["schedule_profiles"][0]["min_section_minutes"] = 0
        # Two sections at one instant would leave one never in force.
        self.assertTrue(any("min_section_minutes" in p for p in problems_of(config.loads, doc)))

    def test_a_bad_trigger_names_what_is_wrong(self):
        for trigger, expected in (
            (
                {"type": "clock", "time": "24:00"},
                "sections/4/trigger: time: '24:00' does not match",
            ),
            ({"type": "sun", "event": "noon"}, "trigger: event: 'noon' is not one of"),
            (
                {
                    "type": "latest",
                    "of": [{"type": "clock", "time": "05:30"}, {"type": "clock", "time": "5:30"}],
                },
                "trigger: of/1/time: '5:30' does not match",
            ),
            ({"type": "earliest", "of": [{"type": "clock", "time": "05:30"}]}, "of: "),
        ):
            with self.subTest(trigger):
                doc = committed_config()
                doc["schedule_profiles"][0]["sections"][4]["trigger"] = trigger
                problems = problems_of(config.loads, doc)
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_learning_is_checked_and_every_field_optional(self):
        doc = committed_config()
        doc["learning"] = {
            "lookback_days": 14,
            "publish_delay_days": 0,
            "bootstrap_min_days": 3,
            "reactive_weight": 4.5,
            "post_reactive_boost": [2, 1.5],
            "recency_weights": [
                {"max_age_days": 7, "weight": 2},
                {"max_age_days": None, "weight": 0},  # older days don't count
            ],
            "confidence": {"high_weight_threshold": 20, "low_multiplier": 0.25},
        }
        self.assertEqual(config.loads(doc)["learning"], doc["learning"])
        for key, value in (
            ("lookback_days", 0),
            ("reactive_weight", 0.5),
            ("recency_weights", [{"max_age_days": 3}]),
            ("recency_weights", []),
            ("confidence", {"high": 1}),
            ("lookbak_days", 21),
        ):
            with self.subTest(key):
                doc = committed_config()
                doc["learning"] = {key: value}
                self.assertTrue(problems_of(config.loads, doc))


class CrossCheckTest(unittest.TestCase):
    def assert_flags(self, doc, *fragments):
        found = problems_of(config.cross_check, doc)
        for fragment in fragments:
            self.assertTrue(any(fragment in p for p in found), f"{fragment!r} not in {found}")
        return found

    def test_the_fixture_passes(self):
        config.cross_check(committed_config())

    def test_a_room_may_mix_tiers(self):
        doc = committed_config()
        block(doc)["adaptive_bulbs"] = ["chandelier_1", "chandelier_2", "chandelier_3"]
        block(doc)["learning_units"] = block(doc)["learning_units"][:1]
        block(doc)["scenes"] = scenes("chandelier")
        config.cross_check(doc)
        config.validate(doc)

    def test_an_adaptive_bulb_must_be_a_bulb_of_the_room(self):
        doc = committed_config()
        block(doc)["adaptive_bulbs"].append("kitchen_strip")
        self.assert_flags(
            doc,
            "adaptive bulb 'kitchen_strip' is not a bulb in this room (it is in room 'kitchen')",
        )
        block(doc)["adaptive_bulbs"][-1] = "ghost"
        self.assert_flags(doc, "adaptive bulb 'ghost' is not a bulb in this room (no bulb has")

    def test_an_ignored_bulb_cant_be_adaptive(self):
        doc = committed_config()
        living(doc)["bulbs"][3]["ignored"] = True
        self.assert_flags(doc, "adaptive bulb 'reading_lamp' is ignored")

    def test_a_unit_member_must_be_adaptive(self):
        doc = committed_config()
        block(doc)["adaptive_bulbs"].remove("chandelier_2")
        self.assert_flags(
            doc,
            "learning unit 'chandelier': member 'chandelier_2' is switched-only; list it in "
            "adaptive_bulbs, or take it out of the unit",
        )

    def test_every_adaptive_bulb_is_in_a_unit(self):
        doc = committed_config()
        block(doc)["learning_units"][0]["members"].remove("chandelier_3")
        self.assert_flags(doc, "adaptive bulb 'chandelier_3' is in no learning unit")

    def test_no_bulb_is_in_two_units(self):
        doc = committed_config()
        block(doc)["learning_units"][1]["members"].append("chandelier_1")
        self.assert_flags(
            doc, "bulb 'chandelier_1' is in learning units 'chandelier' and 'reading'"
        )

    def test_the_matrix_covers_every_unit_and_section(self):
        doc = committed_config()
        del block(doc)["scenes"]["night"]["units"]["reading"]
        del block(doc)["scenes"]["sleep"]
        self.assert_flags(
            doc,
            "scenes 'night': no mode for learning unit(s) reading",
            "scenes: no entry for section 'sleep'",
        )

    def test_the_matrix_has_no_column_for_a_unit_the_room_lacks(self):
        doc = committed_config()
        block(doc)["scenes"]["day"]["units"]["ghost"] = {"mode": "off"}
        self.assert_flags(doc, "scenes 'day': 'ghost' is not a learning unit of the room")

    def test_a_mode_is_auto_or_off(self):
        doc = committed_config()
        block(doc)["scenes"]["day"]["units"]["reading"] = {"mode": "dim"}
        self.assert_flags(doc, "scenes 'day' unit 'reading': mode must be auto or off")
        self.assertTrue(problems_of(config.validate, doc))
        block(doc)["scenes"]["day"]["units"]["reading"] = {"mode": "off"}
        config.cross_check(doc)

    def test_only_the_six_sections(self):
        doc = committed_config()
        block(doc)["scenes"]["evening"] = copy.deepcopy(block(doc)["scenes"]["day"])
        self.assert_flags(doc, "scenes: 'evening' is not a section")
        self.assertTrue(problems_of(config.validate, doc))

    def test_the_schedule_profile_resolves(self):
        doc = committed_config()
        block(doc)["schedule_profile"] = "midnight"
        self.assert_flags(doc, "adaptive_config.schedule_profile 'midnight' does not exist")

    def test_sensor_ids_are_well_formed(self):
        doc = committed_config()
        block(doc)["lux_sensors"] = ["binary_sensor.window", "sensor.al_almanac_living_room"]
        block(doc)["presence_sensors"] = [
            "person.alex",
            "input_boolean.lm_soft_porch",
            "binary_sensor.sofa",
            "input_boolean.guest_mode",
        ]
        found = self.assert_flags(
            doc,
            "lux sensor binary_sensor.window is not a sensor.*",
            "lux sensor sensor.al_almanac_living_room is one of Light Map's own entities",
            "presence sensor person.alex is not a binary_sensor.* or input_boolean.*",
            "presence sensor input_boolean.lm_soft_porch is one of Light Map's own",
        )
        self.assertEqual(len(found), 4)  # the sofa and guest mode are fine

    def test_existence_is_not_checked_here(self):
        doc = committed_config()
        block(doc)["lux_sensors"] = ["sensor.not_in_ha"]
        config.cross_check(doc)

    def test_the_schema_refuses_unknown_keys_and_bad_knobs(self):
        for path, value, fragment in (
            (["dimmable"], True, "Additional properties"),
            (["scenes", "day", "lux_margn"], 5, "Additional properties"),
            (
                ["scenes", "day", "max_step_pct"],
                0,
                "rooms/0/adaptive_config/scenes/day/max_step_pct",
            ),
            (
                ["scenes", "day", "max_step_pct"],
                4,
                "rooms/0/adaptive_config/scenes/day/max_step_pct",
            ),
            (["scenes", "day", "transition_seconds"], 1.5, "transition_seconds"),
            (["scenes", "day", "lux_target_seed"], -1, "lux_target_seed"),
            (["adaptive_bulbs"], ["Chandelier 1"], "adaptive_bulbs/0"),
        ):
            doc = committed_config()
            target = block(doc)
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            with self.subTest(path=path, value=value):
                found = problems_of(config.validate, doc)
                self.assertTrue(any(fragment in p for p in found), found)

    def test_a_seed_may_be_null_or_a_number(self):
        doc = committed_config()
        block(doc)["scenes"]["day"]["lux_target_seed"] = 300
        config.validate(doc)


class NormaliseTest(unittest.TestCase):
    def test_an_orphan_column_is_dropped_and_a_new_unit_filled(self):
        doc = committed_config()
        block(doc)["scenes"]["night"]["units"]["reading"] = {"mode": "off"}
        block(doc)["learning_units"][1]["id"] = "reading_corner"
        loaded = config.loads(doc)
        for section in config.SECTIONS:
            self.assertEqual(
                block(loaded)["scenes"][section]["units"],
                {"chandelier": {"mode": "auto"}, "reading_corner": {"mode": "auto"}},
            )

    def test_missing_sections_and_knobs_take_their_defaults(self):
        doc = committed_config()
        block(doc)["scenes"] = {
            "night": {"lux_margin": 8, "units": {"chandelier": {"mode": "off"}}}
        }
        night = block(config.loads(doc))["scenes"]["night"]
        self.assertEqual(
            night,
            {
                "lux_target_seed": None,
                "lux_margin": 8,
                "max_step_pct": 0.04,
                "maintenance_enabled": False,
                "transition_seconds": 120,
                "units": {"chandelier": {"mode": "off"}, "reading": {"mode": "auto"}},
            },
        )
        self.assertEqual(
            block(config.loads(doc))["scenes"]["day"], scenes("chandelier", "reading")["day"]
        )

    def test_what_it_cant_read_is_left_for_validation(self):
        doc = committed_config()
        block(doc)["scenes"]["day"]["lux_margn"] = 5  # a typo is reported, not dropped
        block(doc)["scenes"]["sunset"]["units"]["reading"] = {"mode": None}
        found = problems_of(config.loads, doc)
        self.assertTrue(any("lux_margn" in p for p in found), found)
        self.assertTrue(any("sunset/units/reading/mode" in p for p in found), found)

    def test_only_what_is_absent_is_filled(self):
        # An explicit null is something someone wrote: reported, not replaced by a default.
        doc = committed_config()
        block(doc)["scenes"]["night"]["units"]["reading"] = None
        found = problems_of(config.loads, doc)
        self.assertTrue(any("night/units/reading" in p for p in found), found)
        doc = committed_config()
        block(doc)["scenes"] = None
        found = problems_of(config.loads, doc)
        self.assertTrue(any("adaptive_config/scenes" in p for p in found), found)
        doc = committed_config()
        del block(doc)["scenes"]  # absent: filled
        self.assertEqual(block(config.loads(doc))["scenes"], scenes("chandelier", "reading"))

    def test_normalising_is_idempotent_and_deterministic(self):
        doc = committed_config()
        block(doc)["scenes"] = dict(reversed(list(block(doc)["scenes"].items())))
        once = config.apply_defaults(doc)
        self.assertEqual(list(block(once)["scenes"]), list(config.SECTIONS))
        self.assertEqual(json.dumps(config.apply_defaults(once)), json.dumps(once))

    def test_ignoring_a_units_last_member_drops_its_column(self):
        # The P4.1 gap: set_ignored and prune_members dropped a unit without its scenes.
        doc = inventory.set_ignored(committed_config(), "reading_lamp", True)
        self.assertEqual([u["id"] for u in block(doc)["learning_units"]], ["chandelier"])
        self.assertNotIn("reading_lamp", block(doc)["adaptive_bulbs"])
        for section in config.SECTIONS:
            self.assertEqual(list(block(doc)["scenes"][section]["units"]), ["chandelier"])
        config.validate(doc)
        config.cross_check(doc)

    def test_a_bulb_moved_out_leaves_its_tier_and_unit(self):
        doc = inventory.snapshot_with_move(committed_config(), "reading_lamp", "kitchen")
        self.assertNotIn("reading_lamp", block(doc)["adaptive_bulbs"])
        self.assertEqual(list(block(doc)["scenes"]["day"]["units"]), ["chandelier"])
        config.cross_check(doc)

    def test_a_sync_that_takes_a_bulb_away_leaves_a_config_that_saves(self):
        areas = [
            {"area_id": "living_room", "name": "Living Room"},
            {"area_id": "kitchen", "name": "Kitchen"},
        ]
        lights = [
            light(s["entity_id"], "living_room")
            for s in STATES
            if s["entity_id"] in ("light.chandelier_1", "light.chandelier_2", "light.chandelier_3")
        ]
        lights.append(light("light.kitchen_strip", "kitchen"))
        result = config.reconcile_area_snapshot(committed_config(), areas, lights)
        self.assertEqual(
            block(result)["adaptive_bulbs"], ["chandelier_1", "chandelier_2", "chandelier_3"]
        )
        config.validate(result)
        config.cross_check(result)
        self.assertIn(
            "learning unit removed: 'living_room/reading'",
            config.snapshot_changes(committed_config(), result),
        )


class EditTest(unittest.TestCase):
    def test_set_adaptive_config_replaces_the_block(self):
        doc = committed_config()
        new = {"adaptive_bulbs": [], "learning_units": []}
        result = inventory.set_adaptive_config(doc, "living_room", new)
        self.assertEqual(block(result), new)
        self.assertNotEqual(block(doc), new)  # pure
        saved = config.checked(result)
        self.assertEqual(block(saved)["scenes"], scenes())
        self.assertEqual(block(saved)["lux_sensors"], [])

    def test_a_room_that_isnt_adaptive_is_refused(self):
        self.assertIn(
            "isn't adaptive",
            problems_of(inventory.set_adaptive_config, committed_config(), "kitchen", {})[0],
        )

    def test_what_unflagging_drops_counts_the_scenes_only_when_set(self):
        doc = committed_config()
        self.assertNotIn("scene", " ".join(inventory.adaptive_config_content(living(doc))))
        block(doc)["scenes"]["night"]["units"]["reading"]["mode"] = "off"
        block(doc)["scenes"]["day"]["lux_margin"] = 8
        self.assertIn(
            "2 scene setting(s) changed from the defaults",
            inventory.adaptive_config_content(living(doc)),
        )
        self.assertEqual(
            config.non_default_scenes(block(doc)["scenes"]),
            ["day lux margin", "night reading: off"],
        )

    def test_the_change_summary(self):
        before = block(committed_config())
        after = copy.deepcopy(before)
        after["adaptive_bulbs"].remove("reading_lamp")
        after["learning_units"] = [after["learning_units"][0]]
        after["learning_units"][0]["name"] = "Ceiling"
        after["lux_sensors"] = ["sensor.window_lux"]
        after["schedule_profile"] = "default"
        after["scenes"]["night"]["units"]["chandelier"]["mode"] = "off"
        after["scenes"]["day"]["lux_target_seed"] = 300
        names = {b["id"]: b["name"] for b in living(committed_config())["bulbs"]}
        self.assertEqual(
            inventory.adaptive_config_changes(before, after, names),
            [
                "lux sensors added: sensor.window_lux",
                "lux sensors removed: sensor.living_room_lux",
                "schedule profile: evening -> default",
                "Reading lamp: adaptive -> switched-only",
                "learning unit removed: Reading",
                "learning unit renamed: Chandelier -> Ceiling",
                "day lux target seed: None -> 300",
                "night Ceiling: auto -> off",
            ],
        )
        self.assertEqual(inventory.adaptive_config_changes(before, before, names), [])


class CapabilityTest(unittest.TestCase):
    def capability(self, entity_id, states=STATES):
        state = next((s for s in states if s["entity_id"] == entity_id), None)
        return inventory.bulb_capability(state)

    def test_dimmable(self):
        found = self.capability("light.chandelier_1")
        self.assertEqual(found["dimmable"], True)
        self.assertFalse(found["locked"])
        self.assertEqual(found["color_modes"], ["color_temp", "xy"])
        self.assertIsNone(found["note"])
        self.assertTrue(self.capability("light.reading_lamp")["dimmable"])

    def test_on_off_only_is_locked(self):
        found = self.capability("light.counter_lamp")
        self.assertEqual((found["dimmable"], found["locked"]), (False, True))
        self.assertIn("only switches on and off", found["note"])

    def test_offline_is_unknown_and_not_locked(self):
        found = self.capability("light.chandelier_3")
        self.assertEqual((found["dimmable"], found["locked"]), (None, False))
        self.assertIn("offline", found["note"])
        # Even if HA restored its capability while it is unavailable: an outage never locks.
        offline = copy.deepcopy(STATES)
        state = next(s for s in offline if s["entity_id"] == "light.counter_lamp")
        state["state"] = "unknown"
        self.assertFalse(self.capability("light.counter_lamp", offline)["locked"])

    def test_a_missing_attribute_or_light_is_unknown(self):
        for entity_id in ("light.porch", "light.not_there"):
            found = self.capability(entity_id)
            self.assertEqual((found["dimmable"], found["locked"]), (None, False), entity_id)
        odd = [
            {
                "entity_id": "light.x",
                "state": "on",
                "attributes": {"supported_color_modes": "onoff"},
            }
        ]
        self.assertFalse(self.capability("light.x", odd)["locked"])
        empty = [
            {"entity_id": "light.x", "state": "on", "attributes": {"supported_color_modes": []}}
        ]
        self.assertFalse(self.capability("light.x", empty)["locked"])


class EditorApiTest(ApiCase):
    def initial(self):
        doc = committed_config()
        # The counter lamp (on/off only) in the living room, switched-only, so the editor
        # has every kind of bulb to show.
        kitchen = next(r for r in doc["rooms"] if r["id"] == "kitchen")
        from tests.fakes import bulb

        living(doc)["bulbs"].append(bulb("counter_lamp"))
        living(doc)["bulbs"].append(bulb("porch_light", entity_id="light.porch"))
        doc["unassigned"] = []
        kitchen["bulbs"] = [bulb("kitchen_strip")]
        return doc

    def editor_version(self, room_id="living_room"):
        """The editor's `version` for the config as saved: the room's block and its active
        bulbs (`main.editor_version`), not the Map's room version."""
        doc = config.loads(self.saved())
        return main.editor_version(doc, next(r for r in doc["rooms"] if r["id"] == room_id))

    def view(self, room="living_room"):
        response = self.client.get(f"/api/rooms/{room}/adaptive_config")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, adaptive_config, base, room="living_room"):
        return self.client.put(
            f"/api/rooms/{room}/adaptive_config",
            json={"adaptive_config": adaptive_config, "base": base},
        )

    def test_the_view_annotates_each_bulb(self):
        view = self.view()
        self.assertEqual(
            view["room"], {"id": "living_room", "name": "Living Room", "ha_area_id": "living_room"}
        )
        self.assertEqual(view["version"], self.editor_version())
        self.assertNotEqual(view["version"], self.version("living_room"))
        self.assertEqual(view["sections"], list(config.SECTIONS))
        bulbs = {b["id"]: b for b in view["bulbs"]}
        self.assertEqual(
            list(bulbs),
            [
                "chandelier_1",
                "chandelier_2",
                "chandelier_3",
                "reading_lamp",
                "counter_lamp",
                "porch_light",
            ],
        )
        self.assertEqual(
            (bulbs["chandelier_1"]["tier"], bulbs["chandelier_1"]["unit"]),
            ("adaptive", "chandelier"),
        )
        self.assertEqual(
            (bulbs["counter_lamp"]["tier"], bulbs["counter_lamp"]["unit"]), ("switched", None)
        )
        self.assertTrue(bulbs["chandelier_1"]["capability"]["dimmable"])  # dimmable
        self.assertTrue(bulbs["counter_lamp"]["capability"]["locked"])  # onoff only: locked
        self.assertFalse(bulbs["chandelier_3"]["capability"]["locked"])  # unavailable: not
        self.assertIn("offline", bulbs["chandelier_3"]["capability"]["note"])
        self.assertFalse(bulbs["porch_light"]["capability"]["locked"])  # attribute missing
        self.assertEqual(bulbs["reading_lamp"]["groups"], ["living_all", "reading_corner"])
        self.assertEqual([g["id"] for g in view["groups"]], ["living_all", "reading_corner"])
        self.assertEqual(
            view["schedule_profiles"],
            [{"id": "default", "name": "Default"}, {"id": "evening", "name": "evening"}],
        )
        self.assertEqual(
            view["suggestions"]["lux_sensors"],
            [{"entity_id": "sensor.living_room_lux", "name": "Living room lux"}],
        )
        self.assertEqual(view["ha"], {"available": True, "error": None})
        # One read of HA's states; nothing written.
        self.assertEqual(self.ha.calls, [("get_states", ())])

    def test_an_ignored_bulb_is_never_offered(self):
        response = self.ignore("porch_light")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("porch_light", [b["id"] for b in self.view()["bulbs"]])

    def test_without_ha_every_capability_is_unknown_and_nothing_locked(self):
        async def broken():
            raise HAError("GET /api/states failed: connection refused")

        self.ha.get_states = broken
        view = self.view()
        self.assertFalse(view["ha"]["available"])
        self.assertIn("connection refused", view["ha"]["error"])
        self.assertEqual({b["capability"]["locked"] for b in view["bulbs"]}, {False})
        self.assertEqual(view["suggestions"], {"lux_sensors": [], "presence_sensors": []})

    def test_an_unexpected_answer_from_ha_never_fails_the_read(self):
        async def returning(value):
            return value

        async def timing_out():
            raise TimeoutError()

        for name, reader, logged in (
            ("an empty body", lambda: returning(None), False),
            ("a proxy's login page", lambda: returning("<html>login</html>"), False),
            ("a timeout the client didn't wrap", timing_out, True),
        ):
            self.ha.get_states = reader
            # A failure the client didn't wrap is logged: it may be a bug worth seeing.
            logs = self.assertLogs("app.main", "WARNING") if logged else nullcontext()
            with self.subTest(name), logs:
                view = self.view()
                self.assertFalse(view["ha"]["available"], name)
                self.assertEqual({b["capability"]["locked"] for b in view["bulbs"]}, {False})
        odd = copy.deepcopy(STATES)
        for state in odd:
            if state["entity_id"] == "light.counter_lamp":
                state["attributes"] = ["onoff"]
        odd.append("not a state")
        self.ha.get_states = lambda: returning(odd)
        view = self.view()
        self.assertTrue(view["ha"]["available"])
        counter = next(b for b in view["bulbs"] if b["id"] == "counter_lamp")
        self.assertEqual(
            (counter["capability"]["dimmable"], counter["capability"]["locked"]), (None, False)
        )

    def test_a_room_that_isnt_adaptive_has_no_editor(self):
        self.assert_error(
            self.client.get("/api/rooms/kitchen/adaptive_config"), 409, "not_adaptive"
        )
        self.assert_error(self.client.get("/api/rooms/attic/adaptive_config"), 404, "not_found")
        self.assert_error(
            self.save({}, self.version("kitchen"), room="kitchen"), 409, "not_adaptive"
        )

    def test_save_validates_then_writes_and_logs(self):
        view = self.view()
        edited = copy.deepcopy(view["adaptive_config"])
        edited["adaptive_bulbs"].append("porch_light")
        edited["learning_units"][0]["members"].append("porch_light")
        edited["scenes"]["night"]["units"]["chandelier"]["mode"] = "off"
        edited["presence_sensors"] = ["binary_sensor.sofa"]
        del edited["scenes"]["day"]  # the defaults fill what the editor leaves out
        self.ha.calls.clear()
        response = self.save(edited, view["version"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body["changes"],
            [
                "presence sensors added: binary_sensor.sofa",
                "Porch light: switched-only -> adaptive",
                "learning unit Chandelier: members Chandelier 1, Chandelier 2, Chandelier 3 -> "
                "Chandelier 1, Chandelier 2, Chandelier 3, Porch light",
                "night Chandelier: auto -> off",
            ],
        )
        saved = block(self.saved())
        self.assertIn("porch_light", saved["adaptive_bulbs"])
        self.assertEqual(saved["scenes"]["night"]["units"]["chandelier"], {"mode": "off"})
        self.assertEqual(saved["scenes"]["day"], scenes("chandelier", "reading")["day"])
        self.assertEqual(body["version"], self.editor_version())
        self.assertEqual(self.ha.calls, [])  # the save never touches HA
        log = self.client.get("/api/log", params={"category": "config"}).json()["entries"]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["room_id"], "living_room")
        self.assertTrue(
            log[0]["message"].startswith("Saved the adaptive config of Living Room: 5 adaptive")
        )
        self.assertEqual(log[0]["detail"]["changes"], body["changes"])

    def test_a_stale_save_never_reverts_another_edit(self):
        view = self.view()
        first = copy.deepcopy(view["adaptive_config"])
        first["scenes"]["night"]["units"]["reading"]["mode"] = "off"
        self.assertEqual(self.save(first, view["version"]).status_code, 200)
        before = self.path.read_text()
        second = copy.deepcopy(view["adaptive_config"])  # made against the older view
        second["lux_sensors"] = []
        body = self.assert_error(self.save(second, view["version"]), 409, "edit_stale")
        self.assertIn("changed since the editor showed it", body["detail"])
        self.assert_unchanged(before)
        self.assert_error(self.save(second, None), 422, "bad_request")

    def test_the_room_version_covers_edits_from_the_map(self):
        view = self.view()
        # Ignoring a unit member (from the Map) changes the block, so the editor is stale.
        self.assertEqual(self.ignore("reading_lamp", confirm_drops=True).status_code, 200)
        self.assert_error(self.save(view["adaptive_config"], view["version"]), 409, "edit_stale")

    def test_a_bulb_ignored_while_the_editor_is_open_makes_it_stale(self):
        # The race the editor's own version closes (P5.1): the porch light is switched-only,
        # so ignoring it in another tab leaves the block, and the Map's room version, as
        # they were. The editor still offers it; made adaptive there and saved, it used to
        # fail validation (a 422 about an ignored bulb) instead of asking for a reload.
        view = self.view()
        room_before = self.version("living_room")
        self.assertEqual(self.ignore("porch_light").status_code, 200)
        self.assertEqual(self.version("living_room"), room_before)
        self.assertEqual(block(self.saved()), view["adaptive_config"])
        edited = copy.deepcopy(view["adaptive_config"])
        edited["adaptive_bulbs"].append("porch_light")
        edited["learning_units"].append(
            {"id": "porch", "name": "Porch", "members": ["porch_light"]}
        )
        before = self.path.read_text()
        body = self.assert_error(self.save(edited, view["version"]), 409, "edit_stale")
        self.assertIn("changed since the editor showed it", body["detail"])
        self.assert_unchanged(before)
        # Reloaded, the editor no longer offers it, and saves against the new version.
        fresh = self.view()
        self.assertNotIn("porch_light", [b["id"] for b in fresh["bulbs"]])
        self.assertEqual(self.save(fresh["adaptive_config"], fresh["version"]).status_code, 200)
        # Un-ignoring it (or a sync bringing a bulb in) changes the roster the same way.
        self.assertEqual(self.ignore("porch_light", ignored=False).status_code, 200)
        self.assert_error(self.save(fresh["adaptive_config"], fresh["version"]), 409, "edit_stale")

    def test_a_save_that_wouldnt_validate_writes_nothing(self):
        view = self.view()
        before = self.path.read_text()
        for change, fragment in (
            (
                lambda c: c["adaptive_bulbs"].append("porch_light"),
                "adaptive bulb 'porch_light' is in no learning unit",
            ),
            (
                lambda c: c["adaptive_bulbs"].remove("reading_lamp"),
                "member 'reading_lamp' is switched-only",
            ),
            (lambda c: c["adaptive_bulbs"].append("kitchen_strip"), "not a bulb in this room"),
            (
                lambda c: c["learning_units"][1]["members"].append("chandelier_1"),
                "is in learning units",
            ),
            (lambda c: c.update(schedule_profile="midnight"), "'midnight' does not exist"),
            (lambda c: c.update(lux_sensors=["person.alex"]), "is not a sensor.*"),
            (
                lambda c: c["scenes"]["night"]["units"]["reading"].update(mode="dim"),
                "'dim' is not one of",
            ),
            (lambda c: c.update(dimmable=["chandelier_1"]), "Additional properties"),
        ):
            edited = copy.deepcopy(view["adaptive_config"])
            change(edited)
            with self.subTest(fragment):
                body = self.assert_error(self.save(edited, view["version"]), 422, "invalid_config")
                self.assertTrue(any(fragment in p for p in body["problems"]), body["problems"])
        self.assert_unchanged(before)

    def test_dimmability_is_not_the_configs_to_check(self):
        # A UI-time guard only: the save can't see HA, so an on/off-only bulb is the
        # editor's to keep out, and a config that lists one still saves.
        view = self.view()
        edited = copy.deepcopy(view["adaptive_config"])
        edited["adaptive_bulbs"].append("counter_lamp")
        edited["learning_units"].append(
            {"id": "counter", "name": "Counter", "members": ["counter_lamp"]}
        )
        self.ha.calls.clear()
        self.assertEqual(self.save(edited, view["version"]).status_code, 200)
        self.assertEqual(self.ha.calls, [])
        # And the editor's read still says it is locked, so the editor demotes it.
        counter = next(b for b in self.view()["bulbs"] if b["id"] == "counter_lamp")
        self.assertEqual((counter["tier"], counter["capability"]["locked"]), ("adaptive", True))

    def test_the_map_shows_whats_adaptive(self):
        living_view = next(
            r for r in self.client.get("/api/map").json()["rooms"] if r["id"] == "living_room"
        )
        self.assertEqual(
            living_view["adaptive_config"]["adaptive_bulbs"],
            ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"],
        )


if __name__ == "__main__":
    unittest.main()
