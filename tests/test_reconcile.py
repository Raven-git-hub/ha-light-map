"""app.core.config.reconcile_area_snapshot and snapshot_changes."""

import json
import random
import unittest

from app.core import config
from tests.fakes import bulb, committed_config, light, without_keys

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


def renamed(old, new, lights=LIGHTS, **changes):
    """LIGHTS with entity `old` renamed to `new` in HA: the same registry entry, so the same
    match key."""
    return [
        {**item, "entity_id": new, **changes} if item["entity_id"] == old else item
        for item in lights
    ]


def reconcile(areas=AREAS, lights=LIGHTS, doc=None):
    doc = committed_config() if doc is None else doc
    result = config.reconcile_area_snapshot(doc, areas, lights)
    # Whatever the reconcile does, its output must be a config that saves.
    config.validate(result)
    config.cross_check(result)
    return result


def find(doc, entity_id):
    holders = [*(r["bulbs"] for r in doc["rooms"]), doc["unassigned"]]
    return next(b for bulbs in holders for b in bulbs if b["entity_id"] == entity_id)


class ReconcileTest(unittest.TestCase):
    def reconcile(self, areas=AREAS, lights=LIGHTS, doc=None):
        return reconcile(areas, lights, doc)

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
                    {
                        "id": "desk",
                        "name": "Desk",
                        "entity_id": "light.desk",
                        "ha_match_key": "reg_light_desk",
                        "hard_switches": [],
                    }
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

    def test_a_new_bulb_id_avoids_bulb_and_group_ids(self):
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


