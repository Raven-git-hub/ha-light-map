"""app.core.store: the CSV archive and the SQLite query layer over it, in a temp directory.
Round-trips (archive <-> query layer, and a rebuild that reproduces the query layer
exactly), validate-then-write, the recoveries (a failed database write, a deleted or broken
database, a write cut short), re-ingest of older or hand-edited files, and the event log."""

import csv
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.core import store as store_mod
from app.core.store import Store, StoreError, timestamp

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

HEARTBEAT = {
    "ts": "2026-10-16T12:00:00+00:00",
    "room_id": "living_room",
    "lux": 0.0,  # the floor: recorded as 0, never as "no reading"
    "lux_sensors": 1,
    "occupancy": True,
}
READINGS = [
    {"bulb_id": "chandelier_1", "is_on": True, "brightness": 128},
    {"bulb_id": "chandelier_2", "is_on": False, "brightness": None},
    {"bulb_id": "chandelier_3", "is_on": None, "brightness": None},  # unavailable
]
REACTIVE = {
    "ts": "2026-10-16T12:03:00+00:00",
    "room_id": "living_room",
    "window_seconds": 120,
    "lux_before": 42.5,
    "lux_after": 61.0,
    "occupancy": None,
}
CHANGES = [
    {
        "bulb_id": "chandelier_1",
        "is_on_before": True,
        "is_on_after": True,
        "brightness_before": 128,
        "brightness_after": 200,
        "changed": True,
    },
    {
        "bulb_id": "chandelier_2",
        "is_on_before": False,
        "is_on_after": False,
        "brightness_before": None,
        "brightness_after": None,
        "changed": False,
    },
]

# Everything the query layer holds, without the surrogate ids: what a rebuild must
# reproduce exactly.
CONTENT = {
    "heartbeat": "SELECT ts, room_id, section, lux, lux_sensors, occupancy, deferred_ms "
    "FROM heartbeat ORDER BY room_id, ts",
    "bulb_reading": "SELECT h.room_id, h.ts, r.bulb_id, r.is_on, r.brightness FROM bulb_reading r "
    "JOIN heartbeat h ON h.id = r.heartbeat_id ORDER BY h.room_id, h.ts, r.bulb_id",
    "reactive_event": "SELECT ts, room_id, section, window_seconds, lux_before, lux_after, "
    "occupancy FROM reactive_event ORDER BY room_id, ts",
    "reactive_change": "SELECT e.room_id, e.ts, c.bulb_id, c.is_on_before, c.is_on_after, "
    "c.brightness_before, c.brightness_after, c.changed FROM reactive_change c "
    "JOIN reactive_event e ON e.id = c.reactive_event_id ORDER BY e.room_id, e.ts, c.bulb_id",
    "almanac": "SELECT ts, room_id, section, learning_unit_id, lux_target, lux_margin, "
    "maintenance_enabled, max_step_pct, trust_weight, valid_from, mode FROM almanac ORDER BY id",
    "event_log": "SELECT ts, level, category, room_id, message, detail FROM event_log ORDER BY id",
}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data = Path(self.dir.name)
        self.now = NOW
        self.store = self.open()

    def open(self, **kwargs):
        store = Store(self.data, now=lambda: self.now, **kwargs)
        store.open()
        self.addCleanup(store.close)
        return store

    def reopen(self, **kwargs):
        self.store.close()
        self.store = self.open(**kwargs)
        return self.store

    def content(self, store=None):
        store = store or self.store
        return {table: store.query(sql) for table, sql in CONTENT.items()}

    def archive(self, table):
        rows = []
        for path in sorted((self.data / "archive" / table).glob("*.csv")):
            with path.open(newline="") as handle:
                rows += list(csv.DictReader(handle))
        return rows

    def fill(self):
        self.store.append_heartbeat(HEARTBEAT, READINGS)
        self.store.append_heartbeat(
            {"ts": "2026-10-16T12:10:00+00:00", "room_id": "kitchen", "deferred_ms": 1500},
            [{"bulb_id": "kitchen_strip", "is_on": True, "brightness": 255}],
        )
        self.store.append_reactive_event(REACTIVE, CHANGES)
        self.store.append_almanac(
            [
                {
                    "room_id": "living_room",
                    "section": "evening",
                    "learning_unit_id": "chandelier",
                    "lux_target": 80.0,
                    "maintenance_enabled": True,
                    "mode": "learning",
                }
            ]
        )
        self.store.log_event(
            "info", "reactive", "Manual change", room_id="living_room", detail={"bulbs": ["a"]}
        )


