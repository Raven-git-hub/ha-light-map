"""Fixture readings for the analyser's tests, written to Light Map's store and to the
predecessor's (`tests/reference/`) schema alike, so the two analysers can be run on the
same input.

`readings(seed, ...)` makes a room's days of heartbeats (every 10 minutes, the section from
a fixed day plan, the bulbs drifting, ramping, switching off and on, the lux sensor sitting
at its 0 floor now and then, occupancy coming and going) and reactive events (some phantoms
whose changes are all under the min-delta, some on a day with no heartbeats). Times are local
to `TZ`; Light Map's store keeps them in UTC, the predecessor's as local time with its
offset, as each recorded them. Nothing here touches a network.
"""

import random
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc

from app.core.store import Store

TZ = ZoneInfo("Europe/London")
ROOM = "main_room"

# The day plan the fixtures file readings under: (section, from).
PLAN = (
    ("sunrise", time(5, 30)),
    ("day", time(10, 0)),
    ("afternoon", time(16, 0)),
    ("sunset", time(19, 0)),
    ("night", time(20, 30)),
    ("sleep", time(22, 0)),
)

# Just what tests/reference/ha_adaptive_light_analyser.py reads of the predecessor's schema.
PREDECESSOR_DDL = """
CREATE TABLE heartbeat (
    id INTEGER PRIMARY KEY, room_id TEXT, ts TEXT, local_date TEXT, section TEXT,
    ambient_lux REAL, occupied INTEGER, any_light_on INTEGER
);
CREATE TABLE heartbeat_group (
    heartbeat_id INTEGER, group_id TEXT, is_on INTEGER, brightness INTEGER
);
CREATE TABLE reactive (
    id INTEGER PRIMARY KEY, room_id TEXT, ts TEXT, local_date TEXT, section TEXT,
    lux_after REAL
);
CREATE TABLE reactive_group (
    reactive_id INTEGER, group_id TEXT, is_on_after INTEGER, brightness_after INTEGER,
    changed INTEGER
);
"""


def section_for(moment: datetime) -> str:
    current = "sleep"
    for name, start in PLAN:
        if moment.time() >= start:
            current = name
    return current


@dataclass
class Heartbeat:
    at: datetime  # local
    section: str
    lux: float | None
    occupied: bool
    bulbs: dict[str, tuple[bool, int | None]]  # bulb id -> (is_on, brightness)


@dataclass
class Reactive:
    at: datetime
    section: str
    lux_after: float | None
    bulbs: dict[str, tuple[bool, int | None, bool]]  # bulb id -> (is_on, brightness, changed)


@dataclass
class Fixture:
    bulbs: list[str]
    heartbeats: list[Heartbeat] = field(default_factory=list)
    reactive: list[Reactive] = field(default_factory=list)


def readings(
    seed: int,
    *,
    bulbs: Sequence[str] = ("tv_light", "play_area", "dining_area"),
    first: date = date(2026, 5, 20),
    days: int = 26,
    skip_days: Sequence[int] = (5, 17),
    off_brightness_zero: bool = True,
) -> Fixture:
    """A room's readings over `days` days from `first` (none on `skip_days`). An off bulb
    reads brightness 0 now and then (the n8n CSV's way) when `off_brightness_zero`, else
    always None (the observer's)."""
    rng = random.Random(seed)
    fixture = Fixture(list(bulbs))
    level = {b: rng.randint(40, 220) for b in bulbs}
    on = {b: rng.random() < 0.6 for b in bulbs}
    for d in range(days):
        day = first + timedelta(days=d)
        if d in skip_days:
            if rng.random() < 0.9:
                # A reactive event on a day with no heartbeats: the predecessor skips it.
                at = datetime.combine(day, time(12, 3), tzinfo=TZ)
                fixture.reactive.append(
                    Reactive(at, section_for(at), 30.0, {b: (True, 120, True) for b in bulbs})
                )
            continue
        # Stepped in UTC, so a day the clocks change on has its 23 or 25 hours.
        moment = datetime.combine(day, time(0, 0), tzinfo=TZ).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=TZ).astimezone(UTC)
        occupied = True
        while moment < end:
            here = moment.astimezone(TZ)
            if rng.random() < 0.05:
                occupied = not occupied
            for b in bulbs:
                roll = rng.random()
                if roll < 0.04:
                    on[b] = not on[b]
                elif roll < 0.25:
                    level[b] = max(1, min(255, level[b] + rng.choice([-30, -8, -2, 3, 9, 40])))
            lux = (
                None
                if rng.random() < 0.03
                else (0.0 if rng.random() < 0.15 else round(rng.uniform(2, 300), 1))
            )
            state = {}
            for b in bulbs:
                if on[b]:
                    state[b] = (True, level[b])
                else:
                    state[b] = (False, 0 if off_brightness_zero and rng.random() < 0.5 else None)
            fixture.heartbeats.append(Heartbeat(here, section_for(here), lux, occupied, state))
            if rng.random() < 0.02:
                # Now and then at the very instant of a heartbeat (the heartbeat sorts first).
                at = (moment + timedelta(minutes=rng.choice([0, 1, 4, 7]))).astimezone(TZ)
                changed_any = rng.random() < 0.8
                event = {}
                for b in bulbs:
                    if on[b] and rng.random() < 0.1:
                        event[b] = (False, None, changed_any)  # the user turned it off
                    elif on[b]:
                        event[b] = (
                            True,
                            min(255, max(1, level[b] + rng.choice([-50, 25]))),
                            changed_any and rng.random() < 0.7,
                        )
                    else:
                        event[b] = (False, None, False)
                fixture.reactive.append(Reactive(at, section_for(at), lux, event))
            moment += timedelta(minutes=10)
    return fixture


