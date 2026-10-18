"""app.core.observe: the observer, driven by synthetic `state_changed` events through the
fake HA (and once end to end, over the real client and a local WebSocket server), with the
store in a temp directory. Heartbeats with per-bulb readings, lux and occupancy; reactive
detection that ignores the plane's own changes (`parent_id`) and guarded rooms;
consolidation within the window; the guard and section hooks; fan-out; the event log. No
real HA, and the observer never writes to it."""

import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from websockets.asyncio.server import serve

from app.core import config, inventory
from app.core.ha import HAClient
from app.core.observe import Observer, _until_next_tick, is_user_intervention
from app.core.store import Store
from tests.fakes import FakeHA, committed_config
from tests.test_ha import TOKEN, FakeHAServer

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
GUARD = "input_boolean.al_active_living_room"


def event(old, new, *, parent_id=None, event_parent=None, entity="light.chandelier_1"):
    """A state_changed event between two light states, each `None`, `"off"`, or a
    brightness for "on"."""

    def state(value, parent):
        if value is None:
            return None
        if value == "off" or value == "unavailable":
            return {"entity_id": entity, "state": value, "attributes": {}, "context": {}}
        return {
            "entity_id": entity,
            "state": "on",
            "attributes": {"brightness": value},
            "context": {"id": "c", "parent_id": parent, "user_id": None},
        }

    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity,
            "old_state": state(old, None),
            "new_state": state(new, parent_id),
        },
        "context": {"id": "c", "parent_id": event_parent or parent_id, "user_id": None},
    }


class InterventionTest(unittest.TestCase):
    def test_only_an_unparented_brightness_change_counts(self):
        self.assertTrue(is_user_intervention(event(128, 200), guarded=False))
        # Made under an automation run's own context (one fired by time has no parent).
        self.assertFalse(
            is_user_intervention(event(128, 200), guarded=False, automation_runs={"c"})
        )
        self.assertTrue(
            is_user_intervention(event(128, 200), guarded=False, automation_runs={"other"})
        )
        for name, case, guarded in (
            ("the bridge or a switch caused it", event(128, 200, parent_id="auto"), False),
            ("the event's context says so", event(128, 200, event_parent="auto"), False),
            ("a guard is raised", event(128, 200), True),
            ("power on", event("off", 200), False),
            ("power off", event(128, "off"), False),
            ("back from unavailable", event("unavailable", 200), False),
            ("attributes only", event(128, 128), False),
            ("created", event(None, 200), False),
            ("no brightness (non-dimmable)", event(128, "x"), False),
        ):
            with self.subTest(name):
                if name.startswith("no brightness"):
                    case["data"]["new_state"]["attributes"] = {}
                self.assertFalse(is_user_intervention(case, guarded=guarded))

    def test_ticks_align_to_the_clock(self):
        self.assertEqual(
            _until_next_tick(datetime(2026, 1, 1, 12, 3, tzinfo=timezone.utc), 600), 420
        )
        self.assertEqual(
            _until_next_tick(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), 600), 600
        )


class ObserverCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = NOW
        self.store = Store(Path(directory.name), now=lambda: self.now)
        self.store.open()
        self.addCleanup(self.store.close)
        self.ha = self.make_ha()
        self.config = committed_config()
        self.observer = Observer(
            self.ha, self.store, lambda: self.config, now=lambda: self.now, guard_wait=0.2
        )
        self.seen = []
        self.observer.subscribe(self.collect)

    def make_ha(self):
        return FakeHA()

    async def collect(self, observation):
        self.seen.append(copy.deepcopy(observation))

    async def start(self):
        await self.observer.start()
        self.addAsyncCleanup(self.observer.stop)
        for _ in range(50):
            if self.observer.connected:
                return
            await asyncio.sleep(0)
        self.fail("the observer did not subscribe")

    async def manual(self, entity, brightness, **kwargs):
        await self.ha.emit(self.ha.change(entity, "on", brightness=brightness, **kwargs))

    async def close_windows(self, seconds=121):
        self.now += timedelta(seconds=seconds)
        await self.observer.flush()

    def rows(self, sql):
        return self.store.query(sql)

    def log(self, category=None):
        return [(e["level"], e["message"]) for e in reversed(self.store.events(category=category))]