class WriteTest(StoreCase):
    def test_a_heartbeat_and_its_readings_land_in_both_layers(self):
        heartbeat_id = self.store.append_heartbeat(HEARTBEAT, READINGS)
        self.assertEqual(
            self.store.query("SELECT * FROM heartbeat"),
            [
                {
                    "id": heartbeat_id,
                    "ts": "2026-10-16T12:00:00.000000+00:00",
                    "room_id": "living_room",
                    "section": None,
                    "lux": 0.0,
                    "lux_sensors": 1,
                    "occupancy": 1,
                    "deferred_ms": 0,
                }
            ],
        )
        self.assertEqual(
            self.store.query("SELECT bulb_id, is_on, brightness, heartbeat_id FROM bulb_reading"),
            [
                {"bulb_id": "chandelier_1", "is_on": 1, "brightness": 128, "heartbeat_id": 1},
                {"bulb_id": "chandelier_2", "is_on": 0, "brightness": None, "heartbeat_id": 1},
                {"bulb_id": "chandelier_3", "is_on": None, "brightness": None, "heartbeat_id": 1},
            ],
        )
        # The archive mirrors the tables, a reading naming its heartbeat by (room, ts).
        self.assertEqual(
            self.archive("heartbeat"),
            [
                {
                    "ts": "2026-10-16T12:00:00.000000+00:00",
                    "room_id": "living_room",
                    "section": "",
                    "lux": "0.0",
                    "lux_sensors": "1",
                    "occupancy": "1",
                    "deferred_ms": "0",
                }
            ],
        )
        self.assertEqual(
            [list(r.values()) for r in self.archive("bulb_reading")],
            [
                ["2026-10-16T12:00:00.000000+00:00", "living_room", "chandelier_1", "1", "128"],
                ["2026-10-16T12:00:00.000000+00:00", "living_room", "chandelier_2", "0", ""],
                ["2026-10-16T12:00:00.000000+00:00", "living_room", "chandelier_3", "", ""],
            ],
        )

    def test_files_go_by_utc_month(self):
        # 00:30 on 1 November in UTC+1 is still October in UTC.
        self.store.append_heartbeat({"ts": "2026-11-01T00:30:00+01:00", "room_id": "a"}, [])
        self.store.append_heartbeat({"ts": "2026-11-01T00:30:00+00:00", "room_id": "a"}, [])
        names = sorted(p.name for p in (self.data / "archive" / "heartbeat").iterdir())
        self.assertEqual(names, ["2026-10.csv", "2026-11.csv"])
        self.assertEqual(
            [r["ts"] for r in self.store.query("SELECT ts FROM heartbeat ORDER BY ts")],
            ["2026-10-31T23:30:00.000000+00:00", "2026-11-01T00:30:00.000000+00:00"],
        )

    def test_the_timestamp_defaults_to_now(self):
        self.store.log_event("info", "observer", "Started")
        self.assertEqual(
            self.store.query("SELECT ts FROM event_log"),
            [{"ts": "2026-10-16T12:00:00.000000+00:00"}],
        )

    def test_a_row_that_does_not_fit_is_refused_before_anything_is_written(self):
        for heartbeat, readings in (
            (HEARTBEAT, [{"bulb_id": "a", "is_on": True, "brightness": 300}]),
            (HEARTBEAT, [{"bulb_id": "a", "is_on": "yes"}]),
            (HEARTBEAT, [{"bulb_id": "a"}, {"bulb_id": "a"}]),
            ({**HEARTBEAT, "lux": -1}, []),
            ({**HEARTBEAT, "lux": float("nan")}, []),
            ({**HEARTBEAT, "room_id": ""}, []),
            ({**HEARTBEAT, "colour": "blue"}, []),
            ({**HEARTBEAT, "ts": "2026-10-16T12:00:00"}, []),  # naive: which zone?
            ({**HEARTBEAT, "ts": "yesterday"}, []),
        ):
            with self.subTest(heartbeat=heartbeat, readings=readings):
                with self.assertRaises(ValueError):
                    self.store.append_heartbeat(heartbeat, readings)
        with self.assertRaises(ValueError):
            self.store.log_event("fatal", "observer", "x")
        with self.assertRaises(ValueError):
            self.store.log_event("info", "observer", "")
        with self.assertRaises(ValueError):
            self.store.append_almanac([{"room_id": "a", "section": "day"}])
        self.assertFalse(any((self.data / "archive").rglob("*.csv")))
        self.assertEqual(self.content(), {table: [] for table in CONTENT})

    def test_a_heartbeat_is_recorded_once(self):
        self.store.append_heartbeat(HEARTBEAT, READINGS)
        with self.assertRaises(ValueError):
            self.store.append_heartbeat(HEARTBEAT, READINGS)
        self.assertEqual(len(self.archive("heartbeat")), 1)
        self.assertEqual(len(self.archive("bulb_reading")), 3)

    def test_the_almanac_is_insert_only(self):
        snapshot = {"room_id": "a", "section": "day", "learning_unit_id": "u", "lux_target": 50}
        self.store.append_almanac([snapshot])
        self.now += timedelta(days=1)
        self.store.append_almanac([{**snapshot, "lux_target": 60}])
        self.assertEqual(
            [r["lux_target"] for r in self.store.query("SELECT * FROM almanac ORDER BY ts")],
            [50.0, 60.0],
        )

    def test_queries_are_read_only(self):
        self.store.append_heartbeat(HEARTBEAT, READINGS)
        for sql in (
            "DELETE FROM bulb_reading",
            "INSERT INTO event_log VALUES (9, '', '', '', '', '', '')",
        ):
            with self.subTest(sql), self.assertRaises(sqlite3.OperationalError):
                self.store.query(sql)
        self.assertEqual(len(self.store.query("SELECT * FROM bulb_reading")), 3)
        self.store.log_event("info", "observer", "still writable")  # the store's own writes

    def test_a_closed_store_says_so(self):
        store = Store(self.data)
        with self.assertRaises(StoreError):
            store.log_event("info", "observer", "x")
        with self.assertRaises(StoreError):
            store.query("SELECT 1")


