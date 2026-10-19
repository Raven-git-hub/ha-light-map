"""The `al_` deploy lane (P5.1) against the fake HA, and the cutover switch at the HTTP
surface: off, nothing generates, previews or writes `al_`; on (the tests' own scope), `al_`
is reconciled as its own lane, verified after every write, previewed beside the switching
deploy and applied after it under the one fingerprint (P6a)."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from app.core import config, deploy, generate, switching
from app.core.deploy import DeployError
from app.core.ha import HAError
from tests.fakes import STATES, FakeHA, adaptive_wired, committed_config
from tests.test_api import ApiCase

ADAPTIVE = ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"]
ROOM_HELPERS = {
    "input_boolean": ["al_active_living_room", "al_hold_living_room"],
    "input_select": ["al_scene_living_room"],
}
AL_AUTOMATIONS = [
    *(f"al_scene_living_room_{s}" for s in config.SECTIONS),
    "al_maint_living_room",
    "al_watchdog_guard_living_room",
    "al_watchdog_stale_living_room",
]

# What the live ha-adaptive-light holds for the same room, by the predecessor's names: its
# helpers named exactly as Light Map names them, its scene select with display names, its
# maintenance under the old id. Plus things that belong to the user.
OLD_SYSTEM = {
    "helpers": {
        "input_boolean": [
            {
                "id": "al_active_living_room",
                "name": "AL Active living_room",
                "icon": "mdi:lightbulb-auto",
            },
            {
                "id": "al_hold_living_room",
                "name": "AL Hold living_room",
                "icon": "mdi:hand-back-right",
            },
            {"id": "guest_mode", "name": "Guest mode"},
        ],
        "input_select": [
            {
                "id": "al_scene_living_room",
                "name": "AL Scene living_room",
                "icon": "mdi:theme-light-dark",
                "options": ["Sunrise", "Day", "Afternoon", "Sunset", "Night", "Sleep"],
            },
            {"id": "house_mode", "name": "House mode", "options": ["home", "away"]},
        ],
    },
    "automations": [
        {"id": "al_scene_living_room_day", "alias": "AL SET SCENE", "triggers": [], "actions": []},
        {
            "id": "al_maintenance_living_room",
            "alias": "AL Lux Maintenance",
            "triggers": [],
            "actions": [],
        },
        {"id": "1700000000000", "alias": "Porch at dusk", "triggers": [], "actions": []},
    ],
}


def setUpModule():
    # The fixture's chandelier_3 is unavailable, so every lm deploy warns that its soft
    # switch keeps its state; noise here.
    deploy.log.disabled = True


def tearDownModule():
    deploy.log.disabled = False


def written_ids(ha, since=0):
    """The artefact ids each write in `ha.calls[since:]` names (a service call: its
    entity_ids)."""
    found = []
    for method, args in ha.calls[since:]:
        if method in ("create_helper",):
            found.append(args[1]["name"])
        elif method in ("update_helper", "delete_helper"):
            found.append(args[1])
        elif method in ("save_automation_config", "delete_automation_config"):
            found.append(args[0])
        elif method == "call_service":
            found.append(f"{args[0]}.{args[1]}")
    return found


class SwitchOffTest(unittest.IsolatedAsyncioTestCase):
    async def test_nothing_can_plan_or_write_al(self):
        ha = FakeHA(**OLD_SYSTEM)
        with self.assertRaises(ValueError):
            await deploy.prepare(ha, committed_config(), "al")
        # The switching deploy, beside the live system's al_ artefacts, never touches one.
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        await deploy.deploy(ha, config.blank(), "lm", delay=0)
        for name in written_ids(ha):
            self.assertTrue(name.startswith(("lm_", "LM Soft", "input_boolean.")), name)
        self.assertEqual(sorted(ha.helpers["input_select"]), ["al_scene_living_room", "house_mode"])
        self.assertIn("al_maintenance_living_room", ha.automations)
        # And its reads stayed P5.0's: it never lists another helper domain.
        self.assertEqual(
            {args[0] for method, args in ha.calls if method == "list_helpers"}, {"input_boolean"}
        )


class LaneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(adaptive_wired())

    async def deploy_both(self, ha, doc=None):
        doc = committed_config() if doc is None else doc
        lm = await deploy.deploy(ha, doc, "lm", delay=0)
        al = await deploy.deploy(ha, doc, "al", delay=0)
        return lm, al

    async def test_a_first_deploy_creates_the_lane_and_verifies_it(self):
        ha = FakeHA()
        lm, al = await self.deploy_both(ha)
        # Each lane summarises only its own helper domains.
        self.assertEqual(list(lm["created"]), ["input_boolean", "automation"])
        self.assertEqual(al["created"], {**ROOM_HELPERS, "automation": AL_AUTOMATIONS})
        self.assertEqual((al["sync"], al["synced"]), ([], {}))
        package = generate.generate(committed_config(), "al")
        self.assertEqual(
            ha.helpers["input_select"]["al_scene_living_room"],
            {"id": "al_scene_living_room", **package["input_select"]["al_scene_living_room"]},
        )
        for automation in package["automation"]:
            self.assertEqual(ha.automations[automation["id"]], automation)
        # Nothing in the deploy drove anything: no service call, no automation fired.
        self.assertFalse([a for m, a in ha.calls if m == "call_service" and a[0] == "light"])
        self.assertEqual(ha.fired, [])
        mark = len(ha.calls)
        again = await deploy.deploy(ha, committed_config(), "al", delay=0)
        self.assertEqual(written_ids(ha, mark), [])
        self.assertEqual(
            again["created"], {"input_boolean": [], "input_select": [], "automation": []}
        )

    async def test_the_al_deploy_writes_nothing_outside_its_lane(self):
        ha = FakeHA(**OLD_SYSTEM)
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        mark = len(ha.calls)
        await deploy.deploy(ha, committed_config(), "al", delay=0)
        names = written_ids(ha, mark)
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith(("al_", "AL ")), name)
        self.assertNotIn("call_service", [m for m, _ in ha.calls[mark:]])
        self.assertIn("guest_mode", ha.helpers["input_boolean"])
        self.assertIn("house_mode", ha.helpers["input_select"])
        self.assertIn("1700000000000", ha.automations)
        # Unflagging the room removes the al_ lane and nothing else.
        mark = len(ha.calls)
        await deploy.deploy(ha, config.blank(), "al", delay=0)
        for name in written_ids(ha, mark):
            self.assertTrue(name.startswith("al_"), name)
        self.assertEqual(sorted(ha.helpers["input_select"]), ["house_mode"])
        self.assertEqual(
            sorted(i for i in ha.helpers["input_boolean"] if not i.startswith("lm_")),
            ["guest_mode"],
        )
        self.assertTrue(any(i.startswith("lm_bridge_") for i in ha.automations))

    async def test_the_lm_deploy_never_touches_al(self):
        ha = FakeHA()
        await self.deploy_both(ha)
        mark = len(ha.calls)
        await deploy.deploy(ha, config.blank(), "lm", delay=0)
        for name in written_ids(ha, mark):
            self.assertFalse(name.startswith(("al_", "AL ")), name)
        self.assertEqual(sorted(ha.helpers["input_select"]), ["al_scene_living_room"])
        self.assertTrue(set(AL_AUTOMATIONS) <= set(ha.automations))

    async def test_what_an_al_deploy_would_do_to_the_live_system(self):
        # Why the lane waits for cutover: planned against the old system's artefacts, it
        # would adopt its helpers, rewrite its scene select and scenes, and remove its
        # maintenance. Planning is read-only.
        ha = FakeHA(**OLD_SYSTEM)
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        mark = len(ha.calls)
        summary = deploy.summarise(await deploy.prepare(ha, committed_config(), "al"))
        self.assertEqual(written_ids(ha, mark), [])
        self.assertEqual(summary["updated"]["input_select"], ["al_scene_living_room"])
        # Its hold adopted as it is; its guard rewritten to start off at every HA start.
        self.assertEqual(summary["updated"]["input_boolean"], ["al_active_living_room"])
        self.assertIn("al_scene_living_room_day", summary["updated"]["automation"])
        self.assertEqual(summary["removed"]["automation"], ["al_maintenance_living_room"])

    async def test_refused_until_the_switching_plane_is_deployed(self):
        ha = FakeHA()
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(ha, committed_config(), "al", delay=0)
        self.assertIn(
            "input_boolean.lm_soft_chandelier_1 (the soft switch of bulb 'chandelier_1') is not "
            "in Home Assistant; deploy the switching plane first",
            caught.exception.problems,
        )
        self.assertEqual(written_ids(ha), [])

    async def test_refused_while_a_sensor_or_bulb_is_missing(self):
        states = [s for s in STATES if s["entity_id"] != "sensor.living_room_lux"]
        ha = FakeHA(states=states)
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        mark = len(ha.calls)
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(ha, committed_config(), "al", delay=0)
        self.assertEqual(
            caught.exception.problems,
            [
                "sensor.living_room_lux (a lux sensor of room 'living_room') is not in Home Assistant"
            ],
        )
        self.assertEqual(written_ids(ha, mark), [])

    async def test_every_write_is_verified(self):
        ha = FakeHA()
        await deploy.deploy(ha, committed_config(), "lm", delay=0)
        ha.drop_writes = True
        with self.assertRaises(DeployError) as caught:
            await deploy.deploy(ha, committed_config(), "al", delay=0, attempts=2)
        self.assertIn("input_select.al_scene_living_room is missing", caught.exception.problems)
        self.assertIn("automation al_maint_living_room is missing", caught.exception.problems)

    async def test_a_scene_select_that_drifted_is_updated(self):
        ha = FakeHA()
        await self.deploy_both(ha)
        ha.helpers["input_select"]["al_scene_living_room"]["options"] = ["day", "night"]
        mark = len(ha.calls)
        result = await deploy.deploy(ha, committed_config(), "al", delay=0)
        self.assertEqual(result["updated"]["input_select"], ["al_scene_living_room"])
        self.assertEqual(
            ha.helpers["input_select"]["al_scene_living_room"]["options"], list(config.SECTIONS)
        )
        self.assertEqual(written_ids(ha, mark), ["al_scene_living_room"])


class ApiSwitchOffTest(ApiCase):
    def test_no_module_lane_is_shown(self):
        preview = self.client.get("/api/deploy/preview").json()
        self.assertTrue(preview["ready"])
        self.assertNotIn("modules", preview)
        self.assertEqual(self.client.get("/api/status").json()["adaptive"], {"wired": False})


class ApiSwitchOnTest(ApiCase):
    """The Map's deploy with the cutover switch on (P6a): `lm_` first, then each module lane
    the preview showed ready, each planned again, checked against the preview, applied and
    verified, under one fingerprint covering every lane."""

    def setUp(self):
        self.enterContext(adaptive_wired())
        super().setUp()

    def preview(self):
        response = self.client.get("/api/deploy/preview")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, preview):
        return self.client.post("/api/deploy", json={"fingerprint": preview["fingerprint"]})

    def log(self):
        entries = self.client.get("/api/log", params={"category": "deploy", "level": "debug"})
        return [e["message"] for e in reversed(entries.json()["entries"])]

    def test_a_lane_refused_in_the_preview_is_not_applied(self):
        self.assertEqual(self.client.get("/api/status").json()["adaptive"], {"wired": True})
        preview = self.preview()
        lane = preview["modules"]["lanes"]["al"]
        # Before the switching plane is deployed, its soft switches are missing.
        self.assertFalse(lane["ready"])
        self.assertTrue(any("deploy the switching plane first" in p for p in lane["refused"]))
        self.assertIn("after the lm_ lane", preview["modules"]["note"])
        self.assertNotIn("fingerprint", lane)  # one fingerprint, for the whole deploy
        response = self.apply(preview)
        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()
        self.assertEqual(answer["modules"], {"al": {"deployed": False, "refused": lane["refused"]}})
        # The switching plane went out, the adaptive bridges with the gated fragment; al_
        # not yet: it was never shown ready.
        self.assertFalse([i for i in self.ha.automations if i.startswith("al_")])
        self.assertNotIn("input_select", self.ha.helpers)
        bridge = self.ha.automations["lm_bridge_chandelier_1"]
        gate, choice = bridge["actions"][0]["choose"][0]["sequence"]
        self.assertIn("sensor.al_almanac_living_room", gate["variables"]["bridge_gate"])
        self.assertIn("brightness", choice["then"][0]["data"])
        self.assertNotIn("data", choice["else"][0])

        # Now the plan shows, and is what the next deploy applies.
        preview = self.preview()
        lane = preview["modules"]["lanes"]["al"]
        self.assertTrue(lane["ready"])
        self.assertEqual(lane["plan"]["created"], {**ROOM_HELPERS, "automation": AL_AUTOMATIONS})
        mark = len(self.ha.calls)
        response = self.apply(preview)
        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()
        self.assertEqual(written_ids(self.ha, mark)[:1], ["AL Active living_room"])
        self.assertEqual(answer["result"]["created"], {"input_boolean": [], "automation": []})
        al = answer["modules"]["al"]
        self.assertTrue(al["deployed"])
        self.assertEqual(al["result"]["created"], lane["plan"]["created"])
        package = generate.generate(committed_config(), "al")
        for automation in package["automation"]:
            self.assertEqual(self.ha.automations[automation["id"]], automation)
        self.assertEqual(sorted(self.ha.helpers["input_select"]), ["al_scene_living_room"])
        # Deploying drove nothing: no light service, no automation fired.
        self.assertFalse([a for m, a in self.ha.calls if m == "call_service" and a[0] == "light"])
        self.assertEqual(self.ha.fired, [])
        self.assertEqual(
            self.log()[-2:],
            [
                "Deployed the switching plane: 0 created, 0 updated, 0 removed.",
                "Deployed the al_ lane: 12 created, 0 updated, 0 removed.",
            ],
        )
        # And then there is nothing left to do in either lane.
        preview = self.preview()
        self.assertEqual(sum(len(i) for i in preview["plan"]["created"].values()), 0)
        self.assertEqual(
            preview["modules"]["lanes"]["al"]["plan"]["created"],
            {"input_boolean": [], "input_select": [], "automation": []},
        )

    def test_the_cutover_deploy_applies_lm_then_al_each_verified(self):
        # As at P6b: the switching plane was deployed with the switch off (plain bridges);
        # with it on, one deploy rewrites the adaptive bulbs' bridges and creates the al_
        # lane, in that order, each lane verified before the next is written.
        with mock.patch.dict(generate._GENERATORS), mock.patch.dict(generate._REFERENCES):
            del generate._GENERATORS["al"], generate._REFERENCES["al"]
            with mock.patch.dict(switching._BRIDGE_BRIGHTNESS, clear=True):
                self.assertEqual(self.apply(self.preview()).status_code, 200)
        self.assertFalse([i for i in self.ha.automations if i.startswith("al_")])
        preview = self.preview()
        self.assertEqual(
            preview["plan"]["updated"]["automation"], [f"lm_bridge_{b}" for b in ADAPTIVE]
        )
        self.assertTrue(preview["modules"]["lanes"]["al"]["ready"])
        mark = len(self.ha.calls)
        response = self.apply(preview)
        self.assertEqual(response.status_code, 200, response.text)
        calls = self.ha.calls[mark:]
        writes = [i for i, (m, _) in enumerate(calls) if (m, _) in self.ha.writes]
        names = written_ids(self.ha, mark)
        lm = [n for n in names if n.startswith("lm_")]
        al = [n for n in names if n.startswith(("al_", "AL "))]
        self.assertEqual(lm, [f"lm_bridge_{b}" for b in ADAPTIVE])
        self.assertEqual(len(al), 12)
        self.assertEqual(len(names), len(lm) + len(al))  # nothing written outside the lanes
        # lm_ first, all of it, and verified (its automations read back) before any al_.
        first_al = writes[len(lm)]
        self.assertEqual(names[: len(lm)], lm)
        between = calls[writes[len(lm) - 1] + 1 : first_al]
        read_back = [args[0] for method, args in between if method == "get_automation_config"]
        self.assertTrue(set(lm) <= set(read_back), read_back)
        self.assertEqual(
            response.json()["modules"]["al"]["result"]["created"]["automation"], AL_AUTOMATIONS
        )
        self.assertEqual(self.ha.fired, [])

    def test_the_fingerprint_covers_every_lane(self):
        self.apply(self.preview())  # the switching plane, so al_ is ready
        preview = self.preview()
        self.assertTrue(preview["modules"]["lanes"]["al"]["ready"])
        # Something changes in the al_ lane alone (a leftover the plan would now remove):
        # the whole deploy is stale, and nothing is written, lm_ included.
        self.ha.automations["al_maintenance_living_room"] = {
            "id": "al_maintenance_living_room",
            "alias": "AL Lux Maintenance",
            "triggers": [],
            "actions": [],
        }
        del self.ha.automations["lm_hard_reading_switch"]  # and lm_ has work to do
        mark = len(self.ha.calls)
        self.assert_error(self.apply(preview), 409, "preview_stale")
        self.assertEqual(written_ids(self.ha, mark), [])
        # A lane refused in the preview is part of what was shown, too: one that is ready
        # by the time the deploy runs is still stale.
        states = self.ha.states
        self.ha.states = [s for s in states if s["entity_id"] != "sensor.living_room_lux"]
        preview = self.preview()
        self.assertFalse(preview["modules"]["lanes"]["al"]["ready"])
        self.ha.states = states
        mark = len(self.ha.calls)
        self.assert_error(self.apply(preview), 409, "preview_stale")
        self.assertEqual(written_ids(self.ha, mark), [])

    def test_a_lane_that_changes_once_lm_is_written_is_not_applied(self):
        self.apply(self.preview())
        del self.ha.automations["lm_hard_reading_switch"]
        preview = self.preview()
        self.assertEqual(preview["plan"]["created"]["automation"], ["lm_hard_reading_switch"])
        self.assertTrue(preview["modules"]["lanes"]["al"]["ready"])
        real = deploy.apply
        ha = self.ha

        async def apply_then_meddle(client, deploy_plan):
            report = await real(client, deploy_plan)
            if deploy_plan["prefix"] == "lm":
                # Someone edits HA in the seconds between the two lanes.
                ha.helpers.setdefault("input_select", {})["al_scene_living_room"] = {
                    "id": "al_scene_living_room",
                    "name": "AL Scene living_room",
                    "options": ["Day", "Night"],
                }
            return report

        mark = len(self.ha.calls)
        with mock.patch.object(deploy, "apply", apply_then_meddle):
            body = self.assert_error(self.apply(preview), 409, "preview_stale")
        self.assertEqual(body["deployed"], ["lm"])
        self.assertIn("Deployed and verified: lm_.", body["detail"])
        names = written_ids(self.ha, mark)
        self.assertIn("lm_hard_reading_switch", names)
        self.assertFalse([n for n in names if n.startswith(("al_", "AL "))])
        self.assertEqual(
            self.log()[-2:],
            [
                "Deployed the switching plane: 1 created, 0 updated, 0 removed.",
                "The al_ lane changed after the preview, so it was not deployed.",
            ],
        )

    def test_a_lane_that_does_not_verify_stops_the_deploy(self):
        self.apply(self.preview())
        preview = self.preview()
        self.ha.rejected.add("al_maint_living_room")
        body = self.assert_error(self.apply(preview), 502, "deploy_failed")
        self.assertEqual(body["deployed"], ["lm"])
        self.assertIn("al_ lane's deploy stopped or did not verify", body["detail"])
        self.assertEqual(
            body["problems"],
            ["automation al_maint_living_room did not load: HA rejected it (see HA's log)"],
        )
        self.assertEqual(self.log()[-1], "The al_ lane's deploy stopped or did not verify.")

    def test_a_lane_ha_cant_be_read_for_never_costs_the_switching_preview(self):
        # The switching plane deployed, so the al_ lane gets as far as reading its helpers.
        preview = self.client.get("/api/deploy/preview").json()
        self.client.post("/api/deploy", json={"fingerprint": preview["fingerprint"]})
        real = self.ha.list_helpers

        async def failing(domain):
            if domain == "input_select":
                raise HAError("input_select/list failed: unknown command")
            return await real(domain)

        self.ha.list_helpers = failing
        response = self.client.get("/api/deploy/preview")
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview["ready"])
        self.assertIn("fingerprint", preview)
        lane = preview["modules"]["lanes"]["al"]
        self.assertFalse(lane["ready"])
        self.assertTrue(any("input_select/list failed" in p for p in lane["refused"]))


class DoctorWiringTest(unittest.TestCase):
    def test_the_doctor_runs_under_the_composition_roots_wiring(self):
        # Its lm_ preview must carry the bridges the app would deploy: once the cutover
        # switch is on, a doctor that never wired the modules would read every adaptive
        # bridge as one to rewrite.
        result = subprocess.run(
            [sys.executable, "-c", "import sys, tools.doctor; print('app.main' in sys.modules)"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "True", result.stderr)


if __name__ == "__main__":
    unittest.main()
