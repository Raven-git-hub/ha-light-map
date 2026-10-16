"""app.core.inventory against a fake HAClient."""

import copy
import unittest

from app.core import config, inventory
from app.core.config import ConfigError
from app.core.inventory import InventoryError
from tests.fakes import FakeHA, committed_config, registry_id


def by_id(lights):
    return {item["entity_id"]: item for item in lights}


class DiscoverTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_light_joined_to_its_effective_area(self):
        lights = await inventory.discover_lights(FakeHA())
        self.assertEqual(
            [(x["entity_id"], x["area_id"], x["area_inherited"]) for x in lights],
            [
                ("light.chandelier_1", "living_room", True),
                ("light.chandelier_2", "living_room", True),
                ("light.chandelier_3", "living_room", True),
                ("light.counter_lamp", "kitchen", False),
                ("light.kitchen_strip", "kitchen", True),
                ("light.legacy_yaml", None, False),
                ("light.living_room_group", "living_room", False),
                ("light.porch", None, False),
                ("light.reading_lamp", "living_room", False),
            ],
        )

    async def test_light_records(self):
        lights = by_id(await inventory.discover_lights(FakeHA()))
        self.assertEqual(
            lights["light.chandelier_3"],
            {
                "entity_id": "light.chandelier_3",
                "ha_match_key": "reg_light_chandelier_3",
                "name": "Chandelier 3",
                "state": "unavailable",
                "area_id": "living_room",
                "area_inherited": True,
                "in_registry": True,
                "is_group": False,
            },
        )
        self.assertFalse(lights["light.legacy_yaml"]["in_registry"])
        self.assertIsNone(lights["light.legacy_yaml"]["ha_match_key"])
        self.assertTrue(lights["light.living_room_group"]["is_group"])

    async def test_device_ids_never_leave_the_join(self):
        lights = await inventory.discover_lights(FakeHA())
        self.assertNotIn("dev_", repr(lights))

    def test_hue_rooms_count_as_groups(self):
        self.assertTrue(inventory.is_light_group({"is_hue_group": True}))
        self.assertFalse(inventory.is_light_group({"friendly_name": "Lamp"}))


class ReadRoomsTest(unittest.IsolatedAsyncioTestCase):
    async def test_rooms_from_areas_plus_unassigned(self):
        rooms = await inventory.read_rooms(FakeHA())
        self.assertEqual(
            [(r["area_id"], r["name"], [b["entity_id"] for b in r["bulbs"]]) for r in rooms],
            [
                ("garage", "Garage", []),
                ("kitchen", "Kitchen", ["light.counter_lamp", "light.kitchen_strip"]),
                (
                    "living_room",
                    "Living Room",
                    [
                        "light.chandelier_1",
                        "light.chandelier_2",
                        "light.chandelier_3",
                        "light.reading_lamp",
                    ],
                ),
                (None, "Unassigned", ["light.legacy_yaml", "light.porch"]),
            ],
        )

    async def test_unassigned_is_always_present(self):
        ha = FakeHA(states=[], entities=[])
        rooms = await inventory.read_rooms(ha)
        self.assertEqual(rooms[-1], {"area_id": None, "name": "Unassigned", "bulbs": []})


class SyncSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_ha_and_reconciles_without_writing(self):
        ha = FakeHA()
        result = await inventory.sync_snapshot(ha, committed_config())
        self.assertEqual(ha.writes, [])
        expected = config.reconcile_area_snapshot(
            committed_config(), ha.areas, await inventory.discover_lights(FakeHA())
        )
        self.assertEqual(result, expected)
        self.assertEqual(
            config.snapshot_changes(committed_config(), result),
            [
                "room added: 'garage' (Garage, Area garage)",
                "bulb added: light.counter_lamp -> room 'kitchen'",
                "bulb added: light.legacy_yaml -> unassigned",
            ],
        )
        config.cross_check(result)

    async def test_follows_an_entity_renamed_in_ha(self):
        ha = FakeHA()
        # HA renames in place: same registry entry (same id), new entity_id.
        for record in (*ha.entities, *ha.states):
            if record["entity_id"] == "light.reading_lamp":
                record["entity_id"] = "light.reading_light"
        result = await inventory.sync_snapshot(ha, committed_config())
        living = result["rooms"][0]
        moved = next(b for b in living["bulbs"] if b["id"] == "reading_lamp")
        self.assertEqual(moved["entity_id"], "light.reading_light")
        self.assertEqual(moved["hard_switches"][0]["id"], "reading_switch")
        self.assertEqual(
            living["switching_groups"], committed_config()["rooms"][0]["switching_groups"]
        )
        self.assertIn(
            "bulb entity_id changed: light.reading_lamp -> light.reading_light",
            config.snapshot_changes(committed_config(), result),
        )

    async def test_a_light_outside_the_registry_gets_no_key(self):
        result = await inventory.sync_snapshot(FakeHA(), committed_config())
        legacy = next(b for b in result["unassigned"] if b["entity_id"] == "light.legacy_yaml")
        self.assertNotIn("ha_match_key", legacy)


