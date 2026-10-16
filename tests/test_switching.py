"""app.core.switching: the `lm` generator (soft switches, hard->soft, soft->bulb)."""

import unittest
from unittest import mock

from app.core import config, deploy, generate, switching
from tests.fakes import committed_config, state_trigger_matches

# A change of state HA reports when an entity is restored, reloaded, created, removed,
# loses its device, or gets it back: none of them is anyone flipping a switch.
NOT_A_FLIP = [
    (None, "on"),
    (None, "off"),
    ("on", None),
    ("off", None),
    ("unavailable", "on"),
    ("unavailable", "off"),
    ("unknown", "on"),
    ("on", "unavailable"),
    ("off", "unknown"),
    ("on", "on"),
    ("off", "off"),
]


def living(doc):
    return doc["rooms"][0]


def by_id(automations):
    return {a["id"]: a for a in automations}


def fires(automation, entity_id, old, new):
    return any(state_trigger_matches(t, entity_id, old, new) for t in automation["triggers"])


def bind(doc, binding, *, group=None, bulb=None):
    """Replace the fixture's bindings with one, on a group or a bulb, and return the
    automation it generates."""
    for room in doc["rooms"]:
        for holder in [*room["bulbs"], *room["switching_groups"]]:
            holder["hard_switches"] = []
    for holder in doc["unassigned"]:
        holder["hard_switches"] = []
    rooms_holders = [h for r in doc["rooms"] for h in [*r["bulbs"], *r["switching_groups"]]]
    target = next(h for h in [*rooms_holders, *doc["unassigned"]] if h["id"] == (group or bulb))
    target["hard_switches"] = [{"id": "hs", "name": "Wall switch", **binding}]
    config.validate(doc)
    config.cross_check(doc)
    [automation] = switching.hard_switch_automations(doc)
    return automation


def state(entity_id):
    return {"type": "state", "entity_id": entity_id}


