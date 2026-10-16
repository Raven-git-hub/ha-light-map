"""Ignored lights (P4.1): a bulb the user set aside (an indicator or gadget LED) stays in
the snapshot, followed through syncs by its match key, but gets no soft switch or bridge,
is in no switching group or learning unit, is left out of the deploy and the Area writes,
and is never ignored unless the user said so. The observer's side is in test_observe, the
HTTP side in test_api, the Map's in test_ui."""

import copy
import unittest
from unittest import mock

from app.core import config, deploy, generate, inventory, switching
from app.core.config import ConfigError
from app.core.deploy import DeployError
from tests.fakes import STATES, FakeHA, committed_config, light, v2_config, without_keys

AREAS = [
    {"area_id": "living_room", "name": "Living Room"},
    {"area_id": "kitchen", "name": "Kitchen"},
]

# HA as of the committed snapshot.
LIGHTS = [
    light("light.chandelier_1", "living_room", "Chandelier 1"),
    light("light.chandelier_2", "living_room", "Chandelier 2"),
    light("light.chandelier_3", "living_room", "Chandelier 3"),
    light("light.reading_lamp", "living_room", "Reading lamp"),
    light("light.kitchen_strip", "kitchen", "Kitchen strip"),
    light("light.porch", None, "Porch"),
]


def setUpModule():
    # The fixture's chandelier_3 is unavailable, so every deploy warns about its soft.
    deploy.log.disabled = True


def tearDownModule():
    deploy.log.disabled = False


def problems_of(call, *args) -> list[str]:
    try:
        call(*args)
    except ConfigError as exc:
        return exc.problems
    raise AssertionError("expected a ConfigError")


def ignoring(*bulb_ids, doc=None):
    """The committed fixture with some bulbs ignored, the way the Map ignores them."""
    doc = committed_config() if doc is None else doc
    for bulb_id in bulb_ids:
        doc = inventory.set_ignored(doc, bulb_id, True)
    return config.checked(doc)


def bulb_of(doc, bulb_id):
    return inventory.find_bulb(doc, bulb_id)[1]


def living(doc):
    return next(r for r in doc["rooms"] if r["id"] == "living_room")


class SchemaTest(unittest.TestCase):
    def test_a_version_2_config_migrates_with_no_light_ignored(self):
        old = v2_config()
        self.assertEqual(config.migrate(old), committed_config())
        self.assertNotIn("ignored", old["rooms"][0]["bulbs"][0])  # the input is untouched
        loaded = config.loads(v2_config())
        self.assertEqual(loaded["version"], 3)
        self.assertEqual({b["ignored"] for _, b in inventory._all_bulbs(loaded)}, {False})

    def test_a_bulb_without_the_flag_is_not_ignored(self):
        doc = v2_config()
        doc["version"] = config.CURRENT_VERSION
        loaded = config.loads(doc)
        self.assertFalse(bulb_of(loaded, "porch")["ignored"])
        self.assertEqual(len(inventory.active_bulbs(loaded)), 6)

    def test_the_flag_is_a_boolean(self):
        doc = committed_config()
        doc["unassigned"][0]["ignored"] = "yes"
        self.assertTrue(any("unassigned/0/ignored" in p for p in problems_of(config.validate, doc)))

    def test_a_newer_light_map_is_refused_by_an_older_one(self):
        # What the bump buys: a version-2 reader stops at the version, rather than loading
        # a house whose indicator LEDs it doesn't know to leave alone.
        doc = committed_config()
        self.assertEqual(doc["version"], config.CURRENT_VERSION)
        with mock.patch.object(config, "CURRENT_VERSION", 2):
            self.assertIn("newer Light Map", problems_of(config.migrate, doc)[0])