class MoveBulbTest(unittest.IsolatedAsyncioTestCase):
    async def area_of(self, ha, entity_id):
        return by_id(await inventory.discover_lights(ha))[entity_id]["area_id"]

    async def test_moves_a_bulb_with_its_own_area(self):
        ha = FakeHA()
        await inventory.move_bulb(ha, "light.reading_lamp", "kitchen")
        self.assertEqual(ha.writes, [("set_entity_area", ("light.reading_lamp", "kitchen"))])
        self.assertEqual(await self.area_of(ha, "light.reading_lamp"), "kitchen")

    async def test_moves_a_bulb_that_inherits_its_area(self):
        ha = FakeHA()
        await inventory.move_bulb(ha, "light.chandelier_1", "garage")
        self.assertEqual(ha.writes, [("set_entity_area", ("light.chandelier_1", "garage"))])
        self.assertEqual(await self.area_of(ha, "light.chandelier_1"), "garage")
        self.assertEqual(await self.area_of(ha, "light.chandelier_2"), "living_room")

    async def test_unassigns_a_bulb_whose_device_has_no_area(self):
        ha = FakeHA()
        await inventory.move_bulb(ha, "light.reading_lamp", None)
        self.assertEqual(ha.writes, [("set_entity_area", ("light.reading_lamp", None))])
        self.assertIsNone(await self.area_of(ha, "light.reading_lamp"))

    async def test_a_bulb_already_there_is_not_written(self):
        ha = FakeHA()
        await inventory.move_bulb(ha, "light.chandelier_1", "living_room")
        await inventory.move_bulb(ha, "light.porch", None)
        self.assertEqual(ha.writes, [])

    async def assert_refused(self, ha, entity_id, area_id, fragment):
        with self.assertRaises(InventoryError) as caught:
            await inventory.move_bulb(ha, entity_id, area_id)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(ha.writes, [])

    async def test_refuses_an_unknown_area(self):
        await self.assert_refused(FakeHA(), "light.reading_lamp", "attic", "no HA Area 'attic'")

    async def test_refuses_a_light_with_no_registry_entry(self):
        await self.assert_refused(
            FakeHA(), "light.legacy_yaml", "kitchen", "no entity-registry entry"
        )

    async def test_refuses_a_non_light(self):
        await self.assert_refused(FakeHA(), "sensor.living_room_lux", "kitchen", "is not a light")

    async def test_refuses_to_unassign_a_bulb_its_device_would_keep(self):
        await self.assert_refused(
            FakeHA(), "light.chandelier_1", None, "inherits Area 'living_room' from its device"
        )
        # Its own Area is an override; clearing it would drop it back into the device's.
        await self.assert_refused(
            FakeHA(), "light.counter_lamp", None, "inherits Area 'living_room' from its device"
        )

    async def test_a_write_that_does_not_land_is_an_error(self):
        ha = FakeHA(drop_writes=True)
        with self.assertRaises(InventoryError) as caught:
            await inventory.move_bulb(ha, "light.reading_lamp", "kitchen")
        self.assertIn("did not land", str(caught.exception))
        self.assertEqual(ha.writes, [("set_entity_area", ("light.reading_lamp", "kitchen"))])

    async def test_verifies_by_re_reading(self):
        ha = FakeHA()
        await inventory.move_bulb(ha, "light.reading_lamp", "kitchen")
        write = ha.calls.index(("set_entity_area", ("light.reading_lamp", "kitchen")))
        self.assertIn(("get_entity_registry_entry", ("light.reading_lamp",)), ha.calls[write + 1 :])

    async def test_a_different_light_under_the_bulbs_entity_id_is_refused(self):
        ha = FakeHA()
        # The bulb's own light was renamed; another light took its old entity_id.
        entry = next(e for e in ha.entities if e["entity_id"] == "light.reading_lamp")
        entry["entity_id"] = "light.reading_lamp_old"
        ha.entities.append({"entity_id": "light.reading_lamp", "id": "reg_other", "area_id": None})
        key = registry_id("light.reading_lamp")
        with self.assertRaises(InventoryError) as caught:
            await inventory.move_bulb(ha, "light.reading_lamp", "kitchen", match_key=key)
        self.assertIn("is not this bulb's light any more", str(caught.exception))
        self.assertEqual(ha.writes, [])
        # Without a key (a bulb outside the registry when snapshotted) there is nothing to
        # compare, and entity_id is all there is.
        self.assertIsNone(await inventory.check_move(ha, "light.reading_lamp", "kitchen"))

    async def test_check_move_answers_in_advance_without_writing(self):
        ha = FakeHA()
        self.assertIsNone(await inventory.check_move(ha, "light.reading_lamp", "kitchen"))
        self.assertIsNone(await inventory.check_move(ha, "light.porch", None))
        for entity_id, area_id, fragment in (
            ("light.reading_lamp", "attic", "no HA Area 'attic'"),
            ("light.legacy_yaml", "kitchen", "no entity-registry entry"),
            ("light.chandelier_1", None, "inherits Area 'living_room' from its device"),
        ):
            self.assertIn(fragment, await inventory.check_move(ha, entity_id, area_id))
        self.assertEqual(ha.writes, [])


class AreaWritesTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_room_creates_an_area(self):
        ha = FakeHA()
        area = await inventory.create_room(ha, "  Home Office ")
        self.assertEqual(ha.writes, [("create_area", ("Home Office",))])
        self.assertEqual((area["area_id"], area["name"]), ("home_office", "Home Office"))
        rooms = await inventory.read_rooms(ha)
        self.assertIn("home_office", [r["area_id"] for r in rooms])
        synced = await inventory.sync_snapshot(ha, committed_config())
        self.assertEqual(synced["rooms"][-1]["ha_area_id"], "home_office")

    async def test_create_room_refuses_a_taken_or_empty_name(self):
        for name in ("living room", "LivingRoom", "   "):
            ha = FakeHA()
            with self.assertRaises(InventoryError):
                await inventory.create_room(ha, name)
            self.assertEqual(ha.writes, [])

    async def test_create_room_that_does_not_land_is_an_error(self):
        with self.assertRaises(InventoryError):
            await inventory.create_room(FakeHA(drop_writes=True), "Attic")

    async def test_rename_room_renames_the_area(self):
        ha = FakeHA()
        area = await inventory.rename_room(ha, "living_room", "Lounge")
        self.assertEqual(ha.writes, [("rename_area", ("living_room", "Lounge"))])
        self.assertEqual(area["name"], "Lounge")
        synced = await inventory.sync_snapshot(ha, committed_config())
        self.assertEqual(
            [(r["id"], r["name"]) for r in synced["rooms"][:1]], [("living_room", "Lounge")]
        )

    async def test_rename_room_refusals(self):
        for area_id, name, fragment in (
            ("attic", "Attic", "no HA Area 'attic'"),
            ("living_room", "kitchen", "already has an Area named 'Kitchen'"),
            ("living_room", "", "needs a name"),
        ):
            ha = FakeHA()
            with self.assertRaises(InventoryError) as caught:
                await inventory.rename_room(ha, area_id, name)
            self.assertIn(fragment, str(caught.exception))
            self.assertEqual(ha.writes, [])

    async def test_rename_to_the_same_name_does_not_write(self):
        ha = FakeHA()
        await inventory.rename_room(ha, "kitchen", "Kitchen")
        self.assertEqual(ha.writes, [])

    async def test_rename_that_does_not_land_is_an_error(self):
        with self.assertRaises(InventoryError):
            await inventory.rename_room(FakeHA(drop_writes=True), "kitchen", "Galley")


