"""app.core.deploy against the fake HA: reconcile the lm_ lane, verify after writing, stay
in the lane, never drive a bulb, and write Areas from the committed snapshot."""

import copy
import unittest
from unittest import mock

from app.core import config, deploy, generate, inventory, switching
from app.core.config import ConfigError
from app.core.deploy import DeployError
from tests.fakes import FakeHA, committed_config

BULBS = ["chandelier_1", "chandelier_2", "chandelier_3", "kitchen_strip", "porch", "reading_lamp"]
SOFTS = [f"lm_soft_{b}" for b in BULBS]
BRIDGES = [f"lm_bridge_{b}" for b in BULBS]
HARD = ["lm_hard_living_all_off", "lm_hard_reading_switch"]

# The live ha-adaptive-light's artefacts, and things that belong to the user.
FOREIGN_HELPERS = [
    {"id": "al_active_lounge", "name": "AL Active lounge", "icon": "mdi:lightbulb-auto"},
    {"id": "al_hold_lounge", "name": "AL Hold lounge"},
    {"id": "guest_mode", "name": "Guest mode"},
    {"id": "lmx_switch", "name": "LMX switch"},
]
FOREIGN_AUTOMATIONS = [
    {"id": "al_scene_lounge_day", "alias": "al_scene_lounge_day", "triggers": [], "actions": []},
    {"id": "al_watchdog_guard", "alias": "al_watchdog_guard", "triggers": [], "actions": []},
    {"id": "1700000000000", "alias": "Porch at dusk", "triggers": [], "actions": []},
]


def setUpModule():
    # The fixture's chandelier_3 is unavailable, so every deploy warns that its soft switch
    # keeps its state. CoexistenceTest checks that warning once; elsewhere it is noise.
    deploy.log.disabled = True


def tearDownModule():
    deploy.log.disabled = False


def writes(ha, since=0):
    return [
        call
        for call in ha.calls[since:]
        if call[0]
        in (
            "create_helper",
            "update_helper",
            "delete_helper",
            "save_automation_config",
            "delete_automation_config",
            "call_service",
            "set_entity_area",
            "create_area",
            "rename_area",
        )
    ]


def named(ha, method):
    return [call[1] for call in ha.calls if call[0] == method]