def to_store(fixture: Fixture, store: Store, room: str = ROOM) -> None:
    """Into Light Map's store, as the observer records: UTC timestamps, per bulb."""
    for hb in fixture.heartbeats:
        store.append_heartbeat(
            {
                "ts": hb.at,
                "room_id": room,
                "section": hb.section,
                "lux": hb.lux,
                "lux_sensors": 0 if hb.lux is None else 1,
                "occupancy": hb.occupied,
            },
            [
                {"bulb_id": b, "is_on": is_on, "brightness": brightness}
                for b, (is_on, brightness) in hb.bulbs.items()
            ],
        )
    for rx in fixture.reactive:
        store.append_reactive_event(
            {
                "ts": rx.at,
                "room_id": room,
                "section": rx.section,
                "window_seconds": 120,
                "lux_before": rx.lux_after,
                "lux_after": rx.lux_after,
                "occupancy": True,
            },
            [
                {
                    "bulb_id": b,
                    "is_on_before": is_on,
                    "is_on_after": is_on,
                    "brightness_before": brightness,
                    "brightness_after": brightness,
                    "changed": changed,
                }
                for b, (is_on, brightness, changed) in rx.bulbs.items()
            ],
        )


def to_predecessor(fixture: Fixture, room: str = ROOM) -> sqlite3.Connection:
    """Into the predecessor's schema, as it recorded: local timestamps, per group."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(PREDECESSOR_DDL)
    for hb in fixture.heartbeats:
        cur = conn.execute(
            "INSERT INTO heartbeat (room_id, ts, local_date, section, ambient_lux, occupied, "
            "any_light_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                room,
                hb.at.isoformat(),
                hb.at.date().isoformat(),
                hb.section,
                hb.lux,
                int(hb.occupied),
                int(any(is_on for is_on, _ in hb.bulbs.values())),
            ),
        )
        conn.executemany(
            "INSERT INTO heartbeat_group VALUES (?, ?, ?, ?)",
            [
                (cur.lastrowid, b, int(is_on), brightness)
                for b, (is_on, brightness) in hb.bulbs.items()
            ],
        )
    for rx in fixture.reactive:
        cur = conn.execute(
            "INSERT INTO reactive (room_id, ts, local_date, section, lux_after) VALUES (?, ?, ?, ?, ?)",
            (room, rx.at.isoformat(), rx.at.date().isoformat(), rx.section, rx.lux_after),
        )
        conn.executemany(
            "INSERT INTO reactive_group VALUES (?, ?, ?, ?, ?)",
            [
                (cur.lastrowid, b, int(is_on), brightness, int(changed))
                for b, (is_on, brightness, changed) in rx.bulbs.items()
            ],
        )
    conn.commit()
    return conn


def predecessor_scene_config(bulbs: Sequence[str]) -> dict[str, Any]:
    """Every group auto in every section, as the predecessor's analyser takes it."""
    return {
        section: {
            "lux_margin": 5,
            "max_step_pct": 0.04,
            "maintenance_enabled": section not in ("night", "sleep"),
            "groups": {b: {"mode": "auto"} for b in bulbs},
        }
        for section, _ in PLAN
    }