class SwitchingGroupEditTest(unittest.TestCase):
    GROUP = {"id": "chandelier", "name": "Chandelier", "members": ["chandelier_1", "chandelier_2"]}

    def test_upsert_adds_a_group_without_touching_the_input(self):
        doc = committed_config()
        result = inventory.upsert_switching_group(doc, "living_room", self.GROUP)
        self.assertEqual(doc, committed_config())
        groups = result["rooms"][0]["switching_groups"]
        self.assertEqual([g["id"] for g in groups], ["living_all", "reading_corner", "chandelier"])
        config.cross_check(result)
        groups[-1]["members"].append("x")
        self.assertEqual(self.GROUP["members"], ["chandelier_1", "chandelier_2"])

    def test_upsert_replaces_by_id_in_place(self):
        group = {"id": "living_all", "name": "Everything", "members": ["chandelier_1"]}
        result = inventory.upsert_switching_group(committed_config(), "living_room", group)
        groups = result["rooms"][0]["switching_groups"]
        self.assertEqual([g["id"] for g in groups], ["living_all", "reading_corner"])
        self.assertEqual(groups[0], group)

    def test_upsert_into_a_room_with_no_groups_yet(self):
        doc = committed_config()
        del doc["rooms"][1]["switching_groups"]
        group = {"id": "strip", "name": "Strip", "members": ["kitchen_strip"]}
        result = inventory.upsert_switching_group(doc, "kitchen", group)
        self.assertEqual(result["rooms"][1]["switching_groups"], [group])

    def test_upsert_refusals(self):
        with self.assertRaises(ConfigError):
            inventory.upsert_switching_group(committed_config(), "attic", self.GROUP)
        with self.assertRaises(ConfigError):
            inventory.upsert_switching_group(committed_config(), "living_room", {"name": "No id"})

    def test_a_bad_member_is_caught_when_saving(self):
        group = {"id": "bad", "name": "Bad", "members": ["kitchen_strip"]}
        result = inventory.upsert_switching_group(committed_config(), "living_room", group)
        with self.assertRaises(ConfigError):
            config.cross_check(result)

    def test_remove(self):
        doc = committed_config()
        result = inventory.remove_switching_group(doc, "living_room", "reading_corner")
        self.assertEqual([g["id"] for g in result["rooms"][0]["switching_groups"]], ["living_all"])
        self.assertEqual(doc, committed_config())
        with self.assertRaises(ConfigError):
            inventory.remove_switching_group(doc, "living_room", "nope")
        with self.assertRaises(ConfigError):
            inventory.remove_switching_group(doc, "attic", "living_all")


class SnapshotCommitTest(unittest.TestCase):
    """An HA edit made from the Map lands in the snapshot as exactly that change."""

    def test_the_snapshot_reproduces_itself(self):
        doc = config.loads(committed_config())
        areas, lights = config.snapshot_as_ha(doc)
        self.assertEqual(config.reconcile_area_snapshot(doc, areas, lights), doc)

    def test_a_move_out_of_a_room_prunes_its_memberships(self):
        doc = committed_config()
        result = inventory.snapshot_with_move(doc, "reading_lamp", "kitchen")
        self.assertEqual(doc, committed_config())
        self.assertEqual(
            config.snapshot_changes(doc, result),
            [
                "bulb moved: light.reading_lamp: room 'living_room' -> room 'kitchen'",
                "switching group 'living_room/living_all': members dropped: reading_lamp",
                "switching group removed: 'living_room/reading_corner'",
                "learning unit removed: 'living_room/reading'",
            ],
        )
        moved = result["rooms"][1]["bulbs"][-1]
        # Its id and hard switches travel with it.
        self.assertEqual(moved["id"], "reading_lamp")
        self.assertEqual(moved["hard_switches"][0]["id"], "reading_switch")
        config.checked(result)

    def test_a_move_to_unassigned_and_back(self):
        result = inventory.snapshot_with_move(committed_config(), "kitchen_strip", None)
        self.assertEqual([b["id"] for b in result["unassigned"]], ["porch", "kitchen_strip"])
        back = inventory.snapshot_with_move(result, "kitchen_strip", "kitchen")
        self.assertEqual(back["rooms"][1]["bulbs"][0]["id"], "kitchen_strip")

    def test_nothing_else_ha_changed_is_picked_up(self):
        # The snapshot is built from the config alone, so a light renamed in HA meanwhile
        # waits for a reviewed sync.
        doc = committed_config()
        result = inventory.snapshot_with_move(doc, "porch", "kitchen")
        self.assertEqual(
            config.snapshot_changes(doc, result),
            ["bulb moved: light.porch: unassigned -> room 'kitchen'"],
        )

    def test_an_added_and_a_renamed_area(self):
        doc = committed_config()
        added = inventory.snapshot_with_area(doc, {"area_id": "garage", "name": "Garage"})
        self.assertEqual(
            config.snapshot_changes(doc, added), ["room added: 'garage' (Garage, Area garage)"]
        )
        renamed = inventory.snapshot_with_area(added, {"area_id": "kitchen", "name": "Galley"})
        self.assertEqual(
            config.snapshot_changes(added, renamed),
            ["room renamed: 'kitchen': 'Kitchen' -> 'Galley'"],
        )
        self.assertEqual([r["id"] for r in renamed["rooms"]], ["living_room", "kitchen", "garage"])

    def test_a_dropped_group_names_the_hard_switches_it_takes(self):
        doc = committed_config()
        doc["rooms"][1]["switching_groups"] = [
            {
                "id": "kitchen_all",
                "name": "Kitchen all",
                "members": ["kitchen_strip"],
                "hard_switches": [
                    {
                        "id": "kitchen_wall",
                        "name": "Wall",
                        "source": {"type": "state", "entity_id": "switch.reading_relay"},
                        "mode": "follow",
                    }
                ],
            }
        ]
        result = inventory.snapshot_with_move(doc, "kitchen_strip", None)
        self.assertIn(
            "switching group removed: 'kitchen/kitchen_all'; drops hard switch(es): kitchen_wall",
            config.snapshot_changes(doc, result),
        )

    def test_a_room_name_with_stray_spaces_is_not_renamed_by_an_edit(self):
        doc = committed_config()
        doc["rooms"][1]["name"] = "Kitchen "  # a hand edit; the schema allows it
        result = inventory.snapshot_with_area(doc, {"area_id": "attic", "name": "Attic"})
        self.assertEqual(
            config.snapshot_changes(doc, result), ["room added: 'attic' (Attic, Area attic)"]
        )

    def test_unknown_names(self):
        with self.assertRaises(inventory.NotFound):
            inventory.snapshot_with_move(committed_config(), "ghost", None)
        with self.assertRaises(inventory.NotFound):
            inventory.snapshot_with_move(committed_config(), "porch", "attic")