class DeployTest(unittest.IsolatedAsyncioTestCase):
    async def run_deploy(self, ha, doc=None, **kwargs):
        doc = committed_config() if doc is None else doc
        return await deploy.deploy(ha, doc, "lm", delay=0, **kwargs)

    async def test_a_first_deploy_creates_the_lane(self):
        ha = FakeHA()
        result = await self.run_deploy(ha)
        self.assertEqual(
            result["created"], {"input_boolean": SOFTS, "automation": [*BRIDGES, *HARD]}
        )
        self.assertEqual(result["updated"], {"input_boolean": [], "automation": []})
        self.assertEqual(result["removed"], {"input_boolean": [], "automation": []})
        package = generate.generate(committed_config(), "lm")
        self.assertEqual(
            {
                i: {k: v for k, v in item.items() if k != "id"}
                for i, item in ha.helpers["input_boolean"].items()
            },
            package["input_boolean"],
        )
        self.assertEqual(list(ha.automations), [*BRIDGES, *HARD])
        for automation in package["automation"]:
            self.assertEqual(ha.automations[automation["id"]], automation)

    async def test_write_order(self):
        ha = FakeHA()
        await self.run_deploy(ha)
        order = [
            (
                call[0],
                call[1][1]
                if call[0] == "create_helper"
                else call[1][0]
                if call[0] == "save_automation_config"
                else call[1],
            )
            for call in writes(ha)
        ]
        kinds = [kind for kind, _ in order]
        # Helpers, then the soft-switch syncs, then automations: bridges before hard switches.
        last_helper = max(i for i, k in enumerate(kinds) if k == "create_helper")
        first_sync = kinds.index("call_service")
        first_automation = kinds.index("save_automation_config")
        self.assertLess(last_helper, first_sync)
        self.assertLess(
            max(i for i, k in enumerate(kinds) if k == "call_service"), first_automation
        )
        saved = [target for kind, target in order if kind == "save_automation_config"]
        self.assertEqual(saved, [*BRIDGES, *HARD])

    async def test_a_second_deploy_changes_nothing(self):
        ha = FakeHA()
        await self.run_deploy(ha)
        mark = len(ha.calls)
        result = await self.run_deploy(ha)
        self.assertEqual(writes(ha, mark), [])
        for label in ("created", "updated", "removed"):
            self.assertEqual(result[label], {"input_boolean": [], "automation": []})
        self.assertEqual((result["sync"], result["synced"]), ([], {}))

    async def test_updates_what_drifted(self):
        ha = FakeHA()
        await self.run_deploy(ha)
        ha.helpers["input_boolean"]["lm_soft_porch"]["icon"] = "mdi:star"
        ha.automations["lm_hard_reading_switch"]["mode"] = "single"
        doc = committed_config()
        doc["unassigned"][0]["name"] = "Front porch"
        mark = len(ha.calls)
        result = await self.run_deploy(ha, doc)
        self.assertEqual(
            result["updated"],
            {
                "input_boolean": ["lm_soft_porch"],
                "automation": ["lm_bridge_porch", "lm_hard_reading_switch"],
            },
        )
        self.assertEqual(result["created"], {"input_boolean": [], "automation": []})
        self.assertIn("Front porch", ha.automations["lm_bridge_porch"]["description"])
        self.assertEqual(ha.automations["lm_hard_reading_switch"]["mode"], "queued")
        self.assertEqual(ha.helpers["input_boolean"]["lm_soft_porch"]["icon"], "mdi:light-switch")
        # An update re-arms nothing: the bridge already exists, so its soft isn't touched.
        self.assertNotIn("call_service", [call[0] for call in writes(ha, mark)])

    async def test_removes_what_the_config_no_longer_has(self):
        ha = FakeHA()
        await self.run_deploy(ha)
        ha.set_light("light.porch", "on")
        await ha.call_service(
            "input_boolean", "turn_on", {"entity_id": "input_boolean.lm_soft_porch"}
        )
        ha.fired.clear()
        doc = committed_config()
        doc["unassigned"] = []
        doc["rooms"][0]["bulbs"][3]["hard_switches"] = []
        mark = len(ha.calls)
        result = await self.run_deploy(ha, doc)
        self.assertEqual(
            result["removed"],
            {
                "input_boolean": ["lm_soft_porch"],
                "automation": ["lm_bridge_porch", "lm_hard_reading_switch"],
            },
        )
        self.assertNotIn("lm_soft_porch", ha.helpers["input_boolean"])
        self.assertNotIn("lm_bridge_porch", ha.automations)
        # Automations go before the helpers they reference, and the bulb is left alone.
        kinds = [call[0] for call in writes(ha, mark)]
        self.assertLess(kinds.index("delete_automation_config"), kinds.index("delete_helper"))
        self.assertEqual(ha.fired, [])
        self.assertEqual(
            next(s for s in ha.states if s["entity_id"] == "light.porch")["state"], "on"
        )

    async def test_a_deploy_into_an_empty_house_removes_everything(self):
        ha = FakeHA()
        await self.run_deploy(ha)
        result = await deploy.deploy(ha, config.blank(), "lm", delay=0)
        self.assertEqual(result["removed"]["input_boolean"], SOFTS)
        self.assertEqual(ha.helpers["input_boolean"], {})
        self.assertEqual(ha.automations, {})


