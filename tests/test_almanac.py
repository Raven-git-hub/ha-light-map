"""The almanac (P5.2a): insert-only snapshots recorded and read back as the one in force,
the payload published to HA in exactly the contract P5.1's automations read (docs/DESIGN.md,
"The almanac contract"), the config's mode and knobs resolved into it, and the published
payload driving the generated scene and bridge as the contract says. Against a temporary
store and fakes; nothing here reaches a real HA."""

import asyncio
import copy
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.core import config, generate
from app.core.store import Store
from app.modules.adaptive import almanac
from app.modules.adaptive import generate as al
from tests import ha_script
from tests.fakes import FakeHA, adaptive_wired, committed_config

ROOM = "living_room"
UNITS = ("chandelier", "reading")
SECTIONS = config.SECTIONS
TODAY = date(2026, 6, 14)


def snapshot(
    section,
    unit,
    *,
    brightness=None,
    lux_target=None,
    valid_from="2026-06-13",
    ts=None,
    mode="bootstrap",
    days=3,
    **extra,
):
    return {
        "ts": ts or datetime(2026, 6, 13, 23, 15, tzinfo=timezone.utc),
        "room_id": ROOM,
        "section": section,
        "learning_unit_id": unit,
        "brightness": brightness,
        "on_fraction": None if brightness is None else 0.5,
        "lux_target": lux_target,
        "lux_margin": 5,
        "maintenance_enabled": True,
        "max_step_pct": 0.04,
        "trust_weight": None if brightness is None else 12.5,
        "days_contributing": 0 if brightness is None else 2,
        "high_confidence_days": 0,
        "days_analysed": days,
        "valid_from": valid_from,
        "mode": mode,
        **extra,
    }


def a_pass(levels=None, **kwargs):
    """One nightly pass's rows: every unit x section, `levels` {(section, unit): brightness}."""
    levels = levels or {}
    return [
        snapshot(s, u, brightness=levels.get((s, u)), lux_target=levels.get((s, "lux")), **kwargs)
        for s in SECTIONS
        for u in UNITS
    ]


def assert_contract(test, published, unit_ids):
    """Exactly the contract: each section's five keys, a target in one of its three forms
    for every unit, and the knobs' types and ranges; nothing else read by the automations."""
    attributes = published["attributes"]
    test.assertIsInstance(published["state"], str)
    test.assertLessEqual(len(published["state"]), 255)  # an HA state's limit
    test.assertEqual(
        [k for k in attributes if k in SECTIONS], list(SECTIONS), "every section, in order"
    )
    test.assertEqual(set(attributes) - set(SECTIONS), {"friendly_name", "mode", "days_analysed"})
    for section in SECTIONS:
        row = attributes[section]
        test.assertEqual(
            list(row), ["units", "lux_target", "lux_margin", "max_step_pct", "maintenance_enabled"]
        )
        test.assertEqual(sorted(row["units"]), sorted(unit_ids))
        for value in row["units"].values():
            test.assertTrue(
                value is None or value == "off" or (type(value) is int and 1 <= value <= 255),
                f"{section}: {value!r}",
            )
        test.assertTrue(row["lux_target"] is None or type(row["lux_target"]) in (int, float))
        test.assertIn(type(row["lux_margin"]), (int, float))
        test.assertTrue(0 < row["max_step_pct"] <= 1)
        test.assertIs(type(row["maintenance_enabled"]), bool)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        self.store.open()
        self.addCleanup(self.store.close)
        self.doc = config.loads(committed_config())
        self.room = self.doc["rooms"][0]


