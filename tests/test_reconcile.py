"""app.core.config.reconcile_area_snapshot and snapshot_changes."""

import json
import random
import unittest

from app.core import config
from tests.fakes import committed_config, light

AREAS = [
    {"area_id": "living_room", "name": "Living Room"},
    {"area_id": "kitchen", "name": "Kitchen"},
]

# HA as of the committed snapshot: nothing has changed yet.
LIGHTS = [
    light("light.chandelier_1", "living_room", "Chandelier 1"),
    light("light.chandelier_2", "living_room", "Chandelier 2"),
    light("light.chandelier_3", "living_room", "Chandelier 3"),
    light("light.reading_lamp", "living_room", "Reading lamp"),
    light("light.kitchen_strip", "kitchen", "Kitchen strip"),
    light("light.porch", None, "Porch"),
]


def room(doc, room_id):
    return next(r for r in doc["rooms"] if r["id"] == room_id)


def bulb_ids(holder):
    return [b["id"] for b in holder["bulbs"]] if "bulbs" in holder else [b["id"] for b in holder]


def lights_with(*changes, drop=()):
    """LIGHTS with some entities re-homed ((entity_id, area_id) pairs) or dropped."""
    moved = dict(changes)
    result = [
        {**item, "area_id": moved.get(item["entity_id"], item["area_id"])}
        for item in LIGHTS
        if item["entity_id"] not in drop
    ]
    return result


