"""The cutover switch off (how the live container runs until P6b): P6a's changes are pure
computation or gated on the module being wired in, so everything the live app shows, plans,
applies and writes is P5.2b's, byte for byte.

`tests/golden/switch_off.json` was captured from `main` at the P5.2b merge, before any P6a
change: the deploy preview, the apply's answer and every write it made to HA, the preview
after it, every call the three made to HA (reads too, in order), and the Now tab and the
schedule editor's days on an ordinary day and on both of London's clock changes, against a
fake HA that still holds the old ha-adaptive-light's `al_` helpers and automations (one of
them under an id Light Map's own `al_` lane would generate). `lm_only.yaml` holds the
generated set to the same standard."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.core import config, deploy, generate
from app.main import create_app
from tests.fakes import FakeHA, committed_config

GOLDEN = Path(__file__).resolve().parent / "golden" / "switch_off.json"

UTC = timezone.utc

# What the live ha-adaptive-light holds beside Light Map: its helpers, named exactly as
# Light Map's al_ lane names them; a scene automation under the very id the lane would
# generate and its maintenance under its old one, each driving a mapped bulb; and a user's
# own automation and helpers.
OLD_SYSTEM = {
    "helpers": {
        "input_boolean": [
            {
                "id": "al_active_living_room",
                "name": "AL Active living_room",
                "icon": "mdi:lightbulb-auto",
            },
            {"id": "al_hold_living_room", "name": "AL Hold living_room"},
            {"id": "guest_mode", "name": "Guest mode"},
        ],
        "input_select": [
            {
                "id": "al_scene_living_room",
                "name": "AL Scene living_room",
                "options": ["Sunrise", "Day", "Afternoon", "Sunset", "Night", "Sleep"],
            },
        ],
    },
    "automations": [
        {
            "id": "al_scene_living_room_day",
            "alias": "AL SET SCENE",
            "triggers": [],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.chandelier_1"}}],
        },
        {
            "id": "al_maintenance_living_room",
            "alias": "AL Lux Maintenance",
            "triggers": [],
            "actions": [
                {"action": "light.turn_on", "target": {"entity_id": ["light.reading_lamp"]}}
            ],
        },
        {
            "id": "1700000000000",
            "alias": "Porch at dusk",
            "triggers": [],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": ["light.porch"]}}],
        },
    ],
}

# An ordinary day, then London's clocks going forward (01:00 GMT -> 02:00 BST, 29 March
# 2026: just before, and that afternoon) and back (02:00 BST -> 01:00 GMT, 25 October: in
# the hour that happens twice, the first time and the second).
MOMENTS = (
    datetime(2026, 10, 16, 11, 0, tzinfo=UTC),
    datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
    datetime(2026, 3, 29, 15, 0, tzinfo=UTC),
    datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
    datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
)


def live_facing_output() -> dict:
    """Everything the live app (switch off) shows, plans, applies and writes for the
    fixture house beside the old system, as JSON-ready data."""
    deploy.log.disabled = True  # the fixture's unavailable chandelier_3 warns on every deploy
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            ha = FakeHA(**OLD_SYSTEM)
            now = [MOMENTS[0]]
            app = create_app(ha=ha, config_path=path, verify_delay=0, clock=lambda: now[0])
            with TestClient(app, base_url="http://localhost") as client:
                out: dict = {}
                start = len(ha.calls)
                preview = client.get("/api/deploy/preview")
                out["preview"] = [preview.status_code, preview.json()]
                mark = len(ha.calls)
                applied = client.post(
                    "/api/deploy", json={"fingerprint": preview.json()["fingerprint"]}
                )
                out["apply"] = [applied.status_code, applied.json()]
                out["writes"] = [[m, list(a)] for m, a in ha.calls[mark:] if (m, a) in ha.writes]
                again = client.get("/api/deploy/preview")
                out["preview_after"] = [again.status_code, again.json()]
                out["ha"] = {"helpers": ha.helpers, "automations": ha.automations}
                # Every call made to HA, reads included, in order: the switch-off deploy
                # reads exactly what it read before, and nothing else.
                out["calls"] = [[method, list(args)] for method, args in ha.calls[start:]]
                out["views"] = {}
                for moment in MOMENTS:
                    now[0] = moment
                    reads = {}
                    for url in ("/api/rooms/living_room/now", "/api/schedule_profiles"):
                        response = client.get(url)
                        reads[url] = [response.status_code, response.json()]
                    out["views"][moment.isoformat()] = reads
                return json.loads(json.dumps(out))
    finally:
        deploy.log.disabled = False


class SwitchOffTest(unittest.TestCase):
    def test_the_live_facing_output_is_p5_2bs_byte_for_byte(self):
        golden = json.loads(GOLDEN.read_text())
        actual = live_facing_output()
        for key in golden:
            with self.subTest(key):
                self.assertEqual(actual[key], golden[key])
        self.assertEqual(sorted(actual), sorted(golden))

    def test_what_that_means(self):
        # The same output, read for what the cutover switch promises while it is off.
        self.assertIs(main.ADAPTIVE_ENABLED, False)
        self.assertEqual(generate.namespaces(), ["lm"])
        out = live_facing_output()
        preview, applied = out["preview"][1], out["apply"][1]
        # No module lane is previewed or applied, and the fingerprint is the lm_ plan's.
        self.assertNotIn("modules", preview)
        self.assertEqual(sorted(applied), ["deployed", "result"])
        self.assertEqual(applied["result"]["prefix"], "lm")
        # Nothing written is an al_ artefact; no input_select is even read.
        for method, args in out["writes"]:
            named = json.dumps(args)
            self.assertNotIn('"al_', named, method)
            self.assertNotIn("AL ", named, method)
        self.assertNotIn(["list_helpers", ["input_select"]], out["calls"])
        self.assertEqual(
            list(out["ha"]["helpers"]["input_select"].values()),
            OLD_SYSTEM["helpers"]["input_select"],
        )
        # Every al_ automation is the old system's, the one under an id Light Map's own
        # al_ lane would generate included.
        drivers = {d["id"]: d["adaptive_light"] for d in preview["crossover"]["drivers"]}
        self.assertEqual(
            drivers,
            {
                "al_maintenance_living_room": True,
                "al_scene_living_room_day": True,
                "1700000000000": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