class SubscriptionTest(ObserverCase):
    async def test_it_subscribes_once_the_config_has_bulbs(self):
        self.config = {**committed_config(), "rooms": [], "unassigned": []}
        await self.observer.start()
        self.addAsyncCleanup(self.observer.stop)
        await asyncio.sleep(0.01)
        self.assertEqual(self.ha.subscribers, {})
        self.config = committed_config()  # the first sync saves rooms
        self.assertTrue(self.observer.reload())
        await asyncio.sleep(0.01)
        self.assertIn("state_changed", self.ha.subscribers)
        self.assertTrue(self.observer.connected)
        self.assertEqual(
            self.log("connection"),
            [("info", "Observing 2 room(s) and 5 bulb(s) on Home Assistant 2026.10.0")],
        )

    async def test_it_only_reads(self):
        await self.start()
        await self.manual("light.chandelier_1", 200)
        await self.observer.tick()
        await self.close_windows()
        await self.observer.stop()
        self.assertEqual(self.ha.writes, [])

    async def test_a_drop_and_a_reconnect_reread_the_states(self):
        await self.start()
        reads = sum(1 for call in self.ha.calls if call[0] == "get_states")
        with self.assertLogs("app.core.observe", "WARNING"):
            await self.ha.announce("disconnected", "closed")
        self.assertFalse(self.observer.connected)
        await self.ha.announce("connected", "2026.10.0")
        self.assertTrue(self.observer.connected)
        self.assertEqual(sum(1 for c in self.ha.calls if c[0] == "get_states"), reads + 1)
        with self.assertLogs("app.core.observe", "ERROR"):
            await self.ha.announce("auth_failed", "Invalid access token")
        self.assertEqual(
            self.log("connection")[1:],
            [
                ("warning", "Lost the connection to Home Assistant; reconnecting."),
                ("info", "Reconnected to Home Assistant."),
                (
                    "error",
                    "Home Assistant rejected the token, so observation has stopped: Invalid "
                    "access token. Fix LM_HA_TOKEN (or LM_HA_TOKEN_FILE) and restart Light Map.",
                ),
            ],
        )

    async def test_stop_unsubscribes(self):
        await self.start()
        await self.observer.stop()
        self.assertEqual(self.ha.subscribers["state_changed"], [])
        self.assertEqual(self.ha.listeners, [])

    async def test_a_config_that_stops_loading_keeps_the_last_one(self):
        await self.start()

        def broken():
            raise ValueError("the config at /data/config.json does not load")

        self.observer._load = broken
        with self.assertLogs("app.core.observe", "WARNING"):
            self.assertFalse(self.observer.reload())
        self.assertFalse(self.observer.reload())  # said once, not on every tick
        self.assertEqual(self.observer.status()["rooms"], 2)
        self.assertEqual(len(self.log("observer")), 1)