class CoexistenceTest(unittest.IsolatedAsyncioTestCase):
    """Deploying the plane must never drive the user's bulbs."""

    async def test_softs_start_where_their_bulbs_are_and_nothing_fires(self):
        ha = FakeHA()  # chandelier_1/2 on, chandelier_3 unavailable, the rest off.
        with mock.patch.object(deploy.log, "disabled", False):
            with self.assertLogs(deploy.log, "WARNING") as logs:
                result = await deploy.deploy(ha, committed_config(), "lm", delay=0)
        self.assertEqual(
            logs.output,
            [
                "WARNING:app.core.deploy:light.chandelier_3 is unavailable, so "
                "input_boolean.lm_soft_chandelier_3 keeps its state"
            ],
        )
        self.assertEqual(ha.fired, [])
        self.assertEqual([c for c in named(ha, "call_service") if c[0] != "input_boolean"], [])
        self.assertEqual(
            {e: ha.helper_states[e] for e in sorted(ha.helper_states)},
            {f"input_boolean.{s}": ("on" if s in SOFTS[:2] else "off") for s in SOFTS},
        )
        self.assertEqual(
            result["synced"],
            {
                f"input_boolean.{s}": ("on" if s in SOFTS[:2] else "off")
                for s in SOFTS
                if s != "lm_soft_chandelier_3"
            },
        )
        self.assertEqual(result["unsynced"], {"input_boolean.lm_soft_chandelier_3": "unavailable"})
        # Only the softs that differ from their new "off" are written.
        self.assertEqual(
            named(ha, "call_service"),
            [
                ("input_boolean", "turn_on", {"entity_id": "input_boolean.lm_soft_chandelier_1"}),
                ("input_boolean", "turn_on", {"entity_id": "input_boolean.lm_soft_chandelier_2"}),
            ],
        )

    async def test_the_sync_would_have_fired_a_live_bridge(self):
        # The control for the test above: had the bridge gone live first, the same sync
        # would have triggered it, and the fake would have seen it.
        ha = FakeHA()
        bridge = switching.bridge_automations(committed_config(), providers={})[0]
        await ha.create_helper("input_boolean", {"name": "LM Soft chandelier_1"})
        await ha.save_automation_config(bridge["id"], bridge)
        await ha.call_service(
            "input_boolean", "turn_on", {"entity_id": "input_boolean.lm_soft_chandelier_1"}
        )
        self.assertEqual(
            ha.fired,
            [("lm_bridge_chandelier_1", "input_boolean.lm_soft_chandelier_1", "off", "on")],
        )

    async def test_a_restart_after_deploy_fires_nothing(self):
        ha = FakeHA()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        ha.restart()
        self.assertEqual(ha.fired, [])

    async def test_a_soft_is_synced_whenever_its_bridge_first_goes_live(self):
        # A deploy interrupted after creating the soft switches: the next one still syncs
        # each soft before creating its bridge, and only then.
        ha = FakeHA()
        await ha.create_helper(
            "input_boolean", {"name": "LM Soft chandelier_1", "icon": "mdi:light-switch"}
        )
        result = await deploy.deploy(ha, committed_config(), "lm", delay=0)
        self.assertIn("input_boolean.lm_soft_chandelier_1", result["sync"])
        self.assertEqual(ha.helper_states["input_boolean.lm_soft_chandelier_1"], "on")
        self.assertEqual(ha.fired, [])

    async def test_once_live_a_flip_drives_the_bulb(self):
        # End to end in the fake: a hard switch moves a soft, which triggers its bridge.
        ha = FakeHA()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        await ha.call_service(
            "input_boolean", "turn_on", {"entity_id": "input_boolean.lm_soft_reading_lamp"}
        )
        self.assertEqual(
            ha.fired,
            [("lm_bridge_reading_lamp", "input_boolean.lm_soft_reading_lamp", "off", "on")],
        )


