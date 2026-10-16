"""The HTTP API (app.main) against the fake HA and a config file in a temp directory: the
Map's reads, every edit (Area edits written back to HA and verified), the previewed
actions (sync, deploy, Area writes) and their fingerprints, the validate-then-write
refusals, and the coexistence warning."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.core import config, deploy
from app.core.ha import HAClient, HAError
from app.main import create_app
from tests.fakes import FakeHA, committed_config

BUTTON = {
    "name": "Porch button",
    "source": {"type": "state", "entity_id": "switch.reading_relay"},
    "mode": "toggle",
}


def setUpModule():
    # The fixture's chandelier_3 is unavailable, so every deploy warns about its soft.
    deploy.log.disabled = True


def tearDownModule():
    deploy.log.disabled = False


class ApiCase(unittest.TestCase):
    """A running app over a fake HA, with the committed fixture config saved on disk."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.json"
        self.ha = self.make_ha()
        if self.initial() is not None:
            config.save(self.path, self.initial())
        self.client = self.start(create_app(ha=self.ha, config_path=self.path, verify_delay=0))

    def make_ha(self):
        return FakeHA()

    def initial(self):
        return committed_config()

    def start(self, app):
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def saved(self):
        return json.loads(self.path.read_text())

    def assert_error(self, response, status, error):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"], error, response.text)
        return response.json()

    def assert_unchanged(self, before):
        self.assertEqual(self.path.read_text(), before)

    def version(self, item_id):
        """The `version` the Map shows for a room, bulb, or switching group: what an edit
        sends back as `base`."""
        view = self.client.get("/api/map").json()
        for room in view["rooms"]:
            if room["id"] == item_id:
                return room["version"]
            for item in [*room["bulbs"], *room["groups"]]:
                if item["id"] == item_id:
                    return item["version"]
        return next((b["version"] for b in view["unassigned"]["bulbs"] if b["id"] == item_id), None)

    def adaptive(self, room_id, adaptive, **extra):
        body = {"adaptive": adaptive, "base": self.version(room_id), **extra}
        return self.client.put(f"/api/rooms/{room_id}/adaptive", json=body)

    def bind(self, bulb_id, bindings, **extra):
        body = {"hard_switches": bindings, "base": self.version(bulb_id), **extra}
        return self.client.put(f"/api/bulbs/{bulb_id}/hard_switches", json=body)

    def edit_group(self, room_id, group_id, body):
        body = {"base": self.version(group_id), **body}
        return self.client.put(f"/api/rooms/{room_id}/groups/{group_id}", json=body)


class ReadTest(ApiCase):
    def test_map(self):
        view = self.client.get("/api/map").json()
        self.assertEqual([r["name"] for r in view["rooms"]], ["Kitchen", "Living Room"])
        living = view["rooms"][1]
        self.assertEqual(
            [(b["id"], b["soft"], b["live"]) for b in living["bulbs"]],
            [
                ("chandelier_1", "input_boolean.lm_soft_chandelier_1", True),
                ("chandelier_2", "input_boolean.lm_soft_chandelier_2", True),
                ("chandelier_3", "input_boolean.lm_soft_chandelier_3", True),
                ("reading_lamp", "input_boolean.lm_soft_reading_lamp", True),
            ],
        )
        reading = living["bulbs"][3]
        self.assertEqual(
            [v["id"] for v in reading["live_via"]], ["living_all_off", "reading_switch"]
        )
        self.assertEqual(reading["groups"], ["living_all", "reading_corner"])
        self.assertEqual(
            [(g["id"], g["live"]) for g in living["groups"]],
            [("living_all", True), ("reading_corner", False)],
        )
        self.assertEqual(len(living["adaptive_drops"]), 3)
        kitchen = view["rooms"][0]
        self.assertEqual((kitchen["adaptive"], kitchen["bulbs"][0]["live"]), (False, False))
        self.assertEqual([b["id"] for b in view["unassigned"]["bulbs"]], ["porch"])
        self.assertEqual(view["live"]["hard_switches"], 2)
        self.assertEqual(self.ha.calls, [])  # the Map reads the committed config only

    def test_status_and_export(self):
        status = self.client.get("/api/status").json()
        self.assertEqual(status["config"], {"path": str(self.path), "exists": True, "problems": []})
        self.assertEqual(status["ha"], {"configured": True, "error": None})
        doc = self.saved()
        doc["homeassistant"]["access_token"] = "secret-token"
        self.path.write_text(json.dumps(doc))
        with self.assertLogs("app.core.config", "WARNING"):
            exported = self.client.get("/api/config")
        self.assertNotIn("secret-token", exported.text)
        self.assertEqual(exported.json()["rooms"], committed_config()["rooms"])

    def test_entities_for_the_picker(self):
        entities = self.client.get("/api/entities").json()["entities"]
        by_id = {e["entity_id"]: e for e in entities}
        self.assertIn("switch.reading_relay", by_id)
        self.assertIn("event.living_room_button", by_id)
        self.assertTrue(by_id["light.porch"]["mapped"])
        self.assertFalse(by_id["switch.reading_relay"]["mapped"])
        self.assertNotIn("sensor.living_room_lux", by_id)
        self.assertEqual([e["entity_id"] for e in entities], sorted(by_id))

    def test_light_maps_own_entities_are_not_offered(self):
        self.ha.helper_states["input_boolean.lm_soft_porch"] = "off"
        self.ha.helper_states["input_boolean.guest_mode"] = "off"
        ids = [e["entity_id"] for e in self.client.get("/api/entities").json()["entities"]]
        self.assertIn("input_boolean.guest_mode", ids)
        self.assertNotIn("input_boolean.lm_soft_porch", ids)

    def test_an_ha_error_is_a_bad_gateway(self):
        async def broken():
            raise HAError("GET /api/states failed: connection refused")

        self.ha.get_states = broken
        body = self.assert_error(self.client.get("/api/entities"), 502, "ha_error")
        self.assertIn("connection refused", body["detail"])


