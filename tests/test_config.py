"""app.core.config: load pipeline, schema, cross-check, save, connection."""

import json
import math
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import config
from app.core.config import ConfigError
from app.core.ha import HAError
from tests.fakes import bulb, committed_config


def problems_of(call, *args) -> list[str]:
    try:
        call(*args)
    except ConfigError as exc:
        return exc.problems
    raise AssertionError("expected a ConfigError")


def living(doc):
    return doc["rooms"][0]


class SchemaTest(unittest.TestCase):
    def test_the_fixture_and_blank_are_valid(self):
        for doc in (committed_config(), config.blank()):
            config.validate(doc)
            config.cross_check(doc)

    def test_minimal_document_validates_after_defaults(self):
        doc = config.apply_defaults(
            {"version": 1, "rooms": [], "schedule_profiles": [{"id": "default"}]}
        )
        config.validate(doc)

    def assert_rejected(self, doc, fragment):
        problems = problems_of(config.validate, doc)
        self.assertTrue(any(fragment in p for p in problems), f"{fragment!r} not in {problems}")

    def test_rejects_a_bulb_that_is_not_a_light(self):
        doc = committed_config()
        living(doc)["bulbs"][0]["entity_id"] = "switch.chandelier_1"
        self.assert_rejected(doc, "rooms/0/bulbs/0/entity_id")

    def test_rejects_a_device_id_where_an_entity_id_belongs(self):
        doc = committed_config()
        living(doc)["adaptive_config"]["lux_sensors"] = ["3f2a9c1e0b7d4e5f8a6b2c1d0e9f8a7b"]
        self.assert_rejected(doc, "rooms/0/adaptive_config/lux_sensors/0")

    def test_adaptive_requires_adaptive_config(self):
        doc = committed_config()
        del living(doc)["adaptive_config"]
        self.assert_rejected(doc, "'adaptive_config' is a required property")

    def test_adaptive_config_is_forbidden_on_a_non_adaptive_room(self):
        doc = committed_config()
        living(doc)["adaptive"] = False
        self.assert_rejected(doc, "adaptive_config is only allowed when adaptive is true")

    def test_rejects_unknown_keys(self):
        for path in (["surprise"], ["homeassistant", "surprise"], ["rooms", 0, "surprise"]):
            doc = committed_config()
            target = doc
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = 1
            self.assert_rejected(doc, "Additional properties are not allowed")

    def test_rejects_bad_slugs_modes_and_urls(self):
        doc = committed_config()
        living(doc)["id"] = "Living Room"
        self.assert_rejected(doc, "rooms/0/id")
        doc = committed_config()
        living(doc)["switching_groups"][0]["hard_switches"][0]["mode"] = "hold"
        self.assert_rejected(doc, "switching_groups/0/hard_switches/0/mode")
        doc = committed_config()
        doc["homeassistant"]["base_url"] = "homeassistant.local:8123"
        self.assert_rejected(doc, "homeassistant/base_url")

    def test_rejects_an_unassigned_entry_that_is_not_a_bulb(self):
        doc = committed_config()
        doc["unassigned"].append({"id": "x", "entity_id": "light.x"})
        self.assert_rejected(doc, "unassigned/1")

    def test_a_missing_default_profile_is_named_plainly(self):
        doc = committed_config()
        doc["schedule_profiles"] = [{"id": "evening"}]
        self.assert_rejected(doc, "a schedule profile with id 'default' is required")

    def test_reports_every_problem_at_once(self):
        doc = committed_config()
        living(doc)["id"] = "Bad Id"
        doc["rooms"][1]["bulbs"][0]["entity_id"] = "sensor.x"
        self.assertEqual(len(problems_of(config.validate, doc)), 2)