class MapEditTest(unittest.TestCase):
    BUTTON = {
        "name": "Porch button",
        "source": {"type": "event", "event_type": "zha_event", "event_data": {"command": "on"}},
        "mode": "toggle",
    }

    def test_adaptive_on_writes_a_minimal_config(self):
        result = inventory.set_adaptive(committed_config(), "kitchen", True)
        self.assertEqual(result["rooms"][1]["adaptive_config"], {})
        saved = config.checked(result)
        self.assertEqual(saved["rooms"][1]["adaptive_config"]["schedule_profile"], "default")
        # An existing adaptive config is kept.
        doc = committed_config()
        again = inventory.set_adaptive(doc, "living_room", True)
        self.assertEqual(again["rooms"][0]["adaptive_config"], doc["rooms"][0]["adaptive_config"])

    def test_adaptive_off_drops_the_config_and_says_what_that_loses(self):
        doc = committed_config()
        self.assertEqual(
            inventory.adaptive_config_content(doc["rooms"][0]),
            [
                "2 learning unit(s)",
                "lux sensors: sensor.living_room_lux",
                "schedule profile 'evening'",
            ],
        )
        result = inventory.set_adaptive(doc, "living_room", False)
        self.assertNotIn("adaptive_config", result["rooms"][0])
        config.checked(result)
        minimal = config.checked(inventory.set_adaptive(committed_config(), "kitchen", True))
        self.assertEqual(inventory.adaptive_config_content(minimal["rooms"][1]), [])

    def test_hard_switches_get_ids_from_owner_and_name(self):
        result = inventory.set_hard_switches(
            committed_config(), "porch", [self.BUTTON, {**self.BUTTON, "name": "Porch"}]
        )
        bindings = result["unassigned"][0]["hard_switches"]
        self.assertEqual([b["id"] for b in bindings], ["porch_button", "porch"])
        config.checked(result)
        kitchen = inventory.set_hard_switches(
            committed_config(), "kitchen_strip", [{**self.BUTTON, "name": "Wall switch"}]
        )
        self.assertEqual(
            kitchen["rooms"][1]["bulbs"][0]["hard_switches"][0]["id"], "kitchen_strip_wall_switch"
        )

    def test_a_new_id_never_takes_another_owners(self):
        doc = committed_config()
        result = inventory.set_hard_switches(
            doc, "reading_corner", [{**self.BUTTON, "name": "Reading switch"}]
        )
        group = result["rooms"][0]["switching_groups"][1]
        self.assertEqual(group["hard_switches"][0]["id"], "reading_corner_reading_switch")
        # Another owner already holds porch_button; two new ones of one name differ too.
        result = inventory.set_hard_switches(
            doc, "kitchen_strip", [{**self.BUTTON, "id": "porch_button"}]
        )
        result = inventory.set_hard_switches(result, "porch", [self.BUTTON, self.BUTTON])
        self.assertEqual(
            [b["id"] for b in result["unassigned"][0]["hard_switches"]],
            ["porch_button_2", "porch_button_3"],
        )
        config.checked(result)

    def test_hard_switches_keep_ids_and_drop_empty_keys(self):
        kept = {**self.BUTTON, "id": "keep_me", "turn": None}
        result = inventory.set_hard_switches(committed_config(), "porch", [kept])
        self.assertEqual(
            result["unassigned"][0]["hard_switches"],
            [{k: v for k, v in kept.items() if k != "turn"}],
        )
        cleared = inventory.set_hard_switches(result, "porch", [])
        self.assertEqual(cleared["unassigned"][0]["hard_switches"], [])

    def test_a_feedback_loop_is_caught_when_saving(self):
        loop = {"name": "Loop", "source": {"type": "state", "entity_id": "light.porch"}}
        result = inventory.set_hard_switches(
            committed_config(), "kitchen_strip", [{**loop, "mode": "toggle"}]
        )
        with self.assertRaises(ConfigError) as caught:
            config.checked(result)
        self.assertIn("a toggle can't take light.porch", str(caught.exception))

    def test_an_id_is_the_owners_only_on_a_whole_word(self):
        result = inventory.set_hard_switches(
            committed_config(), "porch", [{**self.BUTTON, "name": "Porches light"}]
        )
        self.assertEqual(result["unassigned"][0]["hard_switches"][0]["id"], "porch_porches_light")

    def test_a_non_string_id_is_left_for_the_schema(self):
        result = inventory.set_hard_switches(
            committed_config(), "porch", [{**self.BUTTON, "id": ["a"]}]
        )
        with self.assertRaises(ConfigError):
            config.checked(result)

    def test_unknown_owner(self):
        with self.assertRaises(inventory.NotFound):
            inventory.set_hard_switches(committed_config(), "ghost", [])

    def test_new_group_ids_share_the_bulb_namespace(self):
        doc = committed_config()
        self.assertEqual(inventory.new_group_id(doc, "Kitchen strip"), "kitchen_strip_2")
        self.assertEqual(inventory.new_group_id(doc, "Reading corner"), "reading_corner_2")
        self.assertEqual(inventory.new_group_id(doc, "!!!"), "group")


