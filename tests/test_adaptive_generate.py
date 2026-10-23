"""app.modules.adaptive.generate (P5.1): the `al_` artefacts and the bridge fragment.

Structure first (always run): what is generated, for which rooms and bulbs, in what order,
byte for byte; that no `al_` artefact powers a bulb except through a soft switch, and that
nothing from the almanac is baked in. Then behaviour (`tests.ha_script`, needs jinja2):
the templates rendered and the automations run against a fake state machine, through the
core bridges they flip, with the almanac absent, partial, odd, and complete.
"""

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from app import main
from app.core import config, deploy, generate, inventory, switching
from app.modules.adaptive import almanac
from app.modules.adaptive import generate as al
from tests import ha_script
from tests.fakes import adaptive_wired, bulb, committed_config

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"

ROOM = "living_room"
ADAPTIVE = ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"]
CHANDELIER = ["chandelier_1", "chandelier_2", "chandelier_3"]

RENDER = (
    "import sys; from app.core import generate; from app.modules.adaptive import generate as al; "
    "from tests.fakes import committed_config; "
    "sys.stdout.write(generate.to_yaml(al.generate(committed_config())))"
)


def al_yaml(doc):
    return generate.to_yaml(al.generate(doc))


def living(doc):
    return next(r for r in doc["rooms"] if r["id"] == ROOM)


def by_id(automations):
    return {a["id"]: a for a in automations}


def walk(node, path=()):
    """Every (action, enclosing `if` conditions) pair in an action tree."""
    if isinstance(node, list):
        for item in node:
            yield from walk(item, path)
    elif isinstance(node, dict):
        if "action" in node:
            yield node, path
        if "if" in node:
            yield from walk(node.get("then", []), path + tuple(node["if"]))
            yield from walk(node.get("else", []), path)
        for option in node.get("choose", []):
            yield from walk(option["sequence"], path + tuple(option["conditions"]))
        yield from walk(node.get("default", []), path)
        for key in ("actions", "sequence"):
            if key in node and "choose" not in node:
                yield from walk(node[key], path)


def with_switched_only(doc):
    """The living room with a switched-only bulb (the porch light moved in, not adaptive)."""
    doc = copy.deepcopy(doc)
    porch = doc["unassigned"].pop()
    living(doc)["bulbs"].append(porch)
    return config.checked(doc)


class CutoverSwitchTest(unittest.TestCase):
    """The switch that keeps `al_` off the live HA by default: on only when the deployment
    sets `LM_ADAPTIVE_ENABLED=1` (at the P6b cutover, once the old `al_` system is gone)."""

    def test_the_live_composition_root_leaves_it_off(self):
        # With the variable unset (as here, and as a fresh checkout or a local run), the
        # module is not wired in and the core runs exactly as in P5.0.
        self.assertIs(main.ADAPTIVE_ENABLED, False)
        self.assertEqual(generate.namespaces(), ["lm"])
        self.assertEqual(switching.bridge_brightness_providers(), {})

    def test_the_environment_turns_it_on(self):
        # Only `LM_ADAPTIVE_ENABLED=1` turns it on; unset or any other value leaves it off,
        # so a stray or truthy-looking value can't switch the adaptive module on by surprise.
        with mock.patch.dict(os.environ, {"LM_ADAPTIVE_ENABLED": "1"}):
            self.assertTrue(main._adaptive_enabled_from_env())
        for off in ("", "0", "true", "yes", "TRUE"):
            with mock.patch.dict(os.environ, {"LM_ADAPTIVE_ENABLED": off}):
                self.assertFalse(main._adaptive_enabled_from_env())

    def test_off_the_generated_set_is_p5_0s_byte_for_byte(self):
        # tests/golden/lm_only.yaml was captured from main before P5.1: every namespace's
        # artefacts for the fixture config (whose living room has adaptive bulbs, so a
        # registered provider would change their bridges).
        everything = {p: generate.generate(committed_config(), p) for p in generate.namespaces()}
        self.assertEqual(generate.to_yaml(everything), (GOLDEN / "lm_only.yaml").read_text())

    def test_on_al_is_generated_and_adaptive_bridges_read_the_almanac(self):
        with adaptive_wired():
            self.assertEqual(generate.namespaces(), ["lm", "al"])
            lm = generate.generate(committed_config(), "lm")
            self.assertEqual(
                generate.generate(committed_config(), "al"), al.generate(committed_config())
            )
        bridges = by_id(lm["automation"])
        plain = {}
        for bulb_id in ["kitchen_strip", "porch"]:
            [on] = bridges[f"lm_bridge_{bulb_id}"]["actions"][0]["choose"][0]["sequence"]
            self.assertNotIn("data", on, bulb_id)
        for bulb_id in ADAPTIVE:
            soft_on = bridges[f"lm_bridge_{bulb_id}"]["actions"][0]["choose"][0]
            gate, choice = soft_on["sequence"]
            self.assertEqual(list(gate["variables"]), ["bridge_gate", "bridge_brightness"])
            for value in gate["variables"].values():
                self.assertIn("sensor.al_almanac_living_room", value)
            [with_target] = choice["then"]
            [without] = choice["else"]
            self.assertEqual(with_target["data"], {"brightness": "{{ bridge_brightness }}"})
            self.assertNotIn("data", without)
            plain[bulb_id] = without
        # Only those bridges' soft-on step differs from the switch-off output: put the plain
        # turn_on back, and it is P5.0's byte for byte.
        for bulb_id in ADAPTIVE:
            soft_on = bridges[f"lm_bridge_{bulb_id}"]["actions"][0]["choose"][0]
            soft_on["sequence"] = [plain[bulb_id]]
        self.assertEqual(generate.to_yaml({"lm": lm}), (GOLDEN / "lm_only.yaml").read_text())
        # And off again afterwards: the test's wiring is its own.
        self.assertEqual(generate.namespaces(), ["lm"])
        self.assertEqual(switching.bridge_brightness_providers(), {})