class RebuildTest(StoreCase):
    def test_a_rebuild_reproduces_the_query_layer(self):
        self.fill()
        before = self.content()
        counts = self.store.rebuild_from_csv()
        self.assertEqual(self.content(), before)
        self.assertEqual(
            counts,
            {
                "heartbeat": 2,
                "bulb_reading": 4,
                "reactive_event": 1,
                "reactive_change": 2,
                "almanac": 1,
                "event_log": 1,
                "skipped": 0,
            },
        )

    def test_a_deleted_database_is_rebuilt_at_startup(self):
        self.fill()
        before = self.content()
        self.store.close()
        (self.data / store_mod.DB_NAME).unlink()
        with self.assertLogs("app.core.store", "WARNING") as logs:
            store = self.reopen()
        self.assertIn("the query layer is empty", logs.output[0])
        after = self.content(store)
        # The rebuild is itself logged, after everything it restored.
        self.assertEqual(after["event_log"][:-1], before["event_log"])
        self.assertIn("Rebuilt the query layer", after["event_log"][-1]["message"])
        self.assertEqual(
            {k: v for k, v in after.items() if k != "event_log"},
            {k: v for k, v in before.items() if k != "event_log"},
        )

    def test_an_unchanged_archive_is_not_rebuilt(self):
        self.fill()
        before = self.content()
        with mock.patch.object(Store, "rebuild_from_csv") as rebuild:
            self.reopen()
        rebuild.assert_not_called()
        self.assertEqual(self.content(), before)

    def test_a_failed_database_write_costs_nothing(self):
        self.store.append_heartbeat(HEARTBEAT, READINGS)
        with (
            mock.patch.object(Store, "_insert", side_effect=sqlite3.OperationalError("disk I/O")),
            self.assertLogs("app.core.store", "ERROR"),
        ):
            result = self.store.append_heartbeat(
                {**HEARTBEAT, "ts": "2026-10-16T12:10:00+00:00"}, READINGS
            )
        self.assertIsNone(result)
        self.assertTrue(self.store.stale)
        self.assertEqual(len(self.archive("heartbeat")), 2)  # the archive has it
        self.assertEqual(len(self.store.query("SELECT * FROM heartbeat")), 1)
        with self.assertLogs("app.core.store", "WARNING") as logs:
            store = self.reopen()
        self.assertIn("the archive holds rows the query layer doesn't", logs.output[0])
        self.assertFalse(store.stale)
        self.assertEqual(len(store.query("SELECT * FROM heartbeat")), 2)
        self.assertEqual(len(store.query("SELECT * FROM bulb_reading")), 6)

    def test_a_broken_database_is_moved_aside_and_rebuilt(self):
        self.fill()
        before = self.content()
        self.store.close()
        for name in (store_mod.DB_NAME + "-wal", store_mod.DB_NAME + "-shm"):
            (self.data / name).unlink(missing_ok=True)
        (self.data / store_mod.DB_NAME).write_bytes(b"this is not a database" * 100)
        with self.assertLogs("app.core.store", "WARNING"):
            store = self.reopen()
        self.assertEqual(self.content(store)["heartbeat"], before["heartbeat"])
        aside = [p.name for p in self.data.iterdir() if ".broken-" in p.name]
        self.assertEqual(aside, ["light_map.db.broken-20261016T120000Z"])

    def test_readings_whose_heartbeat_never_landed_are_skipped(self):
        # A write cut short after the readings (written first) and before the heartbeat.
        self.store.append_heartbeat(HEARTBEAT, READINGS)
        path = self.data / "archive" / "bulb_reading" / "2026-10.csv"
        with path.open("a", newline="") as handle:
            csv.writer(handle).writerow(
                ["2026-10-16T12:10:00.000000+00:00", "living_room", "chandelier_1", "1", "90"]
            )
        with self.assertLogs("app.core.store", "WARNING"):
            store = self.reopen()
        self.assertEqual(len(store.query("SELECT * FROM heartbeat")), 1)
        self.assertEqual(len(store.query("SELECT * FROM bulb_reading")), 3)

    def test_a_hand_edited_line_is_skipped_not_fatal(self):
        self.fill()
        path = self.data / "archive" / "event_log" / "2026-10.csv"
        with path.open("a", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["2026-10-16T13:00:00+00:00", "fatal", "x", "", "bad level", ""])
            writer.writerow(["not a time", "info", "x", "", "bad ts", ""])
            writer.writerow(["2026-10-16T13:00:00+00:00", "info", "x", "", "fine", ""])
        with self.assertLogs("app.core.store", "WARNING"):
            counts = self.store.rebuild_from_csv()
        self.assertEqual(counts["skipped"], 2)
        self.assertEqual(
            [r["message"] for r in self.store.query("SELECT message FROM event_log")],
            ["Manual change", "fine"],
        )

    def test_a_file_with_other_columns_is_never_appended_to(self):
        # An archive file from a version that had fewer heartbeat columns.
        old = self.data / "archive" / "heartbeat" / "2026-10.csv"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("ts,room_id,lux\n2026-10-16T11:00:00+00:00,living_room,12.5\n")
        with self.assertLogs("app.core.store", "WARNING"):
            store = self.reopen()
        with self.assertLogs("app.core.store", "WARNING") as logs:
            store.append_heartbeat(HEARTBEAT, [])
        self.assertIn("columns changed", logs.output[0])
        self.assertEqual(old.read_text().count("\n"), 2)  # untouched
        self.assertTrue((old.parent / "2026-10.1.csv").exists())
        store.rebuild_from_csv()
        self.assertEqual(
            store.query("SELECT ts, lux, lux_sensors, deferred_ms FROM heartbeat ORDER BY ts"),
            [
                # Read by its own header: the columns it lacks take their defaults.
                {
                    "ts": "2026-10-16T11:00:00.000000+00:00",
                    "lux": 12.5,
                    "lux_sensors": 0,
                    "deferred_ms": 0,
                },
                {
                    "ts": "2026-10-16T12:00:00.000000+00:00",
                    "lux": 0.0,
                    "lux_sensors": 1,
                    "deferred_ms": 0,
                },
            ],
        )


