"""The adaptive runtime (P5.2a): its hooks into the observer (the section, the guard, the
hold on a reactive event), level-triggered crossovers (fired once per section across a day,
caught up after downtime, never on a restart into the same section), the nightly pass and
the level-triggered publish, its failures, and the composition root wiring it in only
behind the cutover switch. Against the fake HA and a temporary store; nothing here reaches
a real HA."""

import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import main
from app.core import config, switching
from app.core.ha import HAError
from app.core.observe import Observer
from app.core.store import Store
from app.modules.adaptive import almanac
from app.modules.adaptive.runtime import AdaptiveRuntime
from tests.fakes import FakeHA, adaptive_wired, committed_config

LONDON = ZoneInfo("Europe/London")
ROOM = "living_room"
SELECT = "input_select.al_scene_living_room"
HOLD = "input_boolean.al_hold_living_room"
SCENES = [{"id": f"al_scene_{ROOM}_{s}", "alias": f"al_scene_{ROOM}_{s}"} for s in config.SECTIONS]


def local(day, hh, mm=0):
    return datetime.combine(day, time(hh, mm), tzinfo=LONDON)


MIDSUMMER = datetime(2026, 6, 21).date()


class RuntimeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.store.open()
        self.addCleanup(self.store.close)
        self.doc = config.loads(committed_config())
        self.ha = FakeHA(automations=copy.deepcopy(SCENES))
        self.ha.helper_states[SELECT] = "sunrise"
        self.clock = local(MIDSUMMER, 10, 30)
        self.observer = Observer(self.ha, self.store, lambda: self.doc)
        self.runtime = AdaptiveRuntime(
            self.ha, self.observer, self.store, lambda: self.doc, now=lambda: self.clock
        )
        self.runtime._attach()
        self.observer.reload()  # as its start does

    async def tick(self, at=None):
        if at is not None:
            self.clock = at
        await self.runtime.tick(self.clock)
        await self.settle()

    async def settle(self):
        if self.runtime._background:
            await asyncio.gather(*list(self.runtime._background))

    def services(self, domain=None):
        return [
            (d, s, data)
            for method, (d, s, data) in ((m, a) for m, a in self.ha.calls if m == "call_service")
            if domain is None or d == domain
        ]

    def crossovers(self):
        return [data["option"] for d, s, data in self.services("input_select")]

    def triggered(self):
        return [data["entity_id"] for d, s, data in self.services("automation")]

    def events(self, category=None):
        return [
            e for e in self.store.events(limit=500) if category is None or e["category"] == category
        ]


class HookTest(RuntimeCase):
    async def test_it_hooks_into_the_observer(self):
        self.assertEqual(self.observer._section_provider, self.runtime.section)
        self.assertIn(self.runtime.on_observation, self.observer._handlers)
        living, kitchen = self.doc["rooms"]
        self.assertEqual(
            self.runtime.guards(self.doc, living), ["input_boolean.al_active_living_room"]
        )
        self.assertEqual(self.runtime.guards(self.doc, kitchen), [])
        # The observer now waits on the adaptive room's guard, and only that room's.
        self.assertEqual(
            self.observer._rooms[ROOM].guards, ("input_boolean.al_active_living_room",)
        )
        self.assertEqual(self.observer._rooms["kitchen"].guards, ())
        self.assertIn(self.runtime._on_connection, self.ha.listeners)

    async def test_the_section_an_observation_records(self):
        # Before HA's location is known: none. Then the scheduled one, until a crossover.
        self.assertIsNone(self.runtime.section(ROOM))
        self.ha.helper_states[SELECT] = "day"  # already there: adopted, not fired
        await self.tick()
        self.assertEqual(self.runtime.section(ROOM), "day")
        self.assertEqual(self.crossovers(), [])
        # A heartbeat records it.
        recorded = await self.observer.heartbeat(ROOM)
        self.assertEqual(recorded["heartbeat"]["section"], "day")
        # A room that isn't adaptive has none.
        self.assertIsNone(self.runtime.section("kitchen"))
        self.assertIsNone(self.runtime.section("nowhere"))

    async def test_a_reactive_event_raises_the_hold(self):
        await self.runtime.on_observation({"kind": "heartbeat", "room_id": ROOM})
        await self.runtime.on_observation({"kind": "reactive", "room_id": "kitchen"})
        self.assertEqual(self.services(), [])
        await self.runtime.on_observation(
            {"kind": "reactive", "room_id": ROOM, "event": {}, "changes": []}
        )
        self.assertEqual(self.services(), [("input_boolean", "turn_on", {"entity_id": HOLD})])
        self.assertEqual(self.ha.helper_states[HOLD], "on")
        self.assertEqual(len(self.events("hold")), 1)
        # It records nothing itself: the observer did.
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM reactive_event")[0]["n"], 0)

    async def test_a_hold_ha_refuses_is_logged_not_raised(self):
        with mock.patch.object(self.ha, "call_service", side_effect=HAError("down")):
            await self.runtime.on_observation({"kind": "reactive", "room_id": ROOM})
        self.assertIn("Could not hold", self.events("hold")[0]["message"])

    async def test_a_real_reactive_event_reaches_it_through_the_observer(self):
        await self.observer.start()
        self.addAsyncCleanup(self.observer.stop)
        for _ in range(50):
            if self.observer.connected:
                break
            await asyncio.sleep(0)
        self.observer._settings["reactive_window_seconds"] = 1
        await self.ha.emit(self.ha.change("light.chandelier_1", "on", brightness=128))
        await self.ha.emit(self.ha.change("light.chandelier_1", "on", brightness=40))
        await self.observer.flush(everything=True)
        self.assertEqual(self.ha.helper_states.get(HOLD), "on")