class CrossCheckTest(unittest.TestCase):
    def assert_flags(self, doc, *fragments):
        problems = problems_of(config.cross_check, doc)
        for fragment in fragments:
            self.assertTrue(any(fragment in p for p in problems), f"{fragment!r} not in {problems}")

    def test_dangling_switching_group_member(self):
        doc = committed_config()
        living(doc)["switching_groups"][0]["members"].append("ghost")
        self.assert_flags(doc, "switching group 'living_all': member 'ghost' is not a bulb")

    def test_a_member_must_be_in_the_same_room(self):
        doc = committed_config()
        living(doc)["switching_groups"][0]["members"].append("kitchen_strip")
        self.assert_flags(
            doc, "member 'kitchen_strip' is not a bulb in this room (it is in room 'kitchen')"
        )

    def test_dangling_learning_unit_member(self):
        doc = committed_config()
        living(doc)["adaptive_config"]["learning_units"][0]["members"].append("porch")
        self.assert_flags(
            doc,
            "learning unit 'chandelier': member 'porch' is not a bulb in this room "
            "(it is in unassigned)",
        )

    def test_missing_default_profile(self):
        doc = committed_config()
        doc["schedule_profiles"] = [{"id": "evening"}]
        self.assert_flags(doc, "a profile with id 'default' is required")

    def test_bad_schedule_profile_reference(self):
        doc = committed_config()
        living(doc)["adaptive_config"]["schedule_profile"] = "midnight"
        self.assert_flags(doc, "adaptive_config.schedule_profile 'midnight' does not exist")

    def test_schedule_profile_defaults_to_default(self):
        doc = committed_config()
        del living(doc)["adaptive_config"]["schedule_profile"]
        config.cross_check(doc)

    def test_duplicate_room_and_area(self):
        doc = committed_config()
        doc["rooms"][1]["id"] = "living_room"
        doc["rooms"][1]["ha_area_id"] = "living_room"
        self.assert_flags(doc, "duplicate room id 'living_room'", "same HA Area 'living_room'")

    def test_bulb_ids_are_global(self):
        doc = committed_config()
        doc["rooms"][1]["bulbs"].append(bulb("chandelier_1", entity_id="light.other"))
        self.assert_flags(doc, "soft-switch id 'chandelier_1' is used more than once")

    def test_bulb_and_group_ids_share_the_soft_switch_namespace(self):
        doc = committed_config()
        doc["unassigned"].append(bulb("living_all", entity_id="light.other"))
        self.assert_flags(doc, "soft-switch id 'living_all'", "share one namespace")

    def test_an_entity_is_in_one_place(self):
        doc = committed_config()
        doc["unassigned"].append(bulb("porch_again", entity_id="light.kitchen_strip"))
        self.assert_flags(doc, "entity_id 'light.kitchen_strip' is used more than once")

    def test_hard_switch_ids_are_global(self):
        doc = committed_config()
        doc["rooms"][1]["bulbs"][0]["hard_switches"].append(
            {"id": "reading_switch", "name": "Dup", "source": "switch.x", "mode": "toggle"}
        )
        self.assert_flags(doc, "hard-switch id 'reading_switch' is used more than once")

    def test_learning_units_partition_the_room(self):
        doc = committed_config()
        units = living(doc)["adaptive_config"]["learning_units"]
        units[1]["members"].append("chandelier_1")
        self.assert_flags(
            doc, "bulb 'chandelier_1' is in learning units 'chandelier' and 'reading'"
        )
        units[1]["id"] = "chandelier"
        self.assert_flags(doc, "duplicate learning-unit id 'chandelier'")

    def test_learning_unit_ids_are_scoped_to_the_room(self):
        doc = committed_config()
        kitchen = doc["rooms"][1]
        kitchen["adaptive"] = True
        kitchen["adaptive_config"] = {
            "learning_units": [{"id": "chandelier", "name": "Strip", "members": ["kitchen_strip"]}]
        }
        config.cross_check(doc)

    def test_switching_groups_may_overlap(self):
        doc = committed_config()
        self.assertIn("reading_lamp", living(doc)["switching_groups"][0]["members"])
        self.assertIn("reading_lamp", living(doc)["switching_groups"][1]["members"])
        config.cross_check(doc)

    def test_reports_every_problem_at_once(self):
        doc = committed_config()
        living(doc)["switching_groups"][0]["members"].append("ghost")
        living(doc)["adaptive_config"]["schedule_profile"] = "midnight"
        self.assertEqual(len(problems_of(config.cross_check, doc)), 2)


class MigrateAndDefaultsTest(unittest.TestCase):
    def test_current_version_passes_through(self):
        doc = committed_config()
        self.assertEqual(config.migrate(doc), doc)

    def test_refuses_a_newer_version(self):
        doc = committed_config()
        doc["version"] = config.CURRENT_VERSION + 1
        self.assertIn("newer Light Map", problems_of(config.migrate, doc)[0])

    def test_refuses_a_missing_or_non_integer_version(self):
        for version in (None, "1", True, 1.0):
            doc = committed_config()
            doc["version"] = version
            problems_of(config.migrate, doc)

    def test_runs_registered_steps_in_order(self):
        def step(doc):
            doc["rooms"] = doc.pop("legacy_rooms")
            return doc

        old = {"version": 0, "legacy_rooms": [], "schedule_profiles": [{"id": "default"}]}
        with mock.patch.dict(config._MIGRATIONS, {0: step}):
            migrated = config.migrate(old)
        self.assertEqual(migrated["version"], 1)
        self.assertEqual(migrated["rooms"], [])
        self.assertIn("legacy_rooms", old)

    def test_a_version_with_no_migration_is_refused(self):
        self.assertIn("no migration", problems_of(config.migrate, {"version": 0})[0])

    def test_defaults_fill_structure_without_touching_the_input(self):
        doc = {
            "version": 1,
            "rooms": [
                {
                    "id": "r",
                    "name": "R",
                    "ha_area_id": "r",
                    "adaptive": True,
                    "bulbs": [{"id": "b", "name": "B", "entity_id": "light.b"}],
                    "adaptive_config": {},
                }
            ],
            "schedule_profiles": [{"id": "default"}],
        }
        snapshot = json.dumps(doc)
        filled = config.apply_defaults(doc)
        self.assertEqual(json.dumps(doc), snapshot)
        self.assertEqual(filled["homeassistant"], {"verify_ssl": True})
        self.assertEqual(filled["unassigned"], [])
        room = filled["rooms"][0]
        self.assertEqual(room["switching_groups"], [])
        self.assertEqual(room["bulbs"][0]["hard_switches"], [])
        self.assertEqual(
            room["adaptive_config"],
            {
                "lux_sensors": [],
                "presence_sensors": [],
                "schedule_profile": "default",
                "learning_units": [],
            },
        )

    def test_defaults_tolerate_wrong_types_and_leave_them_for_validation(self):
        filled = config.apply_defaults({"version": 1, "rooms": "nope", "homeassistant": 3})
        self.assertEqual(filled["rooms"], "nope")
        self.assertTrue(problems_of(config.validate, filled))


class LoadSaveTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "config.json"

    def tearDown(self):
        for child in self.dir.iterdir():
            child.unlink()
        self.dir.rmdir()

    def test_round_trip(self):
        config.save(self.path, committed_config())
        self.assertEqual(config.load(self.path), config.loads(committed_config()))

    def test_load_reports_a_missing_file_and_bad_json(self):
        self.assertIn("no config at", problems_of(config.load, self.path)[0])
        self.path.write_text("{not json")
        self.assertIn("not valid JSON", problems_of(config.load, self.path)[0])
        self.path.write_text("[]")
        self.assertIn("must be a JSON object", problems_of(config.load, self.path)[0])

    def test_load_ignores_a_token_in_the_file(self):
        doc = committed_config()
        doc["homeassistant"]["access_token"] = "secret-token"
        self.path.write_text(json.dumps(doc))
        with self.assertLogs("app.core.config", "WARNING"):
            loaded = config.load(self.path)
        self.assertNotIn("access_token", loaded["homeassistant"])

    def test_save_never_persists_the_token(self):
        doc = committed_config()
        doc["homeassistant"]["access_token"] = "secret-token"
        config.save(self.path, doc)
        self.assertNotIn("secret-token", self.path.read_text())
        self.assertEqual(doc["homeassistant"]["access_token"], "secret-token")

    def test_redact_copies(self):
        doc = committed_config()
        doc["homeassistant"]["access_token"] = "secret-token"
        self.assertNotIn("access_token", config.redact(doc)["homeassistant"])
        self.assertIn("access_token", doc["homeassistant"])

    def test_save_writes_the_normalised_document(self):
        config.save(
            self.path, {"version": 1, "rooms": [], "schedule_profiles": [{"id": "default"}]}
        )
        self.assertEqual(json.loads(self.path.read_text())["unassigned"], [])

    def assert_save_refused_and_file_untouched(self, doc):
        config.save(self.path, committed_config())
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError):
            config.save(self.path, doc)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["config.json"])

    def test_validate_then_write_refuses_a_schema_failure(self):
        doc = committed_config()
        living(doc)["bulbs"][0]["entity_id"] = "switch.nope"
        self.assert_save_refused_and_file_untouched(doc)

    def test_validate_then_write_refuses_a_cross_check_failure(self):
        doc = committed_config()
        living(doc)["switching_groups"][0]["members"].append("ghost")
        self.assert_save_refused_and_file_untouched(doc)

    def test_validate_then_write_refuses_what_load_would_refuse(self):
        doc = committed_config()
        doc["version"] = 99  # passes the schema (minimum 1), but no Light Map can load it
        self.assert_save_refused_and_file_untouched(doc)

    def test_validate_then_write_refuses_what_json_cannot_carry(self):
        doc = committed_config()
        doc["learning"] = {"rate": math.nan}
        self.assert_save_refused_and_file_untouched(doc)

    def test_save_keeps_the_file_mode(self):
        config.save(self.path, committed_config())
        os.chmod(self.path, 0o640)
        config.save(self.path, committed_config())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_a_failed_write_leaves_no_temp_file(self):
        config.save(self.path, committed_config())
        with mock.patch("app.core.config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.save(self.path, committed_config())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["config.json"])


class ConnectionTest(unittest.TestCase):
    def env(self, **values):
        return mock.patch.dict(os.environ, values, clear=True)

    def test_environment_url_overrides_the_config(self):
        with self.env(LM_HA_BASE_URL="https://ha.example:8123", LM_HA_TOKEN="t"):
            conn = config.connection(committed_config())
        self.assertEqual(
            conn, {"base_url": "https://ha.example:8123", "token": "t", "verify_ssl": True}
        )

    def test_falls_back_to_the_config_url(self):
        with self.env(LM_HA_BASE_URL="", LM_HA_TOKEN="t"):
            conn = config.connection(committed_config())
        self.assertEqual(conn["base_url"], "http://ha.local:8123")

    def test_needs_a_url_and_a_token(self):
        with self.env(LM_HA_TOKEN="t"):
            problems_of(config.connection, config.blank())
        with self.env(LM_HA_BASE_URL="http://ha:8123"), self.assertRaises(HAError):
            config.connection(config.blank())


if __name__ == "__main__":
    unittest.main()