class StableIdentityTest(unittest.TestCase):
    """A bulb is its HA registry entry (ha_match_key), not its entity_id."""

    def test_a_renamed_entity_keeps_its_bulb(self):
        lights = renamed("light.reading_lamp", "light.reading_light", name="Reading light")
        result = reconcile(lights=lights)
        living, before = room(result, "living_room"), room(committed_config(), "living_room")
        self.assertEqual(bulb_ids(living), bulb_ids(before))
        self.assertEqual(
            living["bulbs"][3],
            {
                **before["bulbs"][3],
                "entity_id": "light.reading_light",
                "name": "Reading light",
            },
        )
        self.assertEqual(living["bulbs"][3]["hard_switches"][0]["id"], "reading_switch")
        self.assertEqual(living["switching_groups"], before["switching_groups"])
        self.assertEqual(living["adaptive_config"], before["adaptive_config"])
        self.assertEqual(
            config.snapshot_changes(committed_config(), result),
            [
                "bulb entity_id changed: light.reading_lamp -> light.reading_light",
                "bulb renamed: light.reading_light: 'Reading lamp' -> 'Reading light'",
            ],
        )

    def test_a_renamed_entity_that_also_moved(self):
        lights = renamed("light.reading_lamp", "light.kitchen_reader", area_id="kitchen")
        result = reconcile(lights=lights)
        moved = find(result, "light.kitchen_reader")
        self.assertEqual(
            (moved["id"], moved["hard_switches"][0]["id"]), ("reading_lamp", "reading_switch")
        )
        self.assertEqual(bulb_ids(room(result, "kitchen")), ["kitchen_strip", "reading_lamp"])
        self.assertEqual(
            config.snapshot_changes(committed_config(), result)[:2],
            [
                "bulb entity_id changed: light.reading_lamp -> light.kitchen_reader",
                "bulb moved: light.kitchen_reader: room 'living_room' -> room 'kitchen'",
            ],
        )

    def test_entities_that_swap_entity_ids_keep_their_bulbs(self):
        swap = {
            "light.chandelier_1": "light.chandelier_2",
            "light.chandelier_2": "light.chandelier_1",
        }
        lights = [{**x, "entity_id": swap.get(x["entity_id"], x["entity_id"])} for x in LIGHTS]
        result = reconcile(lights=lights)
        self.assertEqual(
            [(b["id"], b["entity_id"]) for b in room(result, "living_room")["bulbs"][:2]],
            [("chandelier_1", "light.chandelier_2"), ("chandelier_2", "light.chandelier_1")],
        )

    def test_a_genuinely_new_entity_is_added(self):
        result = reconcile(lights=[*LIGHTS, light("light.floor_lamp", "living_room")])
        added = find(result, "light.floor_lamp")
        self.assertEqual(
            (added["id"], added["ha_match_key"]), ("floor_lamp", "reg_light_floor_lamp")
        )
        self.assertEqual(
            config.snapshot_changes(committed_config(), result),
            ["bulb added: light.floor_lamp -> room 'living_room'"],
        )

    def test_a_removed_entity_is_dropped(self):
        result = reconcile(lights=lights_with(drop={"light.reading_lamp"}))
        self.assertNotIn("reading_lamp", bulb_ids(room(result, "living_room")))
        self.assertIn(
            "bulb removed: light.reading_lamp (was in room 'living_room'); "
            "drops hard switch(es): reading_switch",
            config.snapshot_changes(committed_config(), result),
        )

    def test_a_different_light_under_an_old_entity_id_is_a_new_bulb(self):
        # The old reading lamp was deleted in HA and another light now holds its entity_id:
        # a different registry entry, so a different bulb. A keyed bulb is never matched by
        # entity_id.
        lights = [
            {**x, "ha_match_key": "reg_replacement"}
            if x["entity_id"] == "light.reading_lamp"
            else x
            for x in LIGHTS
        ]
        result = reconcile(lights=lights)
        living = room(result, "living_room")
        replacement = find(result, "light.reading_lamp")
        self.assertEqual(
            replacement,
            {
                "id": "reading_lamp_2",
                "name": "Reading lamp",
                "entity_id": "light.reading_lamp",
                "ha_match_key": "reg_replacement",
                "hard_switches": [],
            },
        )
        # It gets a fresh id rather than the dropped bulb's, so it can't half-inherit the
        # old bulb's memberships.
        self.assertNotIn("reading_lamp", bulb_ids(living))
        members = [m for g in living["switching_groups"] for m in g["members"]]
        members += [m for u in living["adaptive_config"]["learning_units"] for m in u["members"]]
        self.assertNotIn("reading_lamp_2", members)
        self.assertNotIn("reading_lamp", members)
        changes = config.snapshot_changes(committed_config(), result)
        self.assertIn("bulb added: light.reading_lamp -> room 'living_room'", changes)
        self.assertIn(
            "bulb removed: light.reading_lamp (was in room 'living_room'); "
            "drops hard switch(es): reading_switch",
            changes,
        )

    def test_a_p1_snapshot_matches_by_entity_id_and_records_the_keys(self):
        before = without_keys(committed_config())
        result = reconcile(doc=before)
        self.assertEqual(result, committed_config())
        self.assertEqual(
            config.snapshot_changes(before, result), ["match key recorded for 6 bulb(s)"]
        )

    def test_without_a_key_a_rename_still_reads_as_remove_and_add(self):
        # Why the first sync after upgrading should come before renaming entities in HA.
        result = reconcile(
            lights=renamed("light.reading_lamp", "light.reading_light"),
            doc=without_keys(committed_config()),
        )
        self.assertEqual(find(result, "light.reading_light")["id"], "reading_light")

    def test_a_light_outside_the_registry_is_matched_by_entity_id(self):
        doc = committed_config()
        doc["unassigned"].append(bulb("legacy_yaml", key=None))
        lights = [*LIGHTS, light("light.legacy_yaml", None, "Legacy yaml", key=None)]
        result = reconcile(lights=lights, doc=doc)
        self.assertEqual(result, doc)
        self.assertNotIn("ha_match_key", find(result, "light.legacy_yaml"))

    def test_is_idempotent_after_a_rename(self):
        lights = renamed("light.reading_lamp", "light.reading_light")
        once = reconcile(lights=lights)
        self.assertEqual(reconcile(lights=lights, doc=once), once)


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