class CrossoverTest(RuntimeCase):
    async def test_a_section_change_sets_the_select_and_fires_the_scene(self):
        await self.tick()  # 10:30 on midsummer's day: day (sunrise + 3 h is 07:43)
        self.assertEqual(self.crossovers(), ["day"])
        self.assertEqual(self.triggered(), [f"automation.al_scene_{ROOM}_day"])
        # The select first, then the scene, which HA answers only when its run is over.
        [select, trigger] = [c for c in self.ha.calls if c[0] == "call_service"]
        self.assertEqual(select[1][:2], ("input_select", "select_option"))
        self.assertEqual(trigger[1][:2], ("automation", "trigger"))
        self.assertEqual(self.ha.helper_states[SELECT], "day")
        [event] = [e for e in self.events("scene") if "began" in e["message"]]
        # Day began at sunrise + 3 h (07:42 that day): fired at 10:30, it's a catch-up.
        self.assertRegex(
            event["message"], r"^Living Room: day began \(\d+ s after its boundary\)\.$"
        )

    async def test_nothing_fires_while_the_section_is_unchanged(self):
        await self.tick()
        before = len(self.services())
        for minutes in (1, 5, 20, 60):
            await self.tick(local(MIDSUMMER, 10, 30) + timedelta(minutes=minutes))
        self.assertEqual(len(self.services()), before)

    async def test_once_per_section_across_a_day(self):
        self.ha.helper_states[SELECT] = "sleep"
        moment = local(MIDSUMMER, 0, 0)
        while moment < local(MIDSUMMER, 0, 0) + timedelta(days=1):
            await self.tick(moment)
            moment += timedelta(minutes=10)
        # Midsummer in London: sunset falls after night, so it collapses for the day.
        self.assertEqual(self.crossovers(), ["sunrise", "day", "afternoon", "night", "sleep"])
        self.assertEqual(
            self.triggered(), [f"automation.al_scene_{ROOM}_{s}" for s in self.crossovers()]
        )
        skipped = [e["message"] for e in self.events("scene") if "skipped" in e["message"]]
        self.assertEqual(len(skipped), 1)
        self.assertIn("sunset is skipped today: order conflict", skipped[0])

    async def test_level_triggered_it_catches_up_to_the_section_in_force(self):
        # Down from mid-afternoon to 23:00: it fires sleep once, not night then sleep.
        self.ha.helper_states[SELECT] = "afternoon"
        await self.tick(local(MIDSUMMER, 23, 0))
        self.assertEqual(self.crossovers(), ["sleep"])
        self.assertIn("after its boundary", self.events("scene")[0]["message"])

    async def test_a_restart_into_the_same_section_fires_nothing(self):
        # The select already shows the section in force: re-firing it would turn back on
        # every soft switch the user has turned off since.
        self.ha.helper_states[SELECT] = "day"
        await self.tick()
        self.assertEqual(self.services(), [])

    async def test_a_scene_is_found_by_its_id_not_its_entity_id(self):
        # One adopted by an update keeps its old entity id.
        self.ha.automations[f"al_scene_{ROOM}_day"]["alias"] = "Old name"
        real_states = self.ha.get_states

        async def renamed():
            states = await real_states()
            for state in states:
                if state["entity_id"] == f"automation.al_scene_{ROOM}_day":
                    state["entity_id"] = "automation.old_name"
            return states

        with mock.patch.object(self.ha, "get_states", renamed):
            await self.tick()
        self.assertEqual(self.triggered(), ["automation.old_name"])

    async def test_a_missing_scene_sets_the_section_and_waits_for_the_next(self):
        del self.ha.automations[f"al_scene_{ROOM}_day"]
        await self.tick()
        self.assertEqual(self.crossovers(), ["day"])
        self.assertEqual(self.triggered(), [])
        self.assertIn("isn't in Home Assistant", self.events("scene")[0]["message"])
        await self.tick(self.clock + timedelta(minutes=1))
        self.assertEqual(self.crossovers(), ["day"])  # not retried every tick

    async def test_a_crossover_ha_cant_take_is_retried_and_logged_once(self):
        real = self.ha.call_service
        with mock.patch.object(self.ha, "call_service", side_effect=HAError("down")):
            for minutes in range(3):
                await self.tick(local(MIDSUMMER, 10, 30) + timedelta(minutes=minutes))
        self.assertEqual(len([e for e in self.events("scene") if "retrying" in e["message"]]), 1)
        self.assertEqual(self.runtime._fired[ROOM], "sunrise")  # still where it was
        with mock.patch.object(self.ha, "call_service", real):
            await self.tick(local(MIDSUMMER, 10, 34))
        self.assertEqual(self.crossovers(), ["day"])

    async def test_a_scene_that_fails_is_retried(self):
        real = self.ha.call_service

        async def flaky(domain, service, data, **kwargs):
            if domain == "automation" and not getattr(flaky, "once", False):
                flaky.once = True
                raise HAError("POST /api/services/automation/trigger failed: ReadTimeout")
            return await real(domain, service, data, **kwargs)

        with mock.patch.object(self.ha, "call_service", flaky):
            await self.tick()
            self.assertNotIn(ROOM, self.runtime._fired)
            await self.tick(self.clock + timedelta(seconds=20))
        self.assertEqual(self.triggered(), [f"automation.al_scene_{ROOM}_day"])
        self.assertEqual(self.runtime._fired[ROOM], "day")

    async def test_the_trigger_waits_for_the_run(self):
        seen = {}
        real = self.ha.call_service

        async def watch(domain, service, data, *, timeout=None):
            seen[(domain, service)] = timeout
            return await real(domain, service, data)

        with mock.patch.object(self.ha, "call_service", watch):
            await self.tick()
        # The day scene's 120 s transition, its 30 s settle, and a margin.
        self.assertEqual(seen[("automation", "trigger")], 120 + 30 + 60)
        self.assertIsNone(seen[("input_select", "select_option")])

    async def test_nothing_until_ha_says_where_and_when(self):
        with self.assertLogs("app.modules.adaptive.runtime", "WARNING"):
            with mock.patch.object(self.ha, "get_config", side_effect=HAError("down")):
                await self.tick()
            self.ha.location = {"time_zone": "Europe/London"}  # no latitude
            await self.tick()
        self.assertEqual(self.services(), [])
        self.assertEqual(len([e for e in self.events() if "location" in e["message"]]), 2)

    async def test_a_room_made_adaptive_later_is_picked_up(self):
        await self.tick()
        kitchen = self.doc["rooms"][1]
        kitchen["adaptive"] = True
        kitchen["adaptive_config"] = {}
        self.doc = config.loads(self.doc)
        self.ha.helper_states["input_select.al_scene_kitchen"] = "sunrise"
        await self.tick(self.clock + timedelta(minutes=1))
        self.assertEqual(
            [data["entity_id"] for d, s, data in self.services("input_select")],
            [SELECT, "input_select.al_scene_kitchen"],
        )