class StoredButNotLoadedTest(unittest.IsolatedAsyncioTestCase):
    """HA lists automations only as entities. One whose config is stored but which has no
    entity (still reloading, or disabled) must read as there: re-creating a bridge would
    re-sync its soft switch while the bridge may already be live, driving the bulb."""

    async def drifted(self):
        ha = FakeHA()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        # The bulb is switched outside the plane, so its soft switch is now stale.
        ha.set_light("light.porch", "on")
        return ha

    async def test_one_still_reloading_is_rewritten_never_recreated(self):
        ha = await self.drifted()
        # Hidden for the two state reads planning makes, as if an interrupted deploy had
        # written it a moment ago.
        ha.appearing["lm_bridge_porch"] = 2
        mark = len(ha.calls)
        result = await deploy.deploy(ha, committed_config(), "lm", delay=0)
        self.assertEqual(result["updated"]["automation"], ["lm_bridge_porch"])
        self.assertEqual((result["created"]["automation"], result["sync"]), ([], []))
        self.assertNotIn("call_service", [call[0] for call in writes(ha, mark)])
        self.assertEqual(ha.fired, [])
        self.assertEqual(ha.helper_states["input_boolean.lm_soft_porch"], "off")

    async def test_one_whose_entity_is_disabled_fails_loudly_without_driving_anything(self):
        ha = await self.drifted()
        ha.disabled.add("lm_bridge_porch")
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(ha, committed_config(), "lm", delay=0, attempts=2)
        self.assertEqual(
            caught.exception.problems,
            ["automation lm_bridge_porch has not loaded (is its entity disabled in HA?)"],
        )
        self.assertEqual([c for c in named(ha, "call_service")[2:]], [])
        self.assertEqual(ha.fired, [])

    async def test_one_ha_rejected_is_written_again(self):
        ha = FakeHA()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        ha.rejected.add("lm_hard_reading_switch")  # e.g. its integration loaded late
        deploy_plan = await deploy.prepare(ha, committed_config(), "lm")
        self.assertEqual(
            [(s["op"], s["id"]) for s in deploy_plan["steps"]],
            [("update", "lm_hard_reading_switch")],
        )

    async def test_an_id_the_config_api_cannot_address_is_left_alone(self):
        ha = FakeHA(yaml_automations=("lm_Porch-2",))
        result = await deploy.deploy(ha, committed_config(), "lm", delay=0)
        self.assertEqual(
            result["warnings"],
            [
                "automation lm_Porch-2 is in the 'lm' lane but defined in HA's YAML (or under an "
                "id its config API can't address), so a deploy can't remove it"
            ],
        )
        self.assertNotIn("lm_Porch-2", repr(writes(ha)))


class LaneTest(unittest.IsolatedAsyncioTestCase):
    def foreign_ha(self, **kwargs):
        return FakeHA(
            helpers={"input_boolean": copy.deepcopy(FOREIGN_HELPERS)},
            automations=copy.deepcopy(FOREIGN_AUTOMATIONS),
            **kwargs,
        )

    async def test_a_lm_deploy_never_touches_anything_else(self):
        ha = self.foreign_ha()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        await deploy.deploy(ha, config.blank(), "lm", delay=0)  # and removes only its own
        foreign = [h["id"] for h in FOREIGN_HELPERS] + [a["id"] for a in FOREIGN_AUTOMATIONS]
        for call in writes(ha):
            for name in foreign:
                self.assertNotIn(name, repr(call))
        self.assertEqual(
            sorted(ha.helpers["input_boolean"]), sorted(h["id"] for h in FOREIGN_HELPERS)
        )
        self.assertEqual(sorted(ha.automations), sorted(a["id"] for a in FOREIGN_AUTOMATIONS))
        self.assertEqual(ha.automations["al_scene_lounge_day"], FOREIGN_AUTOMATIONS[0])

    async def test_fetch_live_reads_only_the_lane(self):
        ha = self.foreign_ha()
        live = await deploy.fetch_live(ha, "lm", domains=["input_boolean"])
        self.assertEqual(
            live,
            {"input_boolean": {}, "automation": {}, "unloaded": [], "unmanaged": [], "held": []},
        )
        # Without `domains`, every helper domain deploy knows is read.
        self.assertEqual((await deploy.fetch_live(ha, "lm"))["input_select"], {})
        al = await deploy.fetch_live(ha, "al")
        self.assertEqual(sorted(al["input_boolean"]), ["al_active_lounge", "al_hold_lounge"])
        self.assertEqual(sorted(al["automation"]), ["al_scene_lounge_day", "al_watchdog_guard"])

    def test_a_plan_ignores_whatever_live_holds_outside_its_lane(self):
        live = {
            "input_boolean": {h["id"]: {"name": h["name"]} for h in FOREIGN_HELPERS},
            "automation": {a["id"]: a for a in FOREIGN_AUTOMATIONS},
            "unmanaged": ["al_maint_lounge"],
        }
        deploy_plan = deploy.plan("lm", generate.generate(config.blank(), "lm"), live)
        self.assertEqual(deploy_plan["steps"], [])
        self.assertEqual(deploy_plan["warnings"], [])

    def test_a_plan_refuses_to_write_outside_its_lane(self):
        desired = {"input_boolean": {}, "automation": [{"id": "al_scene_lounge_day"}]}
        with self.assertRaises(ValueError):
            deploy.plan("lm", desired, {})

    def test_a_nested_lane_belongs_to_its_own_namespace(self):
        live = {
            "input_boolean": {"lm_al_hold_lounge": {"name": "LM AL Hold lounge"}},
            "automation": {},
        }
        with mock.patch.dict(generate._GENERATORS, {"lm_al": lambda doc: {}}):
            self.assertEqual(
                deploy.plan("lm", {"input_boolean": {}, "automation": []}, live)["steps"], []
            )
        self.assertEqual(
            len(deploy.plan("lm", {"input_boolean": {}, "automation": []}, live)["steps"]), 1
        )


class VerifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_write_that_did_not_land(self):
        ha = FakeHA(drop_writes=True)
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(ha, committed_config(), "lm", delay=0, attempts=2)
        problems = caught.exception.problems
        self.assertIn("input_boolean.lm_soft_porch is missing", problems)
        self.assertIn("automation lm_bridge_porch is missing", problems)

    async def test_an_automation_ha_rejected(self):
        ha = FakeHA(rejected=("lm_hard_reading_switch",))
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(ha, committed_config(), "lm", delay=0, attempts=2)
        self.assertEqual(
            caught.exception.problems,
            ["automation lm_hard_reading_switch did not load: HA rejected it (see HA's log)"],
        )

    async def test_waits_for_ha_to_reload_what_was_written(self):
        ha = FakeHA(lag=3)
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(FakeHA(lag=3), committed_config(), "lm", delay=0, attempts=1)
        self.assertIn(
            "automation lm_bridge_porch has not loaded (is its entity disabled in HA?)",
            caught.exception.problems,
        )
        # And for a removal, until the entity is gone.
        mark = len(ha.calls)
        await deploy.deploy(ha, config.blank(), "lm", delay=0)
        self.assertGreater(len([c for c in ha.calls[mark:] if c[0] == "get_states"]), 3)

    async def test_checks_the_whole_lane_not_just_the_steps(self):
        ha = FakeHA()
        doc = committed_config()
        await deploy.deploy(ha, doc, "lm", delay=0)
        # Someone renames a soft switch's entity in HA: the helper is unchanged, but the
        # automations now address an entity that isn't there.
        ha.helper_states["input_boolean.porch_soft"] = ha.helper_states.pop(
            "input_boolean.lm_soft_porch"
        )
        live = await deploy.fetch_live(ha, "lm")
        deploy_plan = deploy.plan("lm", generate.generate(doc, "lm"), live)
        self.assertEqual(deploy_plan["steps"], [])
        with self.assertRaises(DeployError) as caught:
            await deploy.verify(ha, deploy_plan, attempts=1)
        self.assertEqual(
            caught.exception.problems,
            ["input_boolean.lm_soft_porch: the helper exists but its entity has another id"],
        )

    async def test_a_synced_soft_that_did_not_hold(self):
        ha = FakeHA()
        doc = committed_config()
        await deploy.deploy(ha, doc, "lm", delay=0)
        deploy_plan = deploy.plan(
            "lm", generate.generate(doc, "lm"), await deploy.fetch_live(ha, "lm")
        )
        with self.assertRaises(DeployError) as caught:
            await deploy.verify(
                ha, deploy_plan, synced={"input_boolean.lm_soft_porch": "on"}, attempts=1
            )
        self.assertEqual(
            caught.exception.problems, ["input_boolean.lm_soft_porch is off, not on like its bulb"]
        )