class RecordAndLatestTest(StoreCase):
    def test_record_then_latest_round_trips(self):
        rows = a_pass(
            {("night", "chandelier"): 90, ("night", "reading"): 40, ("night", "lux"): 12.5}
        )
        almanac.record(self.store, rows)
        current = almanac.latest(self.store, ROOM, today=TODAY)
        self.assertEqual(sorted(current), sorted(SECTIONS))
        night = current["night"]
        self.assertEqual(sorted(night), list(UNITS))
        self.assertEqual(night["chandelier"]["brightness"], 90)
        self.assertEqual(night["reading"]["brightness"], 40)
        self.assertEqual(night["reading"]["lux_target"], 12.5)
        self.assertIsNone(current["day"]["chandelier"]["brightness"])
        for key in ("on_fraction", "trust_weight", "days_contributing", "days_analysed", "mode"):
            self.assertEqual(
                night["chandelier"][key], rows[8][key] if key != "on_fraction" else 0.5
            )
        # The archive is the source of truth: a rebuild from it reads back the same.
        self.store.rebuild_from_csv()
        self.assertEqual(almanac.latest(self.store, ROOM, today=TODAY), current)
        # Nothing to record is nothing written.
        almanac.record(self.store, [])
        self.assertEqual(len(self.store.query("SELECT * FROM almanac")), 12)

    def test_insert_only_and_the_newest_pass_in_force_wins(self):
        first = datetime(2026, 6, 12, 23, 15, tzinfo=timezone.utc)
        second = first + timedelta(days=1)
        almanac.record(
            self.store, a_pass({("day", "reading"): 100}, ts=first, valid_from="2026-06-12")
        )
        almanac.record(
            self.store, a_pass({("day", "reading"): 150}, ts=second, valid_from="2026-06-13")
        )
        self.assertEqual(len(self.store.query("SELECT * FROM almanac")), 24)  # nothing replaced
        self.assertEqual(
            almanac.latest(self.store, ROOM, today=TODAY)["day"]["reading"]["brightness"], 150
        )
        # Before the second's valid_from, the first is in force.
        self.assertEqual(
            almanac.latest(self.store, ROOM, today=date(2026, 6, 12))["day"]["reading"][
                "brightness"
            ],
            100,
        )
        self.assertEqual(almanac.latest(self.store, ROOM, today=date(2026, 6, 11)), {})

    def test_a_learning_pass_waits_for_its_day(self):
        # Learning mode: valid two days on. Until then the bootstrap pass stays in force, as
        # the predecessor's almanac did.
        almanac.record(self.store, a_pass({("day", "reading"): 100}, valid_from="2026-06-13"))
        almanac.record(
            self.store,
            a_pass(
                {("day", "reading"): 180},
                valid_from="2026-06-16",
                mode="learning",
                ts=datetime(2026, 6, 14, 23, 15, tzinfo=timezone.utc),
            ),
        )
        for today, expected in ((date(2026, 6, 15), 100), (date(2026, 6, 16), 180)):
            current = almanac.latest(self.store, ROOM, today=today)
            self.assertEqual(current["day"]["reading"]["brightness"], expected, today)

    def test_rooms_and_units_are_their_own(self):
        almanac.record(self.store, a_pass({("day", "reading"): 100}))
        other = [{**row, "room_id": "kitchen", "brightness": 7} for row in a_pass()]
        almanac.record(self.store, other)
        self.assertEqual(
            almanac.latest(self.store, ROOM, today=TODAY)["day"]["reading"]["brightness"], 100
        )
        self.assertEqual(
            almanac.latest(self.store, "kitchen", today=TODAY)["day"]["reading"]["brightness"], 7
        )

    def test_the_store_refuses_what_isnt_a_snapshot(self):
        for change in (
            {"brightness": 256},
            {"brightness": -1},
            {"on_fraction": 1.5},
            {"days_analysed": -1},
        ):
            with self.subTest(change), self.assertRaises(ValueError):
                almanac.record(self.store, [{**snapshot("day", "reading"), **change}])