class NightlyAndPublishTest(RuntimeCase):
    def night_heartbeats(self, nights=2):
        for d in range(nights):
            day = MIDSUMMER - timedelta(days=d + 1)
            for i in range(6):
                self.store.append_heartbeat(
                    {
                        "ts": local(day, 22, 10) + timedelta(minutes=10 * i),
                        "room_id": ROOM,
                        "section": "sleep",
                        "lux": 3.0,
                    },
                    [
                        {"bulb_id": b, "is_on": True, "brightness": 30}
                        for b in ("chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp")
                    ],
                )

    def published(self):
        return [
            (entity, state, attributes)
            for m, (entity, state, attributes) in (
                (m, a) for m, a in self.ha.calls if m == "set_state"
            )
        ]

    async def test_the_nightly_pass_records_and_publishes(self):
        self.night_heartbeats()
        await self.tick(local(MIDSUMMER, 0, 10))  # before 00:15: not yet
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM almanac")[0]["n"], 0)
        [(entity, state, attributes)] = self.published()  # nothing learned, still published
        self.assertEqual((entity, state), ("sensor.al_almanac_living_room", "unknown"))
        await self.tick(local(MIDSUMMER, 0, 20))
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM almanac")[0]["n"], 12)
        entity, state, attributes = self.published()[-1]
        self.assertEqual(state, "2026-06-20")  # bootstrap: valid from its last day
        self.assertEqual(attributes["sleep"]["units"], {"chandelier": 30, "reading": 30})
        self.assertEqual(attributes["sleep"]["lux_target"], 3.0)
        self.assertEqual(len(self.events("analysis")), 1)
        self.assertIsNotNone(self.runtime.status()["last_analysis"])
        # Once a day: not again at the next tick, nor after a restart the same day.
        await self.tick(local(MIDSUMMER, 3, 0))
        restarted = AdaptiveRuntime(
            self.ha, Observer(self.ha, self.store, lambda: self.doc), self.store, lambda: self.doc
        )
        await restarted.tick(local(MIDSUMMER, 4, 0))
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM almanac")[0]["n"], 12)
        # The next night's pass runs.
        await self.tick(local(MIDSUMMER + timedelta(days=1), 0, 15))
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM almanac")[0]["n"], 24)

    async def test_a_failing_pass_is_logged_and_waits_a_day(self):
        with (
            mock.patch("app.modules.adaptive.analyser.run", side_effect=RuntimeError("boom")),
            self.assertLogs("app.modules.adaptive.runtime", "ERROR"),
        ):
            await self.tick(local(MIDSUMMER, 0, 20))
            await self.tick(local(MIDSUMMER, 0, 40))
        [event] = self.events("analysis")
        self.assertIn("boom", event["message"])

    async def test_the_publish_is_level_triggered(self):
        await self.tick()
        self.assertEqual(len(self.published()), 1)
        await self.tick(self.clock + timedelta(minutes=1))
        self.assertEqual(len(self.published()), 1)  # unchanged: not published again
        # A unit turned off in the editor: a republish, nothing else.
        self.doc["rooms"][0]["adaptive_config"]["scenes"]["sleep"]["units"]["reading"]["mode"] = (
            "off"
        )
        await self.tick(self.clock + timedelta(minutes=2))
        self.assertEqual(len(self.published()), 2)
        self.assertEqual(self.published()[-1][2]["sleep"]["units"]["reading"], "off")
        # HA restarted (a reconnect): states set over REST are gone, so publish again.
        await self.ha.announce("connected", "2026.10.0")
        await self.tick(self.clock + timedelta(minutes=3))
        self.assertEqual(len(self.published()), 3)

    async def test_a_snapshot_coming_into_force_is_published(self):
        rows = [
            {
                "ts": local(MIDSUMMER, 0, 15),
                "room_id": ROOM,
                "section": s,
                "learning_unit_id": u,
                "brightness": 80,
                "days_analysed": 8,
                "valid_from": "2026-06-23",
                "mode": "learning",
            }
            for s in config.SECTIONS
            for u in ("chandelier", "reading")
        ]
        almanac.record(self.store, rows)
        await self.tick(local(MIDSUMMER, 12, 0))
        self.assertEqual(self.published()[-1][1], "unknown")
        await self.tick(local(MIDSUMMER + timedelta(days=2), 0, 1))
        self.assertEqual(self.published()[-1][1], "2026-06-23")
        self.assertEqual(self.published()[-1][2]["day"]["units"]["chandelier"], 80)

    async def test_publish_almanac_publishes_now(self):
        await self.runtime.publish_almanac(ROOM)
        await self.runtime.publish_almanac(ROOM)
        self.assertEqual(len(self.published()), 2)
        with self.assertRaises(KeyError):
            await self.runtime.publish_almanac("kitchen")

    async def test_a_publish_ha_refuses_is_retried(self):
        with mock.patch.object(self.ha, "set_state", side_effect=HAError("down")):
            await self.tick()
            await self.tick(self.clock + timedelta(minutes=1))
        self.assertEqual(len([e for e in self.events() if "couldn't publish" in e["message"]]), 1)
        await self.tick(self.clock + timedelta(minutes=2))
        self.assertEqual(len(self.published()), 1)

    async def test_nothing_without_an_adaptive_room(self):
        self.doc = config.loads(config.blank())
        await self.tick()
        self.assertEqual(self.ha.writes, [])