class SoftSwitchTest(unittest.TestCase):
    def test_one_per_bulb_and_none_per_group(self):
        softs = switching.soft_switches(committed_config())
        self.assertEqual(
            list(softs),
            [
                "lm_soft_chandelier_1",
                "lm_soft_chandelier_2",
                "lm_soft_chandelier_3",
                "lm_soft_kitchen_strip",
                "lm_soft_porch",
                "lm_soft_reading_lamp",
            ],
        )
        self.assertNotIn("lm_soft_living_all", softs)
        self.assertNotIn("lm_soft_reading_corner", softs)

    def test_each_name_makes_ha_create_the_id_the_automations_address(self):
        for soft_id, fields in switching.soft_switches(committed_config()).items():
            self.assertEqual(deploy._slugify(fields["name"]), soft_id)
            self.assertEqual(set(fields), {"name", "icon"})

    def test_an_unassigned_bulb_has_the_full_plane(self):
        package = switching.generate(committed_config())
        self.assertIn("lm_soft_porch", package["input_boolean"])
        self.assertIn("lm_bridge_porch", [a["id"] for a in package["automation"]])


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.bridges = by_id(switching.bridge_automations(committed_config(), providers={}))
        self.bridge = self.bridges["lm_bridge_kitchen_strip"]

    def test_one_per_bulb(self):
        bulbs = ["chandelier_1", "chandelier_2", "chandelier_3", "kitchen_strip", "porch"]
        bulbs.append("reading_lamp")
        self.assertEqual(list(self.bridges), [f"lm_bridge_{b}" for b in bulbs])

    def branches(self, bridge=None):
        [choose] = (bridge or self.bridge)["actions"]
        return {branch["conditions"][0]["state"]: branch for branch in choose["choose"]}

    def test_soft_on_turns_the_bulb_on_and_soft_off_turns_it_off(self):
        branches = self.branches()
        soft = "input_boolean.lm_soft_kitchen_strip"
        for wanted, branch in branches.items():
            self.assertEqual(
                branch["conditions"],
                [{"condition": "state", "entity_id": soft, "state": wanted}],
            )
        self.assertEqual(
            branches["on"]["sequence"],
            [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen_strip"}}],
        )
        self.assertEqual(
            branches["off"]["sequence"],
            [{"action": "light.turn_off", "target": {"entity_id": "light.kitchen_strip"}}],
        )

    def test_plain_path_keeps_the_last_brightness(self):
        self.assertNotIn("data", self.branches()["on"]["sequence"][0])

    def test_fires_only_on_a_real_flip_never_on_a_restart_or_reload(self):
        soft = "input_boolean.lm_soft_kitchen_strip"
        self.assertTrue(fires(self.bridge, soft, "off", "on"))
        self.assertTrue(fires(self.bridge, soft, "on", "off"))
        for old, new in NOT_A_FLIP:
            with self.subTest(old=old, new=new):
                self.assertFalse(fires(self.bridge, soft, old, new))
        self.assertFalse(fires(self.bridge, "light.kitchen_strip", "off", "on"))

    def test_the_last_flip_wins(self):
        self.assertEqual(self.bridge["mode"], "restart")

    def test_addresses_bulbs_by_entity_id_only(self):
        package = repr(switching.generate(committed_config()))
        self.assertNotIn("reg_light_", package)
        self.assertNotIn("ha_match_key", package)


class BridgeBrightnessTest(unittest.TestCase):
    def provider(self, data, bulbs=("chandelier_1",)):
        self.seen = []

        def supply(doc, room, bulb):
            self.seen.append((room["id"] if room else None, bulb["id"]))
            return data if bulb["id"] in bulbs else None

        return supply

    def on_branch(self, bridges, bulb_id):
        [choose] = by_id(bridges)[f"lm_bridge_{bulb_id}"]["actions"]
        return choose["choose"][0]

    def test_a_module_supplies_the_brightness_on_soft_on_only(self):
        template = "{{ state_attr('sensor.al_almanac_living_room', 'chandelier') | int }}"
        supply = self.provider({"transition": 2, "brightness_pct": template})
        bridges = switching.bridge_automations(committed_config(), providers={"al": supply})
        on = self.on_branch(bridges, "chandelier_1")
        self.assertEqual(on["conditions"][0]["state"], "on")
        self.assertEqual(on["sequence"][0]["data"], {"brightness_pct": template, "transition": 2})
        self.assertEqual(list(on["sequence"][0]["data"]), ["brightness_pct", "transition"])
        self.assertNotIn("data", self.on_branch(bridges, "chandelier_2")["sequence"][0])
        [choose] = by_id(bridges)["lm_bridge_chandelier_1"]["actions"]
        self.assertNotIn("data", choose["choose"][1]["sequence"][0])
        # The template is carried verbatim, never rendered.
        self.assertIn(f'"{template}"', generate.to_yaml(bridges))

    def test_the_provider_sees_each_bulb_with_its_room(self):
        supply = self.provider({"brightness": 1}, bulbs=())
        switching.bridge_automations(committed_config(), providers={"al": supply})
        self.assertIn(("living_room", "chandelier_1"), self.seen)
        self.assertIn((None, "porch"), self.seen)

    def test_a_module_can_set_brightness_and_nothing_else(self):
        for data in (
            {"entity_id": "light.other"},
            {"brightness": 10, "target": {}},
            {"brightness": True},
            {"brightness": [10]},
            {},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                switching.bridge_automations(
                    committed_config(), providers={"al": self.provider(data)}
                )

    def test_one_module_per_bulb(self):
        providers = {"al": self.provider({"brightness": 1}), "pr": self.provider({"brightness": 2})}
        with self.assertRaises(ValueError) as caught:
            switching.bridge_automations(committed_config(), providers=providers)
        self.assertIn("more than one module", str(caught.exception))

    def test_registered_providers_reach_the_lm_generator(self):
        supply = self.provider({"brightness": 128})
        with mock.patch.dict(switching._BRIDGE_BRIGHTNESS, clear=True):
            switching.register_bridge_brightness("al", supply)
            with self.assertRaises(ValueError):
                switching.register_bridge_brightness("al", supply)
            with self.assertRaises(ValueError):
                switching.register_bridge_brightness("lm", supply)
            bridges = generate.generate(committed_config(), "lm")["automation"]
        self.assertEqual(
            self.on_branch(bridges, "chandelier_1")["sequence"][0]["data"], {"brightness": 128}
        )
        self.assertEqual(switching.bridge_brightness_providers(), {})


class HardSwitchTest(unittest.TestCase):
    def test_one_per_binding_with_its_targets(self):
        automations = by_id(switching.hard_switch_automations(committed_config()))
        self.assertEqual(list(automations), ["lm_hard_living_all_off", "lm_hard_reading_switch"])
        self.assertEqual(
            automations["lm_hard_living_all_off"]["actions"],
            [
                {
                    "action": "input_boolean.turn_off",
                    "target": {
                        "entity_id": [
                            "input_boolean.lm_soft_chandelier_1",
                            "input_boolean.lm_soft_chandelier_2",
                            "input_boolean.lm_soft_chandelier_3",
                            "input_boolean.lm_soft_reading_lamp",
                        ]
                    },
                }
            ],
        )

    def test_a_bulb_binding_targets_its_own_soft_switch(self):
        doc = committed_config()
        automation = bind(
            doc, {"source": state("switch.wall"), "mode": "press", "turn": "on"}, bulb="porch"
        )
        self.assertEqual(
            automation["actions"],
            [
                {
                    "action": "input_boolean.turn_on",
                    "target": {"entity_id": ["input_boolean.lm_soft_porch"]},
                }
            ],
        )

    def test_a_group_binding_targets_every_member_and_never_a_group_entity(self):
        doc = committed_config()
        living(doc)["switching_groups"][0]["members"].reverse()
        automation = bind(
            doc,
            {"source": state("switch.wall"), "mode": "press", "turn": "off"},
            group="living_all",
        )
        targets = automation["actions"][0]["target"]["entity_id"]
        self.assertEqual(targets, sorted(targets))
        self.assertEqual(len(targets), 4)
        self.assertNotIn("living_all", repr(automation["actions"]))

    def test_toggle_is_state_aware_across_the_group(self):
        doc = committed_config()
        automation = bind(
            doc, {"source": state("switch.wall"), "mode": "toggle"}, group="living_all"
        )
        [action] = automation["actions"]
        softs = [
            f"input_boolean.lm_soft_{b}"
            for b in ("chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp")
        ]
        any_on = " or ".join(f"is_state('{s}', 'on')" for s in softs)
        self.assertEqual(
            action["if"], [{"condition": "template", "value_template": f"{{{{ {any_on} }}}}"}]
        )
        self.assertEqual(
            action["then"], [{"action": "input_boolean.turn_off", "target": {"entity_id": softs}}]
        )
        self.assertEqual(
            action["else"], [{"action": "input_boolean.turn_on", "target": {"entity_id": softs}}]
        )
        # HA Jinja, passed through as an inert string.
        self.assertIn(f'value_template: "{{{{ {any_on} }}}}"', generate.to_yaml(automation))

    def test_toggle_on_one_bulb(self):
        automation = bind(
            committed_config(), {"source": state("switch.wall"), "mode": "toggle"}, bulb="porch"
        )
        self.assertEqual(
            automation["actions"][0]["if"][0]["value_template"],
            "{{ is_state('input_boolean.lm_soft_porch', 'on') }}",
        )

    def test_press_sets_the_targets_one_way(self):
        for turn in ("on", "off"):
            automation = bind(
                committed_config(),
                {"source": state("switch.wall"), "mode": "press", "turn": turn},
                group="living_all",
            )
            self.assertEqual(automation["actions"][0]["action"], f"input_boolean.turn_{turn}")

    def test_follow_tracks_the_source(self):
        automation = bind(
            committed_config(),
            {"source": state("switch.wall"), "mode": "follow"},
            group="living_all",
        )
        self.assertEqual(
            [(t["from"], t["to"], t["id"]) for t in automation["triggers"]],
            [("off", "on", "on"), ("on", "off", "off")],
        )
        [choose] = automation["actions"]
        self.assertEqual(
            [(b["conditions"], b["sequence"][0]["action"]) for b in choose["choose"]],
            [
                ([{"condition": "trigger", "id": "on"}], "input_boolean.turn_on"),
                ([{"condition": "trigger", "id": "off"}], "input_boolean.turn_off"),
            ],
        )
        self.assertEqual(automation["conditions"], [])
        for old, new in NOT_A_FLIP:
            with self.subTest(old=old, new=new):
                self.assertFalse(fires(automation, "switch.wall", old, new))

    def test_a_state_source_acts_on_every_real_change_and_nothing_else(self):
        for mode, extra in (("toggle", {}), ("press", {"turn": "off"})):
            automation = bind(
                committed_config(),
                {"source": state("event.button"), "mode": mode, **extra},
                bulb="porch",
            )
            self.assertTrue(fires(automation, "event.button", "off", "on"))
            self.assertTrue(fires(automation, "event.button", "on", "off"))
            # An event entity's state is the time of its last press.
            self.assertTrue(
                fires(automation, "event.button", "2026-10-15T21:04", "2026-10-15T21:05")
            )
            for old, new in NOT_A_FLIP:
                if old is None or new is None:
                    continue  # The trigger lets these through; the condition stops them.
                with self.subTest(mode=mode, old=old, new=new):
                    self.assertFalse(fires(automation, "event.button", old, new))
            self.assertEqual(
                automation["conditions"],
                [
                    {
                        "condition": "template",
                        "value_template": (
                            "{{ trigger.from_state is not none and trigger.to_state is not none }}"
                        ),
                    }
                ],
            )

    def test_an_event_source(self):
        source = {
            "type": "event",
            "event_type": "zha_event",
            "event_data": {"device_ieee": "00:11", "command": "on", "args": [{"b": 1, "a": 2}]},
        }
        automation = bind(
            committed_config(), {"source": source, "mode": "toggle"}, group="living_all"
        )
        self.assertEqual(
            automation["triggers"],
            [
                {
                    "trigger": "event",
                    "event_type": "zha_event",
                    "event_data": {
                        "args": [{"a": 2, "b": 1}],
                        "command": "on",
                        "device_ieee": "00:11",
                    },
                }
            ],
        )
        self.assertEqual(automation["conditions"], [])
        bare = bind(
            committed_config(),
            {"source": {"type": "event", "event_type": "x"}, "mode": "toggle"},
            bulb="porch",
        )
        self.assertEqual(bare["triggers"], [{"trigger": "event", "event_type": "x"}])

    def test_a_device_source_reads_a_press_and_addresses_nothing(self):
        source = {
            "type": "device",
            "device_id": "3f2a9c1e0b7d4e5f8a6b2c1d0e9f8a7b",
            "trigger": {
                "subtype": "turn_off",
                "type": "remote_button_short_press",
                "domain": "zha",
            },
        }
        automation = bind(
            committed_config(),
            {"source": source, "mode": "press", "turn": "off"},
            group="living_all",
        )
        self.assertEqual(
            automation["triggers"],
            [
                {
                    "trigger": "device",
                    "device_id": "3f2a9c1e0b7d4e5f8a6b2c1d0e9f8a7b",
                    "domain": "zha",
                    "subtype": "turn_off",
                    "type": "remote_button_short_press",
                }
            ],
        )
        self.assertNotIn("3f2a9c1e", repr(automation["actions"]))

    def test_presses_queue_in_order(self):
        for automation in switching.hard_switch_automations(committed_config()):
            self.assertEqual(automation["mode"], "queued")


class PackageTest(unittest.TestCase):
    def test_bridges_come_before_hard_switches(self):
        package = switching.generate(committed_config())
        self.assertEqual(list(package), ["input_boolean", "automation"])
        ids = [a["id"] for a in package["automation"]]
        self.assertEqual(ids[:6], sorted(i for i in ids if i.startswith("lm_bridge_")))
        self.assertEqual(ids[6:], ["lm_hard_living_all_off", "lm_hard_reading_switch"])

    def test_everything_is_in_the_lm_lane(self):
        package = generate.generate(committed_config(), "lm")
        for name in generate.artefact_ids(package):
            self.assertTrue(name.startswith("lm_"), name)

    def test_a_config_with_no_rooms(self):
        self.assertEqual(
            switching.generate(config.blank()), {"input_boolean": {}, "automation": []}
        )

    def test_mirrors_and_references(self):
        doc = committed_config()
        self.assertEqual(
            switching.bridge_mirrors(doc)["lm_bridge_porch"],
            {"helper": "input_boolean.lm_soft_porch", "entity_id": "light.porch"},
        )
        living(doc)["switching_groups"][1]["hard_switches"] = [
            {
                "id": "corner",
                "name": "Corner remote",
                "source": {
                    "type": "device",
                    "device_id": "abc123",
                    "trigger": {"domain": "zha", "type": "x"},
                },
                "mode": "toggle",
            }
        ]
        refs = switching.references(doc)
        self.assertEqual(refs["entities"]["light.porch"], "bulb 'porch'")
        self.assertEqual(refs["entities"]["switch.reading_relay"], "hard switch 'reading_switch'")
        self.assertEqual(refs["devices"], {"abc123": "hard switch 'corner'"})


if __name__ == "__main__":
    unittest.main()