class RefusalTest(unittest.IsolatedAsyncioTestCase):
    async def assert_refused(self, ha, doc, *fragments, error=DeployError):
        with self.assertRaises(error) as caught:
            await deploy.deploy(ha, doc, "lm", delay=0)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(writes(ha), [])

    async def test_a_bulb_ha_no_longer_has(self):
        ha = FakeHA()
        ha.states = [s for s in ha.states if s["entity_id"] != "light.porch"]
        await self.assert_refused(ha, committed_config(), "light.porch (bulb 'porch')", "sync")

    async def test_a_source_ha_does_not_have(self):
        ha = FakeHA()
        ha.states = [s for s in ha.states if s["entity_id"] != "switch.reading_relay"]
        await self.assert_refused(
            ha, committed_config(), "switch.reading_relay (hard switch 'reading_switch')"
        )

    async def test_a_device_ha_does_not_have(self):
        doc = committed_config()
        doc["rooms"][1]["bulbs"][0]["hard_switches"] = [
            {
                "id": "strip_remote",
                "name": "Strip remote",
                "source": {
                    "type": "device",
                    "device_id": "dev_gone",
                    "trigger": {"domain": "zha", "type": "press"},
                },
                "mode": "toggle",
            }
        ]
        await self.assert_refused(FakeHA(), doc, "device dev_gone (hard switch 'strip_remote')")
        doc["rooms"][1]["bulbs"][0]["hard_switches"][0]["source"]["device_id"] = "dev_strip"
        await deploy.deploy(FakeHA(), doc, "lm", delay=0)

    async def test_a_toggle_on_an_ha_group_of_its_own_bulbs(self):
        doc = committed_config()
        doc["rooms"][0]["switching_groups"][0]["hard_switches"] = [
            {
                "id": "living_toggle",
                "name": "Living toggle",
                "source": {"type": "state", "entity_id": "light.living_room_group"},
                "mode": "toggle",
            }
        ]
        config.cross_check(doc)  # The config alone can't see inside the group.
        await self.assert_refused(
            FakeHA(),
            doc,
            "light.living_room_group (hard switch 'living_toggle') is an HA group of bulbs "
            "Light Map drives (light.chandelier_1, light.chandelier_2, light.chandelier_3)",
        )
        # follow settles, so it may.
        doc["rooms"][0]["switching_groups"][0]["hard_switches"][0]["mode"] = "follow"
        await deploy.deploy(FakeHA(), doc, "lm", delay=0)

    async def test_an_invalid_config(self):
        doc = committed_config()
        doc["rooms"][0]["switching_groups"][0]["members"].append("ghost")
        await self.assert_refused(FakeHA(), doc, "ghost", error=ConfigError)

    async def test_an_automation_defined_elsewhere_in_yaml(self):
        await self.assert_refused(
            FakeHA(yaml_automations=("lm_bridge_porch",)),
            committed_config(),
            "automation lm_bridge_porch is already defined in HA's YAML",
        )

    def test_a_helper_name_that_would_land_under_another_id(self):
        desired = {"input_boolean": {"lm_soft_porch": {"name": "Porch soft"}}, "automation": []}
        with self.assertRaises(DeployError) as caught:
            deploy.plan("lm", desired, {})
        self.assertIn("would make HA create input_boolean.porch_soft", str(caught.exception))

    async def test_a_helper_id_held_by_a_yaml_helper_is_refused_before_writing(self):
        await self.assert_refused(
            FakeHA(yaml_helpers=("lm_soft_porch",)),
            committed_config(),
            "input_boolean.lm_soft_porch already exists in HA but is not one of Light Map's "
            "UI-managed helpers",
        )

    async def test_a_helper_id_taken_after_planning_and_the_recovery(self):
        # Taken between the plan and the create (or held where HA's states don't show it):
        # the created id is still checked, and the stray is reconciled later.
        ha = FakeHA()
        deploy_plan = await deploy.prepare(ha, committed_config(), "lm")
        ha.yaml_helpers.add("lm_soft_porch")
        with self.assertRaises(DeployError) as caught:
            await deploy.apply(ha, deploy_plan)
        self.assertIn("created input_boolean.lm_soft_porch_2 instead", str(caught.exception))
        self.assertEqual(ha.automations, {})  # it stopped before any automation
        # Once the collision is resolved in HA, the next deploy reconciles, stray and all.
        ha.yaml_helpers.clear()
        result = await deploy.deploy(ha, committed_config(), "lm", delay=0)
        self.assertEqual(result["removed"]["input_boolean"], ["lm_soft_porch_2"])
        self.assertIn("lm_soft_porch", ha.helpers["input_boolean"])

    async def test_a_preview_writes_nothing(self):
        ha = FakeHA()
        doc = committed_config()
        deploy_plan = deploy.plan(
            "lm",
            generate.generate(doc, "lm"),
            await deploy.fetch_live(ha, "lm"),
            mirrors=switching.bridge_mirrors(doc),
        )
        summary = deploy.summarise(deploy_plan)
        self.assertEqual(summary["created"]["automation"], [*BRIDGES, *HARD])
        self.assertEqual(summary["sync"], [f"input_boolean.{s}" for s in SOFTS])
        self.assertEqual(writes(ha), [])