class LoopTest(RuntimeCase):
    async def test_start_runs_the_loop_and_stop_ends_it(self):
        runtime = AdaptiveRuntime(
            self.ha,
            Observer(self.ha, self.store, lambda: self.doc),
            self.store,
            lambda: self.doc,
            now=lambda: self.clock,
            tick_seconds=0.01,
        )
        await runtime.start()
        await runtime.start()  # once only
        for _ in range(50):
            await asyncio.sleep(0.01)
            if self.crossovers():
                break
        self.assertTrue(runtime.status()["running"])
        await runtime.stop()
        self.assertFalse(runtime.status()["running"])
        self.assertNotIn(runtime._on_connection, self.ha.listeners)
        self.assertEqual(self.crossovers(), ["day"])

    async def test_a_failing_tick_never_stops_the_loop(self):
        runtime = AdaptiveRuntime(
            self.ha,
            Observer(self.ha, self.store, lambda: self.doc),
            self.store,
            lambda: self.doc,
            now=lambda: self.clock,
            tick_seconds=0.01,
        )
        with (
            mock.patch.object(
                runtime, "tick", side_effect=[RuntimeError("once"), None, None, None, None, None]
            ) as tick,
            self.assertLogs("app.modules.adaptive.runtime", "ERROR"),
        ):
            await runtime.start()
            for _ in range(20):
                await asyncio.sleep(0.01)
                if tick.call_count > 2:
                    break
            await runtime.stop()
        self.assertGreater(tick.call_count, 2)
        self.assertIn("once", self.events("runtime")[0]["message"])