class NoConfigYetTest(ApiCase):
    def initial(self):
        return None

    def test_a_fresh_install_starts_blank_and_the_first_sync_saves(self):
        view = self.client.get("/api/map").json()
        self.assertEqual((view["rooms"], view["config"]["exists"]), ([], False))
        preview = self.client.get("/api/sync/preview").json()
        self.assertEqual(preview["state"], "unsaved")
        self.assertIn("room added: 'garage' (Garage, Area garage)", preview["changes"])
        self.assertFalse(self.path.exists())
        response = self.client.post("/api/sync", json={"fingerprint": preview["fingerprint"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [r["id"] for r in self.saved()["rooms"]], ["garage", "kitchen", "living_room"]
        )
        self.assertEqual(self.client.get("/api/sync/preview").json()["state"], "synced")

    def test_no_room_is_created_before_the_first_sync(self):
        # It would save a config holding only that room, skipping the reviewed first sync.
        self.assert_error(self.client.post("/api/rooms", json={"name": "Attic"}), 409, "sync_first")
        self.assertEqual(self.ha.writes, [])
        self.assertFalse(self.path.exists())


class BrokenConfigTest(ApiCase):
    def initial(self):
        return None

    def test_a_config_that_does_not_load_is_never_overwritten(self):
        self.path.write_text('{"version": 2, "rooms": "oops"}')
        before = self.path.read_text()
        body = self.assert_error(self.client.get("/api/map"), 503, "config_broken")
        self.assertTrue(body["problems"])
        self.assertTrue(self.client.get("/api/status").json()["config"]["problems"])
        self.assert_error(
            self.client.put("/api/rooms/kitchen/adaptive", json={"adaptive": True, "base": "x"}),
            503,
            "config_broken",
        )
        self.assert_error(
            self.client.post("/api/rooms", json={"name": "Attic"}), 503, "config_broken"
        )
        self.assertEqual(self.ha.writes, [])
        self.assert_unchanged(before)


class LifespanTest(unittest.TestCase):
    def test_without_a_token_the_map_still_serves_and_ha_actions_say_why(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            env = {
                "LM_HA_BASE_URL": "http://ha.local:8123",
                "LM_HA_TOKEN": "",
                "LM_HA_TOKEN_FILE": "",
            }
            with (
                mock.patch.dict(os.environ, env),
                self.assertLogs("app.main", "WARNING") as logs,
                TestClient(create_app(config_path=path)) as client,
            ):
                self.assertIn("not configured", logs.output[0])
                self.assertEqual(client.get("/api/map").status_code, 200)
                body = client.get("/api/sync/preview").json()
                self.assertEqual(body["error"], "ha_not_configured")
                self.assertIn("LM_HA_TOKEN", body["detail"])
                self.assertFalse(client.get("/api/status").json()["ha"]["configured"])

    def test_the_client_is_made_from_the_environment_and_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"LM_HA_BASE_URL": "http://ha.local:8123", "LM_HA_TOKEN": "abc"}
            app = create_app(config_path=Path(directory) / "config.json")
            with mock.patch.dict(os.environ, env), TestClient(app):
                client = app.state.ha
                self.assertIsInstance(client, HAClient)
                self.assertEqual(client.base_url, "http://ha.local:8123")
                self.assertFalse(client.connected)  # nothing opened until a request needs it
            self.assertIsNone(app.state.ha)

    def test_the_config_path_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"LM_CONFIG_PATH": "/tmp/lm-test/config.json"}):
            app = create_app(ha=FakeHA())
        self.assertEqual(app.state.store.path, Path("/tmp/lm-test/config.json"))