class HeartbeatTest(ObserverCase):
    async def test_a_heartbeat_records_whether_presence_had_a_sensor(self):
        await self.start()
        self.config["rooms"][0]["adaptive_config"]["presence_sensors"] = ["binary_sensor.sofa"]
        self.observer.reload()
        await self.observer.heartbeat("living_room")
        # A null occupancy now means the sensor had no usable state (it isn't in HA), not
        # "no sensor": the learner tells the two apart by this, never by today's config.
        self.assertEqual(
            self.rows("SELECT presence_configured, occupancy FROM heartbeat"),
            [{"presence_configured": 1, "occupancy": None}],
        )

    async def test_one_heartbeat_per_room_and_one_reading_per_bulb(self):
        await self.start()
        recorded = await self.observer.tick()
        self.assertEqual([o["room_id"] for o in recorded], ["kitchen", "living_room"])
        self.assertEqual(
            self.rows(
                "SELECT room_id, section, lux, lux_sensors, occupancy, deferred_ms "
                "FROM heartbeat ORDER BY room_id"
            ),
            [
                # Kitchen isn't adaptive, so it has no sensors: nothing assumed.
                {
                    "room_id": "kitchen",
                    "section": None,
                    "lux": None,
                    "lux_sensors": 0,
                    "occupancy": None,
                    "deferred_ms": 0,
                },
                {
                    "room_id": "living_room",
                    "section": None,
                    "lux": 42.0,
                    "lux_sensors": 1,
                    "occupancy": None,
                    "deferred_ms": 0,
                },
            ],
        )
        # Whether a presence sensor was configured is recorded with each heartbeat: a null
        # occupancy means "no sensor" or "no usable state", and only now can tell which.
        self.assertEqual(
            self.rows("SELECT room_id, presence_configured FROM heartbeat ORDER BY room_id"),
            [
                {"room_id": "kitchen", "presence_configured": 0},
                {"room_id": "living_room", "presence_configured": 0},
            ],
        )
        self.assertEqual(
            self.rows(
                "SELECT h.room_id, r.bulb_id, r.is_on, r.brightness FROM bulb_reading r "
                "JOIN heartbeat h ON h.id = r.heartbeat_id ORDER BY h.room_id, r.bulb_id"
            ),
            [
                {"room_id": "kitchen", "bulb_id": "kitchen_strip", "is_on": 0, "brightness": None},
                {
                    "room_id": "living_room",
                    "bulb_id": "chandelier_1",
                    "is_on": 1,
                    "brightness": 128,
                },
                {
                    "room_id": "living_room",
                    "bulb_id": "chandelier_2",
                    "is_on": 1,
                    "brightness": 128,
                },
                # Unavailable is not off.
                {
                    "room_id": "living_room",
                    "bulb_id": "chandelier_3",
                    "is_on": None,
                    "brightness": None,
                },
                {
                    "room_id": "living_room",
                    "bulb_id": "reading_lamp",
                    "is_on": 0,
                    "brightness": None,
                },
            ],
        )
        # The unassigned porch belongs to no room, so it isn't observed.
        self.assertNotIn("porch", str(self.rows("SELECT bulb_id FROM bulb_reading")))
        self.assertEqual([o["kind"] for o in self.seen], ["heartbeat", "heartbeat"])
        self.assertEqual(
            self.seen[1]["readings"][0],
            {"bulb_id": "chandelier_1", "is_on": True, "brightness": 128},
        )
        self.assertEqual(
            self.log("heartbeat"), [("debug", "Heartbeat: 2 room(s), 5 bulb(s), 2 on.")]
        )

    async def test_each_heartbeat_reads_ha_afresh(self):
        await self.start()
        self.ha.set_light("light.reading_lamp", "on")  # no event: only a fresh read sees it
        observation = await self.observer.heartbeat("living_room")
        reading = next(r for r in observation["readings"] if r["bulb_id"] == "reading_lamp")
        self.assertTrue(reading["is_on"])
        with self.assertRaises(KeyError):
            await self.observer.heartbeat("garage")  # no bulbs: not observed

    async def test_lux_zero_is_a_floor(self):
        self.config["rooms"][0]["adaptive_config"]["lux_sensors"] = [
            "sensor.living_room_lux",
            "sensor.window_lux",
            "sensor.broken_lux",
        ]
        self.ha.states.append({"entity_id": "sensor.window_lux", "state": "0", "attributes": {}})
        self.ha.states.append(
            {"entity_id": "sensor.broken_lux", "state": "unavailable", "attributes": {}}
        )
        await self.start()
        cases = (
            ("42", "0", 42.0, 1),  # a floor never drags a measurement down
            ("0", "0", 0.0, 2),  # every sensor at its floor: dark, recorded as 0
            ("unavailable", "unknown", None, 0),  # no reading at all
            ("40", "61", 50.5, 2),
        )
        for first, second, lux, sensors in cases:
            with self.subTest(first=first, second=second):
                self.ha.change("sensor.living_room_lux", first)
                self.ha.change("sensor.window_lux", second)
                self.now += timedelta(minutes=10)
                observation = await self.observer.heartbeat("living_room")
                self.assertEqual(
                    (observation["heartbeat"]["lux"], observation["heartbeat"]["lux_sensors"]),
                    (lux, sensors),
                )

    async def test_occupancy_from_presence_sensors(self):
        self.config["rooms"][0]["adaptive_config"]["presence_sensors"] = [
            "binary_sensor.sofa",
            "binary_sensor.door",
        ]
        await self.start()
        for sofa, door, occupancy in (
            ("off", "on", True),
            ("off", "off", False),
            ("off", "unavailable", False),
            ("unavailable", "unknown", None),
        ):
            with self.subTest(sofa=sofa, door=door):
                self.ha.change("binary_sensor.sofa", sofa)
                self.ha.change("binary_sensor.door", door)
                self.now += timedelta(minutes=10)
                observation = await self.observer.heartbeat("living_room")
                self.assertIs(observation["heartbeat"]["occupancy"], occupancy)

    async def test_ha_unreachable_skips_the_tick_once_with_a_warning(self):
        await self.start()

        async def down():
            from app.core.ha import HAError

            raise HAError("Home Assistant did not answer")

        reachable = self.ha.get_states
        self.ha.get_states = down
        with self.assertLogs("app.core.observe", "WARNING"):
            self.assertEqual(await self.observer.tick(), [])
        self.assertEqual(await self.observer.tick(), [])
        self.ha.get_states = reachable
        self.assertEqual(len(await self.observer.tick()), 2)
        self.assertEqual(
            [m for _, m in self.log("heartbeat") if "Heartbeat:" not in m],
            [
                "Heartbeats are skipped while Home Assistant can't be read: "
                "Home Assistant did not answer",
                "Heartbeats resumed.",
            ],
        )