class CrossCheckTest(unittest.TestCase):
    def test_an_ignored_switching_group_member_is_refused(self):
        doc = committed_config()
        bulb_of(doc, "chandelier_1")["ignored"] = True  # by hand, without the pruning
        problems = problems_of(config.cross_check, doc)
        self.assertIn(
            "room 'living_room' switching group 'living_all': member 'chandelier_1' is "
            "ignored; an ignored bulb is in no switching group or learning unit",
            problems,
        )
        self.assertIn(
            "room 'living_room' learning unit 'chandelier': member 'chandelier_1' is "
            "ignored; an ignored bulb is in no switching group or learning unit",
            problems,
        )
        self.assertEqual(len(problems), 2)

    def test_an_ignored_bulb_with_hard_switches_is_refused(self):
        doc = committed_config()
        doc["unassigned"][0]["ignored"] = True
        doc["unassigned"][0]["hard_switches"] = [
            {
                "id": "porch_button",
                "name": "Porch button",
                "source": {"type": "event", "event_type": "zha_event"},
                "mode": "press",
                "turn": "off",
            }
        ]
        self.assertEqual(
            problems_of(config.cross_check, doc),
            [
                (
                    "bulb 'porch' is ignored, so it has no soft switch for a hard switch to act "
                    "on; remove its hard switches (or un-ignore it)"
                )
            ],
        )

    def test_a_toggle_may_not_take_an_ignored_light_as_its_source(self):
        # Nothing drives an ignored light, but allowed, the toggle would loop the moment
        # the porch was un-ignored (refused then, naming another bulb's switch), or a sync
        # gave light.porch to a new bulb (a proposal that couldn't be saved).
        doc = ignoring("porch")
        toggle = {
            "name": "Porch light as a switch",
            "source": {"type": "state", "entity_id": "light.porch"},
            "mode": "toggle",
        }
        doc = inventory.set_hard_switches(doc, "kitchen_strip", [toggle])
        problems = problems_of(config.checked, doc)
        self.assertTrue(any("a bulb Light Map drives" in p for p in problems), problems)
        # Follow and press set a state rather than flip one, so they settle: still allowed.
        follow = {**toggle, "mode": "follow"}
        config.checked(inventory.set_hard_switches(ignoring("porch"), "kitchen_strip", [follow]))

    def test_a_sync_that_hands_an_ignored_lights_entity_id_to_a_new_bulb_still_saves(self):
        doc = ignoring("porch")
        lights = [
            {**x, "ha_match_key": "reg_repaired"} if x["entity_id"] == "light.porch" else x
            for x in LIGHTS
        ]
        config.checked(config.reconcile_area_snapshot(doc, AREAS, lights))

    def test_an_ignored_bulb_keeps_its_id(self):
        doc = ignoring("porch")
        self.assertEqual(inventory.new_group_id(doc, "Porch"), "porch_2")
        doc["unassigned"].append(copy.deepcopy(doc["unassigned"][0]))
        doc["unassigned"][1].update(entity_id="light.other", ha_match_key="reg_other")
        self.assertTrue(
            any(
                "bulb or switching-group id 'porch'" in p
                for p in problems_of(config.cross_check, doc)
            )
        )


class InventoryTest(unittest.TestCase):
    def test_the_active_inventory_leaves_out_the_ignored(self):
        doc = ignoring("porch", "chandelier_3")
        self.assertEqual(
            [b["id"] for _, b in inventory.active_bulbs(doc)],
            ["chandelier_1", "chandelier_2", "reading_lamp", "kitchen_strip"],
        )
        self.assertEqual(
            [(r and r["id"], b["id"]) for r, b in inventory.ignored_bulbs(doc)],
            [("living_room", "chandelier_3"), (None, "porch")],
        )
        # Still in the snapshot, where a sync and an un-ignore can find it.
        self.assertEqual(inventory.find_bulb(doc, "porch")[1]["ignored"], True)

    def test_ignore_drops_says_what_ignoring_would_throw_away(self):
        doc = committed_config()
        self.assertEqual(
            inventory.ignore_drops(doc, "reading_lamp"),
            [
                "its hard switch(es): reading_switch",
                "its place in switching group 'All living room lights'",
                "switching group 'Reading corner', which would have no members left",
                "learning unit 'Reading', which would have no members left",
            ],
        )
        self.assertEqual(inventory.ignore_drops(doc, "porch"), [])
        self.assertEqual(inventory.ignore_drops(ignoring("reading_lamp"), "reading_lamp"), [])

    def test_a_group_left_empty_takes_its_hard_switches_with_it(self):
        doc = committed_config()
        living(doc)["switching_groups"][1]["hard_switches"] = [
            {
                "id": "corner_button",
                "name": "Corner button",
                "source": {"type": "event", "event_type": "zha_event"},
                "mode": "press",
                "turn": "on",
            }
        ]
        self.assertIn(
            "switching group 'Reading corner', which would have no members left, with its "
            "hard switch(es): corner_button",
            inventory.ignore_drops(doc, "reading_lamp"),
        )
        after = ignoring("reading_lamp", doc=doc)
        self.assertNotIn("corner_button", inventory.hard_switch_targets(after))

    def test_ignoring_takes_the_bulbs_bindings_and_memberships(self):
        before = committed_config()
        after = inventory.set_ignored(before, "reading_lamp", True)
        self.assertEqual(before, committed_config())  # pure
        config.cross_check(config.checked(after))
        lamp = bulb_of(after, "reading_lamp")
        self.assertEqual((lamp["ignored"], lamp["hard_switches"]), (True, []))
        room = living(after)
        self.assertEqual(
            [(g["id"], g["members"]) for g in room["switching_groups"]],
            [("living_all", ["chandelier_1", "chandelier_2", "chandelier_3"])],
        )
        self.assertEqual(
            [u["id"] for u in room["adaptive_config"]["learning_units"]], ["chandelier"]
        )
        self.assertEqual(
            config.snapshot_changes(before, after),
            [
                "bulb ignored: light.reading_lamp; drops hard switch(es): reading_switch",
                "switching group 'living_room/living_all': members dropped: reading_lamp",
                "switching group removed: 'living_room/reading_corner'",
                "learning unit removed: 'living_room/reading'",
            ],
        )
        # It stays where it was, in its room, with its id and key.
        self.assertIn("reading_lamp", [b["id"] for b in room["bulbs"]])
        self.assertEqual(lamp["ha_match_key"], "reg_light_reading_lamp")

    def test_an_unassigned_bulb_has_nothing_to_drop_but_its_switches(self):
        doc = committed_config()
        after = inventory.set_ignored(doc, "porch", True)
        self.assertEqual(config.snapshot_changes(doc, after), ["bulb ignored: light.porch"])

    def test_un_ignoring_brings_the_bulb_back_bare(self):
        ignored = ignoring("reading_lamp")
        back = config.checked(inventory.set_ignored(ignored, "reading_lamp", False))
        lamp = bulb_of(back, "reading_lamp")
        self.assertEqual((lamp["ignored"], lamp["hard_switches"]), (False, []))
        self.assertNotIn("reading_lamp", inventory.live_bulbs(back))
        # Nothing it had comes back, so nothing is made live by un-ignoring it.
        self.assertEqual(inventory.going_live(ignored, back), [])
        self.assertEqual(
            config.snapshot_changes(ignored, back), ["bulb un-ignored: light.reading_lamp"]
        )
        self.assertIn("reading_lamp", [b["id"] for _, b in inventory.active_bulbs(back)])

    def test_an_unknown_bulb(self):
        with self.assertRaises(inventory.NotFound):
            inventory.set_ignored(committed_config(), "nope", True)


