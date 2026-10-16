"""The observation core as the app wires it (P4): the store opened beside the config in the
lifespan, the observer started over HA and re-reading the config on every save, the event
log read through `GET /api/log`, the actions taken from the Map recorded in it, and
`/api/status`. Against the fake HA, driven inside the app's own event loop."""

import asyncio
import functools
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core import config, deploy
from app.main import create_app
from tests.fakes import FakeHA, committed_config


def setUpModule():
    deploy.log.disabled = True  # the fixture's unavailable bulb warns on every deploy


def tearDownModule():
    deploy.log.disabled = False


class AppCase(unittest.TestCase):
    observe = True
    saved = True

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "config.json"
        if self.saved:
            config.save(self.path, committed_config())
        self.ha = FakeHA()
        self.app = create_app(
            ha=self.ha, config_path=self.path, verify_delay=0, observe=self.observe
        )
        self.client = TestClient(self.app, base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def call(self, function, *args, **kwargs):
        """Run a coroutine function inside the app's event loop."""
        return self.client.portal.call(functools.partial(function, *args, **kwargs))

    def log(self, **params):
        response = self.client.get("/api/log", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def messages(self, **params):
        return [e["message"] for e in self.log(**params)["entries"]]


class WiringTest(AppCase):
    def test_the_lifespan_starts_the_observer_over_ha(self):
        status = self.client.get("/api/status").json()
        self.assertEqual(status["store"]["path"], str(self.dir))
        self.assertTrue(status["store"]["open"])
        self.assertTrue((self.dir / "archive").is_dir())
        self.assertTrue((self.dir / "light_map.db").exists())
        observer = status["observer"]
        self.assertTrue(observer["running"])
        self.assertTrue(observer["connected"])
        self.assertEqual((observer["rooms"], observer["bulbs"]), (2, 5))
        self.assertIn("state_changed", self.ha.subscribers)

    def test_a_manual_change_reaches_the_log(self):
        observer = self.app.state.observer
        change = self.ha.change("light.chandelier_1", "on", brightness=220)
        self.call(self.ha.emit, change)
        self.call(observer.flush, everything=True)
        self.call(observer.tick)
        log = self.log()
        self.assertEqual(
            log["entries"][0]["message"], "Manual change in Living Room: chandelier_1."
        )
        self.assertEqual(log["entries"][0]["room_name"], "Living Room")
        self.assertEqual(log["entries"][0]["detail"], {"chandelier_1": [128, 220]})
        status = self.client.get("/api/status").json()
        self.assertEqual(status["store"]["reactive_events"], 1)
        self.assertEqual(status["store"]["heartbeats"], 2)
        # The tick summary is debug: hidden unless asked for.
        self.assertNotIn("Heartbeat:", " ".join(self.messages()))
        self.assertIn("Heartbeat: 2 room(s), 5 bulb(s), 2 on.", self.messages(level="debug"))

    def test_shutting_down_stops_observing(self):
        observer = self.app.state.observer
        self.call(self.ha.emit, self.ha.change("light.chandelier_1", "on", brightness=220))
        self.client.__exit__(None, None, None)
        self.assertIsNone(self.app.state.observer)
        self.assertEqual(self.ha.subscribers["state_changed"], [])
        self.assertEqual(observer.status()["open_windows"], 0)  # flushed, not dropped
        self.assertEqual(self.ha.writes, [])  # observing never wrote to HA


class FirstSyncTest(AppCase):
    saved = False  # a fresh install: no config yet, so no rooms to observe

    def test_the_first_sync_starts_observing(self):
        self.assertNotIn("state_changed", self.ha.subscribers)
        self.assertEqual(self.client.get("/api/status").json()["observer"]["rooms"], 0)
        preview = self.client.get("/api/sync/preview").json()
        response = self.client.post("/api/sync", json={"fingerprint": preview["fingerprint"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.call(asyncio.sleep, 0.01)  # the subscription is a task of its own
        self.assertIn("state_changed", self.ha.subscribers)
        self.assertGreater(self.client.get("/api/status").json()["observer"]["rooms"], 0)
        self.assertTrue(self.messages(category="sync")[0].startswith("Saved the snapshot"))


class ActionsTest(AppCase):
    observe = False

    def test_map_actions_are_recorded(self):
        doc = self.client.get("/api/map").json()
        kitchen = next(r for r in doc["rooms"] if r["id"] == "kitchen")
        response = self.client.put(
            "/api/rooms/kitchen/adaptive", json={"adaptive": True, "base": kitchen["version"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        preview = self.client.get("/api/deploy/preview").json()
        response = self.client.post("/api/deploy", json={"fingerprint": preview["fingerprint"]})
        self.assertEqual(response.status_code, 200, response.text)
        log = self.log()
        self.assertEqual(
            [(e["category"], e["room_id"]) for e in log["entries"]],
            [("deploy", None), ("map", "kitchen")],
        )
        self.assertEqual(log["entries"][1]["message"], "Flagged Kitchen as adaptive.")
        self.assertRegex(
            log["entries"][0]["message"], r"^Deployed the switching plane: \d+ created"
        )
        self.assertEqual(log["categories"], ["deploy", "map"])
        self.assertIn({"id": "kitchen", "name": "Kitchen"}, log["rooms"])

    def test_a_refused_edit_is_not_recorded(self):
        response = self.client.put(
            "/api/rooms/kitchen/adaptive", json={"adaptive": True, "base": "stale"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.log()["entries"], [])

    def test_observation_off_says_why(self):
        observer = self.client.get("/api/status").json()["observer"]
        self.assertEqual(observer, {"running": False, "reason": "observation is off for this app"})


class LogFilterTest(AppCase):
    observe = False

    def setUp(self):
        super().setUp()
        data = self.app.state.data
        for level, category, room, message in (
            ("debug", "heartbeat", None, "tick"),
            ("info", "reactive", "living_room", "manual change"),
            ("warning", "heartbeat", "kitchen", "skipped"),
            ("error", "connection", None, "token rejected"),
        ):
            data.log_event(level, category, message, room_id=room, detail={"why": message})

    def test_filters(self):
        self.assertEqual(self.messages(), ["token rejected", "skipped", "manual change"])
        self.assertEqual(self.messages(level="debug")[-1], "tick")
        self.assertEqual(self.messages(level="warning"), ["token rejected", "skipped"])
        self.assertEqual(self.messages(category="heartbeat", level="debug"), ["skipped", "tick"])
        self.assertEqual(self.messages(room="living_room"), ["manual change"])
        self.assertEqual(self.messages(room=""), ["token rejected", "skipped", "manual change"])
        first = self.log(limit=1)
        self.assertTrue(first["more"])
        older = self.log(before=first["entries"][0]["id"], limit=5)
        self.assertEqual([e["message"] for e in older["entries"]], ["skipped", "manual change"])
        self.assertFalse(older["more"])
        self.assertEqual(first["levels"], ["debug", "info", "warning", "error"])

    def test_bad_requests(self):
        for params in ({"level": "fatal"}, {"limit": 0}, {"limit": 5000}, {"before": "x"}):
            with self.subTest(params):
                response = self.client.get("/api/log", params=params)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"], "bad_request")

    def test_a_broken_config_does_not_hide_the_log(self):
        self.path.write_text("{ not json")
        log = self.log()
        self.assertEqual(len(log["entries"]), 3)
        self.assertEqual(log["entries"][1]["room_name"], None)
        self.assertEqual([r["id"] for r in log["rooms"]], ["kitchen", "living_room"])


class NoStoreTest(unittest.TestCase):
    def test_a_data_dir_that_cannot_be_opened_leaves_the_map_working(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            blocker = Path(directory) / "not-a-dir"
            blocker.write_text("a file where the data directory should be")
            app = create_app(ha=FakeHA(), config_path=path, data_dir=blocker, observe=True)
            with (
                self.assertLogs("app.main", "WARNING"),
                TestClient(app, base_url="http://localhost") as client,
            ):
                self.assertEqual(client.get("/api/map").status_code, 200)
                response = client.get("/api/log")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"], "store_unavailable")
                status = client.get("/api/status").json()
                self.assertFalse(status["store"]["open"])
                self.assertIn("the store did not open", status["observer"]["reason"])
                # Edits still save; they just can't be logged.
                kitchen = next(
                    r for r in client.get("/api/map").json()["rooms"] if r["id"] == "kitchen"
                )
                response = client.put(
                    "/api/rooms/kitchen/adaptive",
                    json={"adaptive": True, "base": kitchen["version"]},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(json.loads(path.read_text())["rooms"][1]["adaptive"])


if __name__ == "__main__":
    unittest.main()