class ReconcileTest(unittest.TestCase):
    def reconcile(self, areas=AREAS, lights=LIGHTS, doc=None):
        doc = committed_config() if doc is None else doc
        result = config.reconcile_area_snapshot(doc, areas, lights)
        # Whatever the reconcile does, its output must be a config that saves.
        config.validate(result)
        config.cross_check(result)
        return result

    def test_nothing_changed_means_nothing_changes(self):
        self.assertEqual(self.reconcile(), committed_config())

    def test_a_new_area_becomes_a_room(self):
        areas = [*AREAS, {"area_id": "home_office", "name": "Home Office"}]
        lights = [*LIGHTS, light("light.desk", "home_office", "Desk")]
        result = self.reconcile(areas, lights)
        office = room(result, "home_office")
        self.assertEqual(
            office,
            {
                "id": "home_office",
                "name": "Home Office",
                "ha_area_id": "home_office",
                "adaptive": False,
                "bulbs": [
                    {"id": "desk", "name": "Desk", "entity_id": "light.desk", "hard_switches": []}
                ],
                "switching_groups": [],
            },
        )
        self.assertEqual(result["rooms"][-1]["id"], "home_office")

    def test_an_empty_area_is_still_a_room(self):
        result = self.reconcile([*AREAS, {"area_id": "garage", "name": "Garage"}])
        self.assertEqual(room(result, "garage")["bulbs"], [])

    def test_new_rooms_are_appended_by_name(self):
        areas = [
            *AREAS,
            {"area_id": "z_area", "name": "Attic"},
            {"area_id": "a_area", "name": "Yard"},
        ]
        result = self.reconcile(areas)
        self.assertEqual(
            [r["id"] for r in result["rooms"]], ["living_room", "kitchen", "z_area", "a_area"]
        )

    def test_a_new_room_id_never_clashes(self):
        doc = committed_config()
        room(doc, "kitchen")["ha_area_id"] = "old_kitchen_area"
        areas = [
            {"area_id": "living_room", "name": "Living Room"},
            {"area_id": "old_kitchen_area", "name": "Kitchen"},
            {"area_id": "kitchen", "name": "Second kitchen"},
        ]
        result = self.reconcile(
            areas, lights_with(("light.kitchen_strip", "old_kitchen_area")), doc
        )
        self.assertEqual(room(result, "kitchen_2")["ha_area_id"], "kitchen")

    def test_a_renamed_area_renames_the_room_and_keeps_its_id(self):
        areas = [{"area_id": "living_room", "name": "Lounge"}, AREAS[1]]
        result = self.reconcile(areas)
        lounge = room(result, "living_room")
        self.assertEqual(lounge["name"], "Lounge")
        before = room(committed_config(), "living_room")
        self.assertEqual(lounge["switching_groups"], before["switching_groups"])
        self.assertEqual(lounge["adaptive_config"], before["adaptive_config"])

    def test_a_moved_bulb_follows_its_area_with_its_id_and_hard_switches(self):
        result = self.reconcile(lights=lights_with(("light.reading_lamp", "kitchen")))
        kitchen = room(result, "kitchen")
        self.assertEqual(bulb_ids(kitchen), ["kitchen_strip", "reading_lamp"])
        moved = kitchen["bulbs"][1]
        self.assertEqual(moved["hard_switches"][0]["id"], "reading_switch")
        self.assertNotIn("reading_lamp", bulb_ids(room(result, "living_room")))

    def test_lights_with_no_area_go_to_unassigned(self):
        lights = [*lights_with(("light.kitchen_strip", None)), light("light.new_one", None)]
        result = self.reconcile(lights=lights)
        self.assertEqual(bulb_ids(result["unassigned"]), ["porch", "kitchen_strip", "new_one"])
        self.assertEqual(room(result, "kitchen")["bulbs"], [])

    def test_a_light_in_an_unknown_area_goes_to_unassigned(self):
        result = self.reconcile(lights=lights_with(("light.kitchen_strip", "deleted_area")))
        self.assertIn("kitchen_strip", bulb_ids(result["unassigned"]))

    def test_an_unassigned_bulb_that_gains_an_area_moves_into_the_room(self):
        result = self.reconcile(lights=lights_with(("light.porch", "kitchen")))
        self.assertEqual(result["unassigned"], [])
        self.assertEqual(bulb_ids(room(result, "kitchen")), ["kitchen_strip", "porch"])

    def test_user_set_room_config_is_preserved(self):
        before = room(committed_config(), "living_room")
        after = room(self.reconcile(), "living_room")
        for key in ("adaptive", "switching_groups", "adaptive_config"):
            self.assertEqual(after[key], before[key])

    def test_members_that_leave_the_room_are_dropped_and_empty_sets_with_them(self):
        lights = lights_with(("light.reading_lamp", "kitchen"), drop={"light.chandelier_3"})
        living = room(self.reconcile(lights=lights), "living_room")
        self.assertEqual(
            [(g["id"], g["members"]) for g in living["switching_groups"]],
            [("living_all", ["chandelier_1", "chandelier_2"])],
        )
        self.assertEqual(living["switching_groups"][0]["hard_switches"][0]["id"], "living_all_off")
        self.assertEqual(
            [(u["id"], u["members"]) for u in living["adaptive_config"]["learning_units"]],
            [("chandelier", ["chandelier_1", "chandelier_2"])],
        )
        self.assertEqual(living["adaptive_config"]["lux_sensors"], ["sensor.living_room_lux"])

    def test_a_room_whose_area_is_gone_is_dropped(self):
        result = self.reconcile(
            [AREAS[1]],
            lights_with(*[(e["entity_id"], None) for e in LIGHTS if e["area_id"] == "living_room"]),
        )
        self.assertEqual([r["id"] for r in result["rooms"]], ["kitchen"])
        self.assertEqual(
            bulb_ids(result["unassigned"]),
            ["porch", "chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"],
        )

    def test_a_light_gone_from_ha_is_dropped(self):
        result = self.reconcile(lights=lights_with(drop={"light.porch"}))
        self.assertEqual(result["unassigned"], [])

    def test_bulb_names_follow_ha(self):
        lights = [
            {**item, "name": "Strip lights"} if item["entity_id"] == "light.kitchen_strip" else item
            for item in LIGHTS
        ]
        self.assertEqual(
            room(self.reconcile(lights=lights), "kitchen")["bulbs"][0]["name"], "Strip lights"
        )

    def test_a_new_bulb_id_avoids_the_soft_switch_namespace(self):
        lights = [
            *LIGHTS,
            light("light.living_all", "kitchen"),
            light("light.chandelier_1_", "kitchen"),
        ]
        kitchen = room(self.reconcile(lights=lights), "kitchen")
        self.assertEqual(bulb_ids(kitchen), ["kitchen_strip", "chandelier_1_2", "living_all_2"])

    def test_light_groups_are_not_bulbs(self):
        lights = [*LIGHTS, light("light.living_room_group", "living_room", is_group=True)]
        self.assertEqual(self.reconcile(lights=lights), committed_config())

    def test_the_token_is_never_carried_over(self):
        doc = committed_config()
        doc["homeassistant"]["access_token"] = "secret-token"
        self.assertNotIn("secret-token", json.dumps(self.reconcile(doc=doc)))

    def test_inputs_are_not_modified(self):
        doc, areas, lights = (
            committed_config(),
            json.loads(json.dumps(AREAS)),
            json.loads(json.dumps(LIGHTS)),
        )
        self.reconcile(areas, lights_with(("light.reading_lamp", "kitchen")), doc)
        self.assertEqual(doc, committed_config())
        self.assertEqual((areas, lights), (AREAS, LIGHTS))

    def test_is_idempotent(self):
        areas = [*AREAS, {"area_id": "garage", "name": "Garage"}]
        lights = [
            *lights_with(("light.reading_lamp", "kitchen"), drop={"light.chandelier_3"}),
            light("light.bench", "garage"),
        ]
        once = self.reconcile(areas, lights)
        self.assertEqual(self.reconcile(areas, lights, once), once)

    def test_does_not_depend_on_the_order_ha_lists_things(self):
        areas = [
            *AREAS,
            {"area_id": "garage", "name": "Garage"},
            {"area_id": "attic", "name": "Attic"},
        ]
        lights = [
            *LIGHTS,
            light("light.bench", "garage"),
            light("light.loft", "attic"),
            light("light.shed", None),
        ]
        expected = json.dumps(self.reconcile(areas, lights))
        shuffler = random.Random(7)
        for _ in range(5):
            shuffled_areas, shuffled_lights = areas[:], lights[:]
            shuffler.shuffle(shuffled_areas)
            shuffler.shuffle(shuffled_lights)
            self.assertEqual(json.dumps(self.reconcile(shuffled_areas, shuffled_lights)), expected)

    def test_builds_a_first_snapshot_from_a_blank_config(self):
        result = self.reconcile(doc=config.blank())
        self.assertEqual([r["id"] for r in result["rooms"]], ["kitchen", "living_room"])
        self.assertEqual(bulb_ids(result["unassigned"]), ["porch"])
        self.assertEqual(
            bulb_ids(room(result, "living_room")),
            ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"],
        )