class PayloadTest(StoreCase):
    def payload(self, rows=(), today=TODAY):
        almanac.record(self.store, list(rows))
        return almanac.payload(self.room, almanac.latest(self.store, ROOM, today=today))

    def test_exactly_the_contract(self):
        published = self.payload(
            a_pass({("night", "chandelier"): 90, ("night", "lux"): 12.5, ("day", "reading"): 140})
        )
        assert_contract(self, published, UNITS)
        self.assertEqual(published["state"], "2026-06-13")
        self.assertEqual(
            published["attributes"]["night"],
            {
                "units": {"chandelier": 90, "reading": None},
                "lux_target": 12.5,
                "lux_margin": 5,
                "max_step_pct": 0.04,
                "maintenance_enabled": False,  # the config's: night has none by default
            },
        )
        self.assertEqual(
            published["attributes"]["day"]["units"], {"chandelier": None, "reading": 140}
        )
        self.assertEqual(
            {k: published["attributes"][k] for k in ("friendly_name", "mode", "days_analysed")},
            {"friendly_name": "AL Almanac living_room", "mode": "bootstrap", "days_analysed": 3},
        )

    def test_nothing_learned_yet(self):
        published = self.payload()
        assert_contract(self, published, UNITS)
        self.assertEqual(published["state"], "unknown")
        for section in SECTIONS:
            self.assertEqual(
                published["attributes"][section]["units"], {"chandelier": None, "reading": None}
            )
            self.assertIsNone(published["attributes"][section]["lux_target"])
        self.assertEqual(
            (published["attributes"]["mode"], published["attributes"]["days_analysed"]), (None, 0)
        )

    def test_a_unit_held_off_publishes_off_learned_or_not(self):
        scenes = self.room["adaptive_config"]["scenes"]
        scenes["sleep"]["units"]["chandelier"]["mode"] = "off"
        scenes["night"]["units"]["reading"]["mode"] = "off"
        published = self.payload(a_pass({("night", "reading"): 40}))
        assert_contract(self, published, UNITS)
        self.assertEqual(
            published["attributes"]["sleep"]["units"]["chandelier"], "off"
        )  # nothing learned
        self.assertEqual(published["attributes"]["night"]["units"]["reading"], "off")  # learned 40
        # Back to auto: the learned target was kept, and a republish is all it takes.
        scenes["night"]["units"]["reading"]["mode"] = "auto"
        current = almanac.latest(self.store, ROOM, today=TODAY)
        self.assertEqual(
            almanac.payload(self.room, current)["attributes"]["night"]["units"]["reading"], 40
        )

    def test_the_knobs_and_the_seed_are_the_configs_as_it_is_now(self):
        scenes = self.room["adaptive_config"]["scenes"]
        scenes["day"].update(
            lux_margin=8, max_step_pct=0.1, maintenance_enabled=False, lux_target_seed=300
        )
        scenes["afternoon"]["lux_target_seed"] = 150
        published = self.payload(
            a_pass({("afternoon", "lux"): 210.5, ("afternoon", "reading"): 30})
        )
        day = published["attributes"]["day"]
        self.assertEqual(
            (day["lux_margin"], day["max_step_pct"], day["maintenance_enabled"]), (8, 0.1, False)
        )
        self.assertEqual(day["lux_target"], 300)  # nothing learned: the seed
        self.assertEqual(published["attributes"]["afternoon"]["lux_target"], 210.5)  # learned wins

    def test_a_learned_zero_is_nothing_learned(self):
        published = self.payload(a_pass({("day", "reading"): 0, ("day", "chandelier"): 255}))
        self.assertEqual(
            published["attributes"]["day"]["units"], {"chandelier": 255, "reading": None}
        )
        for row, mode, expected in (
            ({"brightness": True}, "auto", None),
            ({"brightness": 12.0}, "auto", None),
            ({"brightness": 1}, "auto", 1),
            (None, "auto", None),
            ({"brightness": 9}, "off", "off"),
        ):
            self.assertEqual(almanac.target(row, mode), expected, (row, mode))

    def test_units_follow_the_config(self):
        # A unit redrawn away keeps its rows but isn't published; a new one has nothing yet,
        # and the room's lux target (on every unit's row) still comes through.
        rows = a_pass({("day", "reading"): 100, ("day", "lux"): 50.0})
        block = self.room["adaptive_config"]
        block["learning_units"] = [
            {
                "id": "chandelier",
                "name": "Chandelier",
                "members": ["chandelier_1", "chandelier_2", "chandelier_3"],
            },
            {"id": "lamp", "name": "Lamp", "members": ["reading_lamp"]},
        ]
        block["scenes"] = config.normalize_scenes(block["scenes"], block["learning_units"])
        published = self.payload(rows)
        assert_contract(self, published, ("chandelier", "lamp"))
        self.assertEqual(
            published["attributes"]["day"]["units"], {"chandelier": None, "lamp": None}
        )
        self.assertEqual(published["attributes"]["day"]["lux_target"], 50.0)

    def test_the_same_almanac_and_config_give_the_same_payload(self):
        rows = a_pass({("night", "chandelier"): 90})
        first = self.payload(rows)
        shuffled = copy.deepcopy(self.room)
        shuffled["adaptive_config"]["learning_units"].reverse()
        current = almanac.latest(self.store, ROOM, today=TODAY)
        self.assertEqual(almanac.payload(shuffled, current), first)
        self.assertEqual(list(first["attributes"]["night"]["units"]), ["chandelier", "reading"])


class PublishTest(StoreCase):
    def test_publish_sets_the_sensor(self):
        almanac.record(self.store, a_pass({("night", "chandelier"): 90}))
        published = almanac.payload(self.room, almanac.latest(self.store, ROOM, today=TODAY))
        ha = FakeHA()
        asyncio.run(almanac.publish(ha, ROOM, published))
        [(method, (entity_id, state, attributes))] = ha.writes
        self.assertEqual(
            (method, entity_id, state), ("set_state", "sensor.al_almanac_living_room", "2026-06-13")
        )
        self.assertEqual(attributes, published["attributes"])
        self.assertEqual(almanac.entity_id(ROOM), "sensor.al_almanac_living_room")