class DeployAreasTest(unittest.IsolatedAsyncioTestCase):
    def entity(self, ha, entity_id):
        return next(e for e in ha.entities if e["entity_id"] == entity_id)

    async def test_nothing_to_write_when_ha_matches(self):
        ha = FakeHA()
        self.assertEqual(await deploy.deploy_areas(ha, committed_config()), {"moves": []})
        self.assertEqual(ha.writes, [])

    async def test_moves_bulbs_back_to_the_committed_snapshot(self):
        ha = FakeHA()
        self.entity(ha, "light.reading_lamp")["area_id"] = "kitchen"
        self.entity(ha, "light.porch")["area_id"] = "garage"
        result = await deploy.deploy_areas(ha, committed_config())
        self.assertEqual(
            result["moves"],
            [
                {"bulb": "porch", "entity_id": "light.porch", "from": "garage", "to": None},
                {
                    "bulb": "reading_lamp",
                    "entity_id": "light.reading_lamp",
                    "from": "kitchen",
                    "to": "living_room",
                },
            ],
        )
        self.assertEqual(
            ha.writes,
            [
                ("set_entity_area", ("light.porch", None)),
                ("set_entity_area", ("light.reading_lamp", "living_room")),
            ],
        )
        self.assertEqual(self.entity(ha, "light.porch")["area_id"], None)

    async def test_refuses_the_lot_if_any_move_would_be_refused(self):
        ha = FakeHA()
        self.entity(ha, "light.reading_lamp")["area_id"] = "kitchen"
        # Porch now inherits Garage from its device: unassigning the bulb would mean
        # clearing the device's Area, which is the user's call.
        next(d for d in ha.devices if d["id"] == "dev_porch")["area_id"] = "garage"
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy_areas(ha, committed_config())
        self.assertIn("inherits Area 'garage' from its device", str(caught.exception))
        self.assertEqual(ha.writes, [])

    async def test_refuses_a_stale_snapshot(self):
        ha = FakeHA()
        ha.areas = [a for a in ha.areas if a["area_id"] != "kitchen"]
        ha.states = [s for s in ha.states if s["entity_id"] != "light.porch"]
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy_areas(ha, committed_config())
        self.assertEqual(
            caught.exception.problems,
            [
                "room 'kitchen': HA has no Area 'kitchen' any more; sync the snapshot first "
                "(Light Map can't recreate an Area under a given id)",
                "bulb 'porch': HA has no light.porch; sync the snapshot first",
            ],
        )
        self.assertEqual(ha.writes, [])

    async def test_a_move_that_did_not_land(self):
        ha = FakeHA(drop_writes=True)
        self.entity(ha, "light.reading_lamp")["area_id"] = "kitchen"
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy_areas(ha, committed_config())
        self.assertIn("did not land", str(caught.exception))

    def test_plan_is_pure_and_previews(self):
        lights = [
            {"entity_id": "light.porch", "area_id": "garage", "is_group": False},
            {"entity_id": "light.group", "area_id": "garage", "is_group": True},
        ]
        doc = {"rooms": [], "unassigned": [{"id": "porch", "entity_id": "light.porch"}]}
        self.assertEqual(
            deploy.plan_areas(doc, [{"area_id": "garage"}], lights),
            {
                "moves": [
                    {"bulb": "porch", "entity_id": "light.porch", "from": "garage", "to": None}
                ],
                "problems": [],
            },
        )