class WiringTest(unittest.TestCase):
    """The composition root makes a runtime only behind the cutover switch."""

    def app(self, directory):
        path = Path(directory) / "config.json"
        config.save(path, committed_config())
        return main.create_app(
            ha=FakeHA(automations=copy.deepcopy(SCENES)), config_path=path, observe=True
        )

    def test_off_there_is_no_runtime_and_nothing_adaptive_runs(self):
        self.assertIs(main.ADAPTIVE_ENABLED, False)
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            with TestClient(app, base_url="http://localhost") as client:
                self.assertIsNone(app.state.runtime)
                observer = app.state.observer
                self.assertIsNotNone(observer)
                # No hook: no section, no guard, no handler but core's own.
                self.assertIsNone(observer._section_provider)
                self.assertEqual(observer._guard_providers, [])
                self.assertEqual(observer._handlers, [])
                status = client.get("/api/status").json()
                self.assertEqual(status["adaptive"], {"wired": False})
                self.assertNotIn("adaptive_runtime", status)
                ha = app.state.ha
                # Nothing adaptive reached HA: no select, no scene, no hold, no almanac.
                self.assertEqual(ha.writes, [])

    def test_importing_and_making_the_app_starts_nothing(self):
        with mock.patch.object(AdaptiveRuntime, "start") as start, adaptive_wired():
            main.create_app(ha=FakeHA())
        start.assert_not_called()

    def test_on_the_lifespan_runs_it(self):
        with tempfile.TemporaryDirectory() as directory, adaptive_wired():
            app = self.app(directory)
            with TestClient(app, base_url="http://localhost") as client:
                runtime = app.state.runtime
                self.assertIsInstance(runtime, AdaptiveRuntime)
                self.assertEqual(app.state.observer._section_provider, runtime.section)
                status = client.get("/api/status").json()
                self.assertEqual(status["adaptive"], {"wired": True})
                self.assertIn("adaptive_runtime", status)
            self.assertIsNone(app.state.runtime)
            self.assertFalse(runtime.status()["running"])
        # And off again afterwards: the test's wiring is its own.
        self.assertEqual(switching.bridge_brightness_providers(), {})


if __name__ == "__main__":
    unittest.main()