@unittest.skipUnless(ha_script.AVAILABLE, "jinja2 is not installed (test-only dependency)")
class ContractConformanceTest(StoreCase):
    """What the publish writes drives P5.1's automations as the contract says: the reader
    (the generated Jinja, run by tests/ha_script.py) and the writer agree."""

    def setUp(self):
        super().setUp()
        with adaptive_wired():
            lm = generate.generate(self.doc, "lm")
        self.automations = {a["id"]: a for a in al.generate(self.doc)["automation"]}
        self.ha = ha_script.HA(
            {
                "light.chandelier_1": ("on", {"brightness": 128}),
                "light.chandelier_2": ("off", {"brightness": None}),
                "light.chandelier_3": ("off", {"brightness": None}),
                "light.reading_lamp": ("on", {"brightness": 200}),
                "input_boolean.lm_soft_chandelier_1": "on",
                "input_boolean.lm_soft_chandelier_2": "off",
                "input_boolean.lm_soft_chandelier_3": "off",
                "input_boolean.lm_soft_reading_lamp": "on",
                "input_boolean.al_active_living_room": "off",
                "input_boolean.al_hold_living_room": "off",
                "input_select.al_scene_living_room": "afternoon",
            },
            automations=[a for a in lm["automation"] if a["id"].startswith("lm_bridge_")],
            options={"input_select.al_scene_living_room": list(SECTIONS)},
        )

    def publish(self, rows):
        almanac.record(self.store, rows)
        published = almanac.payload(self.room, almanac.latest(self.store, ROOM, today=TODAY))
        assert_contract(self, published, UNITS)
        self.ha.set(almanac.entity_id(ROOM), published["state"], published["attributes"])

    def light(self, bulb_id):
        return self.ha.state(f"light.{bulb_id}"), self.ha.attr(f"light.{bulb_id}", "brightness")

    def test_a_published_almanac_drives_the_crossover(self):
        self.room["adaptive_config"]["scenes"]["night"]["units"]["reading"]["mode"] = "off"
        self.publish(a_pass({("night", "chandelier"): 60, ("night", "reading"): 40}))
        self.ha.run(self.automations[f"al_scene_{ROOM}_night"], check_conditions=False)
        for bulb_id in ("chandelier_1", "chandelier_2", "chandelier_3"):
            self.assertEqual(self.light(bulb_id), ("on", 60), bulb_id)
        # Held off by the config, whatever was learned: the soft switch, then the bulb.
        self.assertEqual(self.ha.state("input_boolean.lm_soft_reading_lamp"), "off")
        self.assertEqual(self.light("reading_lamp"), ("off", None))

    def test_nothing_learned_leaves_the_bulbs_and_the_bridge_on_its_plain_path(self):
        self.publish(a_pass())
        before = copy.deepcopy(self.ha.states)
        self.ha.run(self.automations[f"al_scene_{ROOM}_day"], check_conditions=False)
        for bulb_id in ("chandelier_1", "chandelier_2", "reading_lamp"):
            self.assertEqual(self.ha.states[f"light.{bulb_id}"], before[f"light.{bulb_id}"])
        # Switched on by hand, a bulb with nothing learned comes back at its own level.
        self.ha.memory["light.chandelier_2"] = 33
        bridge = next(a for a in self.ha.automations if a["id"] == "lm_bridge_chandelier_2")
        self.ha.set("input_boolean.lm_soft_chandelier_2", "on")
        self.ha.run(bridge)
        self.assertEqual(self.light("chandelier_2"), ("on", 33))

    def test_maintenance_reads_the_published_knobs(self):
        scenes = self.room["adaptive_config"]["scenes"]
        scenes["afternoon"].update(lux_margin=2, max_step_pct=0.5)
        self.publish(a_pass({("afternoon", "lux"): 100.0, ("afternoon", "chandelier"): 128}))
        self.ha.set("sensor.living_room_lux", "50")
        self.ha.run(self.automations[f"al_maint_{ROOM}"])
        # Too dark by more than the margin: a step up of at most MAX_STEP (10).
        self.assertEqual(self.light("chandelier_1"), ("on", 138))
        self.assertEqual(self.light("reading_lamp"), ("on", 210))


if __name__ == "__main__":
    unittest.main()