class GoldenTest(unittest.TestCase):
    def test_the_al_lane_byte_for_byte(self):
        self.assertEqual(al_yaml(committed_config()), (GOLDEN / "al_committed.yaml").read_text())

    def test_same_bytes_whatever_order_the_config_lists_things_in(self):
        shuffled = committed_config()
        shuffled["rooms"].reverse()
        for room in shuffled["rooms"]:
            room["bulbs"].reverse()
            block = room.get("adaptive_config")
            if block:
                block["adaptive_bulbs"].reverse()
                block["learning_units"].reverse()
                for unit in block["learning_units"]:
                    unit["members"].reverse()
                block["lux_sensors"] = list(reversed(block["lux_sensors"] + ["sensor.b_lux"]))
        doc = committed_config()
        living(doc)["adaptive_config"]["lux_sensors"].append("sensor.b_lux")
        self.assertEqual(al_yaml(doc), al_yaml(shuffled))

    def test_same_bytes_in_every_process(self):
        outputs = set()
        for seed in ("0", "1", "4242"):
            result = subprocess.run(
                [sys.executable, "-c", RENDER],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.add(result.stdout)
        self.assertEqual(outputs, {al_yaml(committed_config())})

    def test_generation_leaves_the_config_alone(self):
        doc = committed_config()
        before = copy.deepcopy(doc)
        al.generate(doc)
        self.assertEqual(doc, before)


class ArtefactTest(unittest.TestCase):
    def setUp(self):
        self.doc = committed_config()
        self.package = al.generate(self.doc)
        self.automations = by_id(self.package["automation"])

    def test_the_lane(self):
        self.assertEqual(
            self.package["input_boolean"],
            {
                # The guard starts off at every HA start (no run survives a restart); the
                # hold is restored.
                "al_active_living_room": {
                    "name": "AL Active living_room",
                    "initial": False,
                    "icon": al.GUARD_ICON,
                },
                "al_hold_living_room": {"name": "AL Hold living_room", "icon": al.HOLD_ICON},
            },
        )
        self.assertEqual(
            self.package["input_select"],
            {
                "al_scene_living_room": {
                    "name": "AL Scene living_room",
                    "icon": al.SCENE_ICON,
                    "options": list(config.SECTIONS),
                }
            },
        )
        self.assertEqual(
            list(self.automations),
            [f"al_scene_living_room_{s}" for s in config.SECTIONS]
            + [
                "al_maint_living_room",
                "al_watchdog_guard_living_room",
                "al_watchdog_stale_living_room",
            ],
        )
        for item_id, fields in [
            *self.package["input_boolean"].items(),
            *self.package["input_select"].items(),
        ]:
            # HA names a network-created helper by its slugified name.
            self.assertEqual(deploy._slugify(fields["name"]), item_id)
        for automation in self.automations.values():
            self.assertEqual(automation["alias"], automation["id"])
        # The core's own lane check passes (and would refuse a stray lm_ name).
        with adaptive_wired():
            self.assertEqual(generate.generate(self.doc, "al"), self.package)

    def test_no_adaptive_room_still_names_both_helper_domains(self):
        # The lane owns them, so the last room's helpers are removed when it goes.
        self.assertEqual(
            al.generate(config.blank()),
            {"input_boolean": {}, "input_select": {}, "automation": []},
        )

    def test_scenes_carry_no_triggers_and_no_conditions(self):
        for section in config.SECTIONS:
            scene = self.automations[f"al_scene_living_room_{section}"]
            self.assertEqual(scene["triggers"], [])
            self.assertEqual(scene["conditions"], [])
            self.assertEqual((scene["mode"], scene["max_exceeded"]), ("single", "silent"))

    def test_a_scene_raises_the_guard_clears_the_hold_and_sets_the_section_first(self):
        actions = self.automations["al_scene_living_room_night"]["actions"]
        self.assertEqual(
            [(a.get("action"), a.get("target", {}).get("entity_id")) for a in actions[:3]],
            [
                ("input_boolean.turn_on", "input_boolean.al_active_living_room"),
                ("input_boolean.turn_off", "input_boolean.al_hold_living_room"),
                ("input_select.select_option", "input_select.al_scene_living_room"),
            ],
        )
        self.assertEqual(actions[2]["data"], {"option": "night"})
        self.assertIn("'sensor.al_almanac_living_room', 'night'", actions[3]["variables"]["units"])
        self.assertEqual(actions[-2], {"delay": {"seconds": 120 + al.SETTLE_SECONDS}})
        # The guard is lowered last, unless another of the room's runs still holds it.
        release = actions[-1]
        self.assertEqual(
            release["then"],
            [
                {
                    "action": "input_boolean.turn_off",
                    "target": {"entity_id": "input_boolean.al_active_living_room"},
                }
            ],
        )
        holders = release["if"][0]["value_template"]
        for other in [*(s for s in config.SECTIONS if s != "night"), None]:
            name = f"al_scene_living_room_{other}" if other else "al_maint_living_room"
            self.assertIn(f"state_attr('automation.{name}', 'current')", holders)
        self.assertNotIn("al_scene_living_room_night'", holders)  # not itself

    def test_power_goes_only_through_soft_switches(self):
        softs = {switching.soft_entity(b) for b in ADAPTIVE}
        for automation in self.automations.values():
            for action, guards in walk(automation["actions"]):
                service = action["action"]
                self.assertNotEqual(service, "light.turn_off", automation["id"])
                if service == "light.turn_on":
                    # Brightness only, and guarded on the soft switch and the bulb being on.
                    light = action["target"]["entity_id"]
                    bulb_id = light.removeprefix("light.")
                    states = {(g.get("entity_id"), g.get("state")) for g in guards}
                    self.assertIn((switching.soft_entity(bulb_id), "on"), states, automation["id"])
                    self.assertIn((light, "on"), states, automation["id"])
                    self.assertLessEqual(set(action["data"]), {"brightness", "transition"})
                elif service.startswith("input_boolean."):
                    targets = action["target"]["entity_id"]
                    targets = targets if isinstance(targets, list) else [targets]
                    for target in targets:
                        self.assertTrue(
                            target in softs or target.startswith("input_boolean.al_"), target
                        )
                        if target in softs:
                            self.assertTrue(
                                automation["id"].startswith("al_scene_"), automation["id"]
                            )

    def test_each_unit_turns_its_members_soft_switches_on_or_off(self):
        actions = self.automations["al_scene_living_room_day"]["actions"]
        units = {a["alias"]: a for a in actions if "choose" in a}
        self.assertEqual(list(units), ["Chandelier (chandelier)", "Reading (reading)"])
        on, off = units["Chandelier (chandelier)"]["choose"]
        self.assertEqual(
            on["sequence"][-1],
            {
                "action": "input_boolean.turn_on",
                "target": {"entity_id": [switching.soft_entity(b) for b in CHANDELIER]},
            },
        )
        self.assertEqual(
            [p["then"][0]["target"]["entity_id"] for p in on["sequence"][:-1]],
            [f"light.{b}" for b in CHANDELIER],
        )
        self.assertEqual(
            off["sequence"],
            [
                {
                    "action": "input_boolean.turn_off",
                    "target": {"entity_id": [switching.soft_entity(b) for b in CHANDELIER]},
                }
            ],
        )

    def test_nothing_from_the_almanac_or_the_mode_is_baked_in(self):
        doc = committed_config()
        for section in config.SECTIONS:
            living(doc)["adaptive_config"]["scenes"][section]["units"]["chandelier"]["mode"] = "off"
            living(doc)["adaptive_config"]["scenes"][section]["lux_target_seed"] = 300
            living(doc)["adaptive_config"]["scenes"][section]["lux_margin"] = 50
        # A unit's mode and the knobs reach HA through the almanac (P5.2's publish); the
        # transition, which is the scene's own, is the only knob in the YAML.
        self.assertEqual(al_yaml(doc), al_yaml(committed_config()))
        living(doc)["adaptive_config"]["scenes"]["day"]["transition_seconds"] = 30
        self.assertNotEqual(al_yaml(doc), al_yaml(committed_config()))

    def test_maintenance_iterates_bulbs_and_only_touches_ones_that_are_on(self):
        maint = self.automations["al_maint_living_room"]
        self.assertEqual(maint["triggers"], [{"trigger": "time_pattern", "minutes": "/10"}])
        self.assertEqual(
            maint["conditions"],
            [
                {
                    "condition": "state",
                    "entity_id": "input_boolean.al_active_living_room",
                    "state": "off",
                },
                {
                    "condition": "state",
                    "entity_id": "input_boolean.al_hold_living_room",
                    "state": "off",
                },
            ],
        )
        nudges = [a for a in maint["actions"] if "if" in a and "alias" in a]
        self.assertEqual(
            [n["then"][0]["target"]["entity_id"] for n in nudges], [f"light.{b}" for b in ADAPTIVE]
        )
        for nudge, bulb_id in zip(nudges, ADAPTIVE):
            self.assertEqual(
                nudge["if"][0],
                {"condition": "state", "entity_id": switching.soft_entity(bulb_id), "state": "on"},
            )
            self.assertEqual(
                nudge["if"][1],
                {"condition": "state", "entity_id": f"light.{bulb_id}", "state": "on"},
            )
            self.assertEqual(set(nudge["then"][0]["data"]), {"brightness", "transition"})
        services = [a["action"] for a, _ in walk(maint["actions"])]
        self.assertEqual(services.count("input_boolean.turn_on"), 1)  # the guard, raised once
        self.assertNotIn("light.turn_off", services)
        release = maint["actions"][-1]["if"][0]["value_template"]
        self.assertNotIn("al_maint_living_room'", release)  # not itself
        self.assertIn("'automation.al_scene_living_room_day', 'current'", release)

    def test_maintenance_cadence(self):
        doc = committed_config()
        doc["system"]["heartbeat_interval_minutes"] = 60
        maint = by_id(al.generate(doc)["automation"])["al_maint_living_room"]
        # HA's time_pattern takes /1../59 for minutes; "/60" would be refused.
        self.assertEqual(maint["triggers"], [{"trigger": "time_pattern", "minutes": 0}])
        doc["system"]["heartbeat_interval_minutes"] = 15
        maint = by_id(al.generate(doc)["automation"])["al_maint_living_room"]
        self.assertEqual(maint["triggers"], [{"trigger": "time_pattern", "minutes": "/15"}])

    def test_a_room_with_nothing_to_drive_or_measure(self):
        doc = committed_config()
        block = living(doc)["adaptive_config"]
        block["lux_sensors"] = []
        self.assertNotIn("al_maint_living_room", by_id(al.generate(doc)["automation"]))
        # A room just ticked adaptive: every bulb switched-only, no unit.
        block.update(adaptive_bulbs=[], learning_units=[])
        doc = config.checked(doc)
        automations = by_id(al.generate(doc)["automation"])
        scene = automations["al_scene_living_room_day"]["actions"]
        # Guard, hold, section, guard: it changes no light, so there is nothing to wait out.
        self.assertEqual(
            [a.get("action") or a["then"][0]["action"] for a in scene],
            [
                "input_boolean.turn_on",
                "input_boolean.turn_off",
                "input_select.select_option",
                "input_boolean.turn_off",
            ],
        )
        # With no maintenance, only the other scenes hold the guard.
        self.assertNotIn("al_maint_", scene[-1]["if"][0]["value_template"])
        self.assertNotIn("al_maint_living_room", automations)
        self.assertIn("al_watchdog_guard_living_room", automations)

    def test_an_ignored_bulb_is_in_none_of_it(self):
        doc = inventory.set_ignored(committed_config(), "reading_lamp", True)
        doc = config.checked(doc)
        text = al_yaml(doc)
        self.assertNotIn("reading_lamp", text)
        self.assertNotIn("Reading (reading)", text)  # its unit went with it
        self.assertIn("lm_soft_chandelier_1", text)

    def test_a_switched_only_bulb_is_left_to_the_plain_plane(self):
        doc = with_switched_only(committed_config())
        self.assertNotIn("porch", al_yaml(doc))

    def test_watchdogs(self):
        guard = self.automations["al_watchdog_guard_living_room"]
        self.assertEqual(
            guard["triggers"],
            [
                {
                    "trigger": "state",
                    "entity_id": "input_boolean.al_active_living_room",
                    "to": "on",
                    "for": {"minutes": 10},
                    "id": "stuck",
                },
                {"trigger": "event", "event_type": "automation_reloaded", "id": "reloaded"},
            ],
        )
        self.assertEqual(guard["actions"][0]["action"], "input_boolean.turn_off")
        self.assertEqual(guard["actions"][1]["then"][0]["action"], "persistent_notification.create")
        stale = self.automations["al_watchdog_stale_living_room"]
        self.assertEqual(
            stale["triggers"],
            [
                {
                    "trigger": "state",
                    "entity_id": "input_select.al_scene_living_room",
                    "for": {"hours": 10},
                }
            ],
        )
        # A slow crossover keeps its guard up longer than ten minutes: the watchdog waits
        # longer, rather than releasing the guard mid-fade.
        doc = committed_config()
        living(doc)["adaptive_config"]["scenes"]["sunset"]["transition_seconds"] = 900
        guard = by_id(al.generate(doc)["automation"])["al_watchdog_guard_living_room"]
        self.assertEqual(
            guard["triggers"][0]["for"], {"minutes": 16 + al.GUARD_WATCHDOG_MARGIN_MINUTES}
        )

    def test_a_room_name_never_reaches_a_template(self):
        # Notification data is rendered by HA as a template: an Area named with braces
        # mustn't open one.
        doc = committed_config()
        living(doc)["name"] = "Den {{ states('lock.front') }} {% if x %}"
        text = json.dumps(
            [a["actions"] for a in al.generate(doc)["automation"] if "watchdog" in a["id"]]
        )
        self.assertIn("Den (( states('lock.front') )) (% if x %)", text)
        self.assertNotIn("{{ states('lock.front')", text)

    def test_a_config_that_was_never_validated_is_refused(self):
        doc = committed_config()
        living(doc)["adaptive_config"]["learning_units"][0]["members"].append("kitchen_strip")
        with self.assertRaises(ValueError):
            al.generate(doc)
        doc = committed_config()
        living(doc)["adaptive_config"]["adaptive_bulbs"].remove("reading_lamp")
        with self.assertRaises(ValueError):
            al.generate(doc)
        doc = committed_config()
        living(doc)["adaptive_config"]["learning_units"][0]["id"] = "x') or ('"
        with self.assertRaises(ValueError):
            al.generate(doc)

    def test_references(self):
        found = al.references(committed_config())
        self.assertEqual(found["devices"], {})
        entities = found["entities"]
        for bulb_id in ADAPTIVE:
            self.assertEqual(entities[f"light.{bulb_id}"], f"bulb '{bulb_id}'")
            self.assertIn(bulb_id, entities[switching.soft_entity(bulb_id)])
        self.assertIn("lux sensor", entities["sensor.living_room_lux"])
        self.assertNotIn("light.kitchen_strip", entities)
        self.assertNotIn("light.porch", entities)


class BridgeProviderTest(unittest.TestCase):
    def provide(self, doc, bulb_id):
        room, found = inventory.find_bulb(doc, bulb_id)
        return al.bridge_brightness(doc, room, found)

    def test_an_adaptive_bulb_gets_a_gated_template_and_nothing_else(self):
        fragment = self.provide(committed_config(), "chandelier_2")
        self.assertIsInstance(fragment, switching.GatedBrightness)
        self.assertEqual(list(fragment.data), ["brightness"])
        self.assertLessEqual(set(fragment.data), set(switching.BRIGHTNESS_KEYS))
        for template in (fragment.when, fragment.data["brightness"]):
            for read in (
                "states('input_select.al_scene_living_room')",
                "state_attr('sensor.al_almanac_living_room'",
                "get('chandelier')",
            ):
                self.assertIn(read, template)
            # Nothing made up from the bulb's own state any more: with no target, the
            # bridge's plain turn_on brings it back at its own level.
            self.assertNotIn("light.chandelier_2", template)
        # And no made-up level to fall back on: when the gate is false the data is rendered
        # but not used, so its fallback is 0, never a brightness like 255.
        self.assertTrue(fragment.data["brightness"].endswith("{{ t | int(0) }}"))
        self.assertNotIn("255", fragment.data["brightness"])

    def test_every_other_bulb_keeps_the_plain_path(self):
        doc = with_switched_only(committed_config())
        self.assertIsNone(self.provide(doc, "porch"))  # switched-only, in an adaptive room
        self.assertIsNone(self.provide(doc, "kitchen_strip"))  # a room that isn't adaptive
        doc = committed_config()
        self.assertIsNone(self.provide(doc, "porch"))  # Unassigned

    def test_the_seams_guards_hold(self):
        doc = committed_config()
        bridges = switching.bridge_automations(doc, providers={"al": al.bridge_brightness})
        on = by_id(bridges)["lm_bridge_reading_lamp"]["actions"][0]["choose"][0]
        self.assertEqual(
            on["conditions"],
            [
                {
                    "condition": "state",
                    "entity_id": "input_boolean.lm_soft_reading_lamp",
                    "state": "on",
                }
            ],
        )
        # The gate only chooses between two turn_ons, both on the bridge's own target and
        # both inside the soft-on branch; the provider's data is all that differs.
        gate, choice = on["sequence"]
        self.assertEqual(list(gate), ["variables"])
        [with_target], [without] = choice["then"], choice["else"]
        for turn_on in (with_target, without):
            self.assertEqual(turn_on["action"], "light.turn_on")
            self.assertEqual(turn_on["target"], {"entity_id": "light.reading_lamp"})
        self.assertEqual(set(with_target) - set(without), {"data"})
        off = by_id(bridges)["lm_bridge_reading_lamp"]["actions"][0]["choose"][1]
        self.assertEqual(
            off["sequence"],
            [{"action": "light.turn_off", "target": {"entity_id": "light.reading_lamp"}}],
        )

        def also(config_, room, bulb_):
            return {"brightness": 1} if bulb_["id"] == "reading_lamp" else None

        with self.assertRaises(ValueError) as caught:
            switching.bridge_automations(doc, providers={"al": al.bridge_brightness, "pr": also})
        self.assertIn("more than one module", str(caught.exception))


@unittest.skipUnless(ha_script.AVAILABLE, "jinja2 is not installed (test-only dependency)")
class BehaviourTest(unittest.TestCase):
    """The generated automations run against a fake HA, through the core bridges."""

    def setUp(self):
        self.doc = committed_config()
        with adaptive_wired():
            lm = generate.generate(self.doc, "lm")
        self.package = al.generate(self.doc)
        self.automations = by_id(self.package["automation"])
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
                "input_boolean.al_hold_living_room": "on",
                "input_select.al_scene_living_room": "afternoon",
                "sensor.living_room_lux": "42",
            },
            automations=[a for a in lm["automation"] if a["id"].startswith("lm_bridge_")],
            options={"input_select.al_scene_living_room": list(config.SECTIONS)},
        )

    def publish(self, rows):
        """Set the almanac sensor's attributes, as P5.2's publish will."""
        self.ha.set(almanac.entity_id(ROOM), "2026-10-17", {"friendly_name": "AL Almanac", **rows})

    def crossover(self, section):
        # automation.trigger skips conditions: the runtime fires scenes that way.
        self.assertTrue(
            self.ha.run(self.automations[f"al_scene_{ROOM}_{section}"], check_conditions=False)
        )

    def light(self, bulb_id):
        return self.ha.state(f"light.{bulb_id}"), self.ha.attr(f"light.{bulb_id}", "brightness")

    def lights_and_softs(self):
        return [
            (s, e)
            for s, e in self.ha.services()
            if e.startswith(("light.", "input_boolean.lm_soft_"))
        ]

    def test_with_no_almanac_a_crossover_leaves_every_bulb_as_it_is(self):
        before = copy.deepcopy(self.ha.states)
        self.crossover("night")
        self.assertEqual(self.lights_and_softs(), [])
        self.assertEqual(
            self.ha.services(),
            [
                ("input_boolean.turn_on", "input_boolean.al_active_living_room"),
                ("input_boolean.turn_off", "input_boolean.al_hold_living_room"),
                ("input_select.select_option", "input_select.al_scene_living_room"),
                ("input_boolean.turn_off", "input_boolean.al_active_living_room"),
            ],
        )
        for entity_id in [
            *(f"light.{b}" for b in ADAPTIVE),
            *(switching.soft_entity(b) for b in ADAPTIVE),
        ]:
            self.assertEqual(self.ha.states[entity_id], before[entity_id], entity_id)
        self.assertEqual(self.ha.state("input_select.al_scene_living_room"), "night")

    def test_an_absent_or_odd_almanac_leaves_bulbs_as_they_are(self):
        for rows in (
            {},  # the sensor, with no row for the section
            {"night": "not a mapping"},
            {"night": {"units": ["chandelier"]}},
            {"night": {"lux_target": 3}},  # no units
            {"night": {"units": {}}},  # nothing learned for any unit
            {"night": {"units": {"chandelier": None, "reading": None}}},
            {"night": {"units": {"chandelier": 0, "reading": 256}}},  # out of range
            {"night": {"units": {"chandelier": True, "reading": "dim"}}},
            {"night": {"units": {"chandelier": "128", "reading": "OFF"}}},
        ):
            with self.subTest(rows=rows):
                self.setUp()
                self.publish(rows)
                self.crossover("night")
                self.assertEqual(self.lights_and_softs(), [])

    def test_a_crossover_drives_each_unit_through_its_soft_switches(self):
        self.publish({"night": {"units": {"chandelier": 60, "reading": "off"}}})
        self.crossover("night")
        # Chandelier: 1 was on, so its brightness is pushed (guarded, with the section's
        # transition); 2 and 3 were off, so their soft switches turn on and their bridges
        # bring them up at the target, through the fragment. Reading: held off, so its soft
        # switch turns off and its bridge turns the bulb off.
        scene = f"al_scene_{ROOM}_night"
        self.assertEqual(
            self.ha.services(scene)[3:-1],
            [
                ("light.turn_on", "light.chandelier_1"),
                ("input_boolean.turn_on", "input_boolean.lm_soft_chandelier_1"),
                ("input_boolean.turn_on", "input_boolean.lm_soft_chandelier_2"),
                ("input_boolean.turn_on", "input_boolean.lm_soft_chandelier_3"),
                ("input_boolean.turn_off", "input_boolean.lm_soft_reading_lamp"),
            ],
        )
        self.assertEqual(self.ha.calls[3][2], {"brightness": 60, "transition": 120})
        for bulb_id in CHANDELIER:
            self.assertEqual(self.light(bulb_id), ("on", 60), bulb_id)
        self.assertEqual(self.light("reading_lamp"), ("off", None))
        self.assertEqual(
            [s for s, e in self.ha.services("lm_bridge_reading_lamp")], ["light.turn_off"]
        )
        # Nothing in the al_ lane turned a light off itself, or powered one on.
        self.assertEqual(
            [s for s, _ in self.ha.services(scene) if s.startswith("light.")], ["light.turn_on"]
        )
        self.assertEqual(self.ha.delays, [{"seconds": 120 + al.SETTLE_SECONDS}])
        self.assertEqual(self.ha.state("input_boolean.al_active_living_room"), "off")
        self.assertEqual(self.ha.state("input_boolean.al_hold_living_room"), "off")

    def test_a_brightness_push_never_powers_a_bulb_on(self):
        # Chandelier 1's soft switch is on but the bulb was turned off out of band: the push
        # is skipped (a brightness command would power it on), and its soft switch doesn't
        # change, so its bridge doesn't fire either.
        self.ha.set("light.chandelier_1", "off", {"brightness": None})
        self.publish({"day": {"units": {"chandelier": 90, "reading": 90}}})
        self.crossover("day")
        self.assertEqual(self.light("chandelier_1"), ("off", None))
        self.assertNotIn(("light.turn_on", "light.chandelier_1"), self.ha.services())

    def test_one_units_target_is_independent_of_anothers(self):
        self.publish({"sunset": {"units": {"reading": 30}}})
        self.crossover("sunset")
        self.assertEqual(self.light("reading_lamp"), ("on", 30))
        self.assertEqual(self.light("chandelier_2"), ("off", None))
        self.assertEqual(self.ha.state("input_boolean.lm_soft_chandelier_2"), "off")

    def test_the_bridge_applies_a_target_and_otherwise_takes_the_plain_path(self):
        bridge = by_id(self.ha.automations)["lm_bridge_chandelier_2"]
        target = {"day": {"units": {"chandelier": 140}}}
        # (case, the select, the almanac's rows, the brightness it comes on at, data sent).
        # Chandelier 2 is off, and was at 60 when it went off.
        cases = [
            ("a target", "day", target, 140, {"brightness": 140}),
            (
                "a float target",
                "day",
                {"day": {"units": {"chandelier": 140.6}}},
                140,
                {"brightness": 140},
            ),
            ("the lowest", "day", {"day": {"units": {"chandelier": 1}}}, 1, {"brightness": 1}),
            (
                "the highest",
                "day",
                {"day": {"units": {"chandelier": 255}}},
                255,
                {"brightness": 255},
            ),
            ("no almanac", None, None, 60, {}),
            (
                "held off: a person's power decision",
                "day",
                {"day": {"units": {"chandelier": "off"}}},
                60,
                {},
            ),
            ("nothing learned", "day", {"day": {"units": {"chandelier": None}}}, 60, {}),
            ("another section's row only", "night", target, 60, {}),
            ("the select unavailable", "unavailable", target, 60, {}),
            ("the select missing", None, target, 60, {}),
            ("an odd row", "day", {"day": ["chandelier"]}, 60, {}),
            ("an odd units", "day", {"day": {"units": ["chandelier"]}}, 60, {}),
            ("out of range", "day", {"day": {"units": {"chandelier": 999}}}, 60, {}),
            ("zero", "day", {"day": {"units": {"chandelier": 0}}}, 60, {}),
            ("a boolean", "day", {"day": {"units": {"chandelier": True}}}, 60, {}),
            ("a number as text", "day", {"day": {"units": {"chandelier": "140"}}}, 60, {}),
        ]
        for name, section, rows, expected, sent in cases:
            with self.subTest(name):
                self.setUp()
                self.ha.set("input_select.al_scene_living_room", section)
                if rows is not None:
                    self.publish(rows)
                self.ha.memory["light.chandelier_2"] = 60
                self.ha.set("input_boolean.lm_soft_chandelier_2", "on")
                self.assertTrue(self.ha.run(bridge))
                self.assertEqual(self.light("chandelier_2"), ("on", expected))
                [(service, entity_id, data, by)] = self.ha.calls
                self.assertEqual((service, by), ("light.turn_on", "lm_bridge_chandelier_2"))
                self.assertEqual(data, sent)
        # A bulb already on (switched out of band) with no target keeps its level.
        self.setUp()
        self.ha.set("light.chandelier_2", "on", {"brightness": 77})
        self.ha.set("input_boolean.lm_soft_chandelier_2", "on")
        self.ha.run(bridge)
        self.assertEqual(self.light("chandelier_2"), ("on", 77))
        self.assertEqual(self.ha.calls[0][2], {})

    def test_a_unit_held_off_switched_on_by_hand_comes_back_at_its_own_level(self):
        # The P5.1 must-fix. The chandelier is held off for sleep: the crossover holds its
        # soft switches off and the bridges turn the bulbs off (chandelier 1 was at 128). At
        # 3 a.m. a hard switch turns chandelier 1's soft switch on: the unit has no on-target
        # for the section, so the bridge takes the plain turn_on and the bulb comes back at
        # its own last level, as a switched-only bulb would. It came on at 255 before.
        self.publish({"sleep": {"units": {"chandelier": "off", "reading": 20}}})
        self.crossover("sleep")
        self.assertEqual(self.light("chandelier_1"), ("off", None))
        self.assertEqual(self.ha.state("input_boolean.lm_soft_chandelier_1"), "off")
        self.ha.calls.clear()
        wall = {
            "id": "lm_hard_wall",
            "actions": [
                {
                    "action": "input_boolean.turn_on",
                    "target": {"entity_id": "input_boolean.lm_soft_chandelier_1"},
                }
            ],
        }
        self.ha.run(wall)
        self.assertEqual(self.light("chandelier_1"), ("on", 128))
        self.assertNotEqual(self.light("chandelier_1")[1], 255)
        [bridge_call] = [c for c in self.ha.calls if c[3] == "lm_bridge_chandelier_1"]
        self.assertEqual(bridge_call[:3], ("light.turn_on", "light.chandelier_1", {}))
        # The reading lamp has a target for sleep: switched on by hand, it comes on at it.
        self.ha.set("light.reading_lamp", "off", {"brightness": None})
        self.ha.set("input_boolean.lm_soft_reading_lamp", "off")
        wall["actions"][0]["target"]["entity_id"] = "input_boolean.lm_soft_reading_lamp"
        self.ha.run(wall)
        self.assertEqual(self.light("reading_lamp"), ("on", 20))

    def maintain(self, lux, row=None, section="afternoon"):
        self.setUp()
        self.ha.set("input_boolean.al_hold_living_room", "off")
        self.ha.set("input_select.al_scene_living_room", section)
        self.ha.set("sensor.living_room_lux", lux)
        if row is not None:
            self.publish({section: row})
        return self.ha.run(self.automations[f"al_maint_{ROOM}"])

    ROW = {
        "units": {},
        "lux_target": 100,
        "lux_margin": 5,
        "max_step_pct": 0.04,
        "maintenance_enabled": True,
    }

    def test_maintenance_nudges_only_bulbs_that_are_on(self):
        self.assertTrue(self.maintain("50", self.ROW))
        # Chandelier 1 (on, 128) and the reading lamp (on, 200) step up by 4% (5 and 8);
        # chandelier 2 and 3 are off and untouched.
        self.assertEqual(self.light("chandelier_1"), ("on", 133))
        self.assertEqual(self.light("reading_lamp"), ("on", 208))
        self.assertEqual(self.light("chandelier_2"), ("off", None))
        self.assertEqual(
            self.lights_and_softs(),
            [("light.turn_on", "light.chandelier_1"), ("light.turn_on", "light.reading_lamp")],
        )
        self.assertEqual(self.ha.calls[1][2], {"brightness": 133, "transition": 30})
        self.assertEqual(self.ha.state("input_boolean.al_active_living_room"), "off")  # lowered
        self.assertTrue(self.maintain("150", self.ROW))
        self.assertEqual(self.light("chandelier_1"), ("on", 123))

    def test_maintenance_steps_are_bounded_and_never_turn_a_light_off(self):
        row = {**self.ROW, "max_step_pct": 0.9}
        self.setUp()
        self.assertTrue(self.maintain("10", row))
        self.assertEqual(self.light("chandelier_1"), ("on", 138))  # at most 10 a step
        self.setUp()
        self.ha.set("light.chandelier_1", "on", {"brightness": 1})
        self.ha.set("input_boolean.al_hold_living_room", "off")
        self.ha.set("input_select.al_scene_living_room", "afternoon")
        self.ha.set("sensor.living_room_lux", "900")
        self.publish({"afternoon": self.ROW})
        self.ha.run(self.automations[f"al_maint_{ROOM}"])
        self.assertEqual(self.light("chandelier_1"), ("on", 1))  # floored at 1

    def test_maintenance_stays_out_when_it_should(self):
        for name, lux, row in (
            ("no almanac", "50", None),
            ("disabled for the section", "50", {**self.ROW, "maintenance_enabled": False}),
            ("enabled not quite true", "50", {**self.ROW, "maintenance_enabled": "yes"}),
            ("no target yet", "50", {**self.ROW, "lux_target": None}),
            ("in band", "103", self.ROW),
            ("the sensor at its floor", "0", self.ROW),
            ("the sensor unavailable", "unavailable", self.ROW),
        ):
            with self.subTest(name):
                self.assertFalse(self.maintain(lux, row))
                self.assertEqual(self.ha.calls, [])  # not even the guard
        # Guard or hold raised: the automation's own conditions keep it from running.
        for helper in ("al_active", "al_hold"):
            with self.subTest(helper):
                self.setUp()
                self.ha.set("input_select.al_scene_living_room", "afternoon")
                self.ha.set("sensor.living_room_lux", "50")
                self.publish({"afternoon": self.ROW})
                self.ha.set("input_boolean.al_hold_living_room", "off")
                self.ha.set(f"input_boolean.{helper}_living_room", "on")
                self.assertFalse(self.ha.run(self.automations[f"al_maint_{ROOM}"]))
                self.assertEqual(self.ha.calls, [])

    def test_maintenance_leaves_a_bulb_whose_soft_switch_is_off(self):
        # On out of band, its soft switch off: not the plane's to touch.
        self.setUp()
        self.ha.set("input_boolean.lm_soft_reading_lamp", "off")
        self.ha.set("input_boolean.al_hold_living_room", "off")
        self.ha.set("input_select.al_scene_living_room", "afternoon")
        self.ha.set("sensor.living_room_lux", "50")
        self.publish({"afternoon": self.ROW})
        self.ha.run(self.automations[f"al_maint_{ROOM}"])
        self.assertEqual(self.light("reading_lamp"), ("on", 200))

    def test_maintenance_survives_numbers_ha_wont_parse(self):
        # HA keeps a render that isn't a plain decimal (1e-05, nan, inf) as a string, so a
        # number passed between variables can turn into text: maintenance reads each from
        # the row where it uses it, and a tiny lux or step still runs to the end.
        for name, lux, row, expected in (
            ("a tiny step", "50", {**self.ROW, "max_step_pct": 1e-05}, 129),
            ("a step of 'nan'", "50", {**self.ROW, "max_step_pct": "nan"}, 129),
            ("a step of 'inf'", "50", {**self.ROW, "max_step_pct": "inf"}, 138),
            ("a tiny lux", "0.00001", self.ROW, 133),
            ("a tiny margin", "99.99", {**self.ROW, "lux_margin": 1e-05}, 133),
        ):
            with self.subTest(name):
                self.assertTrue(self.maintain(lux, row))
                self.assertEqual(self.light("chandelier_1"), ("on", expected))
                self.assertEqual(self.ha.state("input_boolean.al_active_living_room"), "off")
        # A row HA can't read back at all (a float nan in it: no literal) is no row: the
        # run stops before raising the guard.
        self.assertFalse(self.maintain("50", {**self.ROW, "max_step_pct": float("nan")}))
        self.assertEqual(self.ha.calls, [])

    def test_the_last_run_out_lowers_the_guard(self):
        # A crossover fired while maintenance is mid-run (automation.trigger skips the
        # scene's conditions): the scene finishes first, and leaves the guard to it.
        self.publish({"day": {"units": {"chandelier": 90}}})
        self.ha.set("automation.al_maint_living_room", "on", {"current": 1})
        self.crossover("day")
        self.assertEqual(self.ha.state("input_boolean.al_active_living_room"), "on")
        # ...and a maintenance run that ends while a crossover still fades leaves it too.
        self.assertTrue(self.maintain("50", self.ROW))
        self.ha.set("automation.al_scene_living_room_day", "on", {"current": 1})
        self.ha.set("input_boolean.al_active_living_room", "off")
        self.ha.set("input_boolean.al_hold_living_room", "off")
        self.assertTrue(self.ha.run(self.automations[f"al_maint_{ROOM}"]))
        self.assertEqual(self.ha.state("input_boolean.al_active_living_room"), "on")
        # With nothing else running, it is lowered (as in every other test).
        self.ha.set("automation.al_scene_living_room_day", "on", {"current": 0})
        self.ha.set("input_boolean.al_active_living_room", "off")
        self.assertTrue(self.ha.run(self.automations[f"al_maint_{ROOM}"]))
        self.assertEqual(self.ha.state("input_boolean.al_active_living_room"), "off")

    def test_the_guard_watchdog(self):
        guard = self.automations["al_watchdog_guard_living_room"]
        on = "input_boolean.al_active_living_room"
        # Stuck on for its time: released, and said so.
        self.ha.set(on, "on")
        self.assertTrue(self.ha.run(guard, trigger={"id": "stuck"}))
        self.assertEqual(self.ha.state(on), "off")
        self.assertEqual(
            self.ha.notifications[0]["notification_id"], "al_watchdog_guard_living_room"
        )
        # Automations reloaded (a deploy cut a run short): a guard no run holds is released
        # at once, quietly; one a run still holds is left to it.
        self.setUp()
        self.ha.set(on, "on")
        self.ha.set("automation.al_scene_living_room_sunset", "on", {"current": 1})
        self.assertFalse(self.ha.run(guard, trigger={"id": "reloaded"}))
        self.assertEqual(self.ha.state(on), "on")
        self.ha.set("automation.al_scene_living_room_sunset", "on", {"current": 0})
        self.assertTrue(self.ha.run(guard, trigger={"id": "reloaded"}))
        self.assertEqual(self.ha.state(on), "off")
        self.assertEqual(self.ha.notifications, [])
        # A guard already off is left alone.
        self.assertFalse(self.ha.run(guard, trigger={"id": "stuck"}))
        self.assertEqual(self.ha.calls[-1][0], "input_boolean.turn_off")
        self.assertEqual(len(self.ha.calls), 1)

    def test_the_stale_scene_watchdog(self):
        self.ha.run(self.automations["al_watchdog_stale_living_room"])
        self.assertIn("has not changed section", self.ha.notifications[0]["message"])


if __name__ == "__main__":
    unittest.main()