class ReactiveTest(ObserverCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.start()

    def reactive(self):
        return self.rows(
            "SELECT ts, room_id, section, window_seconds, lux_before, lux_after, occupancy "
            "FROM reactive_event ORDER BY ts"
        )

    def changes(self):
        return self.rows(
            "SELECT e.ts, c.bulb_id, c.is_on_before, c.brightness_before, c.is_on_after, "
            "c.brightness_after, c.changed FROM reactive_change c "
            "JOIN reactive_event e ON e.id = c.reactive_event_id ORDER BY e.ts, c.bulb_id"
        )

    async def test_a_manual_change_becomes_a_reactive_event(self):
        await self.manual("light.chandelier_1", 200, user_id="person")
        self.ha.change("sensor.living_room_lux", "61")  # the room got brighter
        await self.ha.emit(self.ha.change("sensor.living_room_lux", "61"))
        self.assertEqual(self.reactive(), [])  # nothing until the window closes
        await self.close_windows()
        self.assertEqual(
            self.reactive(),
            [
                {
                    "ts": "2026-10-16T12:00:00.000000+00:00",
                    "room_id": "living_room",
                    "section": None,
                    "window_seconds": 120,
                    "lux_before": 42.0,
                    "lux_after": 61.0,
                    "occupancy": None,
                }
            ],
        )
        # The whole room, as the user left it; only the bulb they touched is `changed`.
        self.assertEqual(
            [
                (c["bulb_id"], c["brightness_before"], c["brightness_after"], c["changed"])
                for c in self.changes()
            ],
            [
                ("chandelier_1", 128, 200, 1),
                ("chandelier_2", 128, 128, 0),
                ("chandelier_3", None, None, 0),
                ("reading_lamp", None, None, 0),
            ],
        )
        self.assertEqual(self.seen[-1]["kind"], "reactive")
        self.assertEqual(self.seen[-1]["event"]["lux_after"], 61.0)
        self.assertEqual(
            self.log("reactive"), [("info", "Manual change in Living Room: chandelier_1.")]
        )
        self.assertEqual(
            self.store.events(category="reactive")[0]["detail"], {"chandelier_1": [128, 200]}
        )

    async def test_the_planes_own_changes_are_never_counted(self):
        # The bridge powering a bulb on at a brightness, and a hard switch's press: both
        # carry the automation's parent_id.
        await self.manual("light.chandelier_1", 200, parent_id="bridge-run")
        await self.ha.emit(
            self.ha.change("light.reading_lamp", "on", brightness=90, parent_id="hard")
        )
        await self.manual("light.reading_lamp", 30, parent_id="bridge-run")
        # A power change with no parent_id (HA's UI) is a power decision, not a preference.
        await self.ha.emit(self.ha.change("light.chandelier_2", "off"))
        self.assertEqual(self.observer.status()["open_windows"], 0)
        await self.close_windows()
        self.assertEqual(self.reactive(), [])
        self.assertEqual([o["kind"] for o in self.seen], [])

    async def test_changes_within_the_window_are_one_event(self):
        await self.manual("light.chandelier_1", 150)
        self.now += timedelta(seconds=5)
        await self.manual("light.chandelier_2", 60)
        self.now += timedelta(seconds=5)
        await self.manual("light.chandelier_1", 180)  # the same bulb again
        await self.close_windows(seconds=111)
        self.assertEqual(len(self.reactive()), 1)
        self.assertEqual(
            [
                (c["bulb_id"], c["brightness_before"], c["brightness_after"])
                for c in self.changes()
                if c["changed"]
            ],
            [("chandelier_1", 128, 180), ("chandelier_2", 128, 60)],
        )
        # After the window, a new change starts a new event.
        await self.manual("light.chandelier_1", 100)
        await self.close_windows()
        self.assertEqual(len(self.reactive()), 2)
        self.assertEqual(self.reactive()[1]["ts"], "2026-10-16T12:02:01.000000+00:00")

    async def test_a_change_inside_the_min_delta_is_noise(self):
        await self.manual("light.chandelier_1", 131)  # 3 from 128; the min-delta is 5
        await self.close_windows()
        self.assertEqual(self.reactive(), [])
        self.config["system"] = {"reactive_min_delta": 2}
        self.observer.reload()
        await self.manual("light.chandelier_1", 128)
        await self.close_windows()
        self.assertEqual(len(self.reactive()), 1)

    async def test_the_window_comes_from_the_config(self):
        self.config["system"] = {"reactive_window_seconds": 30}
        self.observer.reload()
        await self.manual("light.chandelier_1", 200)
        await self.close_windows(seconds=29)
        self.assertEqual(self.reactive(), [])
        await self.close_windows(seconds=1)
        self.assertEqual(self.reactive()[0]["window_seconds"], 30)

    async def test_the_window_closes_by_itself(self):
        self.observer._settings["reactive_window_seconds"] = 1  # below what the config allows
        await self.manual("light.chandelier_1", 200)
        await asyncio.sleep(0.5)
        self.assertEqual(self.reactive(), [])
        await asyncio.sleep(0.7)
        self.assertEqual(len(self.reactive()), 1)

    async def test_stopping_flushes_an_open_window(self):
        await self.manual("light.chandelier_1", 200)
        await self.observer.stop()
        self.assertEqual(len(self.reactive()), 1)

    async def test_a_time_triggered_automation_is_not_a_user(self):
        # HA starts an automation fired by time, a time pattern or the sun with a fresh
        # context and no parent_id; its light.turn_on would otherwise pass for a dimmer.
        run, context = self.ha.automation_run("nightly_dim")
        await self.ha.emit(run)
        await self.manual("light.chandelier_1", 40, context_id=context)
        await self.ha.emit(self.ha.change("light.reading_lamp", "on", brightness=90))  # power
        await self.manual("light.reading_lamp", 30, context_id=context)
        self.assertEqual(self.observer.status()["open_windows"], 0)
        await self.manual("light.chandelier_1", 120, user_id="person")  # a person after it
        await self.close_windows()
        self.assertEqual(
            [
                (c["bulb_id"], c["brightness_before"], c["brightness_after"])
                for c in self.changes()
                if c["changed"]
            ],
            [("chandelier_1", 40, 120)],
        )

    async def test_the_users_change_is_recorded_not_what_followed_it(self):
        await self.manual("light.chandelier_1", 60, user_id="person")
        await self.manual("light.chandelier_2", 90)  # a dimmer: no user
        # Before the window closes, a hard switch turns chandelier_1 off, an automation
        # moves chandelier_2, and the reading lamp's power goes.
        await self.ha.emit(self.ha.change("light.chandelier_1", "off", parent_id="hard"))
        await self.manual("light.chandelier_2", 250, parent_id="automation")
        await self.ha.emit(self.ha.change("light.reading_lamp", "unavailable"))
        await self.close_windows()
        rows = self.rows(
            "SELECT bulb_id, is_on_after, brightness_after, changed, user_id FROM reactive_change "
            "ORDER BY bulb_id"
        )
        self.assertEqual(
            rows[:2],
            [
                {
                    "bulb_id": "chandelier_1",
                    "is_on_after": 1,
                    "brightness_after": 60,
                    "changed": 1,
                    "user_id": "person",
                },
                {
                    "bulb_id": "chandelier_2",
                    "is_on_after": 1,
                    "brightness_after": 90,
                    "changed": 1,
                    "user_id": None,
                },
            ],
        )
        # An untouched bulb is recorded as the close found it: context, never a change.
        self.assertEqual(
            rows[3],
            {
                "bulb_id": "reading_lamp",
                "is_on_after": None,
                "brightness_after": None,
                "changed": 0,
                "user_id": None,
            },
        )

    async def test_a_change_survives_its_bulb_going_unavailable(self):
        await self.manual("light.chandelier_1", 60)
        await self.ha.emit(self.ha.change("light.chandelier_1", "unavailable"))
        await self.close_windows()
        self.assertEqual(
            [(c["bulb_id"], c["brightness_after"]) for c in self.changes() if c["changed"]],
            [("chandelier_1", 60)],
        )

    async def test_nothing_is_taken_once_stopping(self):
        await self.manual("light.chandelier_1", 60)
        stopping = asyncio.create_task(self.observer.stop())
        await asyncio.sleep(0)  # stop() has begun: it waits on its tasks
        self.assertFalse(stopping.done())
        await self.manual("light.chandelier_2", 20)  # arrives while it stops
        await stopping
        self.assertEqual(self.observer.status()["open_windows"], 0)
        self.assertEqual([c["bulb_id"] for c in self.changes() if c["changed"]], ["chandelier_1"])

    async def test_a_stale_snapshot_never_overwrites_a_newer_event(self):
        await self.manual("light.chandelier_1", 200)
        stale = copy.deepcopy(
            next(s for s in self.ha.states if s["entity_id"] == "light.chandelier_1")
        )
        stale["attributes"]["brightness"] = 10
        stale["last_updated"] = "2026-10-16T10:00:00+00:00"
        index = next(
            i for i, s in enumerate(self.ha.states) if s["entity_id"] == "light.chandelier_1"
        )
        self.ha.states[index] = stale  # a read that raced the event
        await self.observer._refresh_states()
        await self.close_windows()
        self.assertEqual(self.changes()[0]["brightness_after"], 200)


class HooksTest(ObserverCase):
    def guards(self, config, room):
        return [GUARD] if room["id"] == "living_room" else []

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ha.states.append({"entity_id": GUARD, "state": "off", "attributes": {}})
        self.observer.register_guard(self.guards)
        await self.start()

    async def test_a_raised_guard_ignores_brightness_changes(self):
        await self.ha.emit(self.ha.change(GUARD, "on"))
        self.assertTrue(self.observer.guarded("living_room"))
        await self.manual("light.chandelier_1", 200)  # a long transition's late step
        await self.ha.emit(self.ha.change(GUARD, "off"))
        await self.close_windows()
        self.assertEqual(self.rows("SELECT * FROM reactive_event"), [])

    async def test_a_heartbeat_waits_for_the_guard(self):
        await self.ha.emit(self.ha.change(GUARD, "on"))
        heartbeat = asyncio.create_task(self.observer.heartbeat("living_room"))
        await asyncio.sleep(0.05)
        self.assertFalse(heartbeat.done())  # never sampled mid-transition
        await self.ha.emit(self.ha.change(GUARD, "off"))
        observation = await asyncio.wait_for(heartbeat, 1)
        self.assertGreaterEqual(observation["heartbeat"]["deferred_ms"], 40)

    async def test_a_guard_that_stays_up_skips_the_heartbeat(self):
        await self.ha.emit(self.ha.change(GUARD, "on"))
        with self.assertLogs("app.core.observe", "WARNING"):
            recorded = await self.observer.tick()
        self.assertEqual([o["room_id"] for o in recorded], ["kitchen"])  # not guarded
        self.assertEqual(self.rows("SELECT room_id FROM heartbeat"), [{"room_id": "kitchen"}])
        self.assertIn("Skipped a heartbeat", self.log("heartbeat")[0][1])

    async def test_the_section_is_recorded_with_each_observation(self):
        self.observer.register_section(
            lambda room_id: "evening" if room_id == "living_room" else None
        )
        with self.assertRaises(ValueError):
            self.observer.register_section(lambda room_id: None)
        await self.observer.heartbeat("living_room")
        await self.manual("light.chandelier_1", 200)
        await self.close_windows()
        self.assertEqual(self.rows("SELECT section FROM heartbeat"), [{"section": "evening"}])
        self.assertEqual(self.rows("SELECT section FROM reactive_event"), [{"section": "evening"}])

    async def test_a_failing_module_handler_stops_nothing(self):
        async def broken(observation):
            raise RuntimeError("module bug")

        later = []

        async def after(observation):
            later.append(observation["kind"])

        self.observer._handlers.insert(0, broken)
        self.observer.subscribe(after)
        with self.assertLogs("app.core.observe", "ERROR"):
            await self.observer.heartbeat("kitchen")
        self.assertEqual(later, ["heartbeat"])
        self.assertEqual(len(self.rows("SELECT * FROM heartbeat")), 1)  # stored first
        self.assertIn("RuntimeError('module bug')", self.log("observer")[0][1])


class IgnoredTest(ObserverCase):
    """An ignored bulb (P4.1: a light the user set aside, like an indicator LED) is never
    read, and a room whose bulbs are all ignored isn't observed."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        doc = inventory.set_ignored(committed_config(), "chandelier_1", True)
        self.config = config.checked(inventory.set_ignored(doc, "kitchen_strip", True))
        await self.start()

    async def test_an_ignored_bulb_gets_no_reading(self):
        recorded = await self.observer.tick()
        self.assertEqual([o["room_id"] for o in recorded], ["living_room"])  # kitchen: none left
        self.assertEqual(
            [r["bulb_id"] for r in self.rows("SELECT bulb_id FROM bulb_reading ORDER BY bulb_id")],
            ["chandelier_2", "chandelier_3", "reading_lamp"],
        )
        self.assertEqual(self.observer.status()["bulbs"], 3)

    async def test_an_ignored_bulb_is_never_a_reactive_change(self):
        await self.manual("light.chandelier_1", 200)  # an LED dimming itself, say
        await self.manual("light.kitchen_strip", 90)
        await self.close_windows()
        self.assertEqual(self.rows("SELECT * FROM reactive_event"), [])
        # Its room's other bulbs are still watched.
        await self.manual("light.chandelier_2", 200)
        await self.close_windows()
        self.assertEqual(
            [c["bulb_id"] for c in self.rows("SELECT bulb_id FROM reactive_change ORDER BY 1")],
            ["chandelier_2", "chandelier_3", "reading_lamp"],
        )

    async def test_a_bulb_ignored_while_a_window_is_open_isnt_read_at_the_close(self):
        # On through the plane (a parent_id: not an intervention), then dimmed by hand.
        lamp_on = self.ha.change("light.reading_lamp", "on", brightness=100, parent_id="p")
        await self.ha.emit(lamp_on)
        await self.manual("light.chandelier_2", 200)
        await self.manual("light.reading_lamp", 60)  # touched while it was still watched
        doc = inventory.set_ignored(self.config, "chandelier_3", True)
        self.config = config.checked(inventory.set_ignored(doc, "reading_lamp", True))
        self.observer.reload()
        await self.close_windows()
        self.assertEqual(
            self.rows(
                "SELECT bulb_id, brightness_after, changed FROM reactive_change ORDER BY bulb_id"
            ),
            [
                {"bulb_id": "chandelier_2", "brightness_after": 200, "changed": 1},
                # Kept: the user changed it while it was watched, and that is what's recorded.
                {"bulb_id": "reading_lamp", "brightness_after": 60, "changed": 1},
            ],
        )  # chandelier_3, untouched and no longer watched, isn't recorded as unavailable

    async def test_un_ignored_it_is_watched_again(self):
        self.config = config.checked(inventory.set_ignored(self.config, "kitchen_strip", False))
        self.observer.reload()
        recorded = await self.observer.tick()
        self.assertEqual([o["room_id"] for o in recorded], ["kitchen", "living_room"])


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Over the real client: events arrive on the WebSocket from a local fake server
    (subscribed after the read loop started), states over REST from a mock transport."""

    async def asyncSetUp(self):
        self.fake = FakeHAServer()
        self.server = await serve(self.fake.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.ha_states = FakeHA()
        self.addAsyncCleanup(self.stop_server)

        def rest(request):
            return httpx.Response(200, json=self.ha_states.states)

        self.client = HAClient(
            f"http://127.0.0.1:{port}",
            TOKEN,
            transport=httpx.MockTransport(rest),
            reconnect_backoff=(0.01,),
        )
        self.addAsyncCleanup(self.client.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.store.open()
        self.addCleanup(self.store.close)
        self.observer = Observer(self.client, self.store, committed_config())
        self.addAsyncCleanup(self.observer.stop)

    async def stop_server(self):
        self.server.close()
        await self.server.wait_closed()

    async def wait_for(self, condition):
        for _ in range(300):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("timed out")

    async def test_events_over_the_websocket(self):
        await self.observer.start()
        await self.wait_for(lambda: self.observer.connected)
        self.assertEqual(
            [(m["type"], m["event_type"]) for m in self.fake.received],
            [("subscribe_events", "automation_triggered"), ("subscribe_events", "state_changed")],
        )
        await self.fake.fire(
            "state_changed",
            self.ha_states.change("light.chandelier_1", "on", brightness=60, parent_id="bridge"),
        )
        # A time-triggered automation: no parent_id, but its run's context gives it away.
        run, context = self.ha_states.automation_run("dim_at_ten")
        await self.fake.fire("automation_triggered", run)
        await self.fake.fire(
            "state_changed",
            self.ha_states.change("light.reading_lamp", "on", brightness=10, context_id=context),
        )
        await self.fake.fire(
            "state_changed",
            self.ha_states.change("light.reading_lamp", "on", brightness=40, context_id=context),
        )
        await self.fake.fire(
            "state_changed", self.ha_states.change("light.chandelier_2", "on", brightness=60)
        )
        await self.wait_for(lambda: self.observer.status()["events_seen"] == 4)
        await self.observer.flush(everything=True)
        changed = self.store.query("SELECT bulb_id FROM reactive_change WHERE changed = 1")
        self.assertEqual(changed, [{"bulb_id": "chandelier_2"}])
        # A drop: the ladder reconnects and subscribes again, and the stream still flows.
        with self.assertLogs("app.core.observe", "WARNING"):
            await self.fake.hang_up()
            await self.wait_for(lambda: self.fake.connections == 2 and self.observer.connected)
        await self.fake.fire(
            "state_changed", self.ha_states.change("light.chandelier_2", "on", brightness=200)
        )
        await self.wait_for(lambda: self.observer.status()["events_seen"] == 5)
        messages = [e["message"] for e in reversed(self.store.events(category="connection"))]
        self.assertEqual(
            messages,
            [
                "Observing 2 room(s) and 5 bulb(s) on Home Assistant 2026.10.0",
                "Lost the connection to Home Assistant; reconnecting.",
                "Reconnected to Home Assistant.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
