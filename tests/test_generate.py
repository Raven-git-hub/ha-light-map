"""app.core.generate: the namespace registry, lanes, and deterministic YAML."""

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

from app.core import generate
from tests.fakes import committed_config

ROOT = Path(__file__).resolve().parent.parent

RENDER = (
    "import sys; from app.core import generate; from tests.fakes import committed_config; "
    "sys.stdout.write(generate.to_yaml(generate.generate(committed_config(), 'lm')))"
)


def lm_yaml(doc):
    return generate.to_yaml(generate.generate(doc, "lm"))


def with_event_source(doc, event_data):
    doc["rooms"][0]["switching_groups"][1]["hard_switches"] = [
        {
            "id": "corner_remote",
            "name": "Corner remote",
            "source": {"type": "event", "event_type": "zha_event", "event_data": event_data},
            "mode": "toggle",
        }
    ]
    return doc


class RegistryTest(unittest.TestCase):
    def test_the_core_registers_lm(self):
        self.assertEqual(generate.namespaces(), ["lm"])

    def test_registration(self):
        def al(doc):
            return {
                "input_boolean": {"al_hold_lounge": {"name": "AL Hold lounge"}},
                "automation": [],
            }

        with mock.patch.dict(generate._GENERATORS):
            generate.register_namespace("al", al)
            self.assertEqual(generate.namespaces(), ["lm", "al"])
            self.assertEqual(generate.generate({}, "al")["input_boolean"], al({})["input_boolean"])
            for prefix in ("al", "lm", "AL", "al_", "", "1x"):
                with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                    generate.register_namespace(prefix, al)
        self.assertEqual(generate.namespaces(), ["lm"])

    def test_an_unregistered_namespace(self):
        with self.assertRaises(ValueError):
            generate.generate(committed_config(), "al")

    def test_a_generator_cannot_mint_names_in_another_lane(self):
        def trespass(doc):
            return {"automation": [{"id": "lm_bridge_porch"}], "input_boolean": {"al_x": {}}}

        with mock.patch.dict(generate._GENERATORS):
            generate.register_namespace("al", trespass)
            with self.assertRaises(ValueError) as caught:
                generate.generate({}, "al")
        self.assertIn("lm_bridge_porch", str(caught.exception))

    def test_lanes(self):
        self.assertTrue(generate.in_lane("lm_soft_porch", "lm"))
        self.assertTrue(generate.in_lane("al_scene_lounge_day", "al"))
        for name in ("al_scene_lounge", "lmx_soft", "lm", "soft_lm_x"):
            self.assertFalse(generate.in_lane(name, "lm"), name)
        # Were the adaptive lane ever folded under lm_al_, lm would still not claim it.
        with mock.patch.dict(generate._GENERATORS, {"lm_al": lambda doc: {}}):
            self.assertFalse(generate.in_lane("lm_al_scene_lounge", "lm"))
            self.assertTrue(generate.in_lane("lm_al_scene_lounge", "lm_al"))
            self.assertTrue(generate.in_lane("lm_soft_porch", "lm"))


class DeterminismTest(unittest.TestCase):
    def test_same_config_same_bytes(self):
        self.assertEqual(lm_yaml(committed_config()), lm_yaml(committed_config()))

    def test_same_bytes_whatever_order_the_config_lists_things_in(self):
        doc = with_event_source(committed_config(), {"command": "toggle", "args": [], "ieee": "x"})
        shuffled = with_event_source(
            committed_config(), {"ieee": "x", "args": [], "command": "toggle"}
        )
        shuffled["rooms"].reverse()
        for room in shuffled["rooms"]:
            room["bulbs"].reverse()
            room["switching_groups"].reverse()
            for group in room["switching_groups"]:
                group["members"].reverse()
        self.assertEqual(lm_yaml(doc), lm_yaml(shuffled))

    def test_same_bytes_in_every_process(self):
        # Hash randomisation changes set and str-keyed hash order between interpreters; a
        # set leaking into output would show up here.
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
        self.assertEqual(outputs, {lm_yaml(committed_config())})

    def test_generation_leaves_the_config_alone(self):
        doc = committed_config()
        before = json.dumps(doc, sort_keys=True)
        package = generate.generate(doc, "lm")
        self.assertEqual(json.dumps(doc, sort_keys=True), before)
        # Nor does the output share structure with it.
        package["automation"][0]["triggers"].clear()
        self.assertEqual(lm_yaml(doc), lm_yaml(committed_config()))


class YamlTest(unittest.TestCase):
    def test_round_trips(self):
        package = generate.generate(committed_config(), "lm")
        self.assertEqual(yaml.safe_load(generate.to_yaml(package)), package)

    def test_jinja_passes_through_verbatim(self):
        template = "{{ is_state('input_boolean.lm_soft_a', 'on') or states('x') | int > 3 }}"
        text = generate.to_yaml({"value_template": template})
        self.assertEqual(text, f'value_template: "{template}"\n')
        self.assertEqual(yaml.safe_load(text)["value_template"], template)

    def test_no_anchors_for_shared_objects(self):
        shared = {"entity_id": ["input_boolean.lm_soft_a"]}
        text = generate.to_yaml({"then": [{"target": shared}], "else": [{"target": shared}]})
        self.assertNotIn("&", text)
        self.assertNotIn("*", text)

    def test_refuses_sets(self):
        with self.assertRaises(yaml.representer.RepresenterError):
            generate.to_yaml({"targets": {"a", "b"}})

    def test_on_and_off_stay_strings(self):
        data = {"from": "off", "to": "on", "state": "on"}
        self.assertEqual(yaml.safe_load(generate.to_yaml(data)), data)

    def test_layout(self):
        text = lm_yaml(committed_config())
        self.assertTrue(
            text.startswith(
                "input_boolean:\n"
                "  lm_soft_chandelier_1:\n"
                "    name: LM Soft chandelier_1\n"
                "    icon: mdi:light-switch\n"
            ),
            text[:200],
        )
        self.assertIn(
            "automation:\n  - id: lm_bridge_chandelier_1\n    alias: lm_bridge_chandelier_1\n",
            text,
        )
        self.assertIn("    mode: restart\n    triggers:\n      - trigger: state\n", text)

    def test_multi_line_strings_are_blocks(self):
        self.assertEqual(generate.to_yaml({"d": "one\ntwo\n"}), "d: |\n  one\n  two\n")

    def test_unicode_is_kept(self):
        doc = copy.deepcopy(committed_config())
        doc["rooms"][1]["bulbs"][0]["name"] = "Küche strip"
        self.assertIn("Küche strip", lm_yaml(doc))


if __name__ == "__main__":
    unittest.main()