class LiveTest(unittest.TestCase):
    def test_live_bulbs_directly_and_through_groups(self):
        self.assertEqual(
            inventory.live_bulbs(committed_config()),
            {
                "chandelier_1": ["living_all_off"],
                "chandelier_2": ["living_all_off"],
                "chandelier_3": ["living_all_off"],
                "reading_lamp": ["living_all_off", "reading_switch"],
            },
        )

    def test_going_live(self):
        doc = committed_config()
        self.assertEqual(inventory.going_live(doc, doc), [])
        button = {
            "name": "Porch button",
            "source": {"type": "state", "entity_id": "switch.reading_relay"},
            "mode": "follow",
        }
        added = inventory.set_hard_switches(doc, "porch", [button])
        self.assertEqual(
            inventory.going_live(doc, added),
            [
                {
                    "id": "porch_button",
                    "name": "Porch button",
                    "owner": {"kind": "bulb", "id": "porch", "name": "Porch"},
                    "bulbs": ["porch"],
                }
            ],
        )
        # Removing it makes nothing live.
        self.assertEqual(inventory.going_live(added, doc), [])

    def test_a_member_added_to_a_bound_group_goes_live(self):
        doc = committed_config()
        group = copy.deepcopy(doc["rooms"][0]["switching_groups"][0])
        group["members"] = ["chandelier_1"]
        shrunk = inventory.upsert_switching_group(doc, "living_room", group)
        self.assertEqual(inventory.going_live(doc, shrunk), [])
        self.assertEqual([c["id"] for c in inventory.going_live(shrunk, doc)], ["living_all_off"])
        changed = copy.deepcopy(doc)
        binding = changed["rooms"][0]["switching_groups"][0]["hard_switches"][0]
        binding["name"] = "Renamed"
        self.assertEqual(inventory.going_live(doc, changed), [])
        binding["turn"] = "on"
        self.assertEqual([c["id"] for c in inventory.going_live(doc, changed)], ["living_all_off"])


if __name__ == "__main__":
    unittest.main()