class EventLogTest(StoreCase):
    def setUp(self):
        super().setUp()
        for level, category, room, message in (
            ("debug", "heartbeat", None, "tick"),
            ("info", "reactive", "living_room", "manual change"),
            ("warning", "heartbeat", "kitchen", "skipped"),
            ("error", "connection", None, "token rejected"),
        ):
            self.now += timedelta(minutes=1)
            self.store.log_event(level, category, message, room_id=room, detail={"n": 1})

    def messages(self, **filters):
        return [e["message"] for e in self.store.events(**filters)]

    def test_newest_first_and_filtered(self):
        self.assertEqual(self.messages(), ["token rejected", "skipped", "manual change", "tick"])
        self.assertEqual(self.messages(level="warning"), ["token rejected", "skipped"])
        self.assertEqual(self.messages(category="heartbeat"), ["skipped", "tick"])
        self.assertEqual(self.messages(room_id="kitchen"), ["skipped"])
        self.assertEqual(self.messages(limit=2), ["token rejected", "skipped"])
        newest = self.store.events(limit=1)[0]
        self.assertEqual(self.messages(before=newest["id"], limit=1), ["skipped"])
        self.assertEqual(newest["detail"], {"n": 1})
        with self.assertRaises(ValueError):
            self.store.events(level="fatal")

    def test_facets(self):
        self.assertEqual(
            self.store.event_facets(),
            {
                "categories": ["connection", "heartbeat", "reactive"],
                "rooms": ["kitchen", "living_room"],
            },
        )

    def test_retention_prunes_the_query_layer_only(self):
        self.store.retention_days = 1
        # Entries were logged at +1..+4 minutes; a day after +2.5, the first two are old.
        self.now = NOW + timedelta(days=1, minutes=2, seconds=30)
        self.assertEqual(self.store.prune_event_log(), 2)
        self.assertEqual(self.messages(), ["token rejected", "skipped"])
        self.assertEqual(len(self.archive("event_log")), 4)  # the archive keeps everything
        # A rebuild leaves out what retention would have pruned.
        self.store.rebuild_from_csv()
        self.assertEqual(self.messages(), ["token rejected", "skipped"])


class TimestampTest(unittest.TestCase):
    def test_one_format(self):
        self.assertEqual(timestamp("2026-10-16T13:00:00+01:00"), "2026-10-16T12:00:00.000000+00:00")
        self.assertEqual(
            timestamp(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)),
            "2026-01-02T03:04:05.000006+00:00",
        )
        for bad in ("2026-10-16 12:00", datetime(2026, 10, 16), 12):
            with self.subTest(bad), self.assertRaises(ValueError):
                timestamp(bad)


if __name__ == "__main__":
    unittest.main()