class SyncTest(ApiCase):
    def test_preview_writes_nothing_and_apply_saves_what_was_shown(self):
        before = self.path.read_text()
        preview = self.client.get("/api/sync/preview").json()
        self.assertEqual(preview["state"], "stale")
        self.assertIn("room added: 'garage' (Garage, Area garage)", preview["changes"])
        self.assertIn("bulb added: light.counter_lamp -> room 'kitchen'", preview["changes"])
        self.assertEqual(preview["problems"], [])
        self.assert_unchanged(before)
        response = self.client.post("/api/sync", json={"fingerprint": preview["fingerprint"]})
        self.assertEqual(response.json()["changes"], preview["changes"])
        self.assertIn("garage", [r["id"] for r in self.saved()["rooms"]])
        self.assertEqual(self.client.get("/api/sync/preview").json()["state"], "synced")
        self.assertEqual(self.ha.writes, [])  # a sync reads HA, never writes it

    def test_an_apply_is_refused_when_ha_changed_since_the_preview(self):
        before = self.path.read_text()
        preview = self.client.get("/api/sync/preview").json()
        self.ha.areas[0]["name"] = "Lounge"
        body = self.assert_error(
            self.client.post("/api/sync", json={"fingerprint": preview["fingerprint"]}),
            409,
            "preview_stale",
        )
        self.assertIn("changed since the preview", body["detail"])
        self.assert_unchanged(before)

    def test_an_apply_needs_the_fingerprint(self):
        body = self.assert_error(self.client.post("/api/sync", json={}), 422, "bad_request")
        self.assertEqual(body["problems"], ["body/fingerprint: Field required"])
        self.assert_error(
            self.client.post("/api/sync", json={"fingerprint": "made-up"}), 409, "preview_stale"
        )