class GenerationTest(unittest.TestCase):
    def test_an_ignored_bulb_generates_nothing(self):
        doc = ignoring("porch", "reading_lamp")
        package = generate.generate(doc, "lm")
        ids = generate.artefact_ids(package)
        for name in ("lm_soft_porch", "lm_bridge_porch", "lm_soft_reading_lamp"):
            self.assertNotIn(name, ids)
        self.assertNotIn("lm_bridge_reading_lamp", ids)
        self.assertNotIn("lm_hard_reading_switch", ids)  # its hard switch went with it
        self.assertIn("lm_soft_kitchen_strip", ids)
        # The group it left no longer switches its soft switch.
        group = next(a for a in package["automation"] if a["id"] == "lm_hard_living_all_off")
        self.assertNotIn("input_boolean.lm_soft_reading_lamp", generate.to_yaml(group))
        references = switching.references(doc)
        self.assertNotIn("light.porch", references["bulbs"])
        self.assertNotIn("light.porch", references["entities"])
        self.assertNotIn("lm_bridge_porch", switching.bridge_mirrors(doc))

    def test_ignored_generates_exactly_what_absent_does(self):
        # The plane can't tell an ignored bulb from one that isn't in the config at all.
        doc = ignoring("porch")
        absent = copy.deepcopy(doc)
        absent["unassigned"] = []
        self.assertEqual(
            generate.to_yaml(generate.generate(doc, "lm")),
            generate.to_yaml(generate.generate(absent, "lm")),
        )
        self.assertEqual(
            generate.to_yaml(generate.generate(doc, "lm")),
            generate.to_yaml(generate.generate(ignoring("porch"), "lm")),
        )

    def test_a_brightness_provider_never_meets_an_ignored_bulb(self):
        seen = []

        def provider(doc, room, bulb):
            seen.append(bulb["id"])  # and leaves every bulb on the plain path

        switching.bridge_automations(ignoring("porch"), {"al": provider})
        self.assertNotIn("porch", seen)
        self.assertIn("kitchen_strip", seen)


class DeployTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_sweep_removes_a_now_ignored_bulbs_soft_and_bridge(self):
        ha = FakeHA()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        self.assertIn("lm_soft_porch", ha.helpers["input_boolean"])
        ha.set_light("light.porch", "on")
        lights_before = {
            s["entity_id"]: s["state"]
            for s in await ha.get_states()
            if s["entity_id"].startswith("light.")
        }
        calls_before = len(ha.calls)

        doc = ignoring("porch", "reading_lamp")
        preview = deploy.summarise(await deploy.prepare(ha, doc, "lm"))
        self.assertEqual(
            preview["removed"],
            {
                "input_boolean": ["lm_soft_porch", "lm_soft_reading_lamp"],
                "automation": [
                    "lm_bridge_porch",
                    "lm_bridge_reading_lamp",
                    "lm_hard_reading_switch",
                ],
            },
        )
        self.assertEqual(preview["created"], {"input_boolean": [], "automation": []})
        # The group's automation no longer targets the lamp's soft switch.
        self.assertEqual(preview["updated"]["automation"], ["lm_hard_living_all_off"])
        self.assertEqual(preview["sync"], [])

        result = await deploy.deploy(ha, doc, "lm", delay=0)  # verified
        self.assertEqual(result["removed"], preview["removed"])
        self.assertNotIn("lm_soft_porch", ha.helpers["input_boolean"])
        self.assertNotIn("lm_bridge_porch", ha.automations)
        # Nothing drove a light: no light service, no bridge fired, every light as it was.
        self.assertFalse(
            [c for c in ha.calls[calls_before:] if c[0] == "call_service" and c[1][0] == "light"]
        )
        self.assertEqual(ha.fired, [])
        self.assertEqual(
            {
                s["entity_id"]: s["state"]
                for s in await ha.get_states()
                if s["entity_id"].startswith("light.")
            },
            lights_before,
        )
        # Deployed again, nothing changes: the lane holds exactly what the config calls for.
        again = deploy.summarise(await deploy.prepare(ha, doc, "lm"))
        self.assertEqual(again["removed"], {"input_boolean": [], "automation": []})
        self.assertEqual(again["created"], {"input_boolean": [], "automation": []})

    async def test_un_ignored_it_comes_back_synced_first(self):
        ha = FakeHA()
        doc = ignoring("porch")
        await deploy.deploy(ha, doc, "lm", delay=0)
        ha.set_light("light.porch", "on")
        back = config.checked(inventory.set_ignored(doc, "porch", False))
        result = await deploy.deploy(ha, back, "lm", delay=0)
        self.assertEqual(result["created"]["input_boolean"], ["lm_soft_porch"])
        self.assertEqual(result["synced"], {"input_boolean.lm_soft_porch": "on"})
        self.assertEqual(ha.fired, [])

    async def test_a_deploy_doesnt_need_an_ignored_light_to_exist(self):
        ha = FakeHA(states=[s for s in copy.deepcopy(STATES) if s["entity_id"] != "light.porch"])
        with self.assertRaises(DeployError) as caught:
            await deploy.prepare(ha, committed_config(), "lm")
        self.assertIn(
            "light.porch (bulb 'porch') is not in Home Assistant", caught.exception.problems[0]
        )
        await deploy.prepare(ha, ignoring("porch"), "lm")

    async def test_the_area_writes_leave_an_ignored_light_alone(self):
        ha = FakeHA()
        porch = next(e for e in ha.entities if e["entity_id"] == "light.porch")
        porch["area_id"] = "garage"  # moved in HA since the last sync
        lights = await inventory.discover_lights(ha)
        areas = await ha.list_areas()
        self.assertEqual(
            [m["bulb"] for m in deploy.plan_areas(committed_config(), areas, lights)["moves"]],
            ["porch"],
        )
        doc = ignoring("porch")
        self.assertEqual(deploy.plan_areas(doc, areas, lights), {"moves": [], "problems": []})
        self.assertEqual(await deploy.deploy_areas(ha, doc), {"moves": []})
        self.assertEqual(porch["area_id"], "garage")
        # Nor does an ignored light HA no longer has hold up the others.
        gone = [x for x in lights if x["entity_id"] != "light.porch"]
        self.assertEqual(deploy.plan_areas(doc, areas, gone)["problems"], [])

    async def test_the_crossover_report_skips_an_ignored_light(self):
        ha = FakeHA(related={"light.porch": {"automation": ["automation.porch_at_dusk"]}})
        self.assertTrue((await deploy.crossover(ha, committed_config()))["drivers"])
        self.assertEqual((await deploy.crossover(ha, ignoring("porch")))["drivers"], [])