class CrossoverTest(unittest.IsolatedAsyncioTestCase):
    """The read-only report of what else in HA touches the plane's bulbs."""

    OLD_SCENE = {
        "id": "al_scene_lounge_day",
        "alias": "AL scene lounge day",
        "triggers": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.chandelier_1"}}],
    }
    PORCH = {
        "id": "1700000000000",
        "alias": "Porch at dusk",
        "triggers": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": ["light.porch"]}}],
    }

    async def test_foreign_drivers_and_groups(self):
        ha = FakeHA(
            automations=[self.PORCH, self.OLD_SCENE],
            related={"light.kitchen_strip": {"script": ["script.cooking"], "area": ["kitchen"]}},
        )
        await deploy.deploy(ha, committed_config(), "lm", delay=0)  # our bridges name bulbs too
        mark = len(ha.calls)
        report = await deploy.crossover(ha, committed_config())
        self.assertEqual(writes(ha, mark), [])
        self.assertEqual(
            report["drivers"],
            [
                {
                    "entity_id": "automation.al_scene_lounge_day",
                    "kind": "automation",
                    "name": "al_scene_lounge_day",
                    "id": "al_scene_lounge_day",
                    "adaptive_light": True,
                    "bulbs": ["light.chandelier_1"],
                },
                {
                    "entity_id": "automation.1700000000000",
                    "kind": "automation",
                    "name": "1700000000000",
                    "id": "1700000000000",
                    "adaptive_light": False,
                    "bulbs": ["light.porch"],
                },
                {
                    "entity_id": "script.cooking",
                    "kind": "script",
                    "name": "script.cooking",
                    "id": None,
                    "adaptive_light": False,
                    "bulbs": ["light.kitchen_strip"],
                },
            ],
        )
        self.assertEqual(
            report["groups"],
            [
                {
                    "entity_id": "light.living_room_group",
                    "name": "Living room lights",
                    "bulbs": ["light.chandelier_1", "light.chandelier_2", "light.chandelier_3"],
                }
            ],
        )
        self.assertEqual((report["collisions"], report["errors"]), ([], []))

    async def test_name_collisions(self):
        ha = FakeHA(
            yaml_helpers=("lm_soft_porch",),
            automations=[{"id": "1700000000001", "alias": "x", "triggers": [], "actions": []}],
        )
        # A user automation whose entity id is the one Light Map's bridge would take.
        ha.states.append(
            {
                "entity_id": "automation.lm_bridge_porch",
                "state": "on",
                "attributes": {"friendly_name": "My porch", "id": "1700000000001"},
            }
        )
        report = await deploy.crossover(ha, committed_config())
        self.assertEqual(
            report["collisions"],
            [
                "input_boolean.lm_soft_porch is already in HA but isn't one of Light Map's "
                "UI-managed helpers (a YAML helper, or another helper's entity renamed to it?): "
                "the deploy is refused until the id is freed",
                "automation.lm_bridge_porch is already taken by automation id 1700000000001 "
                "(My porch): Light Map's lm_bridge_porch would load as automation.lm_bridge_porch_2",
            ],
        )

    async def test_an_ha_without_search_says_so(self):
        report = await deploy.crossover(FakeHA(search_fails=True), committed_config())
        self.assertEqual(report["drivers"], [])
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("could not ask HA", report["errors"][0])
        # The groups come from HA's states, so they are still found.
        self.assertEqual(len(report["groups"]), 1)


class DeployAreasExpectedTest(unittest.IsolatedAsyncioTestCase):
    async def test_writes_only_what_was_previewed(self):
        ha = FakeHA()
        entry = next(e for e in ha.entities if e["entity_id"] == "light.reading_lamp")
        entry["area_id"] = "kitchen"
        preview = deploy.plan_areas(
            committed_config(), await ha.list_areas(), await inventory.discover_lights(ha)
        )
        # HA moves on after the preview: nothing is written.
        next(e for e in ha.entities if e["entity_id"] == "light.porch")["area_id"] = "garage"
        with self.assertRaises(deploy.StalePreview) as caught:
            await deploy.deploy_areas(ha, committed_config(), expected=preview["moves"])
        self.assertIn("changed since the preview", str(caught.exception))
        self.assertIsNone(caught.exception.written)
        self.assertEqual(ha.writes, [])
        entry_porch = next(e for e in ha.entities if e["entity_id"] == "light.porch")
        entry_porch["area_id"] = None
        result = await deploy.deploy_areas(ha, committed_config(), expected=preview["moves"])
        self.assertEqual(result["moves"], preview["moves"])


if __name__ == "__main__":
    unittest.main()