class RoomTest(ApiCase):
    def test_create_room_writes_the_area_and_commits_only_that(self):
        response = self.client.post("/api/rooms", json={"name": "Attic"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            response.json()["room"], {"id": "attic", "name": "Attic", "ha_area_id": "attic"}
        )
        self.assertIn("attic", [a["area_id"] for a in self.ha.areas])
        # Only the new room: HA's other differences (Garage, counter_lamp, ...) still wait
        # for a reviewed sync.
        self.assertEqual(
            config.snapshot_changes(committed_config(), self.saved()),
            ["room added: 'attic' (Attic, Area attic)"],
        )

    def test_create_room_refused_before_writing(self):
        before = self.path.read_text()
        body = self.assert_error(
            self.client.post("/api/rooms", json={"name": " kitchen "}), 409, "refused"
        )
        self.assertIn("already has an Area named 'Kitchen'", body["detail"])
        self.assertEqual(self.ha.writes, [])
        self.assert_unchanged(before)

    def test_an_area_write_that_does_not_land(self):
        self.ha.drop_writes = True
        before = self.path.read_text()
        body = self.assert_error(
            self.client.post("/api/rooms", json={"name": "Attic"}), 409, "refused"
        )
        self.assertIn("did not land", body["detail"])
        self.assert_unchanged(before)

    def test_rename_room_renames_the_area(self):
        response = self.client.put("/api/rooms/kitchen/name", json={"name": "Galley"})
        self.assertEqual(response.json()["room"], {"id": "kitchen", "name": "Galley"})
        self.assertEqual(
            next(a for a in self.ha.areas if a["area_id"] == "kitchen")["name"], "Galley"
        )
        self.assertEqual(
            config.snapshot_changes(committed_config(), self.saved()),
            ["room renamed: 'kitchen': 'Kitchen' -> 'Galley'"],
        )
        self.assert_error(
            self.client.put("/api/rooms/attic/name", json={"name": "X"}), 404, "not_found"
        )

    def test_adaptive_on_writes_a_minimal_config(self):
        response = self.adaptive("kitchen", True)
        self.assertEqual(response.status_code, 200, response.text)
        kitchen = self.saved()["rooms"][1]
        self.assertTrue(kitchen["adaptive"])
        self.assertEqual(
            kitchen["adaptive_config"],
            {
                "lux_sensors": [],
                "presence_sensors": [],
                "schedule_profile": "default",
                "learning_units": [],
            },
        )
        self.assertEqual(self.ha.calls, [])

    def test_adaptive_off_confirms_what_it_drops(self):
        before = self.path.read_text()
        body = self.assert_error(
            self.adaptive("living_room", False),
            409,
            "confirm_required",
        )
        self.assertIn("2 learning unit(s)", body["drops"])
        self.assert_unchanged(before)
        response = self.adaptive("living_room", False, discard_adaptive_config=True)
        self.assertEqual(response.status_code, 200, response.text)
        living = self.saved()["rooms"][0]
        self.assertEqual((living["adaptive"], "adaptive_config" in living), (False, False))

    def test_adaptive_off_with_nothing_to_drop_needs_no_confirmation(self):
        self.adaptive("kitchen", True)
        response = self.adaptive("kitchen", False)
        self.assertEqual(response.status_code, 200, response.text)


class MoveTest(ApiCase):
    def preview(self, bulb, room_id):
        return self.client.post(f"/api/bulbs/{bulb}/move/preview", json={"room_id": room_id})

    def entity(self, entity_id):
        return next(e for e in self.ha.entities if e["entity_id"] == entity_id)

    def test_preview_then_move(self):
        preview = self.preview("porch", "kitchen").json()
        self.assertEqual(
            preview["changes"], ["bulb moved: light.porch: unassigned -> room 'kitchen'"]
        )
        self.assertEqual((preview["refused"], preview["area_id"]), (None, "kitchen"))
        self.assertEqual(self.ha.writes, [])
        response = self.client.post(
            "/api/bulbs/porch/move",
            json={"room_id": "kitchen", "fingerprint": preview["fingerprint"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ha.writes, [("set_entity_area", ("light.porch", "kitchen"))])
        self.assertEqual(self.entity("light.porch")["area_id"], "kitchen")
        self.assertEqual(
            [b["id"] for b in self.saved()["rooms"][1]["bulbs"]], ["kitchen_strip", "porch"]
        )
        self.assertEqual(self.saved()["unassigned"], [])

    def test_the_preview_shows_the_memberships_a_move_drops(self):
        preview = self.preview("reading_lamp", "kitchen").json()
        self.assertIn("switching group removed: 'living_room/reading_corner'", preview["changes"])
        self.assertIn(
            "switching group 'living_room/living_all': members dropped: reading_lamp",
            preview["changes"],
        )

    def test_a_move_ha_would_refuse_is_refused_before_writing(self):
        before = self.path.read_text()
        preview = self.preview("chandelier_1", None).json()
        self.assertIn("inherits Area 'living_room' from its device", preview["refused"])
        body = self.assert_error(
            self.client.post(
                "/api/bulbs/chandelier_1/move",
                json={"room_id": None, "fingerprint": preview["fingerprint"]},
            ),
            409,
            "refused",
        )
        self.assertIn("clear the device's Area in HA", body["detail"])
        self.assertEqual(self.ha.writes, [])
        self.assert_unchanged(before)

    def test_a_stale_preview_is_refused(self):
        preview = self.preview("porch", "kitchen").json()
        self.adaptive("kitchen", True)
        self.assert_error(
            self.client.post(
                "/api/bulbs/porch/move",
                json={"room_id": "kitchen", "fingerprint": preview["fingerprint"]},
            ),
            409,
            "preview_stale",
        )
        self.assertEqual(self.ha.writes, [])

    def test_a_light_that_took_the_bulbs_entity_id_is_never_moved(self):
        # In HA, the reading lamp's entity was renamed, and an unrelated light now holds its
        # old entity_id. The snapshot (not synced since) still names light.reading_lamp.
        self.entity("light.reading_lamp")["entity_id"] = "light.reading_lamp_old"
        self.ha.entities.append(
            {
                "entity_id": "light.reading_lamp",
                "id": "reg_other",
                "device_id": None,
                "area_id": "garage",
            }
        )
        before = self.path.read_text()
        preview = self.preview("reading_lamp", "kitchen").json()
        self.assertIn("is not this bulb's light any more", preview["refused"])
        self.assert_error(
            self.client.post(
                "/api/bulbs/reading_lamp/move",
                json={"room_id": "kitchen", "fingerprint": preview["fingerprint"]},
            ),
            409,
            "refused",
        )
        self.assertEqual(self.ha.writes, [])
        self.assertEqual(self.entity("light.reading_lamp")["area_id"], "garage")
        self.assert_unchanged(before)

    def test_nonsense_moves(self):
        self.assert_error(self.preview("porch", None), 409, "no_change")
        self.assert_error(self.preview("ghost", None), 404, "not_found")
        self.assert_error(self.preview("porch", "attic"), 404, "not_found")


class HardSwitchTest(ApiCase):
    def put(self, bulb, bindings, **extra):
        return self.bind(bulb, bindings, **extra)

    def test_binding_a_hard_switch_needs_the_coexistence_acknowledgement(self):
        before = self.path.read_text()
        body = self.assert_error(self.put("porch", [BUTTON]), 409, "coexistence")
        self.assertIn("ha-adaptive-light", body["detail"])
        self.assertEqual(
            body["going_live"],
            [
                {
                    "id": "porch_button",
                    "name": "Porch button",
                    "owner": {"kind": "bulb", "id": "porch", "name": "Porch"},
                    "bulbs": ["porch"],
                }
            ],
        )
        self.assert_unchanged(before)
        response = self.put("porch", [BUTTON], acknowledge_live=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["going_live"][0]["id"], "porch_button")
        self.assertEqual(
            self.saved()["unassigned"][0]["hard_switches"], [{**BUTTON, "id": "porch_button"}]
        )
        porch = self.client.get("/api/map").json()["unassigned"]["bulbs"][0]
        self.assertTrue(porch["live"])
        self.assertEqual(self.ha.calls, [])  # a config edit; HA sees it only on deploy

    def test_removing_or_renaming_needs_no_acknowledgement(self):
        living = committed_config()["rooms"][0]
        binding = living["bulbs"][3]["hard_switches"][0]
        self.assertEqual(
            self.put("reading_lamp", [{**binding, "name": "Renamed"}]).status_code, 200
        )
        self.assertEqual(self.put("reading_lamp", []).status_code, 200)
        self.assertEqual(self.saved()["rooms"][0]["bulbs"][3]["hard_switches"], [])

    def test_a_binding_that_would_loop_is_refused_even_acknowledged(self):
        before = self.path.read_text()
        loop = {**BUTTON, "source": {"type": "state", "entity_id": "light.porch"}}
        body = self.assert_error(
            self.put("kitchen_strip", [loop], acknowledge_live=True), 422, "invalid_config"
        )
        self.assertTrue(any("a toggle can't take light.porch" in p for p in body["problems"]))
        self.assert_unchanged(before)

    def test_a_malformed_binding_is_refused(self):
        before = self.path.read_text()
        self.assert_error(
            self.put("porch", [{**BUTTON, "mode": "flip"}], acknowledge_live=True),
            422,
            "invalid_config",
        )
        follow_event = {
            "name": "x",
            "source": {"type": "event", "event_type": "zha_event"},
            "mode": "follow",
        }
        body = self.assert_error(
            self.put("porch", [follow_event], acknowledge_live=True), 422, "invalid_config"
        )
        self.assertTrue(any("follow needs a state source" in p for p in body["problems"]))
        self.assert_unchanged(before)

    def test_event_and_device_sources(self):
        bindings = [
            {
                "name": "Remote on",
                "source": {
                    "type": "event",
                    "event_type": "zha_event",
                    "event_data": {"command": "on"},
                },
                "mode": "press",
                "turn": "on",
            },
            {
                "name": "Remote off",
                "source": {
                    "type": "device",
                    "device_id": "abc123",
                    "trigger": {
                        "domain": "zha",
                        "type": "remote_button_short_press",
                        "subtype": "turn_off",
                    },
                },
                "mode": "press",
                "turn": "off",
            },
        ]
        response = self.put("porch", bindings, acknowledge_live=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [b["id"] for b in self.saved()["unassigned"][0]["hard_switches"]],
            ["porch_remote_on", "porch_remote_off"],
        )

    def test_a_binding_id_that_is_not_a_string_is_refused_not_a_crash(self):
        bad = {**BUTTON, "id": ["a"]}
        self.assert_error(self.put("porch", [bad], acknowledge_live=True), 422, "invalid_config")
        group = {"name": "G", "members": ["kitchen_strip"], "hard_switches": [bad]}
        self.assert_error(
            self.client.post("/api/rooms/kitchen/groups", json={**group, "acknowledge_live": True}),
            422,
            "invalid_config",
        )

    def test_unknown_bulb(self):
        self.assert_error(self.put("ghost", [], base="x"), 404, "not_found")
        # A group's hard switches are edited with the group.
        self.assert_error(self.put("living_all", []), 404, "not_found")


class GroupTest(ApiCase):
    def test_create_a_group(self):
        response = self.client.post(
            "/api/rooms/kitchen/groups", json={"name": "Kitchen all", "members": ["kitchen_strip"]}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["group"], {"id": "kitchen_all"})
        self.assertEqual(
            self.saved()["rooms"][1]["switching_groups"],
            [
                {
                    "id": "kitchen_all",
                    "name": "Kitchen all",
                    "members": ["kitchen_strip"],
                    "hard_switches": [],
                }
            ],
        )

    def test_a_group_with_a_hard_switch_needs_the_acknowledgement(self):
        group = {"name": "Kitchen all", "members": ["kitchen_strip"], "hard_switches": [BUTTON]}
        before = self.path.read_text()
        body = self.assert_error(
            self.client.post("/api/rooms/kitchen/groups", json=group), 409, "coexistence"
        )
        self.assertEqual(body["going_live"][0]["owner"]["kind"], "group")
        self.assert_unchanged(before)
        response = self.client.post(
            "/api/rooms/kitchen/groups", json={**group, "acknowledge_live": True}
        )
        self.assertEqual(response.status_code, 201, response.text)
        binding = self.saved()["rooms"][1]["switching_groups"][0]["hard_switches"][0]
        self.assertEqual(binding["id"], "kitchen_all_porch_button")

    def test_adding_a_member_to_a_bound_group_needs_the_acknowledgement(self):
        living_all = committed_config()["rooms"][0]["switching_groups"][0]
        smaller = {**living_all, "members": ["chandelier_1"]}
        self.assertEqual(
            self.edit_group("living_room", "living_all", smaller).status_code,
            200,
        )
        self.assert_error(
            self.edit_group("living_room", "living_all", living_all),
            409,
            "coexistence",
        )

    def test_edit_validation(self):
        before = self.path.read_text()
        body = self.assert_error(
            self.edit_group(
                "living_room",
                "reading_corner",
                {"name": "Reading corner", "members": ["kitchen_strip"]},
            ),
            422,
            "invalid_config",
        )
        self.assertIn("it is in room 'kitchen'", body["problems"][0])
        self.assert_error(
            self.client.post(
                "/api/rooms/kitchen/groups", json={"name": " ", "members": ["kitchen_strip"]}
            ),
            422,
            "invalid_config",
        )
        self.assert_error(
            self.edit_group("kitchen", "living_all", {"name": "x", "members": []}),
            404,
            "not_found",
        )
        self.assert_unchanged(before)

    def test_delete_a_group(self):
        base = self.version("living_all")
        response = self.client.delete(f"/api/rooms/living_room/groups/living_all?base={base}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [g["id"] for g in self.saved()["rooms"][0]["switching_groups"]], ["reading_corner"]
        )
        self.assert_error(
            self.client.delete(f"/api/rooms/living_room/groups/living_all?base={base}"),
            404,
            "not_found",
        )


class StaleEditTest(ApiCase):
    """An edit carries the version the Map showed; one made against an older version is
    refused rather than silently reverting what changed meanwhile."""

    def test_a_save_from_a_stale_view_never_reverts_another_edit(self):
        seen = self.version("porch")
        self.assertEqual(self.bind("porch", [BUTTON], acknowledge_live=True).status_code, 200)
        before = self.path.read_text()
        body = self.assert_error(
            self.client.put(
                "/api/bulbs/porch/hard_switches", json={"hard_switches": [], "base": seen}
            ),
            409,
            "edit_stale",
        )
        self.assertIn("reload the Map", body["detail"])
        self.assert_unchanged(before)

    def test_an_acknowledgement_covers_only_what_was_shown(self):
        seen = self.version("reading_lamp")
        self.assertEqual(self.bind("reading_lamp", []).status_code, 200)  # another tab
        stale_view = committed_config()["rooms"][0]["bulbs"][3]["hard_switches"]
        new_one = {**BUTTON, "name": "New one"}
        self.assert_error(
            self.client.put(
                "/api/bulbs/reading_lamp/hard_switches",
                json={
                    "hard_switches": [*stale_view, new_one],
                    "acknowledge_live": True,
                    "base": seen,
                },
            ),
            409,
            "edit_stale",
        )
        self.assertEqual(self.saved()["rooms"][0]["bulbs"][3]["hard_switches"], [])

    def test_a_confirmed_discard_covers_only_what_was_listed(self):
        seen = self.version("living_room")
        doc = self.saved()
        doc["rooms"][0]["adaptive_config"]["presence_sensors"] = ["binary_sensor.sofa"]
        config.save(self.path, doc)  # changed by hand since the Map listed the drops
        self.assert_error(
            self.client.put(
                "/api/rooms/living_room/adaptive",
                json={"adaptive": False, "discard_adaptive_config": True, "base": seen},
            ),
            409,
            "edit_stale",
        )
        self.assertIn("adaptive_config", self.saved()["rooms"][0])

    def test_groups_too(self):
        seen = self.version("reading_corner")
        group = committed_config()["rooms"][0]["switching_groups"][1]
        self.assertEqual(
            self.edit_group("living_room", "reading_corner", {**group, "name": "Nook"}).status_code,
            200,
        )
        self.assert_error(
            self.client.put(
                "/api/rooms/living_room/groups/reading_corner", json={**group, "base": seen}
            ),
            409,
            "edit_stale",
        )
        self.assert_error(
            self.client.delete(f"/api/rooms/living_room/groups/reading_corner?base={seen}"),
            409,
            "edit_stale",
        )
        self.assert_error(
            self.client.put("/api/rooms/living_room/groups/reading_corner", json=group),
            409,
            "edit_stale",
        )

    def test_an_edit_must_say_what_it_was_made_against(self):
        body = self.assert_error(
            self.client.put("/api/bulbs/porch/hard_switches", json={"hard_switches": []}),
            422,
            "bad_request",
        )
        self.assertEqual(body["problems"], ["body/base: Field required"])


class DeployTest(ApiCase):
    OLD_SCENE = {
        "id": "al_scene_lounge_day",
        "alias": "al_scene_lounge_day",
        "triggers": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.chandelier_1"}}],
    }

    def make_ha(self):
        return FakeHA(automations=[self.OLD_SCENE])

    def test_preview_writes_nothing_and_shows_everything(self):
        preview = self.client.get("/api/deploy/preview").json()
        self.assertTrue(preview["ready"])
        self.assertEqual(len(preview["plan"]["created"]["input_boolean"]), 6)
        self.assertIn("lm_bridge_porch", preview["plan"]["created"]["automation"])
        self.assertEqual(
            [(g["id"], g["op"], g["bulbs"]) for g in preview["going_live"]],
            [
                (
                    "living_all_off",
                    "create",
                    ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"],
                ),
                ("reading_switch", "create", ["reading_lamp"]),
            ],
        )
        drivers = preview["crossover"]["drivers"]
        self.assertEqual(
            [(d["entity_id"], d["adaptive_light"]) for d in drivers],
            [("automation.al_scene_lounge_day", True)],
        )
        self.assertEqual(preview["crossover"]["groups"][0]["entity_id"], "light.living_room_group")
        self.assertIn("ha-adaptive-light", preview["coexistence"]["note"])
        self.assertEqual(len(preview["coexistence"]["live_bulbs"]), 4)
        self.assertEqual(self.ha.writes, [])

    def test_apply_deploys_exactly_what_was_previewed(self):
        preview = self.client.get("/api/deploy/preview").json()
        response = self.client.post("/api/deploy", json={"fingerprint": preview["fingerprint"]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["created"], preview["plan"]["created"])
        self.assertIn("input_boolean.lm_soft_chandelier_1", result["synced"])
        self.assertIn("lm_soft_porch", self.ha.helpers["input_boolean"])
        self.assertEqual(self.ha.fired, [])  # deploying never drives a bulb
        self.assertNotIn("set_entity_area", [call[0] for call in self.ha.writes])
        # The old system's automation is reported, never touched.
        self.assertEqual(self.ha.automations["al_scene_lounge_day"], self.OLD_SCENE)
        again = self.client.get("/api/deploy/preview").json()
        self.assertEqual(again["plan"]["created"], {"input_boolean": [], "automation": []})
        self.assertEqual(again["going_live"], [])

    def test_apply_is_refused_without_or_with_a_stale_preview(self):
        self.assert_error(self.client.post("/api/deploy", json={}), 422, "bad_request")
        preview = self.client.get("/api/deploy/preview").json()
        self.bind("porch", [BUTTON], acknowledge_live=True)
        self.assert_error(
            self.client.post("/api/deploy", json={"fingerprint": preview["fingerprint"]}),
            409,
            "preview_stale",
        )
        self.assertEqual(self.ha.writes, [])

    def test_a_deploy_that_would_be_refused(self):
        self.ha.states = [s for s in self.ha.states if s["entity_id"] != "switch.reading_relay"]
        preview = self.client.get("/api/deploy/preview").json()
        self.assertFalse(preview["ready"])
        self.assertEqual(
            preview["refused"],
            ["switch.reading_relay (hard switch 'reading_switch') is not in Home Assistant"],
        )
        self.assertNotIn("fingerprint", preview)
        self.assertIn("crossover", preview)
        body = self.assert_error(
            self.client.post("/api/deploy", json={"fingerprint": "x"}), 409, "deploy_refused"
        )
        self.assertEqual(body["problems"], preview["refused"])
        self.assertEqual(self.ha.writes, [])

    def test_a_deploy_that_does_not_verify(self):
        preview = self.client.get("/api/deploy/preview").json()
        self.ha.disabled.add("lm_bridge_porch")
        body = self.assert_error(
            self.client.post("/api/deploy", json={"fingerprint": preview["fingerprint"]}),
            502,
            "deploy_failed",
        )
        self.assertIn("reconciles", body["detail"])
        self.assertEqual(
            body["problems"],
            ["automation lm_bridge_porch has not loaded (is its entity disabled in HA?)"],
        )


class AreasTest(ApiCase):
    def entity(self, entity_id):
        return next(e for e in self.ha.entities if e["entity_id"] == entity_id)

    def test_nothing_to_write(self):
        preview = self.client.get("/api/areas/preview").json()
        self.assertEqual((preview["moves"], preview["problems"], preview["ready"]), ([], [], False))

    def test_preview_then_write_the_snapshot_back(self):
        self.entity("light.reading_lamp")["area_id"] = "kitchen"
        preview = self.client.get("/api/areas/preview").json()
        self.assertEqual(
            preview["moves"],
            [
                {
                    "bulb": "reading_lamp",
                    "entity_id": "light.reading_lamp",
                    "from": "kitchen",
                    "to": "living_room",
                    "from_name": "Kitchen",
                    "to_name": "Living Room",
                }
            ],
        )
        self.assertTrue(preview["ready"])
        self.assertIn("reverts", preview["note"])
        self.assertEqual(self.ha.writes, [])
        response = self.client.post(
            "/api/areas/deploy", json={"fingerprint": preview["fingerprint"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.entity("light.reading_lamp")["area_id"], "living_room")
        # Area writes create no helpers or automations.
        self.assertEqual([call[0] for call in self.ha.writes], ["set_entity_area"])

    def test_a_write_that_stops_partway_says_what_landed(self):
        self.entity("light.reading_lamp")["area_id"] = "kitchen"
        self.entity("light.porch")["area_id"] = "garage"
        preview = self.client.get("/api/areas/preview").json()
        self.assertEqual([m["bulb"] for m in preview["moves"]], ["porch", "reading_lamp"])
        write = self.ha.set_entity_area

        async def second_does_not_land(entity_id, area_id):
            if entity_id == "light.reading_lamp":
                self.ha.calls.append(("set_entity_area", (entity_id, area_id)))
                return {}
            return await write(entity_id, area_id)

        self.ha.set_entity_area = second_does_not_land
        body = self.assert_error(
            self.client.post("/api/areas/deploy", json={"fingerprint": preview["fingerprint"]}),
            502,
            "areas_partial",
        )
        self.assertIn("1 of 2 moves landed", body["detail"])
        self.assertEqual([m["bulb"] for m in body["written"]], ["porch"])
        self.assertIn("did not land", body["problems"][0])
        self.assertIsNone(self.entity("light.porch")["area_id"])  # that one really moved

    def test_a_light_that_took_a_bulbs_entity_id_refuses_the_lot(self):
        self.entity("light.reading_lamp")["entity_id"] = "light.reading_lamp_old"
        self.ha.entities.append(
            {
                "entity_id": "light.reading_lamp",
                "id": "reg_other",
                "device_id": None,
                "area_id": "garage",
            }
        )
        preview = self.client.get("/api/areas/preview").json()
        self.assertFalse(preview["ready"])
        self.assertIn("is not this bulb's light any more", " ".join(preview["problems"]))
        self.assert_error(
            self.client.post("/api/areas/deploy", json={"fingerprint": preview["fingerprint"]}),
            409,
            "deploy_refused",
        )
        self.assertEqual(self.ha.writes, [])

    def test_a_stale_or_refused_area_write(self):
        self.entity("light.reading_lamp")["area_id"] = "kitchen"
        preview = self.client.get("/api/areas/preview").json()
        self.entity("light.porch")["area_id"] = "garage"
        self.assert_error(
            self.client.post("/api/areas/deploy", json={"fingerprint": preview["fingerprint"]}),
            409,
            "preview_stale",
        )
        next(d for d in self.ha.devices if d["id"] == "dev_porch")["area_id"] = "garage"
        self.entity("light.porch")["area_id"] = None
        preview = self.client.get("/api/areas/preview").json()
        self.assertFalse(preview["ready"])
        self.assertIn("inherits Area 'garage' from its device", preview["problems"][0])
        self.assert_error(
            self.client.post("/api/areas/deploy", json={"fingerprint": preview["fingerprint"]}),
            409,
            "deploy_refused",
        )
        self.assertEqual(self.ha.writes, [])


if __name__ == "__main__":
    unittest.main()