class ReconcileTest(unittest.TestCase):
    def reconcile(self, doc, lights=LIGHTS, areas=AREAS):
        result = config.reconcile_area_snapshot(doc, areas, lights)
        config.validate(result)
        config.cross_check(result)
        return result

    def test_the_flag_survives_a_sync(self):
        doc = ignoring("porch", "chandelier_3")
        self.assertEqual(self.reconcile(doc), doc)

    def test_the_flag_survives_a_rename_by_match_key(self):
        doc = ignoring("porch")
        lights = [
            {**x, "entity_id": "light.front_porch"} if x["entity_id"] == "light.porch" else x
            for x in LIGHTS
        ]
        result = self.reconcile(doc, lights)
        porch = bulb_of(result, "porch")
        self.assertEqual((porch["entity_id"], porch["ignored"]), ("light.front_porch", True))
        self.assertEqual(
            config.snapshot_changes(doc, result),
            ["bulb entity_id changed: light.porch -> light.front_porch (ignored)"],
        )

    def test_the_flag_survives_a_move_between_areas(self):
        doc = ignoring("porch")
        lights = [
            {**x, "area_id": "kitchen"} if x["entity_id"] == "light.porch" else x for x in LIGHTS
        ]
        result = self.reconcile(doc, lights)
        kitchen = next(r for r in result["rooms"] if r["id"] == "kitchen")
        self.assertEqual([(b["id"], b["ignored"]) for b in kitchen["bulbs"]][-1], ("porch", True))
        self.assertEqual(
            config.snapshot_changes(doc, result),
            ["bulb moved: light.porch: unassigned -> room 'kitchen' (ignored)"],
        )

    def test_a_keyless_bulb_keeps_the_flag_by_entity_id(self):
        doc = without_keys(ignoring("porch"))
        result = self.reconcile(doc)
        porch = bulb_of(result, "porch")
        self.assertEqual((porch["ignored"], porch["ha_match_key"]), (True, "reg_light_porch"))

    def test_a_new_light_is_never_ignored(self):
        # Not even one whose name says indicator LED: guessing could hide a real bulb.
        lights = [
            *LIGHTS,
            light("light.zigbee_coordinator_led", None, "Zigbee coordinator LED"),
            light("light.desk", "kitchen", "Desk"),
        ]
        result = self.reconcile(committed_config(), lights)
        for bulb_id in ("zigbee_coordinator_led", "desk"):
            self.assertIs(bulb_of(result, bulb_id)["ignored"], False)

    def test_another_registry_entry_under_its_entity_id_is_a_new_bulb_not_ignored(self):
        doc = ignoring("porch")
        lights = [
            {**x, "ha_match_key": "reg_replacement"} if x["entity_id"] == "light.porch" else x
            for x in LIGHTS
        ]
        result = self.reconcile(doc, lights)
        replacement = result["unassigned"][0]
        self.assertEqual((replacement["id"], replacement["ignored"]), ("porch_2", False))
        self.assertIn(
            "bulb removed: light.porch (was in unassigned, ignored)",
            config.snapshot_changes(doc, result),
        )

    def test_an_ignored_bulb_is_dropped_when_ha_no_longer_has_its_light(self):
        doc = ignoring("porch")
        result = self.reconcile(doc, [x for x in LIGHTS if x["entity_id"] != "light.porch"])
        self.assertEqual(result["unassigned"], [])
        self.assertEqual(
            config.snapshot_changes(doc, result),
            ["bulb removed: light.porch (was in unassigned, ignored)"],
        )

    def test_a_map_edit_commits_only_itself_around_an_ignored_bulb(self):
        # snapshot_as_ha reproduces the snapshot, ignored bulbs and all.
        doc = ignoring("porch", "chandelier_3")
        areas, lights = config.snapshot_as_ha(doc)
        self.assertEqual(config.reconcile_area_snapshot(doc, areas, lights), doc)
        moved = inventory.snapshot_with_move(doc, "kitchen_strip", "living_room")
        self.assertTrue(bulb_of(moved, "porch")["ignored"])
        self.assertTrue(bulb_of(moved, "chandelier_3")["ignored"])

    def test_the_reconcile_prunes_an_ignored_member(self):
        # The reconcile, as a function, prunes an ignored member like a departed one. (A
        # config that has one doesn't load, so this is only ever the function on its own:
        # it doesn't lean on its input having been pruned.)
        doc = committed_config()
        bulb_of(doc, "kitchen_strip")["ignored"] = True
        kitchen = doc["rooms"][1]
        kitchen["switching_groups"] = [{"id": "kg", "name": "K", "members": ["kitchen_strip"]}]
        result = self.reconcile(doc)
        self.assertEqual(
            next(r for r in result["rooms"] if r["id"] == "kitchen")["switching_groups"], []
        )


if __name__ == "__main__":
    unittest.main()