class SnapshotChangesTest(unittest.TestCase):
    def test_no_changes(self):
        self.assertEqual(config.snapshot_changes(committed_config(), committed_config()), [])

    def test_describes_a_sync(self):
        areas = [
            {"area_id": "living_room", "name": "Lounge"},
            {"area_id": "garage", "name": "Garage"},
        ]
        lights = [
            *lights_with(
                ("light.reading_lamp", None),
                ("light.kitchen_strip", "garage"),
                drop={"light.chandelier_3"},
            ),
            light("light.bench", "garage", "Bench"),
        ]
        before = committed_config()
        after = config.reconcile_area_snapshot(before, areas, lights)
        self.assertEqual(
            config.snapshot_changes(before, after),
            [
                "room added: 'garage' (Garage, Area garage)",
                "room removed: 'kitchen' (Kitchen, Area kitchen)",
                "room renamed: 'living_room': 'Living Room' -> 'Lounge'",
                "bulb added: light.bench -> room 'garage'",
                "bulb removed: light.chandelier_3 (was in room 'living_room')",
                "bulb moved: light.kitchen_strip: room 'kitchen' -> room 'garage'",
                "bulb moved: light.reading_lamp: room 'living_room' -> unassigned",
                "switching group 'living_room/living_all': members dropped: "
                "chandelier_3, reading_lamp",
                "switching group removed: 'living_room/reading_corner'",
                "learning unit 'living_room/chandelier': members dropped: chandelier_3",
                "learning unit removed: 'living_room/reading'",
            ],
        )

    def test_says_what_a_removed_room_takes_with_it(self):
        after = config.reconcile_area_snapshot(committed_config(), [AREAS[1]], LIGHTS)
        self.assertIn(
            "room removed: 'living_room' (Living Room, Area living_room); drops 2 switching "
            "group(s) and its adaptive config",
            config.snapshot_changes(committed_config(), after),
        )


if __name__ == "__main__":
    unittest.main()
